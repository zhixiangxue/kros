"""Engine: the in-daemon half of the lightpanda_mcp driver.

Lives in the long-running daemon process. Owns one ``lightpanda mcp``
child over stdio and implements the 14 :class:`BrowseDriver` methods
by translating each into an MCP ``tools/call`` JSON-RPC message.

The proxy (CLI-side) never imports this module. Only ``daemon.py``
(which runs inside the daemon process) does.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Optional

from kros.commands.browse.contract import (
    ENV_LIGHTPANDA_BIN,
    DriverError,
    Element,
    FindResult,
    NavigationTimeoutError,
    PageState,
    ReadResult,
    SessionInfo,
)

log = logging.getLogger(__name__)


# Size budget for read().markdown — soft cap to keep agent tokens sane.
_READ_MARKDOWN_MAX_BYTES = 32 * 1024


# ---------------------------------------------------------------------------
# fyle fallback — non-HTML resource handling
# ---------------------------------------------------------------------------
#
# lightpanda can *navigate* to non-HTML resources (PDF, DOCX, images, audio,
# ...) — location.href updates correctly — but it cannot render them: the
# markdown tool comes back empty. fyle, the engine behind `kros read`, has
# dedicated readers for exactly these formats. When read() lands on such a
# URL with empty markdown, we delegate to fyle so the agent sees the
# document content in the same round-trip it made the action.
#
# The whitelist is intentionally narrow: only formats where fyle is certain
# to help AND lightpanda is certain to fail. HTML / Markdown / plain text
# / source code are deliberately excluded — lightpanda's rendered DOM
# (post-JS) is a strictly better input for those than fyle's static
# extractor. Source of truth: fyle/_core/sniffer.py::_EXT_MAP entries that
# route to non-text readers.
_FYLE_FALLBACK_EXTS: frozenset[str] = frozenset(
    {
        # Office documents
        "pdf", "docx", "xlsx", "pptx",
        # Images (fyle emits base64 data: URLs for multimodal models)
        "png", "jpg", "jpeg", "webp",
        # Audio — requires fylepy[audio] extra; if missing, fyle raises
        # a clear "install this extra" error which we surface as-is.
        "mp3", "m4a", "wav", "flac", "ogg",
        # Video — requires fylepy[video] extra.
        "mp4", "m4v", "mov", "avi", "mkv", "webm",
        # Structured data
        "csv", "db", "sqlite", "sqlite3",
        # Archive containers
        "zip", "tar", "gz", "tgz", "bz2", "xz",
    }
)

# Match the final path-segment extension of a URL, stripping ?query/#fragment.
_URL_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,8})(?:[?#]|$)")


# ---------------------------------------------------------------------------
# Low-level MCP stdio client
# ---------------------------------------------------------------------------


class _MCPStdioClient:
    """Minimal MCP client over a child process's stdio.

    JSON-RPC 2.0 with line-delimited JSON framing (no LSP-style
    ``Content-Length`` headers). One client owns one child.
    """

    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._stderr_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        resolved = shutil.which(self._binary) or self._binary
        if not os.path.isabs(resolved) and shutil.which(resolved) is None:
            raise DriverError(
                f"lightpanda binary not found: {self._binary!r}. "
                f"Put it on PATH or set {ENV_LIGHTPANDA_BIN}=<abs-path>."
            )
        self._proc = subprocess.Popen(
            [resolved, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="lightpanda-mcp-stderr"
        )
        self._stderr_thread.start()

        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kros-browse", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # --- JSON-RPC primitives ------------------------------------------

    def _request(self, method: str, params: dict) -> Any:
        assert self._proc and self._proc.stdin and self._proc.stdout
        rpc_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})

        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise DriverError(
                    f"lightpanda mcp closed stdout before responding to {method!r}"
                )
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-JSON stdout line: %r", line.rstrip())
                continue
            if resp.get("id") != rpc_id:
                continue
            if "error" in resp:
                err = resp["error"]
                raise DriverError(
                    f"{method}: {err.get('message', err)!s} (code={err.get('code')})"
                )
            return resp.get("result")

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            log.debug("lightpanda: %s", line.rstrip())

    def call(self, tool: str, arguments: dict) -> Any:
        """tools/call, then unwrap ``content[0].text`` (JSON-decoded if possible)."""
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            content = result.get("content") or []
            msg = content[0].get("text", "<no detail>") if content else "<no detail>"
            raise DriverError(f"tool {tool!r} returned error: {msg}")
        content = result.get("content") or []
        if not content:
            return None
        first = content[0]
        text = first.get("text")
        if text is None:
            return first
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text


# ---------------------------------------------------------------------------
# Engine: implements the 14 BrowseDriver methods over _MCPStdioClient.
# ---------------------------------------------------------------------------


class LightpandaMCPEngine:
    """Daemon-side implementation. Not exported as a driver itself.

    A proxy (see ``proxy.py``) satisfies ``BrowseDriver`` on the CLI side;
    it forwards each call over unix socket; the daemon dispatches here.
    """

    name = "lightpanda_mcp"

    def __init__(self) -> None:
        binary = os.environ.get(ENV_LIGHTPANDA_BIN, "lightpanda")
        self._mcp = _MCPStdioClient(binary=binary)
        self._mcp.spawn()
        self._url: str = ""
        self._title: str = ""
        # Element metadata cache, keyed by ref (= backendNodeId). Filled
        # by read() and find(); consulted by click() to recover from a
        # confirmed lightpanda limitation: its `click` MCP tool only
        # dispatches the click event and does NOT execute the default
        # action of <a href> (verified via tmp/probe_lightpanda_click.py
        # — clicking a link leaves location.href unchanged). When that
        # happens we fall back to `goto(href)` using the cached element.
        self._elements_by_ref: dict[int, Element] = {}

    # --- tier 1 -------------------------------------------------------

    def open(self, url: str, *, timeout_ms: int = 5000) -> ReadResult:
        # NOTE: we deliberately do NOT pass waitUntil here.
        # lightpanda's default is 'done' (more lenient than 'load', which
        # waits for every subresource); heavy pages like duckduckgo/html
        # routinely OperationTimedout on 'load'.
        self._mcp.call("goto", {"url": url, "timeout": timeout_ms})
        self._refresh_state_from_eval(fallback_url=url)
        # New page → previous backendNodeIds are stale; clear cache so
        # click() never falls back using a ref from the prior DOM.
        self._elements_by_ref.clear()

        # lightpanda reports isError=false + "Navigated successfully."
        # even when stderr shows OperationTimedout. The only reliable
        # signal that navigation actually happened is location.href
        # moving off about:blank. Surface timeout as a distinct exception
        # so agents can retry with a larger --timeout vs. giving up.
        if self._url in ("", "about:blank") and url not in ("", "about:blank"):
            raise NavigationTimeoutError(
                f"open({url!r}) did not complete within {timeout_ms}ms "
                f"(page still at {self._url!r}). This tab is now in "
                f"an unrecoverable state and will be closed — re-open "
                f"{url!r} with a larger --timeout, or inspect "
                f"~/.kros/browse/tabs/*/daemon.log for network/engine "
                f"errors."
            )

        # Return a full snapshot so the caller gets page content in one
        # round-trip — no separate `read` needed just to see what loaded.
        return self.read()

    def read(self, *, selector: Optional[str] = None) -> ReadResult:
        if selector is not None:
            raise DriverError(
                "read --selector is not implemented yet; call read without "
                "--selector and grep/jq the markdown, or use find/wait."
            )
        md = self._mcp.call("markdown", {}) or ""
        if not isinstance(md, str):
            md = str(md)
        truncated = False
        if len(md.encode("utf-8")) > _READ_MARKDOWN_MAX_BYTES:
            md = md.encode("utf-8")[:_READ_MARKDOWN_MAX_BYTES].decode(
                "utf-8", errors="ignore"
            )
            truncated = True
        raw_elems = self._mcp.call("interactiveElements", {}) or []
        elements = [_parse_element(e) for e in _ensure_list(raw_elems)]
        self._refresh_state_from_eval()
        # Refresh element metadata cache so click() can fall back via
        # goto(href) when lightpanda's click tool fails to navigate.
        self._elements_by_ref = {e.ref: e for e in elements}
        result = ReadResult(
            url=self._url,
            title=self._title,
            markdown=md,
            elements=elements,
            truncated=truncated,
        )
        # Non-HTML resource hook: when lightpanda produced nothing and
        # the URL is a format fyle handles, parse it inline. Part of the
        # driver's BrowseDriver contract ("read returns page content"):
        # the CLI stays dumb and format-agnostic.
        return self._maybe_fyle_fallback(result)

    def click(self, *, ref: int, timeout_ms: int = 5000) -> ReadResult:
        # lightpanda's click tool reply is human-prose
        # ("Clicked element ... Page url: ..., title: ..."), so we cannot
        # parse it as JSON. We rely on location.href via evaluate to
        # observe whether navigation actually happened.
        pre_url = self._url
        self._mcp.call("click", {"backendNodeId": ref})
        self._refresh_state_from_eval()

        # Confirmed lightpanda limitation (tmp/probe_lightpanda_click.py
        # + tmp/probe_click_async_follow.py): `click` only dispatches the
        # event; it does NOT execute the default action of <a href>,
        # even after waiting 20s. If the URL didn't move and the target
        # is a link with an href, navigate explicitly via goto.
        if self._url != pre_url:
            # Rare path: click did mutate location (e.g. a JS handler).
            # Return a full snapshot so the agent sees the new page.
            return self.read()
        elem = self._elements_by_ref.get(ref)
        if elem is None or elem.role != "link" or not elem.href:
            # Nothing to fall back to. Still snapshot so any in-page
            # DOM change from the click handler (menus, dialogs) is
            # visible without a separate `read`.
            return self.read()

        target = elem.href
        log.info(
            "click(ref=%d): lightpanda did not follow link; "
            "falling back to goto(%s) with timeout=%dms",
            ref,
            target,
            timeout_ms,
        )
        # Use the caller-supplied timeout verbatim — the agent chose the
        # budget. No internal 30s hardcode: that was hiding real cost
        # from the agent and made retries impossible.
        try:
            self._mcp.call("goto", {"url": target, "timeout": timeout_ms})
        except DriverError as e:
            # Do not try to restore: any "rollback" would leave the tab
            # in a silently-inconsistent state (about:blank or a half-
            # loaded DOM) while reusing stale backendNodeIds, which
            # lightpanda reports later as "Node is not an HTML element".
            # Honest signal beats a best-effort rollback: the proxy
            # layer will close this tab, and the agent re-opens fresh.
            raise DriverError(
                f"click(ref={ref}): goto({target!r}) failed: {e}. "
                f"This tab is now in an unrecoverable state and will "
                f"be closed — re-open {pre_url!r} (or a new url) to "
                f"continue. If the resource isn't HTML, try "
                f"`kros read {target}` directly."
            ) from e

        self._elements_by_ref.clear()
        self._refresh_state_from_eval(fallback_url=target)

        # lightpanda's goto tool returns "Navigated successfully" even on
        # timeout (documented known behavior). The only reliable failure
        # signals are location.href moving to about:blank, or the
        # markdown tool returning a "Navigation failed" notice.
        if self._navigation_failed(self._url):
            raise NavigationTimeoutError(
                f"click(ref={ref}): navigation to {target!r} did not "
                f"complete within {timeout_ms}ms. This tab is now in "
                f"an unrecoverable state and will be closed — re-open "
                f"{pre_url!r} (or {target!r}) with a larger --timeout, "
                f"or try `kros read {target}` / `curl` if the resource "
                f"isn't HTML."
            )
        # Navigation succeeded — hand back a complete snapshot of the
        # destination page in one round-trip (markdown + fresh refs).
        return self.read()

    def fill(self, *, ref: int, value: str) -> ReadResult:
        self._mcp.call("fill", {"backendNodeId": ref, "text": value})
        # fill can trigger in-page validation / dynamic forms — return
        # a full snapshot so the agent sees the resulting state.
        return self.read()

    def close(self) -> None:
        self._mcp.close()

    def info(self) -> SessionInfo:
        return SessionInfo(
            alive=self._mcp.pid is not None,
            url=self._url,
            title=self._title,
            driver=self.name,
            browser_pid=self._mcp.pid,
        )

    # --- tier 2 -------------------------------------------------------

    def find(
        self, *, role: Optional[str] = None, name: Optional[str] = None
    ) -> FindResult:
        args: dict[str, Any] = {}
        if role is not None:
            args["role"] = role
        if name is not None:
            args["name"] = name
        raw = self._mcp.call("findElement", args) or []
        elements = [_parse_element(e) for e in _ensure_list(raw)]
        # Merge into cache (find returns a subset of the page; do not wipe
        # other refs that read() may have populated).
        for e in elements:
            self._elements_by_ref[e.ref] = e
        return FindResult(elements=elements)

    def wait(self, *, selector: str, timeout_ms: int = 5000) -> int:
        res = self._mcp.call(
            "waitForSelector", {"selector": selector, "timeout": timeout_ms}
        )
        if isinstance(res, dict) and "backendNodeId" in res:
            return int(res["backendNodeId"])
        raise DriverError(f"waitForSelector returned unexpected shape: {res!r}")

    def scroll(
        self,
        *,
        ref: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> ReadResult:
        args: dict[str, Any] = {}
        if ref is not None:
            args["backendNodeId"] = ref
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        self._mcp.call("scroll", args)
        # Scrolling exposes new elements into view; a full read gives
        # the agent fresh refs + the newly-visible markdown section.
        return self.read()

    def eval(self, *, script: str) -> str:
        res = self._mcp.call("evaluate", {"script": script})
        if res is None:
            return ""
        return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)

    # --- tier 3 -------------------------------------------------------

    def press(self, *, key: str, ref: Optional[int] = None) -> ReadResult:
        args: dict[str, Any] = {"key": key}
        if ref is not None:
            args["backendNodeId"] = ref
        self._mcp.call("press", args)
        # press often submits forms / triggers hotkeys — snapshot so
        # the agent sees the resulting page in one round-trip.
        return self.read()

    def hover(self, *, ref: int) -> ReadResult:
        self._mcp.call("hover", {"backendNodeId": ref})
        # hover reveals tooltips / dropdown menus; new refs need to
        # be surfaced to the agent.
        return self.read()

    def select(self, *, ref: int, value: str) -> ReadResult:
        self._mcp.call("selectOption", {"backendNodeId": ref, "value": value})
        # select can trigger cascading form updates.
        return self.read()

    def check(self, *, ref: int, checked: bool) -> ReadResult:
        self._mcp.call("setChecked", {"backendNodeId": ref, "checked": checked})
        # check can toggle dependent form fields.
        return self.read()

    # --- helpers ------------------------------------------------------

    def _state_from(self, res: Any) -> PageState:
        # Retained for backward-compat with callers that expect a
        # PageState built purely from a tool response. New mutating ops
        # should prefer _refresh_state_from_eval — lightpanda tool
        # replies are unreliable about including url/title (see click).
        if isinstance(res, dict):
            url = res.get("url")
            title = res.get("title")
            if isinstance(url, str):
                self._url = url
            if isinstance(title, str):
                self._title = title
        return PageState(url=self._url, title=self._title)

    def _refresh_state_from_eval(self, *, fallback_url: str = "") -> PageState:
        try:
            href = self._mcp.call("evaluate", {"script": "location.href"})
            if isinstance(href, str) and href:
                self._url = href
        except DriverError:
            if fallback_url:
                self._url = fallback_url
        try:
            title = self._mcp.call("evaluate", {"script": "document.title"})
            if isinstance(title, str):
                self._title = title
        except DriverError:
            pass
        return PageState(url=self._url, title=self._title)

    @staticmethod
    def _navigation_failed(url: str) -> bool:
        # about:blank after a goto means lightpanda bailed (typically
        # OperationTimedout). Empty url is a similar "nothing loaded"
        # signal. Either way the tab is not on the target page.
        return url in ("", "about:blank")

    def _maybe_fyle_fallback(self, result: ReadResult) -> ReadResult:
        """If lightpanda returned empty markdown for a URL whose extension
        is in :data:`_FYLE_FALLBACK_EXTS`, re-parse it through fyle.

        Trigger — both must hold:

        1. ``result.markdown`` is blank (lightpanda rendered nothing), AND
        2. URL ends in an extension from the fyle whitelist.

        Anything else is left alone, so this never masks a legitimate
        "page loaded but is empty" signal (SPA first paint, login
        redirect to /, etc.).
        """
        if result.markdown.strip():
            return result
        m = _URL_EXT_RE.search(result.url)
        if not m:
            return result
        ext = m.group(1).lower()
        if ext not in _FYLE_FALLBACK_EXTS:
            return result

        # Lazy import: fyle pulls PDF/OCR/ASR toolchains and we want
        # browse to boot fast. The PyPI package is ``fylepy``; the
        # import name is ``fyle``. fylepy is a hard kros dependency so
        # ImportError should not happen in a normal install — but we
        # degrade gracefully if someone has dismantled it.
        try:
            import fyle  # type: ignore
        except ImportError as e:
            return result.model_copy(
                update={
                    "markdown": (
                        f"(non-HTML resource at {result.url}; install "
                        f"`fylepy` to parse it inline, or run "
                        f"`kros read {result.url}` directly. {e})"
                    )
                }
            )

        try:
            # fyle.read is sugar for str(fyle.open(url)) — returns an
            # LLM-ready header + markdown for a URL in one call.
            md = fyle.read(result.url)
        except Exception as e:  # UnsupportedFormat / Parse / Download / ...
            return result.model_copy(
                update={
                    "markdown": (
                        f"(non-HTML resource at {result.url}; fyle "
                        f"fallback failed: {type(e).__name__}: {e}. Try "
                        f"`kros read {result.url}` directly.)"
                    )
                }
            )

        # Re-check the size budget: fyle documents (esp. transcripts /
        # large PDFs) can easily exceed the 32 KiB markdown cap. Apply
        # the same trim we do for the lightpanda branch.
        truncated = result.truncated
        if len(md.encode("utf-8")) > _READ_MARKDOWN_MAX_BYTES:
            md = md.encode("utf-8")[:_READ_MARKDOWN_MAX_BYTES].decode(
                "utf-8", errors="ignore"
            )
            truncated = True
        return result.model_copy(update={"markdown": md, "truncated": truncated})


def _ensure_list(x: Any) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for key in ("items", "elements", "results"):
            v = x.get(key)
            if isinstance(v, list):
                return v
    return []


def _parse_element(raw: Any) -> Element:
    if not isinstance(raw, dict):
        raise DriverError(f"unexpected element payload: {raw!r}")
    ref = raw.get("backendNodeId")
    if ref is None:
        raise DriverError(f"element missing backendNodeId: {raw!r}")
    return Element(
        ref=int(ref),
        role=str(raw.get("role") or ""),
        name=str(raw.get("name") or ""),
        type=raw.get("inputType") or raw.get("type"),
        value=raw.get("value"),
        checked=raw.get("checked"),
        href=raw.get("href"),
        placeholder=raw.get("placeholder"),
        disabled=raw.get("disabled") if raw.get("disabled") else None,
    )
