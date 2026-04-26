"""FastAPI app for the test-analyst UI.

Standalone app for iterating on the Analyst + clarification subsystem
without touching real Mattermost / Jira / GitLab. The browser page at
``/`` shows the task input, a live activity feed (every tool_use,
LLM prompt, and LLM text block), and a mock chat that drives the
clarification flow.

Build it with :func:`build_test_analyst_app`. The app owns:
* an in-memory SQLite engine (per-session, wiped on restart),
* an :class:`InMemoryChat` instead of MattermostChat,
* the real Analyst / Clarification orchestrator wired against both,
* an :class:`AgentTrace` shared with the SDK code-agent.

No background pollers (Reviewer / DevOps / Orchestrator-Jira-poll) —
the test app is *single-shot*: the operator clicks Run, the analyst
runs once, and any clarification questions land in the chat.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from virtual_dev.adapters.chat.in_memory import InMemoryChat
from virtual_dev.adapters.code_agent import ClaudeAgentSdkCodeAgent
from virtual_dev.application.agents import AnalystAgent
from virtual_dev.application.agents.clarification_planner import ClarificationPlanner
from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    PromptsLoader,
    ResearcherToolkit,
)
from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
)
from virtual_dev.application.services.clarification import (
    GoalOrchestrator,
    GoalRepository,
)
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.infrastructure.config import (
    AppConfig,
    Settings,
    load_config,
)
from virtual_dev.infrastructure.db import (
    Base,
    PlanRow,
    TaskRow,
    make_engine,
    make_session_factory,
)
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.runtime.workers.answer_coalescer import make_answer_coalescer_worker

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class TestAnalystState:
    """Container of singletons for one test-analyst process."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        settings: Settings,
        trace: AgentTrace,
        chat: InMemoryChat,
        analyst: AnalystAgent,
        goal_orchestrator: GoalOrchestrator,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.config = config
        self.settings = settings
        self.trace = trace
        self.chat = chat
        self.analyst = analyst
        self.goal_orchestrator = goal_orchestrator


async def _build_state(
    config_dir: str | Path = "config",
    *,
    coalesce_window_seconds: int = 30,
) -> TestAnalystState:
    settings = Settings()
    config = load_config(config_dir)
    # Tighter idle window for the test UI (default 30s instead of the
    # production 10 min) — when iterating on the analyst manually we
    # don't want to wait that long after each reply.
    config.agents.clarification.coalesce_window_seconds = coalesce_window_seconds
    # Auto-route escalation DMs to the operator (they ARE the lead in
    # the test-analyst session). Without this an `escalate_to_lead`
    # decision silently drops the DM and the activity feed shows
    # nothing — the operator can't tell the bot gave up.
    if not config.agents.escalation.mattermost_user.strip():
        config.agents.escalation.mattermost_user = "you"

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = make_session_factory(engine)

    trace = AgentTrace()
    chat = InMemoryChat(trace=trace)
    # Seed the operator into the directory with a real first/last name
    # so search_mm_users_by_name("you" / "operator") returns them and
    # the planner has SOMEONE to ask when the analyst's hint is empty.
    chat.register_user(
        "you",
        first_name="Тестировщик",
        last_name="Оператор",
        position="Issue reporter (test-analyst session)",
    )
    injection_filter = InjectionFilter()

    code_agent = ClaudeAgentSdkCodeAgent(
        default_model=config.agents.models.default,
        trace=trace,
    )
    prompts_loader = PromptsLoader(Path(config_dir) / "prompts")
    communicator = CommunicatorService(
        chat, injection_filter, respect_working_hours=False,
    )
    # Researcher needs a workspace dir; wire to settings even if we
    # don't really code on it during this UI flow.
    researcher = ResearcherToolkit(
        config=config,
        workspaces_dir=settings.workspaces_dir,
        knowledge_base=None,
        injection_filter=injection_filter,
        mr_history=None,
    )

    analyst = AnalystAgent(
        code_agent=code_agent,
        researcher=researcher,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        settings=settings,
        prompts_loader=prompts_loader,
    )

    planner = ClarificationPlanner(
        code_agent=code_agent,
        config=config,
        prompts_loader=prompts_loader,
        communicator=communicator,
        researcher=researcher,
        injection_filter=injection_filter,
        trace=trace,
    )
    goal_orchestrator = GoalOrchestrator(
        repo=GoalRepository(session_factory),
        communicator=communicator,
        planner=planner,
        config=config,
        session_factory=session_factory,
        message_bus=None,
        trace=trace,
    )

    return TestAnalystState(
        engine=engine,
        session_factory=session_factory,
        config=config,
        settings=settings,
        trace=trace,
        chat=chat,
        analyst=analyst,
        goal_orchestrator=goal_orchestrator,
    )


