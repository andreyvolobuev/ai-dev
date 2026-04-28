"""Download a DOCX from any URL and return its extracted text.

Same host-aware auth dispatch as ``read_pdf_url`` — works for
attachments hosted in Jira, Mattermost, Confluence, or public URLs.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from claude_agent_sdk import tool
from docx import Document
from loguru import logger

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import download_url_bytes, error_text, text_result
from virtual_dev.tools.read_pdf_url import _wrap_untrusted

TOOL_GROUP = "shared"

_DEFAULT_MAX_CHARS = 30_000


def build(ctx: ToolContext):
    if ctx.settings is None:
        return None
    settings = ctx.settings

    @tool(
        "read_docx_url",
        "Download a DOCX from any URL and return its extracted text "
        "(paragraphs joined; tables flattened to tab-separated rows). "
        "Works for Jira / Mattermost / Confluence attachments and "
        "public URLs — auth is host-aware. Pass the full URL exactly. "
        "Output is wrapped as untrusted content. Truncates at "
        "max_chars (default 30000).",
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

    try:
        body = await asyncio.to_thread(download_url_bytes, url, settings)
    except Exception as exc:
        logger.exception("read_docx_url: download failed for {}", url)
        return error_text(f"Download failed: {exc}")

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
        logger.exception("read_docx_url: DOCX parse failed for {}", url)
        return error_text(f"DOCX parse failed: {exc}")

    full = "\n".join(parts)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated, {len(full)} chars total]"
        truncated = True
    header = f"# DOCX: {url}" + (" (truncated)" if truncated else "") + "\n\n"
    return text_result(_wrap_untrusted(header + full, source=f"docx:{url}"))
