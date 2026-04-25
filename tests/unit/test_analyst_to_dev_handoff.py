"""AnalystInbox publishes plan.ready; DevInbox handles it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import DevOutcome, DevResult
from virtual_dev.application.agents.orchestrator import TOPIC_PLAN_READY, dev_agent_key
from virtual_dev.domain.models.plan import OpenQuestion, Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    JiraTransitionsCfg,
    MappingsCfg,
)
from virtual_dev.runtime.workers import AnalystInbox, DevInbox


# --- Fakes ---


class _SpyBus(MessageBusPort):
    def __init__(self) -> None:
        self.published: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> None:
        self.published.append(message)

    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:  # pragma: no cover
        raise NotImplementedError


class _SpyTracker(TaskTrackerPort):
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    async def fetch_tasks(self, jql: str, limit: int = 50) -> list[Any]:  # pragma: no cover
        return []

    async def get_task(self, external_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def transition(self, external_id: str, to_status: str) -> None:
        self.transitions.append((external_id, to_status))

    async def comment(self, external_id: str, body: str) -> None:
        self.comments.append((external_id, body))


class _StubAnalyst:
    agent_key = "analyst"

    def __init__(self, plan: Plan | None) -> None:
        self._plan = plan

    async def handle_task(self, tracker: str, external_id: str) -> Plan | None:
        return self._plan


class _StubDev:
    def __init__(self, result: DevResult) -> None:
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def handle_plan(self, tracker: str, external_id: str) -> DevResult:
        self.calls.append((tracker, external_id))
        return self._result


def _cfg() -> AppConfig:
    from virtual_dev.infrastructure.config import (
        JiraTemplatesCfg, NotificationsCfg,
    )
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(jira_transitions=JiraTransitionsCfg(
            to_in_progress="In Progress", to_review="Review",
            to_testing="Testing", to_done="Done",
        )),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(
            jira=JiraTemplatesCfg(
                plan_comment="plan: {summary}",
                mr_link_comment="MR: {web_url} branch {branch}",
                failure_comment="failed: {branch_block} {notes_block}",
            ),
        ),
    )


# --- AnalystInbox → plan.ready ---


def _ready_plan(target_repo: str = "bellingshausen") -> Plan:
    return Plan(
        task_external_id="DM-9", tracker="jira",
        summary="do stuff", status=PlanStatus.READY,
        target_repo_key=target_repo, agent_key="analyst",
    )


def _clarifying_plan() -> Plan:
    return Plan(
        task_external_id="DM-9", tracker="jira",
        summary="need input",
        open_questions=[OpenQuestion(question="which schema?")],
        status=PlanStatus.CLARIFYING, target_repo_key="bellingshausen",
        agent_key="analyst",
    )


@pytest.mark.asyncio
async def test_analyst_inbox_publishes_plan_ready_on_ready_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = _SpyBus()
    inbox = AnalystInbox(
        analyst=_StubAnalyst(_ready_plan()),  # type: ignore[arg-type]
        task_tracker=None, config=_cfg(), message_bus=bus,
    )
    await inbox.handle(AgentMessage(
        id="x", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered", payload={"tracker": "jira", "external_id": "DM-9"},
    ))
    assert len(bus.published) == 1
    msg = bus.published[0]
    assert msg.topic == TOPIC_PLAN_READY
    assert msg.to_agent == dev_agent_key("bellingshausen", "backend")
    assert msg.payload == {
        "tracker": "jira", "external_id": "DM-9", "repo_key": "bellingshausen",
    }


@pytest.mark.asyncio
async def test_analyst_inbox_does_not_publish_on_clarifying_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = _SpyBus()
    inbox = AnalystInbox(
        analyst=_StubAnalyst(_clarifying_plan()),  # type: ignore[arg-type]
        task_tracker=None, config=_cfg(), message_bus=bus,
    )
    await inbox.handle(AgentMessage(
        id="x", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered", payload={"tracker": "jira", "external_id": "DM-9"},
    ))
    assert bus.published == []


@pytest.mark.asyncio
async def test_analyst_inbox_does_not_publish_without_target_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = _SpyBus()
    plan = _ready_plan(target_repo="")
    plan.target_repo_key = None
    inbox = AnalystInbox(
        analyst=_StubAnalyst(plan),  # type: ignore[arg-type]
        task_tracker=None, config=_cfg(), message_bus=bus,
    )
    await inbox.handle(AgentMessage(
        id="x", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered", payload={"tracker": "jira", "external_id": "DM-9"},
    ))
    assert bus.published == []


# --- DevInbox on plan.ready ---


@pytest.mark.asyncio
async def test_dev_inbox_transitions_and_comments_on_mr_opened() -> None:
    from virtual_dev.domain.models.merge_request import (
        MergeRequest, MRStatus, PipelineStatus,
    )

    tracker = _SpyTracker()
    dev_result = DevResult(
        outcome=DevOutcome.MR_OPENED,
        branch_name="ai-dev/dm-9-thing",
        commit_sha="sha",
        merge_request=MergeRequest(
            id="1", iid=77, project_id="p", title="t", description="",
            source_branch="ai-dev/dm-9-thing", target_branch="master",
            author_username="virtual-dev",
            web_url="https://gitlab.example/p/-/merge_requests/77",
            status=MRStatus.DRAFT, pipeline_status=PipelineStatus.UNKNOWN,
        ),
    )
    inbox = DevInbox(
        dev_agent=_StubDev(dev_result),  # type: ignore[arg-type]
        task_tracker=tracker, config=_cfg(),
    )

    await inbox.handle(AgentMessage(
        id="x", from_agent="analyst", to_agent="dev-bellingshausen-backend",
        topic=TOPIC_PLAN_READY,
        payload={"tracker": "jira", "external_id": "DM-9", "repo_key": "bellingshausen"},
    ))

    assert tracker.transitions == [("DM-9", "Review")]
    assert len(tracker.comments) == 1
    assert "merge_requests/77" in tracker.comments[0][1]


@pytest.mark.asyncio
async def test_dev_inbox_comments_on_failure() -> None:
    tracker = _SpyTracker()
    dev_result = DevResult(
        outcome=DevOutcome.FAILED,
        branch_name="ai-dev/dm-9",
        submission={"notes": "stuck on missing migration"},
    )
    inbox = DevInbox(
        dev_agent=_StubDev(dev_result),  # type: ignore[arg-type]
        task_tracker=tracker, config=_cfg(),
    )

    await inbox.handle(AgentMessage(
        id="x", from_agent="analyst", to_agent="dev-bellingshausen-backend",
        topic=TOPIC_PLAN_READY,
        payload={"tracker": "jira", "external_id": "DM-9", "repo_key": "bellingshausen"},
    ))

    assert tracker.transitions == []
    assert len(tracker.comments) == 1
    assert "stuck on missing migration" in tracker.comments[0][1]


@pytest.mark.asyncio
async def test_dev_inbox_silent_on_skipped() -> None:
    tracker = _SpyTracker()
    from virtual_dev.application.agents import DevSkipReason

    dev_result = DevResult(
        outcome=DevOutcome.SKIPPED,
        skip_reason=DevSkipReason.NO_READY_PLAN,
    )
    inbox = DevInbox(
        dev_agent=_StubDev(dev_result),  # type: ignore[arg-type]
        task_tracker=tracker, config=_cfg(),
    )

    await inbox.handle(AgentMessage(
        id="x", from_agent="analyst", to_agent="dev-bellingshausen-backend",
        topic=TOPIC_PLAN_READY,
        payload={"tracker": "jira", "external_id": "DM-9"},
    ))

    assert tracker.transitions == []
    assert tracker.comments == []
