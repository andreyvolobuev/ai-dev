"""Mapping helpers between domain models and ORM rows."""

from __future__ import annotations

from dataclasses import asdict

from virtual_dev.domain.models.task import Task, TaskLink, TaskPriority, TaskStatus
from virtual_dev.infrastructure.db.models import TaskRow


def task_to_row(task: Task) -> TaskRow:
    return TaskRow(
        tracker=task.tracker,
        external_id=task.external_id,
        title=task.title,
        description=task.description,
        url=task.url,
        assignee_id=task.assignee_id,
        reporter_id=task.reporter_id,
        components_json=list(task.components),
        labels_json=list(task.labels),
        links_json=[asdict(link) for link in task.links],
        priority=task.priority.value,
        external_status=task.external_status,
        created_at_external=task.created_at,
        updated_at_external=task.updated_at,
        internal_status=task.internal_status.value,
        target_repo_key=task.target_repo_key,
        dor_satisfied=task.dor_satisfied,
    )


def update_row_from_task(row: TaskRow, task: Task) -> None:
    """Refresh mutable fields on an existing row. Identity is not touched."""
    row.title = task.title
    row.description = task.description
    row.url = task.url
    row.assignee_id = task.assignee_id
    row.reporter_id = task.reporter_id
    row.components_json = list(task.components)
    row.labels_json = list(task.labels)
    row.links_json = [asdict(link) for link in task.links]
    row.priority = task.priority.value
    row.external_status = task.external_status
    row.updated_at_external = task.updated_at
    # internal_status and target_repo_key are ours — don't overwrite from tracker.


def row_to_task(row: TaskRow) -> Task:
    return Task(
        external_id=row.external_id,
        tracker=row.tracker,
        title=row.title,
        description=row.description,
        url=row.url,
        assignee_id=row.assignee_id,
        reporter_id=row.reporter_id,
        components=list(row.components_json or []),
        labels=list(row.labels_json or []),
        links=[TaskLink(**link) for link in (row.links_json or [])],
        priority=TaskPriority(row.priority),
        external_status=row.external_status,
        created_at=row.created_at_external,
        updated_at=row.updated_at_external,
        internal_status=TaskStatus(row.internal_status),
        target_repo_key=row.target_repo_key,
        dor_satisfied=row.dor_satisfied,
    )
