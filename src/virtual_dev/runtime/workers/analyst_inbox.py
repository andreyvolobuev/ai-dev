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

from virtual_dev.application.agents import AnalystAgent
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    dev_agent_key,
)
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AgentsCfg


def _render_plan_comment(plan: Plan, dashboard_url: str | None = None) -> str:
    """Human-readable summary for a Jira comment."""
    lines: list[str] = []
    lines.append("*[virtual-dev] Analyst produced a plan for this ticket.*")
    lines.append("")
    lines.append(f"*Status:* {plan.status.value}")
    lines.append(f"*Confidence:* {plan.confidence:.2f}")
    if plan.target_repo_key:
        lines.append(f"*Target repo:* {plan.target_repo_key}")
    lines.append("")
    lines.append("*Summary*")
    lines.append(plan.summary or "(empty)")
    if plan.steps:
        lines.append("")
        lines.append("*Steps*")
        for step in plan.steps:
            lines.append(f"{step.order}. {step.summary}")
    if plan.open_questions:
        lines.append("")
        lines.append("*Open questions* (blocking implementation)")
        for q in plan.open_questions:
            bits = [f"- {q.question}"]
            if q.ask_whom:
                bits.append(f"(ask: {q.ask_whom})")
            lines.append(" ".join(bits))
    if plan.risks:
        lines.append("")
        lines.append("*Risks*")
        for risk in plan.risks:
            lines.append(f"- {risk}")
    lines.append("")
    lines.append(f"_Turns: {plan.iterations}._")
    if dashboard_url:
        lines.append(f"_Dashboard: {dashboard_url}_")
    return "\n".join(lines)


class AnalystInbox:
    """Single-purpose handler bound to the ``task.discovered`` topic."""

    def __init__(
        self,
        *,
        analyst: AnalystAgent,
        task_tracker: TaskTrackerPort | None,
        agents_config: AgentsCfg,
        message_bus: MessageBusPort | None = None,
        post_to_tracker: bool = True,
        dev_specialisation: str = "backend",
    ) -> None:
        self._analyst = analyst
        self._task_tracker = task_tracker
        self._agents_config = agents_config
        self._message_bus = message_bus
        self._post_to_tracker = post_to_tracker
        self._dev_specialisation = dev_specialisation

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
            to_in_progress = self._agents_config.jira_transitions.to_in_progress
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
            body = _render_plan_comment(plan)
            try:
                await self._task_tracker.comment(external_id, body)
            except Exception:
                logger.exception(
                    "AnalystInbox: failed to comment plan on {}", external_id,
                )

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
