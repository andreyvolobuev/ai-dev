"""0016: tasks failed by a stale planning deadline while their MR is open."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command

from virtual_dev.infrastructure.db.migrations import build_alembic_config


def test_open_mr_task_comes_back_and_stale_deadlines_are_dropped(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    cfg = build_alembic_config(url)
    command.upgrade(cfg, "0015")
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    meta.reflect(engine, only=["tasks", "merge_requests"])
    with engine.begin() as conn:
        for key, status in (("DM-3784", "failed"), ("DM-1", "failed"), ("DM-2", "mr_open")):
            conn.execute(meta.tables["tasks"].insert().values(_filled(
                meta.tables["tasks"], external_id=key, internal_status=status,
                analyst_deadline_at=datetime(2026, 10, 30, 11, 48),
            )))
        conn.execute(meta.tables["merge_requests"].insert().values(_filled(
            meta.tables["merge_requests"], task_external_id="DM-3784", status="open",
        )))

    command.upgrade(cfg, "0016")

    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text(
            "SELECT external_id, internal_status FROM tasks",
        )).all())
        deadlines = conn.execute(sa.text(
            "SELECT count(*) FROM tasks WHERE analyst_deadline_at IS NOT NULL",
        )).scalar()
    assert rows == {"DM-3784": "mr_open", "DM-1": "failed", "DM-2": "mr_open"}
    assert deadlines == 0


def _filled(table: sa.Table, **values: Any) -> dict[str, Any]:
    """``values`` plus a placeholder for every other NOT NULL column."""
    row = dict(values)
    for col in table.columns:
        if col.name in row or col.nullable or col.primary_key or col.server_default is not None:
            continue
        python_type = col.type.python_type
        row[col.name] = {
            int: 0, bool: False, str: "x", datetime: datetime(2026, 10, 1),
        }.get(python_type, "[]" if python_type in (dict, list) else "x")
    return row
