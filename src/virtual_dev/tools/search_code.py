"""Regex-grep across a configured repository (``git grep -nI``).

Output is wrapped via the injection filter so the LLM treats it as
data — not instructions. Capped at ``max_results`` lines so a chatty
codebase doesn't blow up the prompt.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import error_text, git_grep, text_result

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
        return await run(researcher, args)

    return _search_code


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    """Implementation entry point — used by the @tool wrapper above
    and by tests directly (no SDK ceremony required)."""
    pattern = str(args.get("pattern") or "")
    repo_key = str(args.get("repo_key") or "")
    max_results = int(args.get("max_results") or researcher.DEFAULT_MAX_GREP_RESULTS)

    handle = researcher.repos.get(repo_key)
    if handle is None or not handle.local_path.exists():
        return error_text(f"Unknown or missing repo: {repo_key!r}")
    if not pattern:
        return error_text("Empty search pattern")

    text = await asyncio.to_thread(
        git_grep, handle.local_path, pattern, max_results,
    )
    wrapped = researcher.filter.wrap(text, source=f"code:{repo_key}:grep")
    return text_result(wrapped.wrapped_text)
