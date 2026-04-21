"""FastAPI dashboard.

Phase 1 wiring:
    * Orchestrator — polls the tracker, upserts tasks, publishes
      ``task.discovered`` to the bus.
    * Analyst runner — subscribes to the same bus, runs AnalystAgent,
      posts the resulting plan to Jira and updates task status.

Both are started from the ``lifespan`` context so the whole app shares one
event loop.
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
from virtual_dev.application.agents.orchestrator import TOPIC_TASK_DISCOVERED
from virtual_dev.infrastructure import Container
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan
from virtual_dev.runtime.workers import AgentRunner, AnalystInbox

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
        confluence_host=container.confluence_host,
        mattermost_host=container.mattermost_host,
        gitlab_host=container.gitlab_host,
    )
    inbox = AnalystInbox(
        analyst=analyst,
        task_tracker=container.task_tracker,
        agents_config=container.config.agents,
    )
    analyst_runner = AgentRunner(
        agent_key=AnalystAgent.agent_key,
        message_bus=container.message_bus,
        handlers={TOPIC_TASK_DISCOVERED: inbox.handle},
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        background: list[asyncio.Task[None]] = []
        if start_scheduler:
            background.append(asyncio.create_task(orchestrator.run_forever(), name="orchestrator"))
            background.append(asyncio.create_task(analyst_runner.run_forever(), name="analyst-runner"))
            logger.info("Scheduler and agent runners started")
        try:
            yield
        finally:
            await orchestrator.stop()
            await analyst_runner.stop()
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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            rows = (
                await session.execute(
                    select(TaskRow).order_by(TaskRow.discovered_at.desc()).limit(200)
                )
            ).scalars().all()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "tasks": rows,
                "orchestrator_running": orchestrator.is_running,
                "analyst_running": analyst_runner.is_running,
                "jira_configured": container.task_tracker is not None,
                "chat_configured": container.chat is not None,
                "kb_configured": container.knowledge_base is not None,
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: int) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            row = (
                await session.execute(select(TaskRow).where(TaskRow.id == task_id))
            ).scalar_one_or_none()
            plans: list[PlanRow] = []
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
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        return templates.TemplateResponse(
            request, "task.html",
            {"task": row, "plans": [row_to_plan(p) for p in plans]},
        )

    @app.get("/plans", response_class=HTMLResponse)
    async def plans_list(request: Request) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            rows = list((
                await session.execute(
                    select(PlanRow).order_by(PlanRow.created_at.desc()).limit(200)
                )
            ).scalars().all())
            # Look up task rows for linking.
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

    @app.post("/kill")
    async def kill() -> dict[str, str]:
        await orchestrator.stop()
        await analyst_runner.stop()
        logger.warning("Kill-switch pressed via web")
        return {"status": "stopping"}

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "orchestrator_running": orchestrator.is_running,
            "analyst_running": analyst_runner.is_running,
            "jira_configured": container.task_tracker is not None,
            "chat_configured": container.chat is not None,
            "kb_configured": container.knowledge_base is not None,
        }

    return app
