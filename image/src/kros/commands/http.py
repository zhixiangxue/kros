"""kros http — LLM-friendly HTTP client.

See ``design/05-kros-http.md`` for the full spec. This module implements
v1, which deliberately solves only three of the four curl pain points:

* Exit code = HTTP semantics (2xx→0, 4xx→1, 5xx→2, network→3, validation→4)
* stdout = a single structured JSON line
* ``--json`` is validated upfront — invalid JSON exits 4 without sending

The fourth pain (secret in argv) is intentionally NOT addressed in v1;
``--auth-from secret://X`` lands together with the future ``kros secret``
subcommand. Until then agents must use plain ``-H 'Authorization: ...'``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import typer

from .. import _audit

__all__ = ["register", "http_app"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30.0

# Body bytes above this threshold spill to ``~/.kros/log/http/<id>.{txt,bin}``;
# stdout's JSON keeps only the first 2KB as ``body`` (preview) and sets
# ``body_truncated=True`` so agents know the full payload is on disk.
_SPILL_BYTES = 64 * 1024
_PREVIEW_BYTES = 2 * 1024

# Content-Type prefixes treated as text (rendered inline when small).
# Anything else is binary and goes straight to a spill file with body=null.
_TEXT_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/xhtml",
    "application/x-www-form-urlencoded",
)

_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")
_BODYLESS = {"get", "head"}

# Network-layer exceptions worth retrying when --retry > 0. We do NOT
# retry HTTP 5xx automatically (would surprise agents on POST), only
# transport-level failures.
_NETWORK_RETRYABLE = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

http_app = typer.Typer(
    name="http",
    help="LLM-friendly HTTP client. See design/05-kros-http.md.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def register(app: typer.Typer) -> None:
    """Entry point called from ``kros.cli``."""
    app.add_typer(http_app, name="http")


# ---------------------------------------------------------------------------
# Subcommand registration — one ``kros http <verb>`` per HTTP method
# ---------------------------------------------------------------------------


def _make_handler(method: str):
    """Return a typer-decorated callable bound to ``method`` via closure."""

    def cmd(
        url: str = typer.Argument(..., help="Target URL (http:// or https://)."),
        json_str: Optional[str] = typer.Option(
            None, "--json", help="JSON body; validated upfront, invalid → exit 4."
        ),
        data: Optional[str] = typer.Option(
            None,
            "--data",
            help="Raw body. Literal string, '-' for stdin, or '@path' for file.",
        ),
        form: list[str] = typer.Option(
            [], "--form", help="Form field K=V (urlencoded). Repeatable."
        ),
        multipart: list[str] = typer.Option(
            [],
            "--multipart",
            help="Multipart field K=V or K=@path (file). Repeatable.",
        ),
        header: list[str] = typer.Option(
            [], "-H", "--header", help="HTTP header 'K: V' or 'K=V'. Repeatable."
        ),
        timeout: float = typer.Option(
            _DEFAULT_TIMEOUT, "--timeout", help="Wall-clock timeout in seconds."
        ),
        retry: int = typer.Option(
            0, "--retry", min=0, help="Retries on network errors only (not 5xx)."
        ),
        follow: bool = typer.Option(
            False, "--follow", help="Follow redirects (default OFF for safety)."
        ),
        quiet: bool = typer.Option(
            False, "-q", "--quiet", help="Force body to spill; stdout only metadata."
        ),
        verbose: bool = typer.Option(
            False, "-v", "--verbose", help="Verbose debug info to stderr."
        ),
    ) -> None:
        rc = _execute(
            method=method,
            url=url,
            json_str=json_str,
            data_arg=data,
            form=form,
            multipart=multipart,
            header=header,
            timeout=timeout,
            retry=retry,
            follow=follow,
            quiet=quiet,
            verbose=verbose,
        )
        raise typer.Exit(code=rc)

    cmd.__name__ = f"http_{method}"
    return cmd


for _m in _METHODS:
    http_app.command(name=_m, help=f"Issue an HTTP {_m.upper()} request.")(
        _make_handler(_m)
    )


# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------


def _execute(
    *,
    method: str,
    url: str,
    json_str: Optional[str],
    data_arg: Optional[str],
    form: list[str],
    multipart: list[str],
    header: list[str],
    timeout: float,
    retry: int,
    follow: bool,
    quiet: bool,
    verbose: bool,
) -> int:
    # ----- 1. validation (fail fast, no network I/O yet) -----
    body_modes = [
        name
        for name, given in (
            ("json", json_str is not None),
            ("data", data_arg is not None),
            ("form", bool(form)),
            ("multipart", bool(multipart)),
        )
        if given
    ]
    if len(body_modes) > 1:
        return _emit_validation(
            "bad_args",
            "only one of --json/--data/--form/--multipart allowed, got: "
            + ", ".join(body_modes),
        )
    if method in _BODYLESS and body_modes:
        return _emit_validation(
            "bad_args", f"{method.upper()} cannot have a body"
        )

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"invalid URL: {url!r}")
    except Exception as e:
        return _emit_validation("bad_url", str(e))

    json_payload = None
    if json_str is not None:
        try:
            json_payload = json.loads(json_str)
        except json.JSONDecodeError as e:
            return _emit_validation("bad_json", f"invalid JSON for --json: {e}")

    raw_body: Optional[bytes] = None
    if data_arg is not None:
        try:
            raw_body = _read_data(data_arg)
        except OSError as e:
            return _emit_validation("bad_args", f"--data read failed: {e}")

    header_dict, hdr_err = _parse_headers(header)
    if hdr_err is not None:
        return _emit_validation("bad_args", hdr_err)

    form_payload, form_err = _parse_form(form)
    if form_err is not None:
        return _emit_validation("bad_args", form_err)

    multi_data, multi_files, open_files, mp_err = _parse_multipart(multipart)
    if mp_err is not None:
        _close_files(open_files)
        return _emit_validation("bad_args", mp_err)

    # ----- 2. issue the request -----
    start = time.monotonic()
    resp: Optional[httpx.Response] = None
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=follow, trust_env=True
        ) as client:
            kwargs: dict = {"headers": header_dict}
            if json_payload is not None:
                kwargs["json"] = json_payload
            elif raw_body is not None:
                kwargs["content"] = raw_body
            elif form_payload is not None:
                kwargs["data"] = form_payload
            elif multi_files or multi_data:
                if multi_data:
                    kwargs["data"] = multi_data
                if multi_files:
                    kwargs["files"] = multi_files

            for attempt in range(retry + 1):
                try:
                    resp = client.request(method.upper(), url, **kwargs)
                    break
                except _NETWORK_RETRYABLE:
                    if attempt < retry:
                        if verbose:
                            typer.echo(
                                f"kros http: attempt {attempt + 1} failed, retrying...",
                                err=True,
                            )
                        time.sleep(1)
                        continue
                    raise
    except httpx.TimeoutException as e:
        return _emit_network(
            "timeout", str(e) or f"timed out after {timeout}s",
            int((time.monotonic() - start) * 1000),
        )
    except httpx.ConnectError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        # httpx wraps DNS failures in ConnectError too — disambiguate by
        # message so the agent can distinguish "host unreachable" from
        # "host doesn't resolve".
        msg = str(e)
        kind = "dns" if _looks_like_dns(msg) else "connect"
        return _emit_network(kind, msg, elapsed)
    except httpx.RemoteProtocolError as e:
        return _emit_network(
            "protocol", str(e), int((time.monotonic() - start) * 1000)
        )
    except httpx.NetworkError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        msg = str(e)
        kind = "dns" if _looks_like_dns(msg) else "connect"
        return _emit_network(kind, msg, elapsed)
    except Exception as e:  # safety net — should never hit
        return _emit_network(
            "connect",
            f"{type(e).__name__}: {e}",
            int((time.monotonic() - start) * 1000),
        )
    finally:
        _close_files(open_files)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    assert resp is not None
    return _emit_response(resp, elapsed_ms, quiet)


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def _parse_headers(header: list[str]) -> tuple[dict, Optional[str]]:
    out: dict = {}
    for h in header:
        if ":" in h:
            k, _, v = h.partition(":")
        elif "=" in h:
            k, _, v = h.partition("=")
        else:
            return {}, f"--header expects 'K: V' or 'K=V', got: {h!r}"
        k, v = k.strip(), v.strip()
        if not k:
            return {}, f"--header key must not be empty: {h!r}"
        out[k] = v
    return out, None


def _parse_form(form: list[str]) -> tuple[Optional[dict], Optional[str]]:
    if not form:
        return None, None
    out: dict = {}
    for kv in form:
        if "=" not in kv:
            return None, f"--form expects K=V, got: {kv!r}"
        k, _, v = kv.partition("=")
        out[k] = v
    return out, None


def _parse_multipart(
    multipart: list[str],
) -> tuple[Optional[dict], Optional[list], list, Optional[str]]:
    if not multipart:
        return None, None, [], None
    data: dict = {}
    files: list = []
    opened: list = []
    for kv in multipart:
        if "=" not in kv:
            return None, None, opened, f"--multipart expects K=V or K=@path, got: {kv!r}"
        k, _, v = kv.partition("=")
        if v.startswith("@"):
            p = v[1:]
            try:
                fh = open(p, "rb")
            except OSError as e:
                return None, None, opened, f"--multipart file open failed: {e}"
            opened.append(fh)
            files.append((k, (Path(p).name, fh)))
        else:
            data[k] = v
    return data or None, files or None, opened, None


def _read_data(spec: str) -> bytes:
    if spec == "-":
        return sys.stdin.buffer.read()
    if spec.startswith("@"):
        return Path(spec[1:]).read_bytes()
    return spec.encode("utf-8")


def _close_files(files: list) -> None:
    for f in files:
        try:
            f.close()
        except Exception:
            pass


def _looks_like_dns(msg: str) -> bool:
    """Heuristic — httpx doesn't expose a dedicated DNS exception."""
    low = msg.lower()
    return any(
        k in low
        for k in (
            "name or service not known",
            "nodename nor servname",
            "name resolution",
            "could not resolve",
            "dns",
            "getaddrinfo",
        )
    )


