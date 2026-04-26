"""MCP tools the ClarificationPlanner exposes to its Claude session.

Two tools wrap :class:`CommunicatorService`:

* ``lookup_mm_user`` — exact resolve by handle / email. Use after the
  handle is known.
* ``search_mm_users_by_name`` — fuzzy directory search (Mattermost
  ``/api/v4/users/autocomplete``). Use FIRST when the analyst handed
  you a free-form name like "Вася Курочкин" — see who actually
  matches in the directory before guessing transliterations.

The factory returns ``(mcp_server, allowed_tool_names)`` for the
planner agent to slot into ``CodeAgentRequest.extras`` alongside the
researcher's existing tools.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.communicator import CommunicatorService


_LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
    },
}


_SEARCH_SCHEMA: dict[str, Any] = {
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


def build_planner_mcp_server(
    communicator: CommunicatorService,
) -> tuple[McpSdkServerConfig, list[str]]:
    """Build the MCP server hosting the planner's lookup + search tools."""

    @tool(
        "lookup_mm_user",
        "Resolve a Mattermost user by handle or email. "
        "Returns {found: bool, mm_user_id?: str, display_name?: str}. "
        "Use AFTER you already know a confirmed handle (e.g. "
        "search_mm_users_by_name returned a single hit, or a human "
        "DM'd you the @-handle).",
        _LOOKUP_SCHEMA,
    )
    async def _lookup_mm_user(args: dict[str, Any]) -> dict[str, Any]:
        handle = (args.get("handle") or "").strip().lstrip("@") or None
        email = (args.get("email") or "").strip() or None
        if not handle and not email:
            return _wrap({
                "found": False,
                "reason": "no_handle_or_email_provided",
            })
        try:
            user_id = await communicator.resolve_user_id(
                username=handle, email=email,
            )
        except Exception as exc:
            logger.exception(
                "lookup_mm_user: resolve_user_id raised for handle={!r}, email={!r}",
                handle, email,
            )
            return _wrap({"found": False, "reason": f"error: {type(exc).__name__}"})
        if user_id is None:
            return _wrap({"found": False, "reason": "not_found"})
        return _wrap({
            "found": True,
            "mm_user_id": user_id,
            "display_name": handle or email,
        })

    @tool(
        "search_mm_users_by_name",
        "Fuzzy-search the Mattermost directory by name. Use this FIRST "
        "when you have a free-form Russian/English name like 'Вася "
        "Курочкин' — it matches first_name, last_name, nickname, "
        "username. Returns up to `limit` (default 10) candidates with "
        "their handle, email, full name, and position. If no candidates "
        "match, ask whoever gave you the name for a confirmed handle "
        "rather than guessing transliterations.",
        _SEARCH_SCHEMA,
    )
    async def _search_mm_users_by_name(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return _wrap({"matches": [], "reason": "empty_query"})
        raw_limit = args.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else 10
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        try:
            users = await communicator.search_users_by_name(query, limit=limit)
        except Exception as exc:
            logger.exception(
                "search_mm_users_by_name: raised for query={!r}", query,
            )
            return _wrap({
                "matches": [],
                "reason": f"error: {type(exc).__name__}",
            })
        return _wrap({
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
        })

    server = create_sdk_mcp_server(
        name="virtual_dev_planner_tools", version="0.1.0",
        tools=[_lookup_mm_user, _search_mm_users_by_name],
    )
    return server, [
        "mcp__virtual_dev_planner_tools__lookup_mm_user",
        "mcp__virtual_dev_planner_tools__search_mm_users_by_name",
    ]


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a dict result into the SDK's tool-response shape."""
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


__all__ = ["build_planner_mcp_server"]
