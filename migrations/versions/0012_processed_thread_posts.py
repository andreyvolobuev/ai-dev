"""processed_thread_posts: DB claim dedup for MM thread replies

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-30 15:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per MM post the bot has responded to. The ✅-reaction on
    # the post remains as the human-visible fast-path marker; this table
    # is the cross-restart / cross-instance authority (same pattern as
    # 0011's `/restart` claim). No backfill: posts already answered
    # carry the ✅-reaction, which is still checked first.
    op.create_table(
        "processed_thread_posts",
        sa.Column("post_id", sa.String(64), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_thread_posts")
