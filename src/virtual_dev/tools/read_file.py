"""Read a small window of a file in a configured repository."""

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
        "read_file",
        "Read a small window of a file in a repository. Returns up to "
        "max_bytes characters, defaults to 12000.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "repo_key": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["path", "repo_key"],
        },
    )
    async def _read_file(args: dict[str, Any]) -> dict[str, Any]:
        return await researcher._run_read_file(args)

    return _read_file
