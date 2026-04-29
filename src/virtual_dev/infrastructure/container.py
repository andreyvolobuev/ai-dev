"""Dependency-injection container.

Simple hand-rolled wiring. The container is built once at application startup
from YAML + env, stored on the FastAPI app state, and handed to agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from virtual_dev.adapters.chat import MattermostChat
from virtual_dev.adapters.code_agent import ClaudeAgentSdkCodeAgent
from virtual_dev.adapters.embedder import FastembedEmbedder
from virtual_dev.adapters.knowledge_base import ConfluenceKb
from virtual_dev.adapters.llm import ClaudeAgentSdkLlm
from virtual_dev.adapters.message_bus import SqliteMessageBus
from virtual_dev.adapters.mr_history import LocalMrHistory
from virtual_dev.adapters.secrets import EnvSecrets
from virtual_dev.adapters.task_tracker import JiraTaskTracker
from virtual_dev.adapters.vcs import GitIdentity, GitLabVcs
from virtual_dev.application.agents.dev import DevAgent
from virtual_dev.application.agents.devops import DevOpsAgent
from virtual_dev.application.agents.orchestrator import dev_agent_key
from virtual_dev.application.agents.reviewer import ReviewerAgent
from virtual_dev.application.agents.thread_responder import ThreadResponderAgent
from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    PromptsLoader,
    ResearcherToolkit,
    RulesLoader,
)
from virtual_dev.application.services.agent_trace import AgentTrace
from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
)
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.code_agent import CodeAgentPort
from virtual_dev.domain.ports.embedder import EmbedderPort
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.domain.ports.llm import LlmPort
from virtual_dev.domain.ports.message_bus import MessageBusPort
from virtual_dev.domain.ports.mr_history import MrHistoryPort
from virtual_dev.domain.ports.secrets import SecretsPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import (
    AppConfig,
    Settings,
    apply_settings_overrides,
    load_config,
)
from virtual_dev.infrastructure.db import make_engine, make_session_factory
from virtual_dev.infrastructure.db.migrations import upgrade_to_head


@dataclass
class Container:
    """Bag of wired-up singletons.

    Third-party adapters (``task_tracker``, ``chat``, ``knowledge_base``) are
    optional: we degrade gracefully when their credentials are absent so the
    dashboard and ``db init`` can run on a fresh clone. The orchestrator and
    analyst log a warning and no-op when a dependency is missing.
    """

    settings: Settings
    config: AppConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    secrets: SecretsPort
    message_bus: MessageBusPort
    task_tracker: TaskTrackerPort | None
    chat: ChatPort | None
    knowledge_base: KnowledgeBasePort | None
    vcs: VcsPort | None
    embedder: EmbedderPort
    mr_history: MrHistoryPort | None
    code_agent: CodeAgentPort
    llm: LlmPort
    injection_filter: InjectionFilter
    researcher: ResearcherToolkit
    communicator: CommunicatorService
    rules_loader: RulesLoader
    prompts_loader: PromptsLoader
    dev_agents: dict[str, DevAgent]   # repo_key → DevAgent (backend specialisation)
    reviewer: ReviewerAgent
    devops: DevOpsAgent
    thread_responder: ThreadResponderAgent
    # Phase 5.0: analyst is the only agent. Session state per ticket
    # lives on TaskRow + analyst_conversation_steps.
    analyst_session_repo: AnalystSessionRepository
    # Always-on event broadcaster. Subscribed by the log sink in the
    # web app's lifespan so DEBUG logs mirror the test-analyst UI feed.
    trace: AgentTrace

    async def init_db(self) -> None:
        """Apply Alembic migrations to head. Used by ``virtual-dev db init``.

        Idempotent on an already-migrated DB. Alembic is sync; we run it
        in a worker thread so we don't block the event loop. Migrations
        manage their own connections, so we don't share ``self.engine``.
        """
        import asyncio

        await asyncio.to_thread(upgrade_to_head, self.settings.db_url)
        logger.info("DB migrated to head at {}", self.settings.db_url)

    async def dispose(self) -> None:
        await self.engine.dispose()

    # Host-only forms (for the link extractor).
    @property
    def confluence_host(self) -> str | None:
        return _host(self.settings.confluence_url)

    @property
    def mattermost_host(self) -> str | None:
        return _host(self.settings.mattermost_url)

    @property
    def gitlab_host(self) -> str | None:
        return _host(self.settings.gitlab_url)


def build_container(config_dir: Path | str = "config") -> Container:
    """Assemble a :class:`Container` from YAML configs + env.

    Adapters whose env credentials are missing are skipped. The core stack
    (DB, message bus, code agent, injection filter, researcher, communicator)
    is always built so agents can run offline in test/dev loops.
    """
    settings = Settings()
    config = load_config(config_dir)
    apply_settings_overrides(config, settings)

    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)

    secrets = EnvSecrets()
    message_bus: MessageBusPort = SqliteMessageBus(session_factory)

    task_tracker: TaskTrackerPort | None = None
    if settings.jira_url and settings.jira_token:
        task_tracker = JiraTaskTracker(
            url=settings.jira_url,
            token=settings.jira_token,
            user=settings.jira_user,   # kept for reference; not used for Bearer auth
        )
    else:
        logger.warning(
            "Jira credentials are incomplete (JIRA_URL or JIRA_TOKEN missing) — task tracker disabled"
        )

    chat: ChatPort | None = _build_chat_adapter(settings)

    knowledge_base: KnowledgeBasePort | None = None
    if settings.confluence_url and settings.confluence_user and settings.confluence_token:
        knowledge_base = ConfluenceKb(
            url=settings.confluence_url,
            user=settings.confluence_user,
            token=settings.confluence_token,
        )
    else:
        logger.warning("Confluence credentials missing — KB disabled")

    vcs: VcsPort | None = None
    gitlab_bot_username: str | None = None
    if settings.gitlab_url and settings.gitlab_token:
        vcs = GitLabVcs(
            config=config,
            gitlab_url=settings.gitlab_url,
            gitlab_token=settings.gitlab_token,
            workspaces_dir=settings.workspaces_dir,
            identity=GitIdentity(
                name=settings.dev_git_author_name,
                email=settings.dev_git_author_email,
            ),
        )
        # Resolve our own GitLab username so the Reviewer filters out the
        # bot's own MR comments instead of feeding them back through the
        # ThreadResponder (#13 in techdebt).
        try:
            client = vcs._client   # type: ignore[attr-defined]
            client.auth()
            gitlab_bot_username = (
                str(client.user.username) if client.user else None  # type: ignore[union-attr]
            ) or None
            if gitlab_bot_username:
                logger.info("GitLab bot username resolved: @{}", gitlab_bot_username)
        except Exception:
            logger.exception("could not resolve GitLab bot username; reviewer filter degraded")
    else:
        logger.warning(
            "GitLab credentials missing — VCS disabled; Dev-agent will not run"
        )

    # Embedder is always constructed (the ONNX model is loaded lazily on first
    # embed call, so we pay no price if nobody indexes MRs).
    embedder: EmbedderPort = FastembedEmbedder()
    mr_history: MrHistoryPort | None = None
    if vcs is not None:
        mr_history = LocalMrHistory(
            session_factory=session_factory, vcs=vcs, embedder=embedder,
        )
    else:
        logger.info("MR-history index disabled (VCS not configured)")

    trace = AgentTrace()
    code_agent: CodeAgentPort = ClaudeAgentSdkCodeAgent(
        default_model=config.agents.models.default,
        trace=trace,
    )
    llm: LlmPort = ClaudeAgentSdkLlm()

    injection_filter = InjectionFilter()
    researcher = ResearcherToolkit(
        config=config,
        workspaces_dir=settings.workspaces_dir,
        knowledge_base=knowledge_base,
        injection_filter=injection_filter,
        mr_history=mr_history,
    )
    communicator_cfg = config.agents.agents.get("communicator")
    communicator_rate = (
        communicator_cfg.rate_limit_per_hour
        if communicator_cfg and communicator_cfg.rate_limit_per_hour
        else 20
    )
    communicator = CommunicatorService(
        chat,
        injection_filter,
        working_hours=config.agents.working_hours,
        rate_limit_per_hour=communicator_rate,
        respect_working_hours=settings.communicator_respect_working_hours,
    )
    rules_loader = RulesLoader(Path(config_dir) / "rules")
    prompts_loader = PromptsLoader(Path(config_dir) / "prompts")

    # Build Dev-agents first so DevOps + Reviewer can share the same
    # instances for iteration dispatch.
    dev_agents: dict[str, DevAgent] = {}
    if vcs is not None:
        for repo in config.repositories:
            if not repo.agents.backend:
                continue
            dev_agents[repo.key] = DevAgent(
                agent_key=dev_agent_key(repo.key, "backend"),
                repo_key=repo.key,
                specialisation="backend",
                vcs=vcs,
                code_agent=code_agent,
                rules_loader=rules_loader,
                prompts_loader=prompts_loader,
                session_factory=session_factory,
                config=config,
                settings=settings,
                researcher=researcher if mr_history else None,
            )

    thread_responder = ThreadResponderAgent(
        code_agent=code_agent,
        config=config,
        injection_filter=injection_filter,
        prompts_loader=prompts_loader,
    )

    # Phase 5.0: analyst is the only agent.
    analyst_session_repo = AnalystSessionRepository(
        session_factory=session_factory,
    )

    # bot_username here is the GitLab username — comments authored by that
    # user on our MRs are our own. Primary signal inside the agent is still
    # the per-MR author_username (which matches by definition); this is a
    # secondary safety net for cases where another bot account replies.
    # Phase 4: Reviewer also routes actionable GitLab MR comments through
    # the ThreadResponder (and Dev for iterations) so feedback in GitLab
    # gets a response in GitLab, mirroring the MM-thread flow.
    reviewer = ReviewerAgent(
        vcs=vcs,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        message_bus=message_bus,
        bot_username=gitlab_bot_username,
        responder=thread_responder,
        dev_agents=dict(dev_agents),
    )

    devops = DevOpsAgent(
        vcs=vcs,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        dev_agents=dev_agents,
        message_bus=message_bus,
    )

    return Container(
        settings=settings,
        config=config,
        engine=engine,
        session_factory=session_factory,
        secrets=secrets,
        message_bus=message_bus,
        task_tracker=task_tracker,
        chat=chat,
        knowledge_base=knowledge_base,
        vcs=vcs,
        embedder=embedder,
        mr_history=mr_history,
        code_agent=code_agent,
        llm=llm,
        injection_filter=injection_filter,
        researcher=researcher,
        communicator=communicator,
        rules_loader=rules_loader,
        prompts_loader=prompts_loader,
        dev_agents=dev_agents,
        reviewer=reviewer,
        devops=devops,
        thread_responder=thread_responder,
        analyst_session_repo=analyst_session_repo,
        trace=trace,
    )


def _host(url: str) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname


def _build_chat_adapter(settings: Settings) -> "ChatPort | None":
    """Pick the chat adapter from ``CHAT_PROVIDER`` env. Defaults to
    Mattermost. Slack/Telegram raise NotImplementedError until adapters
    land — keep the env knob so swapping is config-only when they do.
    """
    provider = (settings.chat_provider or "mattermost").lower()
    if provider == "mattermost":
        if not (settings.mattermost_url and settings.mattermost_token):
            logger.warning("Mattermost credentials missing — chat disabled")
            return None
        return MattermostChat(
            url=settings.mattermost_url,
            token=settings.mattermost_token,
            bot_username=settings.mattermost_bot_username or None,
            ssl_verify=settings.mattermost_ssl_verify,
            ssl_ca_file=settings.mattermost_ssl_ca_file or None,
        )
    if provider in ("slack", "telegram"):
        raise NotImplementedError(
            f"chat_provider={provider!r} is configurable but no adapter "
            f"is wired yet. Set CHAT_PROVIDER=mattermost or implement "
            f"the adapter under src/virtual_dev/adapters/chat/."
        )
    raise ValueError(
        f"Unknown CHAT_PROVIDER={provider!r}. "
        f"Expected one of: mattermost, slack, telegram."
    )
