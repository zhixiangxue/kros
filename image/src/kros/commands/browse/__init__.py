"""`kros browse` — headless browser for LLM agents. Backed by Lightpanda + CDP.

The subcommand tree:

- ``kros browse get <url>``           — LLM-ready Markdown (fast path, no CDP)
- ``kros browse get <url> --selector`` / ``--eval``  — DOM query over CDP
- ``kros browse interact <url> --script <file.py>``  — run a Python script
  with live ``browser``, ``context``, ``page`` globals (raw Playwright sync API)
- ``kros browse serve``               — foreground CDP server (shareable)
- ``kros browse info``                — inspect active driver + endpoint
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Optional

import typer

from .driver import get_driver, get_endpoint
from .session import browse_session


browse_app = typer.Typer(
    help="Headless browser for LLM agents. Backed by Lightpanda + CDP.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@browse_app.command("get")
def get_cmd(
    url: str = typer.Argument(..., help="URL to fetch."),
    selector: Optional[str] = typer.Option(
        None,
        "--selector",
        "-s",
        help="CSS selector; print the matching element's inner_text instead of the full page markdown.",
    ),
    eval_js: Optional[str] = typer.Option(
        None,
        "--eval",
        help="JavaScript expression to evaluate on the page; print the return value.",
    ),
    wait_until: str = typer.Option(
        "domcontentloaded",
        "--wait-until",
        help="Page load state to wait for: load, domcontentloaded, networkidle.",
    ),
    timeout: float = typer.Option(
        30.0, "--timeout", "-t", help="Overall timeout in seconds."
    ),
) -> None:
    """Fetch a URL.

    Default path: return LLM-ready Markdown via ``lightpanda fetch --dump markdown``
    (no CDP, no server, minimum latency).

    With ``--selector`` or ``--eval``: take the CDP path so we can query the
    live DOM / evaluate JS against the loaded page.
    """
    driver = get_driver()

    # Fast path: plain dump, no DOM query → skip CDP entirely.
    if selector is None and eval_js is None:
        try:
            md = driver.fetch_markdown(url, timeout=timeout)
        except FileNotFoundError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=127)
        except Exception as e:
            typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        sys.stdout.write(md)
        if md and not md.endswith("\n"):
            sys.stdout.write("\n")
        return

    # DOM path: connect (or spawn+connect) over CDP, run one query, exit.
    try:
        with browse_session(driver=driver) as (_browser, _ctx, page):
            page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            if selector is not None:
                text = page.locator(selector).first.inner_text()
                sys.stdout.write(text)
                if not text.endswith("\n"):
                    sys.stdout.write("\n")
            elif eval_js is not None:
                val = page.evaluate(eval_js)
                sys.stdout.write(f"{val}\n")
    except FileNotFoundError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=127)
    except Exception as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@browse_app.command()
def interact(
    url: str = typer.Argument(..., help="URL to open before running the script."),
    script: Path = typer.Option(
        ...,
        "--script",
        help="Path to a Python file. It runs with live `browser`, `context`, `page` globals (raw Playwright sync API).",
    ),
    wait_until: str = typer.Option(
        "domcontentloaded",
        "--wait-until",
        help="Page load state to wait for before handing control to the script.",
    ),
    timeout: float = typer.Option(
        60.0, "--timeout", "-t", help="Initial page load timeout in seconds."
    ),
) -> None:
    """Open a URL, then run a user Python script against the live page.

    The script sees three globals — ``browser``, ``context``, ``page`` — from
    the raw Playwright sync API. Example script::

        page.fill('input[name=q]', 'kros')
        page.click('button[type=submit]')
        print(page.locator('h1').inner_text())
    """
    if not script.exists():
        typer.secho(f"Script not found: {script}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        with browse_session() as (browser, ctx, page):
            page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            init_globals = {
                "browser": browser,
                "context": ctx,
                "page": page,
                "__file__": str(script),
            }
            runpy.run_path(
                str(script),
                init_globals=init_globals,
                run_name="__kros_browse_script__",
            )
    except FileNotFoundError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=127)
    except Exception as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@browse_app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    port: int = typer.Option(9222, "--port", help="Port to bind."),
) -> None:
    """Run the driver's CDP server in the foreground. Ctrl+C to stop.

    Useful for sharing one long-lived browser across multiple tools, or for
    external Playwright / Puppeteer / chromedp clients.
    """
    driver = get_driver()
    try:
        bin_path = driver.binary_path()
    except FileNotFoundError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=127)

    cmd = [
        bin_path,
        "serve",
        "--host", host,
        "--port", str(port),
        "--log-format", "pretty",
        "--log-level", "info",
    ]
    typer.echo(f"[kros browse serve] exec: {' '.join(cmd)}", err=True)
    # exec so Ctrl+C / signals go straight to the driver, no PID indirection.
    os.execvp(cmd[0], cmd)


@browse_app.command()
def info() -> None:
    """Print the active driver, binary, version, and CDP endpoint status."""
    driver = get_driver()
    endpoint = get_endpoint()
    typer.echo(f"driver:   {driver.name}")
    try:
        typer.echo(f"binary:   {driver.binary_path()}")
        typer.echo(f"version:  {driver.version()}")
    except FileNotFoundError as e:
        typer.echo(f"binary:   NOT FOUND ({e})")
    typer.echo(f"cdp:      {endpoint.ws_url}")
    typer.echo(f"alive:    {driver.is_alive(endpoint)}")
    typer.echo(
        "override: KROS_BROWSE_DRIVER / KROS_BROWSE_LIGHTPANDA_BIN / "
        "KROS_BROWSE_CDP_HOST / KROS_BROWSE_CDP_PORT"
    )


def register(app: typer.Typer) -> None:
    app.add_typer(browse_app, name="browse")
