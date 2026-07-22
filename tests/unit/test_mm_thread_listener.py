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

async def _settle(condition, *, timeout: float = 3.0) -> None:
    """Poll until ``condition()`` is truthy — a fixed 0.1s sleep flakes
    when the full suite loads the machine and dispatch takes longer."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)




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
            pipeline_autofix_restart_ack="ack: retrying",
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
    # Listener posts ONLY the acknowledgement here. The "done with sha"
    # ack is now Reviewer's job, fired when CI for the new commit
    # actually turns green — listener stays silent on the push itself.
    thread_replies = [body for _, body, root in chat.sent if root == "root-2"]
    assert any("Принято" in r for r in thread_replies)
    assert not any("deadbeef0000" in r for r in thread_replies)
    # MR row marked as awaiting CI confirmation.
    from sqlalchemy import select
    from virtual_dev.infrastructure.db import MergeRequestRow
    async with session_factory() as s:
        row = (await s.execute(
            select(MergeRequestRow).where(MergeRequestRow.iid == 1001)
        )).scalar_one()
        assert row.iteration_pending_ci_sha == "deadbeef0000deadbeef"
        assert row.pipeline_autofix_attempts == 0   # reset by user-driven iter
    # ✅ reaction set on the source comment.
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


@pytest.mark.asyncio
async def test_iterate_forwards_full_thread_transcript_to_dev(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The dev needs the raw thread (not just the responder's
    ``iteration_feedback`` blob) to ground itself in what reviewers
    actually wrote — otherwise it inherits the responder's
    interpretation as ground truth and there's nothing to cross-check
    against. See discussion mirroring the Analyst+Clarification merge:
    a downstream agent that only sees an upstream agent's prose
    paraphrase silently loses the original intent."""
    await _insert_mr(session_factory, root_id="root-thread-fwd")
    events = [
        _human_post(id="post-1", thread_root_id="root-thread-fwd",
                    text="please rename foo to bar", author="alice"),
        _human_post(id="post-2", thread_root_id="root-thread-fwd",
                    text="actually let's call it baz", author="bob"),
    ]
    chat = _ScriptedChat(events)
    # post-1 is prior history (already handled by the bot — pre-react ✅
    # so the listener skips it on dispatch). post-2 is the new reply
    # that should drive the iteration. Both stay in the transcript that
    # ``read_thread`` returns.
    chat._bot_reactions["post-1"] = [_PROCESSED_REACTION]
    responder = _ScriptedResponder([ResponderDecision(
        action=ResponderAction.ITERATE,
        reply_text="Принято.",
        iteration_feedback="Rename foo to baz.",
        reasoning="latest-message-wins",
    )])
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(
        outcome=DevOutcome.MR_OPENED, branch_name="ai-dev/dm-1",
        commit_sha="cafef00d",
    ))
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    task = asyncio.create_task(listener.run_forever())
    await _settle(lambda: dev.calls)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    assert len(dev.calls) == 1
    call = dev.calls[0]
    # Listener already fetched the transcript to feed the responder;
    # it must hand the same transcript to the dev.
    thread = call.get("thread")
    assert thread is not None, "dev.handle_iteration must receive thread="
    texts = [m.text for m in thread]
    assert "please rename foo to bar" in texts
    assert "actually let's call it baz" in texts


