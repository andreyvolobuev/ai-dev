"""Tests for ``DevInbox`` — the bus → DevAgent → tracker glue.

The DevAgent itself is exhaustively covered in ``test_dev_agent.py``;
this file focuses on inbox-level behaviour: how it reacts to
``CodeAgentPermanentError`` (must surface a Jira comment so a human can
fix the local checkout) and that it doesn't re-raise (so the bus acks
the message and the recovery sweep doesn't see a stuck CODING task).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from virtual_dev.application.agents.dev import (
    CodeAgentPermanentError,
    DevResult,
)
from virtual_dev.domain.models.task import Task
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
)
from virtual_dev.infrastructure.config.schema import (
    JiraTemplatesCfg,
    NotificationsCfg,
)
from virtual_dev.runtime.workers.dev_inbox import DevInbox


class _FakeTracker(TaskTrackerPort):
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        return []

    async def get_task(self, external_id: str) -> Task:
        raise NotImplementedError

    async def transition(self, external_id: str, to_status: str) -> None:
        self.transitions.append((external_id, to_status))

    async def comment(self, external_id: str, body: str) -> None:
        self.comments.append((external_id, body))


class _FakeDev:
    """Minimal dev stand-in. We only exercise the exception path here —
    DevAgent's success branches are covered by ``test_dev_agent.py``."""

    def __init__(self, *, raises: BaseException | None = None,
                 result: DevResult | None = None) -> None:
        self._raises = raises
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def handle_plan(self, tracker: str, external_id: str) -> DevResult:
        self.calls.append((tracker, external_id))
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _cfg() -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(
            jira=JiraTemplatesCfg(
                failure_comment="Dev failed.{branch_block}{notes_block}",
            ),
        ),
    )


def _msg(external_id: str = "DM-7") -> AgentMessage:
    return AgentMessage(
        id="msg-1", from_agent="analyst", to_agent="dev",
        topic="plan.ready", payload={"tracker": "jira", "external_id": external_id},
    )


@pytest.mark.asyncio
async def test_dev_inbox_posts_comment_on_permanent_error_then_returns() -> None:
    """If the dev raises ``CodeAgentPermanentError`` (e.g. dirty local
    checkout), the inbox must:

    1. Catch it (not re-raise) so the bus acks — no redelivery storm.
    2. Post a Jira comment so the human knows to clean up.

    DevAgent already moved the task to FAILED, so the recovery sweep
    won't re-publish ``plan.ready`` either."""
    tracker = _FakeTracker()
    dev = _FakeDev(
        raises=CodeAgentPermanentError(
            "local_path for 'bellingshausen' has uncommitted changes"
        ),
    )
    inbox = DevInbox(
        dev_agent=dev,  # type: ignore[arg-type]
        task_tracker=tracker,
        config=_cfg(),
    )

    # Must NOT raise — that would tell AgentRunner to skip the ack.
    await inbox.handle(_msg())

    assert len(tracker.comments) == 1
    ext_id, body = tracker.comments[0]
    assert ext_id == "DM-7"
    assert "uncommitted" in body
    assert "operator" in body.lower()


@pytest.mark.asyncio
async def test_dev_inbox_skips_comment_when_post_to_tracker_disabled() -> None:
    """The test-analyst UI passes ``post_to_tracker=False`` so a synthetic
    DM-TEST-1 ticket doesn't end up commenting on a real Jira ticket
    (or, worse, raising on a missing one). Permanent failures must
    respect that flag."""
    tracker = _FakeTracker()
    dev = _FakeDev(raises=CodeAgentPermanentError("dirty tree"))
    inbox = DevInbox(
        dev_agent=dev,  # type: ignore[arg-type]
        task_tracker=tracker,
        config=_cfg(),
        post_to_tracker=False,
    )

    await inbox.handle(_msg())

    assert tracker.comments == []


@pytest.mark.asyncio
async def test_dev_inbox_swallows_unrelated_exceptions_for_ack() -> None:
    """Existing behaviour: the inbox catches *any* exception so
    AgentRunner sees a clean return and acks the message. We don't want
    a transient bug in the dev-agent to wedge the bus on this one
    message forever. Lock that contract in here."""
    tracker = _FakeTracker()
    dev = _FakeDev(raises=RuntimeError("boom"))
    inbox = DevInbox(
        dev_agent=dev,  # type: ignore[arg-type]
        task_tracker=tracker,
        config=_cfg(),
    )

    await inbox.handle(_msg())  # must not raise

    # Unrelated exceptions don't get a tracker comment — the existing
    # contract (avoid spamming Jira with low-signal stack traces).
    assert tracker.comments == []
