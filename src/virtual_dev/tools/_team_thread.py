"""Late replies in the team thread — the ones the analyst hasn't seen yet.

While the analyst works, the inbox isn't waiting on the thread, so a
teammate's «и ещё…» posted mid-run reaches nobody. ``ask_team`` and
``submit_plan`` call this right before acting: anything new is shown to
the analyst first, and recorded so the next run doesn't see it twice.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage

# Parses the bot's own post, never a human's: whom it @-asked to answer.
_MENTION = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])")


def unseen_replies(
    posts: list[ChatMessage], seen_post_ids: set[str],
) -> list[ChatMessage]:
    fresh = [
        post for post in posts
        if not post.trusted and post.id not in seen_post_ids and post.text.strip()
    ]
    return sorted(fresh, key=lambda post: post.timestamp)


async def collect_late_replies(
    communicator: CommunicatorService,
    run_state: dict[str, Any],
    effects: list[AnalystEffect],
) -> str | None:
    """Signed text of the replies posted since the analyst last looked,
    or ``None`` when there are none (or no team thread yet)."""
    thread = run_state.get("team_thread")
    if not thread:
        return None
    try:
        posts = await communicator.read_thread(thread["root_id"])
    except Exception:
        # An MM outage shouldn't freeze the ticket; the inbox's catch-up
        # still delivers anything we miss here once the bot waits again.
        logger.warning("Team thread {} unreadable — skipping late-reply check", thread["root_id"])
        return None
    seen: set[str] = run_state.setdefault("team_seen_post_ids", set())
    fresh = unseen_replies(posts, seen)
    if not fresh:
        return None
    text = await communicator.sign_posts(fresh)
    post_ids = [post.id for post in fresh]
    seen.update(post_ids)
    run_state["proposal_confirmed"] = True
    effects.append(AnalystEffect(
        kind="team_replies_seen", payload={"post_ids": post_ids, "text": text},
    ))
    return text


async def unanswered_mentions(
    communicator: CommunicatorService,
    run_state: dict[str, Any],
    *,
    go_ahead_from: str | None = None,
) -> list[str]:
    """People the analyst @-asked in its latest thread post who haven't
    written there since. DM-3784: it asked two devs to confirm a changed
    status meaning, got silence, and shipped the plan anyway.

    ``go_ahead_from`` lifts the wait when that person, posting after the
    ask, told the analyst to proceed without the others."""
    thread = run_state.get("team_thread")
    if not thread:
        return []
    try:
        posts = await communicator.read_thread(thread["root_id"])
    except Exception:
        logger.warning("Team thread {} unreadable — skipping mention check", thread["root_id"])
        return []
    own = [post for post in posts if post.trusted]
    if not own:
        return []
    last_ask = max(own, key=lambda post: post.timestamp)
    asked = list(dict.fromkeys(_MENTION.findall(last_ask.text)))
    if not asked:
        return []
    answered: set[str] = set()
    for post in posts:
        if post.trusted or post.timestamp <= last_ask.timestamp:
            continue
        handle = await communicator.author_handle(post.author_id)
        if handle:
            answered.add(handle)
    if go_ahead_from and go_ahead_from.lstrip("@") in answered:
        return []
    return [handle for handle in asked if handle not in answered]


def late_replies_result(text: str, *, root_id: str) -> dict[str, Any]:
    return {
        "reason": "new_team_replies",
        "new_replies": (
            f"<untrusted_content source={f'mm:thread:{root_id}'!r}>\n"
            f"{text}\n</untrusted_content>"
        ),
        "instruction": (
            "The team wrote more in the thread while you were working "
            "(above). Take it into account: if it changes or questions "
            "the plan, answer in the thread with ask_team; if it's fine "
            "as is, call the tool again."
        ),
    }
