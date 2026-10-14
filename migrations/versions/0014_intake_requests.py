"""intake_requests: MM-просьбы «заведи задачу» и созданные по ним тикеты

Revision ID: 0014
Revises: 0013

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Одна строка на просьбу. source_post_id уникален: строка пишется до
    # похода в Jira, так что повторная доставка поста (WS + catch-up)
    # второй тикет не создаст. issue_key заполняется после ответа Jira,
    # поэтому nullable.
    op.create_table(
        "intake_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.String(64), nullable=False, unique=True),
        sa.Column("mm_root_id", sa.String(64), nullable=False),
        sa.Column("mm_channel_id", sa.String(64), nullable=False),
        sa.Column("requester_mm_user_id", sa.String(64), nullable=False),
        sa.Column("issue_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_intake_requests_mm_root_id", "intake_requests", ["mm_root_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_intake_requests_mm_root_id", table_name="intake_requests")
    op.drop_table("intake_requests")
