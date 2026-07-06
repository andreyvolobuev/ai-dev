"""Alembic environment.

The DB URL comes from ``alembic.ini``'s ``sqlalchemy.url`` option which
``build_alembic_config()`` populates at runtime (always in sync-driver
form). PRAGMA tuning (WAL, busy_timeout, FK) is reapplied on each
SQLite connection so migrations behave the same way as the live engine.
PostgreSQL doesn't need PRAGMAs; ``render_as_batch`` is SQLite-only
(batch mode for ALTER TABLE).

``virtual_dev`` is importable because ``alembic.ini`` sets
``prepend_sys_path = .`` and we run from the repo root.
"""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from virtual_dev.infrastructure.db import Base
from virtual_dev.infrastructure.db import models as _models  # noqa: F401  (register tables)

config = context.config
target_metadata = Base.metadata


def _attach_sqlite_pragma(connection: Any) -> None:
    """Same PRAGMAs as the live engine — see infrastructure/db/base.py."""
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def run_migrations_offline() -> None:
    """Generate SQL without connecting — useful for ``alembic upgrade --sql``."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    url = (config.get_main_option("sqlalchemy.url") or "").lower()
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:

        @event.listens_for(connectable, "connect")
        def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
            _attach_sqlite_pragma(dbapi_connection)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,  # SQLite needs ALTER via batch mode
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
