"""Unit tests for MmThreadListener + ThreadResponder routing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import (
    DevOutcome,
    DevResult,
    ResponderAction,
    ResponderDecision,
)
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    Settings,
)
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.runtime.workers.mm_thread_listener import (
    _PROCESSED_REACTION,
    MmThreadListener,
)


class _ScriptedChat(ChatPort):
    """ChatPort stub where subscribe yields a scripted list of ChatMessages."""

    def __init__(self, events: list[ChatMessage]) -> None:
        self._events = events
        self.sent: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str]] = []
        # post_id → ChatMessage with current reactions
        self._posts: dict[str, ChatMessage] = {m.id: m for m in events}
        self._bot_reactions: dict[str, list[str]] = {}

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return [m for m in self._events if m.thread_root_id == thread_root_id or m.id == thread_root_id]

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent.append((user_id, text, None))
        return _stub_reply()

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent.append((channel_id, text, thread_root_id))
        return _stub_reply()

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return None

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.append((post_id, emoji_name))
        self._bot_reactions.setdefault(post_id, []).append(emoji_name)

    async def get_post(self, post_id: str) -> ChatMessage | None:
        m = self._posts.get(post_id)
        if m is None:
            return None
        # Return a fresh copy with latest bot_reactions so idempotency test works.
        return ChatMessage(
            id=m.id, channel_id=m.channel_id, author_id=m.author_id,
            text=m.text, timestamp=m.timestamp,
            thread_root_id=m.thread_root_id, trusted=m.trusted,
            reactions=list(m.reactions),
            bot_reactions=list(self._bot_reactions.get(post_id, [])),
        )

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        events = self._events

        async def _gen() -> AsyncIterator[ChatMessage]:
            for event in events:
                yield event

        return _gen()


def _test_config() -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg(
            thread_reply_no_dev_agent="нет dev-агента",
            thread_reply_no_task="нет тикета",
            thread_reply_iteration_crashed="dev упал",
            thread_reply_iteration_done="готово {commit_sha_short} на {branch}",
            thread_reply_iteration_no_changes="без изменений",
        )),
    )


def _stub_reply() -> ChatMessage:
    return ChatMessage(
        id="bot-reply", channel_id="c", author_id="bot", text="ok",
        timestamp=datetime.now(timezone.utc), trusted=True,
    )


def _human_post(
    *, id: str, thread_root_id: str | None, text: str, author: str = "alice",
) -> ChatMessage:
    return ChatMessage(
        id=id, channel_id="team-chan", author_id=author, text=text,
        timestamp=datetime.now(timezone.utc),
        thread_root_id=thread_root_id, trusted=False,
    )


class _ScriptedResponder:
    def __init__(self, decisions: list[ResponderDecision]) -> None:
        self._decisions = iter(decisions)
        self.calls = 0

    async def decide(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return next(self._decisions)


class _ScriptedDev:
    _repo_key = "bellingshausen"

    def __init__(self, result: DevResult) -> None:
        self.calls: list[dict] = []
        self._result = result

    async def handle_iteration(self, **kwargs) -> DevResult:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return self._result


async def _insert_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    root_id: str,
    channel_id: str = "team-chan",
    source_branch: str = "ai-dev/dm-1",
) -> None:
    async with session_scope(session_factory) as session:
        row = MergeRequestRow(
            repo_key="bellingshausen", iid=1001, external_id="1001",
            task_external_id="DM-1",
            title="Add /health", description="desc",
            source_branch=source_branch, target_branch="master",
            author_username="virtual-dev",
            web_url="https://gitlab/x/merge_requests/1001",
            status="open", approvals_count=0, approvals_required=1,
            review_ping_sent=True,
            review_thread_channel_id=channel_id,
            review_thread_root_id=root_id,
        )
        session.add(row)


@pytest.mark.asyncio
async def test_routes_reply_to_responder_and_posts_in_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, root_id="root-1")
    events = [_human_post(id="post-a", thread_root_id="root-1", text="как это работает?")]
    chat = _ScriptedChat(events)
    responder = _ScriptedResponder([ResponderDecision(
        action=ResponderAction.REPLY,
        reply_text="Вот как это работает: …",
        reasoning="explain-code",
    )])
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(outcome=DevOutcome.NO_CHANGES))
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    # Drive one-shot by running run_forever briefly.
    task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    assert responder.calls == 1
    assert listener.stats.events_routed == 1
    # Reply sent in thread.
    assert any(
        body == "Вот как это работает: …" and root == "root-1"
        for _, body, root in chat.sent
    )
    # ✅ reaction set.
    assert ("post-a", _PROCESSED_REACTION) in chat.reactions
    # Dev NOT called (it was a reply, not iterate).
    assert dev.calls == []


@pytest.mark.asyncio
async def test_iterate_triggers_dev_and_reports_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, root_id="root-2")
    events = [_human_post(id="post-b", thread_root_id="root-2", text="переименуй foo в bar")]
    chat = _ScriptedChat(events)
    responder = _ScriptedResponder([ResponderDecision(
        action=ResponderAction.ITERATE,
        reply_text="Принято, правлю.",
        iteration_feedback="Rename foo to bar.",
        reasoning="clear-rename",
    )])
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev_result = DevResult(
        outcome=DevOutcome.MR_OPENED,
        branch_name="ai-dev/dm-1",
        commit_sha="deadbeef0000deadbeef",
    )
    dev = _ScriptedDev(dev_result)
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    # Dev iteration was dispatched.
    assert len(dev.calls) == 1
    call = dev.calls[0]
    assert call["tracker"] == "jira"
    assert call["external_id"] == "DM-1"
    assert call["branch_name"] == "ai-dev/dm-1"
    assert "foo" in call["feedback"]
    # Two replies: acknowledgement + "done" with sha.
    thread_replies = [body for _, body, root in chat.sent if root == "root-2"]
    assert any("Принято" in r for r in thread_replies)
    assert any("deadbeef0000" in r for r in thread_replies)
    # ✅ reaction set.
    assert ("post-b", _PROCESSED_REACTION) in chat.reactions


@pytest.mark.asyncio
async def test_skips_already_reacted_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, root_id="root-3")
    event = _human_post(id="post-c", thread_root_id="root-3", text="?")
    chat = _ScriptedChat([event])
    # Pre-seed the bot reaction — listener must treat this as already-handled.
    chat._bot_reactions["post-c"] = [_PROCESSED_REACTION]
    responder = _ScriptedResponder([])    # should NOT be called
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(outcome=DevOutcome.NO_CHANGES))
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    assert responder.calls == 0
    assert chat.sent == []


@pytest.mark.asyncio
async def test_skips_posts_without_thread_root(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, root_id="root-4")
    # Top-level post, not a threaded reply.
    event = _human_post(id="post-d", thread_root_id=None, text="first message")
    chat = _ScriptedChat([event])
    responder = _ScriptedResponder([])
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(outcome=DevOutcome.NO_CHANGES))
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    assert responder.calls == 0
    assert listener.stats.events_routed == 0
