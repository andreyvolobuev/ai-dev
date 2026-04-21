"""Port for a knowledge base (Confluence / Notion / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.kb import KBPage


class KnowledgeBasePort(ABC):
    """Abstraction over a documentation system."""

    @abstractmethod
    async def fetch_page(self, page_id: str) -> KBPage:
        """Return a page by id, including plain-text content."""

    @abstractmethod
    async def fetch_page_by_url(self, url: str) -> KBPage:
        """Resolve a page id from a URL (useful for links in ticket descriptions)."""

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> Sequence[KBPage]:
        """Full-text search across the KB."""
