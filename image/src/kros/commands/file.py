"""``kros file`` — file operations for agents.

Currently one verb:

- ``kros file read <src>`` — render any document (local path or URL)
  into LLM-ready Markdown (filename + format + size header + content),
  backed by `fyle <https://pypi.org/project/fyle/>`_.
"""

from __future__ import annotations

import typer
import fyle


_CATEGORIES = ", ".join(sorted(fyle.accepts()))

file_app = typer.Typer(
    help=f"File operations (read). Supported: {_CATEGORIES}",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@file_app.command(
    "read",
    help=f"Read a document into LLM-ready Markdown. Supported: {_CATEGORIES}",
)
def read_cmd(
    src: str = typer.Argument(..., help="Local file path or http(s) URL."),
) -> None:
    """Render ``src`` into LLM-ready Markdown via fyle."""
    try:
        doc = fyle.open(src)
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
