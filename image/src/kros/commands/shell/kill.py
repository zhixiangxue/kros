"""``kros shell kill <task_id>`` — terminate a spawned task by signal.

Two design points worth calling out (see design/04 §2):

* **Refuses raw PIDs**. ``kros shell kill 412`` errors out and points
  the agent at ``kros shell run 'kill 412'`` instead. This is one of
  four anti-confusion gates between ``kros shell jobs`` and system
  ``ps``/``kill``: the agent never has a chance to mistake a system
  process for a kros-managed task.
* **Signals the whole pgroup, not just the wrapper**. Spawn put the
  task in its own session, so ``os.killpg`` reaches the wrapper bash,
  the user command, *and* any backgrounded grandchildren in one shot.
  A naive ``os.kill(pid, sig)`` would only hit the wrapper and leave
  ``sleep 100 & wait``-style grandchildren as init's orphans.
"""

from __future__ import annotations

import json
import os
import signal as signal_mod

import typer

from ._store import TASK_ID_PREFIX, JobsStore
from .jobs import compute_state_and_exit

# Map case-insensitive friendly names -> signal numbers. Only the three
# signals that make sense for "stop this task" are exposed; agents who
# want SIGUSR1/SIGHUP can fall back to ``kros shell run 'kill -USR1 <pid>'``.
_SIGNALS: dict[str, int] = {
    "TERM": signal_mod.SIGTERM,
    "INT": signal_mod.SIGINT,
}
# SIGKILL doesn't exist on Windows, but we only run inside the Docker
# image (Linux). The guard keeps the module importable for dev-time
# static analysis on Windows / macOS.
if hasattr(signal_mod, "SIGKILL"):
    _SIGNALS["KILL"] = signal_mod.SIGKILL


def kill_cmd(
    task_id: str = typer.Argument(
        ...,
        metavar="TASK_ID",
        help="The t_xxxxxx id returned by `kros shell spawn` (NOT a raw PID).",
    ),
    signal_name: str = typer.Option(
        "TERM",
        "--signal",
        "-s",
        metavar="NAME",
        help="Signal to send: TERM (default) / KILL / INT. Case-insensitive.",
    ),
) -> None:
    """Send a signal to the spawned task's whole process group."""
    _validate_task_id(task_id)

    sig_upper = signal_name.upper()
    if sig_upper.startswith("SIG"):
        sig_upper = sig_upper[3:]
    sig = _SIGNALS.get(sig_upper)
    if sig is None:
        raise typer.BadParameter(
            f"unsupported signal {signal_name!r}; choose TERM / KILL / INT",
            param_hint="--signal",
        )

    meta = JobsStore.read_meta(task_id)
    if meta is None:
        typer.secho(f"kros: no such task: {task_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    state, exit_code = compute_state_and_exit(task_id, meta)

    if state in ("exited", "killed_by_timeout", "lost"):
        result = {
            "task_id": task_id,
            "killed": False,
            "reason": f"already_{state}" if state != "exited" else "already_exited",
            "signal": _signal_name(sig),
        }
        if exit_code is not None:
            result["exit_code"] = exit_code
        typer.echo(json.dumps(result, ensure_ascii=False))
        raise typer.Exit(code=0)

    pgid = int(meta.get("pgid") or 0)
    pid = int(meta.get("pid") or 0)
    killed, reason = _send_signal(pgid, pid, sig)

    payload = {
        "task_id": task_id,
        "killed": killed,
        "signal": _signal_name(sig),
    }
    if reason:
        payload["reason"] = reason
    typer.echo(json.dumps(payload, ensure_ascii=False))
    raise typer.Exit(code=0 if killed else 1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _validate_task_id(task_id: str) -> None:
    """Bounce raw PIDs back to ``kros shell run 'kill ...'`` (see §2)."""
    if task_id.isdigit():
        raise typer.BadParameter(
            f"expected a task_id like '{TASK_ID_PREFIX}9x82f1', not a raw PID. "
            f"To kill a system process, use: kros shell run 'kill {task_id}'",
            param_hint="TASK_ID",
        )
    if not task_id.startswith(TASK_ID_PREFIX):
        raise typer.BadParameter(
            f"task_id must start with '{TASK_ID_PREFIX}' (got {task_id!r})",
            param_hint="TASK_ID",
        )


def _send_signal(pgid: int, pid: int, sig: int) -> tuple[bool, str]:
    """Try ``killpg`` first, fall back to ``kill``. Return (success, reason)."""
    if pgid > 0:
        try:
            os.killpg(pgid, sig)
            return True, ""
        except ProcessLookupError:
            return False, "already_exited"
        except PermissionError:
            return False, "permission_denied"
        except OSError as e:
            # Fall through to single-process kill rather than failing hard.
            last_err = str(e)
        else:
            last_err = ""
    else:
        last_err = "no_pgid"

    if pid > 0:
        try:
            os.kill(pid, sig)
            return True, ""
        except ProcessLookupError:
            return False, "already_exited"
        except PermissionError:
            return False, "permission_denied"
        except OSError as e:
            return False, str(e)
    return False, last_err or "no_target"


def _signal_name(sig: int) -> str:
    """e.g. signal.SIGTERM -> 'SIGTERM'."""
    try:
        return signal_mod.Signals(sig).name
    except ValueError:
        return f"signal_{sig}"
