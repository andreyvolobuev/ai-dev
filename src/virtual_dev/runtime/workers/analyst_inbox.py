"""AnalystInbox + AnalystOrchestrator (Phase 5.0).

Single driver for the analyst's per-ticket session. Responsibilities:

* Subscribe to ``task.discovered`` from the message bus and run the
  analyst on each new ticket.
* Persist the analyst's effects (ASK dispatch installs awaiting state
  on TaskRow; submit_plan finalises; stuck / blocked close).
* On a coalesced human reply (driven by AnswerCoalescerWorker) re-run
  the analyst with the conversation history rendered into the prompt.
* Sweep deadlines.

Replaces the multi-component pipeline of phases 3.x–4.x:
``AnalystInbox → spawn ClarificationTasks → ClarificationAgent →
TaskOrchestrator → analyst replan``. Now: ONE agent, ONE driver.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import AnalystAgent
from virtual_dev.application.agents.analyst import (
    AnalystEffect,
    AnalystRunInput,
    AnalystRunResult,
)
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    dev_agent_key,
)
from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.analyst_conversation import ConversationStepKind
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope


def _render_plan_comment(
    plan: Plan,
    template: str,
    dashboard_url: str | None = None,
) -> str:
    target_repo_block = (
        f"*Target repo:* {plan.target_repo_key}\n" if plan.target_repo_key else ""
    )
    steps_block = ""
    if plan.steps:
        steps_block = "\n*Steps*\n" + "\n".join(
            f"{step.order}. {step.summary}" for step in plan.steps
        )
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
            open_questions_block="",
            risks_block=risks_block,
            dashboard_block=dashboard_block,
        )
    except (KeyError, IndexError) as exc:
        logger.warning("AnalystInbox: plan template format failed: {}", exc)
        return template


@dataclass
class AnalystInboxStats:
    runs: int = 0
    plans_finalised: int = 0
    asks_dispatched: int = 0
    escalations: int = 0
    blocked_count: int = 0
    fragments_appended: int = 0
    deadlines_swept: int = 0


class AnalystInbox:
    """Drives the analyst across the lifetime of a ticket."""

    def __init__(
        self,
        *,
        analyst: AnalystAgent,
        session_repo: AnalystSessionRepository,
        communicator: CommunicatorService,
        task_tracker: TaskTrackerPort | None,
        config: AppConfig,
        message_bus: MessageBusPort | None = None,
        post_to_tracker: bool = True,
        dev_specialisation: str = "backend",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        trace: AgentTrace | None = None,
    ) -> None:
        self._analyst = analyst
        self._sessions = session_repo
        self._communicator = communicator
        self._task_tracker = task_tracker
        self._config = config
        self._message_bus = message_bus
        self._post_to_tracker = post_to_tracker
        self._dev_specialisation = dev_specialisation
        self._session_factory = session_factory
        self._trace = trace
        self.stats = AnalystInboxStats()

    # ---------------------------------------------------------------- entry
    async def handle(self, message: AgentMessage) -> None:
        """Triggered by ``task.discovered``."""
        tracker = str(message.payload.get("tracker") or "")
        external_id = str(message.payload.get("external_id") or "")
        if not tracker or not external_id:
            logger.warning("AnalystInbox: malformed payload {}", message.payload)
            return

        task_row = await self._analyst.load_task(tracker, external_id)
        if task_row is None:
            logger.warning(
                "AnalystInbox: task {}/{} not found", tracker, external_id,
            )
            return

        if await self._analyst.has_fresh_plan(task_row):
            logger.info(
                "AnalystInbox: task {} already has fresh plan; skipping",
                external_id,
            )
            return

        # Optimistic transition to In Progress. Done once per ticket.
        if (
            self._post_to_tracker
            and self._task_tracker is not None
            and task_row.internal_status == TaskStatus.DISCOVERED.value
        ):
            to_in_progress = self._config.agents.jira_transitions.to_in_progress
            try:
                await self._task_tracker.transition(external_id, to_in_progress)
            except Exception:
                logger.exception(
                    "AnalystInbox: failed to transition {} to {}",
                    external_id, to_in_progress,
                )

        await self._set_internal_status(task_row.id, TaskStatus.PLANNING)
        await self._run_step(task_row)

    # ---------------------------------------------------------------- chat fragments
    async def find_task_by_thread(
        self, asked_post_id: str,
    ) -> TaskRow | None:
        return await self._sessions.find_by_awaiting_post_id(asked_post_id)

    async def find_task_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> TaskRow | None:
        return await self._sessions.find_by_awaiting_channel(
            mm_channel_id, mm_user_id,
        )

    async def append_fragment(
        self, task_id: int, mm_post: ChatMessage,
    ) -> bool:
        ok = await self._sessions.append_fragment(
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
                agent_key="analyst",
                payload={
                    "task_id": task_id,
                    "post_id": mm_post.id,
                    "text": mm_post.text,
                },
            ))
        return ok

    # ---------------------------------------------------------------- worker ticks
    async def flush_idle(self) -> int:
        now = datetime.now(timezone.utc)
        idle = await self._sessions.find_idle_awaiting(now=now)
        flushed = 0
        for task_row in idle:
            try:
                await self._coalesce_and_resume(task_row)
                flushed += 1
            except Exception:
                logger.exception(
                    "AnalystInbox: coalesce+resume failed for task {}",
                    task_row.id,
                )
        return flushed

    async def sweep_deadlines(self) -> int:
        now = datetime.now(timezone.utc)
        overdue = await self._sessions.find_overdue(now=now)
        swept = 0
        for task_row in overdue:
            await self._terminate(
                task_row,
                kind="stuck",
                reason="deadline_exceeded",
            )
            swept += 1
            self.stats.deadlines_swept += 1
        return swept

    # ---------------------------------------------------------------- core
    async def _coalesce_and_resume(self, task_row: TaskRow) -> None:
        """Idle window passed → merge fragments, append HUMAN_REPLIED
        step, run analyst again."""
        fragments = await self._sessions.list_unflushed_fragments(task_row.id)
        if not fragments:
            return
        merged = "\n\n".join(
            f.text.strip() for f in fragments if f.text and f.text.strip()
        ) or fragments[0].text
        last_post_id = fragments[-1].mm_post_id

        await self._sessions.append_step(
            task_id=task_row.id,
            kind=ConversationStepKind.HUMAN_REPLIED,
            text=merged,
            metadata={
                "asked_post_id": task_row.awaiting_post_id,
                "fragment_count": len(fragments),
                "from_user_id": task_row.awaiting_user_id,
                "from_username": task_row.awaiting_username,
            },
        )
        await self._sessions.mark_fragments_flushed(task_row.id)
        if last_post_id:
            await self._communicator.add_reaction(last_post_id, "white_check_mark")
        await self._sessions.clear_awaiting(task_row.id)
        refreshed = await self._sessions.get_task(task_row.id)
        if refreshed is not None:
            await self._run_step(refreshed)

    async def _run_step(self, task_row: TaskRow) -> None:
        if task_row.internal_status in (
            TaskStatus.READY.value, TaskStatus.FAILED.value, TaskStatus.DONE.value,
        ):
            return

        max_iter = (
            self._config.agents.clarification.max_planner_calls_per_goal
            or 8
        )
        if (task_row.analyst_iteration_count or 0) >= max_iter:
            logger.warning(
                "AnalystInbox: task {} hit max iterations ({}); escalating",
                task_row.id, max_iter,
            )
            await self._terminate(
                task_row, kind="stuck",
                reason="max_iterations_reached",
            )
            return

        deadline_at = (
            task_row.analyst_deadline_at
            or datetime.now(timezone.utc) + timedelta(
                hours=self._config.agents.clarification.max_goal_age_hours,
            )
        )
        coalesce_window = self._config.agents.clarification.coalesce_window_seconds
        await self._sessions.increment_iteration(
            task_row.id,
            deadline_at=deadline_at,
            coalesce_window_seconds=coalesce_window,
        )
        # Refresh ``links_json`` from Jira before every analyst run.
        # ``fetch_tasks(jql)`` (the discovery sweep) only populates the
        # cheap inline links — remote links (Confluence "mentioned in"
        # back-references) come from a per-ticket REST endpoint and
        # aren't in that batch. Doing the refresh here means the prompt
        # the analyst sees always has the full link set, without making
        # discovery slower. Failure-tolerant: on a Jira hiccup we keep
        # whatever ``links_json`` is in the DB and log.
        if self._task_tracker is not None:
            try:
                fresh = await self._task_tracker.get_task(
                    task_row.external_id,
                )
                await self._sessions.update_links(
                    task_row.id,
                    [asdict(link) for link in fresh.links],
                )
            except Exception:
                logger.exception(
                    "AnalystInbox: link refresh failed for {}; "
                    "continuing with stale links_json",
                    task_row.external_id,
                )
        refreshed = await self._sessions.get_task(task_row.id)
        if refreshed is None:
            return

        history = await self._sessions.list_steps(refreshed.id)
        target_repo = self._guess_target_repo(refreshed)
        repo_workspace = self._resolve_repo_workspace(refreshed, target_repo)

        try:
            run = await self._analyst.run(AnalystRunInput(
                task_row=refreshed, history=history,
                target_repo=target_repo,
                repo_workspace=repo_workspace,
            ))
        except Exception:
            logger.exception(
                "AnalystInbox: analyst run crashed for task {}", refreshed.id,
            )
            await self._sessions.clear_started_at(refreshed.id)
            return

        self.stats.runs += 1
        await self._record_run(refreshed, run)
        await self._apply_effects(refreshed, run)

    async def _record_run(
        self, task_row: TaskRow, run: AnalystRunResult,
    ) -> None:
        summary = (
            f"analyst run {run.turns} turn(s), stop={run.stopped_reason}, "
            f"effects={[e.kind for e in run.effects]}"
        )
        await self._sessions.append_step(
            task_id=task_row.id,
            kind=ConversationStepKind.PLANNER_DECIDED,
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
        self, task_row: TaskRow, run: AnalystRunResult,
    ) -> None:
        for e in run.effects:
            if e.kind == "ask_dispatched":
                await self._on_ask(task_row, e)
                return
            if e.kind == "plan_submitted":
                await self._on_plan(task_row, run, e)
                return
            if e.kind == "stuck":
                await self._terminate(
                    task_row, kind="stuck",
                    reason=e.payload.get("reason", "agent_stuck"),
                )
                return
            if e.kind == "blocked":
                await self._terminate(
                    task_row, kind="blocked",
                    reason=e.payload.get("reason", "agent_blocked"),
                )
                return
        # Silent run — page the lead so we don't dead-air.
        logger.warning(
            "AnalystInbox: task {} run ended without effect (stop={})",
            task_row.id, run.stopped_reason,
        )
        await self._terminate(
            task_row, kind="stuck",
            reason=f"agent_no_action:{run.stopped_reason}",
        )

    async def _on_ask(
        self, task_row: TaskRow, effect: AnalystEffect,
    ) -> None:
        p = effect.payload
        await self._sessions.append_step(
            task_id=task_row.id,
            kind=ConversationStepKind.BOT_ASKED,
            text=p.get("asked_text") or "",
            metadata={
                "asked_post_id": p.get("asked_post_id"),
                "channel_id": p.get("channel_id"),
                "target_user_id": p.get("target_user_id"),
                "target_username": p.get("target_username"),
                "dedupe_key": p.get("dedupe_key"),
            },
        )
        await self._sessions.install_awaiting(
            task_row.id,
            post_id=p.get("asked_post_id") or "",
            user_id=p.get("target_user_id") or "",
            username=p.get("target_username"),
            channel_id=p.get("channel_id") or "",
            dedupe_key=p.get("dedupe_key"),
        )
        await self._sessions.clear_started_at(task_row.id)
        self.stats.asks_dispatched += 1
        await emit_if(self._trace, AgentTraceEvent(
            type="task_event", agent_key="analyst",
            payload={
                "task_id": task_row.id, "action": "ask_dispatched",
                "target_user_id": p.get("target_user_id"),
                "target_username": p.get("target_username"),
            },
        ))

    async def _on_plan(
        self, task_row: TaskRow, run: AnalystRunResult, effect: AnalystEffect,
    ) -> None:
        plan = run.plan
        if plan is None:
            logger.warning(
                "AnalystInbox: plan_submitted effect but no plan parsed for task {}",
                task_row.id,
            )
            await self._terminate(
                task_row, kind="stuck",
                reason="plan_submitted_but_unparseable",
            )
            return

        if plan.status == PlanStatus.FAILED:
            await self._set_internal_status(task_row.id, TaskStatus.FAILED)
            await self._sessions.clear_awaiting(task_row.id)
            await self._sessions.clear_started_at(task_row.id)
            return

        # Always treat submit_plan as READY — the new prompt forbids
        # the clarifying status. Defensive: if a stale prompt sneaks
        # through, log + treat as ready.
        if plan.status != PlanStatus.READY:
            logger.warning(
                "AnalystInbox: task {} submitted with status={}; coercing to READY",
                task_row.id, plan.status.value,
            )
            plan.status = PlanStatus.READY

        await self._analyst.save_plan(plan)
        await self._set_internal_status(task_row.id, TaskStatus.READY)
        await self._sessions.clear_awaiting(task_row.id)
        await self._sessions.clear_started_at(task_row.id)
        self.stats.plans_finalised += 1

        if self._post_to_tracker and self._task_tracker is not None:
            body = _render_plan_comment(
                plan, self._config.notifications.jira.plan_comment,
            )
            try:
                await self._task_tracker.comment(task_row.external_id, body)
            except Exception:
                logger.exception(
                    "AnalystInbox: failed to comment plan on {}", task_row.external_id,
                )

        if (
            self._message_bus is not None
            and plan.target_repo_key
        ):
            await self._message_bus.publish(AgentMessage(
                id=uuid.uuid4().hex,
                from_agent="analyst",
                to_agent=dev_agent_key(plan.target_repo_key, self._dev_specialisation),
                topic=TOPIC_PLAN_READY,
                payload={
                    "tracker": task_row.tracker,
                    "external_id": task_row.external_id,
                    "repo_key": plan.target_repo_key,
                },
            ))

        await emit_if(self._trace, AgentTraceEvent(
            type="task_event", agent_key="analyst",
            payload={
                "task_id": task_row.id, "action": "plan_finalised",
                "summary": plan.summary[:200],
                "target_repo_key": plan.target_repo_key,
            },
        ))

    async def _terminate(
        self,
        task_row: TaskRow,
        *,
        kind: str,
        reason: str,
    ) -> None:
        """Close the analyst session for this ticket.

        Two flavours, both DM the team-lead with the chain:

        * ``stuck`` — agent ran out of angles, lead help needed. Jira
          stays In Progress.
        * ``blocked`` — ticket is blocked / unworkable. Orchestrator
          transitions Jira to "Waiting For Response", posts a comment
          explaining why, then DMs the lead.
        """
        await self._set_internal_status(task_row.id, TaskStatus.FAILED)
        await self._sessions.clear_awaiting(task_row.id)
        await self._sessions.clear_started_at(task_row.id)
        await self._sessions.append_step(
            task_id=task_row.id,
            kind=ConversationStepKind.NOTE,
            text=f"Task {kind}: {reason}",
            metadata={"reason": reason, "kind": kind},
        )
        if kind == "blocked":
            self.stats.blocked_count += 1
            await self._jira_blocked_actions(task_row, reason=reason)
        else:
            self.stats.escalations += 1
        await self._send_lead_escalation(
            task_row, reason=reason, kind=kind,
        )
        await emit_if(self._trace, AgentTraceEvent(
            type="task_event", agent_key="analyst",
            payload={
                "task_id": task_row.id,
                "action": f"{kind}_terminal",
                "reason": reason,
            },
        ))

    async def _jira_blocked_actions(
        self, task_row: TaskRow, *, reason: str,
    ) -> None:
        """Transition Jira to Pending + post explanatory comment.

        Both sub-actions are best-effort — if Jira is unreachable or the
        workflow has no matching transition, we still want to DM the
        lead so the block doesn't go silent. A trace event is always
        emitted so the operator can see the intent even when dispatch
        is disabled (e.g. the test-analyst session wires the inbox
        with ``post_to_tracker=False`` to avoid touching real Jira).
        """
        external_id = task_row.external_id
        to_pending = self._config.agents.jira_transitions.to_pending
        comment_template = (
            self._config.notifications.jira.blocked_comment
            or
            "*[virtual-dev] Задача переведена в \"{status}\".*\n\n"
            "**Причина:** {reason}"
        )
        try:
            comment_body = comment_template.format(
                external_id=external_id,
                task_url=task_row.url or "",
                reason=reason,
                status=to_pending,
            )
        except (KeyError, IndexError) as exc:
            logger.warning(
                "AnalystInbox: blocked_comment template format failed: {}",
                exc,
            )
            comment_body = comment_template

        if not self._post_to_tracker or self._task_tracker is None:
            await emit_if(self._trace, AgentTraceEvent(
                type="task_event", agent_key="analyst",
                payload={
                    "task_id": task_row.id,
                    "action": "blocked_jira_actions_skipped",
                    "external_id": external_id,
                    "intended_transition": to_pending,
                    "intended_comment": comment_body,
                    "skip_reason": "post_to_tracker_disabled",
                },
            ))
            return

        transitioned = False
        try:
            await self._task_tracker.transition(external_id, to_pending)
            transitioned = True
        except Exception:
            logger.exception(
                "AnalystInbox: failed to transition {} to {} on block",
                external_id, to_pending,
            )

        commented = False
        try:
            await self._task_tracker.comment(external_id, comment_body)
            commented = True
        except Exception:
            logger.exception(
                "AnalystInbox: failed to post blocked comment on {}",
                external_id,
            )

        await emit_if(self._trace, AgentTraceEvent(
            type="task_event", agent_key="analyst",
            payload={
                "task_id": task_row.id,
                "action": "blocked_jira_actions",
                "external_id": external_id,
                "to_status": to_pending,
                "transitioned": transitioned,
                "commented": commented,
                "comment_body": comment_body,
            },
        ))

    async def _send_lead_escalation(
        self, task_row: TaskRow, *, reason: str, kind: str,
    ) -> None:
        lead = await self._lead_user_id()
        steps = await self._sessions.list_steps(task_row.id)
        chain = "\n".join(
            f"- [{s.seq}] {s.kind.value}: «{(s.text or '').splitlines()[0][:160] if s.text else ''}»"
            for s in steps
        ) or "(no steps)"
        mm = self._config.notifications.mattermost
        if kind == "blocked":
            body_template = (
                mm.blocked_escalation_to_lead
                or
                "Перевел в Pending задачу [{external_id}]({task_url})\n\n"
                "**Причина:** {reason}\n\n"
                "**Цепочка шагов:**\n{chain_summary}"
            )
        else:
            body_template = (
                mm.stuck_escalation_to_lead
                or
                "Застрял с уточнением по тикету [{external_id}]({task_url}).\n\n"
                "**Причина:** {reason}\n\n**Цель:** {original_question}\n\n"
                "**Цепочка:**\n{chain_summary}"
            )
        body = body_template.format(
            tracker=task_row.tracker,
            external_id=task_row.external_id,
            task_url=task_row.url or "",
            original_question=(task_row.title or "")[:300],
            chain_summary=chain,
            reason=reason,
        )
        if lead is None:
            handle = (self._config.agents.escalation.mattermost_user or "").strip()
            if not handle or handle == "your.name":
                note = "No team-lead handle configured."
            else:
                note = (
                    f"Configured handle {handle!r} did not resolve to a "
                    f"chat user — check ESCALATION_USER / chat directory."
                )
            await emit_if(self._trace, AgentTraceEvent(
                type="escalation_dropped", agent_key="analyst",
                payload={
                    "task_id": task_row.id, "reason": reason, "kind": kind,
                    "configured_handle": handle,
                    "body": body,
                    "note": note,
                },
            ))
            return
        outcome = await self._communicator.send_dm(lead, body)
        await emit_if(self._trace, AgentTraceEvent(
            type="escalation_sent" if outcome.sent else "escalation_dropped",
            agent_key="analyst",
            payload={
                "task_id": task_row.id, "reason": reason, "kind": kind,
                "lead_user_id": lead,
                "sent": outcome.sent,
                "skip_reason": outcome.skip_reason,
                "body": body,
            },
        ))

    # ---------------------------------------------------------------- helpers
    async def _set_internal_status(
        self, task_row_id: int, status: TaskStatus,
    ) -> None:
        if self._session_factory is None:
            return
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_row_id)
            )).scalar_one_or_none()
            if row is not None:
                row.internal_status = status.value

    async def _lead_user_id(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    def _guess_target_repo(self, task_row: TaskRow) -> str | None:
        mapping: Mapping[str, str] = self._config.mappings.component_to_repo
        for component in task_row.components_json or []:
            if component in mapping:
                return mapping[component]
        if task_row.target_repo_key:
            return task_row.target_repo_key
        if len(self._config.repositories) == 1:
            return self._config.repositories[0].key
        return None

    def _resolve_repo_workspace(
        self, task_row: TaskRow, target_repo: str | None,
    ) -> Path | None:
        if target_repo is None:
            return None
        repo_cfg = self._config.get_repository(target_repo)
        if repo_cfg is None:
            return None
        if repo_cfg.local_path:
            return Path(repo_cfg.local_path).expanduser()
        return None


__all__ = ["AnalystInbox", "AnalystInboxStats"]
