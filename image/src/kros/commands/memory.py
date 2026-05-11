"""`kros memory` — cross-session memory for agents. Backed by seeka.

All configuration is environment-driven so that an agent inside the Kros
container can inherit credentials from `docker run -e ...` without having
to pass flags on every invocation:

    KROS_MEMORY_HOME         Directory for all memory data.
                             Default: ~/.kros/memory
    KROS_MEMORY_NAMESPACE    Default namespace if --namespace is omitted.
                             Default: "default"
    KROS_LLM_URI             LLM URI for seeka's dream() / conflict resolution.
    KROS_LLM_API_KEY         API key for the LLM above.
    KROS_EMBEDDING_URI       Embedding URI. Falls back to local
                             sentence-transformers when unset.
    KROS_EMBEDDING_API_KEY   API key for the embedding model above.

Without an LLM URI, seeka's dream() still works but stores notes verbatim
(no extraction, no conflict resolution). Without an embedding URI, seeka
uses a bundled local model. Both downgrades are intentional and silent.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import typer
from seeka import Memory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS_HELP = "Memory partition. Defaults to $KROS_MEMORY_NAMESPACE or 'default'."


def _memory_home() -> Path:
    return Path(os.environ.get("KROS_MEMORY_HOME") or (Path.home() / ".kros" / "memory"))


def _default_namespace() -> str:
    return os.environ.get("KROS_MEMORY_NAMESPACE", "default")


def _memory(namespace: str) -> Memory:
    home = _memory_home()
    home.mkdir(parents=True, exist_ok=True)
    return Memory(
        str(home),
        namespace=namespace,
        llm_uri=os.environ.get("KROS_LLM_URI"),
        llm_api_key=os.environ.get("KROS_LLM_API_KEY"),
        embedding_uri=os.environ.get("KROS_EMBEDDING_URI"),
        embedding_api_key=os.environ.get("KROS_EMBEDDING_API_KEY"),
    )


def _resolve_ns(namespace: Optional[str]) -> str:
    return namespace or _default_namespace()


def _print_memo(memo) -> None:
    # One memo per line: "<id>\t<content>". Tab-separated so agents can
    # `cut -f1` for ids and humans can still read both columns.
    typer.echo(f"{memo.id}\t{memo.content}")


# ---------------------------------------------------------------------------
# Group definition
# ---------------------------------------------------------------------------

memory_app = typer.Typer(
    help="Cross-session memory for agents. Backed by seeka.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def register(app: typer.Typer) -> None:
    app.add_typer(memory_app, name="memory")


# ---------------------------------------------------------------------------
# Write-side subcommands
# ---------------------------------------------------------------------------


@memory_app.command()
def note(
    content: str = typer.Argument(..., help="Raw text to record."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Record raw input as a Note. Fast, no LLM call."""
    mem = _memory(_resolve_ns(namespace))
    note_id = asyncio.run(mem.note(content))
    typer.echo(note_id)


@memory_app.command()
def dream(
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Process pending notes into memos (LLM extraction + conflict resolution)."""
    mem = _memory(_resolve_ns(namespace))
    memos = asyncio.run(mem.dream())
    for m in memos:
        _print_memo(m)


@memory_app.command()
def remember(
    content: str = typer.Argument(..., help="Raw text to record and immediately process."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Convenience: note() + dream() in one call."""
    mem = _memory(_resolve_ns(namespace))
    memos = asyncio.run(mem.remember(content))
    for m in memos:
        _print_memo(m)


# ---------------------------------------------------------------------------
# Read-side subcommands
# ---------------------------------------------------------------------------


@memory_app.command()
def recall(
    query: str = typer.Argument(..., help="Semantic search query."),
    n: int = typer.Option(5, "--n", help="Number of results to return."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Semantic search over stored memos."""
    mem = _memory(_resolve_ns(namespace))
    results = asyncio.run(mem.recall(query, n=n))
    for r in results:
        _print_memo(r)


@memory_app.command("list")
def list_(
    limit: int = typer.Option(100, "--limit", help="Max items to return."),
    offset: int = typer.Option(0, "--offset", help="Paging offset."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """List stored memos, newest first."""
    mem = _memory(_resolve_ns(namespace))
    results = asyncio.run(mem.memos(limit=limit, offset=offset))
    for r in results:
        _print_memo(r)


@memory_app.command()
def get(
    memo_id: str = typer.Argument(..., help="Memo id."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Fetch a single memo by id."""
    mem = _memory(_resolve_ns(namespace))
    memo = asyncio.run(mem.get(memo_id))
    if memo is None:
        typer.secho(f"No memo with id: {memo_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(memo.content)


# ---------------------------------------------------------------------------
# Mutation subcommands
# ---------------------------------------------------------------------------


@memory_app.command()
def update(
    memo_id: str = typer.Argument(..., help="Memo id."),
    content: str = typer.Argument(..., help="New content."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Update a memo's content (re-embeds automatically)."""
    mem = _memory(_resolve_ns(namespace))
    asyncio.run(mem.update(memo_id, content))
    typer.echo(f"updated: {memo_id}")


@memory_app.command()
def delete(
    memo_id: str = typer.Argument(..., help="Memo id."),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
) -> None:
    """Delete a memo by id."""
    mem = _memory(_resolve_ns(namespace))
    asyncio.run(mem.delete(memo_id))
    typer.echo(f"deleted: {memo_id}")


@memory_app.command()
def forget(
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help=_NS_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Wipe ALL memos and pending notes for the namespace."""
    ns = _resolve_ns(namespace)
    if not yes:
        typer.confirm(
            f"This will erase everything in namespace '{ns}'. Continue?",
            abort=True,
        )
    mem = _memory(ns)
    asyncio.run(mem.forget())
    typer.echo(f"forgotten: {ns}")
