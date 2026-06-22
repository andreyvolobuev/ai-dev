"""mr autofix_escalation_root_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('merge_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('autofix_escalation_root_id', sa.String(length=64), nullable=True))
        batch_op.create_index(
            'ix_merge_requests_autofix_escalation_root_id',
            ['autofix_escalation_root_id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('merge_requests', schema=None) as batch_op:
        batch_op.drop_index('ix_merge_requests_autofix_escalation_root_id')
        batch_op.drop_column('autofix_escalation_root_id')
