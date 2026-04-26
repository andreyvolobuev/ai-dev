"""Task-driven clarification orchestrator (Phase 4.6).

Thin shell around :class:`ClarificationAgent`. Responsibilities:

* Spawn one task per analyst open-question.
* Drive the agent on creation and after every coalesced human reply.
* Persist what the agent did (steps + state).
* Translate agent effects (ask_dispatched / final_answer / escalate /
  abandon) into DB writes + DM lead / re-publish task.discovered.
* Coalesce fragments (idle window) and sweep deadlines.

The agent itself does all the LLM-level reasoning: which tool, when
to ask, when to declare done, how to phrase questions. There's no
separate picker / validator any more — Claude-Code-like: one brain,
one chain of thought, persistent across human-reply latency by
re-rendering history into each prompt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.clarification_agent import (
    AgentEffect,
    AgentRunInput,
    AgentRunResult,
    ClarificationAgent,
)
from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.clarification.task_repo import (
    ClarificationTaskRepository,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    TaskStepKind,
)
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope


@dataclass
class TaskOrchestratorStats:
    tasks_created: int = 0
    fragments_appended: int = 0
    agent_runs: int = 0
    asks_dispatched: int = 0
    final_answers: int = 0
    escalations: int = 0
    abandonments: int = 0
    re_dispatches: int = 0


class TaskOrchestrator:
    """Drives every ``ClarificationTask`` via the ClarificationAgent."""

    def __init__(
        self,
        *,
        repo: ClarificationTaskRepository,
        communicator: CommunicatorService,
        agent: ClarificationAgent,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        message_bus: MessageBusPort | None,
        trace: AgentTrace | None = None,
    ) -> None:
        self._repo = repo
        self._communicator = communicator
        self._agent = agent
        self._config = config
        self._session_factory = session_factory
        self._message_bus = message_bus
        self._trace = trace
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
            await self._drive(task)
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
                await self._coalesce_and_drive(task)
                flushed += 1
            except Exception:
                logger.exception(
                    "Task {}: coalesce+drive failed", task.id,
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
                await self._drive(refreshed)

    async def _abandon_overdue(self, *, now: datetime) -> int:
        overdue = await self._repo.find_overdue(now=now)
        swept = 0
        for task in overdue:
            await self._mark_terminal(
                task,
                kind="escalate",
                reason="deadline_exceeded",
                escalate=True,
            )
            swept += 1
        return swept

    # ---------------------------------------------------------------- coalesce
    async def _coalesce_and_drive(self, task: ClarificationTask) -> None:
        """Idle window passed → merge fragments, append HUMAN_REPLIED
        step, drive the agent."""
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
        await self._drive(refreshed)

    # ---------------------------------------------------------------- main loop
    async def _drive(self, task: ClarificationTask) -> None:
        """Run the agent once on this task, then react to the effects."""
        if task.closed:
            return
        max_iter = self._config.agents.clarification.max_planner_calls_per_goal
        if task.iteration_count >= max_iter:
            logger.warning(
                "Task {}: max_iterations ({}) reached; escalating",
                task.id, max_iter,
            )
            await self._mark_terminal(
                task, kind="escalate",
                reason="max_iterations_reached",
                escalate=True,
            )
            return

        await self._repo.update(
            task.id,
            iteration_count_delta=1,
            last_planning_started_at=datetime.now(timezone.utc),
        )

        # Re-fetch so iteration_count is fresh in the prompt.
        latest = await self._repo.get(task.id)
        if latest is None:
            return
        history = await self._repo.list_steps(latest.id)
        issue_summary = await self._load_issue_summary(latest)
        repo_workspace = await self._resolve_repo_workspace(latest)

        try:
            run = await self._agent.run(AgentRunInput(
                task=latest, history=history,
                issue_summary=issue_summary,
                repo_workspace=repo_workspace,
            ))
        except Exception:
            logger.exception("Task {}: agent crashed", latest.id)
            await self._repo.update(
                latest.id, clear_last_planning_started_at=True,
            )
            return
        self.stats.agent_runs += 1
        await self._record_run(latest, run)
        await self._apply_effects(latest, run)

    async def _record_run(
        self, task: ClarificationTask, run: AgentRunResult,
    ) -> None:
        """Append a step capturing the agent's run summary (so the next
        invocation's prompt-history shows what the agent did)."""
        # No effects → agent ran out of turns / didn't act.
        summary = (
            f"agent ran {run.turns} turn(s), stop={run.stopped_reason}, "
            f"effects={[e.kind for e in run.effects]}"
        )
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.PLANNER_DECIDED,
            text=summary,
            metadata={
                "stopped_reason": run.stopped_reason,
                "turns": run.turns,
                "cost_usd": run.cost_usd,
                "effects": [
                    {"kind": e.kind, "payload": e.payload}
                    for e in run.effects
                ],
            },
        )

    async def _apply_effects(
        self, task: ClarificationTask, run: AgentRunResult,
    ) -> None:
        # Find the (single) terminal-class effect, if any.
        for e in run.effects:
            if e.kind == "ask_dispatched":
                await self._on_ask(task, e)
                return
            if e.kind == "final_answer":
                await self._on_final_answer(task, e)
                return
            if e.kind == "escalate":
                await self._mark_terminal(
                    task, kind="escalate",
                    reason=e.payload.get("reason", "agent_escalate"),
                    escalate=True,
                )
                return
            if e.kind == "abandon":
                await self._mark_terminal(
                    task, kind="abandon",
                    reason=e.payload.get("reason", "agent_abandon"),
                    escalate=False,
                )
                return
        # No terminal effect — agent finished without acting. Either the
        # LLM hit max_turns or returned end_turn without using a tool.
        # Treat as escalation so the operator sees it instead of a
        # silent no-op.
        logger.warning(
            "Task {}: agent run ended without effect (stop={}, turns={})",
            task.id, run.stopped_reason, run.turns,
        )
        await self._mark_terminal(
            task, kind="escalate",
            reason=f"agent_no_action:{run.stopped_reason}",
            escalate=True,
        )

    async def _on_ask(
        self, task: ClarificationTask, effect: AgentEffect,
    ) -> None:
        p = effect.payload
        # The agent's MCP tool already performed the DM via
        # CommunicatorService; the orchestrator just records it as a
        # BOT_ASKED step and installs awaiting_*.
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.BOT_ASKED,
            text=p.get("asked_text") or "",
            metadata={
                "asked_post_id": p.get("asked_post_id"),
                "channel_id": p.get("channel_id"),
                "target_user_id": p.get("target_user_id"),
                "target_username": p.get("target_username"),
                "dedupe_key": p.get("dedupe_key"),
            },
        )
        await self._repo.update(
            task.id,
            info_source=(p.get("target_username") or p.get("target_email") or task.info_source),
            info_source_class="mattermost",
            awaiting_post_id=p.get("asked_post_id") or "",
            awaiting_user_id=p.get("target_user_id") or "",
            awaiting_username=p.get("target_username") or "",
            awaiting_channel_id=p.get("channel_id") or "",
            awaiting_dedupe_key=p.get("dedupe_key") or "",
            clear_last_planning_started_at=True,
        )
        self.stats.asks_dispatched += 1
        await self._emit("ask_dispatched", task, payload={
            "target_user_id": p.get("target_user_id"),
            "target_username": p.get("target_username"),
        })

    async def _on_final_answer(
        self, task: ClarificationTask, effect: AgentEffect,
    ) -> None:
        p = effect.payload
        final = str(p.get("final_answer") or "").strip()
        confidence = float(p.get("confidence") or 0.0)
        reasoning = str(p.get("reasoning") or "")
        await self._repo.update(
            task.id,
            is_solved=True,
            final_answer=final,
            confidence=confidence,
            clear_awaiting=True,
            closed=True,
            clear_last_planning_started_at=True,
        )
        await self._repo.append_step(
            task_id=task.id,
            kind=TaskStepKind.NOTE,
            text=(
                f"final_answer (confidence={confidence:.2f}): {final[:300]}"
                + (f"\nreasoning: {reasoning[:300]}" if reasoning else "")
            ),
            metadata={
                "final_answer": final,
                "confidence": confidence,
                "reasoning": reasoning,
            },
        )
        self.stats.final_answers += 1
        await self._emit("final_answer", task, payload={
            "final_answer": final,
            "confidence": confidence,
        })
        await self._maybe_resettle_plan(task)

    async def _mark_terminal(
        self,
        task: ClarificationTask,
        *,
        kind: str,                    # "escalate" | "abandon"
        reason: str,
        escalate: bool,
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
            text=f"Task {kind}d: {reason}",
            metadata={"reason": reason, "kind": kind},
        )
        if kind == "escalate":
            self.stats.escalations += 1
        else:
            self.stats.abandonments += 1
        if escalate:
            await self._send_lead_escalation(task, reason=reason)
        await self._emit(f"{kind}_terminal", task, payload={"reason": reason})
        await self._maybe_resettle_plan(task)

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
        self,
        anchor: ClarificationTask,
        siblings: list[ClarificationTask],
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
