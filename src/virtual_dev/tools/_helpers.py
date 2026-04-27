"""Shared helpers for tool implementations.

Tool files import from here when they need:

* the ``content``-block wrapping shape Claude expects (``text_result``,
  ``error_text``);
* the ``git grep`` shell-out used by code-search tools;
* HTTP fetch with platform-specific bearer auth, used by attachment-
  reading tools.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger


def text_result(text: str) -> dict[str, Any]:
    """Wrap a plain string into an MCP tool result block."""
    return {"content": [{"type": "text", "text": text}]}


def error_text(msg: str) -> dict[str, Any]:
    """Return an MCP error block. ``is_error`` lights up the failure
    indicator on the LLM side."""
    return {
        "content": [{"type": "text", "text": f"ERROR: {msg}"}],
        "is_error": True,
    }


def git_grep(repo_path: Path, pattern: str, max_results: int) -> str:
    """Run ``git grep -nI`` in ``repo_path``.

    Falls back to a plain message if the path is not a git repo. Output
    is capped at ``max_results`` lines; stderr is suppressed.
    """
    try:
        proc = subprocess.run(
            [
                "git", "grep", "-nI",
                "--max-depth", "20",
                "-e", pattern,
            ],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return "git is not installed"
    except subprocess.TimeoutExpired:
        return f"git grep timed out for pattern {pattern!r}"
    except Exception as exc:  # fail loud, but let the Analyst continue
        logger.exception("git grep failed")
        return f"git grep error: {exc}"

    lines = (proc.stdout or "").splitlines()
    if not lines:
        return f"no matches for pattern {pattern!r}"
    if len(lines) > max_results:
        truncated = len(lines) - max_results
        lines = lines[:max_results]
        lines.append(f"... ({truncated} more matches truncated)")
    return "\n".join(lines)


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def fetch_url_with_bearer(url: str, token: str, *, timeout: float = 30.0) -> bytes:
    """GET ``url`` with ``Authorization: Bearer <token>``. Returns the
    response body. Raises on non-2xx. Sync — wrap in ``asyncio.to_thread``."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.content


_MM_PERMALINK_RE = re.compile(r"/pl/([a-z0-9]+)/?", re.IGNORECASE)
_JIRA_ATTACHMENT_RE = re.compile(
    r"/secure/attachment/(\d+)(?:/|$)", re.IGNORECASE,
)


def parse_jira_attachment_id(url: str) -> str | None:
    """Extract the numeric attachment id from a Jira download URL.

    Self-hosted Jira links to attachments as
    ``<base>/secure/attachment/<id>/<filename>`` in the ticket
    description. Hitting that path directly with PAT auth returns
    HTML (login redirect / wrapper) — we use the REST API
    ``/rest/api/2/attachment/content/<id>`` instead, which needs the
    bare id.

    Returns ``None`` for inputs we can't parse.
    """
    if not url:
        return None
    m = _JIRA_ATTACHMENT_RE.search(url)
    if m:
        return m.group(1)
    # Bare numeric id pass-through.
    if re.fullmatch(r"\d+", url.strip()):
        return url.strip()
    return None


def fetch_jira_attachment_content(
    *, jira_url: str, jira_token: str, attachment_id: str,
) -> bytes:
    """Download attachment bytes via Jira REST API. Sync — wrap with
    ``asyncio.to_thread``. Uses ``atlassian-python-api`` so it picks
    up the same auth shape as the rest of the Jira-tracker code.
    """
    from atlassian import Jira
    client = Jira(url=jira_url, token=jira_token, cloud=False)
    body = client.get_attachment_content(attachment_id)
    if not isinstance(body, (bytes, bytearray)):
        raise RuntimeError(
            f"Unexpected attachment response type: {type(body).__name__}"
        )
    return bytes(body)


def parse_mm_post_id(url_or_id: str) -> str | None:
    """Extract the post id from a Mattermost permalink, or return the
    arg as-is if it looks like a bare id (26-char alnum). Returns
    ``None`` for inputs we can't parse."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _MM_PERMALINK_RE.search(s)
    if m:
        return m.group(1)
    # Bare id: Mattermost post ids are 26-char alnum (lower).
    if re.fullmatch(r"[a-z0-9]{20,40}", s):
        return s
    return None


def url_is_on_host(url: str, expected_host: str) -> bool:
    """True if ``url``'s host equals (or ends with) ``expected_host``.

    ``expected_host`` may be a bare hostname (``jira.2gis.ru``) or a
    full URL (``https://jira.2gis.ru/``); both work. Empty arg or
    unparseable URL returns False.
    """
    if not expected_host:
        return False
    url_host = _host_of(url)
    if not url_host:
        return False
    # Treat ``expected_host`` as URL first; if no host parses out,
    # fall back to using it as a literal hostname.
    expected = (urlparse(expected_host).hostname or expected_host).lower()
    if not expected:
        return False
    return url_host == expected or url_host.endswith("." + expected)


__all__ = [
    "error_text",
    "fetch_jira_attachment_content",
    "fetch_url_with_bearer",
    "git_grep",
    "parse_jira_attachment_id",
    "parse_mm_post_id",
    "text_result",
    "url_is_on_host",
]
