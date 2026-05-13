"""CLI-side proxy driver for lightpanda_mcp.

Implements :class:`kros.commands.browse.contract.BrowseDriver` by
forwarding every method call to a long-lived daemon over a unix
socket. The daemon in turn calls the real engine.

All the "is there a socket? fork the daemon? wait for it to be ready?"
logic lives here, strictly inside the lightpanda_mcp package. The CLI
(``commands/browse/__init__.py``) treats this class as an ordinary
``BrowseDriver`` and knows nothing about sockets or daemons.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Optional

from kros.commands.browse.contract import (
    BrowseDriver,
    DriverError,
    FindResult,
    NavigationTimeoutError,
    NoSessionError,
    PageState,
    ReadResult,
    SessionInfo,
    SessionExistsError,
    OP_SHUTDOWN,
)

from . import daemon as daemon_mod
from ._paths import is_daemon_alive, pid_path, socket_path


_DEFAULT_OP_TIMEOUT_S = 60.0
_DAEMON_READY_TIMEOUT_S = 30.0


class LightpandaMCPDriver(BrowseDriver):
    """BrowseDriver impl that fronts a lightpanda_mcp daemon.

    One instance is scoped to **one tab**. The tab id selects which
    ``~/.kros/browse/tabs/<id>/`` the daemon lives under; independent
    tabs never collide on sockets, pid files, or lightpanda processes.

    Instances are cheap to create — they hold no persistent resources.
    State lives entirely in the daemon process.
    """

    name = "lightpanda_mcp"

    def __init__(self, tab_id: int) -> None:
        self._tab_id = tab_id

    # --- tier 1 ------------------------------------------------------

    def open(self, url: str, *, timeout_ms: int = 5000) -> ReadResult:
        if is_daemon_alive(self._tab_id):
            raise SessionExistsError()

        # Clean up stale artifacts from a previously crashed daemon.
        for p in (socket_path(self._tab_id), pid_path(self._tab_id)):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

        # Fork the daemon for this tab. Returns in parent; child is
        # detached and running Daemon.run().
        daemon_mod.daemonize_and_run(self._tab_id)

        # Wait until the daemon has spawned lightpanda and bound its
        # socket (the last step before accept()).
        deadline = time.monotonic() + _DAEMON_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if is_daemon_alive(self._tab_id):
                break
            time.sleep(0.1)
        else:
            raise DriverError(
                "lightpanda_mcp daemon did not become ready within "
                f"{_DAEMON_READY_TIMEOUT_S:g}s; check "
                f"~/.kros/browse/tabs/{self._tab_id}/daemon.log"
            )

        # First RPC: navigation + initial snapshot in one round-trip.
        # Engine.open returns a ReadResult (full page content) so the
        # agent doesn't need a follow-up `read` just to see what loaded.
        try:
            result = self._rpc(
                "open",
                {"url": url, "timeout_ms": timeout_ms},
                op_timeout_s=max(30.0, timeout_ms / 1000 + 15.0),
            )
        except DriverError:
            # Navigation failed — tear down the daemon we just spawned
            # so the user isn't stuck with SessionExists on retry.
            try:
                self.close()
            except Exception:
                pass
            raise
        return ReadResult.model_validate(result)

    def read(self, *, selector: Optional[str] = None) -> ReadResult:
        result = self._rpc("read", {"selector": selector})
        return ReadResult.model_validate(result)

    def click(self, *, ref: int, timeout_ms: int = 5000) -> PageState:
        # IPC budget needs room for: click (fast) + fallback goto
        # (timeout_ms) + best-effort restore (~10s) + slack.
        return PageState.model_validate(
            self._rpc(
                "click",
                {"ref": ref, "timeout_ms": timeout_ms},
                op_timeout_s=max(30.0, timeout_ms / 1000 + 20.0),
            )
        )

    def fill(self, *, ref: int, value: str) -> PageState:
        return PageState.model_validate(
            self._rpc("fill", {"ref": ref, "value": value})
        )

    def close(self) -> None:
        """Idempotent: no-op if this tab's daemon is not running."""
        if not is_daemon_alive(self._tab_id):
            return
        try:
            self._rpc(OP_SHUTDOWN, {})
        except DriverError:
            pass

        # Wait for the daemon to finish tearing down (socket goes away).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not socket_path(self._tab_id).exists():
                return
            time.sleep(0.05)

        # Last resort: SIGTERM the daemon by pid.
        try:
            pid = int(pid_path(self._tab_id).read_text().strip())
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass

    def info(self) -> SessionInfo:
        if not is_daemon_alive(self._tab_id):
            return SessionInfo(alive=False, driver=self.name)
        result = self._rpc("info", {})
        info = SessionInfo.model_validate(result)
        # Enrich with daemon-level facts the engine can't know.
        try:
            dp = int(pid_path(self._tab_id).read_text().strip())
        except (OSError, ValueError):
            dp = None
        return info.model_copy(
            update={
                "daemon_pid": dp,
                "socket_path": str(socket_path(self._tab_id)),
            }
        )

    # --- tier 2 ------------------------------------------------------

    def find(
        self, *, role: Optional[str] = None, name: Optional[str] = None
    ) -> FindResult:
        return FindResult.model_validate(
            self._rpc("find", {"role": role, "name": name})
        )

    def wait(self, *, selector: str, timeout_ms: int = 5000) -> int:
        return int(
            self._rpc(
                "wait",
                {"selector": selector, "timeout_ms": timeout_ms},
                op_timeout_s=max(10.0, timeout_ms / 1000 + 5.0),
            )
        )

    def scroll(
        self,
        *,
        ref: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> PageState:
        return PageState.model_validate(
            self._rpc("scroll", {"ref": ref, "x": x, "y": y})
        )

    def eval(self, *, script: str) -> str:
        return str(self._rpc("eval", {"script": script}) or "")

    # --- tier 3 ------------------------------------------------------

    def press(self, *, key: str, ref: Optional[int] = None) -> PageState:
        return PageState.model_validate(
            self._rpc("press", {"key": key, "ref": ref})
        )

    def hover(self, *, ref: int) -> PageState:
        return PageState.model_validate(self._rpc("hover", {"ref": ref}))

    def select(self, *, ref: int, value: str) -> PageState:
        return PageState.model_validate(
            self._rpc("select", {"ref": ref, "value": value})
        )

    def check(self, *, ref: int, checked: bool) -> PageState:
        return PageState.model_validate(
            self._rpc("check", {"ref": ref, "checked": checked})
        )

    # --- RPC internals -----------------------------------------------

    def _rpc(
        self,
        op: str,
        args: dict,
        *,
        op_timeout_s: float = _DEFAULT_OP_TIMEOUT_S,
    ) -> Any:
        """Send one {op,args} to the daemon, return parsed result.

        Raises :class:`NoSessionError` if the daemon is unreachable,
        :class:`DriverError` (or a subclass) for any daemon-reported
        failure. Callers should translate the returned plain Python
        structure into the appropriate Pydantic model.
        """
        if not is_daemon_alive(self._tab_id):
            raise NoSessionError()
        sp = socket_path(self._tab_id)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(op_timeout_s)
        try:
            try:
                s.connect(str(sp))
            except (ConnectionRefusedError, FileNotFoundError) as e:
                raise NoSessionError() from e
            s.sendall((json.dumps({"op": op, "args": args}) + "\n").encode("utf-8"))
            line = s.makefile("rb").readline()
            if not line:
                raise DriverError("daemon closed connection without responding")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError as e:
                raise DriverError(f"bad response from daemon: {e}")
        finally:
            s.close()

        if not isinstance(resp, dict):
            raise DriverError(f"unexpected response shape: {resp!r}")
        if resp.get("ok"):
            return resp.get("result")

        # Reconstruct the exception type from the daemon-side class name.
        msg = resp.get("error") or "unknown daemon error"
        etype = resp.get("type") or "DriverError"
        if etype == "NoSessionError":
            raise NoSessionError(msg)
        if etype == "SessionExistsError":
            raise SessionExistsError(msg)
        if etype == "NavigationTimeoutError":
            raise NavigationTimeoutError(msg)
        raise DriverError(msg)
