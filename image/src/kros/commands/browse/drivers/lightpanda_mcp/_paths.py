"""Filesystem paths and liveness probe shared by proxy/daemon.

Keeps the proxy (CLI process) and daemon (server process) in agreement
on where the unix socket lives, without pulling in either's heavier
modules.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from kros.commands.browse.contract import (
    DAEMON_LOG_BASENAME,
    DEFAULT_RUNTIME_SUBDIR,
    ENV_RUNTIME_DIR,
    PID_BASENAME,
    SOCKET_BASENAME,
)


def runtime_dir() -> Path:
    override = os.environ.get(ENV_RUNTIME_DIR)
    if override:
        return Path(override)
    return Path.home() / DEFAULT_RUNTIME_SUBDIR


def socket_path() -> Path:
    return runtime_dir() / SOCKET_BASENAME


def pid_path() -> Path:
    return runtime_dir() / PID_BASENAME


def log_path() -> Path:
    return runtime_dir() / DAEMON_LOG_BASENAME


def is_daemon_alive() -> bool:
    """Probe: daemon is alive iff we can connect to its socket."""
    sp = socket_path()
    if not sp.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            s.connect(str(sp))
        return True
    except OSError:
        return False
