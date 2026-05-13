"""``kros browse`` — agent-first headless browser CLI.

Sixteen commands total. The 14 atomic ops work on a **current tab**
(implicit), just like a human flips between tabs in a real browser;
``list`` and ``switch`` manage that implicit pointer.

**Tab model (agent mental model = human using a browser)**

- ``open URL`` opens a new tab and makes it current, returning its
  ``TAB: <id>`` and a full ``read``-style snapshot in one round-trip.
- Every other command (``read`` / ``click`` / ``fill`` / ...) operates
  on the current tab by default, so command lines stay short.
- ``list`` shows every live tab with id / url / title; ``*`` marks the
  current one, so an agent glances once and knows where to go next.
- ``switch --tab N`` changes the current pointer.
- ``--tab N`` on any command is an escape hatch to target a specific
  tab without changing the current pointer.

**Tiers**

- Tier 1 (core 6): open / read / click / fill / close / info
- Tier 2 (high-value 4): find / wait / scroll / eval
- Tier 3 (completion 4): press / hover / select / check
- Tab management (2): list / switch

The CLI is entirely driver-neutral: every command resolves a driver
bound to a tab id via :func:`kros.commands.browse.drivers.get_driver`
and calls the matching method on it. Whether the driver spawns a
daemon, connects to a remote CDP endpoint, or talks to a cloud API is
the driver's private business.
"""

from __future__ import annotations

import re
import shutil
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import typer

from kros.commands.browse import formatting
from kros.commands.browse._tabs import (
    allocate_next_tab_id,
    list_tab_ids,
    read_current_tab,
    tab_dir,
    write_current_tab,
)
from kros.commands.browse.contract import (
    BrowseDriver,
    DriverError,
    NavigationTimeoutError,
    NoSessionError,
    SessionExistsError,
)
from kros.commands.browse.drivers import get_driver