def build_test_analyst_app(
    config_dir: str | Path = "config",
    *,
    coalesce_window_seconds: int = 30,
) -> FastAPI:
    """Construct the FastAPI app. State is built inside lifespan."""

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await _build_state(
            config_dir, coalesce_window_seconds=coalesce_window_seconds,
        )
        app.state.test_state = state

        # Coalescer worker — same as in the prod web app, but fast cycles
        # (10s) and a tighter idle window (we'll override coalesce_window
        # at question level via config). We can't change config for the
        # live app, but the operator can edit clarification.coalesce_window
        # in agents.yaml.
        coalescer = make_answer_coalescer_worker(
            orchestrator=state.goal_orchestrator,
            interval_seconds=10,
        )
        coalescer_task = asyncio.create_task(coalescer.run_forever(), name="coalescer")

        # Listener-style consumer: every user message lands on chat.subscribe()
        # and we route it through the orchestrator's append_fragment using the
        # same lookup logic as MmThreadListener.
        listener_task = asyncio.create_task(
            _drive_chat_inbox(state), name="chat-inbox",
        )
        try:
            yield
        finally:
            await coalescer.stop()
            for task in (coalescer_task, listener_task):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await state.engine.dispose()

    app = FastAPI(title="Virtual Dev — Test Analyst", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "test_analyst.html", {})

    @app.websocket("/ws/test-analyst")
    async def ws_test_analyst(ws: WebSocket) -> None:
        await ws.accept()
        state: TestAnalystState = app.state.test_state
        # Forwarder: drains the trace and pushes JSON to the browser.
        sub = state.trace.subscribe()
        send_task = asyncio.create_task(_forward_events(ws, sub))
        try:
            while True:
                raw = await ws.receive_json()
                kind = str(raw.get("type") or "")
                if kind == "run_task":
                    asyncio.create_task(
                        _run_task(state, raw),
                        name=f"run-{raw.get('external_id')}",
                    )
                elif kind == "chat":
                    await state.chat.post_user_message(
                        text=str(raw.get("text") or ""),
                        thread_root_id=raw.get("thread_root_id"),
                        author_username=(raw.get("speaking_as") or None),
                    )
                elif kind == "register_user":
                    handle = str(raw.get("handle") or "").strip()
                    if handle:
                        state.chat.register_user(
                            handle,
                            first_name=(raw.get("first_name") or None),
                            last_name=(raw.get("last_name") or None),
                            display_name=(raw.get("display_name") or None),
                            position=(raw.get("position") or None),
                        )
                elif kind == "reset":
                    await _reset_state(state)
                else:
                    logger.warning(
                        "test-analyst: unknown ws message type {!r}", kind,
                    )
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass

    return app


async def _forward_events(ws: WebSocket, subscription: AsyncIterator[AgentTraceEvent]) -> None:
    try:
        async for event in subscription:
            try:
                await ws.send_json(event.to_json())
            except Exception:
                # Connection closed; stop forwarding.
                break
    except asyncio.CancelledError:
        pass


async def _run_task(state: TestAnalystState, raw: dict[str, Any]) -> None:
    """Insert a synthetic task row, then run the analyst against it.

    On READY → emit ``orchestrator: task_done``. On CLARIFYING the
    orchestrator will DM into chat, which the operator sees on the
    right panel.
    """
    external_id = str(raw.get("external_id") or "DM-TEST")
    title = str(raw.get("title") or "(no title)")
    description = str(raw.get("description") or "")

    await state.trace.emit(AgentTraceEvent(
        type="orchestrator", agent_key="test-runner",
        payload={"action": "task_started", "external_id": external_id},
    ))

    async with session_scope(state.session_factory) as session:
        # Replace any existing task with the same id.
        from sqlalchemy import select
        existing = (await session.execute(
            select(TaskRow).where(
                TaskRow.tracker == "jira",
                TaskRow.external_id == external_id,
            )
        )).scalar_one_or_none()
        if existing is not None:
            existing.title = title
            existing.description = description
            existing.internal_status = TaskStatus.DISCOVERED.value
        else:
            session.add(TaskRow(
                tracker="jira",
                external_id=external_id,
                title=title,
                description=description,
                url=f"https://jira.example/{external_id}",
                priority="medium",
                external_status="To Do",
                internal_status=TaskStatus.DISCOVERED.value,
                reporter_id="test-user",
            ))

    try:
        plan = await state.analyst.handle_task("jira", external_id)
    except Exception as exc:
        logger.exception("test-analyst: handle_task crashed")
        await state.trace.emit(AgentTraceEvent(
            type="orchestrator", agent_key="test-runner",
            payload={"action": "task_failed", "detail": str(exc)[:300]},
        ))
        return

    if plan is None:
        await state.trace.emit(AgentTraceEvent(
            type="orchestrator", agent_key="test-runner",
            payload={"action": "task_done", "detail": "skipped (no plan)"},
        ))
        return

    await state.trace.emit(AgentTraceEvent(
        type="orchestrator", agent_key="test-runner",
        payload={
            "action": "plan_received",
            "status": plan.status.value,
            "summary": plan.summary,
            "open_questions": [
                {"q": q.question, "ask_whom": q.ask_whom or "", "why": q.why_it_matters}
                for q in plan.open_questions
            ],
            "target_repo": plan.target_repo_key,
        },
    ))

    if plan.status == PlanStatus.CLARIFYING and plan.open_questions:
        await _kick_clarifications(state, external_id, plan)
    else:
        await state.trace.emit(AgentTraceEvent(
            type="orchestrator", agent_key="test-runner",
            payload={
                "action": "task_done",
                "detail": f"plan {plan.status.value}; "
                          f"{len(plan.steps)} steps, "
                          f"{len(plan.open_questions)} open questions",
            },
        ))


