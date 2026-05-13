"""``kros file`` — local-file operations for agents.

Currently one verb:

- ``kros file read <path>`` — render any local document into LLM-ready
  Markdown (filename + format + size header + content), backed by
  `fyle <https://pypi.org/project/fyle/>`_.

Only **local filesystem paths** are accepted. There is no URL support:
for remote pages use ``kros browser open <url>`` instead.
"""

from __future__ import annotations

import typer
import fyle


file_app = typer.Typer(
    help="Local-file operations (read).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@file_app.command("read", help="Read a local document into LLM-ready Markdown.")
def read_cmd(
    path: str = typer.Argument(..., help="Local path to the file."),
) -> None:
    """Render ``path`` into LLM-ready Markdown via fyle."""
    try:
        doc = fyle.open(path)
    except FileNotFoundError as e:
        typer.secho(f"File not found: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    except Exception as e:  # UnsupportedFormatError / ParseError / ReaderNotFoundError / ...
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # str(doc) is fyle's LLM-ready rendering: filename + format + size header + content
    typer.echo(str(doc))


def register(app: typer.Typer) -> None:
    """Entry point called from ``kros.cli``."""
    app.add_typer(file_app, name="file")
