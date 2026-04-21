"""Domain model for a search result from the MR-history RAG index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class MrHistoryHit:
    """One result returned by ``MrHistoryPort.search``."""

    repo_key: str
    iid: int
    title: str
    description: str
    web_url: str
    author_username: str
    merged_at: datetime | None
    score: float   # cosine similarity in [-1, 1]; higher is more relevant
