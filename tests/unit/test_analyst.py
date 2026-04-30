"""Unit tests for AnalystAgent (Phase 5.0).

The plan-submission parser is exercised directly. The full flow
(continuous-reasoning loop with effects + AnalystInbox driver) is
tested in test_analyst_inbox.py — these tests focus on parsing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from virtual_dev.application.agents.analyst import (
    AnalystAgent,
    _plan_from_submission,
)
from virtual_dev.domain.models.analyst_conversation import (
    ConversationStep,
    ConversationStepKind,
)
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.infrastructure.db import TaskRow


def _task_row() -> TaskRow:
    return TaskRow(
        id=1, tracker="jira", external_id="DM-42",
        title="t", description="", url="",
        components_json=[], labels_json=[], links_json=[],
        priority="medium", external_status="To Do",
        internal_status=TaskStatus.DISCOVERED.value, dor_satisfied=False,
    )


def test_submission_parsing_happy_path() -> None:
    submission = {
        "summary": "Fix the thing",
        "steps": [
            {"order": 1, "summary": "Write test", "details": "...", "files_touched": ["t.py"]},
            {"order": 2, "summary": "Make it pass"},
        ],
        "open_questions": [],
        "risks": ["breaks the nightly job"],
        "confidence": 0.85,
        "target_repo_key": "demo",
        "status": "ready",
    }
    plan = _plan_from_submission(
        submission=submission, task_row=_task_row(), target_repo="demo",
        cost_usd=0.05, turns=7, model="m", agent_key="analyst",
    )
    assert plan.status is PlanStatus.READY
    assert [s.order for s in plan.steps] == [1, 2]
    assert plan.steps[0].files_touched == ["t.py"]
    assert plan.confidence == 0.85
    assert plan.target_repo_key == "demo"
    assert plan.cost_usd == 0.05


def test_submission_parsing_clamps_confidence() -> None:
    submission = {
        "summary": "x", "steps": [], "risks": [],
        "confidence": 1.8, "status": "ready",
    }
    plan = _plan_from_submission(
        submission=submission, task_row=_task_row(), target_repo=None,
        cost_usd=0.0, turns=0, model="m", agent_key="analyst",
    )
    assert plan.confidence == 1.0
    assert plan.status is PlanStatus.READY


def test_submission_parsing_unknown_status_becomes_failed() -> None:
    submission = {
        "summary": "x", "steps": [], "risks": [],
        "confidence": 0.5, "status": "whatever",
    }
    plan = _plan_from_submission(
        submission=submission, task_row=_task_row(), target_repo=None,
        cost_usd=0.0, turns=0, model="m", agent_key="analyst",
    )
    assert plan.status is PlanStatus.FAILED


# --- _build_dm_threads -------------------------------------------------

def _step(
    seq: int,
    kind: ConversationStepKind,
    metadata: dict[str, object],
) -> ConversationStep:
    return ConversationStep(
        id=seq, task_id=1, seq=seq, kind=kind,
        timestamp=datetime.now(timezone.utc),
        text="", metadata=metadata,
    )


def test_build_dm_threads_empty_history() -> None:
    assert AnalystAgent._build_dm_threads([]) == {}


def test_build_dm_threads_thread_reply_anchors_thread() -> None:
    """Latest reply was in-thread → emit a thread anchor for that user."""
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-1",
        }),
        _step(2, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": True,
        }),
    ]
    threads = AnalystAgent._build_dm_threads(history)
    assert threads == {
        "uid-A": {"channel_id": "dm-A", "root_id": "bot-post-1"},
    }


def test_build_dm_threads_top_level_reply_no_anchor() -> None:
    """Latest reply was top-level → no anchor; bot will send top-level."""
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-1",
        }),
        _step(2, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": False,
        }),
    ]
    assert AnalystAgent._build_dm_threads(history) == {}


def test_build_dm_threads_mirrors_latest_when_user_switches_modes() -> None:
    """If the user replied in-thread first and later top-level, the
    map reflects the LATEST mode (top-level → no anchor)."""
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-1",
        }),
        _step(2, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": True,
        }),
        _step(3, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-2",
        }),
        _step(4, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": False,
        }),
    ]
    assert AnalystAgent._build_dm_threads(history) == {}


def test_build_dm_threads_anchors_to_most_recent_ask() -> None:
    """Two in-thread replies in a row — the anchor follows the latest
    BOT_ASKED, so subsequent DMs land in the freshest thread."""
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-1",
        }),
        _step(2, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": True,
        }),
        _step(3, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "bot-post-2",
        }),
        _step(4, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": True,
        }),
    ]
    threads = AnalystAgent._build_dm_threads(history)
    assert threads == {
        "uid-A": {"channel_id": "dm-A", "root_id": "bot-post-2"},
    }


def test_build_dm_threads_per_recipient_independent() -> None:
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-A", "channel_id": "dm-A",
            "asked_post_id": "post-A1",
        }),
        _step(2, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-A", "replied_in_thread": True,
        }),
        _step(3, ConversationStepKind.BOT_ASKED, {
            "target_user_id": "uid-B", "channel_id": "dm-B",
            "asked_post_id": "post-B1",
        }),
        _step(4, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-B", "replied_in_thread": False,
        }),
    ]
    threads = AnalystAgent._build_dm_threads(history)
    assert threads == {
        "uid-A": {"channel_id": "dm-A", "root_id": "post-A1"},
    }


def test_build_dm_threads_ignores_reply_with_no_prior_ask() -> None:
    """Defensive: a HUMAN_REPLIED without any preceding BOT_ASKED for
    that user (corrupted log) is silently dropped."""
    history = [
        _step(1, ConversationStepKind.HUMAN_REPLIED, {
            "from_user_id": "uid-Z", "replied_in_thread": True,
        }),
    ]
    assert AnalystAgent._build_dm_threads(history) == {}


