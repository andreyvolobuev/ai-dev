"""Fetch one knowledge-base page by URL."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import error_text, text_result

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
        return await run(researcher, args)

    return _kb_fetch


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    if researcher.kb is None:
        return error_text("Knowledge base is not configured")
    url = str(args.get("url") or "")
    if not url:
        return error_text("Empty URL")
    page = await researcher.kb.fetch_page_by_url(url)
    rendered = f"# {page.title}\n{page.url}\n\n{page.content_text}"
    wrapped = researcher.filter.wrap(rendered, source=f"kb:page:{page.id}")
    return text_result(wrapped.wrapped_text)
