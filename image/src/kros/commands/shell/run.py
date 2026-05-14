"""``kros shell run <cmd>`` — execute one shell command, audit the result.

Design choices (see design/03-kros-audit-and-log.md):

* Interpreter: **``/bin/bash -c``** (not ``/bin/sh``). bash is always
  present in the kros container and is forgiving of bashisms LLMs tend
  to write (``[[ ]]``, ``$(( ))``, ``<( )``).
* Streaming: child stdout/stderr is piped through to the parent tty in
  real time AND tee'd into an in-memory buffer so we can attach a
  preview / spill file to the audit record.
* Timeout: optional ``--timeout SECS``. On expiry we send SIGTERM,
  wait 3s, then SIGKILL. Exit code 124 (GNU timeout convention).
* Unwrap: if ``cmd`` itself starts with ``kros``, dispatch in-process
  (one ledger line, no subprocess). Disable with ``--no-unwrap``.
* Secrets: v1 does not expand ``$secret:xxx``. Agents should pass
  secrets via ``docker run -e`` and reference them as ``$VAR`` wrapped
  in single quotes so the outer shell doesn't interpolate before the
  command reaches us.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Optional

import typer

from kros import _audit

from ._unwrap import dispatch_inline, looks_like_kros_nested

# GNU ``timeout(1)`` convention: 124 = killed by timeout.
_EXIT_TIMEOUT = 124

# Size of per-read chunks when tee'ing subprocess output.
_PUMP_CHUNK = 4096

# Grace period between SIGTERM and SIGKILL on timeout, in seconds.
_GRACE_SECS = 3.0


def run_cmd(
    cmd: str = typer.Argument(
        ...,
        metavar="CMD",
        help="Shell command string. Passed to /bin/bash -c verbatim.",
    ),
    cwd: Optional[str] = typer.Option(
        None,
        "--cwd",
        metavar="PATH",
        help="Working directory for the command. Default: inherit from caller.",
    ),
    env: list[str] = typer.Option(
        [],
        "--env",
        "-e",
        metavar="KEY=VAL",
        help="Extra env var for the child process. Repeatable.",
    ),
    timeout: Optional[float] = typer.Option(
        None,
        "--timeout",
        "-t",
        metavar="SECS",
        help="Kill the command after SECS seconds (SIGTERM, then SIGKILL). Exit 124 on timeout.",
    ),
    no_unwrap: bool = typer.Option(
        False,
        "--no-unwrap",
        help="Don't peel outer ``kros …`` — run via /bin/bash even if CMD starts with kros.",
    ),
) -> None:
    """Execute CMD, stream output, record one audit line on exit."""
    # ── unwrap: ``kros shell run "kros browser open ..."`` ──────────────
    if not no_unwrap:
        tokens = looks_like_kros_nested(cmd)
        if tokens is not None:
            # dispatch_inline raises SystemExit; kros.cli.main's finally
            # block will write the (rewritten) audit line.
            dispatch_inline(tokens)
            return  # unreachable, but keeps linters happy

    # ── subprocess path ────────────────────────────────────────────────
    child_env = _build_child_env(env)
    exit_code = _spawn_and_wait(cmd, cwd=cwd, env=child_env, timeout=timeout)
    raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_child_env(extra: list[str]) -> dict[str, str]:
    """Start from the current env, overlay ``KEY=VAL`` pairs from ``--env``."""
    env = os.environ.copy()
    for item in extra:
        if "=" not in item:
            raise typer.BadParameter(
                f"--env expects KEY=VAL, got: {item!r}",
                param_hint="--env",
            )
        k, _, v = item.partition("=")
        if not k:
            raise typer.BadParameter(
                f"--env key must not be empty: {item!r}",
                param_hint="--env",
            )
        env[k] = v
    return env


def _spawn_and_wait(
    cmd: str,
    *,
    cwd: Optional[str],
    env: dict[str, str],
    timeout: Optional[float],
) -> int:
    """Run bash -c CMD, tee output, enforce timeout, return exit code."""
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            bufsize=0,
        )
    except FileNotFoundError:
        typer.secho(
            "kros: /bin/bash not found — this image is broken.",
            fg=typer.colors.RED,
            err=True,
        )
        return 127
    except OSError as e:
        typer.secho(f"kros: failed to spawn: {e}", fg=typer.colors.RED, err=True)
        return 126

    out_buf = bytearray()
    err_buf = bytearray()
    t_out = threading.Thread(
        target=_pump,
        args=(proc.stdout, sys.stdout.buffer, out_buf),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_pump,
        args=(proc.stderr, sys.stderr.buffer, err_buf),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_with_grace(proc)

    # Join pumps so the buffers are complete before we hand them to audit.
    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)

    _audit.record_stdout(bytes(out_buf), bytes(err_buf))

    if timed_out:
        return _EXIT_TIMEOUT
    return int(proc.returncode or 0)


def _pump(reader, writer, buf: bytearray) -> None:
    """Tee ``reader`` → (``writer`` + ``buf``) until EOF."""
    try:
        while True:
            chunk = reader.read(_PUMP_CHUNK)
            if not chunk:
                break
            try:
                writer.write(chunk)
                writer.flush()
            except (BrokenPipeError, ValueError):
                # tty closed (e.g. user piped to ``head``); keep buffering
                # so the ledger still sees the full output.
                pass
            buf.extend(chunk)
    except Exception:
        # Never let a pump thread crash the parent.
        pass


def _terminate_with_grace(proc: subprocess.Popen) -> None:
    """SIGTERM → wait grace period → SIGKILL."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_GRACE_SECS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
