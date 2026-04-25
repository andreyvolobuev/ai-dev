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
from virtual_dev.application.agents.devops import DevOpsAgent
from virtual_dev.application.agents.reviewer import ReviewerAgent
from virtual_dev.application.agents.thread_responder import ThreadResponderAgent
from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    PromptsLoader,
    ResearcherToolkit,
    RulesLoader,
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
from virtual_dev.infrastructure.config import AppConfig, Settings, load_config
from virtual_dev.infrastructure.db import Base, make_engine, make_session_factory


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
    reviewer: ReviewerAgent
    devops: DevOpsAgent
    thread_responder: ThreadResponderAgent

    async def init_db(self) -> None:
        """Create all tables. Used by ``virtual-dev db init``."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("DB initialised at {}", self.settings.db_url)

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

    chat: ChatPort | None = None
    if settings.mattermost_url and settings.mattermost_token:
        chat = MattermostChat(
            url=settings.mattermost_url,
            token=settings.mattermost_token,
            bot_username=settings.mattermost_bot_username or None,
            ssl_verify=settings.mattermost_ssl_verify,
            ssl_ca_file=settings.mattermost_ssl_ca_file or None,
        )
    else:
        logger.warning("Mattermost credentials missing — chat disabled")

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

    code_agent: CodeAgentPort = ClaudeAgentSdkCodeAgent(
        default_model=config.agents.models.default,
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

    # bot_username here is the GitLab username — comments authored by that
    # user on our MRs are our own. Primary signal inside the agent is still
    # the per-MR author_username (which matches by definition); this is a
    # secondary safety net for cases where another bot account replies.
    reviewer = ReviewerAgent(
        vcs=vcs,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        message_bus=message_bus,
        bot_username=None,   # relying on the MR.author_username signal
    )
    devops = DevOpsAgent(
        vcs=vcs,
        communicator=communicator,
        session_factory=session_factory,
        config=config,
        message_bus=message_bus,
    )
    thread_responder = ThreadResponderAgent(
        code_agent=code_agent,
        config=config,
        injection_filter=injection_filter,
        prompts_loader=prompts_loader,
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
        reviewer=reviewer,
        devops=devops,
        thread_responder=thread_responder,
    )


def _host(url: str) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname
