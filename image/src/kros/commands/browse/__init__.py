"""``kros browse`` — agent-first headless browser CLI.

Fourteen atomic commands, grouped by tier:

**Tier 1 — core (6)**
    open / read / click / fill / close / info

**Tier 2 — high-value (4)**
    find / wait / scroll / eval

**Tier 3 — completion (4)**
    press / hover / select / check

The CLI is entirely driver-neutral: every command resolves a driver via
:func:`kros.commands.browse.drivers.get_driver`, calls the matching
method on it, and formats the return value. Whether the driver spawns a
daemon, connects to a remote CDP endpoint, or talks to a cloud API is
the driver's private business.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import typer

from kros.commands.browse import formatting
from kros.commands.browse.contract import (
    BrowseDriver,
    DriverError,
    NoSessionError,
    SessionExistsError,
)
from kros.commands.browse.drivers import get_driver


browse_app = typer.Typer(
    help="Agent-first headless browser. One session, 14 atomic ops.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# Exit codes — stable so agents can branch on them.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_NO_SESSION = 2
EXIT_SESSION_EXISTS = 3
EXIT_BAD_INPUT = 4


@contextmanager
def _handle_errors() -> Iterator[None]:
    """Translate driver errors into sensible stderr + exit codes."""
    try:
        yield
    except NoSessionError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_NO_SESSION)
    except SessionExistsError as e:
        typer.secho(f"SessionExists: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_SESSION_EXISTS)
    except DriverError as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    except (OSError, TimeoutError) as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_GENERIC)


def _driver() -> BrowseDriver:
    return get_driver()


def _echo(s: str) -> None:
    sys.stdout.write(s.rstrip() + "\n")


def _dump(obj: Any) -> dict:
    """Pydantic v2 ``model_dump``; fall back to dict() if already plain."""
    dump = getattr(obj, "model_dump", None)
    return dump() if callable(dump) else dict(obj)


# ---------------------------------------------------------------------------
# Tier 1 — core
# ---------------------------------------------------------------------------


@browse_app.command("open")
def open_cmd(
    url: str = typer.Argument(..., help="URL to open in the new session."),
    timeout: float = typer.Option(
        30.0,
        "--timeout",
        "-t",
        help="Max seconds to wait for the initial goto to complete.",
    ),
) -> None:
    """Start a new browse session and navigate to ``url``.

    Fails with ``SessionExists`` (exit 3) if a session is already live —
    call ``kros browse close`` first.
    """
    with _handle_errors():
        _driver().open(url, timeout_ms=int(timeout * 1000))
        _echo(formatting.format_session_info(_dump(_driver().info())))


@browse_app.command("read")
def read_cmd(
    selector: Optional[str] = typer.Option(
        None,
        "--selector",
        "-s",
        help="Scope the snapshot to the subtree matched by this CSS selector. "
        "(V1: not yet implemented — run without --selector for now.)",
    ),
) -> None:
    """Snapshot the current page: URL, title, markdown, and ref'd elements."""
    with _handle_errors():
        result = _driver().read(selector=selector)
        _echo(formatting.format_read_result(_dump(result)))


@browse_app.command("click")
def click_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref (from `read` or `find`)."),
) -> None:
    """Click the element identified by ``--ref``."""
    with _handle_errors():
        state = _driver().click(ref=ref)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("fill")
def fill_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref of the input field."),
    value: str = typer.Option(..., "--value", help="Text to write into the field."),
) -> None:
    """Type ``--value`` into the input/textarea identified by ``--ref``."""
    with _handle_errors():
        state = _driver().fill(ref=ref, value=value)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("close")
def close_cmd() -> None:
    """Close the current session.

    Idempotent: doing this when no session exists is a silent no-op.
    """
    with _handle_errors():
        _driver().close()
        _echo("closed.")


@browse_app.command("info")
def info_cmd() -> None:
    """Show the current session's state (or 'no session' if none)."""
    with _handle_errors():
        info = _driver().info()
        _echo(formatting.format_session_info(_dump(info)))


