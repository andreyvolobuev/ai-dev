"""Helpers around Alembic — keep migration ergonomics in one place.

Production code path:
    container.init_db() -> upgrade_to_head(settings.db_url)

Tests + smoke:
    upgrade_to_head("sqlite:///some/path.db")

The helpers live here (not in ``migrations/env.py``) so application code
and tests can import them without dragging in the Alembic CLI/config
file resolution machinery beyond what's necessary.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _repo_root() -> Path:
    """Resolve the repo root from this file's location.

    ``src/virtual_dev/infrastructure/db/migrations.py`` → repo root is
    four parents up. We don't rely on CWD because tests / CLI / web
    can all be invoked from anywhere.
    """
    return Path(__file__).resolve().parents[4]


def _to_sync_url(db_url: str) -> str:
    """Strip async drivers; Alembic only speaks sync.

    ``sqlite+aiosqlite:///./data/foo.db`` -> ``sqlite:///./data/foo.db``
    ``postgresql+asyncpg://user:pass@host/db`` -> ``postgresql://user:pass@host/db``
    """
    return db_url.replace("+aiosqlite", "", 1).replace("+asyncpg", "", 1)


def build_alembic_config(db_url: str) -> Config:
    """Build an Alembic Config pointing at the repo's ``migrations/``
    directory, with the runtime DB URL injected (sync-driver form)."""
    cfg = Config(str(_repo_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_repo_root() / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _to_sync_url(db_url))
    return cfg


def upgrade_to_head(db_url: str) -> None:
    """Apply all pending migrations. Idempotent on an up-to-date DB."""
    command.upgrade(build_alembic_config(db_url), "head")
