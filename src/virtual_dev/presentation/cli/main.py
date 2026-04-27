"""Typer CLI: ``virtual-dev`` entry point."""

from __future__ import annotations

import asyncio

import typer
import uvicorn
from rich.console import Console

from virtual_dev.infrastructure.container import build_container
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


@app.command("test-analyst-ui")
def test_analyst_ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(8090, "--port", help="Bind port"),
    coalesce_seconds: int = typer.Option(
        30, "--coalesce-seconds",
        help="Idle window before LLM classifies a coalesced answer (production default is 600).",
    ),
) -> None:
    """Standalone web UI for iterating on the Analyst + clarification flow.

    Runs in-process — no Mattermost, no Jira, no GitLab. The browser
    page lets the operator paste a ticket description, click Run, and
    watch every Claude tool_use and prompt in real time. Clarification
    questions land in a mock chat panel.

    Useful for debugging Analyst behaviour without burning a real Jira
    ticket on every iteration.
    """
    _bootstrap()
    from virtual_dev.presentation.web.test_analyst_app import build_test_analyst_app

    fastapi_app = build_test_analyst_app(
        "config", coalesce_window_seconds=coalesce_seconds,
    )
    console.print(
        f"[bold]Test Analyst UI[/bold] on http://{host}:{port}\n"
        f"  • paste a ticket on the left\n"
        f"  • watch tool_uses + prompts in the middle\n"
        f"  • answer clarifying questions in the chat on the right\n"
        f"  • coalesce window: {coalesce_seconds}s of silence "
        f"before classifying the merged answer"
    )
    uvicorn.run(fastapi_app, host=host, port=port, log_config=None)


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


@app.command("review-mrs")
def review_mrs() -> None:
    """Run one ReviewerAgent tick against all open MRs in the DB.

    Fetches new human comments, checks approval counts, applies the
    escalation policy. Writes to Mattermost via Communicator (subject
    to rate-limit + working-hours policy).
    """
    _bootstrap()
    container = build_container()
    if container.vcs is None:
        console.print("[red]Reviewer requires VCS (GitLab token) to be configured[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        stats = await container.reviewer.tick()
        await container.dispose()
        console.print(
            f"[green]Reviewer tick[/green]  mrs={stats.mrs_checked}  "
            f"new_comments={stats.new_comments}  approvals_sent={stats.approvals_sent}  "
            f"pings={stats.pings_sent}  escalations={stats.escalations_sent}"
        )

    asyncio.run(_run())


@app.command("watch-ci")
def watch_ci() -> None:
    """Run one DevOpsAgent tick against all open MRs in the DB.

    Polls latest pipeline jobs; when a pipeline flips red, pings MM via
    Communicator. Idempotent: stays quiet on subsequent ticks with the
    same red status.
    """
    _bootstrap()
    container = build_container()
    if container.vcs is None:
        console.print("[red]DevOps requires VCS (GitLab token) to be configured[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        stats = await container.devops.tick()
        await container.dispose()
        console.print(
            f"[green]DevOps tick[/green]  mrs={stats.mrs_checked}  "
            f"failures_detected={stats.failures_detected}  "
            f"notifications_sent={stats.notifications_sent}"
        )

    asyncio.run(_run())


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
        prompts_loader=container.prompts_loader,
        session_factory=container.session_factory,
        config=container.config,
        settings=container.settings,
    )
    inbox = DevInbox(
        dev_agent=dev,
        task_tracker=container.task_tracker if post_to_tracker else None,
        config=container.config,
        post_to_tracker=post_to_tracker,
    )

    async def _run() -> None:
        from virtual_dev.domain.ports.message_bus import AgentMessage
        from virtual_dev.application.agents.orchestrator import TOPIC_PLAN_READY

        ok = await _ensure_task_in_db(container, tracker, external_id)
        if not ok:
            console.print(
                f"[red]Task {external_id} not found in DB and Jira is not configured "
                f"— run `virtual-dev poll-once` first or set Jira credentials.[/red]"
            )
            return

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

    Fetches the ticket from Jira (if configured) and stores it locally
    before running the Analyst — no need to run poll-once first.
    By default does NOT write to Jira; pass ``--post`` to comment the plan.
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
        prompts_loader=container.prompts_loader,
        confluence_host=container.confluence_host,
        mattermost_host=container.mattermost_host,
        gitlab_host=container.gitlab_host,
    )
    inbox = AnalystInbox(
        analyst=analyst,
        session_repo=container.analyst_session_repo,
        communicator=container.communicator,
        task_tracker=container.task_tracker if post_to_tracker else None,
        config=container.config,
        post_to_tracker=post_to_tracker,
        session_factory=container.session_factory,
    )

    async def _run() -> None:
        from virtual_dev.domain.ports.message_bus import AgentMessage

        ok = await _ensure_task_in_db(container, tracker, external_id)
        if not ok:
            console.print(
                f"[red]Task {external_id} not found in DB and Jira is not configured "
                f"— run `virtual-dev poll-once` first or set Jira credentials.[/red]"
            )
            return

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


