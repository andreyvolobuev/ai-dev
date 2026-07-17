"""DM a chat-platform user one question. ASYNC — ends the analyst's turn."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"


def build(ctx: ToolContext):
    if ctx.communicator is None or ctx.effects is None or ctx.run_state is None:
        return None
    communicator = ctx.communicator
    effects = ctx.effects
    run_state = ctx.run_state

    @tool(
        "dm_user",
        "Send a direct message to a chat-platform user with one "
        "question. `message` must read like a human typed it: 1-3 "
        "short sentences, ~350 chars max, no headings, no analysis "
        "dumps — state the conclusion and ask. If it carries more than "
        "one thought, split into short paragraphs with a blank line "
        "(questions may be bullet points) instead of one dense block. "
        "The `message` argument is the ONLY thing the human "
        "sees — your reasoning text, llm_text blocks, and tool-result "
        "summaries are invisible to them. So if you want to "
        "acknowledge or answer something they said, the "
        "acknowledgement must be INSIDE `message=`, not in your "
        "thinking. Pass to_handle OR to_email. **THIS IS ASYNC** — "
        "after calling, END YOUR TURN; you'll be re-invoked when "
        "the human replies. Do NOT call any other tools after this "
        "in the same turn.",
        {
            "type": "object",
            "properties": {
                "to_handle": {"type": ["string", "null"]},
                "to_email": {"type": ["string", "null"]},
                "message": {"type": "string"},
                "dedupe_key": {"type": ["string", "null"]},
            },
            "required": ["message"],
        },
    )
    async def _ask(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "sent": False,
                "reason": "already_dispatched_this_run",
                "instruction": (
                    "You already called dm_user once this turn. "
                    "ASK is async — END YOUR TURN now. The "
                    "orchestrator will re-invoke you when the human "
                    "replies, and only then you can ask another "
                    "person."
                ),
            })
        if run_state.get("terminal"):
            return wrap_text({
                "sent": False, "reason": "after_terminal",
                "instruction": "You already called a terminal tool. End your turn.",
            })
        handle = (args.get("to_handle") or "").strip().lstrip("@") or None
        email = (args.get("to_email") or "").strip() or None
        message = str(args.get("message") or "").strip()
        dedupe_key = (args.get("dedupe_key") or "").strip() or None
        if not message:
            return wrap_text({"sent": False, "reason": "empty_message"})
        if not handle and not email:
            return wrap_text({"sent": False, "reason": "missing_target"})

        # Destination whitelist. Defends against prompt-injection
        # ("ask @ceo about priority") inside a ticket steering the
        # analyst into DMing arbitrary people. Allowed: ticket
        # reporter, configured escalation contact, anyone the analyst
        # has DMed in this conversation already. Empty whitelist =
        # refuse everything (safe default for misconfigured runs).
        allowed_handles = {
            h.lower() for h in (run_state.get("allowed_dm_handles") or set())
        }
        allowed_emails = {
            e.lower() for e in (run_state.get("allowed_dm_emails") or set())
        }
        handle_ok = handle is not None and handle.lower() in allowed_handles
        email_ok = email is not None and email.lower() in allowed_emails
        if not (handle_ok or email_ok):
            return wrap_text({
                "sent": False,
                "reason": "destination_not_allowed",
                "hint": (
                    "dm_user accepts only: the ticket reporter, the "
                    "team's escalation contact, or someone you've "
                    "already DMed in this conversation. Re-read the "
                    "ticket for the reporter; do not guess."
                ),
            })

        uid = await communicator.resolve_user_id(username=handle, email=email)
        if uid is None:
            label = handle or email or ""
            return wrap_text({
                "sent": False, "reason": f"unresolved:{label}",
                "hint": (
                    "Don't guess transliterations. "
                    "find_chat_user_by_name first, or DM the issue "
                    "reporter for a confirmed handle."
                ),
            })
        # Mirror the recipient's reply mode. ``analyst.py`` builds the
        # ``dm_threads`` map by walking history: a recipient gets a
        # thread anchor here only if their LATEST reply landed inside
        # the thread under our previous question. Top-level repliers
        # (and brand-new recipients) hit the fall-through and we send
        # a plain top-level DM.
        dm_threads = run_state.get("dm_threads") or {}
        thread_anchor = dm_threads.get(uid)
        if thread_anchor:
            outcome = await communicator.send_dm(
                uid, message,
                thread_channel_id=thread_anchor.get("channel_id"),
                thread_root_id=thread_anchor.get("root_id"),
            )
        else:
            outcome = await communicator.send_dm(uid, message)
        if not outcome.sent or outcome.message is None:
            return wrap_text({
                "sent": False,
                "reason": f"send_failed:{outcome.skip_reason or 'unknown'}",
            })
        effects.append(AnalystEffect(
            kind="ask_dispatched",
            payload={
                "asked_post_id": outcome.message.id,
                "channel_id": outcome.message.channel_id,
                "target_user_id": uid,
                "target_username": handle,
                "target_email": email,
                "asked_text": message,
                "dedupe_key": dedupe_key,
            },
        ))
        run_state["ask_dispatched"] = True
        return wrap_text({
            "sent": True, "to_user_id": uid,
            "channel_id": outcome.message.channel_id,
            "asked_post_id": outcome.message.id,
            "instruction": (
                "DM dispatched. END YOUR TURN now. The orchestrator "
                "will re-invoke you with the human's reply."
            ),
        })

    return _ask
