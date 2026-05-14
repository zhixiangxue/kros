"""``kros shell`` subcommand group — execute shell commands with audit.

Currently ships a single subcommand ``run``. Package form (not single
file) so we can grow helpers (_unwrap, future _secret) without bloating
one module.
"""

from __future__ import annotations

import typer

from .run import run_cmd

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


def register(app: typer.Typer) -> None:
    """Attach ``shell`` to the root kros app."""
    app.add_typer(shell_app, name="shell")


__all__ = ["shell_app", "register"]
