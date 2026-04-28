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


def auth_headers_for(url: str, settings: Any) -> dict[str, str]:
    """Pick the right HTTP auth header(s) for ``url`` based on host.

    Three cases route automatically — operator only configures the
    creds in ``.env`` and any tool that downloads bytes for the LLM
    just calls this:

    * host == ``CONFLUENCE_URL`` → HTTP Basic ``user:token``
      (Confluence Server/DC PATs use Basic, not Bearer).
    * host == ``JIRA_URL`` → ``Authorization: Bearer <token>``.
    * host == ``MATTERMOST_URL`` → ``Authorization: Bearer <token>``.
    * any other host → empty headers (unauthenticated).

    Returns a dict ready to splat into an ``httpx`` ``headers=`` arg.
    """
    import base64

    confluence_url = (getattr(settings, "confluence_url", "") or "").strip()
    confluence_user = (getattr(settings, "confluence_user", "") or "").strip()
    confluence_token = (getattr(settings, "confluence_token", "") or "").strip()
    if (
        confluence_url and confluence_user and confluence_token
        and url_is_on_host(url, confluence_url)
    ):
        creds = f"{confluence_user}:{confluence_token}".encode()
        return {
            "Authorization": "Basic " + base64.standard_b64encode(creds).decode("ascii"),
        }

    jira_url = (getattr(settings, "jira_url", "") or "").strip()
    jira_token = (getattr(settings, "jira_token", "") or "").strip()
    if jira_url and jira_token and url_is_on_host(url, jira_url):
        return {"Authorization": f"Bearer {jira_token}"}

    mm_url = (getattr(settings, "mattermost_url", "") or "").strip()
    mm_token = (getattr(settings, "mattermost_token", "") or "").strip()
    if mm_url and mm_token and url_is_on_host(url, mm_url):
        return {"Authorization": f"Bearer {mm_token}"}

    return {}


def is_trusted_internal_host(url: str, settings: Any) -> bool:
    """True if ``url`` is on a configured internal service.

    Corporate VPN hosts (Confluence / Jira / Mattermost) routinely
    resolve to RFC1918 — refusing them on private-IP grounds would
    block the very services the operator wired creds for. Untrusted
    hosts still go through the standard SSRF guard.
    """
    for url_attr in ("confluence_url", "jira_url", "mattermost_url"):
        configured = (getattr(settings, url_attr, "") or "").strip()
        if configured and url_is_on_host(url, configured):
            return True
    return False


def download_url_bytes(
    url: str,
    settings: Any,
    *,
    timeout: float = 60.0,
    min_body_bytes: int = 64,
) -> bytes:
    """Sync: download ``url`` with host-aware auth. Returns body bytes.

    Wrap in ``asyncio.to_thread`` from async tool code. Used by the
    format-parser tools (``read_pdf_url`` / ``read_docx_url`` / etc.)
    so they don't each re-implement auth + SSRF + tiny-body handling.

    Refuses anything that isn't ``http(s)``. Internal corporate hosts
    (matching the configured CONFLUENCE_URL / JIRA_URL / MATTERMOST_URL)
    are exempt from the private-IP guard — the rest still get blocked
    so a malicious ticket-supplied URL can't probe the LAN.

    The ``min_body_bytes`` knob exists because Jira returns 200 + a
    near-empty body for non-existent attachment ids (instead of 404)
    — surfaced here so the parser-tool's failure message is "this
    looks like a missing id" instead of "couldn't parse PDF". Pass
    ``min_body_bytes=0`` to disable for sources that can legitimately
    return tiny bodies.
    """
    from urllib.parse import urlparse

    s = (url or "").strip()
    if not s:
        raise RuntimeError("empty URL")
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(
            f"refusing scheme {parsed.scheme!r}; only http/https allowed"
        )
    host = (parsed.hostname or "").strip()
    if not host:
        raise RuntimeError(f"couldn't extract hostname from {url!r}")

    # SSRF guard — skipped for trusted internal hosts (which routinely
    # resolve to RFC1918 over corporate VPN).
    if not is_trusted_internal_host(s, settings) and _host_is_private(host):
        raise RuntimeError(
            f"refusing to fetch from private/internal host {host!r}"
        )

    headers = auth_headers_for(s, settings)
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        resp = c.get(s, headers=headers)
    if resp.status_code != 200:
        body_preview = (resp.text or "")[:200]
        raise RuntimeError(
            f"download failed: HTTP {resp.status_code} from {s} "
            f"(body preview: {body_preview!r})"
        )
    body = resp.content
    if min_body_bytes and len(body) < min_body_bytes:
        raise RuntimeError(
            f"download returned only {len(body)} bytes from {s} — "
            f"likely a missing / unauthorised resource (the server "
            f"answered 200 OK with an empty wrapper)"
        )
    return body


