"""GitLab links people paste into chat: map them onto configured repos and
read what they point at.

«@ai-dev доступен этот коммент? <link>#note_5163279»: the bot answered
«текст мне сюда не дошёл — только ссылка, напиши суть в треде» about a
comment on its own MR, in a repo it has API access to (rainbow!1031).
The web page behind such a link is a login redirect for a token, so the
comment is read through the API and put in front of the model as a fact.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from virtual_dev.domain.models.merge_request import ReviewComment
from virtual_dev.domain.ports.vcs import VcsPort

_MR_URL_RE = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)")
_NOTE_LINK_RE = re.compile(r"https?://[^\s<>()\[\]]+/-/merge_requests/\d+#note_(\d+)")
_MAX_LINKS = 5
_MAX_NOTE_CHARS = 3_000


def project_path_of(repo_url: str) -> str:
    """``git@host:group/proj.git`` / ``https://host/group/proj.git`` →
    ``group/proj``. Mirrors the GitLab adapter's mapping."""
    if ":" in repo_url and "@" in repo_url and not repo_url.startswith(("http://", "https://")):
        path = repo_url.split(":", 1)[1]
    else:
        path = urlparse(repo_url).path.lstrip("/")
    return path.removesuffix(".git")


def resolve_mr_url(url: str, repositories: Any) -> tuple[str, int] | None:
    """Map an MR web URL onto ``(repo_key, iid)`` using the configured
    repositories. None when the URL isn't an MR link of a known repo."""
    parsed = urlparse(url)
    match = _MR_URL_RE.match(parsed.path or "")
    if not match:
        return None
    project = match.group("project").removesuffix(".git")
    for repo in repositories:
        if project_path_of(repo.url) == project:
            return repo.key, int(match.group("iid"))
    return None


def note_id_of(url: str) -> str | None:
    fragment = urlparse(url).fragment
    return fragment.removeprefix("note_") if fragment.startswith("note_") else None


def render_discussion(comments: Sequence[ReviewComment], note_id: str) -> str | None:
    """The note ``note_id`` with the rest of its discussion, or None."""
    target = next((c for c in comments if c.id == note_id), None)
    if target is None:
        return None
    thread = (
        [c for c in comments if c.discussion_id == target.discussion_id]
        if target.discussion_id else [target]
    )
    where = f" ({target.file_path}:{target.line})" if target.file_path else ""
    lines = [f"Обсуждение{where}:"]
    for comment in thread:
        marker = "→ " if comment.id == note_id else ""
        body = comment.body.strip()[:_MAX_NOTE_CHARS]
        lines.append(f"{marker}@{comment.author_username}: {body}")
    return "\n".join(lines)


async def read_linked_notes(
    vcs: VcsPort | None, repositories: Any, texts: Iterable[str],
) -> list[tuple[str, str]]:
    """``(url, rendered discussion)`` for every GitLab note link in
    ``texts`` that points into a configured repo. Unreadable links come
    back with an explanation instead of being dropped, so the model never
    claims it got «только ссылку» without saying why."""
    if vcs is None:
        return []
    urls: list[str] = []
    for text in texts:
        for match in _NOTE_LINK_RE.finditer(text or ""):
            url = match.group(0)
            if url not in urls:
                urls.append(url)
    out: list[tuple[str, str]] = []
    cache: dict[tuple[str, int], Sequence[ReviewComment]] = {}
    for url in urls[:_MAX_LINKS]:
        resolved = resolve_mr_url(url, repositories)
        note_id = note_id_of(url)
        if resolved is None or note_id is None:
            continue
        if resolved not in cache:
            try:
                cache[resolved] = await vcs.list_review_comments(*resolved)
            except Exception as exc:
                logger.warning("read_linked_notes: {} unreadable: {}", url, exc)
                out.append((url, f"(GitLab не отдал комментарии: {type(exc).__name__})"))
                continue
        rendered = render_discussion(cache[resolved], note_id)
        out.append((url, rendered or "(такого комментария в MR нет — удалён?)"))
    return out


__all__ = [
    "note_id_of",
    "project_path_of",
    "read_linked_notes",
    "render_discussion",
    "resolve_mr_url",
]
