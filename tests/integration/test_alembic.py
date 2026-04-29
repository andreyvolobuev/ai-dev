"""Integration tests for Alembic migrations.

Two contracts:

* The baseline migration creates the full set of tables with the right
  UNIQUE constraints — i.e. what was previously done by
  ``Base.metadata.create_all`` happens via migrations now.
* The set of migrations and the ORM ``MetaData`` agree — i.e. there is
  no drift between models and migrations. Adding a column to a model
  without a matching revision must be caught by CI.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import create_engine

from virtual_dev.infrastructure.db import Base
from virtual_dev.infrastructure.db.migrations import (
    build_alembic_config,
    upgrade_to_head,
)

_EXPECTED_TABLES = {
    "tasks",
    "merge_requests",
    "agent_messages",
    "plans",
    "mr_history",
    "analyst_conversation_steps",
    "analyst_conversation_fragments",
    "events",
}

# Per UniqueConstraint defined in models.py.
_EXPECTED_UNIQUE_CONSTRAINTS: dict[str, set[str]] = {
    "tasks": {"uq_tracker_external_id"},
    "merge_requests": {"uq_repo_iid"},
    "mr_history": {"uq_mr_history_repo_iid"},
    "analyst_conversation_steps": {"uq_analyst_conv_step_seq"},
    "analyst_conversation_fragments": {"uq_analyst_conv_fragment_post"},
}


def _sync_url(path: Path) -> str:
    """Sync URL — Alembic doesn't speak aiosqlite."""
    return f"sqlite:///{path}"


def test_baseline_migration_creates_full_schema(tmp_path: Path) -> None:
    """Running ``upgrade_to_head`` on a fresh DB must create every table
    every model declares, with the model-level UNIQUE constraints."""
    db_path = tmp_path / "alembic_baseline.db"
    upgrade_to_head(_sync_url(db_path))

    engine = create_engine(_sync_url(db_path))
    try:
        inspector = inspect(engine)
        actual_tables = set(inspector.get_table_names())
        # alembic_version is an Alembic-internal table; tolerate it.
        actual_tables.discard("alembic_version")
        assert actual_tables == _EXPECTED_TABLES, (
            f"missing/extra tables: "
            f"missing={_EXPECTED_TABLES - actual_tables}, "
            f"extra={actual_tables - _EXPECTED_TABLES}"
        )

        for table, expected_uniques in _EXPECTED_UNIQUE_CONSTRAINTS.items():
            actual = {uc["name"] for uc in inspector.get_unique_constraints(table)}
            missing = expected_uniques - actual
            assert not missing, (
                f"table {table!r} missing UNIQUE constraints: {missing}"
            )
    finally:
        engine.dispose()


def test_models_match_alembic_head(tmp_path: Path) -> None:
    """If a model gains a column without a matching revision, this test
    must fail. We run alembic to head, then ask alembic to autogenerate
    a *diff* against ``Base.metadata`` — a clean head means an empty diff.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    db_path = tmp_path / "alembic_drift.db"
    upgrade_to_head(_sync_url(db_path))

    engine = create_engine(_sync_url(db_path))
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()

    # Filter out trivial "type" diffs that SQLAlchemy<->SQLite can show
    # for benign cases — keep the test honest about real schema drift
    # (added/removed tables/columns/indexes).
    real = [d for d in diff if _is_real_drift(d)]
    assert not real, (
        f"models drifted from migrations head: {real}\n"
        f"Run: uv run alembic revision --autogenerate -m '<description>'"
    )


def _is_real_drift(diff_entry: object) -> bool:
    """Treat add/remove of table/column/index/constraint as real drift.
    Type-only mismatches (e.g. SQLite reporting String as VARCHAR) are
    benign noise we explicitly ignore."""
    if not isinstance(diff_entry, tuple) or not diff_entry:
        return True
    op = diff_entry[0]
    return op in {
        "add_table", "remove_table",
        "add_column", "remove_column",
        "add_index", "remove_index",
        "add_constraint", "remove_constraint",
    }


def test_alembic_config_points_at_migrations_dir() -> None:
    """``build_alembic_config`` returns a Config pointing at the
    real migrations/ directory at the repo root — sanity check that
    the helper isn't silently using a stub path."""
    cfg = build_alembic_config("sqlite:///:memory:")
    script_location = cfg.get_main_option("script_location")
    assert script_location, "script_location must be set"
    # It can be relative (``migrations``) or absolute — both are fine
    # as long as the resolved path exists and contains versions/.
    resolved = Path(script_location)
    if not resolved.is_absolute():
        # Default Alembic resolution is relative to the .ini file's
        # parent or to CWD — accept both.
        candidates = [
            Path.cwd() / resolved,
            resolved.resolve(),
        ]
        resolved = next((c for c in candidates if c.exists()), candidates[0])
    assert (resolved / "versions").is_dir(), (
        f"expected versions/ under {resolved}"
    )
