"""Mattermost adapter.

Reads (``read_thread``, ``find_user_by_*``) — always enabled.

Writes (``send_direct``, ``send_to_channel``) — enabled from Phase 3 for
the Communicator agent to ping reviewers / escalate / answer questions.
Rate-limiting and working-hours policy live in
:class:`virtual_dev.application.services.CommunicatorService`, not here.

``subscribe`` (websocket listener) stays unimplemented: Phase 3 uses
polling of GitLab MR comments instead of MM incoming.

The underlying library ``mattermostdriver`` is synchronous; calls are wrapped
in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlparse

from loguru import logger
from mattermostdriver import Driver

from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort


def _parse_host_port_scheme(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"Mattermost URL has no host: {url!r}")
    scheme = parsed.scheme or "https"
    default_port = 443 if scheme == "https" else 80
    return parsed.hostname, parsed.port or default_port, scheme


class MattermostChat(ChatPort):
    """``ChatPort`` backed by Mattermost (self-hosted), read-only for Phase 1."""

    def __init__(self, *, url: str, token: str, bot_username: str | None = None) -> None:
        if not url or not token:
            raise ValueError("Mattermost URL and token must be provided")
        host, port, scheme = _parse_host_port_scheme(url)
        self._driver = Driver(
            {
                "url": host,
                "port": port,
                "scheme": scheme,
                "token": token,
                "timeout": 30,
            }
        )
        self._bot_username = bot_username
        self._logged_in = False

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._driver.login()
            self._logged_in = True

    def _bot_user_id(self) -> str:
        """Return the authenticated user's id (our bot token → bot account)."""
        self._ensure_login()
        me = self._driver.users.get_user("me")
        if not isinstance(me, dict) or not me.get("id"):
            raise RuntimeError("Could not resolve Mattermost bot user id from /users/me")
        return str(me["id"])

    # --- Phase-1 allowed methods ---

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        def _fetch() -> list[ChatMessage]:
            self._ensure_login()
            raw = self._driver.posts.get_thread(thread_root_id)
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Unexpected response from Mattermost thread API: {type(raw).__name__}"
                )
            posts = cast(dict[str, dict[str, Any]], raw.get("posts") or {})
            order = cast(list[str], raw.get("order") or list(posts.keys()))
            return [self._post_to_message(posts[post_id]) for post_id in order if post_id in posts]

        return await asyncio.to_thread(_fetch)

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        def _fetch() -> ChatUser | None:
            self._ensure_login()
            try:
                raw = self._driver.users.get_user_by_email(email)
            except Exception:  # 404 or 400; surface as None
                logger.debug("mattermost user lookup by email {!r} failed", email)
                return None
            return self._user_from_raw(raw)

        return await asyncio.to_thread(_fetch)

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        def _fetch() -> ChatUser | None:
            self._ensure_login()
            try:
                raw = self._driver.users.get_user_by_username(username)
            except Exception:
                logger.debug("mattermost user lookup by username {!r} failed", username)
                return None
            return self._user_from_raw(raw)

        return await asyncio.to_thread(_fetch)

    # --- Writes (Phase 3) ---

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        def _run() -> ChatMessage:
            self._ensure_login()
            bot_id = self._bot_user_id()
            channel = self._driver.channels.create_direct_message_channel(
                options=[bot_id, user_id]
            )
            if not isinstance(channel, dict) or not channel.get("id"):
                raise RuntimeError(
                    f"create_direct_message_channel returned unexpected: {channel!r}"
                )
            post = self._driver.posts.create_post(
                options={"channel_id": str(channel["id"]), "message": text}
            )
            return self._post_to_message(cast(dict[str, Any], post))

        return await asyncio.to_thread(_run)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None
    ) -> ChatMessage:
        def _run() -> ChatMessage:
            self._ensure_login()
            options: dict[str, Any] = {"channel_id": channel_id, "message": text}
            if thread_root_id:
                options["root_id"] = thread_root_id
            post = self._driver.posts.create_post(options=options)
            return self._post_to_message(cast(dict[str, Any], post))

        return await asyncio.to_thread(_run)

    def subscribe(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover
        # Phase 3 polls GitLab for MR comments; MM inbound is not needed yet.
        raise NotImplementedError(
            "Mattermost websocket subscription is not wired up — Phase 3 uses "
            "polling of GitLab MR comments."
        )

    # --- helpers ---

    def _post_to_message(self, raw: dict[str, Any]) -> ChatMessage:
        ts_ms = int(raw.get("create_at") or 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
        root = raw.get("root_id") or None
        text = str(raw.get("message") or "")
        # Trust is False unless the post is authored by our own bot user.
        trusted = bool(
            self._bot_username
            and raw.get("user_id")
            and str(raw.get("props", {}).get("username", "")) == self._bot_username
        )
        return ChatMessage(
            id=str(raw.get("id") or ""),
            channel_id=str(raw.get("channel_id") or ""),
            author_id=str(raw.get("user_id") or ""),
            text=text,
            timestamp=ts,
            thread_root_id=root,
            trusted=trusted,
        )

    def _user_from_raw(self, raw: Any) -> ChatUser | None:
        if not isinstance(raw, dict):
            return None
        return ChatUser(
            id=str(raw.get("id") or ""),
            username=str(raw.get("username") or ""),
            email=str(raw.get("email") or "") or None,
            display_name=str(raw.get("nickname") or raw.get("first_name") or "") or None,
            is_bot=bool(raw.get("is_bot")),
        )
