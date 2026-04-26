"""Task-driven clarification orchestrator (Phase 4.5).

Runs the loop:

    pick task → list applicable tools → planner picks ONE tool →
    execute tool (SYNC | ASYNC | META) → if SYNC: validate result →
    if ASYNC: install awaiting_*, return (resume on coalesced reply) →
    if META: handle directly (decompose / escalate / abandon)

The validator is *chain-aware*: every validated response is checked
against the full ancestor chain. A response that solves the root
shortcuts the entire branch.

This module replaces ``GoalOrchestrator``. Goal-state-machine fields
(REPLANNING, COALESCING, …) are gone — task lifecycle is just
``is_solved``/``closed``. Internal bookkeeping (last_fragment_at,
last_planning_started_at, awaiting_*) lives on the row but is not
exposed to the LLM as state.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.clarification_tool_picker import (
    ClarificationToolPicker,
    PickerInput,
)
from virtual_dev.application.agents.clarification_validator import (
    ClarificationValidator,
    ValidatorInput,
)
from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.clarification.task_repo import (
    ClarificationTaskRepository,
)
from virtual_dev.application.services.clarification.tools import (
    ToolContext,
    ToolRegistry,
    discover_builtin_tools,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    TaskStepKind,
    ToolMode,
)
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


@dataclass
class TaskOrchestratorStats:
    tasks_created: int = 0
    fragments_appended: int = 0
    tool_picks: int = 0
    tool_invocations: int = 0
    validations: int = 0
    sync_resolutions: int = 0
    async_dispatches: int = 0
    decompositions: int = 0
    escalations: int = 0
    abandonments: int = 0
    re_dispatches: int = 0


class TaskOrchestrator:
    """Drives every ``ClarificationTask`` through the pick→apply→validate loop."""

    def __init__(
        self,
        *,
        repo: ClarificationTaskRepository,
        communicator: CommunicatorService,
        picker: ClarificationToolPicker,
        validator: ClarificationValidator,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        message_bus: MessageBusPort | None,
        trace: AgentTrace | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._communicator = communicator
        self._picker = picker
        self._validator = validator
        self._config = config
        self._session_factory = session_factory
        self._message_bus = message_bus
        self._trace = trace
        self._tools = tool_registry or discover_builtin_tools()
        self.stats = TaskOrchestratorStats()

    # ---------------------------------------------------------------- entry
    async def request_clarifications(
        self,
        *,
        task_row: TaskRow,
        plan: Plan,
        plan_row_id: int,
    ) -> int:
        if not plan.open_questions:
            return 0
        existing = await self._repo.existing_questions_for_plan(plan_row_id)
        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_goal_age_hours,
        )
        created = 0
        for oq in plan.open_questions:
            if oq.question in existing:
                continue
            task = await self._repo.create_task(
                plan_id=plan_row_id,
                parent_id=None,
                tracker=task_row.tracker,
                task_external_id=task_row.external_id,
                question=oq.question,
                info_source=(oq.ask_whom or "").strip() or None,
                info_source_class=None,
                coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
                deadline_at=deadline,
                depth=0,
            )
            self.stats.tasks_created += 1
            created += 1
            await self._emit("task_created", task)
            await self._run_one_step(task)
        return created

    async def find_task_by_thread(
        self, asked_post_id: str,
    ) -> ClarificationTask | None:
        return await self._repo.find_active_by_thread(asked_post_id)

    async def find_task_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> ClarificationTask | None:
        return await self._repo.find_active_by_channel(mm_channel_id, mm_user_id)

    async def append_fragment(
        self, task_id: int, mm_post: ChatMessage,
    ) -> bool:
        ok = await self._repo.append_fragment(
            task_id=task_id,
            mm_post_id=mm_post.id,
            asked_post_id=mm_post.thread_root_id,
            text=mm_post.text,
            received_at=mm_post.timestamp,
        )
        if ok:
            self.stats.fragments_appended += 1
            await emit_if(self._trace, AgentTraceEvent(
                type="task_fragment",
                agent_key="clarification",
                payload={
                    "task_id": task_id,
                    "post_id": mm_post.id,
                    "text": mm_post.text,
                },
            ))
        return ok

    # ---------------------------------------------------------------- ticks
    async def flush_idle(self) -> int:
        now = datetime.now(timezone.utc)
        idle = await self._repo.find_idle_awaiting(now=now)
        flushed = 0
        for task in idle:
            try:
                await self._coalesce_and_validate(task)
                flushed += 1
            except Exception:
                logger.exception(
                    "Task {}: coalesce+validate failed", task.id,
                )
        return flushed

    async def sweep_deadlines(self) -> int:
        now = datetime.now(timezone.utc)
        await self._wake_due(now=now)
        return await self._abandon_overdue(now=now)

    async def _wake_due(self, *, now: datetime) -> None:
        for task in await self._repo.find_due_waiting(now=now):
            await self._repo.update(
                task.id, clear_next_planner_run_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)

    async def _abandon_overdue(self, *, now: datetime) -> int:
        overdue = await self._repo.find_overdue(now=now)
        swept = 0
        for task in overdue:
            await self._abandon_task(
                task, reason="deadline_exceeded", escalate=True,
            )
            swept += 1
        return swept

    # ---------------------------------------------------------------- coalesce
    async def _coalesce_and_validate(
        self, task: ClarificationTask,
    ) -> None:
        """Idle window passed → merge fragments, append HUMAN_REPLIED step,
        run validator."""
        fragments = await self._repo.list_unflushed_fragments(task.id)
        if not fragments:
            return
        merged = "\n\n".join(
            f.text.strip() for f in fragments if f.text and f.text.strip()
        ) or fragments[0].text
        last_post_id = fragments[-1].mm_post_id

        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.HUMAN_REPLIED,
            text=merged,
            metadata={
                "asked_post_id": task.awaiting_post_id,
                "fragment_count": len(fragments),
                "from_user_id": task.awaiting_user_id,
                "from_username": task.awaiting_username,
            },
        )
        await self._repo.mark_fragments_flushed(task.id)
        if last_post_id:
            await self._communicator.add_reaction(last_post_id, "white_check_mark")

        await self._repo.update(
            task.id,
            current_response=merged,
            clear_awaiting=True,
        )
        refreshed = await self._repo.get(task.id)
        if refreshed is None:
            return

        await self._validate_and_route(
            refreshed, response_text=merged,
            response_source_label=task.awaiting_username or "",
            response_source_class="mattermost",
        )

    # ---------------------------------------------------------------- main loop
    async def _run_one_step(self, task: ClarificationTask) -> None:
        """Pick a tool, run it, route the result. Called when a task
        is fresh, when a SYNC tool didn't solve it, or when waking
        from a deferral."""
        if task.closed:
            return
        max_iter = self._config.agents.clarification.max_planner_calls_per_goal
        if task.iteration_count >= max_iter:
            logger.warning(
                "Task {}: max_iterations ({}) reached; escalating",
                task.id, max_iter,
            )
            await self._escalate_task(task, reason="max_iterations_reached")
            return

        await self._repo.update(
            task.id,
            iteration_count_delta=1,
            last_planning_started_at=datetime.now(timezone.utc),
        )

        chain = await self._repo.chain(task.id)
        history = await self._repo.list_steps(task.id)
        issue_summary = await self._load_issue_summary(task)
        repo_workspace = await self._resolve_repo_workspace(task)
        tools = self._tools.filter(tag="clarification")

        try:
            invocation = await self._picker.pick(PickerInput(
                task=task, chain=chain, history=history,
                issue_summary=issue_summary,
                repo_workspace=repo_workspace,
                available_tools=tools,
            ))
        except Exception:
            logger.exception("Task {}: picker crashed", task.id)
            await self._repo.update(
                task.id, clear_last_planning_started_at=True,
            )
            return
        self.stats.tool_picks += 1
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.PLANNER_DECIDED,
            text=invocation.reasoning,
            metadata={
                "tool": invocation.tool, "params": invocation.params,
            },
        )
        await self._emit("tool_picked", task, payload={
            "tool": invocation.tool,
            "reasoning": invocation.reasoning,
        })

        tool = self._tools.get(invocation.tool)
        if tool is None:
            logger.warning(
                "Task {}: picker chose unknown tool {!r}; escalating",
                task.id, invocation.tool,
            )
            await self._escalate_task(
                task, reason=f"unknown_tool:{invocation.tool}",
            )
            return

        ctx = ToolContext(
            task=task, chain=chain,
            communicator=self._communicator, config=self._config,
            session_factory=self._session_factory,
        )

        try:
            outcome = await tool.handler(invocation.params or {}, ctx)
        except Exception as exc:
            logger.exception(
                "Task {}: tool {!r} raised", task.id, invocation.tool,
            )
            await self._repo.append_step(
                task_id=task.id,
                kind=TaskStepKind.TOOL_RESULT,
                text=f"Tool crashed: {type(exc).__name__}: {exc}",
                metadata={"tool": invocation.tool, "error": True},
            )
            await self._repo.update(
                task.id,
                append_tool_tried=invocation.tool,
                clear_last_planning_started_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)
            return

        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.TOOL_INVOKED,
            text=invocation.reasoning,
            metadata={
                "tool": invocation.tool,
                "params": invocation.params,
                "mode": tool.mode.value,
            },
        )
        self.stats.tool_invocations += 1

        if outcome.error:
            await self._repo.append_step(
                task_id=task.id,
                kind=TaskStepKind.TOOL_RESULT,
                text=f"Tool error: {outcome.error}",
                metadata={"tool": invocation.tool, "error": outcome.error},
            )
            await self._repo.update(
                task.id,
                append_tool_tried=invocation.tool,
                clear_last_planning_started_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)
            return

        if tool.mode == ToolMode.SYNC and outcome.result is not None:
            await self._on_sync_result(task, invocation.tool, outcome.result)
        elif tool.mode == ToolMode.ASYNC and outcome.pending is not None:
            await self._on_async_dispatch(task, invocation.tool, outcome)
        elif tool.mode == ToolMode.META:
            await self._on_meta(task, invocation.tool, outcome)
        else:
            logger.warning(
                "Task {}: tool {!r} returned mode={} but no payload",
                task.id, invocation.tool, tool.mode,
            )
            await self._repo.update(
                task.id,
                append_tool_tried=invocation.tool,
                clear_last_planning_started_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)

    async def _on_sync_result(
        self, task: ClarificationTask, tool_name: str, result,
    ) -> None:
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.TOOL_RESULT,
            text=result.text or "",
            metadata={
                "tool": tool_name,
                "structured": result.structured,
                "source_label": result.source_label,
                "source_class": result.source_class,
            },
        )
        await self._repo.update(
            task.id,
            current_response=result.text or "",
        )
        refreshed = await self._repo.get(task.id)
        if refreshed is None:
            return
        await self._validate_and_route(
            refreshed, response_text=result.text or "",
            response_source_label=result.source_label or f"tool:{tool_name}",
            response_source_class=result.source_class or f"tool:{tool_name}",
            tool_name=tool_name,
        )

    async def _on_async_dispatch(
        self, task: ClarificationTask, tool_name: str, outcome,
    ) -> None:
        pending = outcome.pending
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.BOT_ASKED,
            text=pending.asked_text,
            metadata={
                "tool": tool_name,
                "asked_post_id": pending.asked_post_id,
                "channel_id": pending.channel_id,
                "target_user_id": pending.target_user_id,
                "target_username": pending.target_username,
                "dedupe_key": pending.dedupe_key,
            },
        )
        await self._repo.update(
            task.id,
            info_source=pending.info_source or task.info_source,
            info_source_class=pending.info_source_class or task.info_source_class,
            awaiting_post_id=pending.asked_post_id,
            awaiting_user_id=pending.target_user_id,
            awaiting_username=pending.target_username or "",
            awaiting_channel_id=pending.channel_id,
            awaiting_dedupe_key=pending.dedupe_key or "",
            append_tool_tried=tool_name,
            clear_last_planning_started_at=True,
        )
        self.stats.async_dispatches += 1

    async def _on_meta(
        self, task: ClarificationTask, tool_name: str, outcome,
    ) -> None:
        action = outcome.meta_action or ""
        if action == "decompose":
            await self._do_decompose(task, outcome.meta_payload or {})
        elif action == "escalate_to_lead":
            reason = (outcome.meta_payload or {}).get("reason", "planner_escalation")
            await self._escalate_task(task, reason=str(reason))
        elif action == "abandon":
            reason = (outcome.meta_payload or {}).get("reason", "planner_abandon")
            await self._abandon_task(task, reason=str(reason), escalate=False)
        else:
            logger.warning(
                "Task {}: meta-tool {!r} returned unknown action {!r}",
                task.id, tool_name, action,
            )
            await self._repo.update(
                task.id,
                append_tool_tried=tool_name,
                clear_last_planning_started_at=True,
            )

    async def _do_decompose(
        self, task: ClarificationTask, payload: dict,
    ) -> None:
        max_depth = self._config.agents.clarification.max_subgoal_depth
        if task.depth >= max_depth:
            await self._escalate_task(task, reason="max_subgoal_depth")
            return
        specs = payload.get("subtasks") or []
        if not specs:
            await self._escalate_task(task, reason="decompose_empty")
            return
        clar_cfg = self._config.agents.clarification
        deadline = task.deadline_at or (
            datetime.now(timezone.utc) + timedelta(hours=clar_cfg.max_goal_age_hours)
        )
        children: list[ClarificationTask] = []
        for spec in specs:
            child = await self._repo.create_task(
                plan_id=task.plan_id,
                parent_id=task.id,
                tracker=task.tracker,
                task_external_id=task.task_external_id,
                question=spec["question"],
                info_source=spec.get("info_source"),
                info_source_class=spec.get("info_source_class"),
                coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
                deadline_at=deadline,
                depth=task.depth + 1,
            )
            await self._repo.append_step(
                task_id=task.id,
                kind=TaskStepKind.SUBTASK_SPAWNED,
                text=spec["question"],
                metadata={"subtask_id": child.id},
            )
            children.append(child)
            await self._emit("subtask_spawned", task, payload={
                "subtask_id": child.id,
                "question": spec["question"],
            })
        self.stats.decompositions += 1
        await self._repo.update(
            task.id, clear_last_planning_started_at=True,
        )
        # Drive each child immediately.
        for child in children:
            await self._run_one_step(child)

    # ---------------------------------------------------------------- validate
    async def _validate_and_route(
        self,
        task: ClarificationTask,
        *,
        response_text: str,
        response_source_label: str,
        response_source_class: str,
        tool_name: str | None = None,
    ) -> None:
        chain = await self._repo.chain(task.id)
        try:
            verdict = await self._validator.validate(ValidatorInput(
                task=task, chain=chain,
                response_text=response_text,
                response_source_label=response_source_label,
                response_source_class=response_source_class,
                issue_summary=await self._load_issue_summary(task),
            ))
        except Exception:
            logger.exception("Task {}: validator crashed", task.id)
            await self._repo.update(
                task.id,
                append_tool_tried=tool_name or "unknown",
                clear_last_planning_started_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)
            return
        self.stats.validations += 1

        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.VALIDATED,
            text=verdict.reasoning or "",
            metadata={
                "resolves": [
                    {
                        "task_id": v.task_id,
                        "final_answer": v.final_answer,
                        "confidence": v.confidence,
                    }
                    for v in verdict.resolves
                ],
                "tool": tool_name,
            },
        )
        await self._emit("validated", task, payload={
            "tool": tool_name,
            "resolves_count": len(verdict.resolves),
            "reasoning": verdict.reasoning,
        })

        chain_ids = {c.id for c in chain}
        any_resolved = False
        # Sort resolves by depth so deepest closes first; helpful for
        # the resettle-plan logic to see consistent state.
        depth_by_id = {c.id: c.depth for c in chain}
        ordered = sorted(
            verdict.resolves,
            key=lambda v: depth_by_id.get(v.task_id, 0),
            reverse=True,
        )
        for v in ordered:
            if v.task_id not in chain_ids:
                logger.warning(
                    "Task {}: validator returned task_id {} not in chain; ignoring",
                    task.id, v.task_id,
                )
                continue
            await self._mark_solved(
                v.task_id, final_answer=v.final_answer, confidence=v.confidence,
            )
            any_resolved = True
            self.stats.sync_resolutions += 1

        if not any_resolved:
            # No task solved → record tool as tried, run loop again.
            await self._repo.update(
                task.id,
                append_tool_tried=tool_name or "unknown",
                clear_last_planning_started_at=True,
            )
            refreshed = await self._repo.get(task.id)
            if refreshed is not None:
                await self._run_one_step(refreshed)
            return

        # Some tasks solved. Cascade ancestors that are now obsolete:
        # if a deeper-task resolution incidentally answers the parent,
        # the parent will already have been in `resolves` (validator's
        # job). We only need to settle the plan and trigger any
        # parent's NEXT step if it became unblocked.
        await self._cascade_after_resolution(task)

    async def _mark_solved(
        self, task_id: int, *, final_answer: str, confidence: float,
    ) -> None:
        await self._repo.update(
            task_id,
            is_solved=True,
            final_answer=final_answer,
            confidence=confidence,
            clear_awaiting=True,
            closed=True,
            clear_last_planning_started_at=True,
        )
        await self._repo.append_step(
            task_id=task_id,
            kind=TaskStepKind.NOTE,
            text=f"Task solved (confidence={confidence:.2f}): {final_answer[:200]}",
            metadata={"final_answer": final_answer, "confidence": confidence},
        )

    async def _cascade_after_resolution(
        self, anchor: ClarificationTask,
    ) -> None:
        """After at least one task in the chain is solved, drive
        further work:

        * Any UNCLOSED ancestors continue: their next planner pick may
          succeed now that a child finished.
        * If every ancestor in the chain is closed, the plan-resettle
          check runs.
        """
        chain = await self._repo.chain(anchor.id)
        # Walk root → ... → anchor; the deepest unclosed task gets a
        # ``_run_one_step`` so its planner can react to the new
        # subtask outcome.
        for t in chain:
            refreshed = await self._repo.get(t.id)
            if refreshed is None:
                continue
            if not refreshed.closed:
                # Notify on parents: append a SUBTASK_RESOLVED step so
                # the picker sees what just happened on the next pick.
                if refreshed.id != anchor.id:
                    await self._repo.append_step(
                        task_id=refreshed.id,
                        kind=TaskStepKind.SUBTASK_RESOLVED,
                        text=anchor.final_answer or "",
                        metadata={"subtask_id": anchor.id},
                    )
                await self._run_one_step(refreshed)
                return
        # All closed → maybe re-dispatch.
        await self._maybe_resettle_plan(anchor)

    # ---------------------------------------------------------------- terminals
    async def _abandon_task(
        self, task: ClarificationTask, *, reason: str, escalate: bool,
    ) -> None:
        await self._repo.update(
            task.id,
            is_solved=False,
            closed=True,
            clear_awaiting=True,
            clear_last_planning_started_at=True,
        )
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.NOTE,
            text=f"Task abandoned: {reason}",
            metadata={"reason": reason, "kind": "abandon"},
        )
        self.stats.abandonments += 1
        if escalate:
            await self._send_lead_escalation(task, reason=reason)
        await self._cascade_after_terminal(task)

    async def _escalate_task(
        self, task: ClarificationTask, *, reason: str,
    ) -> None:
        await self._repo.update(
            task.id,
            is_solved=False,
            closed=True,
            clear_awaiting=True,
            clear_last_planning_started_at=True,
        )
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.NOTE,
            text=f"Task escalated: {reason}",
            metadata={"reason": reason, "kind": "escalate"},
        )
        self.stats.escalations += 1
        await self._send_lead_escalation(task, reason=reason)
        await self._cascade_after_terminal(task)

    async def _cascade_after_terminal(
        self, anchor: ClarificationTask,
    ) -> None:
        # Same logic as _cascade_after_resolution but the parent
        # gets a SUBTASK_RESOLVED step with the abandon/escalate note.
        chain = await self._repo.chain(anchor.id)
        for t in chain:
            refreshed = await self._repo.get(t.id)
            if refreshed is None:
                continue
            if not refreshed.closed:
                if refreshed.id != anchor.id:
                    await self._repo.append_step(
                        task_id=refreshed.id,
                        kind=TaskStepKind.SUBTASK_RESOLVED,
                        text=f"Subtask {anchor.id} ended without an answer",
                        metadata={
                            "subtask_id": anchor.id,
                            "subtask_outcome": "terminal_no_answer",
                        },
                    )
                await self._run_one_step(refreshed)
                return
        await self._maybe_resettle_plan(anchor)

    async def _send_lead_escalation(
        self, task: ClarificationTask, *, reason: str,
    ) -> None:
        lead_user_id = await self._lead_user_id()
        chain = await self._render_chain(task)
        task_url = await self._task_url(task)
        body_template = (
            self._config.notifications.mattermost.clarifier_escalation_to_lead
            or
            "Застрял с уточнением по тикету [{external_id}]({task_url}).\n\n"
            "**Причина:** {reason}\n\n**Цель:** {original_question}\n\n"
            "**Цепочка:**\n{chain_summary}"
        )
        body = body_template.format(
            tracker=task.tracker,
            external_id=task.task_external_id,
            task_url=task_url,
            original_question=task.question,
            chain_summary=chain,
            reason=reason,
        )
        if lead_user_id is None:
            await emit_if(self._trace, AgentTraceEvent(
                type="escalation_dropped",
                agent_key="clarification",
                payload={
                    "task_id": task.id,
                    "question": task.question,
                    "reason": reason,
                    "configured_handle": (
                        self._config.agents.escalation.mattermost_user or ""
                    ),
                    "body": body,
                    "note": "No team-lead handle configured.",
                },
            ))
            return
        outcome = await self._communicator.send_dm(lead_user_id, body)
        await emit_if(self._trace, AgentTraceEvent(
            type="escalation_sent" if outcome.sent else "escalation_dropped",
            agent_key="clarification",
            payload={
                "task_id": task.id,
                "question": task.question,
                "reason": reason,
                "lead_user_id": lead_user_id,
                "sent": outcome.sent,
                "skip_reason": outcome.skip_reason,
                "body": body,
            },
        ))

    async def _render_chain(self, task: ClarificationTask) -> str:
        steps = await self._repo.list_steps(task.id)
        if not steps:
            return "(no steps)"
        lines: list[str] = []
        for step in steps:
            text = (step.text or "").strip().splitlines()[0] if step.text else ""
            lines.append(
                f"- [{step.seq}] {step.kind.value}: «{text[:160]}»"
            )
        return "\n".join(lines)

    async def _task_url(self, task: ClarificationTask) -> str:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == task.tracker,
                    TaskRow.external_id == task.task_external_id,
                )
            )).scalar_one_or_none()
        return (row.url if row else "") or ""

    async def _lead_user_id(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    # ---------------------------------------------------------------- plan settle
    async def _maybe_resettle_plan(
        self, anchor: ClarificationTask,
    ) -> None:
        if anchor.plan_id is None:
            return
        top_level = await self._repo.list_top_level_for_plan(anchor.plan_id)
        if any(not t.closed for t in top_level):
            return
        if not any(t.is_solved for t in top_level):
            logger.info(
                "Plan {}: all top-level tasks closed, none solved — no re-dispatch",
                anchor.plan_id,
            )
            return
        await self._reseed_task_description(anchor, top_level)
        if self._message_bus is not None:
            await self._message_bus.publish(AgentMessage(
                id=uuid.uuid4().hex,
                from_agent="clarification",
                to_agent="analyst",
                topic="task.discovered",
                payload={
                    "tracker": anchor.tracker,
                    "external_id": anchor.task_external_id,
                },
            ))
            self.stats.re_dispatches += 1

    async def _reseed_task_description(
        self, anchor: ClarificationTask, siblings: list[ClarificationTask],
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == anchor.tracker,
                    TaskRow.external_id == anchor.task_external_id,
                )
            )).scalar_one_or_none()
            if row is None:
                return
            block = "\n\n## Уточнения от человека (собраны ботом)\n"
            for i, t in enumerate(siblings):
                block += f"\n**Q{i + 1}:** {t.question.strip()}\n"
                if t.is_solved and t.final_answer:
                    block += f"**A:** {t.final_answer.strip()}\n"
                else:
                    block += "_(не получили ответ)_\n"
            base_desc = row.description or ""
            if block.strip() not in base_desc:
                row.description = (base_desc.rstrip() + "\n" + block).strip()
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == anchor.plan_id)
            )).scalar_one_or_none()
            if plan_row is not None:
                plan_row.status = PlanStatus.SUPERSEDED.value
            row.internal_status = "discovered"
            row.updated_at = datetime.now(timezone.utc)

    # ---------------------------------------------------------------- helpers
    async def _load_issue_summary(self, task: ClarificationTask) -> str:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == task.tracker,
                    TaskRow.external_id == task.task_external_id,
                )
            )).scalar_one_or_none()
        if row is None:
            return ""
        desc = (row.description or "").strip()
        if len(desc) > 3000:
            desc = desc[:3000] + "\n[truncated]"
        return f"# {row.title}\n\n{desc}"

    async def _resolve_repo_workspace(
        self, task: ClarificationTask,
    ) -> str | None:
        from pathlib import Path

        if task.plan_id is None:
            return None
        async with self._session_factory() as session:
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == task.plan_id)
            )).scalar_one_or_none()
        if plan_row is None or not plan_row.target_repo_key:
            return None
        repo_cfg = self._config.get_repository(plan_row.target_repo_key)
        if repo_cfg is None or not repo_cfg.local_path:
            return None
        return str(Path(repo_cfg.local_path).expanduser().resolve())

    async def _emit(
        self,
        action: str,
        task: ClarificationTask,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        body = {
            "task_id": task.id,
            "question": task.question,
            "is_solved": task.is_solved,
            "closed": task.closed,
            "depth": task.depth,
            "action": action,
        }
        if payload:
            body.update(payload)
        await emit_if(self._trace, AgentTraceEvent(
            type="task_event",
            agent_key="clarification",
            payload=body,
        ))


__all__ = ["TaskOrchestrator", "TaskOrchestratorStats"]
