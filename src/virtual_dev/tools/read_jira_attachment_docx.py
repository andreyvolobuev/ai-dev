"""Download a DOCX attached to a Jira ticket and return its text."""

from __future__ import annotations

import asyncio
import io
from typing import Any

from claude_agent_sdk import tool
from docx import Document
from loguru import logger

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import (
    error_text,
    fetch_jira_attachment_content,
    parse_jira_attachment_id,
    text_result,
)
from virtual_dev.tools.read_jira_attachment_pdf import _wrap_untrusted

TOOL_GROUP = "researcher"

_DEFAULT_MAX_CHARS = 30_000


def build(ctx: ToolContext):
    if ctx.settings is None:
        return None
    settings = ctx.settings

    @tool(
        "read_jira_attachment_docx",
        "Download a DOCX attached to a Jira ticket and return its "
        "extracted text (paragraphs joined; tables flattened to "
        "tab-separated rows). Pass the full URL as it appears in the "
        "ticket description. Authenticated with the Jira PAT. Output "
        "is wrapped as untrusted content. Truncates at max_chars "
        "(default 30000).",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
    )
    async def _read_docx(args: dict[str, Any]) -> dict[str, Any]:
        return await run(settings, args)

    return _read_docx


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
        logger.exception("read_jira_attachment_docx: download failed")
        return error_text(f"Download failed (attachment {attachment_id}): {exc}")

    try:
        doc = await asyncio.to_thread(Document, io.BytesIO(body))
        parts: list[str] = []
        for para in doc.paragraphs:
            text = (para.text or "").strip()
            if text:
                parts.append(text)
        for table in doc.tables:
            for row in table.rows:
                cells = [(c.text or "").strip() for c in row.cells]
                parts.append("\t".join(cells))
    except Exception as exc:
        logger.exception("read_jira_attachment_docx: DOCX parse failed")
        return error_text(f"DOCX parse failed: {exc}")

    full = "\n".join(parts)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated, {len(full)} chars total]"
        truncated = True
    header = f"# DOCX: {url}" + (" (truncated)" if truncated else "") + "\n\n"
    return text_result(_wrap_untrusted(header + full, source=f"jira:docx:{url}"))
