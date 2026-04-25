"""LocalMrHistory: refresh + search against a fake VCS and a deterministic embedder."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.mr_history import LocalMrHistory
from virtual_dev.domain.models.merge_request import MergeRequest, MRStatus, PipelineStatus
from virtual_dev.domain.ports.embedder import EmbedderPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.db import MrHistoryRow


# --- Fakes ---


class _WordBagEmbedder(EmbedderPort):
    """Deterministic, fast, no network: hash each token into a 32-dim vector.

    Token overlap between query and doc → high cosine similarity. Good
    enough to verify the search actually retrieves relevant entries.
    """

    def __init__(self) -> None:
        self._dim = 32

    @property
    def model_name(self) -> str:
        return "fake-wordbag-v1"

    @property
    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for tok in text.lower().replace("/", " ").split():
                h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
                vec[h % self._dim] += 1.0
            out.append(vec)
        return out


class _FakeVcsWithMergedMrs(VcsPort):
    def __init__(self, merged: list[MergeRequest]) -> None:
        self._merged = merged

    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500
    ) -> Sequence[MergeRequest]:
        return list(self._merged)[:limit]

    # --- unused methods kept minimal ---

    async def ensure_clone(self, repo_key: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
        raise NotImplementedError

    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None:
        raise NotImplementedError

    async def commit_all(self, repo_key: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def push(self, repo_key: str, branch: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def current_branch(self, repo_key: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def has_uncommitted_changes(self, repo_key: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def create_merge_request(self, *args: Any, **kwargs: Any) -> MergeRequest:
        raise NotImplementedError

    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:
        raise NotImplementedError

    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[Any]:
        raise NotImplementedError

    async def add_mr_comment(self, repo_key: str, iid: int, body: str) -> None:
        raise NotImplementedError

    async def reply_to_comment(self, repo_key: str, iid: int, comment_id: str, body: str) -> None:
        raise NotImplementedError

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_mr_approvals(self, repo_key: str, iid: int):  # pragma: no cover
        raise NotImplementedError

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80
    ):  # pragma: no cover
        raise NotImplementedError


def _mr(iid: int, title: str, description: str = "") -> MergeRequest:
    return MergeRequest(
        id=str(iid), iid=iid, project_id="p", title=title, description=description,
        source_branch=f"feat/{iid}", target_branch="main",
        author_username="alice",
        web_url=f"https://gitlab.example/p/-/merge_requests/{iid}",
        status=MRStatus.MERGED, pipeline_status=PipelineStatus.SUCCESS,
        updated_at=datetime(2025, 4, 1, 12, iid, tzinfo=timezone.utc),
    )


# --- Tests ---


@pytest.mark.asyncio
async def test_refresh_inserts_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vcs = _FakeVcsWithMergedMrs([
        _mr(1, "Add users endpoint", "POST /users returning 201."),
        _mr(2, "Fix timezone bug in analytics", "Parse UTC explicitly."),
    ])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())

    written = await idx.refresh("demo", limit=10)
    assert written == 2

    async with session_factory() as session:
        rows = (await session.execute(
            select(MrHistoryRow).where(MrHistoryRow.repo_key == "demo")
        )).scalars().all()
    assert {row.iid for row in rows} == {1, 2}
    assert all(row.embed_model == "fake-wordbag-v1" for row in rows)
    assert all(row.embed_dim == 32 for row in rows)
    assert all(row.embedding_blob for row in rows)


@pytest.mark.asyncio
async def test_refresh_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vcs = _FakeVcsWithMergedMrs([_mr(1, "A"), _mr(2, "B")])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    await idx.refresh("demo")
    await idx.refresh("demo")  # should not duplicate rows

    assert await idx.count("demo") == 2


@pytest.mark.asyncio
async def test_refresh_drops_rows_from_old_model(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed a row from a different model → should get dropped on refresh.
    async with session_factory() as session:
        session.add(MrHistoryRow(
            repo_key="demo", iid=99, title="stale", description="", author_username="",
            web_url="", embed_model="old-model", embed_dim=4,
            embed_norm=1.0,
            embedding_blob=struct.pack("<4f", 0.5, 0.5, 0.5, 0.5),
        ))
        await session.commit()

    vcs = _FakeVcsWithMergedMrs([_mr(1, "fresh")])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    await idx.refresh("demo")

    async with session_factory() as session:
        iids = (await session.execute(
            select(MrHistoryRow.iid).where(MrHistoryRow.repo_key == "demo")
        )).all()
    iids_set = {i for (i,) in iids}
    assert iids_set == {1}  # 99 dropped


@pytest.mark.asyncio
async def test_search_ranks_relevant_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vcs = _FakeVcsWithMergedMrs([
        _mr(1, "Add users endpoint",
            "Implement POST /users to create a new user account"),
        _mr(2, "Fix timezone bug in analytics",
            "Parse timestamps as UTC to avoid off-by-3h errors"),
        _mr(3, "Refactor caching layer",
            "Move cache keys generation to a helper module"),
    ])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    await idx.refresh("demo")

    hits = await idx.search("demo", "add users endpoint POST", k=3)
    assert len(hits) == 3
    # The top hit should be the users MR (iid=1), highest lexical overlap.
    assert hits[0].iid == 1
    # Scores are descending.
    assert hits[0].score >= hits[1].score >= hits[2].score


@pytest.mark.asyncio
async def test_search_on_empty_index_returns_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vcs = _FakeVcsWithMergedMrs([])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    hits = await idx.search("demo", "anything", k=5)
    assert hits == []


@pytest.mark.asyncio
async def test_search_on_empty_query_returns_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vcs = _FakeVcsWithMergedMrs([_mr(1, "X")])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    await idx.refresh("demo")
    assert await idx.search("demo", "", k=5) == []


@pytest.mark.asyncio
async def test_researcher_search_mr_history_tool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end through ResearcherToolkit._run_search_mr_history."""
    from virtual_dev.application.services import InjectionFilter, ResearcherToolkit
    from virtual_dev.infrastructure.config.schema import (
        AgentsCfg, AppConfig, MappingsCfg, RepositoryCfg,
    )

    vcs = _FakeVcsWithMergedMrs([
        _mr(1, "Add users endpoint", "POST /users ...."),
        _mr(2, "Unrelated refactor", "tidy imports"),
    ])
    idx = LocalMrHistory(session_factory=session_factory, vcs=vcs, embedder=_WordBagEmbedder())
    await idx.refresh("demo")

    toolkit = ResearcherToolkit(
        config=AppConfig(
            repositories=[RepositoryCfg(key="demo", url="x", local_path="/tmp")],
            agents=AgentsCfg(), mappings=MappingsCfg(),
        ),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
        mr_history=idx,
    )
    result = await toolkit._run_search_mr_history(
        {"repo_key": "demo", "query": "add users endpoint", "k": 2}
    )
    text = result["content"][0]["text"]
    assert "!1" in text
    assert "Add users endpoint" in text
    # Result is wrapped for safe prompt inclusion.
    assert "<untrusted_content" in text


@pytest.mark.asyncio
async def test_researcher_search_mr_history_without_adapter_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from virtual_dev.application.services import InjectionFilter, ResearcherToolkit
    from virtual_dev.infrastructure.config.schema import (
        AgentsCfg, AppConfig, MappingsCfg, RepositoryCfg,
    )

    toolkit = ResearcherToolkit(
        config=AppConfig(
            repositories=[RepositoryCfg(key="demo", url="x", local_path="/tmp")],
            agents=AgentsCfg(), mappings=MappingsCfg(),
        ),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
        mr_history=None,
    )
    result = await toolkit._run_search_mr_history(
        {"repo_key": "demo", "query": "x"}
    )
    assert result.get("is_error") is True
    assert "index-mrs" in result["content"][0]["text"]
