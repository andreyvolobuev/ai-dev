"""Ask the whole team in a team-channel thread. ASYNC — ends the analyst's turn."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.tools import ToolContext, wrap_text
from virtual_dev.tools._helpers import strip_agent_attribution
from virtual_dev.tools._team_thread import collect_late_replies, late_replies_result

TOOL_GROUP = "analyst"


def build(ctx: ToolContext):
    if ctx.communicator is None or ctx.effects is None or ctx.run_state is None:
        return None
    communicator = ctx.communicator
    effects = ctx.effects
    run_state = ctx.run_state

    @tool(
        "ask_team",
        "Post a question or your proposal to the team channel, in the "
        "ticket's discussion thread, and wait for the team. Use it for "
        "anything the TEAM has to agree on: design decisions the ticket "
        "doesn't pin down, unratified spec items, scope expansions, "
        "schema migrations, «в спорных моментах созывай ОПГ»-style "
        "directives. The first call opens a new thread (the ticket link "
        "is added automatically); later calls reply in the same thread. "
        "Several people may answer; you'll be re-invoked with every new "
        "reply, author-attributed. `message` must read like a human "
        "typed it: state your proposal, then ask — short paragraphs, "
        "~600 chars max, no headings. You may @-mention people whose "
        "opinion is needed. **THIS IS ASYNC** — after calling, END YOUR "
        "TURN. Do NOT call any other tools after this in the same turn.",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )
    async def _ask_team(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "sent": False,
                "reason": "already_dispatched_this_run",
                "instruction": "You already asked this turn. END YOUR TURN now.",
            })
        if run_state.get("terminal"):
            return wrap_text({
                "sent": False, "reason": "after_terminal",
                "instruction": "You already called a terminal tool. End your turn.",
            })
        message = strip_agent_attribution(str(args.get("message") or ""))
        if not message:
            return wrap_text({"sent": False, "reason": "empty_message"})

        thread = run_state.get("team_thread")
        late = await collect_late_replies(communicator, run_state, effects)
        if late is not None and thread:
            return wrap_text({
                "sent": False, **late_replies_result(late, root_id=thread["root_id"]),
            })
        if thread:
            channel_id = thread["channel_id"]
            root_id: str | None = thread["root_id"]
            text = message
        else:
            channel_id = run_state.get("team_channel_id")
            root_id = None
            ticket_ref = run_state.get("ticket_ref")
            text = f"{ticket_ref}\n\n{message}" if ticket_ref else message
        if not channel_id:
            return wrap_text({
                "sent": False, "reason": "no_team_channel",
                "hint": (
                    "No team channel is configured for this repo. Put the "
                    "same proposal to the ticket reporter via dm_user."
                ),
            })

        outcome = await communicator.send_channel(
            channel_id, text, thread_root_id=root_id,
        )
        if not outcome.sent or outcome.message is None:
            return wrap_text({
                "sent": False,
                "reason": f"send_failed:{outcome.skip_reason or 'unknown'}",
            })
        # Replies are routed by thread root, so the root — not this
        # post's id — is what the inbox must wait on.
        thread_root = root_id or outcome.message.id
        effects.append(AnalystEffect(
            kind="ask_dispatched",
            payload={
                "audience": "team",
                "asked_post_id": thread_root,
                "channel_id": channel_id,
                "target_user_id": "",
                "target_username": None,
                "target_email": None,
                "asked_text": message,
                "dedupe_key": None,
            },
        ))
        run_state["ask_dispatched"] = True
        return wrap_text({
            "sent": True, "channel_id": channel_id, "thread_root_id": thread_root,
            "instruction": (
                "Posted to the team thread. END YOUR TURN now. You'll be "
                "re-invoked with the team's replies."
            ),
        })

    return _ask_team
