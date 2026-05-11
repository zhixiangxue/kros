"""Driver registry for ``kros browse``.

Each driver is a concrete implementation of :class:`kros.commands.browse.
contract.BrowseDriver`. We register them lazily (string path) so an
unused driver's heavy deps never load. Selection is env-driven
(``KROS_BROWSE_DRIVER``, default ``lightpanda_mcp``).
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Optional

from kros.commands.browse.contract import ENV_DRIVER

if TYPE_CHECKING:
    from kros.commands.browse.contract import BrowseDriver


# driver name → "module:class" lazy import path
_REGISTRY: dict[str, str] = {
    "lightpanda_mcp": "kros.commands.browse.drivers.lightpanda_mcp:LightpandaMCPDriver",
}


def get_driver(name: Optional[str] = None) -> "BrowseDriver":
    """Resolve and instantiate the driver to use."""
    chosen = name or os.environ.get(ENV_DRIVER, "lightpanda_mcp")
    if chosen not in _REGISTRY:
        raise ValueError(
            f"Unknown browse driver: {chosen!r}. "
            f"Known: {sorted(_REGISTRY)}. Override via {ENV_DRIVER}."
        )
    module_path, class_name = _REGISTRY[chosen].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    return cls()
