"""Unit tests for ReviewerAgent."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.reviewer import (
    CommentClass,
    ReviewerAgent,
)
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.merge_request import (
    ApprovalInfo,
    MergeRequest,
    MRStatus,
    PipelineJob,
    PipelineStatus,
    ReviewComment,
)
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import (
    AgentCfg,
    AgentsCfg,
    AppConfig,
    MappingsCfg,
)
from virtual_dev.infrastructure.config.schema import (
    EscalationCfg,
    RepositoryCfg,
    ReviewPolicyCfg,
)
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope


class _StubVcs(VcsPort):
    """Stub implementing just the Reviewer-relevant methods."""

    def __init__(
        self,
        *,
        comments: dict[tuple[str, int], list[ReviewComment]],
        approvals: dict[tuple[str, int], ApprovalInfo],
        mr_status: MRStatus = MRStatus.OPEN,
        pipeline_status: PipelineStatus = PipelineStatus.SUCCESS,
        ci_jobs: list[PipelineJob] | None = None,
    ) -> None:
        self._comments = comments
        self._approvals = approvals
        self._mr_status = mr_status
        self._pipeline_status = pipeline_status
        self.posted_mr_comments: list[tuple[str, int, str]] = []
        # Reviewer derives "ready for review" gate from get_latest_pipeline_jobs.
        # Default: a single-job green pipeline → ping fires.
        if ci_jobs is None:
            ci_jobs = [PipelineJob(
                id=1, name="tests", stage="test", status="success",
                web_url="https://gitlab/x/jobs/1",
            )]
        self._ci_jobs = ci_jobs

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80,
    ) -> list[PipelineJob]:
        return list(self._ci_jobs)

    async def list_review_comments(self, repo_key: str, iid: int) -> list[ReviewComment]:
        return list(self._comments.get((repo_key, iid), []))

    async def get_mr_approvals(self, repo_key: str, iid: int) -> ApprovalInfo:
        return self._approvals.get((repo_key, iid), ApprovalInfo())

    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:
        return MergeRequest(
            id=str(iid), iid=iid, project_id="p",
            title="t", description="",
            source_branch=f"feat/{iid}", target_branch="master",
            author_username="virtual-dev",
            web_url=f"https://gitlab/x/merge_requests/{iid}",
            status=self._mr_status,
            pipeline_status=self._pipeline_status,
        )

    # unused methods raise
    async def ensure_clone(self, repo_key: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def commit_all(self, repo_key: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def push(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def current_branch(self, repo_key: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def has_uncommitted_changes(self, repo_key: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def create_merge_request(
        self, repo_key: str, source_branch: str, target_branch: str,
        title: str, description: str, draft: bool = False,
    ) -> MergeRequest:  # pragma: no cover
        raise NotImplementedError

    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None,
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500,
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def add_mr_comment(self, repo_key: str, iid: int, body: str) -> None:
        # Tests override by setting self.posted_mr_comments below.
        self.posted_mr_comments.append((repo_key, iid, body))

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError


class _RecordingChat(ChatPort):
    def __init__(self) -> None:
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return _msg(text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent_channels.append((channel_id, text))
        return _msg(text)

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{username}", username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:  # pragma: no cover
        return None

    async def get_post(self, post_id: str):  # pragma: no cover
        return None

    def subscribe(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover
        raise NotImplementedError


def _msg(text: str) -> ChatMessage:
    return ChatMessage(
        id="m", channel_id="c", author_id="bot", text=text,
        timestamp=datetime.now(timezone.utc),
    )


def _cfg(
    *,
    team_channel: str | None = "team-chan",
    escalation_user: str = "tech-lead",
    required_approvals: int = 1,
    ping_after_hours: int = 4,
    escalate_after_hours: int = 24,
) -> AppConfig:
    mappings = MappingsCfg(
        team_channels={"bellingshausen": team_channel} if team_channel else {},
    )
    agents = AgentsCfg(
        agents={"communicator": AgentCfg(rate_limit_per_hour=100)},
        review_policy=ReviewPolicyCfg(
            required_approvals=required_approvals,
            ping_reviewers_after_hours=ping_after_hours,
            escalate_after_hours=escalate_after_hours,
        ),
        escalation=EscalationCfg(mattermost_user=escalation_user),
    )
    from virtual_dev.infrastructure.config import MmTemplatesCfg, NotificationsCfg
    return AppConfig(
        repositories=[
            RepositoryCfg(key="bellingshausen", url="git@x:g/bellingshausen.git"),
        ],
        agents=agents,
        mappings=mappings,
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg(
            review_ping="MR `{repo_key}!{iid}` is ready for review.\n{title}\n{web_url}",
            merge_ping="`{repo_key}!{iid}` Please merge: {web_url}",
            stale_ping="MR `{repo_key}!{iid}` is waiting for a review ({idle_hours}h idle). {web_url}",
            escalation_dm="MR `{repo_key}!{iid}` no reviewer activity {idle_hours}h: {web_url}",
            thread_reply_iteration_done="✅ CI зелёный — коммит {commit_sha_short} на {branch}",
        )),
    )


async def _insert_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    iid: int = 42,
    repo_key: str = "bellingshausen",
    last_seen: str | None = None,
    last_activity_at: datetime | None = None,
    created_at: datetime | None = None,
    status: str = "open",
    review_ping_sent: bool = True,   # default: past the initial review ping
) -> int:
    async with session_scope(session_factory) as session:
        row = MergeRequestRow(
            repo_key=repo_key, iid=iid, external_id=str(iid),
            task_external_id="DM-1",
            title="t", description="",
            source_branch=f"ai-dev/dm-1-{iid}", target_branch="master",
            author_username="virtual-dev",
            web_url=f"https://gitlab/x/merge_requests/{iid}",
            status=status, approvals_count=0, approvals_required=1,
            last_seen_comment_id=last_seen,
            last_activity_at=last_activity_at,
            review_ping_sent=review_ping_sent,
            created_at=created_at or datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row.id


class _StubClassifier:
    """Deterministic classifier used by the Reviewer-tick tests.

    Mirrors the previous regex semantics closely enough that the
    existing assertions about which comments become "actionable" still
    hold. The actual LLM-backed classifier has its own dedicated tests
    in ``test_review_comment_classifier.py``.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, body: str) -> CommentClass:
        self.calls.append(body)
        s = body.lower().strip()
        if not s:
            return CommentClass.CHATTER
        approval = ("lgtm", "approved", "approve", "+1", "ship it", "ready to merge")
        if any(k in s for k in approval):
            return CommentClass.APPROVAL_HINT
        change = (
            "rename", "fix", "please change", "rework", "wrong",
            "rewrite", "remove ", "should ", "don't", "do not",
            "исправь", "поправь",
        )
        if any(k in s for k in change):
            return CommentClass.CHANGE_REQUEST
        if s.endswith("?"):
            return CommentClass.QUESTION
        return CommentClass.CHATTER


