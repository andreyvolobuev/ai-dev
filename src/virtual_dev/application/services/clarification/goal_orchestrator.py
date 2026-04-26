"""GoalOrchestrator — owns the state machine for ClarificationGoals.

Responsibilities:

* ``request_clarifications(plan)`` — turn each open question into a
  Goal (deduped by description against active goals on this plan)
  and run the planner once.
* ``append_fragment(goal_id, mm_post)`` — buffer an incoming MM
  message under the goal's current outstanding question.
* ``flush_idle()`` — coalescer tick: pick goals whose idle window has
  elapsed, atomically claim them (COALESCING/READY_TO_REPLAN →
  REPLANNING), invoke the planner with the merged answer.
* ``sweep_deadlines()`` — second tick of the same worker:
  * deadline-overdue → ABANDONED + DM lead
  * stuck REPLANNING (planner crashed) → revert to READY_TO_REPLAN
  * WAITING with ``next_planner_run_at`` past → READY_TO_REPLAN
  * SEND_PENDING → retry the DM
* ``apply_decision(...)`` — the ASK/ACHIEVE/ESCALATE/ABANDON/WAIT
  switchboard. Called from ``flush_idle`` and from
  ``request_clarifications``.

Replaces :class:`ClarificationOrchestrator` (Q-tree). The new state
machine is documented inline on
:class:`virtual_dev.domain.models.clarification_goal.GoalState`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.clarification_planner import (
    ClarificationPlanner,
    PlannerInput,
)
from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.clarification.goal_repo import GoalRepository
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.clarification_goal import (
    ACTIVE_STATES,
    ClarificationGoal,
    GoalState,
    GoalStepKind,
    PlannerActionKind,
    PlannerDecision,
)
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass
class OrchestratorStats:
    goals_created: int = 0
    fragments_appended: int = 0
    planner_calls: int = 0
    asks_dispatched: int = 0
    achievements: int = 0
    escalations: int = 0
    abandonments: int = 0
    waits: int = 0
    deadlines_swept: int = 0
    re_dispatches: int = 0


class GoalOrchestrator:
    """Single instance per process. Drives all clarification goals."""

    def __init__(
        self,
        *,
        repo: GoalRepository,
        communicator: CommunicatorService,
        planner: ClarificationPlanner,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        message_bus: MessageBusPort | None,
        trace: AgentTrace | None = None,
    ) -> None:
        self._repo = repo
        self._communicator = communicator
        self._planner = planner
        self._config = config
        self._session_factory = session_factory
        self._message_bus = message_bus
        self._trace = trace
        self.stats = OrchestratorStats()

    # ---------------------------------------------------------------- entry: kick off

    async def request_clarifications(
        self,
        *,
        task_row: TaskRow,
        plan: Plan,
        plan_row_id: int,
    ) -> int:
        """Create one ClarificationGoal per open_question, dedup against
        active goals on the same plan.
        """
        if not plan.open_questions:
            return 0

        existing_descriptions = await self._repo.existing_descriptions_for_plan(plan_row_id)
        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_goal_age_hours,
        )

        created = 0
        for oq in plan.open_questions:
            if oq.question in existing_descriptions:
                continue
            goal = await self._repo.create_goal(
                plan_id=plan_row_id,
                tracker=task_row.tracker,
                task_external_id=task_row.external_id,
                description=oq.question,
                why_it_matters=oq.why_it_matters or "",
                initial_contact_hint=(oq.ask_whom or "").strip(),
                coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
                deadline_at=deadline,
            )
            self.stats.goals_created += 1
            created += 1
            await self._emit("goal_created", goal)
            # Run planner immediately for the first decision.
            await self._invoke_planner(goal)
        return created

    # ---------------------------------------------------------------- entry: incoming MM event

    async def find_goal_by_thread(
        self, asked_post_id: str,
    ) -> ClarificationGoal | None:
        return await self._repo.find_active_by_thread(asked_post_id)

    async def find_goal_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> ClarificationGoal | None:
        return await self._repo.find_active_by_channel(mm_channel_id, mm_user_id)

    async def append_fragment(
        self, goal_id: int, mm_post: ChatMessage,
    ) -> bool:
        ok = await self._repo.append_fragment(
            goal_id=goal_id,
            mm_post_id=mm_post.id,
            asked_post_id=mm_post.thread_root_id,
            text=mm_post.text,
            received_at=mm_post.timestamp,
        )
        if ok:
            self.stats.fragments_appended += 1
            logger.info(
                "Goal {}: appended fragment (post {})", goal_id, mm_post.id,
            )
            await emit_if(self._trace, AgentTraceEvent(
                type="goal_fragment",
                agent_key="clarification",
                payload={
                    "goal_id": goal_id,
                    "post_id": mm_post.id,
                    "text": mm_post.text,
                },
            ))
        return ok

    # ---------------------------------------------------------------- worker ticks

    async def flush_idle(self) -> int:
        """Pick goals whose idle window passed, atomically claim and
        invoke the planner.
        """
        now = datetime.now(timezone.utc)
        idle = await self._repo.find_idle_coalescing(now=now)
        if not idle:
            return 0
        flushed = 0
        for goal in idle:
            claimed = await self._repo.claim_for_replan(goal.id)
            if claimed is None:
                continue
            try:
                await self._replan_after_reply(claimed)
                flushed += 1
            except Exception:
                logger.exception(
                    "Goal {}: replan failed; reverting to READY_TO_REPLAN",
                    goal.id,
                )
                await self._repo.update_state(
                    goal.id, GoalState.READY_TO_REPLAN,
                    clear_planning_started=True,
                )
        return flushed

    async def sweep_deadlines(self) -> int:
        """Run all the periodic sweeps:

        * stuck REPLANNING (planner crashed) → revert
        * WAITING due → revert
        * SEND_PENDING → retry the DM
        * BLOCKED_ON_SUBGOAL with all children terminal → unblock
          (covers the case where the parent crashed mid-unblock)
        * deadline overdue → ABANDONED + DM lead
        """
        now = datetime.now(timezone.utc)
        await self._recover_stuck_replanning(now=now)
        await self._wake_due_waiting(now=now)
        await self._retry_send_pending()
        await self._unblock_settled_parents()
        return await self._sweep_deadlines(now=now)

    # ---------------------------------------------------------------- internals

    async def _recover_stuck_replanning(self, *, now: datetime) -> None:
        cutoff = now - timedelta(
            minutes=self._config.agents.clarification.replanning_stuck_after_minutes,
        )
        stuck = await self._repo.find_stuck_replanning(older_than=cutoff)
        for goal in stuck:
            logger.warning(
                "Goal {}: REPLANNING stuck since {}; reverting to READY_TO_REPLAN",
                goal.id, goal.last_fragment_at,
            )
            await self._repo.update_state(
                goal.id, GoalState.READY_TO_REPLAN,
                clear_planning_started=True,
            )

    async def _wake_due_waiting(self, *, now: datetime) -> None:
        for goal in await self._repo.find_due_waiting(now=now):
            logger.info(
                "Goal {}: wait_for_human elapsed; replanning",
                goal.id,
            )
            await self._repo.update_state(
                goal.id, GoalState.READY_TO_REPLAN,
                clear_next_planner_run_at=True,
            )
            # Immediate replan in same tick.
            claimed = await self._repo.claim_for_replan(goal.id)
            if claimed is not None:
                try:
                    await self._replan_after_reply(claimed)
                except Exception:
                    logger.exception(
                        "Goal {}: wait-replan failed", goal.id,
                    )
                    await self._repo.update_state(
                        goal.id, GoalState.READY_TO_REPLAN,
                        clear_planning_started=True,
                    )

    async def _retry_send_pending(self) -> None:
        for goal in await self._repo.find_pending_send():
            await self._retry_outstanding_send(goal)

    async def _unblock_settled_parents(self) -> None:
        """Recovery: a parent crashed before noticing its last child
        terminated. Walk BLOCKED_ON_SUBGOAL goals; if every child is
        terminal, unblock the parent."""
        for parent in await self._repo.find_blocked_with_all_subgoals_terminal():
            await self._maybe_unblock_parent(parent.id)

    async def _sweep_deadlines(self, *, now: datetime) -> int:
        overdue = await self._repo.find_overdue(now=now)
        if not overdue:
            return 0
        swept = 0
        for goal in overdue:
            # Race-fix: re-check that no fragment landed since we
            # selected. A reply right at the deadline shouldn't lose.
            if not await self._repo.race_safe_check_no_new_fragment(
                goal.id, since=now - timedelta(seconds=5),
            ):
                continue
            await self._abandon(goal, reason="deadline_exceeded", escalate=True)
            self.stats.deadlines_swept += 1
            swept += 1
        return swept

    async def _replan_after_reply(self, goal: ClarificationGoal) -> None:
        """Goal is in REPLANNING with last_planning_started_at set.
        Pull buffered fragments, append HUMAN_REPLIED step, invoke
        planner with merged answer, then ``apply_decision``.
        """
        fragments = await self._repo.list_unflushed_fragments(goal.id)
        if not fragments:
            # Race / spurious wake — revert.
            await self._repo.update_state(
                goal.id, GoalState.READY_TO_REPLAN,
                clear_planning_started=True,
            )
            return

        # Coalesce.
        merged = "\n\n".join(
            f.text.strip() for f in fragments if f.text and f.text.strip()
        ) or fragments[0].text
        # Use the LAST fragment's mm_post_id for the ✅-reaction.
        last_post_id = fragments[-1].mm_post_id

        # Append HUMAN_REPLIED step. Mark fragments flushed.
        await self._repo.append_step(
            goal_id=goal.id,
            kind=GoalStepKind.HUMAN_REPLIED,
            text=merged,
            target_username=goal.current_target_username,
            target_user_id=goal.current_target_user_id,
            metadata={
                "asked_post_id": goal.current_asked_post_id,
                "fragment_count": len(fragments),
            },
        )
        await self._repo.mark_fragments_flushed(goal.id)

        # ✅-reaction on the last fragment — single visible signal that
        # we read the merged reply.
        if last_post_id:
            await self._communicator.add_reaction(last_post_id, "white_check_mark")

        # Invoke planner.
        await self._invoke_planner(
            goal, latest_fragments=[merged],
        )

    async def _invoke_planner(
        self,
        goal: ClarificationGoal,
        *,
        latest_fragments: list[str] | None = None,
    ) -> None:
        # Circuit breaker.
        if goal.planner_calls_count >= self._config.agents.clarification.max_planner_calls_per_goal:
            logger.warning(
                "Goal {}: max_planner_calls_per_goal reached; escalating",
                goal.id,
            )
            await self._escalate(goal, reason="max_planner_calls")
            return

        # Mark PLANNING (or REPLANNING if we already moved). Always
        # bump counter at this point.
        await self._repo.update_state(
            goal.id, GoalState.REPLANNING if goal.state != GoalState.PENDING else GoalState.PLANNING,
            increment_planner_calls=True,
        )

        # Build planner input.
        history = await self._repo.list_steps(goal.id)
        issue_summary = await self._load_issue_summary(goal)
        repo_workspace = await self._resolve_repo_workspace(goal)

        try:
            decision = await self._planner.decide(PlannerInput(
                goal=goal,
                history=history,
                latest_fragments=latest_fragments or [],
                issue_summary=issue_summary,
                repo_workspace=repo_workspace,
            ))
        except Exception:
            logger.exception("Goal {}: planner crashed", goal.id)
            # Soft revert to READY_TO_REPLAN; sweeper will re-run.
            await self._repo.update_state(
                goal.id, GoalState.READY_TO_REPLAN,
                clear_planning_started=True,
            )
            return

        self.stats.planner_calls += 1

        # Audit-log the decision.
        await self._repo.append_step(
            goal_id=goal.id,
            kind=GoalStepKind.PLANNER_DECIDED,
            text=decision.reasoning,
            metadata={
                "action": decision.action.value,
                "to_handle": decision.to_handle,
                "to_email": decision.to_email,
                "dedupe_key": decision.dedupe_key,
                "final_answer": decision.final_answer,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "note": decision.note,
                "retry_after_minutes": decision.retry_after_minutes,
                "cost_usd": decision.cost_usd,
            },
        )
        await self._emit("planner_decided", goal, payload={
            "action": decision.action.value,
            "reasoning": decision.reasoning,
            "to_handle": decision.to_handle,
            "to_email": decision.to_email,
            "message": decision.message,
            "final_answer": decision.final_answer,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "note": decision.note,
            "retry_after_minutes": decision.retry_after_minutes,
        })

        await self._apply_decision(goal, decision)

    # ---------------------------------------------------------------- apply

    async def _apply_decision(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> None:
        if decision.action == PlannerActionKind.ASK:
            await self._on_ask(goal, decision)
        elif decision.action == PlannerActionKind.ACHIEVE:
            await self._on_achieve(goal, decision)
        elif decision.action == PlannerActionKind.ESCALATE_TO_LEAD:
            await self._escalate(goal, reason=decision.reason or "planner_escalation")
        elif decision.action == PlannerActionKind.ABANDON:
            await self._abandon(goal, reason=decision.reason or "planner_abandon", escalate=False)
        elif decision.action == PlannerActionKind.WAIT_FOR_HUMAN:
            await self._on_wait(goal, decision)
        elif decision.action == PlannerActionKind.SPAWN_SUBGOALS:
            await self._on_spawn_subgoals(goal, decision)
        else:
            logger.warning(
                "Goal {}: unknown action {}; escalating", goal.id, decision.action,
            )
            await self._escalate(goal, reason="unknown_action")

    async def _on_ask(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> None:
        # Validate the planner's output.
        message = (decision.message or "").strip()
        if not message:
            await self._escalate(goal, reason="planner_ask_no_message")
            return
        # no_duplicate_target guard.
        if await self._is_duplicate_ask(goal, decision):
            logger.warning(
                "Goal {}: duplicate ask to {}/{}; escalating",
                goal.id, decision.to_handle, decision.dedupe_key,
            )
            await self._escalate(goal, reason="duplicate_ask")
            return

        # Resolve target user.
        target_user_id, target_username = await self._resolve_target(decision)
        if target_user_id is None:
            # Planner asked someone the bot can't find. Escalate
            # rather than silently re-routing.
            await self._escalate(goal, reason="ask_target_unresolved")
            return

        # Archive any pending fragments from the previous question —
        # they belonged to the previous recipient and aren't evidence
        # for the new ask.
        await self._repo.archive_unflushed_as_stale(goal.id)

        # Send DM via Communicator.
        try:
            outcome = await self._communicator.send_dm(target_user_id, message)
        except Exception:
            logger.exception("Goal {}: send_dm crashed", goal.id)
            await self._enter_send_pending(goal, decision, target_user_id, target_username)
            return

        if not outcome.sent or outcome.message is None:
            logger.warning(
                "Goal {}: send_dm not sent ({}); SEND_PENDING",
                goal.id, outcome.skip_reason,
            )
            await self._enter_send_pending(goal, decision, target_user_id, target_username)
            return

        # DM landed. Record BOT_ASKED step and flip state.
        await self._repo.append_step(
            goal_id=goal.id,
            kind=GoalStepKind.BOT_ASKED,
            text=message,
            target_username=target_username,
            target_user_id=target_user_id,
            metadata={
                "asked_post_id": outcome.message.id,
                "channel_id": outcome.message.channel_id,
                "dedupe_key": decision.dedupe_key,
            },
        )
        await self._repo.set_outstanding(
            goal.id,
            target_user_id=target_user_id,
            target_username=target_username,
            channel_id=outcome.message.channel_id,
            asked_post_id=outcome.message.id,
            asked_text=message,
            dedupe_key=decision.dedupe_key,
            new_state=GoalState.AWAITING_REPLY,
        )
        self.stats.asks_dispatched += 1
        logger.info(
            "Goal {}: dispatched ask to {} (post {})",
            goal.id, target_user_id, outcome.message.id,
        )

    async def _enter_send_pending(
        self,
        goal: ClarificationGoal,
        decision: PlannerDecision,
        target_user_id: str,
        target_username: str | None,
    ) -> None:
        """Communicator refused — increment retry counter, possibly
        give up if we've retried too many times.
        """
        if goal.send_retry_count + 1 >= self._config.agents.clarification.send_retry_max:
            logger.warning(
                "Goal {}: send_retry_max reached; abandoning",
                goal.id,
            )
            await self._abandon(
                goal, reason="send_retry_max_exceeded", escalate=True,
            )
            return
        # Stash decision payload in current_asked_text so the retry
        # tick has the message to retry with.
        await self._repo.update_state(
            goal.id, GoalState.SEND_PENDING,
            outstanding_user_id=target_user_id,
            outstanding_username=target_username,
            outstanding_text=(decision.message or ""),
            outstanding_dedupe_key=decision.dedupe_key,
            increment_send_retry=True,
            clear_planning_started=True,
        )

    async def _retry_outstanding_send(self, goal: ClarificationGoal) -> None:
        if not goal.current_target_user_id or not goal.current_asked_text:
            await self._abandon(goal, reason="send_pending_no_target", escalate=True)
            return
        try:
            outcome = await self._communicator.send_dm(
                goal.current_target_user_id, goal.current_asked_text,
            )
        except Exception:
            logger.exception("Goal {}: retry send_dm crashed", goal.id)
            return
        if not outcome.sent or outcome.message is None:
            # Still not sent — increment retry, possibly abandon.
            if goal.send_retry_count + 1 >= self._config.agents.clarification.send_retry_max:
                await self._abandon(
                    goal, reason="send_retry_max_exceeded", escalate=True,
                )
                return
            await self._repo.update_state(
                goal.id, GoalState.SEND_PENDING,
                increment_send_retry=True,
            )
            return

        # Now it sent.
        await self._repo.append_step(
            goal_id=goal.id,
            kind=GoalStepKind.BOT_ASKED,
            text=goal.current_asked_text,
            target_username=goal.current_target_username,
            target_user_id=goal.current_target_user_id,
            metadata={
                "asked_post_id": outcome.message.id,
                "channel_id": outcome.message.channel_id,
                "dedupe_key": goal.current_dedupe_key,
                "retried": True,
            },
        )
        await self._repo.set_outstanding(
            goal.id,
            target_user_id=goal.current_target_user_id,
            target_username=goal.current_target_username,
            channel_id=outcome.message.channel_id,
            asked_post_id=outcome.message.id,
            asked_text=goal.current_asked_text,
            dedupe_key=goal.current_dedupe_key,
            new_state=GoalState.AWAITING_REPLY,
        )
        self.stats.asks_dispatched += 1

    async def _on_achieve(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> None:
        final = (decision.final_answer or "").strip()
        if not final:
            await self._escalate(goal, reason="achieve_no_final_answer")
            return
        await self._repo.update_state(
            goal.id, GoalState.ACHIEVED,
            final_answer=final,
            clear_outstanding=True,
            closed=True,
        )
        self.stats.achievements += 1
        logger.info(
            "Goal {}: ACHIEVED (confidence={:.2f}): {}",
            goal.id, decision.confidence, final[:160],
        )
        await self._on_terminal(goal, GoalStepKind.SUBGOAL_ACHIEVED, final)

    async def _on_wait(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> None:
        retry_min = decision.retry_after_minutes or 60
        next_at = datetime.now(timezone.utc) + timedelta(minutes=retry_min)
        await self._repo.update_state(
            goal.id, GoalState.WAITING,
            next_planner_run_at=next_at,
            clear_planning_started=True,
        )
        self.stats.waits += 1
        logger.info(
            "Goal {}: WAITING for {} minutes (note: {})",
            goal.id, retry_min, decision.note[:120],
        )

    async def _on_spawn_subgoals(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> None:
        """Create child goals, append SUBGOAL_SPAWNED to the parent's
        history, transition parent to BLOCKED_ON_SUBGOAL, run the
        planner on each new child immediately.
        """
        max_depth = self._config.agents.clarification.max_subgoal_depth
        if goal.depth >= max_depth:
            logger.warning(
                "Goal {}: subgoal depth limit ({}) reached; escalating",
                goal.id, max_depth,
            )
            await self._escalate(goal, reason="max_subgoal_depth")
            return

        specs = [s for s in decision.subgoals if s.description.strip()]
        if not specs:
            await self._escalate(goal, reason="spawn_subgoals_empty")
            return

        clar_cfg = self._config.agents.clarification
        # Children inherit the parent's deadline (no later than parent's)
        # so a stuck child can't outlive its blocking parent.
        deadline = goal.deadline_at or (
            datetime.now(timezone.utc) + timedelta(hours=clar_cfg.max_goal_age_hours)
        )

        children: list[ClarificationGoal] = []
        for spec in specs:
            child = await self._repo.create_goal(
                plan_id=goal.plan_id,
                tracker=goal.tracker,
                task_external_id=goal.task_external_id,
                description=spec.description.strip(),
                why_it_matters=spec.why_it_matters.strip(),
                initial_contact_hint=spec.initial_contact_hint.strip(),
                coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
                deadline_at=deadline,
                parent_goal_id=goal.id,
                depth=goal.depth + 1,
            )
            await self._repo.append_step(
                goal_id=goal.id,
                kind=GoalStepKind.SUBGOAL_SPAWNED,
                text=spec.description.strip(),
                metadata={
                    "subgoal_id": child.id,
                    "why_it_matters": spec.why_it_matters,
                    "initial_contact_hint": spec.initial_contact_hint,
                },
            )
            children.append(child)
            await self._emit("subgoal_spawned", goal, payload={
                "subgoal_id": child.id,
                "subgoal_description": spec.description,
            })

        # Block parent until every child is terminal.
        await self._repo.update_state(
            goal.id, GoalState.BLOCKED_ON_SUBGOAL,
            clear_outstanding=True,
            clear_planning_started=True,
        )
        logger.info(
            "Goal {}: spawned {} subgoal(s) → BLOCKED_ON_SUBGOAL",
            goal.id, len(children),
        )

        # Kick off each child's planner. If a child achieves immediately
        # (synchronous self-research), the unblock check runs on the
        # last child in this loop too.
        for child in children:
            await self._invoke_planner(child)

    async def _abandon(
        self,
        goal: ClarificationGoal,
        *,
        reason: str,
        escalate: bool,
    ) -> None:
        await self._repo.update_state(
            goal.id, GoalState.ABANDONED,
            clear_outstanding=True,
            closed=True,
        )
        self.stats.abandonments += 1
        logger.info("Goal {}: ABANDONED ({})", goal.id, reason)
        if escalate:
            await self._send_lead_escalation(goal, reason=reason)
        await self._on_terminal(goal, GoalStepKind.SUBGOAL_ABANDONED, reason)

    async def _escalate(
        self, goal: ClarificationGoal, *, reason: str,
    ) -> None:
        await self._repo.update_state(
            goal.id, GoalState.ESCALATED,
            clear_outstanding=True,
            closed=True,
        )
        self.stats.escalations += 1
        await self._send_lead_escalation(goal, reason=reason)
        logger.info("Goal {}: ESCALATED ({})", goal.id, reason)
        await self._on_terminal(goal, GoalStepKind.SUBGOAL_ESCALATED, reason)

    async def _on_terminal(
        self,
        goal: ClarificationGoal,
        parent_kind: GoalStepKind,
        summary: str,
    ) -> None:
        """Goal just hit a terminal state. If it has a parent, fold
        the result into the parent's history and try to unblock the
        parent. Then run the plan-level resettle check (top-level only).
        """
        # Re-fetch so we record the *post-update* state in the parent's
        # SUBGOAL_* metadata. The ``goal`` arg is the pre-update copy.
        latest = await self._repo.get(goal.id) or goal
        if latest.parent_goal_id is not None:
            await self._repo.append_step(
                goal_id=latest.parent_goal_id,
                kind=parent_kind,
                text=(summary or "").strip(),
                metadata={
                    "subgoal_id": latest.id,
                    "subgoal_description": latest.description,
                    "subgoal_state": latest.state.value,
                },
            )
            await self._maybe_unblock_parent(latest.parent_goal_id)
        await self._maybe_resettle_plan(latest)

    async def _maybe_unblock_parent(self, parent_id: int) -> None:
        """If every subgoal of ``parent_id`` is terminal, flip the
        parent from BLOCKED_ON_SUBGOAL to READY_TO_REPLAN so its
        planner runs again with the new SUBGOAL_* steps in history.
        """
        siblings = await self._repo.list_subgoals(parent_id)
        if any(s.state in ACTIVE_STATES for s in siblings):
            return
        parent = await self._repo.get(parent_id)
        if parent is None or parent.state != GoalState.BLOCKED_ON_SUBGOAL:
            return
        await self._repo.update_state(parent_id, GoalState.READY_TO_REPLAN)
        logger.info(
            "Goal {}: all subgoals terminal → READY_TO_REPLAN", parent_id,
        )
        # Drive immediately so we don't wait for the next sweep.
        claimed = await self._repo.claim_for_replan(parent_id)
        if claimed is not None:
            try:
                await self._invoke_planner(claimed)
            except Exception:
                logger.exception(
                    "Goal {}: parent replan failed", parent_id,
                )
                await self._repo.update_state(
                    parent_id, GoalState.READY_TO_REPLAN,
                    clear_planning_started=True,
                )

    # ---------------------------------------------------------------- helpers

    async def _is_duplicate_ask(
        self, goal: ClarificationGoal, decision: PlannerDecision,
    ) -> bool:
        """no_duplicate_target guard: same handle + same dedupe_key
        in the last ~3 ASK steps without HUMAN_REPLIED between.
        """
        steps = await self._repo.list_steps(goal.id)
        steps = list(reversed(steps))   # newest first
        # Walk back through recent steps.
        seen_reply_since = False
        recent_asks = 0
        for step in steps:
            if step.kind == GoalStepKind.HUMAN_REPLIED:
                seen_reply_since = True
                # Reset counter — reply gives new evidence.
                if recent_asks == 0:
                    continue
                break
            if step.kind != GoalStepKind.BOT_ASKED:
                continue
            recent_asks += 1
            if recent_asks > 3:
                break
            handle = step.target_username
            dedupe = step.metadata.get("dedupe_key")
            if (
                decision.to_handle
                and handle
                and decision.to_handle.lstrip("@") == handle.lstrip("@")
                and dedupe and decision.dedupe_key
                and dedupe == decision.dedupe_key
                and not seen_reply_since
            ):
                return True
        return False

    async def _resolve_target(
        self, decision: PlannerDecision,
    ) -> tuple[str | None, str | None]:
        handle = (decision.to_handle or "").strip().lstrip("@")
        email = (decision.to_email or "").strip()
        if email and _EMAIL_RE.match(email):
            user_id = await self._communicator.resolve_user_id(email=email)
            if user_id is not None:
                return user_id, email.split("@", 1)[0]
        if handle and _USERNAME_RE.match(handle):
            user_id = await self._communicator.resolve_user_id(username=handle)
            if user_id is not None:
                return user_id, handle
        return None, handle or email or None

    async def _send_lead_escalation(
        self, goal: ClarificationGoal, *, reason: str,
    ) -> None:
        lead_user_id = await self._lead_user_id()
        if lead_user_id is None:
            logger.warning(
                "Goal {}: no team-lead configured; escalation lost",
                goal.id,
            )
            return
        chain = await self._render_chain(goal)
        task_url = await self._task_url(goal)
        body_template = (
            self._config.notifications.mattermost.clarifier_escalation_to_lead
            or
            "Застрял с уточнением по тикету [{external_id}]({task_url}).\n\n"
            "**Причина:** {reason}\n\n**Цель:** {original_question}\n\n"
            "**Цепочка:**\n{chain_summary}"
        )
        body = body_template.format(
            tracker=goal.tracker,
            external_id=goal.task_external_id,
            task_url=task_url,
            original_question=goal.description,
            chain_summary=chain,
            reason=reason,
        )
        await self._communicator.send_dm(lead_user_id, body)

    async def _render_chain(self, goal: ClarificationGoal) -> str:
        steps = await self._repo.list_steps(goal.id)
        if not steps:
            return "(no steps)"
        lines: list[str] = []
        for step in steps:
            who = step.target_username or step.target_user_id or "(internal)"
            text = (step.text or "").strip().splitlines()[0] if step.text else ""
            lines.append(
                f"- [{step.seq}] {step.kind.value} → @{who}: «{text[:160]}»"
            )
        return "\n".join(lines)

    async def _task_url(self, goal: ClarificationGoal) -> str:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == goal.tracker,
                    TaskRow.external_id == goal.task_external_id,
                )
            )).scalar_one_or_none()
        return (row.url if row else "") or ""

    async def _lead_user_id(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    async def _load_issue_summary(self, goal: ClarificationGoal) -> str:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == goal.tracker,
                    TaskRow.external_id == goal.task_external_id,
                )
            )).scalar_one_or_none()
        if row is None:
            return ""
        desc = (row.description or "").strip()
        if len(desc) > 3000:
            desc = desc[:3000] + "\n[truncated]"
        return f"# {row.title}\n\n{desc}"

    async def _resolve_repo_workspace(self, goal: ClarificationGoal) -> str | None:
        from pathlib import Path

        if goal.plan_id is None:
            return None
        async with self._session_factory() as session:
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == goal.plan_id)
            )).scalar_one_or_none()
        if plan_row is None or not plan_row.target_repo_key:
            return None
        repo_cfg = self._config.get_repository(plan_row.target_repo_key)
        if repo_cfg is None or not repo_cfg.local_path:
            return None
        return str(Path(repo_cfg.local_path).expanduser().resolve())

    async def _maybe_resettle_plan(self, goal: ClarificationGoal) -> None:
        """When ALL TOP-LEVEL goals on this plan are terminal AND at
        least one ACHIEVED, fold answers into task description,
        supersede plan, re-publish ``task.discovered`` so Analyst
        replans. Subgoals are excluded — they're internal decomposition.
        """
        if goal.plan_id is None:
            return
        top_level = await self._repo.list_top_level_for_plan(goal.plan_id)
        if any(s.state in ACTIVE_STATES for s in top_level):
            return
        if not any(s.state == GoalState.ACHIEVED for s in top_level):
            logger.info(
                "Plan {}: all top-level goals terminal, none ACHIEVED — not re-dispatching",
                goal.plan_id,
            )
            return

        await self._reseed_task_description(goal, top_level)
        if self._message_bus is not None:
            await self._message_bus.publish(AgentMessage(
                id=uuid.uuid4().hex,
                from_agent="clarification",
                to_agent="analyst",
                topic="task.discovered",
                payload={
                    "tracker": goal.tracker,
                    "external_id": goal.task_external_id,
                },
            ))
            self.stats.re_dispatches += 1
            logger.info(
                "Plan {}: re-dispatched task.discovered for {}",
                goal.plan_id, goal.task_external_id,
            )

    async def _reseed_task_description(
        self,
        anchor: ClarificationGoal,
        siblings: list[ClarificationGoal],
    ) -> None:
        async with session_scope(self._session_factory) as session:
            task = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == anchor.tracker,
                    TaskRow.external_id == anchor.task_external_id,
                )
            )).scalar_one_or_none()
            if task is None:
                return

            block = "\n\n## Уточнения от человека (собраны ботом)\n"
            for i, g in enumerate(siblings):
                block += f"\n**Q{i + 1}:** {g.description.strip()}\n"
                if g.state == GoalState.ACHIEVED and g.final_answer:
                    block += f"**A:** {g.final_answer.strip()}\n"
                else:
                    block += f"_(state: {g.state.value} — не получили ответ)_\n"

            base_desc = task.description or ""
            if block.strip() not in base_desc:
                task.description = (base_desc.rstrip() + "\n" + block).strip()
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == anchor.plan_id)
            )).scalar_one_or_none()
            if plan_row is not None:
                plan_row.status = PlanStatus.SUPERSEDED.value
            task.internal_status = "discovered"
            task.updated_at = datetime.now(timezone.utc)

    async def _emit(
        self,
        action: str,
        goal: ClarificationGoal,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        body = {
            "goal_id": goal.id,
            "description": goal.description,
            "state": goal.state.value,
            "action": action,
        }
        if payload:
            body.update(payload)
        await emit_if(self._trace, AgentTraceEvent(
            type="goal_event",
            agent_key="clarification",
            payload=body,
        ))


__all__ = ["GoalOrchestrator", "OrchestratorStats"]
