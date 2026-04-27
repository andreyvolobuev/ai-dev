"""Full-text search in the knowledge base (Confluence-style)."""

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
        "kb_search",
        "Full-text search in the knowledge base (Confluence). Returns up to `limit` pages.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    )
    async def _kb_search(args: dict[str, Any]) -> dict[str, Any]:
        return await researcher._run_kb_search(args)

    return _kb_search
