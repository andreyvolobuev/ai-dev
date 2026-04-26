"""ClarificationOrchestrator — owns the Question state machine.

Responsibilities:

* Spawn root Questions for a CLARIFYING plan (``request_clarifications``).
* Append fragments from incoming MM events (``append_fragment``).
* Periodically flush idle COALESCING questions through the LLM
  classifier and drive the state machine
  (``flush_idle`` — called by AnswerCoalescerWorker).
* Sweep deadlines: questions that have been open longer than the
  configured age get ``ABANDONED + escalated`` (``sweep_deadlines``).
* When a root's whole subtree settles, fold the Q&A back into the task
  description and re-publish ``task.discovered`` so the Analyst replans.

Loop guards (all enforced in ``_spawn_child`` / ``sweep_deadlines``):

* ``max_chain_depth``: redirect chains can grow at most this many
  hops.
* Cycle detection: a child whose resolved MM user is already on the
  ancestor chain → abort + escalate.
* ``max_question_age_hours``: per-question deadline.
* ``max_subquestions_per_root``: total subtree size cap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.answer_classifier import (
    AnswerClassifier,
    ClassificationResult,
)
from virtual_dev.application.agents.counter_answerer import CounterQuestionAnswerer
from virtual_dev.application.services.clarification.repo import QuestionRepository
from virtual_dev.application.services.clarification.stakeholder_resolver import (
    ResolveContext,
    StakeholderResolver,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.clarification import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    Classification,
    CounterQuestionKind,
    Question,
    QuestionState,
    Stakeholder,
    StakeholderKind,
)
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope

if TYPE_CHECKING:
    pass


@dataclass
class OrchestratorStats:
    questions_created: int = 0
    fragments_appended: int = 0
    classifications_run: int = 0
    redirects_spawned: int = 0
    counter_questions_handled: int = 0
    handles_acquired: int = 0
    deadlines_swept: int = 0
    escalations_sent: int = 0
    re_dispatches: int = 0


class ClarificationOrchestrator:
    """Single instance per process. Drives the entire Q-tree."""

    def __init__(
        self,
        *,
        repo: QuestionRepository,
        communicator: CommunicatorService,
        classifier: AnswerClassifier,
        counter_answerer: CounterQuestionAnswerer,
        stakeholder_resolver: StakeholderResolver,
        config: AppConfig,
        session_factory: async_sessionmaker[AsyncSession],
        message_bus: MessageBusPort | None,
    ) -> None:
        self._repo = repo
        self._communicator = communicator
        self._classifier = classifier
        self._counter_answerer = counter_answerer
        self._stakeholder_resolver = stakeholder_resolver
        self._config = config
        self._session_factory = session_factory
        self._message_bus = message_bus
        self.stats = OrchestratorStats()

    # ---------------------------------------------------------------- entry: kick off

    async def request_clarifications(
        self,
        *,
        task_row: TaskRow,
        plan: Plan,
        plan_row_id: int,
    ) -> int:
        """Spawn root Questions for each ``open_questions`` in the plan
        and DM their stakeholders.

        Idempotent: questions whose ``text`` already exists for this
        plan are skipped, so re-running on restart is safe.
        """
        if not plan.open_questions:
            return 0

        existing = await self._repo.list_roots_for_plan(plan_row_id)
        existing_texts = {q.text for q in existing}

        sent = 0
        clar_cfg = self._config.agents.clarification
        for oq in plan.open_questions:
            if oq.question in existing_texts:
                continue
            stakeholder = await self._resolve_with_fallback(
                raw_hint=(oq.ask_whom or "").strip(),
                context=ResolveContext(issue_summary=plan.summary),
            )
            deadline = datetime.now(timezone.utc) + timedelta(
                hours=clar_cfg.max_question_age_hours
            )
            question = await self._repo.create_root(
                tracker=task_row.tracker,
                task_external_id=task_row.external_id,
                plan_id=plan_row_id,
                text=oq.question,
                why_it_matters=oq.why_it_matters or "",
                stakeholder=stakeholder,
                coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
                deadline_at=deadline,
            )
            self.stats.questions_created += 1
            await self._dispatch_question(question, task_row)
            sent += 1
        return sent

    # ---------------------------------------------------------------- entry: incoming MM event

    async def find_question_by_thread(
        self, asked_post_id: str,
    ) -> Question | None:
        return await self._repo.find_active_by_thread(asked_post_id)

    async def find_question_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> Question | None:
        return await self._repo.find_active_by_channel(mm_channel_id, mm_user_id)

    async def append_fragment(
        self, question_id: int, mm_post: ChatMessage,
    ) -> bool:
        """Persist one MM message as a fragment on this Question.

        Returns True if newly inserted (False on duplicate post id).
        """
        ok = await self._repo.append_fragment(
            question_id=question_id,
            mm_post_id=mm_post.id,
            text=mm_post.text,
            received_at=mm_post.timestamp,
        )
        if ok:
            self.stats.fragments_appended += 1
            logger.info(
                "Clarification: appended fragment to question {} (post {})",
                question_id, mm_post.id,
            )
        return ok

    # ---------------------------------------------------------------- coalescer ticks

    async def flush_idle(self) -> int:
        """Tick: classify all questions whose idle window has elapsed.

        Also recovers questions stuck in ``CLASSIFYING`` for more than
        a few minutes — happens when ``_flush_one`` crashed mid-run
        (e.g. a transient DB error on save_answer) and the soft-lock
        wasn't released. Without recovery the question would sit
        zombie until the deadline_sweep killed it 48h later.
        """
        now = datetime.now(timezone.utc)
        await self._recover_stuck_classifying(now=now)
        idle = await self._repo.find_idle_coalescing(now=now)
        if not idle:
            return 0
        flushed = 0
        for question in idle:
            try:
                await self._flush_one(question)
                flushed += 1
            except Exception:
                logger.exception(
                    "Clarification: flush failed for question {}", question.id,
                )
                # Crucially, revert the soft-lock so the next tick can
                # retry. Otherwise the question stays in CLASSIFYING
                # forever and looks "muted" to the human.
                try:
                    await self._repo.update_state(
                        question.id, QuestionState.COALESCING,
                    )
                except Exception:
                    logger.exception(
                        "Clarification: failed to revert soft-lock on {}",
                        question.id,
                    )
        return flushed

    async def _recover_stuck_classifying(self, *, now: datetime) -> None:
        """Find questions in CLASSIFYING for too long — assume their
        flush_one crashed and revert to COALESCING so they get retried.
        """
        from sqlalchemy import select as _select
        from virtual_dev.infrastructure.db import QuestionRow

        cutoff = now - timedelta(minutes=10)
        async with self._session_factory() as session:
            stuck_ids = list((await session.execute(
                _select(QuestionRow.id).where(
                    QuestionRow.state == QuestionState.CLASSIFYING.value,
                    QuestionRow.last_fragment_at.is_not(None),
                    QuestionRow.last_fragment_at <= cutoff,
                )
            )).scalars().all())
        if not stuck_ids:
            return
        logger.warning(
            "Clarification: recovering {} stuck CLASSIFYING question(s): {}",
            len(stuck_ids), stuck_ids,
        )
        for qid in stuck_ids:
            try:
                await self._repo.update_state(qid, QuestionState.COALESCING)
            except Exception:
                logger.exception(
                    "Clarification: revert stuck classify on {} failed", qid,
                )

    async def sweep_deadlines(self) -> int:
        """Tick: abandon questions whose ``deadline_at`` has passed."""
        now = datetime.now(timezone.utc)
        overdue = await self._repo.find_overdue(now=now)
        if not overdue:
            return 0
        for question in overdue:
            await self._abandon(
                question,
                reason="deadline_exceeded",
                ack_text=None,  # don't pester someone who already went silent
            )
            self.stats.deadlines_swept += 1
        return len(overdue)

    # ---------------------------------------------------------------- internals

    async def _flush_one(self, question: Question) -> None:
        # Soft-lock: move to CLASSIFYING so concurrent ticks skip it.
        await self._repo.update_state(
            question.id, QuestionState.CLASSIFYING,
        )
        fragments = await self._repo.list_unflushed_fragments(question.id)
        if not fragments:
            # Race: someone else already flushed; revert.
            await self._repo.update_state(
                question.id, QuestionState.COALESCING,
            )
            return

        coalesced = "\n\n".join(
            f.text.strip() for f in fragments if f.text.strip()
        ) or fragments[0].text

        is_handle_q = question.state == QuestionState.ASKING_FOR_STAKEHOLDER
        # ^ note: we already mutated state above; original was either
        # COALESCING (most cases) or ASKING_FOR_STAKEHOLDER if a
        # respondent's first reply *to a handle-request* lands here. We
        # detect the latter by checking whether the question's
        # *original* state was ASKING_FOR_STAKEHOLDER. Since
        # update_state already changed it, we re-check via the flag we
        # had pre-flush.
        # In practice, ASKING_FOR_STAKEHOLDER → COALESCING transition
        # happens via append_fragment (it sees state ASKING and bumps
        # to COALESCING). To distinguish, we mark the parent question's
        # answer extras with a flag during dispatch. For now, we infer
        # from chain shape: a Question whose parent is in
        # ASKING_FOR_STAKEHOLDER state was a handle-request child.
        is_handle_q = await self._is_handle_request_question(question)

        issue_summary = await self._load_issue_summary(question)

        result = await self._classifier.classify(
            question_text=question.text,
            why_it_matters=question.why_it_matters,
            coalesced_answer=coalesced,
            issue_summary=issue_summary,
            is_asking_for_stakeholder=is_handle_q,
        )
        self.stats.classifications_run += 1

        await self._repo.save_answer(
            question_id=question.id,
            coalesced_text=coalesced,
            classification=result.classification,
            extracted=result.extracted,
            cost_usd=result.cost_usd,
        )
        await self._repo.mark_fragments_flushed(question.id)

        # Single ✅-reaction on the LAST fragment — visible signal to
        # the human that "I read your whole reply, processing now".
        # Mid-message reactions (one per fragment) read as
        # interrupting; this is the natural place.
        last_post_id = fragments[-1].mm_post_id
        if last_post_id:
            await self._communicator.add_reaction(last_post_id, "white_check_mark")

        await self._apply_classification(question, result, coalesced)

    async def _apply_classification(
        self,
        question: Question,
        result: ClassificationResult,
        coalesced: str,
    ) -> None:
        cl = result.classification
        if cl == Classification.DIRECT:
            await self._on_direct(question, result)
        elif cl == Classification.REDIRECT:
            await self._on_redirect(question, result)
        elif cl == Classification.COUNTER_QUESTION:
            await self._on_counter(question, result)
        elif cl == Classification.DONT_KNOW:
            await self._on_dont_know(question, result)
        elif cl == Classification.OUT_OF_SCOPE:
            await self._on_out_of_scope(question, result)
        elif cl == Classification.HANDLE_PROVIDED:
            await self._on_handle_provided(question, result)
        else:
            logger.warning(
                "Clarification: unknown classification {!r}; abandoning {}",
                cl, question.id,
            )
            await self._abandon(question, reason="unknown_classification")

    async def _on_direct(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        await self._repo.update_state(
            question.id, QuestionState.ANSWERED, closed=True,
        )
        await self._post_thread_ack(
            question,
            self._template("clarifier_answer_ack"),
        )
        await self._maybe_resettle_root(question)

    async def _on_redirect(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        ext = result.extracted
        raw_hint = (
            str(ext.get("redirect_target_handle") or "")
            or str(ext.get("redirect_target_email") or "")
            or str(ext.get("redirect_target_name") or "")
        ).strip()
        if not raw_hint:
            logger.warning(
                "Clarification: redirect with no target on {}; treating as dont_know",
                question.id,
            )
            await self._on_dont_know(question, result)
            return

        # Loop guards (depth + tree size) before resolution work.
        if not await self._guard_chain_depth(question):
            return
        if not await self._guard_tree_size(question):
            return

        new_stakeholder = await self._resolve_with_fallback(
            raw_hint=raw_hint,
            context=ResolveContext(
                issue_summary=await self._load_issue_summary(question),
            ),
        )

        if new_stakeholder.kind == StakeholderKind.UNRESOLVED_NAME:
            # Free-form name → ask original respondent for the handle.
            await self._spawn_handle_request(
                parent=question, raw_hint=raw_hint,
                display_name=new_stakeholder.display_name,
            )
            return

        # Cycle detection.
        chain = await self._repo.chain_user_ids(question)
        if (
            new_stakeholder.resolved_mm_user_id is not None
            and new_stakeholder.resolved_mm_user_id in chain
        ):
            logger.warning(
                "Clarification: redirect cycle detected on question {}; "
                "would resolve to {} which is already in the chain",
                question.id, new_stakeholder.resolved_mm_user_id,
            )
            await self._escalate_to_lead(
                question, reason="redirect_cycle",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return

        # Spawn child + close parent.
        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_question_age_hours
        )
        child = await self._repo.create_child(
            parent=question,
            text=question.text,
            why_it_matters=question.why_it_matters,
            stakeholder=new_stakeholder,
            coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
            deadline_at=deadline,
        )
        self.stats.questions_created += 1
        self.stats.redirects_spawned += 1

        await self._repo.update_state(
            question.id, QuestionState.REDIRECTED, closed=True,
        )

        # Ack original respondent.
        await self._post_thread_ack(
            question,
            self._template("clarifier_redirect_ack").format(
                handle=new_stakeholder.display_name or new_stakeholder.raw_hint,
                target_display_name=new_stakeholder.display_name or "",
            ),
        )

        # DM the new stakeholder.
        task_row = await self._load_task_row(question)
        if task_row is not None:
            await self._dispatch_question(child, task_row)

    async def _on_counter(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        ext = result.extracted
        kind_raw = str(ext.get("counter_question_kind") or "factual").lower()
        try:
            counter_kind = CounterQuestionKind(kind_raw)
        except ValueError:
            counter_kind = CounterQuestionKind.BUSINESS

        counter_text = str(ext.get("counter_question_text") or "").strip()
        counter_reasoning = str(ext.get("counter_question_reasoning") or "").strip()
        if not counter_text:
            # Classifier said "counter" but didn't fill the field — be safe.
            await self._abandon(question, reason="counter_without_text")
            return

        if not await self._guard_tree_size(question):
            return

        if counter_kind == CounterQuestionKind.FACTUAL:
            await self._answer_counter_factual(
                parent=question,
                counter_text=counter_text,
                counter_reasoning=counter_reasoning,
            )
        else:
            await self._escalate_counter_to_reporter(
                parent=question,
                counter_text=counter_text,
                counter_reasoning=counter_reasoning,
            )

    async def _on_dont_know(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        await self._post_thread_ack(
            question, self._template("clarifier_dont_know_ack"),
        )
        await self._escalate_to_lead(
            question, reason="dont_know", ack_text=None,
        )

    async def _on_out_of_scope(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        await self._post_thread_ack(
            question, self._template("clarifier_out_of_scope_ack"),
        )
        await self._escalate_to_lead(
            question, reason="out_of_scope", ack_text=None,
        )

    async def _on_handle_provided(
        self, question: Question, result: ClassificationResult,
    ) -> None:
        ext = result.extracted
        handle = str(ext.get("provided_handle") or "").strip().lstrip("@")
        email = str(ext.get("provided_email") or "").strip()
        raw_hint = handle or email
        if not raw_hint:
            await self._on_dont_know(question, result)
            return

        new_stakeholder = await self._resolve_with_fallback(
            raw_hint=raw_hint,
            context=ResolveContext(
                issue_summary=await self._load_issue_summary(question),
            ),
        )
        if new_stakeholder.kind == StakeholderKind.UNRESOLVED_NAME:
            # Even with a handle hint, MM doesn't know them. Give up
            # cleanly rather than chaining another handle-request.
            await self._escalate_to_lead(
                question, reason="handle_unresolvable",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return

        # Cycle check.
        if not await self._guard_chain_depth(question):
            return
        if not await self._guard_tree_size(question):
            return
        chain = await self._repo.chain_user_ids(question)
        if (
            new_stakeholder.resolved_mm_user_id is not None
            and new_stakeholder.resolved_mm_user_id in chain
        ):
            await self._escalate_to_lead(
                question, reason="handle_cycle",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return

        # Find the question this handle-request was answering — that's
        # the question whose redirect we now retry. Walk up via parent.
        retried_question_text = question.text
        retried_question_why = question.why_it_matters
        # The parent of a handle-request question is the redirected
        # node. Use its text/reasoning so the new ASKING child asks the
        # ORIGINAL question (not the "what's their MM handle?" one).
        parent = await self._repo.get(question.parent_id) if question.parent_id else None
        if parent is not None:
            retried_question_text = parent.text
            retried_question_why = parent.why_it_matters

        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_question_age_hours
        )
        child = await self._repo.create_child(
            parent=question,
            text=retried_question_text,
            why_it_matters=retried_question_why,
            stakeholder=new_stakeholder,
            coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
            deadline_at=deadline,
        )
        self.stats.questions_created += 1
        self.stats.handles_acquired += 1

        await self._repo.update_state(
            question.id, QuestionState.REDIRECTED, closed=True,
        )

        await self._post_thread_ack(
            question,
            self._template("clarifier_redirect_ack").format(
                handle=new_stakeholder.display_name or new_stakeholder.raw_hint,
                target_display_name=new_stakeholder.display_name or "",
            ),
        )
        task_row = await self._load_task_row(question)
        if task_row is not None:
            await self._dispatch_question(child, task_row)

    # ---------------------------------------------------------------- counter-question paths

    async def _answer_counter_factual(
        self,
        *,
        parent: Question,
        counter_text: str,
        counter_reasoning: str,
    ) -> None:
        """Bot self-answers via Sonnet + Read/Glob/Grep + Researcher."""
        await self._repo.update_state(parent.id, QuestionState.COUNTER_PENDING)

        issue_summary = await self._load_issue_summary(parent)
        repo_workspace = await self._resolve_repo_workspace(parent)

        try:
            answer = await self._counter_answerer.answer(
                original_question=parent.text,
                original_question_reasoning=parent.why_it_matters,
                counter_question=counter_text,
                counter_question_reasoning=counter_reasoning,
                issue_summary=issue_summary,
                repo_workspace=repo_workspace,
            )
        except Exception:
            logger.exception("Clarification: counter-answerer crashed")
            await self._escalate_counter_to_reporter(
                parent=parent,
                counter_text=counter_text,
                counter_reasoning=counter_reasoning,
            )
            return

        confidence_threshold = (
            self._config.agents.clarification.counter_question_confidence_threshold
        )
        if (
            answer.escalate_to_reporter
            or answer.confidence < confidence_threshold
            or not answer.answer_text.strip()
        ):
            logger.info(
                "Clarification: counter-answerer fell back (confidence={:.2f}, "
                "escalate={}); routing to reporter",
                answer.confidence, answer.escalate_to_reporter,
            )
            await self._escalate_counter_to_reporter(
                parent=parent,
                counter_text=counter_text,
                counter_reasoning=counter_reasoning,
            )
            return

        # Post the bot's reply in the same DM thread; parent goes back
        # to ASKING (idle timer resets when respondent next writes).
        body = self._template("clarifier_counter_factual_intro").format(
            bot_answer=answer.answer_text.strip(),
        )
        await self._post_thread_ack(parent, body)
        await self._repo.update_state(parent.id, QuestionState.ASKING)
        self.stats.counter_questions_handled += 1

    async def _escalate_counter_to_reporter(
        self,
        *,
        parent: Question,
        counter_text: str,
        counter_reasoning: str,
    ) -> None:
        """BUSINESS counter-question → spawn child to task.reporter."""
        task_row = await self._load_task_row(parent)
        if task_row is None or not task_row.reporter_id:
            logger.warning(
                "Clarification: cannot escalate counter — task or "
                "reporter_id missing for question {}", parent.id,
            )
            await self._abandon(parent, reason="counter_no_reporter")
            return

        # Resolve reporter.
        reporter_user_id = await self._communicator.resolve_user_id(
            email=task_row.reporter_id,
        ) or await self._communicator.resolve_user_id(
            username=task_row.reporter_id,
        )
        if reporter_user_id is None:
            logger.warning(
                "Clarification: cannot resolve reporter {!r}; escalating to lead",
                task_row.reporter_id,
            )
            await self._escalate_to_lead(
                parent, reason="counter_reporter_unresolved",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return

        if not await self._guard_chain_depth(parent):
            return

        new_stakeholder = Stakeholder(
            kind=StakeholderKind.TASK_AUTHOR,
            raw_hint=task_row.reporter_id,
            resolved_mm_user_id=reporter_user_id,
            display_name=task_row.reporter_id,
        )
        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_question_age_hours
        )
        # The child question is the counter, going to the reporter.
        child = await self._repo.create_child(
            parent=parent,
            text=counter_text,
            why_it_matters=counter_reasoning or "Counter-question from a stakeholder",
            stakeholder=new_stakeholder,
            coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
            deadline_at=deadline,
        )
        self.stats.questions_created += 1
        self.stats.counter_questions_handled += 1

        await self._repo.update_state(parent.id, QuestionState.COUNTER_PENDING)
        await self._dispatch_question(child, task_row)

    async def _spawn_handle_request(
        self,
        *,
        parent: Question,
        raw_hint: str,
        display_name: str | None,
    ) -> None:
        """When a redirect's name is unresolvable, ask the original
        respondent for the MM handle.
        """
        # The "original respondent" is the parent's stakeholder.
        if parent.mm_user_id is None or parent.mm_channel_id is None:
            await self._escalate_to_lead(
                parent, reason="handle_request_no_dm",
                ack_text=None,
            )
            return

        if not await self._guard_chain_depth(parent):
            return
        if not await self._guard_tree_size(parent):
            return

        clar_cfg = self._config.agents.clarification
        deadline = datetime.now(timezone.utc) + timedelta(
            hours=clar_cfg.max_question_age_hours
        )
        # Stakeholder of the handle-request is the same human we asked
        # the parent — they redirected, we're asking them to clarify.
        handle_request_stakeholder = Stakeholder(
            kind=parent.stakeholder.kind,
            raw_hint=parent.stakeholder.raw_hint,
            resolved_mm_user_id=parent.stakeholder.resolved_mm_user_id,
            resolved_mm_channel_id=parent.stakeholder.resolved_mm_channel_id,
            display_name=parent.stakeholder.display_name,
        )
        request_text = self._template("clarifier_handle_request").format(
            raw_name=display_name or raw_hint,
            original_question=parent.text,
        )

        child = await self._repo.create_child(
            parent=parent,
            text=request_text,
            why_it_matters=f"Looking for MM handle of: {raw_hint}",
            stakeholder=handle_request_stakeholder,
            coalesce_window_seconds=clar_cfg.coalesce_window_seconds,
            deadline_at=deadline,
        )
        self.stats.questions_created += 1
        await self._repo.update_state(parent.id, QuestionState.ASKING_FOR_STAKEHOLDER)

        # DM in the same thread as parent.
        outcome = await self._communicator.send_channel(
            parent.mm_channel_id, request_text,
            thread_root_id=parent.asked_post_id,
        )
        if outcome.sent and outcome.message is not None:
            await self._repo.update_state(
                child.id, QuestionState.ASKING,
                asked_post_id=outcome.message.id,
                mm_user_id=parent.mm_user_id,
                mm_channel_id=outcome.message.channel_id,
            )
        else:
            logger.warning(
                "Clarification: handle_request DM not sent for {}; abandoning",
                child.id,
            )
            await self._abandon(child, reason="dm_failed")

    # ---------------------------------------------------------------- DM dispatch

    async def _dispatch_question(
        self, question: Question, task_row: TaskRow,
    ) -> None:
        """Send the question's DM; flip state to ASKING; record post id."""
        # If the resolved stakeholder is the team-lead (UNRESOLVED_NAME
        # path's terminal fallback), DM the lead — same channel logic.
        target_user_id = question.stakeholder.resolved_mm_user_id
        unresolved_fallback = False
        if target_user_id is None:
            target_user_id = await self._lead_user_id()
            if target_user_id is None:
                await self._abandon(
                    question, reason="no_target_user",
                )
                return
            # Mark so we can prefix the DM with "couldn't find X" — the
            # lead shouldn't get a question that looks routed to them.
            unresolved_fallback = (
                question.stakeholder.kind == StakeholderKind.UNRESOLVED_NAME
            )

        body = self._render_question_body(question, task_row)
        if unresolved_fallback:
            raw_name = (
                question.stakeholder.display_name
                or question.stakeholder.raw_hint
                or "(unknown)"
            )
            body = (
                f"_(не нашёл `{raw_name}` в Mattermost — перенаправил "
                f"вам как тимлиду; если знаете правильный handle, "
                f"подскажите его в ответ)_\n\n" + body
            )
        outcome = await self._communicator.send_dm(target_user_id, body)
        if not (outcome.sent and outcome.message is not None):
            logger.warning(
                "Clarification: question DM not sent for {} ({})",
                question.id, outcome.skip_reason,
            )
            # Don't abandon — Communicator may have rate-limited; we'll
            # retry next request_clarifications. But we DO need to keep
            # the row in PENDING so it's not eaten by other paths.
            return

        await self._repo.update_state(
            question.id, QuestionState.ASKING,
            asked_post_id=outcome.message.id,
            mm_user_id=target_user_id,
            mm_channel_id=outcome.message.channel_id,
        )
        logger.info(
            "Clarification: dispatched question {} to user {} (post {})",
            question.id, target_user_id, outcome.message.id,
        )

    def _render_question_body(self, question: Question, task_row: TaskRow) -> str:
        template = (
            self._config.notifications.mattermost.clarifier_question
            or "Привет! Уточнение по [{external_id}]({task_url}): {question}"
        )
        why_block = ""
        if question.why_it_matters.strip():
            why_block = (
                "\n_Почему это важно:_ " + question.why_it_matters.strip() + "\n"
            )
        try:
            return template.format(
                external_id=task_row.external_id,
                title=task_row.title,
                task_url=task_row.url or "",
                question=question.text,
                why_it_matters=question.why_it_matters,
                why_it_matters_block=why_block,
            )
        except (KeyError, IndexError) as exc:
            logger.warning("Clarification: question template failed: {}", exc)
            return template

    # ---------------------------------------------------------------- terminal helpers

    async def _abandon(
        self,
        question: Question,
        *,
        reason: str,
        ack_text: str | None = None,
    ) -> None:
        await self._repo.update_state(
            question.id, QuestionState.ABANDONED, closed=True,
        )
        if ack_text is not None:
            await self._post_thread_ack(question, ack_text)
        logger.info(
            "Clarification: abandoned question {} (reason={})", question.id, reason,
        )
        await self._maybe_resettle_root(question)

    async def _escalate_to_lead(
        self,
        question: Question,
        *,
        reason: str,
        ack_text: str | None,
    ) -> None:
        """DM the team-lead with the chain context, mark ESCALATED."""
        if ack_text is not None:
            await self._post_thread_ack(question, ack_text)
        await self._repo.update_state(
            question.id, QuestionState.ESCALATED, closed=True,
        )
        await self._send_lead_escalation(question, reason=reason)
        self.stats.escalations_sent += 1
        await self._maybe_resettle_root(question)

    async def _send_lead_escalation(
        self, question: Question, *, reason: str,
    ) -> None:
        lead_user_id = await self._lead_user_id()
        if lead_user_id is None:
            logger.warning(
                "Clarification: no team-lead configured; escalation lost for {}",
                question.id,
            )
            return
        chain_summary = await self._render_chain_summary(question)
        task_row = await self._load_task_row(question)
        # Walk to root for original question text.
        root = await self._root_of(question)
        body_template = self._template("clarifier_escalation_to_lead")
        body = body_template.format(
            tracker=question.tracker,
            external_id=question.task_external_id,
            task_url=(task_row.url if task_row else "") or "",
            original_question=root.text if root else question.text,
            chain_summary=chain_summary,
            reason=reason,
        )
        await self._communicator.send_dm(lead_user_id, body)

    async def _render_chain_summary(self, question: Question) -> str:
        """Render the ancestor chain ending at this question."""
        chain: list[Question] = []
        cur: Question | None = question
        while cur is not None:
            chain.append(cur)
            cur = await self._repo.get(cur.parent_id) if cur.parent_id else None
        chain.reverse()
        lines: list[str] = []
        for q in chain:
            who = q.stakeholder.display_name or q.stakeholder.raw_hint or "(team-lead)"
            ans = ""
            if q.answer is not None and q.answer.coalesced_text.strip():
                snippet = q.answer.coalesced_text.strip().splitlines()[0][:160]
                ans = f" → ответ: «{snippet}»"
            lines.append(f"- @{who}: «{q.text[:160]}» (state: {q.state.value}){ans}")
        return "\n".join(lines)

    async def _root_of(self, question: Question) -> Question | None:
        if question.id == question.root_id:
            return question
        return await self._repo.get(question.root_id)

    async def _post_thread_ack(self, question: Question, text: str) -> None:
        if not question.mm_channel_id or not question.asked_post_id:
            return
        try:
            await self._communicator.send_channel(
                question.mm_channel_id, text,
                thread_root_id=question.asked_post_id,
            )
        except Exception:
            logger.warning(
                "Clarification: thread ack failed for question {}", question.id,
            )

    # ---------------------------------------------------------------- guards

    async def _guard_chain_depth(self, parent: Question) -> bool:
        clar_cfg = self._config.agents.clarification
        if parent.chain_depth + 1 > clar_cfg.max_chain_depth:
            logger.warning(
                "Clarification: max_chain_depth reached on {} (depth {}); escalating",
                parent.id, parent.chain_depth,
            )
            await self._escalate_to_lead(
                parent, reason="max_chain_depth",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return False
        return True

    async def _guard_tree_size(self, parent: Question) -> bool:
        clar_cfg = self._config.agents.clarification
        count = await self._repo.count_in_root(parent.root_id)
        if count >= clar_cfg.max_subquestions_per_root:
            logger.warning(
                "Clarification: max_subquestions_per_root reached on root {}",
                parent.root_id,
            )
            await self._escalate_to_lead(
                parent, reason="max_subquestions_per_root",
                ack_text=self._template("clarifier_out_of_scope_ack"),
            )
            return False
        return True

    # ---------------------------------------------------------------- settle / re-publish

    async def _maybe_resettle_root(self, question: Question) -> None:
        """If the root subtree is fully terminal, fold answers into the
        task description, supersede the plan, and re-publish
        ``task.discovered``.
        """
        root_id = question.root_id
        all_in_subtree = await self._repo.list_for_root(root_id)
        if not all_in_subtree:
            return
        # Some leaves can be non-terminal: anything in active state means
        # we still wait. (REDIRECTED is terminal for *this* node, but
        # the redirect has a child which must also be terminal; those
        # are separate rows under the same root.)
        active = [q for q in all_in_subtree if q.state in ACTIVE_STATES]
        if active:
            return

        # All settled. Now check whether ALL roots for this plan are
        # settled (the plan can have multiple open_questions).
        root = await self._repo.get(root_id)
        if root is None or root.plan_id is None:
            return
        roots = await self._repo.list_roots_for_plan(root.plan_id)
        for r in roots:
            sub = await self._repo.list_for_root(r.id)
            if any(q.state in ACTIVE_STATES for q in sub):
                return

        # Everything settled. Did at least one branch end in ANSWERED?
        any_answered = False
        for r in roots:
            sub = await self._repo.list_for_root(r.id)
            if any(q.state == QuestionState.ANSWERED for q in sub):
                any_answered = True
                break
        if not any_answered:
            logger.info(
                "Clarification: all roots for plan {} settled but none ANSWERED; "
                "leaving the plan superseded without re-dispatch (lead has been notified).",
                root.plan_id,
            )
            return

        await self._reseed_task_with_answers(
            tracker=root.tracker, external_id=root.task_external_id,
            plan_id=root.plan_id,
        )
        if self._message_bus is not None:
            await self._message_bus.publish(AgentMessage(
                id=uuid.uuid4().hex,
                from_agent="clarification",
                to_agent="analyst",
                topic="task.discovered",
                payload={
                    "tracker": root.tracker,
                    "external_id": root.task_external_id,
                },
            ))
            self.stats.re_dispatches += 1
            logger.info(
                "Clarification: all answers in for {} — re-dispatched to Analyst",
                root.task_external_id,
            )

    async def _reseed_task_with_answers(
        self, *, tracker: str, external_id: str, plan_id: int,
    ) -> None:
        roots = await self._repo.list_roots_for_plan(plan_id)
        # Pull each root's full subtree to flatten Q→...→A pairs.
        subtrees: list[list[Question]] = []
        for r in roots:
            subtrees.append(await self._repo.list_for_root(r.id))

        async with session_scope(self._session_factory) as session:
            task = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == tracker,
                    TaskRow.external_id == external_id,
                )
            )).scalar_one_or_none()
            if task is None:
                return

            qa_block = "\n\n## Уточнения от человека (собраны ботом)\n"
            for i, sub in enumerate(subtrees):
                root_q = sub[0]
                qa_block += f"\n**Q{i + 1}:** {root_q.text.strip()}\n"
                # Walk leaf chain to find the DIRECT answer (or note
                # what happened if escalated).
                final = None
                for q in sub:
                    if q.state == QuestionState.ANSWERED and q.answer is not None:
                        final = q
                        break
                if final is not None and final.answer is not None:
                    answer_text = final.answer.coalesced_text.strip()
                    if final.stakeholder.display_name:
                        qa_block += (
                            f"_(ответил @{final.stakeholder.display_name})_\n"
                        )
                    qa_block += f"**A:** {answer_text}\n"
                else:
                    qa_block += "_(не удалось получить ответ — эскалировано тимлиду)_\n"

            base_desc = task.description or ""
            if qa_block.strip() not in base_desc:
                task.description = (base_desc.rstrip() + "\n" + qa_block).strip()
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == plan_id)
            )).scalar_one_or_none()
            if plan_row is not None:
                plan_row.status = PlanStatus.SUPERSEDED.value
            task.internal_status = "discovered"
            task.updated_at = datetime.now(timezone.utc)

    # ---------------------------------------------------------------- helpers

    def _template(self, key: str) -> str:
        return getattr(
            self._config.notifications.mattermost, key, "",
        ) or ""

    async def _lead_user_id(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    async def _resolve_with_fallback(
        self, *, raw_hint: str, context: ResolveContext,
    ) -> Stakeholder:
        """Resolve a hint or fall back to TEAM_LEAD when no hint at all.

        Empty hint → team-lead. Else go through StakeholderResolver.
        Unresolved ones are returned with kind=UNRESOLVED_NAME so the
        orchestrator can decide whether to ASKING_FOR_STAKEHOLDER or
        treat as team-lead based on context.
        """
        if not raw_hint.strip():
            lead_id = await self._lead_user_id()
            if lead_id is not None:
                return Stakeholder(
                    kind=StakeholderKind.TEAM_LEAD, raw_hint="",
                    resolved_mm_user_id=lead_id, display_name="team-lead",
                )
            return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint="")
        return await self._stakeholder_resolver.resolve(raw_hint, context=context)

    async def _load_issue_summary(self, question: Question) -> str:
        async with self._session_factory() as session:
            task = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == question.tracker,
                    TaskRow.external_id == question.task_external_id,
                )
            )).scalar_one_or_none()
        if task is None:
            return ""
        # Description can be long; trim to ~3KB so the classifier prompt
        # stays cheap.
        desc = (task.description or "").strip()
        if len(desc) > 3000:
            desc = desc[:3000] + "\n[truncated]"
        return f"# {task.title}\n\n{desc}"

    async def _load_task_row(self, question: Question) -> TaskRow | None:
        async with self._session_factory() as session:
            return (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == question.tracker,
                    TaskRow.external_id == question.task_external_id,
                )
            )).scalar_one_or_none()

    async def _resolve_repo_workspace(self, question: Question) -> str | None:
        from pathlib import Path

        # Find the plan to get target_repo_key.
        if question.plan_id is None:
            # walk to root for plan_id
            root = await self._root_of(question)
            plan_id = root.plan_id if root else None
        else:
            plan_id = question.plan_id
        if plan_id is None:
            return None
        async with self._session_factory() as session:
            plan_row = (await session.execute(
                select(PlanRow).where(PlanRow.id == plan_id)
            )).scalar_one_or_none()
        if plan_row is None or not plan_row.target_repo_key:
            return None
        repo_cfg = self._config.get_repository(plan_row.target_repo_key)
        if repo_cfg is None or not repo_cfg.local_path:
            return None
        return str(Path(repo_cfg.local_path).expanduser().resolve())

    async def _is_handle_request_question(self, question: Question) -> bool:
        if question.parent_id is None:
            return False
        async with self._session_factory() as session:
            from virtual_dev.infrastructure.db import QuestionRow
            parent_state = (await session.execute(
                select(QuestionRow.state).where(QuestionRow.id == question.parent_id)
            )).scalar_one_or_none()
        return parent_state == QuestionState.ASKING_FOR_STAKEHOLDER.value


__all__ = ["ClarificationOrchestrator", "OrchestratorStats"]
