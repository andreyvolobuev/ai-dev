"""Unit tests for the Task domain model and its ORM mapping."""

from __future__ import annotations

from datetime import datetime, timezone

from virtual_dev.domain.models.task import Task, TaskLink, TaskPriority, TaskStatus
from virtual_dev.infrastructure.db.mappers import row_to_task, task_to_row


def _sample_task() -> Task:
    return Task(
        external_id="DM-1",
        tracker="jira",
        title="Fix the thing",
        description="Describe the thing",
        url="https://jira.example/browse/DM-1",
        assignee_id="alice",
        components=["api"],
        labels=["ai-dev"],
        links=[TaskLink(url="https://wiki.example/p/42", kind="confluence")],
        priority=TaskPriority.HIGH,
        external_status="To Do",
        created_at=datetime(2025, 4, 21, 10, tzinfo=timezone.utc),
        updated_at=datetime(2025, 4, 21, 11, tzinfo=timezone.utc),
        internal_status=TaskStatus.DISCOVERED,
    )


def test_defaults_are_safe() -> None:
    task = Task(
        external_id="X-1",
        tracker="jira",
        title="t",
        description="",
        url="https://x",
    )
    assert task.internal_status is TaskStatus.DISCOVERED
    assert task.priority is TaskPriority.MEDIUM
    assert task.components == []
    assert task.labels == []
    assert task.links == []
    assert task.dor_satisfied is False


def test_task_roundtrip_through_orm() -> None:
    original = _sample_task()
    row = task_to_row(original)
    recovered = row_to_task(row)

    assert recovered.external_id == original.external_id
    assert recovered.tracker == original.tracker
    assert recovered.title == original.title
    assert recovered.components == original.components
    assert recovered.labels == original.labels
    assert recovered.priority is TaskPriority.HIGH
    assert recovered.internal_status is TaskStatus.DISCOVERED
    assert recovered.links == original.links
