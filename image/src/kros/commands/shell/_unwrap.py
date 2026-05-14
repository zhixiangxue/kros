"""Matryoshka unwrap for ``kros shell run`` (§7 of design 03).

When an agent writes ``kros shell run "kros browser open <url>"`` we
detect the inner ``kros`` and dispatch it in-process instead of forking
``/bin/bash -c kros browser open ...``. Two wins:

1. **One ledger entry, not two** — the outer shell-run is rewritten to
   reflect the inner subcmd (see :func:`kros._audit.rewrite_argv`).
2. **No fork overhead** — saves a subprocess + shell parse.

Only the *outermost* ``kros`` token is peeled. Recursive wrapping
(``kros shell run "kros shell run pytest"``) still works: after one
peel it becomes ``kros shell run pytest``, which re-enters this module
and peels again.

Opt-out: callers can pass ``--no-unwrap`` if they *want* the outer
fork (e.g. to test a subshell env).
"""

from __future__ import annotations

import shlex

__all__ = ["looks_like_kros_nested", "dispatch_inline"]


def looks_like_kros_nested(cmd: str) -> list[str] | None:
    """Return the split argv if ``cmd`` starts with a bare ``kros``, else None.

    We refuse to peel if shlex can't parse (unbalanced quotes etc.) —
    in that case bash will error anyway, let the outer subprocess show
    the real bash error message to the agent.
    """
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] != "kros":
        return None
    return tokens


def dispatch_inline(tokens: list[str]) -> None:
    """Re-enter the typer app with the inner argv.

    We use ``standalone_mode=True`` so typer/click handle exit codes
    and error printing exactly as if the inner command had been typed
    directly. Any ``SystemExit`` raised propagates up through
    :func:`kros.cli.main` which will record the final exit code.

    Before dispatching, we rewrite the audit record's ``argv``/``subcmd``
    to point at the real inner command — the ledger stays honest.
    """
    # Import lazily to avoid a circular import (cli imports shell).
    from kros import _audit
    from kros.cli import app

    # Rewrite the in-flight audit record: ledger line will read as if
    # the user typed the inner command directly.
    _audit.rewrite_argv(["kros"] + tokens[1:])

    app(args=tokens[1:], standalone_mode=True)
