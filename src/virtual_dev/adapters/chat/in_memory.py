"""In-process ChatPort for the test-analyst UI.

Runs entirely inside the FastAPI process — no Mattermost, no
WebSocket to a real server. The web page acts as the "human" side: it
posts user messages via REST, and receives bot messages + reactions
via the AgentTrace broadcast.

This lets us iterate on the Analyst + clarification subsystem without
touching real Mattermost / Jira / GitLab. The ChatPort interface is
the same as MattermostChat, so the orchestrator code-path is identical.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

from loguru import logger

from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort


_BOT_USER_ID = "bot"
_DEFAULT_USER_ID = "test-user"
_DEFAULT_USER_NAME = "you"


class InMemoryChat(ChatPort):
    """Loopback ChatPort whose user-side traffic comes from the UI.

    * ``send_direct`` / ``send_to_channel`` push posts into a local
      ``_posts`` registry AND emit a ``chat_post`` event so the UI
      can render the bot's outgoing message.
    * ``post_user_message`` is the inverse — the UI calls it when the
      operator types a reply. The message lands on ``_inbox``, which
      ``subscribe()`` drains. The orchestrator processes it as if
      it came from Mattermost.
    """

    def __init__(
        self,
        *,
        trace: AgentTrace | None = None,
        user_id: str = _DEFAULT_USER_ID,
        user_name: str = _DEFAULT_USER_NAME,
    ) -> None:
        self._trace = trace
        self._user_id = user_id
        self._user_name = user_name
        self._inbox: asyncio.Queue[ChatMessage] = asyncio.Queue()
        # post_id → ChatMessage (bot-authored). Used by get_post for
        # idempotency-reaction lookups (mirroring MattermostChat).
        self._posts: dict[str, ChatMessage] = {}
        # post_id → list[bot_emoji]
        self._reactions: dict[str, list[str]] = {}
        # username → user_id for handles the operator has actually
        # spoken as (or is, by default). Distinguishes "real" people
        # from fictional names the planner might guess — without this,
        # ``lookup_mm_user`` would say everyone exists and Vasya
        # Kurochkin (who isn't on the team) gets DM'd.
        self._known_users: dict[str, str] = {user_name: user_id}
        self._counter = 0

    # --- ChatPort implementation -----------------------------------

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        # Not used in the test-analyst flow; return everything we have
        # under that root in case the ThreadResponder is exercised.
        return [
            m for m in self._posts.values()
            if m.thread_root_id == thread_root_id or m.id == thread_root_id
        ]

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        msg = self._make_bot_post(
            channel_id=f"dm-{user_id}", text=text, thread_root_id=None,
        )
        await self._publish_bot_post(msg, target_user_id=user_id)
        return msg

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        msg = self._make_bot_post(
            channel_id=channel_id, text=text, thread_root_id=thread_root_id,
        )
        await self._publish_bot_post(msg, target_user_id=None)
        return msg

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        local = email.split("@", 1)[0] or "user"
        if local not in self._known_users:
            return None
        return ChatUser(
            id=self._known_users[local], username=local, email=email,
        )

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        if username not in self._known_users:
            return None
        return ChatUser(id=self._known_users[username], username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self._reactions.setdefault(post_id, []).append(emoji_name)
        await emit_if(self._trace, AgentTraceEvent(
            type="chat_reaction",
            agent_key="chat",
            payload={"post_id": post_id, "emoji": emoji_name},
        ))

    async def get_post(self, post_id: str) -> ChatMessage | None:
        msg = self._posts.get(post_id)
        if msg is None:
            return None
        # Return a copy with current reactions populated.
        return ChatMessage(
            id=msg.id,
            channel_id=msg.channel_id,
            author_id=msg.author_id,
            text=msg.text,
            timestamp=msg.timestamp,
            thread_root_id=msg.thread_root_id,
            trusted=msg.trusted,
            reactions=list(msg.reactions),
            bot_reactions=list(self._reactions.get(post_id, [])),
        )

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        """Yields user-side messages as the UI posts them."""
        async def _iter() -> AsyncIterator[ChatMessage]:
            while True:
                msg = await self._inbox.get()
                yield msg

        return _iter()

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> list[ChatMessage]:
        # Catch-up worker still asks; in-memory has nothing to replay.
        return []

    # --- UI-side surface ------------------------------------------

    async def post_user_message(
        self,
        text: str,
        *,
        author_username: str | None = None,
        channel_id: str | None = None,
        thread_root_id: str | None = None,
    ) -> ChatMessage:
        """Inject a message FROM the operator into the chat.

        ``author_username`` lets the UI play multiple roles in one
        session: when the bot DMs ``v.kura`` (after a redirect), the
        operator switches "speaking as" to ``v.kura`` and the reply
        is attributed to ``uid-v.kura`` and lands in
        ``dm-uid-v.kura`` — exactly where the bot is waiting.
        """
        self._counter += 1
        if author_username:
            author_id = f"uid-{author_username}"
            default_channel = f"dm-{author_id}"
            # Register: from now on lookup_mm_user finds this handle.
            self._known_users.setdefault(author_username, author_id)
        else:
            author_id = self._user_id
            default_channel = f"dm-{self._user_id}"
        msg = ChatMessage(
            id=f"user-{self._counter}-{uuid.uuid4().hex[:8]}",
            channel_id=channel_id or default_channel,
            author_id=author_id,
            text=text,
            timestamp=datetime.now(timezone.utc),
            thread_root_id=thread_root_id,
            trusted=False,
        )
        self._posts[msg.id] = msg
        await self._inbox.put(msg)
        await emit_if(self._trace, AgentTraceEvent(
            type="chat_post",
            agent_key="chat",
            payload={
                "post_id": msg.id,
                "author": "user",
                "author_id": author_id,
                "channel_id": msg.channel_id,
                "thread_root_id": msg.thread_root_id,
                "text": msg.text,
            },
        ))
        return msg

    # --- internals -------------------------------------------------

    def _make_bot_post(
        self,
        *,
        channel_id: str,
        text: str,
        thread_root_id: str | None,
    ) -> ChatMessage:
        self._counter += 1
        return ChatMessage(
            id=f"bot-{self._counter}-{uuid.uuid4().hex[:8]}",
            channel_id=channel_id,
            author_id=_BOT_USER_ID,
            text=text,
            timestamp=datetime.now(timezone.utc),
            thread_root_id=thread_root_id,
            trusted=True,
        )

    async def _publish_bot_post(
        self, msg: ChatMessage, *, target_user_id: str | None,
    ) -> None:
        self._posts[msg.id] = msg
        await emit_if(self._trace, AgentTraceEvent(
            type="chat_post",
            agent_key="chat",
            payload={
                "post_id": msg.id,
                "author": "bot",
                "channel_id": msg.channel_id,
                "thread_root_id": msg.thread_root_id,
                "target_user_id": target_user_id,
                "text": msg.text,
            },
        ))


__all__ = ["InMemoryChat"]
