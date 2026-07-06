"""convert all timestamp columns to timestamptz

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-06 12:00:00.000000

PostgreSQL's asyncpg driver is strict about timezone-aware vs
timezone-naive datetimes.  All ``DateTime`` columns were created as
``TIMESTAMP WITHOUT TIME ZONE``, but the application produces
tz-aware datetimes everywhere (``datetime.now(timezone.utc)``, Jira's
ISO-8601 with offset, etc.).  This migration converts every
``TIMESTAMP WITHOUT TIME ZONE`` column to ``TIMESTAMP WITH TIME ZONE``,
interpreting existing values as UTC.

On SQLite this is a no-op — SQLite stores datetimes as text regardless
of the timezone flag.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'timestamp without time zone'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I TYPE TIMESTAMPTZ '
                    'USING %I AT TIME ZONE ''UTC''',
                    r.table_name, r.column_name, r.column_name
                );
            END LOOP;
        END $$;
    """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'timestamp with time zone'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I TYPE TIMESTAMP '
                    'USING %I AT TIME ZONE ''UTC''',
                    r.table_name, r.column_name, r.column_name
                );
            END LOOP;
        END $$;
    """)
