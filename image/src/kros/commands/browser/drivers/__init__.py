"""Driver registry for ``kros browser``.

Each driver is a concrete implementation of :class:`kros.commands.browser.
contract.BrowseDriver`. We register them lazily (string path) so an
unused driver's heavy deps never load. Selection is env-driven
(``KROS_BROWSER_DRIVER``, default ``lightpanda_mcp``).
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Optional

from ..contract import ENV_DRIVER

if TYPE_CHECKING:
    from ..contract import BrowseDriver


# driver name → "module:class" lazy import path
_REGISTRY: dict[str, str] = {
    "lightpanda_mcp": "kros.commands.browser.drivers.lightpanda_mcp:LightpandaMCPDriver",
}


def get_driver(tab_id: int, name: Optional[str] = None) -> "BrowseDriver":
    """Resolve and instantiate the driver bound to ``tab_id``.

    Every tab is isolated: its own socket, pid file, daemon, and
    browser process. Passing the tab id here is how that isolation
    flows down into the driver. The CLI decides which tab via the
    ``--tab`` flag or the 'current tab' pointer.
    """
    chosen = name or os.environ.get(ENV_DRIVER, "lightpanda_mcp")
    if chosen not in _REGISTRY:
        raise ValueError(
            f"Unknown browser driver: {chosen!r}. "
            f"Known: {sorted(_REGISTRY)}. Override via {ENV_DRIVER}."
        )
    module_path, class_name = _REGISTRY[chosen].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    return cls(tab_id)
