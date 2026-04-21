"""FastAPI dashboard for Phase 0.

The dashboard starts the Orchestrator loop via the ``lifespan`` hook so the
whole app runs in a single asyncio event loop.
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

from virtual_dev.application.agents import Orchestrator
from virtual_dev.infrastructure import Container
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(container: Container, *, start_scheduler: bool = True) -> FastAPI:
    """Build a FastAPI app bound to ``container``.

    ``start_scheduler=False`` is useful in tests / CLI subcommands that reuse
    the HTTP layer without wanting a background task.
    """
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    orchestrator = Orchestrator(
        task_tracker=container.task_tracker,
        session_factory=container.session_factory,
        config=container.config,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler_task: asyncio.Task[None] | None = None
        if start_scheduler:
            scheduler_task = asyncio.create_task(
                orchestrator.run_forever(), name="orchestrator"
            )
            logger.info("Scheduler started")
        try:
            yield
        finally:
            if scheduler_task is not None:
                await orchestrator.stop()
                try:
                    await asyncio.wait_for(scheduler_task, timeout=5)
                except asyncio.TimeoutError:
                    scheduler_task.cancel()
            await container.dispose()

    app = FastAPI(title="Virtual Dev", lifespan=lifespan)
    app.state.container = container
    app.state.orchestrator = orchestrator

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
                "jira_configured": container.task_tracker is not None,
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: int) -> HTMLResponse:
        async with session_scope(container.session_factory) as session:
            row = (
                await session.execute(select(TaskRow).where(TaskRow.id == task_id))
            ).scalar_one_or_none()
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        return templates.TemplateResponse(request, "task.html", {"task": row})

    @app.post("/kill")
    async def kill() -> dict[str, str]:
        """Kill-switch stub. Wiring to real agents comes in Phase 1."""
        await orchestrator.stop()
        logger.warning("Kill-switch pressed via web")
        return {"status": "stopping"}

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "orchestrator_running": orchestrator.is_running,
            "jira_configured": container.task_tracker is not None,
        }

    return app