async def _kick_clarifications(
    state: TestAnalystState, external_id: str, plan: object,
) -> None:
    """Spawn questions in the orchestrator after Analyst returns CLARIFYING."""
    from sqlalchemy import select

    async with state.session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(
                TaskRow.tracker == "jira",
                TaskRow.external_id == external_id,
            )
        )).scalar_one_or_none()
        plan_row = (await session.execute(
            select(PlanRow)
            .where(
                PlanRow.tracker == "jira",
                PlanRow.task_external_id == external_id,
                PlanRow.status != PlanStatus.SUPERSEDED.value,
            )
            .order_by(PlanRow.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    if task_row is None or plan_row is None:
        await state.trace.emit(AgentTraceEvent(
            type="orchestrator", agent_key="test-runner",
            payload={"action": "task_failed", "detail": "task or plan row missing"},
        ))
        return

    sent = await state.goal_orchestrator.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,  # type: ignore[arg-type]
    )
    await state.trace.emit(AgentTraceEvent(
        type="orchestrator", agent_key="test-runner",
        payload={
            "action": "goals_created",
            "detail": f"{sent} goal(s) spawned; planner running for each",
        },
    ))


async def _drive_chat_inbox(state: TestAnalystState) -> None:
    """Replicate MmThreadListener's clarification-fragment routing.

    Drains state.chat.subscribe() — which yields user-side messages —
    and feeds each into the orchestrator's append_fragment via the same
    thread/channel lookup MmThreadListener uses.
    """
    sub = state.chat.subscribe()
    try:
        async for event in sub:
            if event.trusted:
                continue
            try:
                await _route_user_event(state, event)
            except Exception:
                logger.exception("test-analyst: route_user_event failed")
    except asyncio.CancelledError:
        pass


async def _route_user_event(state: TestAnalystState, event: ChatMessage) -> None:
    goal = None
    if event.thread_root_id:
        goal = await state.goal_orchestrator.find_goal_by_thread(
            event.thread_root_id,
        )
    if goal is None:
        goal = await state.goal_orchestrator.find_goal_by_channel(
            mm_channel_id=event.channel_id,
            mm_user_id=event.author_id,
        )
    if goal is None:
        await state.trace.emit(AgentTraceEvent(
            type="orchestrator", agent_key="test-runner",
            payload={
                "action": "user_message_unrouted",
                "detail": f"channel={event.channel_id} author={event.author_id} "
                          f"thread={event.thread_root_id}",
            },
        ))
        return
    await state.goal_orchestrator.append_fragment(goal.id, event)


async def _reset_state(state: TestAnalystState) -> None:
    """Wipe questions/fragments/answers/tasks/plans for a fresh run.

    We keep the same ``InMemoryChat`` instance — CommunicatorService and
    the orchestrator have a reference to it baked in at startup. Clearing
    the DB is enough to make the next ``run_task`` start from scratch.
    """
    from sqlalchemy import delete

    from virtual_dev.infrastructure.db import (
        AgentMessageRow,
        GoalFragmentRow,
        GoalRow,
        GoalStepRow,
        PlanRow,
        TaskRow,
    )

    async with session_scope(state.session_factory) as session:
        for cls in (
            GoalFragmentRow,
            GoalStepRow,
            GoalRow,
            PlanRow,
            TaskRow,
            AgentMessageRow,
        ):
            await session.execute(delete(cls))
    # Wipe trace history so the next page load doesn't replay old
    # events. The reset event itself stays — it's the new "first
    # entry" so it's clear what happened.
    state.trace.clear()
    await state.trace.emit(AgentTraceEvent(
        type="orchestrator", agent_key="test-runner",
        payload={"action": "reset", "detail": "DB + trace history wiped"},
    ))


__all__ = ["build_test_analyst_app"]
