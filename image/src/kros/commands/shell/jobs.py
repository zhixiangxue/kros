"""``kros shell jobs [TASK_ID]`` — list async tasks (see design/04 §6.2).

Explicitly NOT a wrapper of system ``ps``: only tasks dispatched via
``kros shell spawn`` are visible here. To see system processes, agents
should use ``kros shell run 'ps -ef'``.

State derivation (closed set, see design/04 §6.2):

* exit_code file present + value == 124        -> "killed_by_timeout"
* exit_code file present + value != 124        -> "exited"
* exit_code file absent + pid alive            -> "running"
* exit_code file absent + pid gone             -> "lost"
"""

from __future__ import annotations

import json
import time
from typing import Optional

import typer

from ._store import JobsStore

# GNU ``timeout`` writes 124 when the timer fires. We surface this as
# a distinct state so agents can tell "tool killed it" from "user code
# returned 124".
_EXIT_TIMEOUT = 124


def jobs_cmd(
    task_id: Optional[str] = typer.Argument(
        None,
        metavar="[TASK_ID]",
        help="If given, show details of one task; otherwise list all.",
    ),
    state: Optional[str] = typer.Option(
        None,
        "--state",
        metavar="STATE",
        help="Filter list mode by state: running / exited / killed_by_timeout / lost.",
    ),
) -> None:
    """List async tasks dispatched by ``kros shell spawn`` (NOT system ps)."""
    if task_id is not None:
        record = _build_record(task_id)
        if record is None:
            typer.secho(f"kros: no such task: {task_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(record, ensure_ascii=False))
        raise typer.Exit(code=0)

    records: list[dict] = []
    for tid in JobsStore.list_task_ids():
        rec = _build_record(tid)
        if rec is None:
            # meta.json missing or corrupt — surface a minimal "lost"
            # record rather than dropping it silently.
            rec = {"task_id": tid, "state": "lost"}
        if state and rec.get("state") != state:
            continue
        records.append(rec)

    typer.echo(json.dumps(records, ensure_ascii=False))
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# state derivation — also imported by logs.py
# ---------------------------------------------------------------------------


def compute_state_and_exit(task_id: str, meta: dict) -> tuple[str, Optional[int]]:
    """Return (state, exit_code) for a task. exit_code may be None (running/lost)."""
    code = JobsStore.read_exit_code(task_id)
    if code is not None:
        if code == _EXIT_TIMEOUT:
            return "killed_by_timeout", code
        return "exited", code

    # exit_code file not yet written — task is either still running or
    # died abnormally before bash could land the file.
    pid = int(meta.get("pid") or 0)
    if pid > 0 and JobsStore.is_alive(pid):
        return "running", None
    return "lost", None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_record(task_id: str) -> Optional[dict]:
    meta = JobsStore.read_meta(task_id)
    if meta is None:
        return None

    state, exit_code = compute_state_and_exit(task_id, meta)

    record: dict = {
        "task_id": task_id,
        "pid": int(meta.get("pid") or 0),
        "state": state,
        "started_at": meta.get("started_at", ""),
        "uptime_sec": _uptime_sec(meta),
        "cmd": meta.get("cmd", ""),
    }
    if exit_code is not None:
        record["exit_code"] = exit_code
    return record


def _uptime_sec(meta: dict) -> int:
    """Wall-clock seconds since spawn — best-effort, integer.

    We can't use ``time.monotonic`` (it's per-process) and don't want
    to require persistent epoch state on disk, so the source of truth
    is ``started_at`` (ISO-8601 UTC). Failure to parse -> 0, never raise.
    """
    started_at = meta.get("started_at", "")
    try:
        # "2026-05-17T10:23:11Z" — strip the trailing Z, treat as UTC.
        t = time.strptime(started_at.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
        return max(0, int(time.time() - _utc_to_epoch(t)))
    except Exception:
        return 0


def _utc_to_epoch(t: time.struct_time) -> float:
    """``time.struct_time`` -> epoch seconds, treating the input as UTC.

    ``time.mktime`` interprets struct_time as local time, so for a UTC
    struct we use ``calendar.timegm`` semantics inline (avoid importing
    calendar for one call).
    """
    import calendar

    return calendar.timegm(t)
