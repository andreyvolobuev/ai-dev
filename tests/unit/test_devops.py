"""Unit tests for DevOpsAgent."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.devops import DevOpsAgent, _collapse_status
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.merge_request import (
    ApprovalInfo,
    MergeRequest,
    PipelineJob,
    ReviewComment,
)
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AgentsCfg, AppConfig, MappingsCfg
from virtual_dev.infrastructure.config.schema import (
    EscalationCfg,
    RepositoryCfg,
)
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope


class _StubVcs(VcsPort):
    def __init__(self, jobs: dict[tuple[str, int], list[PipelineJob]]) -> None:
        self._jobs = jobs

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80,
    ) -> list[PipelineJob]:
        return list(self._jobs.get((repo_key, iid), []))

    # unused
    async def ensure_clone(self, repo_key: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:  # pragma: no cover
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

    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:  # pragma: no cover
        raise NotImplementedError

    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None,
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500,
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_review_comments(
        self, repo_key: str, iid: int,
    ) -> Sequence[ReviewComment]:  # pragma: no cover
        raise NotImplementedError

    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_mr_approvals(self, repo_key: str, iid: int) -> ApprovalInfo:  # pragma: no cover
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

    async def find_user_by_email(self, email: str) -> ChatUser | None:  # pragma: no cover
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{username}", username=username)

    def subscribe(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover
        raise NotImplementedError


def _msg(text: str) -> ChatMessage:
    return ChatMessage(
        id="m", channel_id="c", author_id="bot", text=text,
        timestamp=datetime.now(timezone.utc),
    )


def _cfg(*, team_channel: str | None = "team-chan") -> AppConfig:
    mappings = MappingsCfg(
        team_channels={"bellingshausen": team_channel} if team_channel else {},
    )
    agents = AgentsCfg(
        escalation=EscalationCfg(mattermost_user="tech-lead"),
    )
    return AppConfig(
        repositories=[RepositoryCfg(key="bellingshausen", url="git@x:g/bellingshausen.git")],
        agents=agents,
        mappings=mappings,
    )


async def _insert_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    iid: int = 42,
    last_pipeline_notified_status: str | None = None,
) -> int:
    async with session_scope(session_factory) as session:
        row = MergeRequestRow(
            repo_key="bellingshausen", iid=iid, external_id=str(iid),
            task_external_id="DM-1", title="t", description="",
            source_branch=f"ai-dev/dm-1-{iid}", target_branch="master",
            author_username="virtual-dev",
            web_url=f"https://gitlab/x/merge_requests/{iid}",
            status="open",
            last_pipeline_notified_status=last_pipeline_notified_status,
        )
        session.add(row)
        await session.flush()
        return row.id


def _job(name: str, status: str, log: str = "") -> PipelineJob:
    return PipelineJob(
        id=1, name=name, stage="test", status=status,
        web_url=f"https://gitlab/x/-/jobs/{name}",
        log_excerpt=log,
    )


def test_collapse_status() -> None:
    assert _collapse_status([]) == "unknown"
    assert _collapse_status([_job("a", "success"), _job("b", "success")]) == "success"
    assert _collapse_status([_job("a", "success"), _job("b", "failed")]) == "failed"
    assert _collapse_status([_job("a", "running"), _job("b", "pending")]) == "running"


@pytest.mark.asyncio
async def test_red_pipeline_fires_notification(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mr_id = await _insert_mr(session_factory)
    vcs = _StubVcs({
        ("bellingshausen", 42): [
            _job("lint", "success"),
            _job("tests", "failed", log="AssertionError: expected 1, got 2"),
        ],
    })
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
    )

    stats = await agent.tick()
    assert stats.failures_detected == 1
    assert stats.notifications_sent == 1
    assert any("Pipeline FAILED" in body for _, body in chat.sent_channels)

    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_status == "failed"
        assert row.last_pipeline_notified_status == "failed"


@pytest.mark.asyncio
async def test_still_red_does_not_re_notify(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_mr(session_factory, last_pipeline_notified_status="failed")
    vcs = _StubVcs({
        ("bellingshausen", 42): [_job("tests", "failed")],
    })
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
    )

    stats = await agent.tick()
    assert stats.failures_detected == 0
    assert chat.sent_channels == []


@pytest.mark.asyncio
async def test_recovery_clears_notified_flag(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mr_id = await _insert_mr(session_factory, last_pipeline_notified_status="failed")
    vcs = _StubVcs({
        ("bellingshausen", 42): [_job("tests", "success")],
    })
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
    )

    stats = await agent.tick()
    assert stats.failures_detected == 0
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_status == "success"
        assert row.last_pipeline_notified_status is None
