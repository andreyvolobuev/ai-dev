"""Auto-discover ``build(ctx)`` factories in this package.

Each tool lives in its own ``<name>.py`` and exposes:

* ``build(ctx: ToolContext) -> SdkMcpTool | None`` — required.
  Return ``None`` to declare the tool unavailable in this context
  (e.g. a Confluence tool with no KB configured).
* ``TOOL_GROUP: str`` — optional, defaults to ``"analyst"``. Picks
  which MCP server name the tool ends up under (each group becomes a
  separate ``virtual_dev_<group>`` server).

Files starting with ``_`` are skipped (internals). Modules without
``build`` are silently ignored — the loader is forgiving so you can
keep notes / data files in here.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]

from virtual_dev.tools._context import ToolContext

_DEFAULT_GROUP = "analyst"
_DEFAULT_PACKAGE = "virtual_dev.tools"
_DEFAULT_PREFIX = "virtual_dev_"


def _iter_module_names(package: Any) -> list[str]:
    """Yield non-private submodule names of ``package``.

    Split out so tests can monkeypatch it with a fixed list (which
    avoids depending on the live filesystem layout of a fake package
    injected into ``sys.modules``)."""
    return [
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    ]


def discover_tools(
    ctx: ToolContext,
    *,
    package_name: str = _DEFAULT_PACKAGE,
) -> dict[str, list[Any]]:
    """Walk the tools package and collect tools, grouped by MCP server.

    Returns ``{group: [SdkMcpTool, ...]}``. The group string comes from
    each module's ``TOOL_GROUP`` attribute, defaulting to ``"analyst"``.
    """
    package = importlib.import_module(package_name)
    groups: dict[str, list[Any]] = {}
    for mod_name in _iter_module_names(package):
        full_name = f"{package_name}.{mod_name}"
        if mod_name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(full_name)
        except Exception:  # pragma: no cover  — surface import errors loudly
            raise
        builder: Callable[[ToolContext], Any] | None = getattr(mod, "build", None)
        if builder is None:
            continue
        tool_obj = builder(ctx)
        if tool_obj is None:
            continue
        group = getattr(mod, "TOOL_GROUP", _DEFAULT_GROUP)
        groups.setdefault(group, []).append(tool_obj)
    return groups


def build_tool_servers(
    ctx: ToolContext,
    *,
    package_name: str = _DEFAULT_PACKAGE,
    mcp_name_prefix: str = _DEFAULT_PREFIX,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build one MCP server per group and the SDK allow-list of names.

    Returns ``(servers, allowed)`` where:
    * ``servers`` maps ``"<prefix><group>"`` → ``McpSdkServerConfig``
      ready to pass into ``ClaudeAgentOptions(mcp_servers=...)``.
    * ``allowed`` is the list of fully-qualified tool names
      (``mcp__<server>__<tool>``) ready for ``allowed_tools=``.
    """
    groups = discover_tools(ctx, package_name=package_name)
    servers: dict[str, McpSdkServerConfig] = {}
    allowed: list[str] = []
    for group, tool_list in groups.items():
        server_name = f"{mcp_name_prefix}{group}"
        servers[server_name] = create_sdk_mcp_server(
            name=server_name, version="0.1.0", tools=tool_list,
        )
        for t in tool_list:
            allowed.append(f"mcp__{server_name}__{t.name}")
    return servers, allowed


__all__ = [
    "_iter_module_names",
    "build_tool_servers",
    "discover_tools",
]
