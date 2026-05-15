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
import signal
import subprocess
import sys
import threading
from typing import Optional

import typer

from ... import _audit

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
    # Pre-validate --cwd. Without this, a missing cwd would surface as a
    # FileNotFoundError from Popen indistinguishable from /bin/bash being
    # absent, leading to a misleading "bash not found" error line.
    if cwd is not None and not os.path.isdir(cwd):
        typer.secho(
            f"kros: --cwd not a directory: {cwd}",
            fg=typer.colors.RED,
            err=True,
        )
        return 126

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            bufsize=0,
            # Put the child in its own session/process group so we can kill
            # the whole tree on timeout — backgrounded grandchildren
            # (``sleep 100 & wait``) would otherwise outlive us.
            start_new_session=True,
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

    # If the downstream tty/pipe (e.g. ``kros shell run ... | head``) closes
    # while the child is still streaming, the original implementation kept
    # buffering forever — for unbounded producers (``yes``, infinite logs)
    # that meant unbounded memory growth and an apparent hang. Now we ask
    # the child to wind down so the command exits promptly while audit
    # still gets whatever made it into the buffer.
    def _on_broken_pipe() -> None:
        _signal_pgroup(proc, signal.SIGTERM)

    # We moved the child into its own session, which severs the controlling
    # tty link. Without explicit forwarding, Ctrl+C at the user's terminal
    # would only hit kros, leaving the child running. Forward SIGINT and
    # SIGTERM to the whole child process group so behavior matches a plain
    # ``bash -c`` invocation.
    _prev_sigint = signal.getsignal(signal.SIGINT)
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _forward(sig, _frame):  # pragma: no cover — signal-driven
        _signal_pgroup(proc, sig)

    try:
        signal.signal(signal.SIGINT, _forward)
        signal.signal(signal.SIGTERM, _forward)
    except ValueError:
        # Not running on the main thread (e.g. unit tests) — best effort.
        pass

    t_out = threading.Thread(
        target=_pump,
        args=(proc.stdout, sys.stdout.buffer, out_buf),
        kwargs={"on_broken": _on_broken_pipe},
        daemon=True,
    )
    t_err = threading.Thread(
        target=_pump,
        args=(proc.stderr, sys.stderr.buffer, err_buf),
        kwargs={"on_broken": _on_broken_pipe},
        daemon=True,
    )
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_with_grace(proc)
    finally:
        # Restore previous handlers no matter what — leaving _forward
        # installed would mishandle later signals during shutdown.
        try:
            signal.signal(signal.SIGINT, _prev_sigint)
            signal.signal(signal.SIGTERM, _prev_sigterm)
        except (ValueError, TypeError):
            pass

    # Join pumps so the buffers are complete before we hand them to audit.
    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)

    _audit.record_stdout(bytes(out_buf), bytes(err_buf))

    if timed_out:
        return _EXIT_TIMEOUT
    return int(proc.returncode or 0)


def _pump(reader, writer, buf: bytearray, on_broken=None) -> None:
    """Tee ``reader`` → (``writer`` + ``buf``) until EOF.

    If the downstream ``writer`` raises ``BrokenPipeError`` (e.g. user piped
    us into ``head``), stop tee'ing — keep filling ``buf`` so audit still
    has whatever the child emits next — and invoke ``on_broken`` once so
    the caller can wind down the child instead of buffering forever.
    """
    try:
        while True:
            chunk = reader.read(_PUMP_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if writer is None:
                continue
            try:
                writer.write(chunk)
                writer.flush()
            except (BrokenPipeError, ValueError):
                # Downstream is gone. Don't keep raising every chunk.
                writer = None
                if on_broken is not None:
                    try:
                        on_broken()
                    except Exception:
                        pass
    except Exception:
        # Never let a pump thread crash the parent.
        pass


def _signal_pgroup(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to the child's process group, falling back to the child.

    The child was launched with ``start_new_session=True``, so it is the
    leader of its own pgroup. ``killpg`` reaches every backgrounded
    grandchild (e.g. ``sleep 100 & wait``); a plain ``proc.terminate()``
    would only hit bash and leave the grandchildren as init's orphans.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        # pgid lookup failed (race) or no permission — fall back.
        pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _terminate_with_grace(proc: subprocess.Popen) -> None:
    """SIGTERM → wait grace period → SIGKILL (whole pgroup, not just bash)."""
    _signal_pgroup(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_GRACE_SECS)
    except subprocess.TimeoutExpired:
        _signal_pgroup(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
