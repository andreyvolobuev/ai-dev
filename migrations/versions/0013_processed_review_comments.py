"""processed_review_comments: DB claim dedup for GitLab review replies

Revision ID: 0013
Revises: 0012

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per GitLab review comment whose reply/ack was delivered.
    # Counterpart of 0012's `processed_thread_posts` for the GitLab path:
    # the Reviewer tick persists its cursor only at the end, so a pod
    # dying mid-iteration re-acked the same comment after every restart.
    # No backfill: already-answered comments are behind the persisted
    # last_seen watermark, which is still checked first.
    op.create_table(
        "processed_review_comments",
        sa.Column("comment_key", sa.String(128), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_review_comments")
