"""Fetch a specific knowledge-base page by its URL."""

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
        "kb_fetch_page_by_url",
        "Fetch a specific knowledge-base page by its URL.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    async def _kb_fetch(args: dict[str, Any]) -> dict[str, Any]:
        return await researcher._run_kb_fetch(args)

    return _kb_fetch
