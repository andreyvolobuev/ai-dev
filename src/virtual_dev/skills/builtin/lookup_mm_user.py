"""Resolve a Mattermost user by handle or email."""

from __future__ import annotations

from typing import Any

from loguru import logger

from virtual_dev.application.services.skills import SkillContext, skill


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
    },
}


@skill(
    name="lookup_mm_user",
    description=(
        "Resolve a Mattermost user by handle or email. "
        "Returns {found: bool, mm_user_id?: str, display_name?: str}. "
        "Use AFTER you already know a confirmed handle (e.g. "
        "search_mm_users_by_name returned a single hit, or a human "
        "DM'd you the @-handle)."
    ),
    schema=_SCHEMA,
    tags={"planner"},
)
async def lookup_mm_user(
    args: dict[str, Any], ctx: SkillContext,
) -> dict[str, Any]:
    handle = (args.get("handle") or "").strip().lstrip("@") or None
    email = (args.get("email") or "").strip() or None
    if not handle and not email:
        return {"found": False, "reason": "no_handle_or_email_provided"}
    try:
        user_id = await ctx.communicator.resolve_user_id(
            username=handle, email=email,
        )
    except Exception as exc:
        logger.exception(
            "lookup_mm_user: resolve_user_id raised for handle={!r}, email={!r}",
            handle, email,
        )
        return {"found": False, "reason": f"error: {type(exc).__name__}"}
    if user_id is None:
        return {"found": False, "reason": "not_found"}
    return {
        "found": True,
        "mm_user_id": user_id,
        "display_name": handle or email,
    }
