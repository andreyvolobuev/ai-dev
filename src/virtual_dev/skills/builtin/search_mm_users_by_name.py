"""Fuzzy-search Mattermost users by free-form name."""

from __future__ import annotations

from typing import Any

from loguru import logger

from virtual_dev.application.services.skills import SkillContext, skill


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-form text — Russian or English. Matches MM "
                "first_name, last_name, nickname, username."
            ),
        },
        "limit": {
            "type": ["integer", "null"],
            "description": "Max results, default 10, max 25.",
        },
    },
    "required": ["query"],
}


@skill(
    name="search_mm_users_by_name",
    description=(
        "Fuzzy-search the Mattermost directory by name. Use this FIRST "
        "when you have a free-form Russian/English name like 'Вася "
        "Курочкин' — it matches first_name, last_name, nickname, "
        "username. Returns up to `limit` (default 10) candidates with "
        "their handle, email, full name, and position. If no candidates "
        "match, ask whoever gave you the name for a confirmed handle "
        "rather than guessing transliterations."
    ),
    schema=_SCHEMA,
    tags={"planner"},
)
async def search_mm_users_by_name(
    args: dict[str, Any], ctx: SkillContext,
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"matches": [], "reason": "empty_query"}
    raw_limit = args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 10
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 25))
    try:
        users = await ctx.communicator.search_users_by_name(query, limit=limit)
    except Exception as exc:
        logger.exception(
            "search_mm_users_by_name: raised for query={!r}", query,
        )
        return {"matches": [], "reason": f"error: {type(exc).__name__}"}
    return {
        "query": query,
        "matches": [
            {
                "handle": u.username,
                "mm_user_id": u.id,
                "email": u.email,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "display_name": u.display_name,
                "position": u.position,
            }
            for u in users
        ],
    }
