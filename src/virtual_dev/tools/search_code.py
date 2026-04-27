"""Search the codebase for a regex pattern."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext

TOOL_GROUP = "researcher"


def build(ctx: ToolContext):
    if ctx.researcher is None:
        return None
    researcher = ctx.researcher

    @tool(
        "search_code",
        "Search the codebase for a regex pattern. Returns matching lines "
        "grouped by file. Large results are truncated.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "repo_key": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern", "repo_key"],
        },
    )
    async def _search_code(args: dict[str, Any]) -> dict[str, Any]:
        return await researcher._run_search_code(args)

    return _search_code
