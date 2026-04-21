"""Mapping helpers between domain models and ORM rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from virtual_dev.domain.models.plan import OpenQuestion, Plan, PlanStatus, PlanStep
from virtual_dev.domain.models.task import Task, TaskLink, TaskPriority, TaskStatus
from virtual_dev.infrastructure.db.models import PlanRow, TaskRow


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


# --- Plan ---


def plan_to_row(plan: Plan) -> PlanRow:
    return PlanRow(
        tracker=plan.tracker,
        task_external_id=plan.task_external_id,
        summary=plan.summary,
        steps_json=[asdict(step) for step in plan.steps],
        open_questions_json=[asdict(q) for q in plan.open_questions],
        risks_json=list(plan.risks),
        confidence=plan.confidence,
        status=plan.status.value,
        target_repo_key=plan.target_repo_key,
        cost_usd=plan.cost_usd,
        iterations=plan.iterations,
        model=plan.model,
        agent_key=plan.agent_key,
    )


def row_to_plan(row: PlanRow) -> Plan:
    steps_raw = cast(list[dict[str, Any]], row.steps_json or [])
    questions_raw = cast(list[dict[str, Any]], row.open_questions_json or [])
    return Plan(
        task_external_id=row.task_external_id,
        tracker=row.tracker,
        summary=row.summary,
        steps=[PlanStep(**step) for step in steps_raw],
        open_questions=[OpenQuestion(**q) for q in questions_raw],
        risks=list(row.risks_json or []),
        confidence=row.confidence,
        status=PlanStatus(row.status),
        target_repo_key=row.target_repo_key,
        cost_usd=row.cost_usd,
        iterations=row.iterations,
        model=row.model,
        agent_key=row.agent_key,
        created_at=row.created_at,
    )
