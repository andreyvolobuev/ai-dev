"""FastAPI dashboard.

Phase 2 wiring adds a Dev-agent runner alongside the orchestrator and
Analyst runner. All three live inside the same ``lifespan`` and share
the event loop. Dev-agent is only started when VCS is configured; the
dashboard reports the missing-VCS state so the operator can tell why
nothing is coding.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import select

from virtual_dev.application.agents import AnalystAgent, Orchestrator
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    TOPIC_TASK_DISCOVERED,
)
from virtual_dev.infrastructure.container import Container
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan
from virtual_dev.runtime.workers import (
    AgentRunner,
    AnalystInbox,
    DevInbox,
    MmThreadListener,
    PollerWorker,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(container: Container, *, start_scheduler: bool = True) -> FastAPI:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    orchestrator = Orchestrator(
        task_tracker=container.task_tracker,
        session_factory=container.session_factory,
        config=container.config,
        message_bus=container.message_bus,
    )

    analyst = AnalystAgent(
        code_agent=container.code_agent,
        researcher=container.researcher,
        communicator=container.communicator,
        session_factory=container.session_factory,
        config=container.config,
        settings=container.settings,
        prompts_loader=container.prompts_loader,
        confluence_host=container.confluence_host,
        mattermost_host=container.mattermost_host,
        gitlab_host=container.gitlab_host,
    )
    analyst_inbox = AnalystInbox(
        analyst=analyst,
        task_tracker=container.task_tracker,
        config=container.config,
        message_bus=container.message_bus,
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

    mm_listener: MmThreadListener | None = None
    if container.chat is not None and container.vcs is not None:
        mm_listener = MmThreadListener(
            chat=container.chat,
            communicator=container.communicator,
            responder=container.thread_responder,
            dev_agents=container.dev_agents,
            session_factory=container.session_factory,
            config=container.config,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        background: list[asyncio.Task[None]] = []
        if start_scheduler:
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
            if mm_listener is not None:
                background.append(asyncio.create_task(
                    mm_listener.run_forever(), name="mm-thread-listener",
                ))
            logger.info(
                "Started: orchestrator + analyst-runner + {} dev runner(s) "
                "+ reviewer/devops pollers + mm-thread-listener={}",
                len(dev_runners), mm_listener is not None,
            )
        try:
            yield
        finally:
            await orchestrator.stop()
            await analyst_runner.stop()
            for runner in dev_runners:
                await runner.stop()
            await reviewer_poller.stop()
            await devops_poller.stop()
            if mm_listener is not None:
                await mm_listener.stop()
            for task in background:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except asyncio.TimeoutError:
                    task.cancel()
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
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        return templates.TemplateResponse(
            request, "task.html",
            {
                "task": row,
                "plans": [row_to_plan(p) for p in plans],
                "mrs": mrs,
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

    @app.post("/kill")
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

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", **_status_block()}

    return app
