"""HybridChat: reads go to one ChatPort, writes to another.

Used by the test-analyst UI to debug a real Jira ticket end-to-end
without the bot DM-ing real teammates: read tools see the real MM
workspace, write tools (`dm_user`) land in the in-memory chat the UI
renders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import pytest

from virtual_dev.adapters.chat.hybrid import HybridChat
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort


class _SpyChat(ChatPort):
    """Records which methods got called so tests can assert routing."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.calls.append(f"send_direct:{user_id}:{text}")
        return ChatMessage(
            id=f"{self.label}-1", channel_id=f"dm-{user_id}",
            author_id="bot", text=text, timestamp=datetime.now(timezone.utc),
            trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.calls.append(f"send_to_channel:{channel_id}:{text}")
        return ChatMessage(
            id=f"{self.label}-2", channel_id=channel_id,
            author_id="bot", text=text, timestamp=datetime.now(timezone.utc),
            thread_root_id=thread_root_id, trusted=True,
        )

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        self.calls.append(f"read_thread:{thread_root_id}")
        return []

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        self.calls.append(f"find_user_by_email:{email}")
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        self.calls.append(f"find_user_by_username:{username}")
        return None

    async def search_users_by_name(
        self, query: str, *, limit: int = 25,
    ) -> Sequence[ChatUser]:
        self.calls.append(f"search_users_by_name:{query}:{limit}")
        return []

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.calls.append(f"add_reaction:{post_id}:{emoji_name}")

    async def get_post(self, post_id: str) -> ChatMessage | None:
        self.calls.append(f"get_post:{post_id}")
        return None

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> Sequence[ChatMessage]:
        self.calls.append(f"read_channel_since:{channel_id}")
        return []

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        self.calls.append("subscribe")

        async def _gen() -> AsyncIterator[ChatMessage]:
            if False:
                yield  # type: ignore[unreachable]

        return _gen()


@pytest.mark.asyncio
async def test_writes_go_to_writes_chat() -> None:
    reads = _SpyChat("R")
    writes = _SpyChat("W")
    hc = HybridChat(reads=reads, writes=writes)

    await hc.send_direct("uid-alice", "hi")
    await hc.send_to_channel("ch-1", "hello", thread_root_id="root-1")
    await hc.add_reaction("post-1", "white_check_mark")

    assert reads.calls == []
    assert writes.calls == [
        "send_direct:uid-alice:hi",
        "send_to_channel:ch-1:hello",
        "add_reaction:post-1:white_check_mark",
    ]


@pytest.mark.asyncio
async def test_reads_go_to_reads_chat() -> None:
    reads = _SpyChat("R")
    writes = _SpyChat("W")
    hc = HybridChat(reads=reads, writes=writes)

    await hc.read_thread("post-42")
    await hc.find_user_by_username("v.kura")
    await hc.find_user_by_email("v@example.com")
    await hc.search_users_by_name("Курочкин", limit=5)
    await hc.get_post("post-42")
    await hc.read_channel_since("ch-1", datetime.now(timezone.utc))

    assert writes.calls == []
    assert reads.calls == [
        "read_thread:post-42",
        "find_user_by_username:v.kura",
        "find_user_by_email:v@example.com",
        "search_users_by_name:Курочкин:5",
        "get_post:post-42",
        "read_channel_since:ch-1",
    ]


@pytest.mark.asyncio
async def test_subscribe_goes_to_writes_chat() -> None:
    """Operator-driven incoming arrives via the in-memory `writes` chat;
    we explicitly do NOT subscribe to the real MM stream because that
    would deliver every unrelated message in the workspace."""
    reads = _SpyChat("R")
    writes = _SpyChat("W")
    hc = HybridChat(reads=reads, writes=writes)
    hc.subscribe()
    assert writes.calls == ["subscribe"]
    assert reads.calls == []
