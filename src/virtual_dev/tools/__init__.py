"""Auto-discovered tool registry.

Drop a ``<name>.py`` file into this package; the loader picks it up at
runtime and adds its tool to the agent's MCP toolkit. See
``README.md`` for the contract.
"""

from virtual_dev.tools._context import ToolContext
from virtual_dev.tools._loader import (
    build_tool_servers,
    discover_tools,
    render_tools_catalog,
)
from virtual_dev.tools._wrap import wrap_text

__all__ = [
    "ToolContext",
    "build_tool_servers",
    "discover_tools",
    "render_tools_catalog",
    "wrap_text",
]
