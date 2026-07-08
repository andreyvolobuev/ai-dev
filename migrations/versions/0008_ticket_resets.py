"""ticket_resets marker table

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-08 19:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_resets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tracker", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tracker", "external_id", name="uq_ticket_resets_ticket"),
    )
    op.create_index(
        "ix_ticket_resets_external_id", "ticket_resets", ["external_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_resets_external_id", table_name="ticket_resets")
    op.drop_table("ticket_resets")
