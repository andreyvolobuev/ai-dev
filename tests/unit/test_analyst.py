"""Unit tests for AnalystAgent (Phase 5.0).

The plan-submission parser is exercised directly. The full flow
(continuous-reasoning loop with effects + AnalystInbox driver) is
tested in test_analyst_inbox.py — these tests focus on parsing.
"""

from __future__ import annotations

import pytest

from virtual_dev.application.agents.analyst import _plan_from_submission
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


