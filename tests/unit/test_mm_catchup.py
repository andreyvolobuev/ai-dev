"""MM catch-up: REST replay of missed posts + listener resilience.

Covers the WebSocket SPOF fix:
- ``MmThreadListener.catch_up()`` pulls missed posts via
  ``ChatPort.read_channel_since`` and replays through ``_dispatch``.
- Idempotency: clarification fragments collapse on UNIQUE(mm_post_id);
  review-thread comments collapse on bot's ✅-reaction.
- ``run_forever`` restarts ``subscribe()`` after a crash with backoff.
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
from virtual_dev.application.services.clarification import (
    GoalOrchestrator,
    GoalRepository,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification_goal import (
    ClarificationGoal,
    GoalState,
)
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
from virtual_dev.runtime.workers.mm_thread_listener import (
    _PROCESSED_REACTION,
    MmThreadListener,
)


# ============================================================
#                          Fakes
# ============================================================


class _CatchupChat(ChatPort):
    """Chat fake whose ``read_channel_since`` returns canned posts."""

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
        # Surface our own ✅-reaction so the listener treats already-
        # processed posts as a no-op on the second pass.
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
        # Filter strictly newer than since.
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
                # Yield nothing; raise on first __anext__ to simulate
                # subscribe() crash mid-iteration.
                if False:
                    yield  # pragma: no cover (forces this to be a generator)
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


async def _seed_goal(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClarificationGoal:
    """Seed an active clarification goal waiting on alice."""
    repo = GoalRepository(session_factory)
    goal = await repo.create_goal(
        plan_id=1,
        tracker="jira", task_external_id="DM-1",
        description="нужен пример body", why_it_matters="",
        initial_contact_hint="alice",
        coalesce_window_seconds=600,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    await repo.update_state(
        goal.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="bot-post-q",
        outstanding_user_id="uid-alice",
        outstanding_username="alice",
        outstanding_channel="dm-uid-alice",
        outstanding_text="дай пример body",
    )
    return goal


def _orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
) -> GoalOrchestrator:
    communicator = CommunicatorService(
        chat, InjectionFilter(), respect_working_hours=False,
    )
    return GoalOrchestrator(
        repo=GoalRepository(session_factory),
        communicator=communicator,
        planner=_StubPlanner(),                       # type: ignore[arg-type]
        config=_config(),
        session_factory=session_factory,
        message_bus=None,
    )


def _listener(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
    orchestrator: GoalOrchestrator | None,
) -> MmThreadListener:
    return MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=None,                                # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        goal_orchestrator=orchestrator,
    )


class _StubPlanner:
    async def decide(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("planner should not run during catch-up tests")


# ============================================================
#                          Tests
# ============================================================


@pytest.mark.asyncio
async def test_catchup_replays_missed_clarification_fragment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A post that arrived while the WS was down lands as a fragment."""
    q = await _seed_goal(session_factory)
    missed = ChatMessage(
        id="missed-1", channel_id="dm-uid-alice", author_id="uid-alice",
        text="вот ответ который бот пропустил",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=False,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [missed]})
    listener = _listener(session_factory, chat, _orchestrator(session_factory, chat))

    total = await listener.catch_up()
    assert total == 1
    fragments = await GoalRepository(session_factory).list_unflushed_fragments(q.id)
    assert [f.mm_post_id for f in fragments] == ["missed-1"]


@pytest.mark.asyncio
async def test_catchup_idempotent_on_replayed_fragment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running catch-up twice doesn't double-insert (UNIQUE on mm_post_id)."""
    q = await _seed_goal(session_factory)
    missed = ChatMessage(
        id="missed-2", channel_id="dm-uid-alice", author_id="uid-alice",
        text="ответ",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=False,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [missed]})
    listener = _listener(session_factory, chat, _orchestrator(session_factory, chat))

    await listener.catch_up()
    await listener.catch_up()
    fragments = await GoalRepository(session_factory).list_unflushed_fragments(q.id)
    assert [f.mm_post_id for f in fragments] == ["missed-2"]


@pytest.mark.asyncio
async def test_catchup_skips_bot_authored_posts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """trusted=True posts (bot's own) are filtered out, never appended."""
    q = await _seed_goal(session_factory)
    own = ChatMessage(
        id="own-1", channel_id="dm-uid-alice", author_id="uid-bot",
        text="мой собственный пост",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="bot-post-q", trusted=True,
    )
    chat = _CatchupChat(catchup_posts={"dm-uid-alice": [own]})
    listener = _listener(session_factory, chat, _orchestrator(session_factory, chat))

    total = await listener.catch_up()
    assert total == 0
    fragments = await GoalRepository(session_factory).list_unflushed_fragments(q.id)
    assert fragments == []


@pytest.mark.asyncio
async def test_catchup_uses_oldest_relevant_cursor_per_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Multiple active goals in the same DM channel → cursor is the oldest."""
    repo = GoalRepository(session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    older_t = datetime.now(timezone.utc) - timedelta(hours=2)
    newer_t = datetime.now(timezone.utc) - timedelta(hours=1)

    g_old = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-1",
        description="old", why_it_matters="", initial_contact_hint="alice",
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    await repo.update_state(
        g_old.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-old",
        outstanding_user_id="uid-alice",
        outstanding_username="alice",
        outstanding_channel="dm-uid-alice",
    )
    g_new = await repo.create_goal(
        plan_id=2, tracker="jira", task_external_id="DM-2",
        description="new", why_it_matters="", initial_contact_hint="alice",
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    await repo.update_state(
        g_new.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-new",
        outstanding_user_id="uid-alice",
        outstanding_username="alice",
        outstanding_channel="dm-uid-alice",
    )
    # Manually adjust asked_at so the oldest is older_t and the newer is newer_t.
    from virtual_dev.infrastructure.db import GoalRow
    from sqlalchemy import select
    from virtual_dev.infrastructure.db.base import session_scope
    async with session_scope(session_factory) as session:
        row_old = (await session.execute(
            select(GoalRow).where(GoalRow.id == g_old.id)
        )).scalar_one()
        row_new = (await session.execute(
            select(GoalRow).where(GoalRow.id == g_new.id)
        )).scalar_one()
        row_old.asked_at = older_t
        row_new.asked_at = newer_t

    chat = _CatchupChat(catchup_posts={})
    listener = _listener(session_factory, chat, _orchestrator(session_factory, chat))
    await listener.catch_up()

    # One channel, one call, with the OLDER cursor.
    assert len(chat.read_channel_calls) == 1
    channel, since = chat.read_channel_calls[0]
    assert channel == "dm-uid-alice"
    assert abs((since - older_t).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_run_forever_restarts_after_subscribe_crash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First subscribe() raises; outer loop must reconnect (with backoff)."""
    chat = _CatchupChat(subscribe_raises=1, subscribe_events=[])
    listener = MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=None,                                # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        goal_orchestrator=None,
        subscription_initial_backoff=0.05,
        subscription_max_backoff=0.1,
    )

    run_task = asyncio.create_task(listener.run_forever())
    # Give the listener time to: subscribe → crash → backoff(0.05s)
    # → re-subscribe (clean, empty) → fall through → backoff(0.1s)
    # → re-subscribe again. ~0.3s is plenty.
    await asyncio.sleep(0.3)
    await listener.stop()
    await run_task

    assert chat.subscribe_calls >= 2, (
        "listener must restart subscription after crash"
    )
    assert listener.stats.subscription_restarts >= 1
    assert listener.stats.errors >= 1
