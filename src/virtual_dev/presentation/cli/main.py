"""Typer CLI: ``virtual-dev`` entry point."""

from __future__ import annotations

import asyncio

import typer
import uvicorn
from rich.console import Console

from virtual_dev.infrastructure import build_container
from virtual_dev.infrastructure.logging import configure_logging
from virtual_dev.presentation.web import create_app

app = typer.Typer(no_args_is_help=True, help="Virtual Dev — AI developer for DataMining (2GIS)")
db_app = typer.Typer(help="Database management")
app.add_typer(db_app, name="db")

console = Console()


def _bootstrap() -> None:
    """Common init: logging before anything else so container warnings surface."""
    # pydantic-settings reads .env; logging level comes from there.
    from virtual_dev.infrastructure.config import Settings

    settings = Settings()
    configure_logging(settings.log_level)


@db_app.command("init")
def db_init() -> None:
    """Create SQLite tables."""
    _bootstrap()
    container = build_container()

    async def _run() -> None:
        await container.init_db()
        await container.dispose()

    asyncio.run(_run())
    console.print("[green]DB initialised[/green]")


@app.command("run")
def run(
    host: str | None = typer.Option(None, "--host", help="Override web host from .env"),
    port: int | None = typer.Option(None, "--port", help="Override web port from .env"),
) -> None:
    """Start dashboard + orchestrator scheduler in one process."""
    _bootstrap()
    container = build_container()

    bind_host = host or container.settings.web_host
    bind_port = port or container.settings.web_port

    fastapi_app = create_app(container, start_scheduler=True)

    console.print(
        f"[bold]Virtual Dev[/bold] starting on http://{bind_host}:{bind_port}"
    )
    uvicorn.run(fastapi_app, host=bind_host, port=bind_port, log_config=None)


@app.command("poll-once")
def poll_once() -> None:
    """Run one orchestrator iteration and exit. Useful for smoke-testing Jira creds."""
    _bootstrap()
    container = build_container()

    from virtual_dev.application.agents import Orchestrator

    orchestrator = Orchestrator(
        task_tracker=container.task_tracker,
        session_factory=container.session_factory,
        config=container.config,
    )

    async def _run() -> None:
        stats = await orchestrator.run_once()
        await container.dispose()
        console.print(
            f"[green]OK[/green]  fetched={stats.fetched}  "
            f"created={stats.created}  updated={stats.updated}"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    app()
