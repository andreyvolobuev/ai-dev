"""SQLite-backed :class:`MrHistoryPort` with in-memory cosine search.

Scale target is small: a single repo has a few hundred to a few thousand
merged MRs. At 384-dim float32 that's ~1.5 KB / row, well under a megabyte
total — we just load all rows for a repo into memory and do the cosine
similarity with numpy. No vector DB, no daemon, no tuning.

Embeddings are stored as little-endian float32 blobs plus a pre-computed L2
norm column so ranking does not need to re-normalise on every query. The
``embed_model`` column lets us detect a model change and drop stale rows
(handled by ``refresh()``).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

import numpy as np
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.mr_history import MrHistoryHit
from virtual_dev.domain.ports.embedder import EmbedderPort
from virtual_dev.domain.ports.mr_history import MrHistoryPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.db import MrHistoryRow
from virtual_dev.infrastructure.db.base import session_scope


class LocalMrHistory(MrHistoryPort):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        vcs: VcsPort,
        embedder: EmbedderPort,
    ) -> None:
        self._session_factory = session_factory
        self._vcs = vcs
        self._embedder = embedder

    async def refresh(self, repo_key: str, limit: int = 500) -> int:
        mrs = await self._vcs.list_merged_merge_requests(repo_key, limit=limit)
        if not mrs:
            logger.info("MrHistory[{}]: no merged MRs to index", repo_key)
            return 0

        texts = [_render_mr_text(mr.title, mr.description) for mr in mrs]
        embeddings = self._embedder.embed(texts)
        if len(embeddings) != len(mrs):
            raise RuntimeError(
                f"Embedder returned {len(embeddings)} vectors for {len(mrs)} inputs"
            )

        model_name = self._embedder.model_name
        dim = self._embedder.dimension

        async with session_scope(self._session_factory) as session:
            # Drop stale rows that were indexed with a different model —
            # mixing embedding spaces would make cosine meaningless.
            stale = await session.execute(
                select(MrHistoryRow.id).where(
                    MrHistoryRow.repo_key == repo_key,
                    MrHistoryRow.embed_model != model_name,
                )
            )
            stale_ids = [row_id for (row_id,) in stale.all()]
            if stale_ids:
                logger.info(
                    "MrHistory[{}]: dropping {} rows indexed with a different model",
                    repo_key, len(stale_ids),
                )
                await session.execute(
                    delete(MrHistoryRow).where(MrHistoryRow.id.in_(stale_ids))
                )

            written = 0
            for mr, vec in zip(mrs, embeddings, strict=True):
                blob, norm = _pack_vector(vec)
                existing = (await session.execute(
                    select(MrHistoryRow).where(
                        MrHistoryRow.repo_key == repo_key,
                        MrHistoryRow.iid == mr.iid,
                    )
                )).scalar_one_or_none()
                if existing is None:
                    session.add(MrHistoryRow(
                        repo_key=repo_key,
                        iid=mr.iid,
                        title=mr.title,
                        description=mr.description,
                        author_username=mr.author_username,
                        web_url=mr.web_url,
                        merged_at=mr.updated_at,
                        embed_model=model_name,
                        embed_dim=dim,
                        embed_norm=norm,
                        embedding_blob=blob,
                    ))
                else:
                    existing.title = mr.title
                    existing.description = mr.description
                    existing.author_username = mr.author_username
                    existing.web_url = mr.web_url
                    existing.merged_at = mr.updated_at
                    existing.embed_model = model_name
                    existing.embed_dim = dim
                    existing.embed_norm = norm
                    existing.embedding_blob = blob
                written += 1

        logger.info("MrHistory[{}]: indexed {} MRs with {}", repo_key, written, model_name)
        return written

    async def search(
        self, repo_key: str, query: str, k: int = 5
    ) -> Sequence[MrHistoryHit]:
        if not query.strip():
            return []
        query_vec = self._embedder.embed([query])[0]
        model_name = self._embedder.model_name

        async with self._session_factory() as session:
            rows = (await session.execute(
                select(MrHistoryRow).where(
                    MrHistoryRow.repo_key == repo_key,
                    MrHistoryRow.embed_model == model_name,
                )
            )).scalars().all()

        if not rows:
            return []

        q = np.asarray(query_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) or 1.0

        # Stack stored vectors into one matrix — O(n*d), still trivial for
        # corpus sizes we care about.
        matrix = np.empty((len(rows), self._embedder.dimension), dtype=np.float32)
        norms = np.empty(len(rows), dtype=np.float32)
        for i, row in enumerate(rows):
            matrix[i] = _unpack_vector(row.embedding_blob)
            norms[i] = row.embed_norm or 1.0

        scores = (matrix @ q) / (norms * q_norm)
        # ``argsort`` ascending; take the tail and reverse.
        top_idx = np.argsort(scores)[::-1][:k]

        hits: list[MrHistoryHit] = []
        for idx in top_idx:
            row = rows[int(idx)]
            hits.append(MrHistoryHit(
                repo_key=row.repo_key,
                iid=row.iid,
                title=row.title,
                description=row.description,
                web_url=row.web_url,
                author_username=row.author_username,
                merged_at=row.merged_at,
                score=float(scores[int(idx)]),
            ))
        return hits

    async def count(self, repo_key: str) -> int:
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(MrHistoryRow.id).where(MrHistoryRow.repo_key == repo_key)
            )).all()
        return len(rows)


def _render_mr_text(title: str, description: str) -> str:
    """What we embed per MR — title carries most of the signal; description adds colour."""
    if description and len(description) > 1500:
        description = description[:1500] + "…"
    return f"{title}\n\n{description}".strip()


def _pack_vector(vec: Sequence[float]) -> tuple[bytes, float]:
    arr = np.asarray(list(vec), dtype=np.float32)
    norm = float(np.linalg.norm(arr)) or 1.0
    blob = struct.pack(f"<{arr.size}f", *arr.tolist())
    return blob, norm


def _unpack_vector(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.asarray(struct.unpack(f"<{n}f", blob), dtype=np.float32)
