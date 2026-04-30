"""MM attachments in coalesced fragments survive to the analyst prompt.

Real prod regression: reporter replied "Привет!" with the brief as
a screenshot attachment. The bot read only the text and missed the
image, then re-asked. Root cause: ``AnalystInbox.append_fragment``
persisted only ``text`` from ``ChatMessage`` and dropped ``files``.

Three contracts pinned here:

1. ``AnalystSessionRepository.append_fragment`` accepts and stores a
   list of file metadata.
2. The coalesced HUMAN_REPLIED step exposes the union of attached
   files via metadata so the prompt builder can render them.
3. ``AnalystAgent._render_step`` includes a per-file line with the
   right ``read_<format>_url`` tool hint so the model knows to fetch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
    ConversationStepKind,
)
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope


async def _seed_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id="DM-MM-FILES",
            title="t", description="", url="",
            components_json=[], labels_json=[], links_json=[],
            priority="medium", external_status="In Progress",
            internal_status="planning", dor_satisfied=False,
            target_repo_key="bellingshausen",
        )
        session.add(row)
        await session.flush()
        return row.id  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_append_fragment_persists_files(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _seed_task(session_factory)
    repo = AnalystSessionRepository(session_factory)
    files = [
        {"id": "f1", "name": "brief.png", "url": "https://mm/api/v4/files/f1",
         "mime_type": "image/png", "extension": "png", "size": 1234},
        {"id": "f2", "name": "spec.pdf", "url": "https://mm/api/v4/files/f2",
         "mime_type": "application/pdf", "extension": "pdf", "size": 9999},
    ]

    ok = await repo.append_fragment(
        task_id=task_id, mm_post_id="post-1", asked_post_id=None,
        text="Привет!", received_at=datetime.now(timezone.utc),
        files=files,
    )
    assert ok is True

    fragments = await repo.list_unflushed_fragments(task_id)
    assert len(fragments) == 1
    assert fragments[0].files_json == files


@pytest.mark.asyncio
async def test_render_step_for_human_replied_lists_attached_files() -> None:
    """When the merged HUMAN_REPLIED step carries `attached_files`
    metadata, the analyst prompt must include a read_<format>_url
    hint per file so the model knows to fetch + read it."""
    from datetime import datetime as _dt

    from virtual_dev.application.agents.analyst import AnalystAgent
    from virtual_dev.application.services import InjectionFilter
    from virtual_dev.domain.models.analyst_conversation import ConversationStep

    step = ConversationStep(
        id=1, task_id=1, seq=2,
        kind=ConversationStepKind.HUMAN_REPLIED,
        timestamp=_dt(2026, 4, 30, 14, 0, tzinfo=timezone.utc),
        text="Привет!",
        metadata={
            "from_username": "an.volobuev",
            "attached_files": [
                {"name": "brief.png", "url": "https://mm/api/v4/files/f1",
                 "mime_type": "image/png", "extension": "png"},
                {"name": "data.xlsx", "url": "https://mm/api/v4/files/f2",
                 "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "extension": "xlsx"},
            ],
        },
    )
    agent = AnalystAgent.__new__(AnalystAgent)
    rendered = agent._render_step(step, InjectionFilter())  # type: ignore[arg-type]

    assert "brief.png" in rendered
    assert "https://mm/api/v4/files/f1" in rendered
    assert "read_image_url" in rendered
    assert "data.xlsx" in rendered
    assert "read_xlsx_url" in rendered


@pytest.mark.asyncio
async def test_analyst_inbox_passes_files_to_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: an MM ChatMessage with a file attachment, after
    going through AnalystInbox.append_fragment, ends up in the
    fragment row with files preserved."""
    from datetime import datetime as _dt

    from virtual_dev.domain.models.chat import ChatFile, ChatMessage
    from virtual_dev.runtime.workers.analyst_inbox import AnalystInbox

    # Minimal AnalystInbox shell — only ``_sessions`` is exercised
    # here, every other dep can stay None.
    inbox = AnalystInbox.__new__(AnalystInbox)
    inbox._sessions = AnalystSessionRepository(session_factory)  # type: ignore[attr-defined]
    inbox._trace = None  # type: ignore[attr-defined]
    inbox.stats = type("S", (), {"fragments_appended": 0})()  # type: ignore[attr-defined]

    task_id = await _seed_task(session_factory)
    msg = ChatMessage(
        id="post-x", channel_id="ch-1", author_id="u-1",
        text="хех", timestamp=_dt(2026, 4, 30, tzinfo=timezone.utc),
        files=[ChatFile(
            id="ff", name="screenshot.png",
            url="https://mm/api/v4/files/ff",
            mime_type="image/png", extension="png", size=42,
        )],
    )

    ok = await inbox.append_fragment(task_id, msg)
    assert ok is True

    fragments = await AnalystSessionRepository(session_factory).list_unflushed_fragments(task_id)
    assert len(fragments) == 1
    assert fragments[0].files_json
    assert fragments[0].files_json[0]["name"] == "screenshot.png"
