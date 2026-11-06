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
from virtual_dev.tools._professor_mcp import (
    PROFESSOR_SERVER_KEY,
    PROFESSOR_WIKI_GET_PAGE_TOOL,
    PROFESSOR_WIKI_SEARCH_TOOL,
    append_professor_catalog,
    merge_professor_mcp,
    professor_catalog_extra,
    professor_mcp_server,
    professor_tool_names,
)
from virtual_dev.tools._wrap import wrap_text

__all__ = [
    "PROFESSOR_SERVER_KEY",
    "PROFESSOR_WIKI_GET_PAGE_TOOL",
    "PROFESSOR_WIKI_SEARCH_TOOL",
    "ToolContext",
    "append_professor_catalog",
    "build_tool_servers",
    "discover_tools",
    "merge_professor_mcp",
    "professor_catalog_extra",
    "professor_mcp_server",
    "professor_tool_names",
    "render_tools_catalog",
    "wrap_text",
]
