"""Dependency-injection container.

Simple hand-rolled wiring. The container is built once at application startup
from YAML + env, stored on the FastAPI app state, and handed to agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from virtual_dev.adapters.message_bus import InMemoryMessageBus
from virtual_dev.adapters.secrets import EnvSecrets
from virtual_dev.adapters.task_tracker import JiraTaskTracker
from virtual_dev.domain.ports.message_bus import MessageBusPort
from virtual_dev.domain.ports.secrets import SecretsPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig, Settings, load_config
from virtual_dev.infrastructure.db import Base, make_engine, make_session_factory


@dataclass
class Container:
    """Bag of wired-up singletons.

    ``task_tracker`` is optional because in Phase 0 we allow the dashboard and
    ``db init`` to run without any third-party credentials. The orchestrator
    logs a warning and no-ops when it's ``None``.
    """

    settings: Settings
    config: AppConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    secrets: SecretsPort
    message_bus: MessageBusPort
    task_tracker: TaskTrackerPort | None

    async def init_db(self) -> None:
        """Create all tables. Used by ``virtual-dev db init``."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("DB initialised at {}", self.settings.db_url)

    async def dispose(self) -> None:
        await self.engine.dispose()


def build_container(config_dir: Path | str = "config") -> Container:
    """Assemble a :class:`Container` from YAML configs + env.

    Adapters whose env credentials are missing are skipped. In Phase 0 this
    means the dashboard and ``db init`` work on a fresh clone without any
    tokens set; the orchestrator simply has nothing to poll.
    """
    settings = Settings()
    config = load_config(config_dir)

    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)

    secrets = EnvSecrets()
    message_bus = InMemoryMessageBus()

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

    return Container(
        settings=settings,
        config=config,
        engine=engine,
        session_factory=session_factory,
        secrets=secrets,
        message_bus=message_bus,
        task_tracker=task_tracker,
    )
