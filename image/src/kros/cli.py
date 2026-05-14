"""Kros CLI entry point — image-side, facing LLM agents.

Each subcommand lives in `kros.commands.*` and exposes a `register(app)`
function that this module calls once at import time.

Configuration is environment-driven. On startup we auto-load
``~/.kros/.env`` so users don't have to ``export`` credentials in every
shell. Values already present in the process environment (shell export
or ``docker run -e ...``) always take precedence and are never overwritten.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from dotenv import load_dotenv

from kros import _audit
from kros.commands import browser as browser_cmd
from kros.commands import file as file_cmd
from kros.commands import memory as memory_cmd
from kros.commands import sandbox as sandbox_cmd
from kros.commands import shell as shell_cmd

# Fill in missing KROS_* / provider env vars from the user-level dotenv
# file, but never override what the shell / container already set.
load_dotenv(Path.home() / ".kros" / ".env", override=False)

app = typer.Typer(
    name="kros",
    help="Kros — Agent OS CLI.",
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
file_cmd.register(app)
memory_cmd.register(app)
sandbox_cmd.register(app)
browser_cmd.register(app)
shell_cmd.register(app)


def main() -> None:
    """Console-script entry point. Wraps ``app()`` with audit before/after.

    Every ``kros`` invocation in the container funnels through this
    function. We stage a ledger record on entry and emit one jsonl line
    on exit — including the exit code — regardless of whether the
    command succeeded, failed, timed out, or was interrupted.

    Typer's ``standalone_mode=True`` (the default for ``app()``) raises
    ``SystemExit`` at the end of every run, so the ``finally`` block is
    where we reliably record the outcome.
    """
    state = _audit.before(list(sys.argv))
    exit_code = 0
    try:
        app()
    except SystemExit as e:
        code = e.code
        if isinstance(code, int):
            exit_code = code
        elif code is None:
            exit_code = 0
        else:
            # String message → treat as error (shell convention).
            exit_code = 1
        raise
    except KeyboardInterrupt:
        exit_code = 130  # 128 + SIGINT
        raise
    except BaseException:
        exit_code = 1
        raise
    finally:
        _audit.after(state, exit_code)


if __name__ == "__main__":
    main()
