"""Read a Mattermost thread by permalink and return its messages.

Wraps :meth:`ChatPort.read_thread` with URL parsing. Recursion into
linked threads / Jira / KB is **not** automatic — the agent decides.
The description tells the agent what links to look at; if it wants
their content it calls this tool (or the Jira / KB tools) again.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool
from loguru import logger

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import error_text, parse_mm_post_id, text_result
from virtual_dev.tools.read_jira_attachment_pdf import _wrap_untrusted

TOOL_GROUP = "researcher"

_DEFAULT_MAX_CHARS = 30_000


def build(ctx: ToolContext):
    if ctx.chat is None:
        return None
    chat = ctx.chat

    @tool(
        "read_mattermost_thread",
        "Read a Mattermost thread by permalink (e.g. "
        "`https://mm.example.com/team/pl/<post_id>`) and return its "
        "messages chronologically with author + timestamp + text. "
        "Output is wrapped as untrusted content. \n\n"
        "**Recursion is your call**: the result will list any links "
        "(other MM threads, Jira tickets, Confluence pages) found in "
        "the thread under a `links` section. Decide whether each one "
        "is worth opening — if yes, call the matching tool "
        "(`read_mattermost_thread`, `fetch_url`, "
        "`read_jira_attachment_*`). Don't blindly recurse — only "
        "follow links that look load-bearing for the ticket. "
        "Truncates at max_chars (default 30000).",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
    )
    async def _read_thread(args: dict[str, Any]) -> dict[str, Any]:
        return await run(chat, args)

    return _read_thread


async def run(chat, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    max_chars = int(args.get("max_chars") or _DEFAULT_MAX_CHARS)
    if not url:
        return error_text("Empty URL")
    post_id = parse_mm_post_id(url)
    if post_id is None:
        return error_text(
            f"Couldn't extract post id from {url!r}. Expected a "
            f"Mattermost permalink like `<host>/team/pl/<post_id>`."
        )
    try:
        messages = await chat.read_thread(post_id)
    except Exception as exc:
        logger.exception("read_mattermost_thread: fetch failed")
        return error_text(f"Mattermost fetch failed: {exc}")

    if not messages:
        return text_result(_wrap_untrusted(
            f"# MM thread: {url}\n(no messages — post may be deleted "
            f"or you may lack access)",
            source=f"mm:thread:{post_id}",
        ))

    lines: list[str] = [f"# MM thread: {url}", f"(post_id={post_id})", ""]
    found_links: set[str] = set()
    for msg in messages:
        ts = msg.timestamp.isoformat() if msg.timestamp else "?"
        author = msg.author_id or "?"
        text = (msg.text or "").strip()
        lines.append(f"## {author} @ {ts}")
        lines.append(text)
        lines.append("")
        for marker in _extract_links(text):
            found_links.add(marker)

    if found_links:
        lines.append("## links found in thread")
        for link in sorted(found_links):
            lines.append(f"* {link}")

    full = "\n".join(lines)
    truncated = False
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n... [truncated, {len(full)} chars total]"
        truncated = True
    if truncated:
        # Note in the header so the agent knows to ask for max_chars more.
        full = full.replace(
            f"# MM thread: {url}",
            f"# MM thread: {url} (truncated)",
            1,
        )
    return text_result(_wrap_untrusted(full, source=f"mm:thread:{post_id}"))


def _extract_links(text: str) -> list[str]:
    """Pull http(s) URLs out of a message body. Cheap regex; the
    agent gets to judge which are worth following."""
    import re
    return re.findall(r"https?://[^\s<>()\"']+", text)
