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
from loguru import logger

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
        if mod_name.startswith("_"):
            # Defense in depth: _iter_module_names already filters, but
            # tests monkeypatch it and the docstring guarantees this.
            continue
        full_name = f"{package_name}.{mod_name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception:
            # One broken tool module shouldn't tank the whole agent. Log
            # the failure prominently — operator will see it on startup —
            # and keep loading the rest.
            logger.exception(
                "Tool loader: failed to import {!r}; skipping", full_name,
            )
            continue
        builder: Callable[[ToolContext], Any] | None = getattr(mod, "build", None)
        if builder is None:
            continue
        try:
            tool_obj = builder(ctx)
        except Exception:
            logger.exception(
                "Tool loader: build() raised in {!r}; skipping", full_name,
            )
            continue
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
    only_groups: set[str] | None = None,
) -> tuple[dict[str, McpSdkServerConfig], list[str], dict[str, list[Any]]]:
    """Build one MCP server per group and the SDK allow-list of names.

    Returns ``(servers, allowed, groups)`` where:
    * ``servers`` maps ``"<prefix><group>"`` → ``McpSdkServerConfig``
      ready to pass into ``ClaudeAgentOptions(mcp_servers=...)``.
    * ``allowed`` is the list of fully-qualified tool names
      (``mcp__<server>__<tool>``) ready for ``allowed_tools=``.
    * ``groups`` is the raw discovery output keyed by group name —
      passed back so callers can introspect tool ``.name`` /
      ``.description`` to render a catalogue (e.g. into a system
      prompt) without re-running ``discover_tools`` (which would
      execute every tool's ``build()`` a second time).

    ``only_groups`` filters the result to the named groups so each
    agent gets only its own surface — analyst doesn't see
    ``submit_mr`` / ``submit_response``, dev doesn't see ``dm_user``,
    etc. ``None`` (default) means "every group". Discovery still runs
    over the whole package so a tool that returns ``None`` from
    ``build()`` (optional dep missing) still has its side effects;
    only the routing into MCP servers + allow-list is filtered.
    """
    groups = discover_tools(ctx, package_name=package_name)
    servers: dict[str, McpSdkServerConfig] = {}
    allowed: list[str] = []
    selected = {
        g: tools for g, tools in groups.items()
        if only_groups is None or g in only_groups
    }
    for group, tool_list in selected.items():
        server_name = f"{mcp_name_prefix}{group}"
        servers[server_name] = create_sdk_mcp_server(
            name=server_name, version="0.1.0", tools=tool_list,
        )
        for t in tool_list:
            allowed.append(f"mcp__{server_name}__{t.name}")
    return servers, allowed, selected


_GROUP_HEADERS: dict[str, str] = {
    "shared": "Shared tools (read tickets / code / external docs — any agent can use)",
    "analyst": "Analyst tools (talk to humans, terminate the run)",
    "dev": "Dev tools (terminate the implementation run)",
    "responder": "Responder tools (terminate the review-reply decision)",
}


def render_tools_catalog(
    groups: dict[str, list[Any]],
    *,
    extra_builtins: str | None = None,
) -> str:
    """Render the auto-discovered tool list as markdown for prompt
    inclusion. Adding a new ``tools/<file>.py`` automatically surfaces
    it in the analyst's prompt without manual edits.

    ``extra_builtins`` is appended verbatim — used to mention things
    like ``Read`` / ``Glob`` / ``Grep`` that the SDK provides directly
    (they don't go through this loader).
    """
    parts: list[str] = []
    # Sort groups: known headers first in their declared order, anything
    # else alphabetically. Within a group: tools alphabetically.
    known = [g for g in _GROUP_HEADERS if g in groups]
    rest = sorted(g for g in groups if g not in _GROUP_HEADERS)
    for group in known + rest:
        header = _GROUP_HEADERS.get(group, group.replace("_", " ").title())
        parts.append(f"**{header}**:")
        parts.append("")
        for tool in sorted(groups[group], key=lambda t: t.name):
            description = " ".join((tool.description or "").split())
            parts.append(f"* `{tool.name}` — {description}")
        parts.append("")
    if extra_builtins:
        parts.append(extra_builtins.strip())
    return "\n".join(parts).rstrip()


__all__ = [
    "_iter_module_names",
    "build_tool_servers",
    "discover_tools",
    "render_tools_catalog",
]
