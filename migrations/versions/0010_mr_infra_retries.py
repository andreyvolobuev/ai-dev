"""merge_requests.pipeline_infra_retries counter

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-10 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("merge_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "pipeline_infra_retries", sa.Integer(),
            nullable=False, server_default="0",
        ))


def downgrade() -> None:
    with op.batch_alter_table("merge_requests", schema=None) as batch_op:
        batch_op.drop_column("pipeline_infra_retries")
