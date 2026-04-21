"""Port for the MR-history RAG index (Phase 2.5).

Two operations:
    * ``refresh`` — pull merged MRs from the VCS, compute embeddings, and
      store them locally. Idempotent: re-indexing the same MRs overwrites
      their rows. The caller controls how many recent MRs to index.
    * ``search`` — embed ``query`` and return the top-``k`` hits by cosine
      similarity. Pure read-side.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.mr_history import MrHistoryHit


class MrHistoryPort(ABC):
    @abstractmethod
    async def refresh(self, repo_key: str, limit: int = 500) -> int:
        """(Re-)index up to ``limit`` most recent merged MRs for ``repo_key``.

        Returns the number of rows written.
        """

    @abstractmethod
    async def search(
        self, repo_key: str, query: str, k: int = 5
    ) -> Sequence[MrHistoryHit]:
        """Return the top-``k`` most similar MRs for ``query``, highest first."""

    @abstractmethod
    async def count(self, repo_key: str) -> int:
        """Return the number of indexed MRs for ``repo_key``."""
