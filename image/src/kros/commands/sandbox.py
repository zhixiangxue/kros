"""`kros sandbox` — run code in an isolated sandbox. Backed by doka."""

from __future__ import annotations

import os
import sys
from typing import List, Optional

import typer
from doka import Limits, Sandbox


_DEFAULT_RUNTIME = "bubblewrap"


def _runtime() -> str:
    """Choose which doka runtime to use.

    Defaults to ``bubblewrap`` — daemonless, unprivileged, container-friendly.
    Override via ``KROS_SANDBOX_RUNTIME`` (e.g. ``docker`` on the host).
    """
    return os.environ.get("KROS_SANDBOX_RUNTIME", _DEFAULT_RUNTIME)


sandbox_app = typer.Typer(
    help="Isolated sandbox for running untrusted / Agent-generated code. Backed by doka.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@sandbox_app.command()
def run(
    command: str = typer.Argument(
        ...,
        help="Shell command to run inside the sandbox (wrap multi-word commands in quotes).",
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", "-t", help="Kill the command after N seconds."
    ),
    memory: str = typer.Option(
        "512m", "--memory", "-m", help="Memory limit, e.g. '512m', '1g'."
    ),
    cpu: float = typer.Option(
        1.0, "--cpu", help="CPU quota in cores. Supports fractions like 0.5."
    ),
    no_network: bool = typer.Option(
        False, "--no-network", help="Disable network access inside the sandbox."
    ),
    readonly: bool = typer.Option(
        False,
        "--readonly",
        help="Make the sandbox filesystem read-only (writes to /workspace and /tmp fail).",
    ),
    env: List[str] = typer.Option(
        [],
        "--env",
        "-e",
        help="Extra env var in KEY=VAL form (repeatable).",
    ),
    workdir: Optional[str] = typer.Option(
        None, "--workdir", "-w", help="Working directory inside the sandbox."
    ),
) -> None:
    """Run a single command in a fresh sandbox. stdout/stderr/exit-code are
    forwarded transparently so this composes like any regular Unix tool."""
    extra_env: dict[str, str] = {}
    for item in env:
        if "=" not in item:
            typer.secho(
                f"Invalid --env value {item!r}; expected KEY=VAL.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        k, v = item.split("=", 1)
        extra_env[k] = v

    limits = Limits(
        cpu=cpu,
        memory=memory,
        timeout=timeout,
        network=not no_network,
        fs_readonly=readonly,
    )

    try:
        with Sandbox(runtime=_runtime(), limits=limits) as sb:
            result = sb.commands.run(
                command,
                env=extra_env or None,
                workdir=workdir,
            )
    except FileNotFoundError as e:
        # bwrap binary not installed on the host (typical on macOS).
        typer.secho(
            f"Sandbox runtime unavailable: {e}. "
            f"On Debian/Ubuntu: 'sudo apt-get install -y bubblewrap'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=127)
    except Exception as e:
        typer.secho(f"{type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # Forward captured output transparently, Unix-style.
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    raise typer.Exit(code=result.exit_code)


@sandbox_app.command()
def info() -> None:
    """Print the active sandbox runtime and where to override it."""
    rt = _runtime()
    typer.echo(f"runtime: {rt}")
    typer.echo("override: export KROS_SANDBOX_RUNTIME=<docker|bubblewrap|cube|kata>")


def register(app: typer.Typer) -> None:
    app.add_typer(sandbox_app, name="sandbox")
