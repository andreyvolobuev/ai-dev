"""Semantic search over past merged MRs of the configured repos."""

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
        "search_mr_history",
        "Search past merged MRs of this repository for ones similar to "
        "`query`. Returns up to `k` hits (default 5) with title, "
        "description, URL, author and a similarity score. Useful to see "
        "how comparable changes were done before.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo_key": {"type": "string"},
                "k": {"type": "integer"},
            },
            "required": ["query", "repo_key"],
        },
    )
    async def _search_mr_history(args: dict[str, Any]) -> dict[str, Any]:
        return await researcher._run_search_mr_history(args)

    return _search_mr_history
