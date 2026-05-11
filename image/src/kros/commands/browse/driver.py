"""Driver abstraction for `kros browse`.

``DriverProtocol`` is the minimal surface every headless browser backing
this subcommand must expose. Implementations live in sibling modules
(:mod:`.lightpanda` today; future ``.obscura``, ``.chromium`` next to it)
and register themselves in ``_REGISTRY``.

"driver" is the same word doka uses for its runtime plugins (bubblewrap /
docker / cube / ...). Keeping one term across kros subsystems keeps the
mental model small.

Runtime selection is driven by environment variables (all optional):

- ``KROS_BROWSE_DRIVER``          — driver name (default: ``lightpanda``)
- ``KROS_BROWSE_LIGHTPANDA_BIN``  — path to the lightpanda binary
- ``KROS_BROWSE_CDP_HOST``        — CDP endpoint host (default: ``127.0.0.1``)
- ``KROS_BROWSE_CDP_PORT``        — CDP endpoint port (default: ``9222``)
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222


class CDPEndpoint(BaseModel):
    """Where a driver's Chrome DevTools Protocol server is (or will be) listening."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(default=DEFAULT_CDP_HOST)
    port: int = Field(default=DEFAULT_CDP_PORT, ge=1, le=65535)

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


class DriverProtocol(Protocol):
    """Shape every browser driver must satisfy.

    Lifecycle: ``is_alive()`` → ``spawn_cdp_server()`` → external use → ``.terminate()`` on the Popen.

    ``fetch_markdown()`` is an optional fast path for drivers that expose a
    native "dump markdown" mode, bypassing CDP for stateless one-shots.
    Lightpanda does; future drivers may raise :class:`NotImplementedError`.
    """

    name: str

    def binary_path(self) -> str: ...
    def version(self) -> str: ...
    def is_alive(self, endpoint: CDPEndpoint) -> bool: ...
    def spawn_cdp_server(self, endpoint: CDPEndpoint) -> "subprocess.Popen": ...
    def fetch_markdown(self, url: str, timeout: Optional[float]) -> str: ...


# Populated by driver modules at import time. We defer their imports to
# :func:`get_driver` so an unused driver's heavy deps never load.
_REGISTRY: dict[str, str] = {
    # driver name → "module:class" lazy import path
    "lightpanda": "kros.commands.browse.lightpanda:LightpandaDriver",
}


def get_driver(name: Optional[str] = None) -> DriverProtocol:
    """Resolve the driver to use.

    Precedence: explicit ``name`` > ``KROS_BROWSE_DRIVER`` env var > default (``lightpanda``).
    """
    chosen = name or os.environ.get("KROS_BROWSE_DRIVER", "lightpanda")
    if chosen not in _REGISTRY:
        raise ValueError(
            f"Unknown browse driver: {chosen!r}. "
            f"Known: {sorted(_REGISTRY)}. "
            f"Override via KROS_BROWSE_DRIVER."
        )
    import importlib

    module_path, class_name = _REGISTRY[chosen].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    return cls()


def get_endpoint() -> CDPEndpoint:
    """Build the CDP endpoint from env (with sensible defaults)."""
    return CDPEndpoint(
        host=os.environ.get("KROS_BROWSE_CDP_HOST", DEFAULT_CDP_HOST),
        port=int(os.environ.get("KROS_BROWSE_CDP_PORT", DEFAULT_CDP_PORT)),
    )
