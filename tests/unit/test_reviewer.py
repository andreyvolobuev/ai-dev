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
    classify_comment,
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
    ) -> None:
        self._comments = comments
        self._approvals = approvals
        self._mr_status = mr_status

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

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80,
    ) -> Sequence[PipelineJob]:  # pragma: no cover
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
        bot_username="virtual-dev",
    )


# --- classify_comment ---------------------------------------------------


def test_classify_approval_variants() -> None:
    for body in ("LGTM", "lgtm!", "approved", "+1", "approve", "Ship it"):
        assert classify_comment(body) == CommentClass.APPROVAL_HINT, body


def test_classify_change_request() -> None:
    assert classify_comment("please change the name of this func") == CommentClass.CHANGE_REQUEST
    assert classify_comment("wrong return type here") == CommentClass.CHANGE_REQUEST


def test_classify_question() -> None:
    assert classify_comment("why are we doing it this way?") == CommentClass.QUESTION


def test_classify_chatter() -> None:
    assert classify_comment("nice work!") == CommentClass.CHATTER
    assert classify_comment("") == CommentClass.CHATTER


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
