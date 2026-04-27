"""Fuzzy-search the chat-platform user directory by name."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"


def build(ctx: ToolContext):
    if ctx.communicator is None:
        return None
    communicator = ctx.communicator

    @tool(
        "find_chat_user_by_name",
        "Fuzzy-search the chat-platform user directory by name "
        "(Russian or English). Matches first_name / last_name / "
        "nickname / username. Use the surname when possible. Returns "
        "0..N candidates. **Use BEFORE asking anyone about a person "
        "whose handle you don't know.**",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["query"],
        },
    )
    async def _find(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return wrap_text({"matches": [], "reason": "empty_query"})
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        users = await communicator.search_users_by_name(query, limit=limit)
        return wrap_text({
            "query": query,
            "matches": [
                {
                    "handle": u.username, "user_id": u.id,
                    "email": u.email,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "display_name": u.display_name,
                    "position": u.position,
                }
                for u in users
            ],
        })

    return _find
