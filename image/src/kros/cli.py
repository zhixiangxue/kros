"""Kros CLI entry point — image-side, facing LLM agents.

Each subcommand lives in `kros.commands.*` and exposes a `register(app)`
function that this module calls once at import time.

Configuration is environment-driven. On startup we auto-load
``~/.kros/.env`` so users don't have to ``export`` credentials in every
shell. Values already present in the process environment (shell export
or ``docker run -e ...``) always take precedence and are never overwritten.
"""

from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv

from kros.commands import browse as browse_cmd
from kros.commands import memory as memory_cmd
from kros.commands import read as read_cmd
from kros.commands import sandbox as sandbox_cmd

# Fill in missing KROS_* / provider env vars from the user-level dotenv
# file, but never override what the shell / container already set.
load_dotenv(Path.home() / ".kros" / ".env", override=False)

app = typer.Typer(
    name="kros",
    help="Kros — Agent OS image-side CLI.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _root() -> None:
    """No-op root callback.

    Registered to keep Typer in multi-command (group) mode even when
    only one subcommand exists today. Without this, Typer flattens a
    single-command app into a bare command, hiding the `Commands:` list
    from `kros -h`. Kros is docker/git-style from day one.
    """


# register subcommands
read_cmd.register(app)
memory_cmd.register(app)
sandbox_cmd.register(app)
browse_cmd.register(app)


if __name__ == "__main__":
    app()
