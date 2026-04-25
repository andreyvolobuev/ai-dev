"""Unit tests for the new auto-fix DevOpsAgent."""

from __future__ import annotations

import asyncio
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
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
)
from virtual_dev.infrastructure.config.schema import (
    EscalationCfg,
    PipelinePolicyCfg,
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

    # everything else: not used
    async def ensure_clone(self, repo_key: str) -> str: raise NotImplementedError  # pragma: no cover
    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None: raise NotImplementedError  # pragma: no cover
    async def create_branch(self, repo_key: str, branch: str, base: str) -> None: raise NotImplementedError  # pragma: no cover
    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None: raise NotImplementedError  # pragma: no cover
    async def commit_all(self, repo_key: str, message: str) -> str: raise NotImplementedError  # pragma: no cover
    async def push(self, repo_key: str, branch: str) -> None: raise NotImplementedError  # pragma: no cover
    async def current_branch(self, repo_key: str) -> str: raise NotImplementedError  # pragma: no cover
    async def has_uncommitted_changes(self, repo_key: str) -> bool: raise NotImplementedError  # pragma: no cover
    async def create_merge_request(self, *args, **kwargs) -> MergeRequest: raise NotImplementedError  # pragma: no cover
    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest: raise NotImplementedError  # pragma: no cover
    async def list_open_merge_requests(self, *args, **kwargs) -> Sequence[MergeRequest]: raise NotImplementedError  # pragma: no cover
    async def list_merged_merge_requests(self, *args, **kwargs) -> Sequence[MergeRequest]: raise NotImplementedError  # pragma: no cover
    async def list_review_comments(self, *args, **kwargs) -> Sequence[ReviewComment]: raise NotImplementedError  # pragma: no cover
    async def reply_to_comment(self, *args, **kwargs) -> None: raise NotImplementedError  # pragma: no cover
    async def add_mr_comment(self, *args, **kwargs) -> None: raise NotImplementedError  # pragma: no cover
    async def approve_merge_request(self, *args, **kwargs) -> None: raise NotImplementedError  # pragma: no cover
    async def merge(self, *args, **kwargs) -> None: raise NotImplementedError  # pragma: no cover
    async def get_mr_approvals(self, *args, **kwargs) -> ApprovalInfo: raise NotImplementedError  # pragma: no cover


class _RecordingChat(ChatPort):
    def __init__(self) -> None:
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]: return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return _msg(text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent_channels.append((channel_id, text))
        return _msg(text)

    async def find_user_by_email(self, email: str) -> ChatUser | None: return None  # pragma: no cover

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{username}", username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None: return None  # pragma: no cover
    async def get_post(self, post_id: str): return None  # pragma: no cover

    def subscribe(self) -> AsyncIterator[ChatMessage]: raise NotImplementedError  # pragma: no cover


class _ScriptedDev:
    """Pretends to be a DevAgent — records iteration calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def handle_iteration(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)


def _msg(text: str) -> ChatMessage:
    return ChatMessage(
        id="m", channel_id="c", author_id="bot", text=text,
        timestamp=datetime.now(timezone.utc),
    )


def _cfg(*, max_attempts: int = 3) -> AppConfig:
    agents = AgentsCfg(
        escalation=EscalationCfg(mattermost_user="tech-lead"),
        pipeline_policy=PipelinePolicyCfg(max_autofix_attempts=max_attempts),
    )
    return AppConfig(
        repositories=[RepositoryCfg(key="bellingshausen", url="git@x:g/bellingshausen.git")],
        agents=agents,
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg(
            pipeline_autofix_gave_up_dm=(
                "CI на `{repo_key}!{iid}` не починился после {attempts} попыток. "
                "Failing: {failing_jobs}. {web_url}"
            ),
        )),
    )


def _job(name: str, status: str, log: str = "") -> PipelineJob:
    return PipelineJob(
        id=1, name=name, stage="test", status=status,
        web_url=f"https://gitlab/x/-/jobs/{name}",
        log_excerpt=log,
    )


async def _insert_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    iid: int = 42,
    pipeline_autofix_attempts: int = 0,
    pipeline_autofix_escalated: bool = False,
    task_external_id: str | None = "DM-1",
) -> int:
    async with session_scope(session_factory) as session:
        row = MergeRequestRow(
            repo_key="bellingshausen", iid=iid, external_id=str(iid),
            task_external_id=task_external_id,
            title="t", description="",
            source_branch=f"ai-dev/dm-1-{iid}", target_branch="master",
            author_username="virtual-dev",
            web_url=f"https://gitlab/x/merge_requests/{iid}",
            status="open",
            pipeline_autofix_attempts=pipeline_autofix_attempts,
            pipeline_autofix_escalated=pipeline_autofix_escalated,
        )
        session.add(row)
        await session.flush()
        return row.id


def test_collapse_status() -> None:
    assert _collapse_status([]) == "unknown"
    assert _collapse_status([_job("a", "success")]) == "success"
    assert _collapse_status([_job("a", "success"), _job("b", "failed")]) == "failed"
    assert _collapse_status([_job("a", "running"), _job("b", "pending")]) == "running"


@pytest.mark.asyncio
async def test_red_pipeline_dispatches_dev_iteration_no_channel_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Red CI → Dev iteration with full log feedback. Channel stays silent."""
    await _insert_mr(session_factory)
    vcs = _StubVcs({
        ("bellingshausen", 42): [
            _job("lint", "success"),
            _job("tests", "failed", log="full log here\nAssertionError: nope"),
        ],
    })
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev()
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
    )

    stats = await agent.tick()
    assert stats.failures_detected == 1
    assert stats.autofix_dispatched == 1
    assert chat.sent_channels == []   # NEVER post CI failures to channels
    assert chat.sent_dms == []        # not yet exhausted

    # Wait for the background autofix task to finish; it just records the
    # iteration call and then increments the counter in the DB.
    await asyncio.sleep(0.05)

    assert len(dev.calls) == 1
    feedback = dev.calls[0]["feedback"]
    assert "tests" in feedback           # job name
    assert "AssertionError: nope" in feedback   # full log