# ---------------------------------------------------------------------------
# Emission — every code path ends in exactly one stdout JSON line
# ---------------------------------------------------------------------------


def _emit_validation(kind: str, msg: str) -> int:
    _emit(_envelope(error={"kind": kind, "msg": msg}))
    return 4


def _emit_network(kind: str, msg: str, elapsed_ms: int) -> int:
    _emit(
        _envelope(
            elapsed_ms=elapsed_ms,
            error={"kind": kind, "msg": msg},
        )
    )
    return 3


def _emit_response(resp: httpx.Response, elapsed_ms: int, quiet: bool) -> int:
    headers = {k.lower(): v for k, v in resp.headers.items()}
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    is_text = (
        not content_type
        or any(content_type.startswith(p) for p in _TEXT_PREFIXES)
    )

    raw = resp.content
    body: Optional[str] = None
    body_path: Optional[str] = None
    body_truncated = False
    body_encoding: Optional[str] = None

    if quiet:
        body_path = _spill(raw, content_type)
        body_encoding = "utf-8" if is_text else "binary"
    elif not is_text:
        body_path = _spill(raw, content_type)
        body_encoding = "binary"
    elif len(raw) > _SPILL_BYTES:
        body = _safe_decode(raw[:_PREVIEW_BYTES])
        body_path = _spill(raw, content_type)
        body_truncated = True
        body_encoding = "utf-8"
    else:
        body = _safe_decode(raw) if raw else ""
        body_encoding = "utf-8"

    status = resp.status_code
    record = _envelope(
        ok=200 <= status < 300,
        status=status,
        headers=headers,
        elapsed_ms=elapsed_ms,
        url_final=str(resp.url),
        body=body,
        body_path=body_path,
        body_truncated=body_truncated,
        body_encoding=body_encoding,
        error=None,
    )
    _emit(record)

    if 200 <= status < 300:
        return 0
    if 400 <= status < 500:
        return 1
    if 500 <= status < 600:
        return 2
    # 1xx / 3xx (when --follow is off) — treat as success; status field
    # carries the truth. Agents who care about redirects should set --follow.
    return 0


