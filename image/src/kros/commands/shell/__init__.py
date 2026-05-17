"""``kros shell`` subcommand group — execute shell commands with audit.

Two execution modes:

* ``run`` — synchronous, streams output to the current tty, one ledger
  entry. Best for short commands whose result the agent needs now.
* ``spawn`` / ``jobs`` / ``logs`` / ``kill`` — asynchronous task
  primitives (see design/04). ``spawn`` returns a task_id immediately;
  ``jobs`` queries kros-managed tasks (NOT a wrapper of system ``ps``);
  ``logs`` tails stdout/stderr; ``kill`` signals a task by id.
"""

from __future__ import annotations

import typer

from .kill import kill_cmd
from .jobs import jobs_cmd
from .logs import logs_cmd
from .run import run_cmd
from .spawn import spawn_cmd

shell_app = typer.Typer(
    name="shell",
    help="Run shell commands (auditable). All runs are logged to ~/.kros/log/.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

shell_app.command(
    "run",
    help=(
        "Run a shell command via /bin/bash -c. "
        "Example: kros shell run 'ls -la'. "
        "Use single quotes around the command so $ENV_VARS expand inside "
        "the container (not the outer shell) — prevents secrets from leaking "
        "into the audit ledger."
    ),
)(run_cmd)

shell_app.command(
    "spawn",
    help=(
        "Dispatch a long-running command asynchronously. Returns a task_id "
        "immediately. Use `kros shell jobs/logs/kill` to query or stop it. "
        "Output goes to ~/.kros/jobs/<task_id>/."
    ),
)(spawn_cmd)

shell_app.command(
    "jobs",
    help=(
        "List async tasks dispatched by `kros shell spawn`. "
        "NOT a wrapper of system `ps` — to inspect arbitrary system processes "
        "use `kros shell run 'ps -ef'`."
    ),
)(jobs_cmd)

shell_app.command(
    "logs",
    help=(
        "Tail stdout/stderr of a spawned task (default: last 80 lines per stream). "
        "Pass --full to dump everything."
    ),
)(logs_cmd)

shell_app.command(
    "kill",
    help=(
        "Signal a spawned task by task_id (default SIGTERM). "
        "Refuses raw PIDs — to kill a system process use `kros shell run 'kill <pid>'`."
    ),
)(kill_cmd)


def register(app: typer.Typer) -> None:
    """Attach ``shell`` to the root kros app."""
    app.add_typer(shell_app, name="shell")


__all__ = ["shell_app", "register"]
