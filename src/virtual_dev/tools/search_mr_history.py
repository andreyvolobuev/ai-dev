"""Semantic search over past merged MRs of a configured repo."""

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
        return await run(researcher, args)

    return _search_mr_history


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    if researcher.mr_history is None:
        return error_text(
            "MR history index is not configured. "
            "Run `virtual-dev index-mrs --repo <key>` first."
        )
    repo_key = str(args.get("repo_key") or "")
    query = str(args.get("query") or "")
    k = int(args.get("k") or 5)
    if not repo_key:
        return error_text("repo_key is required")
    if not query:
        return error_text("query is required")
    if repo_key not in researcher.repos:
        return error_text(f"Unknown repo: {repo_key!r}")

    hits = await researcher.mr_history.search(repo_key, query, k=k)
    if not hits:
        return text_result(
            f"(no MR-history matches for query {query!r}; "
            f"index may be empty — run `virtual-dev index-mrs --repo {repo_key}`)"
        )
    parts: list[str] = []
    for hit in hits:
        parts.append(
            f"## !{hit.iid} — {hit.title}\n"
            f"score={hit.score:.3f}  author={hit.author_username}  "
            f"merged_at={hit.merged_at.isoformat() if hit.merged_at else '—'}\n"
            f"url: {hit.web_url}\n\n"
            f"{(hit.description or '')[:1200]}"
        )
    wrapped = researcher.filter.wrap(
        "\n\n---\n\n".join(parts), source=f"mr_history:{repo_key}",
    )
    return text_result(wrapped.wrapped_text)
