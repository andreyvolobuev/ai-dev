"""Доменная модель страницы базы знаний (Confluence / Notion / etc)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KBPage:
    """Страница из базы знаний."""

    id: str
    title: str
    space: str | None = None
    url: str = ""
    content_text: str = ""               # plain text / markdown
    child_page_ids: list[str] = field(default_factory=list)
    linked_page_ids: list[str] = field(default_factory=list)
