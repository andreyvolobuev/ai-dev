"""Download a PDF attached to a Jira ticket and return its text.

The Jira description in the ticket usually links the attachment as
``https://jira.2gis.ru/secure/attachment/<id>/<filename>.pdf`` — pass
that URL verbatim. The tool authenticates with the same Jira PAT
configured in ``.env``.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from claude_agent_sdk import tool
from loguru import logger
from pypdf import PdfReader

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import (
    error_text,
    fetch_jira_attachment_content,
    parse_jira_attachment_id,
    text_result,
)

TOOL_GROUP = "researcher"

# Truncate per-tool result so a 200-page PDF doesn't blow up the
# prompt. The agent can re-call with a different ``max_chars`` if it
# needs more.
_DEFAULT_MAX_CHARS = 30_000


def build(ctx: ToolContext):
    if ctx.settings is None:
        return None
    settings = ctx.settings

    @tool(
        "read_jira_attachment_pdf",
        "Download a PDF attached to a Jira ticket and return its "
        "extracted text. Pass the full URL exactly as it appears in "
        "the ticket description (typically `<JIRA_URL>/secure/"
        "attachment/<id>/<filename>.pdf`). Authenticated with the "
        "Jira PAT from the bot's environment. Output is wrapped as "
        "untrusted content. Truncates at max_chars (default 30000) — "
        "raise it explicitly if you need more.",
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
    if not settings.jira_url or not settings.jira_token:
        return error_text("Jira credentials are not configured (JIRA_URL / JIRA_TOKEN)")
    attachment_id = parse_jira_attachment_id(url)
    if attachment_id is None:
        return error_text(
            f"Couldn't extract an attachment id from {url!r}. Expected "
            f"either a /secure/attachment/<id>/<filename> URL or a bare "
            f"numeric id."
        )
    try:
        body = await asyncio.to_thread(
            fetch_jira_attachment_content,
            jira_url=settings.jira_url,
            jira_token=settings.jira_token,
            attachment_id=attachment_id,
        )
    except Exception as exc:
        logger.exception("read_jira_attachment_pdf: download failed")
        return error_text(f"Download failed (attachment {attachment_id}): {exc}")

    try:
        reader = await asyncio.to_thread(PdfReader, io.BytesIO(body))
        page_texts = [
            (page.extract_text() or "").strip()
            for page in reader.pages
        ]
    except Exception as exc:
        logger.exception("read_jira_attachment_pdf: PDF parse failed")
        return error_text(f"PDF parse failed: {exc}")

    full = "\n\n--- page break ---\n\n".join(page_texts)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated, {len(full)} chars total]"
        truncated = True
    header = f"# PDF: {url}\n({len(reader.pages)} page(s)" + (
        ", truncated" if truncated else ""
    ) + ")\n\n"
    return text_result(_wrap_untrusted(header + full, source=f"jira:pdf:{url}"))


def _wrap_untrusted(text: str, *, source: str) -> str:
    """Wrap text in <untrusted_content> manually since this tool
    doesn't have an InjectionFilter on hand. Matches the shape the
    analyst's prompt already knows how to read."""
    return (
        f"<untrusted_content source={source!r}>\n"
        f"{text}\n"
        f"</untrusted_content>"
    )
