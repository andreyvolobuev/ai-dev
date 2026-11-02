"""Вернуть из failed задачи с открытым МР; снять забытые дедлайны планирования

Дедлайн аналитика не снимался после сдачи плана, а проверка дедлайнов
пропускала coding / mr_open. DM-3784 через два дня после открытия МР
получил «deadline_exceeded» и стал failed. Код это больше не делает;
миграция чинит уже пострадавшие задачи: открытый МР — значит mr_open.
Если таких нет, миграция отрабатывает вхолостую.

Revision ID: 0016
Revises: 0015

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OPEN_MR = """
    EXISTS (
        SELECT 1 FROM merge_requests m
        WHERE m.task_external_id = tasks.external_id
          AND m.status IN ('open', 'draft')
    )
"""


def upgrade() -> None:
    op.execute(f"""
        UPDATE tasks
        SET internal_status = 'mr_open'
        WHERE internal_status = 'failed' AND {_OPEN_MR}
    """)
    op.execute("""
        UPDATE tasks
        SET analyst_deadline_at = NULL
        WHERE analyst_deadline_at IS NOT NULL
          AND internal_status NOT IN ('planning', 'clarifying')
    """)


def downgrade() -> None:
    # Какие из них были failed «по делу», уже не восстановить — и незачем.
    pass