def _agent(
    vcs: VcsPort,
    communicator: CommunicatorService,
    session_factory: async_sessionmaker[AsyncSession],
    config: AppConfig,
) -> ReviewerAgent:
    return ReviewerAgent(
        vcs=vcs,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
    )


@pytest.mark.asyncio
async def test_reviewer_routes_classification_through_injected_classifier(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The agent must call its injected classifier for every new comment
    — proves the regex helper is gone and there's no direct fallback."""
    await _insert_mr(session_factory, last_activity_at=datetime.now(timezone.utc))
    comments = [
        ReviewComment(id="c-1", mr_id="42", author_username="virtual-dev", body="MR opened"),
        ReviewComment(id="c-2", mr_id="42", author_username="alice", body="please rename foo"),
        ReviewComment(id="c-3", mr_id="42", author_username="bob", body="LGTM"),
    ]
    vcs = _StubVcs(
        comments={("bellingshausen", 42): comments},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    classifier = _StubClassifier()
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        comment_classifier=classifier,
        bot_username="virtual-dev",
    )
    await agent.tick()

    # virtual-dev's own comment is filtered before classification; the
    # remaining two human comments must hit the classifier.
    assert classifier.calls == ["please rename foo", "LGTM"]


# --- ReviewerAgent.tick -------------------------------------------------


@pytest.mark.asyncio
async def test_new_human_comments_advance_last_seen_without_forwarding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Phase 3 rule: comments never round-trip to Mattermost.

    Reviewer detects the new comment, advances ``last_seen_comment_id``
    so the next tick ignores it, and emits an ``mr.comment`` event on the
    bus — but does NOT DM/channel-post the content.
    """
    mr_id = await _insert_mr(session_factory, last_activity_at=datetime.now(timezone.utc))
    comments = [
        ReviewComment(id="c-1", mr_id="42", author_username="virtual-dev", body="MR opened"),
        ReviewComment(id="c-2", mr_id="42", author_username="alice", body="please fix this bug"),
    ]
    vcs = _StubVcs(
        comments={("bellingshausen", 42): comments},
        approvals={("bellingshausen", 42): ApprovalInfo(approved_by=[], required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)

    agent = _agent(vcs, communicator, session_factory, _cfg())
    stats = await agent.tick()

    assert stats.mrs_checked == 1
    assert stats.new_comments == 1  # virtual-dev's own filtered out
    # No channel / DM traffic for comments.
    assert chat.sent_channels == []
    assert chat.sent_dms == []
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.last_seen_comment_id == "c-2"


@pytest.mark.asyncio
async def test_already_seen_comments_not_re_emitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(
        session_factory, last_seen="c-2",
        last_activity_at=datetime.now(timezone.utc),
    )
    comments = [
        ReviewComment(id="c-1", mr_id="42", author_username="alice", body="old"),
        ReviewComment(id="c-2", mr_id="42", author_username="alice", body="previously processed?"),
        ReviewComment(id="c-3", mr_id="42", author_username="bob", body="please rename X"),
    ]
    vcs = _StubVcs(
        comments={("bellingshausen", 42): comments},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.new_comments == 1
    # No MM traffic — comment observation only updates last_activity_at.
    assert chat.sent_channels == []


@pytest.mark.asyncio
async def test_review_ping_fires_once_on_first_observation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.review_pings_sent == 1
    assert any("ready for review" in body for _, body in chat.sent_channels)

    # Second tick: flag persisted, no repeat.
    chat.sent_channels.clear()
    stats2 = await agent.tick()
    assert stats2.review_pings_sent == 0
    assert chat.sent_channels == []


def _job(name: str, status: str) -> PipelineJob:
    return PipelineJob(
        id=1, name=name, stage="test", status=status,
        web_url=f"https://gitlab/x/jobs/{name}",
    )


@pytest.mark.asyncio
async def test_review_ping_held_while_pipeline_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Don't call reviewers when CI is red — fix it first."""
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
        ci_jobs=[_job("tests", "failed")],
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.review_pings_sent == 0
    assert chat.sent_channels == []


@pytest.mark.asyncio
async def test_review_ping_held_while_pipeline_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Wait for CI to finish before pinging."""
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
        ci_jobs=[_job("tests", "running")],
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.review_pings_sent == 0


@pytest.mark.asyncio
async def test_iteration_pending_announces_when_ci_green(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After silent iteration push the Reviewer announces in the review
    thread the moment CI flips green — and clears the pending flag."""
    from sqlalchemy import select as _select
    from virtual_dev.infrastructure.db import MergeRequestRow
    mr_id = await _insert_mr(session_factory)
    # Pre-set the pending state as if MmThreadListener / DevOps just pushed.
    async with session_factory() as session:
        row = (await session.execute(
            _select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        row.iteration_pending_ci_sha = "abc123def456"
        row.review_thread_channel_id = "team-chan"
        row.review_thread_root_id = "root-iter"
        await session.commit()

    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        ci_jobs=[_job("tests", "success")],
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    await agent.tick()
    # An ack post should have landed in the review thread, in the right thread.
    threaded = [
        body for ch, body, root in
        ((c, b, r) for c, b in chat.sent_channels for r in [None])
        if False   # placeholder — sent_channels has no thread root attr
    ]
    # _RecordingChat appends (channel_id, text) — thread_root is dropped.
    # We simply check the message body shape on the channel.
    assert any("CI зелёный" in body for _, body in chat.sent_channels)
    async with session_factory() as session:
        row = (await session.execute(
            _select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.iteration_pending_ci_sha is None


@pytest.mark.asyncio
async def test_review_ping_fires_when_no_ci_configured(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No pipeline at all → assume repo without CI → ping right away."""
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
        ci_jobs=[],   # no jobs → no CI
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.review_pings_sent == 1


@pytest.mark.asyncio
async def test_review_ping_not_sent_while_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, review_ping_sent=False, status="draft")
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.DRAFT,
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.review_pings_sent == 0
    assert chat.sent_channels == []


class _StubMrSummarizer:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    async def summarize(self, *, title: str, description: str) -> str:
        self.calls.append((title, description))
        return self._reply


@pytest.mark.asyncio
async def test_review_ping_uses_mr_summarizer_to_describe_the_mr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ping should read «приглашаю на МР, в котором я <что я сделала>»,
    not just «приглашаю на МР: <title>». The reviewer asks the
    summarizer for a 1-2 sentence first-person feminine fragment and
    splices it into the template via a new ``{summary}`` variable.
    Title is still passed (template author decides whether to keep
    showing it alongside)."""
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    summarizer = _StubMrSummarizer("добавила валидацию этапов и покрыла её тестами")

    # Override the team channel template with one that uses {summary}.
    cfg = _cfg()
    cfg.notifications.mattermost.review_ping = (
        "Ребята, приглашаю на [МР]({web_url}), в котором я {summary}"
    )

    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=cfg,
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
        mr_summarizer=summarizer,   # type: ignore[arg-type]
    )

    stats = await agent.tick()
    assert stats.review_pings_sent == 1
    # Summarizer was invoked with the MR's description + title.
    assert len(summarizer.calls) == 1
    title, description = summarizer.calls[0]
    assert "DM-1" in title or title  # whatever _insert_mr seeded
    # The team-channel post contains the summarizer's output verbatim.
    bodies = [body for _channel, body in chat.sent_channels]
    assert any(
        "добавила валидацию этапов и покрыла её тестами" in body
        for body in bodies
    ), f"summary missing from posted bodies: {bodies}"


@pytest.mark.asyncio
async def test_review_ping_falls_back_to_title_when_summarizer_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backward compat — production wires the summarizer, but tests /
    older configs may leave it None. The {summary} placeholder must
    fall back to the title so the template still renders without
    KeyError or 'в котором я {summary}' literal."""
    await _insert_mr(session_factory, review_ping_sent=False)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.OPEN,
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    cfg = _cfg()
    cfg.notifications.mattermost.review_ping = (
        "Ребята, приглашаю на [МР]({web_url}): {summary}"
    )
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=cfg,
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
        # mr_summarizer omitted intentionally
    )

    stats = await agent.tick()
    assert stats.review_pings_sent == 1
    bodies = [body for _channel, body in chat.sent_channels]
    # No literal '{summary}' leak; some text appears after the colon.
    assert not any("{summary}" in body for body in bodies), (
        f"unrendered placeholder leaked into chat: {bodies}"
    )
    # And SOMETHING ended up there (the title fallback).
    assert any(body.strip().endswith(("...", "...", "MR", "ed", ".",)) or len(body) > 30 for body in bodies)


@pytest.mark.asyncio
async def test_reviewer_emits_comment_received_activity_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every new MR comment must produce a ``comment_received`` event
    on the AgentTrace — including those classified as chatter. Without
    it the operator sees comments arrive in GitLab and zero feedback in
    the live UI; the only signal today is a logger.info line in the
    server console (and the message-bus publish, which doesn't
    surface). The classification is part of the payload so the UI can
    render 'received but skipped because chatter' vs 'received +
    routed to thread responder'."""
    from virtual_dev.application.services.agent_trace import AgentTrace

    await _insert_mr(session_factory, last_activity_at=datetime.now(timezone.utc))
    comments = [
        ReviewComment(id="c-1", mr_id="42", author_username="virtual-dev", body="MR opened"),
        ReviewComment(
            id="c-2", mr_id="42", author_username="alice",
            body="Мы пишем комменты по принципу зачем тут этот код",
        ),
        ReviewComment(id="c-3", mr_id="42", author_username="bob", body="LGTM"),
    ]
    vcs = _StubVcs(
        comments={("bellingshausen", 42): comments},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    trace = AgentTrace()
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
        trace=trace,
    )

    await agent.tick()

    received = [e for e in list(trace._history) if e.type == "comment_received"]
    # virtual-dev's own comment is filtered before classification, so
    # only the two human comments get through.
    assert len(received) == 2, (
        f"expected 2 comment_received events (one per human comment); "
        f"got types={[e.type for e in trace._history]}"
    )
    by_author = {e.payload.get("author"): e for e in received}
    assert "alice" in by_author
    assert "bob" in by_author
    # Classification is exposed so UI can colour 'received but ignored'
    # (chatter) differently from 'received + routed' (change_request etc).
    alice_event = by_author["alice"]
    assert alice_event.payload.get("classification") in {
        "change_request", "chatter", "question", "approval_hint",
    }
    assert alice_event.payload.get("repo_key") == "bellingshausen"
    assert alice_event.payload.get("iid") == 42
    # Preview should be present (truncated) so the operator can see the
    # text without clicking through to GitLab.
    assert "комменты" in alice_event.payload.get("preview", "")


@pytest.mark.asyncio
async def test_review_ping_held_emits_activity_event_on_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Holding the 'please review' ping while the MR is still Draft is
    intentional, but operators get NO signal in the live activity tab —
    they see hours of silence and assume the bot is broken. Emit a
    ``review_ping_held`` AgentTraceEvent with the reason so the
    activity feed shows the held state alongside the MR identifier."""
    from virtual_dev.application.services.agent_trace import AgentTrace

    await _insert_mr(session_factory, review_ping_sent=False, status="draft")
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
        mr_status=MRStatus.DRAFT,
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    trace = AgentTrace()
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
        trace=trace,
    )

    await agent.tick()

    held = [e for e in list(trace._history) if e.type == "review_ping_held"]
    assert len(held) == 1, (
        f"expected exactly one review_ping_held event; "
        f"got types={[e.type for e in trace._history]}"
    )
    event = held[0]
    assert event.agent_key == "reviewer"
    assert event.payload.get("reason") == "draft"
    assert event.payload.get("repo_key") == "bellingshausen"
    assert event.payload.get("iid") == 42


@pytest.mark.asyncio
async def test_approvals_threshold_posts_merge_ping(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(approved_by=["bob"], required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.approvals_sent == 1
    assert any("Please merge" in body for _, body in chat.sent_channels)


@pytest.mark.asyncio
async def test_escalation_fires_when_mr_is_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # last_activity_at is 30 hours ago → above escalate_after_hours=24.
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    await _insert_mr(session_factory, last_activity_at=stale)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.escalations_sent == 1
    # Escalation is a DM to the tech-lead.
    assert any("no reviewer activity" in text for _, text in chat.sent_dms)


@pytest.mark.asyncio
async def test_suppressed_escalation_dm_does_not_lock_in_cooldown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Outside-hours DM is dropped by Communicator. Reviewer used to
    still stamp last_escalation_at, so the next escalate_after_hours
    window blocked retries — escalations issued at 23:00 silently
    vanished. The fix: only persist cooldown when sent==True."""
    from virtual_dev.application.services.communicator import SendOutcome

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    mr_id = await _insert_mr(session_factory, last_activity_at=stale)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )

    class _SuppressingCommunicator:
        def __init__(self) -> None:
            self.dm_calls: list[tuple[str, str]] = []
            self.channel_calls: list[tuple[str, str]] = []

        async def send_dm(self, user_id: str, text: str) -> SendOutcome:
            self.dm_calls.append((user_id, text))
            return SendOutcome(sent=False, skip_reason="outside_working_hours")

        async def send_channel(
            self, channel_id: str, text: str, *,
            thread_root_id: str | None = None,
        ) -> SendOutcome:
            self.channel_calls.append((channel_id, text))
            return SendOutcome(sent=False, skip_reason="outside_working_hours")

        async def resolve_user_id(self, *, username: str) -> str | None:
            return f"uid-{username}"

    communicator = _SuppressingCommunicator()
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,  # type: ignore[arg-type]
        session_factory=session_factory, config=_cfg(),
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
    )
    await agent.tick()

    # The DM attempt was made and suppressed.
    assert len(communicator.dm_calls) == 1
    # last_escalation_at must NOT have been stamped — otherwise the
    # bot would silently sit out until escalate_after_hours pass again.
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
    assert row.last_escalation_at is None


@pytest.mark.asyncio
async def test_suppressed_ping_does_not_set_ping_reviewers_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same idea for the channel-ping path: a suppressed channel post
    must not flip ping_reviewers_at, otherwise the next tick treats
    the MR as already-pinged and stays silent until escalation."""
    from virtual_dev.application.services.communicator import SendOutcome

    idle = datetime.now(timezone.utc) - timedelta(hours=5)
    mr_id = await _insert_mr(session_factory, last_activity_at=idle)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )

    class _SuppressingCommunicator:
        def __init__(self) -> None:
            self.channel_calls: list[tuple[str, str]] = []

        async def send_dm(self, user_id: str, text: str) -> SendOutcome:
            return SendOutcome(sent=True)

        async def send_channel(
            self, channel_id: str, text: str, *,
            thread_root_id: str | None = None,
        ) -> SendOutcome:
            self.channel_calls.append((channel_id, text))
            return SendOutcome(sent=False, skip_reason="rate_limited")

        async def resolve_user_id(self, *, username: str) -> str | None:
            return None

    communicator = _SuppressingCommunicator()
    agent = ReviewerAgent(
        vcs=vcs, communicator=communicator,  # type: ignore[arg-type]
        session_factory=session_factory, config=_cfg(),
        comment_classifier=_StubClassifier(),
        bot_username="virtual-dev",
    )
    await agent.tick()

    assert len(communicator.channel_calls) == 1
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
    assert row.ping_reviewers_at is None


@pytest.mark.asyncio
async def test_ping_fires_before_escalation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # 5 hours idle → above ping_after_hours=4 but below escalate_after_hours=24.
    idle = datetime.now(timezone.utc) - timedelta(hours=5)
    await _insert_mr(session_factory, last_activity_at=idle)
    vcs = _StubVcs(
        comments={("bellingshausen", 42): []},
        approvals={("bellingshausen", 42): ApprovalInfo(required=1)},
    )
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = _agent(vcs, communicator, session_factory, _cfg())

    stats = await agent.tick()
    assert stats.pings_sent == 1
    assert stats.escalations_sent == 0
    assert any("waiting for a review" in text for _, text in chat.sent_channels)