browse_app = typer.Typer(
    help="Agent-first headless browser. Tabs with an implicit 'current'; "
    "16 commands (14 atomic ops + list/switch).",
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
EXIT_NAV_TIMEOUT = 5

# RFC-3986-ish scheme test. We require an explicit scheme on ``open`` so
# that an agent never sees the generic "navigation did not complete" error
# for a trivially fixable input like ``example.com``.
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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
    except NavigationTimeoutError as e:
        # Distinct from generic DriverError: agent can retry with a
        # larger --timeout instead of giving up.
        typer.secho(f"NavigationTimeout: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_NAV_TIMEOUT)
    except DriverError as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    except (OSError, TimeoutError) as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_GENERIC)


def _driver(tab_id: int) -> BrowseDriver:
    return get_driver(tab_id)


def _resolve_tab(explicit: Optional[int]) -> int:
    """Return the tab id a command should target.

    ``--tab`` wins; otherwise fall back to the current-tab pointer;
    otherwise raise :class:`NoSessionError` so the agent knows to
    ``open`` a new tab first.
    """
    if explicit is not None:
        return explicit
    cur = read_current_tab()
    if cur is None:
        raise NoSessionError(
            "no active tab; run `kros browse open <url>` first "
            "(or `kros browse list` to see tabs)"
        )
    return cur


def _echo(s: str) -> None:
    sys.stdout.write(s.rstrip() + "\n")


def _dump(obj: Any) -> dict:
    """Pydantic v2 ``model_dump``; fall back to dict() if already plain."""
    dump = getattr(obj, "model_dump", None)
    return dump() if callable(dump) else dict(obj)


def _quote(s: str) -> str:
    """Minimal shell-friendly quoting for embedding free-text in list output."""
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _tab_option() -> Any:
    """Shared ``--tab`` flag; default None means 'current tab'."""
    return typer.Option(
        None,
        "--tab",
        help="Target a specific tab id (default: current tab).",
    )


# ---------------------------------------------------------------------------
# Tier 1 — core
# ---------------------------------------------------------------------------


@browse_app.command("open")
def open_cmd(
    url: str = typer.Argument(..., help="URL to open (must include scheme)."),
    timeout: float = typer.Option(
        5.0,
        "--timeout",
        "-t",
        help="Max seconds to wait for navigation. Raise for slow CDNs or "
        "heavy pages (exit 5 on timeout so an agent can detect and retry).",
    ),
) -> None:
    """Open ``url`` in a new tab, make it current, and snapshot the page.

    Returns ``TAB: <id>`` plus the same four-section payload as ``read``
    (URL / TITLE / MARKDOWN / ELEMENTS) so an agent gets page content in
    a single round-trip — no separate ``read`` is needed. Every call
    creates a fresh tab (no URL de-dup); close old ones with ``close``
    or ``close --all``.

    A URL without a scheme (e.g. bare ``example.com``) is rejected as
    ``BadInput`` (exit 4) with a fix hint.
    """
    if not _URL_SCHEME_RE.match(url):
        typer.secho(
            f"invalid URL: {url!r} is missing a scheme. "
            f"Prefix with https:// (e.g. 'https://{url}').",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_BAD_INPUT)

    new_tab = allocate_next_tab_id()
    try:
        with _handle_errors():
            result = _driver(new_tab).open(url, timeout_ms=int(timeout * 1000))
            write_current_tab(new_tab)
            _echo(f"TAB: {new_tab}")
            _echo(formatting.format_read_result(_dump(result)))
    except BaseException:
        # Open failed: scrub the allocated tab dir so `list` doesn't show
        # a ghost. The tab id itself is burned (counter is monotonic).
        shutil.rmtree(tab_dir(new_tab), ignore_errors=True)
        raise


@browse_app.command("read")
def read_cmd(
    tab: Optional[int] = _tab_option(),
    selector: Optional[str] = typer.Option(
        None,
        "--selector",
        "-s",
        help="Scope the snapshot to the subtree matched by this CSS selector. "
        "(V1: not yet implemented — run without --selector for now.)",
    ),
) -> None:
    """Snapshot the current (or ``--tab``) page: URL, title, markdown, elements."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        result = _driver(tid).read(selector=selector)
        _echo(formatting.format_read_result(_dump(result)))


@browse_app.command("click")
def click_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref (from `read` or `find`)."),
    timeout: float = typer.Option(
        5.0,
        "--timeout",
        "-t",
        help="Max seconds for the click-fallback navigation. When lightpanda's "
        "click doesn't auto-follow <a href> (the common case), we goto(href) "
        "ourselves — this is the budget for that. Raise for slow CDNs; exit 5 "
        "on timeout so an agent can retry with a larger value.",
    ),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Click the element identified by ``--ref`` on the current (or ``--tab``) page."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).click(ref=ref, timeout_ms=int(timeout * 1000))
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("fill")
def fill_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref of the input field."),
    value: str = typer.Option(..., "--value", help="Text to write into the field."),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Type ``--value`` into the input/textarea identified by ``--ref``."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).fill(ref=ref, value=value)
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("close")
def close_cmd(
    tab: Optional[int] = _tab_option(),
    all_tabs: bool = typer.Option(
        False,
        "--all",
        help="Close every tab (and clear the current-tab pointer).",
    ),
) -> None:
    """Close a tab (default: current). Idempotent.

    - ``close`` with no args: close current; current jumps to any other
      alive tab, or is cleared if none remain.
    - ``close --tab N``: close tab N (current unchanged unless N was it).
    - ``close --all``: close every tab.
    """
    with _handle_errors():
        if all_tabs:
            closed = 0
            for tid in list_tab_ids():
                try:
                    if _driver(tid).info().alive:
                        _driver(tid).close()
                        closed += 1
                except Exception:
                    pass
                shutil.rmtree(tab_dir(tid), ignore_errors=True)
            write_current_tab(None)
            _echo(f"closed all. ({closed} alive)")
            return

        cur = read_current_tab()
        tid = tab if tab is not None else cur
        if tid is None:
            _echo("closed.")
            return

        try:
            _driver(tid).close()
        except Exception:
            pass
        shutil.rmtree(tab_dir(tid), ignore_errors=True)

        # If we just closed the current tab, advance to any remaining
        # alive tab (or clear if none).
        if tid == cur:
            next_tid: Optional[int] = None
            for x in list_tab_ids():
                try:
                    if _driver(x).info().alive:
                        next_tid = x
                        break
                except Exception:
                    continue
            write_current_tab(next_tid)
        _echo("closed.")


@browse_app.command("reset")
def reset_cmd() -> None:
    """Close every tab — shortcut for ``close --all``.

    Useful after a navigation failure (which auto-closes the affected
    tab but leaves sibling tabs untouched) or any time you want a clean
    slate before starting a new browsing task.
    """
    # Delegate to close_cmd's --all branch so there is a single source
    # of truth for "tear everything down".
    close_cmd(tab=None, all_tabs=True)


@browse_app.command("info")
def info_cmd(
    tab: Optional[int] = _tab_option(),
) -> None:
    """Show current tab's state (or ``--tab``'s). Prints 'ALIVE: no' if none."""
    with _handle_errors():
        try:
            tid = _resolve_tab(tab)
        except NoSessionError:
            _echo(formatting.format_session_info({"alive": False}))
            return
        info = _driver(tid).info()
        _echo(formatting.format_session_info(_dump(info)))


# ---------------------------------------------------------------------------
# Tab management
# ---------------------------------------------------------------------------


@browse_app.command("list")
def list_cmd() -> None:
    """List every live tab with id / url / title. ``*`` marks the current."""
    with _handle_errors():
        cur = read_current_tab()
        lines: list[str] = []
        for tid in list_tab_ids():
            try:
                info = _driver(tid).info()
            except Exception:
                continue
            if not info.alive:
                continue
            marker = "*" if tid == cur else " "
            lines.append(
                f"{marker} tab={tid}  url={info.url}  title={_quote(info.title)}"
            )
        if not lines:
            _echo("TABS (0)\n(no open tabs; run `kros browse open <url>`)")
            return
        _echo(f"TABS ({len(lines)})")
        for line in lines:
            _echo(line)
        _echo("('*' = current tab)")


@browse_app.command("switch")
def switch_cmd(
    tab: int = typer.Option(..., "--tab", help="Tab id to make current."),
) -> None:
    """Change the current-tab pointer to ``--tab`` (must be alive)."""
    try:
        info = _driver(tab).info()
    except Exception:
        info = None
    if info is None or not info.alive:
        typer.secho(
            f"tab {tab} not found or not alive (see `kros browse list`)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_BAD_INPUT)
    write_current_tab(tab)
    _echo(f"switched to tab {tab}")
    # Mental model: switching a tab is like looking at that tab — the
    # content should be visible immediately, without a separate `read`
    # step. Snapshot the target tab and print its full state.
    with _handle_errors():
        result = _driver(tab).read()
        _echo(formatting.format_read_result(_dump(result)))


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
    tab: Optional[int] = _tab_option(),
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
        tid = _resolve_tab(tab)
        result = _driver(tid).find(role=role, name=name)
        _echo(formatting.format_find_result(_dump(result)))


@browse_app.command("wait")
def wait_cmd(
    selector: str = typer.Option(
        ..., "--selector", "-s", help="CSS selector to wait for."
    ),
    timeout_ms: int = typer.Option(
        5000, "--timeout-ms", help="Max milliseconds to wait."
    ),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Block until ``--selector`` is in the DOM. Prints ``ref=<int>`` on success."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        ref = _driver(tid).wait(selector=selector, timeout_ms=timeout_ms)
        _echo(f"ref={ref}")


@browse_app.command("scroll")
def scroll_cmd(
    ref: Optional[int] = typer.Option(
        None, "--ref", help="Scroll this element into view."
    ),
    x: Optional[int] = typer.Option(None, "--x", help="Scroll the page by x pixels."),
    y: Optional[int] = typer.Option(None, "--y", help="Scroll the page by y pixels."),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Scroll the page, or bring an element into view."""
    if ref is None and x is None and y is None:
        typer.secho("scroll needs --ref or --x/--y", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_BAD_INPUT)
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).scroll(ref=ref, x=x, y=y)
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("eval")
def eval_cmd(
    script: str = typer.Option(
        ..., "--script", help="JavaScript expression to evaluate on the current page."
    ),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Escape hatch: run arbitrary JS, print the stringified return value."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        val = _driver(tid).eval(script=script)
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
    tab: Optional[int] = _tab_option(),
) -> None:
    """Send a keydown+keyup pair."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).press(key=key, ref=ref)
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("hover")
def hover_cmd(
    ref: int = typer.Option(..., "--ref", help="Element ref to hover."),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Hover over an element."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).hover(ref=ref)
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("select")
def select_cmd(
    ref: int = typer.Option(..., "--ref", help="Ref of the <select> element."),
    value: str = typer.Option(..., "--value", help="The option value to select."),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Pick an option in a ``<select>`` dropdown by value."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).select(ref=ref, value=value)
        _echo(formatting.format_read_result(_dump(state)))


@browse_app.command("check")
def check_cmd(
    ref: int = typer.Option(..., "--ref", help="Ref of the checkbox/radio."),
    checked: bool = typer.Option(
        ..., "--checked", help="True to check, False to uncheck."
    ),
    tab: Optional[int] = _tab_option(),
) -> None:
    """Set the checked state of a checkbox / radio."""
    with _handle_errors():
        tid = _resolve_tab(tab)
        state = _driver(tid).check(ref=ref, checked=checked)
        _echo(formatting.format_read_result(_dump(state)))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Entry point called from ``kros.cli``."""
    app.add_typer(browse_app, name="browse")
