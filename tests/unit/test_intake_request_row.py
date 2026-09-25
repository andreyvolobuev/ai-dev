"""Таблица заявок интейка: дедуп по посту-источнику."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.infrastructure.db import IntakeRequestRow


async def test_source_post_id_is_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один пост = максимум один заведённый тикет. Повторная доставка
    того же поста (WS + catch-up) не должна плодить задачи."""
    async with session_factory() as session:
        session.add(IntakeRequestRow(
            source_post_id="post-1", mm_root_id="root-1",
            mm_channel_id="chan-1", requester_mm_user_id="u1",
        ))
        await session.commit()

    async with session_factory() as session:
        session.add(IntakeRequestRow(
            source_post_id="post-1", mm_root_id="root-1",
            mm_channel_id="chan-1", requester_mm_user_id="u1",
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_issue_key_is_nullable_until_jira_answers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строка-заявка вставляется ДО создания тикета - иначе уникальный
    ключ не защищает от дубля."""
    async with session_factory() as session:
        row = IntakeRequestRow(
            source_post_id="post-2", mm_root_id="root-2",
            mm_channel_id="chan-1", requester_mm_user_id="u1",
        )
        session.add(row)
        await session.commit()
        assert row.issue_key is None
        row.issue_key = "DM-4821"
        await session.commit()
