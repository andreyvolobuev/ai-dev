"""merge_requests.autofix_restart_processed_at replay-dedup marker

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-23 01:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("merge_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "autofix_restart_processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ))
    # Backfill: every `/restart` that exists at migration time has been
    # processed (ad nauseam — this ships as the fix for the ack-spam
    # loop). Claiming "now" makes historical posts stale immediately, so
    # the first catch-up sweep after deploy goes quiet instead of firing
    # one parting ack per replayed command. A fresh `/restart` sent after
    # deploy is newer than this marker and processes normally.
    op.execute(
        "UPDATE merge_requests SET autofix_restart_processed_at = CURRENT_TIMESTAMP"
    )


def downgrade() -> None:
    with op.batch_alter_table("merge_requests", schema=None) as batch_op:
        batch_op.drop_column("autofix_restart_processed_at")
