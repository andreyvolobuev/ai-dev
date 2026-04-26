"""SYNC tool: fuzzy-search Mattermost directory by free-form name."""

from __future__ import annotations

from typing import Any

from virtual_dev.application.services.clarification.tools import (
    ToolContext,
    ToolMode,
    ToolOutcome,
    tool_,
)
from virtual_dev.domain.models.clarification_task import ToolResult


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-form name (Russian or English). Matches MM "
                "first_name, last_name, nickname, username. Use the "
                "surname when possible — short Russian first names "
                "(Вася / Дима) are too ambiguous."
            ),
        },
        "limit": {"type": ["integer", "null"]},
    },
    "required": ["query"],
}


@tool_(
    name="find_mm_user_by_name",
    description=(
        "Fuzzy-search the Mattermost directory by free-form name. "
        "Returns 0..N matching users with their handle, full name, "
        "and position. Use this BEFORE asking anyone about an unknown "
        "person (e.g. 'Вася Курочкин') — never guess transliterations."
    ),
    schema=_SCHEMA,
    mode=ToolMode.SYNC,
    tags={"clarification"},
)
async def find_mm_user_by_name(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolOutcome(
            mode=ToolMode.SYNC,
            error="empty_query",
            result=ToolResult(text="No query provided.", source_class="mattermost"),
        )
    raw_limit = args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 10
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 25))

    users = await ctx.communicator.search_users_by_name(query, limit=limit)
    if not users:
        return ToolOutcome(
            mode=ToolMode.SYNC,
            result=ToolResult(
                text=f"Mattermost directory: 0 matches for {query!r}.",
                structured={"matches": [], "query": query},
                source_class="mattermost",
                source_label=f"search:{query}",
            ),
        )

    lines = [f"Found {len(users)} match(es) for {query!r}:"]
    for u in users:
        full = " ".join(p for p in (u.first_name, u.last_name) if p)
        lines.append(
            f"- @{u.username} (id={u.id}) — {full or '?'}"
            f"{' · ' + u.position if u.position else ''}"
            f"{' · ' + u.email if u.email else ''}"
        )
    matches = [
        {
            "handle": u.username, "mm_user_id": u.id, "email": u.email,
            "first_name": u.first_name, "last_name": u.last_name,
            "display_name": u.display_name, "position": u.position,
        }
        for u in users
    ]
    return ToolOutcome(
        mode=ToolMode.SYNC,
        result=ToolResult(
            text="\n".join(lines),
            structured={"matches": matches, "query": query},
            source_class="mattermost",
            source_label=f"search:{query}",
        ),
    )