@pytest.mark.asyncio
async def test_attempts_exhausted_dms_escalation_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After max_autofix_attempts, DM the team-lead. No channel post."""
    await _insert_mr(session_factory, pipeline_autofix_attempts=3)
    vcs = _StubVcs({("bellingshausen", 42): [_job("tests", "failed")]})
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    dev = _ScriptedDev()
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(max_attempts=3),
        dev_agents={"bellingshausen": dev},   # type: ignore[dict-item]
    )

    stats = await agent.tick()
    assert stats.escalations_sent == 1
    assert stats.autofix_dispatched == 0
    assert chat.sent_channels == []
    assert any("uid-tech-lead" == uid for uid, _ in chat.sent_dms)
    assert any("не починился" in body for _, body in chat.sent_dms)
    assert dev.calls == []


@pytest.mark.asyncio
async def test_already_escalated_does_not_re_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Once we DM'd the team-lead, don't keep DMing them on every tick."""
    await _insert_mr(
        session_factory,
        pipeline_autofix_attempts=3,
        pipeline_autofix_escalated=True,
    )
    vcs = _StubVcs({("bellingshausen", 42): [_job("tests", "failed")]})
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        dev_agents={"bellingshausen": _ScriptedDev()},   # type: ignore[dict-item]
    )

    stats = await agent.tick()
    assert stats.escalations_sent == 0
    assert chat.sent_dms == []


@pytest.mark.asyncio
async def test_green_pipeline_resets_counter_and_escalation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A green pipeline clears autofix bookkeeping so a future regression
    starts fresh."""
    mr_id = await _insert_mr(
        session_factory,
        pipeline_autofix_attempts=2,
        pipeline_autofix_escalated=True,
    )
    vcs = _StubVcs({("bellingshausen", 42): [_job("tests", "success")]})
    chat = _RecordingChat()
    communicator = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    agent = DevOpsAgent(
        vcs=vcs, communicator=communicator,
        session_factory=session_factory, config=_cfg(),
        dev_agents={"bellingshausen": _ScriptedDev()},   # type: ignore[dict-item]
    )

    stats = await agent.tick()
    assert stats.failures_detected == 0
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.id == mr_id)
        )).scalar_one()
        assert row.pipeline_status == "success"
        assert row.pipeline_autofix_attempts == 0
        assert row.pipeline_autofix_escalated is False
