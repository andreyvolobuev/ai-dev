"""Confluence adapter (self-hosted, Server/DC) via ``atlassian-python-api``."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlparse

from atlassian import Confluence
from loguru import logger

from virtual_dev.domain.models.kb import KBPage
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort


class ConfluenceKb(KnowledgeBasePort):
    """``KnowledgeBasePort`` backed by self-hosted Confluence."""

    def __init__(self, *, url: str, user: str, token: str) -> None:
        if not url or not user or not token:
            raise ValueError("Confluence URL/user/token must be provided")
        self._client = Confluence(url=url, username=user, password=token, cloud=False)
        self._base_url = url.rstrip("/")

    async def fetch_page(self, page_id: str) -> KBPage:
        def _fetch() -> KBPage:
            raw = self._client.get_page_by_id(
                page_id, expand="body.view,space,children.page,ancestors"
            )
            if not isinstance(raw, dict):
                raise RuntimeError(f"Unexpected Confluence response: {type(raw).__name__}")
            return self._page_from_raw(cast(dict[str, Any], raw))

        return await asyncio.to_thread(_fetch)

    async def fetch_page_by_url(self, url: str) -> KBPage:
        """Best-effort extraction of a page id from ``url``.

        Confluence URLs come in several flavours:
            /pages/viewpage.action?pageId=12345
            /display/SPACE/Page+Title  (needs a separate lookup)
            /spaces/SPACE/pages/12345/Page+Title
        """
        page_id = _extract_page_id(url)
        if page_id is not None:
            return await self.fetch_page(page_id)

        # /display/SPACE/Title — ask Confluence to resolve it.
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "display":
            space, title = parts[1], parts[2].replace("+", " ")

            def _by_title() -> KBPage:
                raw = self._client.get_page_by_title(
                    space=space, title=title, expand="body.view,space"
                )
                if not isinstance(raw, dict):
                    raise RuntimeError(
                        f"Could not resolve Confluence page by title: space={space} title={title}"
                    )
                return self._page_from_raw(cast(dict[str, Any], raw))

            return await asyncio.to_thread(_by_title)

        raise ValueError(f"Unsupported Confluence URL shape: {url}")

    async def search(self, query: str, limit: int = 10) -> Sequence[KBPage]:
        def _search() -> list[KBPage]:
            # CQL content search; ``limit`` is enforced client-side to keep the call simple.
            cql = f'text ~ "{query}" AND type = "page"'
            raw = self._client.cql(cql, limit=limit, expand="body.view,space")
            if not isinstance(raw, dict):
                logger.warning("Unexpected Confluence search response: {!r}", raw)
                return []
            results = cast(list[dict[str, Any]], raw.get("results") or [])
            pages: list[KBPage] = []
            for item in results:
                content = item.get("content") if isinstance(item, dict) else None
                if isinstance(content, dict):
                    pages.append(self._page_from_raw(content))
            return pages

        return await asyncio.to_thread(_search)

    # --- helpers ---

    def _page_from_raw(self, raw: dict[str, Any]) -> KBPage:
        body_view = ""
        body = raw.get("body")
        if isinstance(body, dict):
            view = body.get("view")
            if isinstance(view, dict):
                body_view = str(view.get("value") or "")

        space_key = ""
        space = raw.get("space")
        if isinstance(space, dict):
            space_key = str(space.get("key") or "")

        children_ids: list[str] = []
        children = raw.get("children")
        if isinstance(children, dict):
            page = children.get("page")
            if isinstance(page, dict):
                for item in page.get("results", []):
                    if isinstance(item, dict) and item.get("id"):
                        children_ids.append(str(item["id"]))

        page_id = str(raw.get("id") or "")
        title = str(raw.get("title") or "")
        url = f"{self._base_url}/pages/viewpage.action?pageId={page_id}" if page_id else ""

        return KBPage(
            id=page_id,
            title=title,
            space=space_key or None,
            url=url,
            content_text=_html_to_plain(body_view),
            child_page_ids=children_ids,
        )


_PAGE_ID_KEYS = ("pageId", "pageid")


def _extract_page_id(url: str) -> str | None:
    parsed = urlparse(url)
    # Query string form.
    query_pairs = [kv.split("=", 1) for kv in parsed.query.split("&") if "=" in kv]
    for key, value in query_pairs:
        if key in _PAGE_ID_KEYS and value.isdigit():
            return value
    # Path form: /spaces/<SPACE>/pages/<ID>/...
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "pages" and i + 1 < len(parts) and parts[i + 1].isdigit():
            return parts[i + 1]
    return None


def _html_to_plain(html: str) -> str:
    """Strip HTML tags; good enough for LLM consumption in Phase 1."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError:
        # Minimal fallback: remove angle-bracket tags.
        import re

        return re.sub(r"<[^>]+>", "", html)
    return BeautifulSoup(html, "html.parser").get_text("\n").strip()
