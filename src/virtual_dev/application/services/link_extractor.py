"""Extract Confluence / Mattermost / GitLab links from free-form text.

Phase 1 uses these links to enrich the Analyst's context. The extraction is
intentionally permissive (better over-fetch than miss a link) — the
knowledge-base adapter itself decides which URLs it can handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+")


@dataclass
class ExtractedLinks:
    confluence: list[str]
    mattermost_threads: list[str]
    mattermost_messages: list[str]
    gitlab: list[str]
    other: list[str]


def extract_links(
    text: str,
    *,
    confluence_host: str | None = None,
    mattermost_host: str | None = None,
    gitlab_host: str | None = None,
) -> ExtractedLinks:
    """Bucket URLs found in ``text`` by host.

    Host matching uses suffix comparison so that
    ``https://confluence.2gis.ru`` matches a plain ``confluence.2gis.ru``.
    """
    confluence, mm_threads, mm_messages, gitlab, other = [], [], [], [], []

    conf_host = _norm_host(confluence_host)
    mm_host = _norm_host(mattermost_host)
    gl_host = _norm_host(gitlab_host)

    for url in _URL_RE.findall(text or ""):
        host = _norm_host(urlparse(url).hostname)
        if conf_host and host == conf_host:
            confluence.append(url)
        elif mm_host and host == mm_host:
            if _is_mm_thread(url):
                mm_threads.append(url)
            else:
                mm_messages.append(url)
        elif gl_host and host == gl_host:
            gitlab.append(url)
        else:
            other.append(url)

    return ExtractedLinks(
        confluence=confluence,
        mattermost_threads=mm_threads,
        mattermost_messages=mm_messages,
        gitlab=gitlab,
        other=other,
    )


def _norm_host(host: str | None) -> str:
    if not host:
        return ""
    h = host.strip().lower()
    if h.startswith("https://"):
        h = h[len("https://") :]
    if h.startswith("http://"):
        h = h[len("http://") :]
    return h.rstrip("/")


def _is_mm_thread(url: str) -> bool:
    """Mattermost thread URLs contain ``/pl/<id>`` or ``/messages/?root=<id>``."""
    return "/pl/" in url or "root=" in url
