"""Hybrid chat adapter — reads from one ChatPort, writes to another.

Used by the test-analyst UI when you want the bot to:

* read **real** Mattermost (real users, real threads — so tools like
  ``read_mattermost_thread`` and ``find_chat_user_by_name`` see the
  workspace's actual directory and history),
* but DM **into** an in-memory chat the UI renders, so debugging a
  ticket flow doesn't spam real teammates.

The `subscribe` stream comes from the ``writes`` (in-memory) side —
that's where the test UI feeds operator replies. Hooking it up to
the real MM WebSocket would deliver every unrelated message in the
workspace into the agent loop, which is the opposite of what we want.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort


class HybridChat(ChatPort):
    """Routes ChatPort calls between a read-source and a write-source.

    * Reads (``read_thread``, ``get_post``, ``read_channel_since``)
      → ``reads``.
    * User-directory lookups (``find_user_*``, ``search_users_by_name``)
      → ``writes`` first, ``reads`` as fallback. This lets test-only
      users registered via ``InMemoryChat.register_user`` (e.g. the
      operator "you" who plays the lead in the test-analyst session)
      win over the real MM directory, while everyone else is still
      resolved against the live workspace.
    * Writes (``send_direct``, ``send_to_channel``, ``add_reaction``)
      → ``writes``.
    * ``subscribe`` → ``writes`` (UI-driven incoming, see module
      docstring).
    """

    def __init__(self, *, reads: ChatPort, writes: ChatPort) -> None:
        self._reads = reads
        self._writes = writes

    # --- writes ---

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        return await self._writes.send_direct(user_id, text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        return await self._writes.send_to_channel(channel_id, text, thread_root_id)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        await self._writes.add_reaction(post_id, emoji_name)

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        return self._writes.subscribe()

    # --- reads ---

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return await self._reads.read_thread(thread_root_id)

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        local = await self._writes.find_user_by_email(email)
        if local is not None:
            return local
        return await self._reads.find_user_by_email(email)

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        local = await self._writes.find_user_by_username(username)
        if local is not None:
            return local
        return await self._reads.find_user_by_username(username)

    async def search_users_by_name(
        self, query: str, *, limit: int = 25,
    ) -> Sequence[ChatUser]:
        local = list(
            await self._writes.search_users_by_name(query, limit=limit)
        )
        if len(local) >= limit:
            return local[:limit]
        seen = {u.id for u in local}
        remote = await self._reads.search_users_by_name(
            query, limit=limit - len(local),
        )
        return local + [u for u in remote if u.id not in seen]

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return await self._reads.get_post(post_id)

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> Sequence[ChatMessage]:
        return await self._reads.read_channel_since(channel_id, since)


__all__ = ["HybridChat"]
