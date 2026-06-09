"""mr pending_comment_ids

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-09 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows need a default for the NOT NULL — empty JSON list
    # matches the model's runtime default.
    with op.batch_alter_table('merge_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'pending_comment_ids', sa.JSON(), nullable=False,
            server_default=sa.text("'[]'"),
        ))


def downgrade() -> None:
    with op.batch_alter_table('merge_requests', schema=None) as batch_op:
        batch_op.drop_column('pending_comment_ids')
