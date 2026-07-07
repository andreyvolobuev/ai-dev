"""FastAPI dashboard.

Phase 2 wiring adds a Dev-agent runner alongside the orchestrator and
Analyst runner. All three live inside the same ``lifespan`` and share
the event loop. Dev-agent is only started when VCS is configured; the
dashboard reports the missing-VCS state so the operator can tell why
nothing is coding.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import select

from virtual_dev.application.agents import AnalystAgent, Orchestrator
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    TOPIC_TASK_DISCOVERED,
)
from virtual_dev.application.services.agent_trace import (
    AgentTraceEvent,
    consume_trace_to_logs,
)
from virtual_dev.infrastructure.container import Container
from virtual_dev.infrastructure.db import (
    AnalystConversationStepRow,
    MergeRequestRow,
    PlanRow,
    TaskRow,
)
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan
from virtual_dev.runtime.workers import (
    AgentRunner,
    AnalystInbox,
    DevInbox,
    MmThreadListener,
    PollerWorker,
    make_answer_coalescer_worker,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


async def _forward_trace(
    ws: WebSocket,
    subscription: AsyncIterator[AgentTraceEvent],
) -> None:
    """Forward AgentTrace events to a websocket as JSON. Stop quietly
    on disconnect or cancellation — the route's finally-block cleans up
    the subscriber slot."""
    try:
        async for event in subscription:
            try:
                await ws.send_json(event.to_json())
            except Exception:
                break
    except asyncio.CancelledError:
        pass


async def _drain_background_tasks(
    tasks: list[asyncio.Task[None]],
    *,
    timeout: float = 10.0,
) -> None:
    """Drain ``tasks`` under a single overall timeout.

    The previous shutdown serialised ``wait_for(task, timeout=5)`` per
    task — N hung workers blocked deploy for N x 5s. We instead wait on
    the whole batch: if it doesn't complete inside ``timeout``, cancel
    survivors and gather again so cancellation actually drains
    (otherwise asyncio leaks "Task exception was never retrieved"
    warnings on Ctrl+C).

    Returns once every task is ``done()``. Exceptions inside tasks are
    swallowed — they're already logged by the worker itself, and the
    shutdown path can't usefully react.
    """
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
        return
    except TimeoutError:
        pass

    for task in tasks:
        if not task.done():
            task.cancel()
    # Second wait absorbs the cancellation flush. Bounded by the same
    # window — any task ignoring cancel beyond this is wedged hard
    # enough that we'd rather kill the process than block uvicorn.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )


async def _prewarm_repo_clones(container: Container) -> None:
    """Clone every configured repo into ``workspaces/`` on startup.

    The Analyst is read-only and never clones on its own; the Researcher tools
    it relies on (``read_file`` / ``search_code``) read straight from the local
    checkout. On a fresh deploy that checkout doesn't exist yet — nothing
    populates it until a Dev-agent runs — so the Analyst would plan blind. We
    pre-clone here so it has real code from the first task.

    Runs as a background task, NOT awaited before uvicorn binds: cloning large
    monorepos can take minutes and blocking startup would trip the readiness
    probe. Clones run concurrently; each failure is logged and skipped, and the
    Analyst degrades gracefully (API-only) for any repo not yet cloned.
    """
    vcs = container.vcs
    if vcs is None:
        return
    repo_keys = [r.key for r in container.config.repositories]
    if not repo_keys:
        return

    async def _clone_one(repo_key: str) -> None:
        try:
            path = await vcs.ensure_clone(repo_key)
            logger.info("Pre-cloned {} → {}", repo_key, path)
        except Exception as exc:
            logger.warning(
                "Pre-clone of {} failed — Analyst runs API-degraded for it "
                "until the next attempt succeeds: {}",
                repo_key, exc,
            )

    logger.info("Pre-cloning {} repo(s) into workspaces/…", len(repo_keys))
    await asyncio.gather(*(_clone_one(k) for k in repo_keys))


def create_app(container: Container, *, start_scheduler: bool = True) -> FastAPI:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    orchestrator = Orchestrator(
        task_tracker=container.task_tracker,
        session_factory=container.session_factory,
        config=container.config,
        message_bus=container.message_bus,
        health=container.health,
    )

    analyst = AnalystAgent(
        code_agent=container.code_agent,
        researcher=container.researcher,
        communicator=container.communicator,
        session_factory=container.session_factory,
        config=container.config,
        settings=container.settings,
        prompts_loader=container.prompts_loader,
        task_tracker=container.task_tracker,
        confluence_host=container.confluence_host,
        mattermost_host=container.mattermost_host,
        gitlab_host=container.gitlab_host,
    )
    analyst_inbox = AnalystInbox(
        analyst=analyst,
        session_repo=container.analyst_session_repo,
        communicator=container.communicator,
        task_tracker=container.task_tracker,
        config=container.config,
        message_bus=container.message_bus,
        session_factory=container.session_factory,
        trace=container.trace,
    )
    analyst_runner = AgentRunner(
        agent_key=AnalystAgent.agent_key,
        message_bus=container.message_bus,
        handlers={TOPIC_TASK_DISCOVERED: analyst_inbox.handle},
    )

    # Dev-agents are constructed in the Container so that DevOps + the
    # MM thread listener can share them. Here we just wire each to a
    # DevInbox + bus runner so plan.ready messages reach them.
    dev_runners: list[AgentRunner] = []
    for repo_key, dev in container.dev_agents.items():
        inbox = DevInbox(
            dev_agent=dev,
            task_tracker=container.task_tracker,
            config=container.config,
        )
        runner = AgentRunner(
            agent_key=dev.agent_key,
            message_bus=container.message_bus,
            handlers={TOPIC_PLAN_READY: inbox.handle},
        )
        dev_runners.append(runner)

    reviewer_poller = PollerWorker(
        name="reviewer",
        interval_seconds=container.settings.review_poll_interval_seconds,
        ticks={"reviewer": container.reviewer.tick},
    )
    devops_poller = PollerWorker(
        name="devops",
        interval_seconds=container.settings.pipeline_poll_interval_seconds,
        ticks={"devops": container.devops.tick},
    )

    coalescer_poller = make_answer_coalescer_worker(
        orchestrator=analyst_inbox,
        interval_seconds=container.settings.answer_coalesce_poll_interval_seconds,
    )

    # Wider safety net than the bus's lease/redelivery: catches tasks
    # that got stuck in CODING after the bus message was already
    # acked (process killed in the gap, manual ack during debug,
    # FAILED outcome operator wants retried).
    recovery_poller = PollerWorker(
        name="recovery",
        interval_seconds=container.settings.recovery_sweep_interval_seconds,
        ticks={"sweep": container.recovery_service.sweep_stuck_tasks},
    )

    mm_listener: MmThreadListener | None = None
    mm_catchup_poller: PollerWorker | None = None
    if container.chat is not None and container.vcs is not None:
        mm_listener = MmThreadListener(
            chat=container.chat,
            communicator=container.communicator,
            responder=container.thread_responder,
            dev_agents=container.dev_agents,
            session_factory=container.session_factory,
            config=container.config,
            settings=container.settings,
            vcs=container.vcs,
            analyst_inbox=analyst_inbox,
        )
        # Catch-up tick — runs independently of the WS health, so
        # missed posts get replayed even if the listener's
        # subscription is in its backoff window.
        mm_catchup_poller = PollerWorker(
            name="mm-catchup",
            interval_seconds=container.settings.mm_catchup_poll_interval_seconds,
            ticks={"catch_up": mm_listener.catch_up},
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Apply Alembic migrations to head before starting any workers.
        # Idempotent — safe to run on every startup; runs in a worker
        # thread so the event loop isn't blocked.
        # Timeout guard: psycopg2 can hang indefinitely when the DB is
        # unreachable (no connect_timeout in DSN). We cancel after 30s so
        # uvicorn still binds and the readinessProbe can reflect the real
        # state, rather than keeping the port closed until Helm times out.
        try:
            await asyncio.wait_for(container.init_db(), timeout=30.0)
        except TimeoutError:
            logger.error("DB migration timed out (30 s) — starting without migrations")
        except Exception:
            logger.exception("DB migration failed — starting without migrations")

        background: list[asyncio.Task[None]] = []
        # Log sink: drains AgentTrace into loguru DEBUG so a prod log
        # at level=DEBUG mirrors the test-analyst UI feed (one line per
        # tool_use / llm_text / agent_started / orchestrator event,
        # tagged with the analyst-run correlation id).
        background.append(asyncio.create_task(
            consume_trace_to_logs(container.trace),
            name="agent-trace-log-sink",
        ))
        if start_scheduler:
            # Populate workspaces/ so the Analyst sees real code from task #1.
            # Background, non-blocking: must not delay uvicorn binding.
            if container.vcs is not None:
                background.append(asyncio.create_task(
                    _prewarm_repo_clones(container), name="prewarm-clones",
                ))
            background.append(asyncio.create_task(orchestrator.run_forever(), name="orchestrator"))
            background.append(asyncio.create_task(analyst_runner.run_forever(), name="analyst-runner"))
            for runner in dev_runners:
                background.append(asyncio.create_task(runner.run_forever(), name=runner.agent_key))
            if container.vcs is not None:
                background.append(asyncio.create_task(
                    reviewer_poller.run_forever(), name="reviewer-poller",
                ))
                background.append(asyncio.create_task(
                    devops_poller.run_forever(), name="devops-poller",
                ))
            background.append(asyncio.create_task(
                coalescer_poller.run_forever(), name="answer-coalescer-poller",
            ))
            background.append(asyncio.create_task(
                recovery_poller.run_forever(), name="recovery-poller",
            ))
            if mm_listener is not None:
                background.append(asyncio.create_task(
                    mm_listener.run_forever(), name="mm-thread-listener",
                ))
            if mm_catchup_poller is not None:
                background.append(asyncio.create_task(
                    mm_catchup_poller.run_forever(), name="mm-catchup-poller",
                ))
            logger.info(
                "Started: orchestrator + analyst-runner + {} dev runner(s) "
                "+ reviewer/devops/coalescer/mm-catchup pollers + mm-thread-listener={}",
                len(dev_runners), mm_listener is not None,
            )
        try:
            yield
        finally:
            # Signal everyone to stop, then drain in parallel under a
            # single timeout — the previous per-task wait_for serialised
            # shutdown for 5s × N workers.
            stop_signals = [
                orchestrator.stop(),
                analyst_runner.stop(),
                *(r.stop() for r in dev_runners),
                reviewer_poller.stop(),
                devops_poller.stop(),
                coalescer_poller.stop(),
                recovery_poller.stop(),
            ]
            if mm_listener is not None:
                stop_signals.append(mm_listener.stop())
            if mm_catchup_poller is not None:
                stop_signals.append(mm_catchup_poller.stop())
            await asyncio.gather(*stop_signals, return_exceptions=True)
            await _drain_background_tasks(background, timeout=10.0)
            await container.dispose()

    app = FastAPI(title="Virtual Dev", lifespan=lifespan)
    app.state.container = container
    app.state.orchestrator = orchestrator
    app.state.analyst_runner = analyst_runner
    app.state.dev_runners = dev_runners

    def _status_block() -> dict[str, object]:
        return {
            "orchestrator_running": orchestrator.is_running,
            "analyst_running": analyst_runner.is_running,
            "dev_runners": [
                {"key": r.agent_key, "running": r.is_running} for r in dev_runners
            ],
            "reviewer_poller_running": reviewer_poller.is_running,
            "devops_poller_running": devops_poller.is_running,
            "reviewer_stats": reviewer_poller.stats.__dict__,
            "devops_stats": devops_poller.stats.__dict__,
            "mm_listener_running": bool(mm_listener and mm_listener.is_running),
            "mm_listener_stats": mm_listener.stats.__dict__ if mm_listener else {},
            "jira_configured": container.task_tracker is not None,
            "chat_configured": container.chat is not None,
            "kb_configured": container.knowledge_base is not None,
            "vcs_configured": container.vcs is not None,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            rows = (
                await session.execute(
                    select(TaskRow).order_by(TaskRow.discovered_at.desc()).limit(200)
                )
            ).scalars().all()
        return templates.TemplateResponse(
            request, "index.html", {"tasks": rows, **_status_block()},
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: int) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            row = (
                await session.execute(select(TaskRow).where(TaskRow.id == task_id))
            ).scalar_one_or_none()
            plans: list[PlanRow] = []
            mrs: list[MergeRequestRow] = []
            if row is not None:
                plans = list((
                    await session.execute(
                        select(PlanRow)
                        .where(
                            PlanRow.tracker == row.tracker,
                            PlanRow.task_external_id == row.external_id,
                        )
                        .order_by(PlanRow.created_at.desc())
                    )
                ).scalars().all())
                mrs = list((
                    await session.execute(
                        select(MergeRequestRow)
                        .where(MergeRequestRow.task_external_id == row.external_id)
                        .order_by(MergeRequestRow.created_at.desc())
                    )
                ).scalars().all())
                # Phase 5.0: analyst conversation log per ticket.
                conv_steps = list((
                    await session.execute(
                        select(AnalystConversationStepRow)
                        .where(AnalystConversationStepRow.task_id == row.id)
                        .order_by(AnalystConversationStepRow.seq.asc())
                    )
                ).scalars().all())
                # Render as a single "conversation" entry the template
                # can show as a timeline.
                if conv_steps:
                    questions = [{"row": row, "steps": conv_steps}]
                else:
                    questions = []
            else:
                questions = []
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        return templates.TemplateResponse(
            request, "task.html",
            {
                "task": row,
                "plans": [row_to_plan(p) for p in plans],
                "mrs": mrs,
                "questions": questions,
            },
        )

    @app.get("/plans", response_class=HTMLResponse)
    async def plans_list(request: Request) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            rows = list((
                await session.execute(
                    select(PlanRow).order_by(PlanRow.created_at.desc()).limit(200)
                )
            ).scalars().all())
            task_lookup: dict[tuple[str, str], int] = {}
            if rows:
                stmt = select(TaskRow.id, TaskRow.tracker, TaskRow.external_id).where(
                    TaskRow.external_id.in_([r.task_external_id for r in rows]),
                )
                for tid, tr, ext in (await session.execute(stmt)).all():
                    task_lookup[(tr, ext)] = tid
        return templates.TemplateResponse(
            request, "plans.html",
            {"plans": rows, "task_lookup": task_lookup},
        )

    @app.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request) -> HTMLResponse:
        """Live agent-trace feed (websocket-driven)."""
        return templates.TemplateResponse(
            request, "activity.html", {**_status_block()},
        )

    @app.websocket("/ws/activity")
    async def ws_activity(ws: WebSocket) -> None:
        """Stream the shared ``AgentTrace`` to one browser tab.

        Reuses the same broadcaster that the test-analyst UI uses — new
        subscribers get the last ~500 events from the ring buffer first
        (so a refresh isn't blank), then live tool_use / llm_text /
        orchestrator events as they happen.
        """
        await ws.accept()
        sub = container.trace.subscribe()
        forwarder = asyncio.create_task(_forward_trace(ws, sub))
        try:
            while True:
                # Block on receive so disconnects are caught immediately;
                # the browser doesn't send anything meaningful, but
                # WebSocketDisconnect lands here.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forwarder

    @app.get("/mrs", response_class=HTMLResponse)
    async def mrs_list(request: Request) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            rows = list((
                await session.execute(
                    select(MergeRequestRow).order_by(MergeRequestRow.created_at.desc()).limit(200)
                )
            ).scalars().all())
            task_lookup: dict[str, int] = {}
            if rows:
                stmt = select(TaskRow.id, TaskRow.external_id).where(
                    TaskRow.external_id.in_([r.task_external_id for r in rows if r.task_external_id]),
                )
                for tid, ext in (await session.execute(stmt)).all():
                    task_lookup[ext] = tid
        return templates.TemplateResponse(
            request, "mrs.html", {"mrs": rows, "task_lookup": task_lookup},
        )

    def _require_admin(request: Request) -> None:
        """Guard destructive endpoints. Bearer token if configured, else
        loopback-only."""
        admin_token = (container.settings.admin_token or "").strip()
        if admin_token:
            header = request.headers.get("authorization", "")
            expected = f"Bearer {admin_token}"
            if header != expected:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="missing or invalid admin token",
                )
            return
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "admin endpoints require ADMIN_TOKEN when bound off "
                    "loopback"
                ),
            )

    @app.post("/kill", dependencies=[Depends(_require_admin)])
    async def kill() -> dict[str, str]:
        await orchestrator.stop()
        await analyst_runner.stop()
        for runner in dev_runners:
            await runner.stop()
        await reviewer_poller.stop()
        await devops_poller.stop()
        if mm_listener is not None:
            await mm_listener.stop()
        logger.warning("Kill-switch pressed via web")
        return {"status": "stopping"}

    @app.get("/health")
    async def health() -> str:
        return "ok"

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        # ``subsystems`` shows last-success per integration: when did
        # we last fetch from Jira / talk to GitLab. Operator looks
        # here to tell "alive but blind" from "fully healthy".
        snap = container.health.snapshot()
        subsystems = {
            name: {
                "last_success_at": s["last_success_at"].isoformat(),
                "seconds_since": s["seconds_since"],
            }
            for name, s in snap.items()
        }
        return {
            "status": "ok",
            "subsystems": subsystems,
            **_status_block(),
        }

    return app