# ---------------------------------------------------------------------------
# Tier 2 — high-value
# ---------------------------------------------------------------------------


@browse_app.command("find")
def find_cmd(
    role: Optional[str] = typer.Option(
        None, "--role", help="ARIA role to match (button, link, textbox, ...)."
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Substring of the element's accessible name (case-insensitive).",
    ),
) -> None:
    """Locate elements by ARIA role and/or accessible name.

    At least one of ``--role`` / ``--name`` is required.
    """
    if role is None and name is None:
        typer.secho(
            "find needs at least --role or --name (use `read` for the full list)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_BAD_INPUT)
    with _handle_errors():
        result = _driver().find(role=role, name=name)
        _echo(formatting.format_find_result(_dump(result)))


@browse_app.command("wait")
def wait_cmd(
    selector: str = typer.Option(
        ..., "--selector", "-s", help="CSS selector to wait for."
    ),
    timeout_ms: int = typer.Option(
        5000, "--timeout-ms", help="Max milliseconds to wait."
    ),
) -> None:
    """Block until an element matching ``--selector`` is in the DOM.

    Prints ``ref=<int>`` on success (agents can pipe into ``click --ref``).
    """
    with _handle_errors():
        ref = _driver().wait(selector=selector, timeout_ms=timeout_ms)
        _echo(f"ref={ref}")


@browse_app.command("scroll")
def scroll_cmd(
    ref: Optional[int] = typer.Option(
        None, "--ref", help="Scroll this element into view."
    ),
    x: Optional[int] = typer.Option(None, "--x", help="Scroll the page by x pixels."),
    y: Optional[int] = typer.Option(None, "--y", help="Scroll the page by y pixels."),
) -> None:
    """Scroll the page, or bring an element into view."""
    if ref is None and x is None and y is None:
        typer.secho("scroll needs --ref or --x/--y", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_BAD_INPUT)
    with _handle_errors():
        state = _driver().scroll(ref=ref, x=x, y=y)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("eval")
def eval_cmd(
    script: str = typer.Option(
        ..., "--script", help="JavaScript expression to evaluate on the current page."
    ),
) -> None:
    """Escape hatch: run arbitrary JS, print the stringified return value."""
    with _handle_errors():
        val = _driver().eval(script=script)
        _echo(str(val))


# ---------------------------------------------------------------------------
# Tier 3 — completion
# ---------------------------------------------------------------------------


@browse_app.command("press")
def press_cmd(
    key: str = typer.Option(
        ..., "--key", help="Key to press (e.g. 'Enter', 'Tab', 'a')."
    ),
    ref: Optional[int] = typer.Option(
        None, "--ref", help="Focus this element before pressing."
    ),
) -> None:
    """Send a keydown+keyup pair."""
    with _handle_errors():
        state = _driver().press(key=key, ref=ref)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("hover")
def hover_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref to hover."),
) -> None:
    """Hover over an element."""
    with _handle_errors():
        state = _driver().hover(ref=ref)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("select")
def select_cmd(
    ref: int = typer.Option(..., "--ref", help="Ref of the <select> element."),
    value: str = typer.Option(..., "--value", help="The option value to select."),
) -> None:
    """Pick an option in a ``<select>`` dropdown by value."""
    with _handle_errors():
        state = _driver().select(ref=ref, value=value)
        _echo(formatting.format_page_state(_dump(state)))


@browse_app.command("check")
def check_cmd(
    ref: int = typer.Option(..., "--ref", help="Ref of the checkbox/radio."),
    checked: bool = typer.Option(
        ..., "--checked", help="True to check, False to uncheck."
    ),
) -> None:
    """Set the checked state of a checkbox / radio."""
    with _handle_errors():
        state = _driver().check(ref=ref, checked=checked)
        _echo(formatting.format_page_state(_dump(state)))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Entry point called from ``kros.cli``."""
    app.add_typer(browse_app, name="browse")
