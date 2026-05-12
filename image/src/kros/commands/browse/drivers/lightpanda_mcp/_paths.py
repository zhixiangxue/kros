"""lightpanda_mcp-private filesystem paths and liveness probe.

Only this driver has a unix socket + pid file + daemon log (because
lightpanda exposes MCP over stdio, forcing us to run a long-lived
relay daemon). Other drivers may use ``tab_dir()`` for entirely
different files.

Tab registry (counter / current / list) is driver-neutral and lives
in :mod:`kros.commands.browse._tabs`.
"""

from __future__ import annotations

import socket
from pathlib import Path

from kros.commands.browse._tabs import tab_dir
from kros.commands.browse.contract import (
    DAEMON_LOG_BASENAME,
    PID_BASENAME,
    SOCKET_BASENAME,
)


def socket_path(tab_id: int) -> Path:
    return tab_dir(tab_id) / SOCKET_BASENAME


def pid_path(tab_id: int) -> Path:
    return tab_dir(tab_id) / PID_BASENAME


def log_path(tab_id: int) -> Path:
    return tab_dir(tab_id) / DAEMON_LOG_BASENAME


def is_daemon_alive(tab_id: int) -> bool:
    """Probe: daemon is alive iff we can connect to its unix socket."""
    sp = socket_path(tab_id)
    if not sp.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(str(sp))
        return True
    except OSError:
        return False