async def _ensure_task_in_db(
    container: "Container",  # type: ignore[name-defined]  # imported lazily below
    tracker: str,
    external_id: str,
) -> bool:
    """Make sure the task is in the DB.

    If it is already there — great, nothing to do.
    If not and Jira is configured — fetch from Jira and upsert.
    If not and Jira is absent — return False so the caller can bail out.
    """
    from sqlalchemy import select

    from virtual_dev.infrastructure.db import TaskRow
    from virtual_dev.infrastructure.db.mappers import task_to_row, update_row_from_task
    from virtual_dev.infrastructure.db.base import session_scope

    async with container.session_factory() as session:
        existing = (await session.execute(
            select(TaskRow).where(
                TaskRow.tracker == tracker,
                TaskRow.external_id == external_id,
            )
        )).scalar_one_or_none()

    if existing is not None:
        return True

    if container.task_tracker is None:
        return False

    console.print(f"Task {external_id} not in DB, fetching from Jira...")
    task = await container.task_tracker.get_task(external_id)

    async with session_scope(container.session_factory) as session:
        # Double-check in case a concurrent run inserted it.
        current = (await session.execute(
            select(TaskRow).where(
                TaskRow.tracker == tracker,
                TaskRow.external_id == external_id,
            )
        )).scalar_one_or_none()
        if current is None:
            session.add(task_to_row(task))
        else:
            update_row_from_task(current, task)

    console.print(f"[green]Fetched {external_id} from Jira and stored locally[/green]")
    return True


clarifications_app = typer.Typer(help="Inspect clarification goals + history")
app.add_typer(clarifications_app, name="clarifications")


@clarifications_app.command("show")
def clarifications_show(
    external_id: str = typer.Argument(..., help="Tracker task id, e.g. DM-1234"),
    tracker: str = typer.Option("jira", help="Tracker name"),
) -> None:
    """Print the task-step timeline for one ticket.

    Phase 5.0: prints the analyst's conversation log for one ticket
    (every BOT_ASKED, HUMAN_REPLIED, run summary, etc.). One ticket =
    one analyst session.
    """
    _bootstrap()
    container = build_container()

    async def _run() -> None:
        repo = container.analyst_session_repo
        from sqlalchemy import select

        from virtual_dev.infrastructure.db import TaskRow
        from virtual_dev.infrastructure.db.base import session_scope

        async with session_scope(container.session_factory) as session:
            task_row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == tracker,
                    TaskRow.external_id == external_id,
                )
            )).scalar_one_or_none()
        if task_row is None:
            console.print(
                f"[yellow]No task {tracker}:{external_id}[/yellow]"
            )
            await container.dispose()
            return
        steps = await repo.list_steps(task_row.id)
        if not steps:
            console.print(
                f"[yellow]No conversation log for {tracker}:{external_id}[/yellow]"
            )
            await container.dispose()
            return

        from rich.tree import Tree

        header = (
            f"[bold]{tracker}:{external_id} — {task_row.title[:80]}[/bold]\n"
            f"  status: {task_row.internal_status}\n"
            f"  iterations: {task_row.analyst_iteration_count}"
        )
        if task_row.awaiting_post_id:
            header += (
                f"\n  awaiting reply from "
                f"@{task_row.awaiting_username or task_row.awaiting_user_id}"
            )
        tree = Tree(header)
        for s in steps:
            label = (
                f"[dim]\\[{s.seq}][/dim] "
                f"[bold]{s.kind.value}[/bold]"
            )
            if s.text:
                label += f"\n   {s.text[:300]}"
            tree.add(label)
        console.print(tree)
        await container.dispose()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
