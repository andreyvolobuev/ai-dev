"""DSN ssl-param normalisation for asyncpg vs psycopg2.

Motivating bug: a single ``DB_DSN`` in ``.env`` has to serve both the
async runtime (asyncpg) and Alembic (psycopg2, sync). asyncpg rejects
libpq's ``sslmode`` kwarg, psycopg2 rejects a bare ``ssl`` kwarg. Rather
than force operators to keep two DSNs in sync, we rewrite the param per
driver via ``normalize_pg_ssl``.
"""

from __future__ import annotations

from virtual_dev.infrastructure.db.base import normalize_pg_ssl
from virtual_dev.infrastructure.db.migrations import _to_sync_url


def test_sslmode_to_ssl_for_asyncpg() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?sslmode=require"
    out = normalize_pg_ssl(url, target="asyncpg")
    assert out == "postgresql+asyncpg://u:p@h:5432/db?ssl=require"


def test_ssl_to_sslmode_for_psycopg2() -> None:
    url = "postgresql://u:p@h:5432/db?ssl=require"
    out = normalize_pg_ssl(url, target="psycopg2")
    assert out == "postgresql://u:p@h:5432/db?sslmode=require"


def test_asyncpg_leaves_existing_ssl_untouched() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
    assert normalize_pg_ssl(url, target="asyncpg") == url


def test_psycopg2_leaves_existing_sslmode_untouched() -> None:
    url = "postgresql://u:p@h:5432/db?sslmode=require"
    assert normalize_pg_ssl(url, target="psycopg2") == url


def test_extra_params_preserved() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?sslmode=require&application_name=bot"
    out = normalize_pg_ssl(url, target="asyncpg")
    assert "ssl=require" in out
    assert "application_name=bot" in out
    assert "sslmode" not in out


def test_no_query_is_noop() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db"
    assert normalize_pg_ssl(url, target="asyncpg") == url


def test_non_postgres_url_is_noop() -> None:
    url = "sqlite+aiosqlite:///./data/x.db?sslmode=require"
    assert normalize_pg_ssl(url, target="asyncpg") == url


def test_to_sync_url_strips_async_driver_and_maps_ssl() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
    assert _to_sync_url(url) == "postgresql://u:p@h:5432/db?sslmode=require"


def test_to_sync_url_keeps_sslmode_for_alembic() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?sslmode=require"
    assert _to_sync_url(url) == "postgresql://u:p@h:5432/db?sslmode=require"


def test_to_sync_url_sqlite_untouched() -> None:
    assert _to_sync_url("sqlite+aiosqlite:///./data/x.db") == "sqlite:///./data/x.db"