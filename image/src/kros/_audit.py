"""Kros audit ledger — one jsonl line per ``kros`` invocation.

Writes happen at ``kros`` process boundaries (see :func:`before` /
:func:`after` called from :mod:`kros.cli`). Existing subcommands
(browser / file / memory / sandbox) stay 0-change: they keep calling
``typer.Exit`` as before and we pick up the exit code in :func:`after`.

Single-line strategy (v1): instead of writing a "before" line and
appending a "after" line, we stage the opening fields in memory during
:func:`before` and emit **one** merged jsonl line in :func:`after`.
Downside: if the kros process is SIGKILL'd mid-command, that record is
lost. Upside: append-only, no rewrite, schema stays one-record-per-line
(§5 of design 03). Acceptable tradeoff for v1.

Fail-open: every filesystem write is wrapped in ``try / except`` so a
broken log directory never breaks the command itself (§2, §13 Q3).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nanoid import generate as _nanoid_generate

__all__ = [
    "AuditState",
    "before",
    "after",
    "record_stdout",
    "rewrite_argv",
    "should_skip",
]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

# stdout bytes above this threshold spill to ~/.kros/log/out/<id>.txt;
# jsonl keeps only the first 2KB as ``stdout_preview``.
_SPILL_BYTES = int(os.environ.get("KROS_LOG_SPILL_BYTES", 64 * 1024))
_PREVIEW_BYTES = 2 * 1024

# Length of the nanoid we hand out as the audit record ``id``. 21 chars
# is the nanoid default and gives ~149 bits of entropy — collision-free
# at any realistic kros invocation rate. We rely on the ``ts`` field
# (not the id) for time-ordering, so a random id is fine here.
_ID_LEN = 21


# ---------------------------------------------------------------------------
# state (module-level so shell/run.py can hand bytes back without typer ctx)
# ---------------------------------------------------------------------------


@dataclass
class AuditState:
    id: str
    ts: str
    subcmd: str
    argv: list[str]
    cwd: str
    pid: int
    parent_id: Optional[str]
    started_monotonic: float
    stdout_buf: bytes = b""
    stderr_buf: bytes = b""
    skip: bool = False  # set when we detect --help / log / disabled env
    extra: dict = field(default_factory=dict)


_current: Optional[AuditState] = None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def should_skip(argv: list[str]) -> bool:
    """Return True if this invocation should not be logged.

    Audit is a *non-negotiable* core promise of kros — there is no
    environment-variable kill switch by design. We only skip the two
    cases that would otherwise pollute the ledger with non-events:

    - ``-h`` / ``--help`` anywhere in argv (the user is reading docs,
      not executing anything)
    - first positional subcommand is ``log`` (read-only meta command —
      reading the ledger should not append to it, §3)
    """
    if any(a in ("-h", "--help") for a in argv):
        return True
    # First token after the program name that doesn't start with '-'
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        return tok == "log"
    return False


def before(argv: list[str]) -> AuditState:
    """Stage the opening fields of one ledger record.

    Caller contract: call exactly once per ``kros`` process, at the very
    top of :func:`kros.cli.main`, before :func:`app` is invoked.
    """
    global _current
    skip = should_skip(argv)
    state = AuditState(
        id=_new_id() if not skip else "",
        ts=_iso_utc_now(),
        subcmd=_infer_subcmd(argv),
        argv=list(argv),
        cwd=_safe_cwd(),
        pid=os.getpid(),
        parent_id=os.environ.get("KROS_CMD_ID") or None,
        started_monotonic=time.monotonic(),
        skip=skip,
    )
    _current = state
    # Expose id to child kros processes so they can fill parent_id (§9).
    if not skip:
        os.environ["KROS_CMD_ID"] = state.id
    return state


def after(state: AuditState, exit_code: int) -> None:
    """Emit the final jsonl line. Fail-open — never raises."""
    global _current
    try:
        if state.skip:
            return
        dur_ms = int((time.monotonic() - state.started_monotonic) * 1000)
        record = {
            "id": state.id,
            "ts": state.ts,
            "subcmd": state.subcmd,
            "argv": state.argv,
            "cwd": state.cwd,
            "pid": state.pid,
            "exit": int(exit_code),
            "dur_ms": dur_ms,
        }
        if state.parent_id:
            record["parent_id"] = state.parent_id
        _attach_stdout(record, state)
        _append_line(record)
    except Exception as e:  # pragma: no cover — fail-open
        _warn(f"audit.after failed: {e}")
    finally:
        _current = None


def record_stdout(out: bytes, err: bytes = b"") -> None:
    """Called by ``shell/run.py`` after subprocess completes.

    Stash the captured bytes on the current audit state; :func:`after`
    decides spill vs preview.
    """
    if _current is None or _current.skip:
        return
    _current.stdout_buf = out or b""
    _current.stderr_buf = err or b""


def rewrite_argv(new_argv: list[str]) -> None:
    """Used by shell-run unwrap (§7): the outer record should reflect
    the real dispatched command, not the redundant wrapper.
    """
    if _current is None or _current.skip:
        return
    _current.argv = list(new_argv)
    _current.subcmd = _infer_subcmd(new_argv)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """Random 21-char URL-safe id via nanoid (default alphabet/length)."""
    return _nanoid_generate(size=_ID_LEN)


def _iso_utc_now() -> str:
    # e.g. "2026-05-14T03:21:08Z"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _infer_subcmd(argv: list[str]) -> str:
    """First non-flag token after 'kros'."""
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        return tok
    return ""


def _log_dir() -> Path:
    return Path.home() / ".kros" / "log"


def _current_jsonl_path() -> Path:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return _log_dir() / f"cmd-{day}.jsonl"


def _spill_path(cmd_id: str) -> Path:
    return _log_dir() / "out" / f"{cmd_id}.txt"


def _attach_stdout(record: dict, state: AuditState) -> None:
    """Decide: preview inline / spill to file / both."""
    buf = state.stdout_buf + (b"\n--- stderr ---\n" + state.stderr_buf if state.stderr_buf else b"")
    if not buf:
        return
    if len(buf) <= _SPILL_BYTES:
        # Small enough to keep inline as preview (no spill file).
        record["stdout_preview"] = _safe_decode(buf[:_PREVIEW_BYTES])
        return
    # Spill: write full bytes to out/<id>.txt; inline preview only.
    try:
        path = _spill_path(state.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf)
        record["stdout_path"] = str(path.relative_to(_log_dir()))
    except Exception as e:  # pragma: no cover — fail-open
        _warn(f"audit.spill failed: {e}")
    record["stdout_preview"] = _safe_decode(buf[:_PREVIEW_BYTES])


def _safe_decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _append_line(record: dict) -> None:
    path = _current_jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    # O_APPEND on Linux guarantees atomic append for writes < PIPE_BUF.
    # Our lines are typically <4KB; preview is capped at 2KB so the
    # total stays well under the limit.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _warn(msg: str) -> None:
    try:
        sys.stderr.write(f"kros: audit warning: {msg}\n")
    except Exception:
        pass
