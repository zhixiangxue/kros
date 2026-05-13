"""lightpanda_mcp driver — one of possibly many BrowseDriver backends.

What the CLI sees (via ``get_driver("lightpanda_mcp")``) is the proxy
class below, which satisfies the ``BrowseDriver`` contract and hides
the fact that a session daemon sits between the CLI and the real
``lightpanda mcp`` subprocess.

Everything that exists only because lightpanda's MCP is stdio-only —
the daemon, the unix socket, the forking dance, the engine — is
internal to this package and cannot leak into the CLI or other
drivers.
"""

from .proxy import LightpandaMCPDriver

__all__ = ["LightpandaMCPDriver"]