def _host_is_private(host: str) -> bool:
    """Resolve ``host`` and refuse if any A/AAAA record is private,
    loopback, link-local, reserved, multicast, or unspecified. Fails
    closed (returns True) on DNS errors so an opaque resolution
    failure doesn't accidentally widen the surface."""
    import ipaddress
    import socket

    h = host.strip().lower()
    if h in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return _ip_is_private(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_private(ip):
            return True
    return False


def _ip_is_private(ip: Any) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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
    *, jira_url: str, jira_token: str, url_or_id: str,
) -> bytes:
    """Download attachment bytes from Jira. Sync — wrap with
    ``asyncio.to_thread``.

    ``url_or_id`` is either a full ``/secure/attachment/<id>/<filename>``
    URL (the form Jira surfaces in ``fields.attachment[].content``) or
    a bare numeric id. For a bare id we hit metadata once to get the
    canonical content URL, then download.

    Verified empirically against Jira Server/DC: a plain GET to
    ``/secure/attachment/<id>/<filename>`` with ``Authorization:
    Bearer <PAT>`` returns 200 + the file bytes. No need for the
    atlassian-python-api ``_session`` wrapper.

    Why a non-200-but-200 used to look like a parsing error: hitting
    ``/secure/attachment/<id>/...`` with a *non-existent* id returns
    ``200 OK`` with a near-empty body (Jira's quirk), not 404 — that's
    why the previous failure surfaced as ``invalid pdf header``
    instead of an HTTP error. We surface non-PDF/non-zip bodies as
    explicit errors below.
    """
    s = (url_or_id or "").strip()
    if not s:
        raise RuntimeError("empty url_or_id")
    if s.startswith(("http://", "https://")):
        download_url = s
    else:
        # Bare id → fetch metadata for the canonical content URL.
        from atlassian import Jira
        client = Jira(url=jira_url, token=jira_token, cloud=False)
        meta = client.get_attachment(s)
        if not isinstance(meta, dict) or not meta.get("content"):
            raise RuntimeError(
                f"Jira attachment {s}: no metadata or missing `content` URL"
            )
        download_url = str(meta["content"])

    if not url_is_on_host(download_url, jira_url):
        raise RuntimeError(
            f"refusing to send Jira PAT to non-Jira host: {download_url}"
        )

    import httpx
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        resp = c.get(download_url, headers={"Authorization": f"Bearer {jira_token}"})
    if resp.status_code != 200:
        body_preview = (resp.text or "")[:200]
        raise RuntimeError(
            f"Jira attachment download failed: HTTP {resp.status_code} "
            f"from {download_url} (body: {body_preview!r})"
        )
    body = resp.content
    # Jira returns 200 + tiny body for non-existent ids. Catch that
    # case explicitly so the parser doesn't barf on a 5-byte "PDF".
    if len(body) < 64:
        raise RuntimeError(
            f"Jira attachment download returned only {len(body)} bytes — "
            f"likely a non-existent id. URL was {download_url}"
        )
    return body


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
    "auth_headers_for",
    "download_url_bytes",
    "error_text",
    "fetch_jira_attachment_content",
    "fetch_url_with_bearer",
    "git_grep",
    "is_trusted_internal_host",
    "parse_jira_attachment_id",
    "parse_mm_post_id",
    "text_result",
    "url_is_on_host",
]
