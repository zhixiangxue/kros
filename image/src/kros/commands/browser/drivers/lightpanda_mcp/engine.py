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
import shutil
import subprocess
import threading
from typing import Any, Optional

from kros.commands.browser.contract import (
    ENV_LIGHTPANDA_BIN,
    DriverError,
    Element,
    FindResult,
    PageState,
    ReadResult,
    SessionInfo,
)

log = logging.getLogger(__name__)


# Size budget for read().markdown — soft cap to keep agent tokens sane.
_READ_MARKDOWN_MAX_BYTES = 32 * 1024


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
                "clientInfo": {"name": "kros-browser", "version": "0.1.0"},
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

    # --- tier 1 -------------------------------------------------------

    def open(self, url: str, *, timeout_ms: int = 15000) -> ReadResult:
        # NOTE: we deliberately do NOT pass waitUntil here.
        # lightpanda's default is 'done' (more lenient than 'load', which
        # waits for every subresource); heavy pages like duckduckgo/html
        # routinely OperationTimedout on 'load'.
        self._mcp.call("goto", {"url": url, "timeout": timeout_ms})
        self._refresh_state_from_eval(fallback_url=url)

        # lightpanda reports isError=false + "Navigated successfully."
        # even when stderr shows OperationTimedout. The only reliable
        # signal that navigation actually happened is location.href
        # moving off about:blank.
        if self._url in ("", "about:blank") and url not in ("", "about:blank"):
            raise DriverError(
                f"navigation to {url!r} did not complete "
                f"(page still at {self._url!r}). Possible causes: URL "
                f"unreachable from this host, site blocks headless browsers, "
                f"or lightpanda engine limitation on this page. "
                f"Try a different URL or check ~/.kros/browser/daemon.log."
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
        return ReadResult(
            url=self._url,
            title=self._title,
            markdown=md,
            elements=elements,
            truncated=truncated,
        )

    def click(self, *, ref: int) -> PageState:
        return self._state_from(self._mcp.call("click", {"backendNodeId": ref}))

    def fill(self, *, ref: int, value: str) -> PageState:
        return self._state_from(
            self._mcp.call("fill", {"backendNodeId": ref, "text": value})
        )

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
        return FindResult(
            elements=[_parse_element(e) for e in _ensure_list(raw)]
        )

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
    ) -> PageState:
        args: dict[str, Any] = {}
        if ref is not None:
            args["backendNodeId"] = ref
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        return self._state_from(self._mcp.call("scroll", args))

    def eval(self, *, script: str) -> str:
        res = self._mcp.call("evaluate", {"script": script})
        if res is None:
            return ""
        return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)

    # --- tier 3 -------------------------------------------------------

    def press(self, *, key: str, ref: Optional[int] = None) -> PageState:
        args: dict[str, Any] = {"key": key}
        if ref is not None:
            args["backendNodeId"] = ref
        return self._state_from(self._mcp.call("press", args))

    def hover(self, *, ref: int) -> PageState:
        return self._state_from(self._mcp.call("hover", {"backendNodeId": ref}))

    def select(self, *, ref: int, value: str) -> PageState:
        return self._state_from(
            self._mcp.call("selectOption", {"backendNodeId": ref, "value": value})
        )

    def check(self, *, ref: int, checked: bool) -> PageState:
        return self._state_from(
            self._mcp.call(
                "setChecked", {"backendNodeId": ref, "checked": checked}
            )
        )

    # --- helpers ------------------------------------------------------

    def _state_from(self, res: Any) -> PageState:
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