def _envelope(**overrides) -> dict:
    """Build the canonical stdout record with all fields present."""
    base: dict = {
        "ok": False,
        "status": None,
        "headers": None,
        "elapsed_ms": 0,
        "url_final": None,
        "body": None,
        "body_path": None,
        "body_truncated": False,
        "body_encoding": None,
        "error": None,
    }
    base.update(overrides)
    return base


def _emit(record: dict) -> None:
    typer.echo(json.dumps(record, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Spill — large/binary bodies go to ``~/.kros/log/http/<id>.{txt,bin}``
# ---------------------------------------------------------------------------


def _spill(data: bytes, content_type: str) -> str:
    cmd_id = _current_cmd_id()
    spill_root = Path.home() / ".kros" / "log" / "http"
    try:
        spill_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    ext = ".txt" if any(content_type.startswith(p) for p in _TEXT_PREFIXES) else ".bin"
    path = spill_root / f"{cmd_id}{ext}"
    try:
        path.write_bytes(data)
    except OSError:
        return ""
    log_root = Path.home() / ".kros" / "log"
    try:
        return str(path.relative_to(log_root))
    except ValueError:
        return str(path)


def _current_cmd_id() -> str:
    """Reuse the audit invocation id so spill files are linkable to log."""
    state = _audit._current
    if state is not None and state.id:
        return state.id
    # Fallback for ``kros log``-skipped invocations or audit failures.
    from nanoid import generate

    return generate(size=21)


def _safe_decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
