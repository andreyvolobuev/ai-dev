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

from virtual_dev.application.agents import AnalystAgent, DevAgent, Orchestrator
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    TOPIC_TASK_DISCOVERED,
    dev_agent_key,
)
from virtual_dev.infrastructure import Container
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan
from virtual_dev.runtime.workers import AgentRunner, AnalystInbox, DevInbox

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
    analyst_inbox = AnalystInbox(
        analyst=analyst,
        task_tracker=container.task_tracker,
        agents_config=container.config.agents,
        message_bus=container.message_bus,
    )
    analyst_runner = AgentRunner(
        agent_key=AnalystAgent.agent_key,
        message_bus=container.message_bus,
        handlers={TOPIC_TASK_DISCOVERED: analyst_inbox.handle},
    )

    # Dev-agent: one per (repo, specialisation). Phase 2 = only backend on
    # whichever repos have backend=True in config. We enumerate here so the
    # dashboard shows each as a separate status line.
    dev_runners: list[AgentRunner] = []
    dev_agents: list[DevAgent] = []
    if container.vcs is not None:
        for repo in container.config.repositories:
            if not repo.agents.backend:
                continue
            dev = DevAgent(
                agent_key=dev_agent_key(repo.key, "backend"),
                repo_key=repo.key,
                specialisation="backend",
                vcs=container.vcs,
                code_agent=container.code_agent,
                rules_loader=container.rules_loader,
                session_factory=container.session_factory,
                config=container.config,
                settings=container.settings,
            )
            inbox = DevInbox(
                dev_agent=dev,
                task_tracker=container.task_tracker,
                agents_config=container.config.agents,
            )
            runner = AgentRunner(
                agent_key=dev.agent_key,
                message_bus=container.message_bus,
                handlers={TOPIC_PLAN_READY: inbox.handle},
            )
            dev_agents.append(dev)
            dev_runners.append(runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        background: list[asyncio.Task[None]] = []
        if start_scheduler:
            background.append(asyncio.create_task(orchestrator.run_forever(), name="orchestrator"))
            background.append(asyncio.create_task(analyst_runner.run_forever(), name="analyst-runner"))
            for runner in dev_runners:
                background.append(asyncio.create_task(runner.run_forever(), name=runner.agent_key))
            logger.info(
                "Started: orchestrator + analyst-runner + {} dev runner(s)",
                len(dev_runners),
            )
        try:
            yield
        finally:
            await orchestrator.stop()
            await analyst_runner.stop()
            for runner in dev_runners:
                await runner.stop()
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
        logger.warning("Kill-switch pressed via web")
        return {"status": "stopping"}

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", **_status_block()}

    return app
