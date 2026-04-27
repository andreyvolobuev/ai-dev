"""MM catch-up: REST replay of missed posts + listener resilience.

Phase 5.0 surface — listener now routes fragments to the analyst
inbox via TaskRow.awaiting_post_id.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
)
from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    RepositoryCfg,
    Settings,
)
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.runtime.workers.analyst_inbox import AnalystInbox
from virtual_dev.runtime.workers.mm_thread_listener import (
    _PROCESSED_REACTION,
    MmThreadListener,
)


# ============================================================
#                          Fakes
# ============================================================


class _CatchupChat(ChatPort):
    def __init__(
        self,
        *,
        catchup_posts: dict[str, list[ChatMessage]] | None = None,
        subscribe_events: list[ChatMessage] | None = None,
        subscribe_raises: int = 0,
    ) -> None:
        self._catchup_posts = catchup_posts or {}
        self._subscribe_events = subscribe_events or []
        self._subscribe_raises_remaining = subscribe_raises
        self.reactions: dict[str, list[str]] = {}
        self.read_channel_calls: list[tuple[str, datetime]] = []
        self.subscribe_calls = 0

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        return _bot_post(user_id=user_id, text=text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        return _bot_post(channel_id=channel_id, text=text, thread_root_id=thread_root_id)

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{username}", username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.setdefault(post_id, []).append(emoji_name)

    async def get_post(self, post_id: str) -> ChatMessage | None:
        bot_reactions = list(self.reactions.get(post_id, []))
        return ChatMessage(
            id=post_id, channel_id="x", author_id="x", text="",
            timestamp=datetime.now(timezone.utc),
            bot_reactions=bot_reactions,
        )

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> list[ChatMessage]:
        self.read_channel_calls.append((channel_id, since))
        return [
            m for m in self._catchup_posts.get(channel_id, [])
            if m.timestamp > since
        ]

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        self.subscribe_calls += 1
        if self._subscribe_raises_remaining > 0:
            self._subscribe_raises_remaining -= 1
            calls = self.subscribe_calls

            async def _crash() -> AsyncIterator[ChatMessage]:
                if False:
                    yield  # pragma: no cover
                raise RuntimeError(f"WS crash #{calls}")

            return _crash()

        events = self._subscribe_events

        async def _gen() -> AsyncIterator[ChatMessage]:
            for event in events:
                yield event

        return _gen()


def _bot_post(
    *,
    user_id: str = "uid-bot", channel_id: str = "ch", text: str = "",
    thread_root_id: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        id=f"bot-post-{datetime.now(timezone.utc).timestamp()}",
        channel_id=channel_id, author_id=user_id, text=text,
        timestamp=datetime.now(timezone.utc), trusted=True,
        thread_root_id=thread_root_id,
    )


def _config() -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg()),
    )


async def _seed_task_awaiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> TaskRow:
    """Insert a TaskRow with awaiting_* set so the listener routes
    fragments to it."""
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id="DM-1",
            title="t", description="",
            url="", priority="medium", external_status="To Do",
            internal_status=TaskStatus.PLANNING.value,
            awaiting_post_id="bot-post-q",
            awaiting_user_id="uid-alice",
            awaiting_username="alice",
            awaiting_channel_id="dm-uid-alice",
            coalesce_window_seconds=600,
        )
        session.add(row)
        await session.flush()
        return row


def _make_inbox(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
) -> AnalystInbox:
    repo = AnalystSessionRepository(session_factory)
    communicator = CommunicatorService(
        chat, InjectionFilter(), respect_working_hours=False,
    )
    return AnalystInbox(
        analyst=_StubAnalyst(),                       # type: ignore[arg-type]
        session_repo=repo,
        communicator=communicator,
        task_tracker=None,
        config=_config(),
        message_bus=None,
        post_to_tracker=False,
        session_factory=session_factory,
    )


def _listener(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
    inbox: AnalystInbox | None,
) -> MmThreadListener:
    return MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=None,                               # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        analyst_inbox=inbox,
    )


class _StubAnalyst:
    async def run(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("analyst should not run during catch-up tests")

    async def load_task(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("load_task should not run during catch-up tests")

    async def has_fresh_plan(self, *args: Any, **kwargs: Any) -> bool:  # pragma: no cover
        return False

    async def save_plan(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


# ============================================================
#                          Tests
# ============================================================


@pytest.mark.asyncio
async def test_catchup_replays_missed_clarification_fragment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_row = await _seed_task_awaiting(session_factory)
    missed = ChatMessage(
        id="missed-1", channel_id="dm-uid-alice", author_id="uid-alice",
        text="вот ответ который бот пропустил",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=False,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [missed]})
    listener = _listener(session_factory, chat, _make_inbox(session_factory, chat))

    total = await listener.catch_up()
    assert total == 1
    fragments = await AnalystSessionRepository(session_factory).list_unflushed_fragments(task_row.id)
    assert [f.mm_post_id for f in fragments] == ["missed-1"]


@pytest.mark.asyncio
async def test_catchup_idempotent_on_replayed_fragment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_row = await _seed_task_awaiting(session_factory)
    missed = ChatMessage(
        id="missed-2", channel_id="dm-uid-alice", author_id="uid-alice",
        text="ответ",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=False,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [missed]})
    listener = _listener(session_factory, chat, _make_inbox(session_factory, chat))

    await listener.catch_up()
    await listener.catch_up()
    fragments = await AnalystSessionRepository(session_factory).list_unflushed_fragments(task_row.id)
    assert [f.mm_post_id for f in fragments] == ["missed-2"]


@pytest.mark.asyncio
async def test_catchup_skips_bot_authored_posts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_row = await _seed_task_awaiting(session_factory)
    own = ChatMessage(
        id="own-1", channel_id="dm-uid-alice", author_id="uid-bot",
        text="мой пост",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=True,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [own]})
    listener = _listener(session_factory, chat, _make_inbox(session_factory, chat))

    total = await listener.catch_up()
    assert total == 0
    fragments = await AnalystSessionRepository(session_factory).list_unflushed_fragments(task_row.id)
    assert fragments == []


@pytest.mark.asyncio
async def test_catchup_uses_oldest_relevant_cursor_per_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    older_t = datetime.now(timezone.utc) - timedelta(hours=2)
    newer_t = datetime.now(timezone.utc) - timedelta(hours=1)

    async with session_scope(session_factory) as session:
        row_old = TaskRow(
            tracker="jira", external_id="DM-1",
            title="x", description="", url="",
            priority="medium", external_status="x",
            internal_status=TaskStatus.PLANNING.value,
            awaiting_post_id="post-old",
            awaiting_user_id="uid-alice",
            awaiting_username="alice",
            awaiting_channel_id="dm-uid-alice",
            coalesce_window_seconds=600,
            discovered_at=older_t,
        )
        row_new = TaskRow(
            tracker="jira", external_id="DM-2",
            title="y", description="", url="",
            priority="medium", external_status="x",
            internal_status=TaskStatus.PLANNING.value,
            awaiting_post_id="post-new",
            awaiting_user_id="uid-alice",
            awaiting_username="alice",
            awaiting_channel_id="dm-uid-alice",
            coalesce_window_seconds=600,
            discovered_at=newer_t,
        )
        session.add(row_old)
        session.add(row_new)
        await session.flush()

    chat = _CatchupChat(catchup_posts={})
    listener = _listener(session_factory, chat, _make_inbox(session_factory, chat))
    await listener.catch_up()

    assert len(chat.read_channel_calls) == 1
    channel, since = chat.read_channel_calls[0]
    assert channel == "dm-uid-alice"
    assert abs((since - older_t).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_run_forever_restarts_after_subscribe_crash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _CatchupChat(subscribe_raises=1, subscribe_events=[])
    listener = MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=None,                               # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        analyst_inbox=None,
        subscription_initial_backoff=0.05,
        subscription_max_backoff=0.1,
    )

    run_task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.3)
    await listener.stop()
    await run_task

    assert chat.subscribe_calls >= 2
    assert listener.stats.subscription_restarts >= 1
    assert listener.stats.errors >= 1