@pytest.mark.asyncio
async def test_concurrent_dispatch_of_same_post_is_processed_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same MM post must be handled at most once even if two delivery
    paths race on it.

    In production both the WebSocket listener and the 60s catch-up poller
    call ``_dispatch``. The ✅ idempotency reaction is only added AFTER the
    (multi-second) ``responder.decide`` + post, so without an in-flight
    guard both dispatches pass the early ✅-check, both call the LLM, and
    the bot posts two *divergent* replies to a single human message —
    observed live as an offer ("if you want, I'll fix it — just say")
    immediately followed by a phantom "good catch, I'll fix it" with no
    human confirmation in between. Exactly one decision must win.
    """
    await _insert_mr(session_factory, root_id="root-race")
    event = _human_post(
        id="post-race", thread_root_id="root-race",
        text="почему здесь PUT вместо PATCH?",
    )
    chat = _ScriptedChat([event])

    # Responder that blocks inside decide() until released — this holds
    # both racing dispatches open across the ✅-check→✅-set window, the
    # exact condition the WS/catch-up overlap creates in production.
    release = asyncio.Event()

    class _BlockingResponder:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            await release.wait()
            return ResponderDecision(
                action=ResponderAction.REPLY,
                reply_text="Ответ на вопрос про PUT/PATCH.",
                reasoning="explain",
            )

    responder = _BlockingResponder()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(outcome=DevOutcome.NO_CHANGES))
    listener = MmThreadListener(
        chat=chat, communicator=communicator,
        responder=responder,   # type: ignore[arg-type]
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
        session_factory=session_factory,
        config=_test_config(), settings=Settings(),
    )

    # Two concurrent deliveries of the SAME post (WS + catch-up overlap).
    d1 = asyncio.create_task(listener._dispatch(event))
    d2 = asyncio.create_task(listener._dispatch(event))
    # Let both tasks run up to decide() (buggy code) or be rejected by the
    # in-flight guard (fixed code) before we unblock decide().
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(d1, d2), timeout=2)

    assert responder.calls == 1, (
        f"same post sent through the LLM {responder.calls} times — "
        f"double-dispatch race not guarded"
    )
    posted = [body for _, body, root in chat.sent if root == "root-race"]
    assert len(posted) == 1, f"expected exactly one reply, got {len(posted)}: {posted}"


def test_responder_action_propose_alternative_parses() -> None:
    """The new ``propose_alternative`` enum value must round-trip from
    its serialised form. Without this the model's submit_response call
    silently degrades to IGNORE (see thread_responder._call_model
    fallback) and the push-back is invisible."""
    assert ResponderAction("propose_alternative") is ResponderAction.PROPOSE_ALTERNATIVE


@pytest.mark.asyncio
async def test_propose_alternative_posts_text_without_iterating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``propose_alternative`` is a chat-only action (the bot pushes
    back with a concrete alternative and waits for confirmation). It
    must NOT trigger a dev iteration, must post the text in the thread
    like a regular reply, and must increment a separate stat counter so
    we can observe how often the bot disagrees vs simply answers."""
    await _insert_mr(session_factory, root_id="root-alt")
    event = _human_post(
        id="post-alt", thread_root_id="root-alt",
        text="оберни это в цикл и дёргай по одному",
    )
    chat = _ScriptedChat([event])
    responder = _ScriptedResponder([ResponderDecision(
        action=ResponderAction.PROPOSE_ALTERNATIVE,
        reply_text="Это создаст N+1 — лучше batch endpoint, ок?",
        reasoning="n+1-warning",
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

    task = asyncio.create_task(listener.run_forever())
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    # Text reached the thread under the right root.
    assert any(
        body.startswith("Это создаст N+1") and root == "root-alt"
        for _, body, root in chat.sent
    )
    # Dev was NOT dispatched — alternative is a conversation, not a code change.
    assert dev.calls == []
    # Listener tracked it under a dedicated counter (separate from
    # plain ``replies_posted`` so we can measure push-back rate).
    assert listener.stats.alternatives_proposed == 1
    # ✅ marker still set so we don't reprocess the source post.
    assert ("post-alt", _PROCESSED_REACTION) in chat.reactions


@pytest.mark.asyncio
async def test_iterate_ack_failure_leaves_post_unmarked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MM-thread mirror of the GitLab invariant: a post must not get the
    ✅ "processed" marker if the bot couldn't deliver its reply. When the
    ITERATE acknowledgement fails to post, leave the post unreacted (so
    the catch-up sweep retries) and do NOT run the dev iteration we
    couldn't announce."""
    await _insert_mr(session_factory, root_id="root-ackfail")
    event = _human_post(
        id="post-ackfail", thread_root_id="root-ackfail",
        text="please rename foo to bar",
    )

    class _SendFailsChat(_ScriptedChat):
        async def send_to_channel(self, channel_id, text, thread_root_id=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("network down")

    chat = _SendFailsChat([event])
    responder = _ScriptedResponder([ResponderDecision(
        action=ResponderAction.ITERATE,
        reply_text="Принято, правлю.",
        iteration_feedback="Rename foo to bar.",
        reasoning="clear-rename",
    )])
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev(DevResult(
        outcome=DevOutcome.MR_OPENED, branch_name="ai-dev/dm-1",
        commit_sha="deadbeef",
    ))
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

    # Ack never reached the thread → no ✅, and no iteration we can't announce.
    assert ("post-ackfail", _PROCESSED_REACTION) not in chat.reactions
    assert dev.calls == []


async def _insert_escalated_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    escalation_root_id: str,
    attempts: int = 3,
) -> int:
    async with session_scope(session_factory) as session:
        row = MergeRequestRow(
            repo_key="bellingshausen", iid=1001, external_id="1001",
            task_external_id="DM-1", title="t", description="d",
            source_branch="ai-dev/dm-1", target_branch="master",
            author_username="virtual-dev",
            web_url="https://gitlab/x/merge_requests/1001",
            status="open", approvals_count=0, approvals_required=1,
            review_ping_sent=True,
            pipeline_autofix_attempts=attempts,
            pipeline_autofix_escalated=True,
            autofix_escalation_root_id=escalation_root_id,
        )
        session.add(row)
        await session.flush()
        return row.id


@pytest.mark.asyncio
async def test_restart_command_resets_autofix_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `/restart` reply in the autofix give-up DM thread resets the MR's
    autofix counter + escalated flag, acks the team-lead, and marks the
    command processed. It must NOT go through the review responder."""
    mr_id = await _insert_escalated_mr(session_factory, escalation_root_id="esc-root")
    event = _human_post(id="cmd-1", thread_root_id="esc-root", text="/restart")
    chat = _ScriptedChat([event])
    responder = _ScriptedResponder([])   # a command is not a review reply
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

    from sqlalchemy import select
    async with session_factory() as s:
        row = (await s.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_autofix_attempts == 0
        assert row.pipeline_autofix_escalated is False
    # Acked the lead in the thread, marked the command processed, and did
    # NOT invoke the review responder.
    assert any(body == "ack: retrying" and root == "esc-root" for _, body, root in chat.sent)
    assert ("cmd-1", _PROCESSED_REACTION) in chat.reactions
    assert responder.calls == 0


@pytest.mark.asyncio
async def test_restart_command_not_reprocessed_on_catchup_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The catch-up sweep re-delivers lead-DM posts every tick (its cursor
    is a fixed lookback, it never advances). A `/restart` that was already
    ✅-acked must be a no-op on replay — otherwise the bot spams one ack
    per sweep and keeps resetting the autofix counter forever."""
    mr_id = await _insert_escalated_mr(session_factory, escalation_root_id="esc-root")
    event = _human_post(id="cmd-1", thread_root_id="esc-root", text="/restart")
    # Same post delivered twice: once via WS, once via catch-up replay.
    chat = _ScriptedChat([event, event])
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
    await _settle(lambda: len(chat.sent) >= 1)
    await asyncio.sleep(0.1)
    await listener.stop()
    await asyncio.wait_for(task, timeout=2)

    from sqlalchemy import select
    async with session_factory() as s:
        row = (await s.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_autofix_attempts == 0
    acks = [body for _, body, root in chat.sent if body == "ack: retrying"]
    assert acks == ["ack: retrying"]   # exactly one, not one per delivery


@pytest.mark.asyncio
async def test_non_restart_reply_in_escalation_thread_does_not_reset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Arbitrary chatter in the give-up DM thread must not reset the
    counter — only the explicit `/restart` command does."""
    mr_id = await _insert_escalated_mr(session_factory, escalation_root_id="esc-root")
    event = _human_post(id="cmd-2", thread_root_id="esc-root", text="спасибо, посмотрю сама")
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

    from sqlalchemy import select
    async with session_factory() as s:
        row = (await s.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_autofix_attempts == 3   # unchanged
        assert row.pipeline_autofix_escalated is True
    assert chat.sent == []
    assert responder.calls == 0
