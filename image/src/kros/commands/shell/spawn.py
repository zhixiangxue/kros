"""``kros shell spawn <cmd>`` — async dispatch (see design/04).

Differs from :mod:`kros.commands.shell.run` along two axes:

* **Async**: Popen and immediately return — never wait. Output goes to
  files under ``~/.kros/jobs/<task_id>/`` instead of being tee'd to the
  current tty. The kros CLI process exits in milliseconds; the spawned
  task may run for hours.
* **Exit code via wrapper**: because we don't wait, the user's exit
  code would normally be lost when the kros CLI exits and PID 1 (tini)
  reaps the orphan. We work around this by wrapping the user command in
  a tiny bash snippet that writes ``$?`` to ``exit_code`` after the user
  command finishes.

The wrapper:

    timeout --preserve-status <SECS>s bash -c '<USER_CMD>'; echo $? > <task_dir>/exit_code

* ``timeout --preserve-status`` keeps the user's real exit code on
  normal exit; if the timer fires it sends SIGTERM (then SIGKILL after
  a coreutils-default grace) and exits 124 (GNU convention).
* The trailing ``echo $? > exit_code`` is what makes ``jobs`` /
  ``logs`` work after the kros CLI is long gone — see design/04 §7.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Optional

import typer

from ._store import DEFAULT_TIMEOUT_SEC, JobsStore


def spawn_cmd(
    cmd: str = typer.Argument(
        ...,
        metavar="CMD",
        help="Shell command string. Passed to /bin/bash -c verbatim.",
    ),
    cwd: Optional[str] = typer.Option(
        None,
        "--cwd",
        metavar="PATH",
        help="Working directory for the spawned task. Default: inherit from caller.",
    ),
    env: list[str] = typer.Option(
        [],
        "--env",
        "-e",
        metavar="KEY=VAL",
        help="Extra env var for the spawned task. Repeatable.",
    ),
    timeout: float = typer.Option(
        float(DEFAULT_TIMEOUT_SEC),
        "--timeout",
        "-t",
        metavar="SECS",
        help=(
            "Kill the task after SECS seconds (GNU `timeout` --preserve-status: "
            "SIGTERM then SIGKILL). State becomes 'killed_by_timeout' (exit 124)."
        ),
    ),
) -> None:
    """Dispatch CMD asynchronously, return a task_id immediately."""
    if cwd is not None and not os.path.isdir(cwd):
        raise typer.BadParameter(f"--cwd not a directory: {cwd}", param_hint="--cwd")
    if timeout <= 0:
        raise typer.BadParameter("--timeout must be > 0", param_hint="--timeout")

    child_env = _build_child_env(env)

    task_id = JobsStore.new_task_id()
    task_dir = JobsStore.task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = task_dir / "stdout"
    stderr_path = task_dir / "stderr"
    exit_code_path = task_dir / "exit_code"

    wrapper = _build_wrapper(cmd, timeout, exit_code_path)
    started_at = _iso_utc_now()

    # Open output files now (not via shell redirection) so any failure
    # to create them surfaces here, not silently inside bash.
    out_fp = open(stdout_path, "ab")
    err_fp = open(stderr_path, "ab")
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", wrapper],
            stdout=out_fp,
            stderr=err_fp,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=child_env,
            # Detach the spawned task from the kros CLI's session so it
            # survives the kros CLI's imminent exit (and so we can later
            # SIGTERM the whole tree via os.killpg).
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError:
        out_fp.close()
        err_fp.close()
        typer.secho(
            "kros: /bin/bash not found — this image is broken.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=127)
    except OSError as e:
        out_fp.close()
        err_fp.close()
        typer.secho(f"kros: failed to spawn: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=126)
    finally:
        # Popen dup'd the fds; we can close ours immediately so the
        # files are only kept open by the spawned task itself.
        try:
            out_fp.close()
            err_fp.close()
        except Exception:
            pass

    pgid = _safe_pgid(proc.pid)

    JobsStore.write_meta(
        task_id,
        {
            "task_id": task_id,
            "pid": proc.pid,
            "pgid": pgid,
            "cmd": cmd,
            "cwd": cwd or os.getcwd(),
            "started_at": started_at,
            "timeout_sec": float(timeout),
        },
    )

    typer.echo(
        json.dumps(
            {
                "task_id": task_id,
                "pid": proc.pid,
                "started_at": started_at,
                "timeout_sec": float(timeout),
            },
            ensure_ascii=False,
        )
    )
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_child_env(extra: list[str]) -> dict[str, str]:
    """Start from the current env, overlay ``KEY=VAL`` pairs from ``--env``.

    Mirrors :func:`kros.commands.shell.run._build_child_env` — kept as
    a small local copy rather than imported to avoid module-load
    coupling (run.py is the hot path; this one is rarer).
    """
    env = os.environ.copy()
    for item in extra:
        if "=" not in item:
            raise typer.BadParameter(
                f"--env expects KEY=VAL, got: {item!r}",
                param_hint="--env",
            )
        k, _, v = item.partition("=")
        if not k:
            raise typer.BadParameter(
                f"--env key must not be empty: {item!r}",
                param_hint="--env",
            )
        env[k] = v
    return env


def _build_wrapper(user_cmd: str, timeout_sec: float, exit_code_path) -> str:
    """Compose the bash snippet that runs the user command and lands the exit code.

    The structure is::

        timeout --preserve-status Ns bash -c '<USER_CMD>'; echo $? > <exit_code_path>

    * ``--preserve-status`` keeps the user's real exit code unless the
      timeout fires; on timeout, exit code is 124.
    * ``echo $?`` runs **after** the timeout step regardless of how
      the user command exited, so the exit_code file is always written
      (this is the contract that makes ``jobs`` work without polling
      ``waitpid`` from a vanished kros CLI).
    """
    # Format the timeout argument as integer if possible — coreutils
    # ``timeout`` accepts fractional seconds, but most agents will pass
    # whole numbers and an integer reads cleaner in the wrapper.
    if float(timeout_sec).is_integer():
        timeout_str = f"{int(timeout_sec)}s"
    else:
        timeout_str = f"{timeout_sec}s"

    inner = f"timeout --preserve-status {shlex.quote(timeout_str)} bash -c {shlex.quote(user_cmd)}"
    return f"{inner}; echo $? > {shlex.quote(str(exit_code_path))}"


def _safe_pgid(pid: int) -> int:
    """``os.getpgid`` can race with the child exiting; return 0 on failure."""
    try:
        return os.getpgid(pid)
    except OSError:
        return 0


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
