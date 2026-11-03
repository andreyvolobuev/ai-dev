"""merge_requests: what the last iteration did, for honest thread replies

Revision ID: 0017
Revises: 0016

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Asked «в чём затык?» or «какие коммиты получились?», the bot used to
    # have nothing but its own earlier chat lines to go on (rainbow!1031).
    # The outcome of the last iteration — and the reason when it failed —
    # now survives between runs and restarts.
    with op.batch_alter_table("merge_requests") as batch:
        batch.add_column(sa.Column("iteration_pending_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("last_iteration_outcome", sa.String(32), nullable=True))
        batch.add_column(sa.Column("last_iteration_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("last_iteration_detail", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("last_iteration_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("merge_requests") as batch:
        batch.drop_column("last_iteration_at")
        batch.drop_column("last_iteration_detail")
        batch.drop_column("last_iteration_summary")
        batch.drop_column("last_iteration_outcome")
        batch.drop_column("iteration_pending_note")
