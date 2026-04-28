"""Full-text knowledge-base search (Confluence-style)."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import error_text, text_result

TOOL_GROUP = "shared"


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
        return await run(researcher, args)

    return _kb_search


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    if researcher.kb is None:
        return error_text("Knowledge base is not configured")
    query = str(args.get("query") or "")
    limit = int(args.get("limit") or 5)
    if not query:
        return error_text("Empty search query")
    pages = await researcher.kb.search(query, limit=limit)
    rendered = "\n\n".join(
        f"# {p.title}\n{p.url}\n\n{p.content_text[:2000]}" for p in pages
    )
    wrapped = researcher.filter.wrap(
        rendered or "(no results)",
        source=f"kb:search:{query[:40]}",
    )
    return text_result(wrapped.wrapped_text)
