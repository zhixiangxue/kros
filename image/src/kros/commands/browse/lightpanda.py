"""Lightpanda driver for `kros browse`.

Shells out to the ``lightpanda`` binary on PATH (overridable via
``KROS_BROWSE_LIGHTPANDA_BIN``). Two modes are used:

- ``lightpanda fetch --dump markdown <url>`` for the stateless fast path
- ``lightpanda serve --host ... --port ...`` to back interactive CDP sessions
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from typing import Optional

from .driver import CDPEndpoint


_SPAWN_TIMEOUT_SECONDS = 10.0


def _tcp_is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """Cheap liveness probe: can we open a TCP connection?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


class LightpandaDriver:
    """Lightpanda implementation of :class:`DriverProtocol`."""

    name = "lightpanda"

    def __init__(self, binary: Optional[str] = None) -> None:
        self._binary = binary or os.environ.get(
            "KROS_BROWSE_LIGHTPANDA_BIN", "lightpanda"
        )

    def binary_path(self) -> str:
        resolved = shutil.which(self._binary)
        if resolved is None:
            raise FileNotFoundError(
                f"lightpanda binary not found on PATH: {self._binary!r}. "
                f"Download from https://lightpanda.io, put it on PATH, "
                f"or set KROS_BROWSE_LIGHTPANDA_BIN=<abs-path>."
            )
        return resolved

    def version(self) -> str:
        """Return ``lightpanda version`` output (best-effort; never raises)."""
        out = subprocess.run(
            [self.binary_path(), "version"],
            check=False,
            capture_output=True,
            text=True,
        )
        return (out.stdout or out.stderr).strip()

    def is_alive(self, endpoint: CDPEndpoint) -> bool:
        return _tcp_is_listening(endpoint.host, endpoint.port)

    def spawn_cdp_server(self, endpoint: CDPEndpoint) -> "subprocess.Popen":
        """Start ``lightpanda serve`` in the background and wait until it accepts TCP."""
        proc = subprocess.Popen(
            [
                self.binary_path(),
                "serve",
                "--host", endpoint.host,
                "--port", str(endpoint.port),
                "--log-format", "pretty",
                "--log-level", "info",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + _SPAWN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.is_alive(endpoint):
                return proc
            if proc.poll() is not None:
                raise RuntimeError(
                    f"lightpanda serve exited with code {proc.returncode} "
                    f"before accepting connections on {endpoint.host}:{endpoint.port}"
                )
            time.sleep(0.1)
        proc.terminate()
        raise TimeoutError(
            f"lightpanda serve did not start listening on "
            f"{endpoint.host}:{endpoint.port} within {_SPAWN_TIMEOUT_SECONDS:g}s"
        )

    def fetch_markdown(self, url: str, timeout: Optional[float] = None) -> str:
        """Run ``lightpanda fetch --dump markdown`` and return stdout."""
        result = subprocess.run(
            [self.binary_path(), "fetch", "--dump", "markdown", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"lightpanda fetch failed (exit {result.returncode}): "
                f"{result.stderr.strip() or '<no stderr>'}"
            )
        return result.stdout
