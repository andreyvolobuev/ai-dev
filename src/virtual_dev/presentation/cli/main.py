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
    """Run one orchestrator iteration and exit.

    Useful for smoke-testing Jira creds. Any newly created tasks are also
    dispatched onto the message bus (task.discovered) so downstream agents
    can pick them up on the next ``virtual-dev run``.
    """
    _bootstrap()
    container = build_container()

    from virtual_dev.application.agents import Orchestrator

    orchestrator = Orchestrator(
        task_tracker=container.task_tracker,
        session_factory=container.session_factory,
        config=container.config,
        message_bus=container.message_bus,
    )

    async def _run() -> None:
        stats = await orchestrator.run_once()
        await container.dispose()
        console.print(
            f"[green]OK[/green]  fetched={stats.fetched}  "
            f"created={stats.created}  updated={stats.updated}  "
            f"dispatched={stats.dispatched}"
        )

    asyncio.run(_run())


@app.command("index-mrs")
def index_mrs(
    repo: str = typer.Option(..., "--repo", help="Repo key from repositories.yaml"),
    limit: int = typer.Option(500, help="Max number of merged MRs to index"),
) -> None:
    """Build / refresh the MR-history RAG index for a repo.

    Pulls the most recent merged MRs from GitLab, embeds title+description
    with a local multilingual model, stores them in the `mr_history` table.
    First run downloads the embedder model (~220MB) into
    ``~/.cache/fastembed``; subsequent runs are fast.
    """
    _bootstrap()
    container = build_container()
    if container.mr_history is None:
        console.print(
            "[red]MR-history index requires VCS (GitLab token) to be configured[/red]"
        )
        raise typer.Exit(code=1)

    async def _run() -> int:
        count = await container.mr_history.refresh(repo, limit=limit)
        await container.dispose()
        return count

    count = asyncio.run(_run())
    console.print(f"[green]Indexed {count} merged MRs for {repo}[/green]")


@app.command("dev-task")
def dev_task(
    external_id: str = typer.Argument(..., help="Tracker task id, e.g. DM-1234"),
    tracker: str = typer.Option("jira", help="Tracker name"),
    repo: str = typer.Option(
        ..., "--repo", help="Target repo key (must be in repositories.yaml)"
    ),
    specialisation: str = typer.Option("backend", "--spec", help="backend|frontend|devops"),
    post_to_tracker: bool = typer.Option(
        False, "--post/--no-post",
        help="If True, transition Jira + comment MR / failure.",
    ),
) -> None:
    """Run the Dev-agent on one ticket locally and exit.

    Requires VCS (GitLab token) configured. The task's latest READY plan
    is used; task.dor_satisfied must be True (set via dashboard or
    ``UPDATE tasks SET dor_satisfied = 1 ...``).
    """
    _bootstrap()
    container = build_container()

    if container.vcs is None:
        console.print("[red]VCS (GitLab) is not configured — cannot run Dev agent[/red]")
        raise typer.Exit(code=1)

    from virtual_dev.application.agents import DevAgent
    from virtual_dev.application.agents.orchestrator import dev_agent_key
    from virtual_dev.runtime.workers import DevInbox

    dev = DevAgent(
        agent_key=dev_agent_key(repo, specialisation),
        repo_key=repo,
        specialisation=specialisation,
        vcs=container.vcs,
        code_agent=container.code_agent,
        rules_loader=container.rules_loader,
        session_factory=container.session_factory,
        config=container.config,
        settings=container.settings,
    )
    inbox = DevInbox(
        dev_agent=dev,
        task_tracker=container.task_tracker if post_to_tracker else None,
        agents_config=container.config.agents,
        post_to_tracker=post_to_tracker,
    )

    async def _run() -> None:
        from virtual_dev.domain.ports.message_bus import AgentMessage
        from virtual_dev.application.agents.orchestrator import TOPIC_PLAN_READY

        await inbox.handle(AgentMessage(
            id="cli",
            from_agent="cli",
            to_agent=dev.agent_key,
            topic=TOPIC_PLAN_READY,
            payload={
                "tracker": tracker,
                "external_id": external_id,
                "repo_key": repo,
            },
        ))
        await container.dispose()

    asyncio.run(_run())
    console.print(f"[green]Dev-agent done for {tracker}:{external_id}[/green]")


@app.command("plan-task")
def plan_task(
    external_id: str = typer.Argument(..., help="Tracker task id, e.g. DM-1234"),
    tracker: str = typer.Option("jira", help="Tracker name"),
    post_to_tracker: bool = typer.Option(
        False, "--post/--no-post", help="If True, post the plan as a Jira comment."
    ),
) -> None:
    """Run the Analyst on one ticket and exit.

    Requires a Claude Code (Max) login for the `claude` CLI subprocess.
    By default it does NOT touch Jira — pass ``--post`` to comment the plan.
    """
    _bootstrap()
    container = build_container()

    from virtual_dev.application.agents import AnalystAgent
    from virtual_dev.runtime.workers import AnalystInbox

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
        task_tracker=container.task_tracker if post_to_tracker else None,
        agents_config=container.config.agents,
        post_to_tracker=post_to_tracker,
    )

    async def _run() -> None:
        from virtual_dev.domain.ports.message_bus import AgentMessage

        await inbox.handle(AgentMessage(
            id="cli",
            from_agent="cli",
            to_agent="analyst",
            topic="task.discovered",
            payload={"tracker": tracker, "external_id": external_id},
        ))
        await container.dispose()

    asyncio.run(_run())
    console.print(f"[green]Analyst done for {tracker}:{external_id}[/green]")


if __name__ == "__main__":
    app()
