"""MmThreadListener: clarification fragment-append behaviour (Phase 3.8).

Verifies that incoming MM events under a recorded clarification
question:
- Append a fragment via the orchestrator (don't classify here).
- React ✅ for idempotency.
- Don't immediately post a "thanks" ack (those move to
  ``apply_classification`` after the coalescer fires).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    PromptsLoader,
)
from virtual_dev.application.services.clarification import ClarificationOrchestrator
from virtual_dev.application.services.clarification.repo import QuestionRepository
from virtual_dev.application.services.clarification.stakeholder_resolver import (
    StakeholderResolver,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification import (
    Question,
    QuestionState,
    Stakeholder,
    StakeholderKind,
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


class _RecordingChat(ChatPort):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str]] = []
        self._counter = 0

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent.append((user_id, text, None))
        self._counter += 1
        return ChatMessage(
            id=f"post-{self._counter}", channel_id=f"dm-{user_id}",
            author_id="bot", text=text, timestamp=datetime.now(timezone.utc),
            trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent.append((channel_id, text, thread_root_id))
        self._counter += 1
        return ChatMessage(
            id=f"post-{self._counter}", channel_id=channel_id, author_id="bot",
            text=text, timestamp=datetime.now(timezone.utc),
            trusted=True, thread_root_id=thread_root_id,
        )

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{username}", username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.append((post_id, emoji_name))

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return None  # never seen — exercise the no-reaction path

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        events = self._events  # type: ignore[attr-defined]

        async def _gen() -> AsyncIterator[ChatMessage]:
            for event in events:
                yield event

        return _gen()


def _config() -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(), mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg(
            clarifier_question="Q: {question}",
        )),
    )


async def _seed_pending_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> Question:
    repo = QuestionRepository(session_factory)
    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=Stakeholder(
            kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint="alice",
            resolved_mm_user_id="uid-alice",
        ),
        coalesce_window_seconds=600,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    await repo.update_state(
        q.id, QuestionState.ASKING,
        asked_post_id="dm-post-root", mm_user_id="uid-alice",
        mm_channel_id="dm-uid-alice",
    )
    return q


@pytest.mark.asyncio
async def test_thread_reply_appends_fragment_no_ack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    q = await _seed_pending_question(session_factory)
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    orch = ClarificationOrchestrator(
        repo=QuestionRepository(session_factory),
        communicator=communicator,
        classifier=_DummyClassifier(),                 # type: ignore[arg-type]
        counter_answerer=_DummyCounter(),              # type: ignore[arg-type]
        stakeholder_resolver=_DummyResolver(           # type: ignore[arg-type]
            communicator=communicator, code_agent=None,  # type: ignore[arg-type]
            config=_config(), prompts_loader=PromptsLoader("config/prompts"),
        ),
        config=_config(),
        session_factory=session_factory,
        message_bus=None,
    )

    listener = MmThreadListener(
        chat=chat,
        communicator=communicator,
        responder=None,                                # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        clarification_orchestrator=orch,
    )

    event = ChatMessage(
        id="m-1", channel_id="dm-uid-alice", author_id="uid-alice",
        text="первый кусок", timestamp=datetime.now(timezone.utc),
        thread_root_id="dm-post-root", trusted=False,
    )
    await listener._dispatch(event)

    # Fragment was persisted.
    fragments = await QuestionRepository(session_factory).list_unflushed_fragments(q.id)
    assert [f.mm_post_id for f in fragments] == ["m-1"]

    # NO ✅ at this point — reactions are now placed by the
    # orchestrator on the LAST fragment when the coalescer flushes
    # (i.e. after the human stops typing). Mid-message reactions look
    # like the bot is interrupting after every line.
    assert ("m-1", _PROCESSED_REACTION) not in chat.reactions
    # Only thing the bot might have sent is the question DM dispatch
    # — but we did NOT call request_clarifications here, so chat.sent
    # should be empty.
    assert chat.sent == []


@pytest.mark.asyncio
async def test_plain_dm_routes_to_oldest_active_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No thread_root_id → match by channel + author; FIFO oldest."""
    q = await _seed_pending_question(session_factory)
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    orch = ClarificationOrchestrator(
        repo=QuestionRepository(session_factory),
        communicator=communicator,
        classifier=_DummyClassifier(),                 # type: ignore[arg-type]
        counter_answerer=_DummyCounter(),              # type: ignore[arg-type]
        stakeholder_resolver=_DummyResolver(           # type: ignore[arg-type]
            communicator=communicator, code_agent=None,  # type: ignore[arg-type]
            config=_config(), prompts_loader=PromptsLoader("config/prompts"),
        ),
        config=_config(),
        session_factory=session_factory,
        message_bus=None,
    )
    listener = MmThreadListener(
        chat=chat,
        communicator=communicator,
        responder=None,                                # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        clarification_orchestrator=orch,
    )

    event = ChatMessage(
        id="m-1", channel_id="dm-uid-alice", author_id="uid-alice",
        text="ответ", timestamp=datetime.now(timezone.utc),
        thread_root_id=None, trusted=False,
    )
    await listener._dispatch(event)
    fragments = await QuestionRepository(session_factory).list_unflushed_fragments(q.id)
    assert [f.mm_post_id for f in fragments] == ["m-1"]


# Lightweight stubs satisfying types (never called during these tests).


class _DummyClassifier:
    async def classify(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("classifier should not be called from listener tests")


class _DummyCounter:
    async def answer(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("counter answerer should not be called")


class _DummyResolver(StakeholderResolver):
    async def resolve(self, raw_hint, context=None):  # type: ignore[no-untyped-def]
        return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint)
