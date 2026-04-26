"""Bus handler that turns ``task.discovered`` messages into planned tasks.

The handler is the *boundary* between pure agent work (AnalystAgent produces
a Plan) and Phase-1 side-effects (transitioning the Jira ticket to "In
Progress" and commenting the plan summary). Keeping side-effects out of the
agent itself keeps the agent easy to test.

When the plan lands in READY status the handler also publishes
``plan.ready`` on the bus so a Dev-agent (Phase 2) can pick it up.
"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import AnalystAgent
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    dev_agent_key,
)
from virtual_dev.application.services.clarification import GoalOrchestrator
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import PlanRow, TaskRow


def _render_plan_comment(
    plan: Plan,
    template: str,
    dashboard_url: str | None = None,
) -> str:
    """Format the Jira plan-summary comment from a config template.

    The template uses Python ``str.format`` with prebuilt section blocks
    (``{steps_block}`` etc.) — empty when the plan has no entries — so
    the YAML stays a single string without conditional logic.
    """
    target_repo_block = (
        f"*Target repo:* {plan.target_repo_key}\n" if plan.target_repo_key else ""
    )
    steps_block = ""
    if plan.steps:
        steps_block = "\n*Steps*\n" + "\n".join(
            f"{step.order}. {step.summary}" for step in plan.steps
        )
    open_questions_block = ""
    if plan.open_questions:
        lines = ["\n*Open questions* (blocking implementation)"]
        for q in plan.open_questions:
            bits = [f"- {q.question}"]
            if q.ask_whom:
                bits.append(f"(ask: {q.ask_whom})")
            lines.append(" ".join(bits))
        open_questions_block = "\n".join(lines)
    risks_block = ""
    if plan.risks:
        risks_block = "\n*Risks*\n" + "\n".join(f"- {r}" for r in plan.risks)
    dashboard_block = f"_Dashboard: {dashboard_url}_" if dashboard_url else ""
    try:
        return template.format(
            tracker=plan.tracker,
            external_id=plan.task_external_id,
            status=plan.status.value,
            confidence=f"{plan.confidence:.2f}",
            summary=plan.summary or "(empty)",
            iterations=plan.iterations,
            target_repo_block=target_repo_block,
            steps_block=steps_block,
            open_questions_block=open_questions_block,
            risks_block=risks_block,
            dashboard_block=dashboard_block,
        )
    except (KeyError, IndexError) as exc:
        logger.warning("AnalystInbox: plan template format failed: {}", exc)
        return template


class AnalystInbox:
    """Single-purpose handler bound to the ``task.discovered`` topic."""

    def __init__(
        self,
        *,
        analyst: AnalystAgent,
        task_tracker: TaskTrackerPort | None,
        config: AppConfig,
        message_bus: MessageBusPort | None = None,
        post_to_tracker: bool = True,
        dev_specialisation: str = "backend",
        goal_orchestrator: GoalOrchestrator | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._analyst = analyst
        self._task_tracker = task_tracker
        self._config = config
        self._message_bus = message_bus
        self._post_to_tracker = post_to_tracker
        self._dev_specialisation = dev_specialisation
        self._goals = goal_orchestrator
        self._session_factory = session_factory

    async def handle(self, message: AgentMessage) -> None:
        tracker = str(message.payload.get("tracker") or "")
        external_id = str(message.payload.get("external_id") or "")
        if not tracker or not external_id:
            logger.warning("AnalystInbox: malformed payload {}", message.payload)
            return

        # Phase 1: we optimistically transition to In Progress before planning
        # so humans see the ticket was picked up even if planning takes a
        # while. Rolling back on failure is not worth it — Analyst setting
        # internal_status=FAILED is enough signal for the dashboard.
        if self._post_to_tracker and self._task_tracker is not None:
            to_in_progress = self._config.agents.jira_transitions.to_in_progress
            try:
                await self._task_tracker.transition(external_id, to_in_progress)
            except Exception:
                logger.exception(
                    "AnalystInbox: failed to transition {} to {}",
                    external_id, to_in_progress,
                )

        plan = await self._analyst.handle_task(tracker, external_id)
        if plan is None:
            return  # skipped (idempotent)

        if plan.status == PlanStatus.FAILED:
            logger.warning(
                "AnalystInbox: plan failed for {} — skipping Jira comment",
                external_id,
            )
            return

        if self._post_to_tracker and self._task_tracker is not None:
            body = _render_plan_comment(
                plan, self._config.notifications.jira.plan_comment,
            )
            try:
                await self._task_tracker.comment(external_id, body)
            except Exception:
                logger.exception(
                    "AnalystInbox: failed to comment plan on {}", external_id,
                )

        # CLARIFYING plan with open questions → spawn one
        # ClarificationGoal per question. Phase 3.9 (goal-driven): the
        # planner agent decides each next step (ask, achieve, escalate,
        # abandon, wait). Dev-agent does NOT get ``plan.ready`` until
        # human answers come back and Analyst re-plans.
        if (
            plan.status == PlanStatus.CLARIFYING
            and plan.open_questions
            and self._goals is not None
            and self._session_factory is not None
        ):
            task_row, plan_row_id = await self._load_task_and_plan_id(
                tracker, external_id,
            )
            if task_row is not None and plan_row_id is not None:
                try:
                    await self._goals.request_clarifications(
                        task_row=task_row, plan=plan, plan_row_id=plan_row_id,
                    )
                except Exception:
                    logger.exception(
                        "AnalystInbox: goal dispatch failed for {}",
                        external_id,
                    )
            return

        # Phase 2 hand-off: when the plan is clean and a target repo is
        # known, publish plan.ready so the Dev-agent can pick it up. We do
        # NOT publish when the plan has open questions — the task waits for
        # a human to answer them.
        if (
            self._message_bus is not None
            and plan.status == PlanStatus.READY
            and plan.target_repo_key
        ):
            await self._message_bus.publish(AgentMessage(
                id=uuid.uuid4().hex,
                from_agent="analyst",
                to_agent=dev_agent_key(plan.target_repo_key, self._dev_specialisation),
                topic=TOPIC_PLAN_READY,
                payload={
                    "tracker": tracker,
                    "external_id": external_id,
                    "repo_key": plan.target_repo_key,
                },
            ))

    async def _load_task_and_plan_id(
        self, tracker: str, external_id: str,
    ) -> tuple[TaskRow | None, int | None]:
        """Look up the task row + the latest non-superseded plan id for
        clarifier bookkeeping."""
        assert self._session_factory is not None
        async with self._session_factory() as session:
            task_row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == tracker,
                    TaskRow.external_id == external_id,
                )
            )).scalar_one_or_none()
            if task_row is None:
                return None, None
            plan_row = (await session.execute(
                select(PlanRow)
                .where(
                    PlanRow.tracker == tracker,
                    PlanRow.task_external_id == external_id,
                    PlanRow.status != PlanStatus.SUPERSEDED.value,
                )
                .order_by(PlanRow.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            return task_row, (plan_row.id if plan_row is not None else None)
