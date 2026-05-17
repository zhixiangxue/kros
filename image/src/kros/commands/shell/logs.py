"""``kros shell logs <task_id>`` — tail stdout/stderr of a spawned task.

Default behavior is ``--tail 80`` per stream, which is the "last_words"
sweet spot: long enough to contain a full Python traceback or a few
HTTP error responses, short enough to keep agent token costs sane.

Pass ``--full`` to dump everything (use sparingly — long-running
services can produce megabytes of stdout).
"""

from __future__ import annotations

import json

import typer

from ._store import TASK_ID_PREFIX, JobsStore
from .jobs import compute_state_and_exit


def logs_cmd(
    task_id: str = typer.Argument(
        ...,
        metavar="TASK_ID",
        help="The t_xxxxxx id returned by `kros shell spawn`.",
    ),
    tail: int = typer.Option(
        80,
        "--tail",
        "-n",
        metavar="LINES",
        help="Lines from the end of each stream. Ignored if --full.",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Dump the entire stdout/stderr instead of tailing.",
    ),
) -> None:
    """Tail (default) or dump (--full) stdout/stderr of a spawned task."""
    _validate_task_id(task_id)

    meta = JobsStore.read_meta(task_id)
    if meta is None:
        typer.secho(f"kros: no such task: {task_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    state, exit_code = compute_state_and_exit(task_id, meta)

    task_dir = JobsStore.task_dir(task_id)
    stdout_path = task_dir / "stdout"
    stderr_path = task_dir / "stderr"

    if full:
        stdout_bytes = JobsStore.read_full(stdout_path)
        stderr_bytes = JobsStore.read_full(stderr_path)
    else:
        if tail <= 0:
            raise typer.BadParameter("--tail must be > 0", param_hint="--tail")
        stdout_bytes = JobsStore.tail_file(stdout_path, tail)
        stderr_bytes = JobsStore.tail_file(stderr_path, tail)

    payload: dict = {
        "task_id": task_id,
        "state": state,
        "stdout_tail": _safe_decode(stdout_bytes),
        "stderr_tail": _safe_decode(stderr_bytes),
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code

    typer.echo(json.dumps(payload, ensure_ascii=False))
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _validate_task_id(task_id: str) -> None:
    """Reject raw PIDs and other non-task-id inputs early.

    Mirrors ``kill_cmd`` — without this, an agent that confuses
    ``kros shell logs`` with reading system logs by PID would get a
    confusing "no such task" message instead of being directed to the
    right tool.
    """
    if task_id.isdigit():
        raise typer.BadParameter(
            f"expected a task_id like '{TASK_ID_PREFIX}9x82f1', not a raw PID. "
            f"To inspect a system process, try `kros shell run 'ps -p {task_id}'`.",
            param_hint="TASK_ID",
        )
    if not task_id.startswith(TASK_ID_PREFIX):
        raise typer.BadParameter(
            f"task_id must start with '{TASK_ID_PREFIX}' (got {task_id!r})",
            param_hint="TASK_ID",
        )


def _safe_decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")
