"""Read a small window of a file from a configured repo, with a
path-escape guard. Output is wrapped via the injection filter."""

from __future__ import annotations

import asyncio
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
        return await run(researcher, args)

    return _read_file


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    repo_key = str(args.get("repo_key") or "")
    max_bytes = int(args.get("max_bytes") or researcher.DEFAULT_MAX_FILE_BYTES)

    handle = researcher.repos.get(repo_key)
    if handle is None:
        return error_text(f"Unknown repo: {repo_key!r}")

    full = (handle.local_path / path).resolve()
    try:
        full.relative_to(handle.local_path.resolve())
    except ValueError:
        return error_text(f"Path escape blocked: {path!r}")
    if not full.is_file():
        return error_text(f"File not found: {path!r}")

    raw = await asyncio.to_thread(full.read_text, "utf-8", "replace")
    if len(raw) > max_bytes:
        raw = raw[:max_bytes] + f"\n... (truncated, {len(raw)} bytes total)"
    wrapped = researcher.filter.wrap(raw, source=f"code:{repo_key}:{path}")
    return text_result(wrapped.wrapped_text)
