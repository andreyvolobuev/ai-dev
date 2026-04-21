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
from virtual_dev.adapters.knowledge_base import ConfluenceKb
from virtual_dev.adapters.llm import ClaudeAgentSdkLlm
from virtual_dev.adapters.message_bus import SqliteMessageBus
from virtual_dev.adapters.secrets import EnvSecrets
from virtual_dev.adapters.task_tracker import JiraTaskTracker
from virtual_dev.adapters.vcs import GitIdentity, GitLabVcs
from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    ResearcherToolkit,
    RulesLoader,
)
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.code_agent import CodeAgentPort
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.domain.ports.llm import LlmPort
from virtual_dev.domain.ports.message_bus import MessageBusPort
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
    code_agent: CodeAgentPort
    llm: LlmPort
    injection_filter: InjectionFilter
    researcher: ResearcherToolkit
    communicator: CommunicatorService
    rules_loader: RulesLoader

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
    if settings.jira_url and settings.jira_user and settings.jira_token:
        task_tracker = JiraTaskTracker(
            url=settings.jira_url,
            user=settings.jira_user,
            token=settings.jira_token,
        )
    else:
        logger.warning(
            "Jira credentials are incomplete (URL/user/token) — task tracker disabled"
        )

    chat: ChatPort | None = None
    if settings.mattermost_url and settings.mattermost_token:
        chat = MattermostChat(
            url=settings.mattermost_url,
            token=settings.mattermost_token,
            bot_username=settings.mattermost_bot_username or None,
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
    )
    communicator = CommunicatorService(chat, injection_filter)
    rules_loader = RulesLoader(Path(config_dir) / "rules")

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
        code_agent=code_agent,
        llm=llm,
        injection_filter=injection_filter,
        researcher=researcher,
        communicator=communicator,
        rules_loader=rules_loader,
    )


def _host(url: str) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname
