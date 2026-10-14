"""SQLAlchemy base + engine/session factories.

Supports both PostgreSQL (production, via ``asyncpg``) and SQLite
(tests, via ``aiosqlite``). SQLite-specific PRAGMAs are applied
conditionally; PostgreSQL gets sensible pool defaults.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def normalize_pg_ssl(db_url: str, *, target: str) -> str:
    """Translate the ``sslmode``/``ssl`` query param for the given driver.

    libpq (psycopg2) speaks ``sslmode=require``; asyncpg speaks
    ``ssl=require``. One DSN has to serve both worlds — asyncpg at runtime
    and psycopg2 for Alembic — so each side rewrites the param it does not
    understand. ``target`` is ``"asyncpg"`` or ``"psycopg2"``.

    No-op for non-PostgreSQL URLs or when the relevant param is absent.
    """
    if not db_url.startswith("postgresql"):
        return db_url

    parsed = urlparse(db_url)
    if not parsed.query:
        return db_url

    params = dict(parse_qsl(parsed.query))
    if target == "asyncpg":
        # asyncpg rejects libpq's sslmode kwarg.
        if "sslmode" in params and "ssl" not in params:
            params["ssl"] = params.pop("sslmode")
    elif target == "psycopg2":
        # psycopg2 (libpq) rejects a bare `ssl` kwarg.
        if "ssl" in params and "sslmode" not in params:
            params["sslmode"] = params.pop("ssl")

    return urlunparse(parsed._replace(query=urlencode(params)))


def make_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine tuned for the dialect in ``db_url``.

    SQLite gets WAL + busy_timeout pragmas to avoid ``SQLITE_BUSY``
    under concurrent workers. PostgreSQL gets a connection pool sized
    for the bot's workload (~8 concurrent workers).
    """
    is_sqlite = db_url.startswith("sqlite")

    if is_sqlite:
        engine = create_async_engine(db_url, echo=echo, future=True)

        # Default journal_mode (DELETE) blocks readers on every write; with
        # ~8 workers polling the same DB this manifests as SQLITE_BUSY.
        # WAL lets readers proceed during writes, busy_timeout retries
        # contended writes for 5s instead of failing immediately.
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    else:
        engine = create_async_engine(
            normalize_pg_ssl(db_url, target="asyncpg"),
            echo=echo,
            future=True,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
        )

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session with automatic commit/rollback."""
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
