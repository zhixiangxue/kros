"""`kros read` — read any document into LLM-ready Markdown. Backed by fyle."""

from __future__ import annotations

import typer
import fyle


def register(app: typer.Typer) -> None:
    @app.command(help="Read any document (local path or http(s) URL) into LLM-ready Markdown.")
    def read(
        src: str = typer.Argument(..., help="Local path or http(s):// URL"),
    ) -> None:
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
