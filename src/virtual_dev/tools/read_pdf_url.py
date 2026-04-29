"""Download a PDF from any URL and return its extracted text.

Works for Jira attachments, Mattermost file attachments, Confluence
attachments, and public URLs. Auth is host-aware (delegated to
``_helpers.download_url_bytes``):

* host == ``JIRA_URL`` → Bearer ``JIRA_TOKEN``
* host == ``MATTERMOST_URL`` → Bearer ``MATTERMOST_TOKEN``
* host == ``CONFLUENCE_URL`` → Basic ``CONFLUENCE_USER:CONFLUENCE_TOKEN``
* anything else → unauthenticated

Pass the full URL exactly as it appears in the surrounding context
(ticket attachments block, MM thread file attachment, plan link).
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from claude_agent_sdk import tool
from loguru import logger
from pypdf import PdfReader

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import download_url_bytes, error_text, text_result

TOOL_GROUP = "shared"

# Truncate per-tool result so a 200-page PDF doesn't blow up the
# prompt. The agent can re-call with a different ``max_chars`` if it
# needs more.
_DEFAULT_MAX_CHARS = 30_000
# Refuse PDFs above this page count outright — extracting text from
# tens of thousands of pages burns minutes and RAM even before
# truncation, and surfaces as a hung run.
MAX_PAGES = 200


def build(ctx: ToolContext):
    if ctx.settings is None:
        return None
    settings = ctx.settings

    @tool(
        "read_pdf_url",
        "Download a PDF from any URL and return its extracted text. "
        "Works for Jira attachments, Mattermost file attachments, "
        "Confluence attachments, and public URLs — auth is picked "
        "automatically based on the host (Jira/MM Bearer, Confluence "
        "Basic, others unauthenticated). Pass the full URL exactly as "
        "it appears in the ticket / thread / plan context. Output is "
        "wrapped as untrusted content. Truncates at max_chars "
        "(default 30000) — raise it explicitly if you need more.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
    )
    async def _read_pdf(args: dict[str, Any]) -> dict[str, Any]:
        return await run(settings, args)

    return _read_pdf


async def run(settings, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    max_chars = int(args.get("max_chars") or _DEFAULT_MAX_CHARS)
    if not url:
        return error_text("Empty URL")

    try:
        body = await asyncio.to_thread(download_url_bytes, url, settings)
    except Exception as exc:
        logger.exception("read_pdf_url: download failed for {}", url)
        return error_text(f"Download failed: {exc}")

    try:
        reader = await asyncio.to_thread(PdfReader, io.BytesIO(body))
    except Exception as exc:
        logger.exception("read_pdf_url: PDF parse failed for {}", url)
        return error_text(f"PDF parse failed: {exc}")

    n_pages = len(reader.pages)
    if n_pages > MAX_PAGES:
        return error_text(
            f"PDF too large: {n_pages} pages exceeds limit of {MAX_PAGES}. "
            f"Ask a human to split / share an excerpt."
        )

    try:
        page_texts = [
            (page.extract_text() or "").strip()
            for page in reader.pages
        ]
    except Exception as exc:
        logger.exception("read_pdf_url: PDF parse failed for {}", url)
        return error_text(f"PDF parse failed: {exc}")

    full = "\n\n--- page break ---\n\n".join(page_texts)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated, {len(full)} chars total]"
        truncated = True
    header = f"# PDF: {url}\n({len(reader.pages)} page(s)" + (
        ", truncated" if truncated else ""
    ) + ")\n\n"
    return text_result(_wrap_untrusted(header + full, source=f"pdf:{url}"))


def _wrap_untrusted(text: str, *, source: str) -> str:
    return (
        f"<untrusted_content source={source!r}>\n"
        f"{text}\n"
        f"</untrusted_content>"
    )
