"""Lease reclaim on the Postgres bus — regression for the prod crash.

asyncpg localises NAIVE datetime binds using the client machine's
timezone, and SQLAlchemy's ORM evaluator can't compare a naive criteria
value with the timezone-aware timestamptz attributes it loads. The
combination made every reclaim of an expired lease raise
``TypeError: can't compare offset-naive and offset-aware datetimes``
in production (the analyst runner crash-looped and processed nothing).

Needs a running Postgres; skipped when TEST_PG_DSN (or the default local
docker-compose DB) is unreachable, so the sqlite-only CI job still passes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, update

from virtual_dev.adapters.message_bus.sqlalchemy_bus import SqlAlchemyMessageBus
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.infrastructure.db import (
    AgentMessageRow,
    Base,
    make_engine,
    make_session_factory,
)

_DSN = os.environ.get(
    "TEST_PG_DSN",
    "postgresql+asyncpg://sd_bots:qwerty123@localhost:5432/virtual_dev",
)


async def _pg_engine():
    engine = make_engine(_DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(delete(AgentMessageRow))
    except Exception:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable at {_DSN}")
    return engine


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimable_on_postgres() -> None:
    engine = await _pg_engine()
    try:
        sf = make_session_factory(engine)
        bus = SqlAlchemyMessageBus(session_factory=sf, dialect_name="postgresql")
        await bus.publish(AgentMessage(
            id="reclaim-1", from_agent="orch", to_agent="analyst",
            topic="task.discovered", payload={"external_id": "DM-1"},
        ))

        first = await bus._claim_next("analyst")
        assert first is not None

        # Simulate a process killed mid-run: expire the lease.
        async with sf() as session:
            await session.execute(update(AgentMessageRow).values(
                claimed_until=datetime.now(timezone.utc) - timedelta(hours=1),
            ))
            await session.commit()

        reclaimed = await bus._claim_next("analyst")
        assert reclaimed is not None
        assert reclaimed.id == "reclaim-1"

        await bus.ack(reclaimed)
        assert await bus._claim_next("analyst") is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(AgentMessageRow))
        await engine.dispose()
