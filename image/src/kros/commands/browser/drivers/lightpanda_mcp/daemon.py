"""Session daemon for the lightpanda_mcp driver.

This lives entirely inside the ``lightpanda_mcp`` package because its
existence is a consequence of lightpanda's stdio-only MCP transport.
Other drivers (e.g. a future ``chromium_cdp`` that connects to an
existing :9222) do not need a daemon at all.

Lifecycle:

- ``daemonize_and_run()`` is called by the proxy (CLI process) when
  :func:`is_daemon_alive` is false. It double-forks, sets up the
  runtime dir, and runs :meth:`Daemon.run`.
- The daemon instantiates one :class:`LightpandaMCPEngine` (which
  spawns the ``lightpanda mcp`` child), binds a unix socket, then
  accepts one connection at a time and dispatches the incoming
  ``{op, args}`` to the engine.
- ``__shutdown__`` op tears the engine down and exits.

Wire format: newline-delimited JSON, one request -> one response.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import socket
import sys
import threading
from typing import Any

from kros.commands.browser._tabs import tab_dir
from kros.commands.browser.contract import (
    OP_SHUTDOWN,
    VALID_OPS,
    DriverError,
)

from ._paths import log_path, pid_path, socket_path
from .engine import LightpandaMCPEngine

log = logging.getLogger("kros.browser.lightpanda_mcp.daemon")


class Daemon:
    """Owns the engine, listens on unix socket, dispatches requests."""

    def __init__(self, tab_id: int) -> None:
        self._tab_id = tab_id
        self._engine: Any = None  # LightpandaMCPEngine — typed Any to avoid circular
        self._srv: socket.socket | None = None
        self._shutdown = threading.Event()
        self._engine_lock = threading.Lock()

    def run(self) -> None:
        td = tab_dir(self._tab_id)
        td.mkdir(parents=True, exist_ok=True)

        # Start the engine first; failure here is fatal and surfaced via
        # the absent socket + daemon.log tail.
        self._engine = LightpandaMCPEngine()

        sp = socket_path(self._tab_id)
        if sp.exists():
            try:
                sp.unlink()
            except OSError:
                pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sp))
        self._srv.listen(8)
        os.chmod(sp, 0o600)

        pid_path(self._tab_id).write_text(str(os.getpid()))

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        log.info(
            "lightpanda_mcp daemon ready: tab=%d pid=%d socket=%s",
            self._tab_id,
            os.getpid(),
            sp,
        )

        try:
            self._accept_loop()
        finally:
            self._cleanup()

    # --- accept loop --------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._srv is not None
        self._srv.settimeout(1.0)
        while not self._shutdown.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if e.errno in (errno.EBADF, errno.EINVAL):
                    break
                raise
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                f = conn.makefile("rwb", buffering=0)
                line = f.readline()
                if not line:
                    return
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    self._send(f, {"ok": False, "error": f"bad json: {e}"})
                    return
                with self._engine_lock:
                    resp = self._dispatch(req)
                self._send(f, resp)
                if resp.pop("_shutdown", False):
                    self._shutdown.set()
        except Exception:
            log.exception("connection handler crashed")

    @staticmethod
    def _send(f: Any, obj: dict) -> None:
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            f.write(payload)
            f.flush()
        except (BrokenPipeError, OSError):
            pass

    # --- dispatch -----------------------------------------------------

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        args = req.get("args") or {}
        if not isinstance(op, str):
            return {"ok": False, "error": "missing 'op'"}
        if op == OP_SHUTDOWN:
            return self._do_shutdown()
        if op not in VALID_OPS:
            return {"ok": False, "error": f"unknown op: {op!r}"}
        method = getattr(self._engine, op, None)
        if method is None:
            return {"ok": False, "error": f"engine lacks op {op!r}"}
        try:
            result = method(**args)
        except DriverError as e:
            return {"ok": False, "error": str(e), "type": type(e).__name__}
        except TypeError as e:
            return {"ok": False, "error": f"bad args for {op}: {e}", "type": "TypeError"}
        except Exception as e:
            log.exception("op %s crashed", op)
            return {"ok": False, "error": str(e), "type": type(e).__name__}
        return {"ok": True, "result": _to_jsonable(result)}

    def _do_shutdown(self) -> dict:
        try:
            self._engine.close()
        except Exception as e:
            log.exception("engine close failed")
            return {
                "ok": False,
                "error": f"engine.close: {e}",
                "type": type(e).__name__,
                "_shutdown": True,
            }
        return {"ok": True, "result": None, "_shutdown": True}

    # --- signals / cleanup -------------------------------------------

    def _on_signal(self, signo: int, _frame: Any) -> None:
        log.info("received signal %d, shutting down", signo)
        self._shutdown.set()

    def _cleanup(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                log.exception("engine close during cleanup failed")
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:
                pass
        for p in (socket_path(self._tab_id), pid_path(self._tab_id)):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        log.info("daemon exit")


# ---------------------------------------------------------------------------
# Daemonization
# ---------------------------------------------------------------------------


def daemonize_and_run(tab_id: int) -> None:
    """Double-fork + setsid + redirect stdio, then :meth:`Daemon.run`.

    Parent returns immediately to the proxy; child becomes a detached
    daemon (bound to ``tab_id``) and only exits via ``os._exit``.
    """
    td = tab_dir(tab_id)
    td.mkdir(parents=True, exist_ok=True)

    first_child = os.fork()
    if first_child != 0:
        # Parent: reap the short-lived first child to avoid zombies.
        try:
            os.waitpid(first_child, 0)
        except OSError:
            pass
        return

    os.setsid()

    if os.fork() != 0:
        os._exit(0)

    os.chdir(str(td))
    os.umask(0o077)
    lf = log_path(tab_id)
    logfd = os.open(str(lf), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)
    os.close(logfd)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    try:
        Daemon(tab_id).run()
    except Exception:
        log.exception("daemon crashed during startup")
        os._exit(1)
    os._exit(0)


def _to_jsonable(obj: Any) -> Any:
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj
