# `virtual_dev.tools/` — auto-discovered agent tools

This directory is the single registry for tools the analyst agent can
call. **Add a tool by dropping a `<name>.py` file here**; remove a tool
by deleting its file. The loader picks up the change at the next
process start — there's no central list to update.

If you're here because you want to teach the bot to read DOCX
attachments, follow the *Adding a new tool* recipe below — your file
goes alongside the existing ones.

## How discovery works

`build_tool_servers(ctx)` walks the package, imports every public
submodule, and asks each to build its tool. Files whose name starts
with `_` (`_loader.py`, `_context.py`, `_wrap.py`, …) are private
internals and are skipped.

The loader expects each tool module to expose:

```python
# src/virtual_dev/tools/<name>.py

from claude_agent_sdk import tool
from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"   # optional; defaults to "analyst". Picks
                         # which MCP server name the tool ends up
                         # under. Use "researcher" for read-only
                         # research tools.


def build(ctx: ToolContext):
    """Build and return one SdkMcpTool. Return None to opt out
    (e.g. an optional dependency on ctx is missing)."""
    if ctx.communicator is None:    # example: tool needs chat
        return None
    communicator = ctx.communicator

    @tool(
        "<name>",                   # the LLM-facing tool name
        "<one-paragraph description, used by the LLM to decide "
        "when to call this tool>",
        {                           # JSON Schema for inputs
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    async def _impl(args: dict) -> dict:
        # Do the work, return MCP content blocks via wrap_text.
        return wrap_text({"result": "..."})

    return _impl
```

The result of `build()` is the value the `@tool` decorator returns:
an `SdkMcpTool`. The loader collects all returned tools, groups them
by `TOOL_GROUP`, and creates one MCP server per group.

## What's in `ToolContext`

```python
@dataclass
class ToolContext:
    communicator:  CommunicatorService | None    # send_dm, resolve_user_id, search_users_by_name
    researcher:    ResearcherToolkit | None      # repo / KB / MR-history bundles
    effects:       list[AnalystEffect] | None    # append to record side-effects
    plan_capture:  dict | None                   # populate from submit_plan
    run_state:     dict | None                   # one-ASK-per-run flags
    extras:        dict                          # ad-hoc, never None
```

Long-lived fields (`communicator`, `researcher`) are non-None while the
agent is configured. The per-run buckets (`effects`, `plan_capture`,
`run_state`) are only set during an actual analyst run; they're `None`
otherwise. **A tool that mutates them must check for `None` and return
`None` from `build()` if it can't function**, which the loader treats
as "skip me."

## Adding a new tool — recipe

1. Pick a short, action-first name (`read_docx_attachment`, not
   `docx_handler`). The name becomes the LLM-visible identifier.
2. Decide the group (`analyst` for things only the analyst calls;
   `researcher` for read-only research tools that may also be exposed
   to the dev agent later).
3. Create `src/virtual_dev/tools/<name>.py` from the template above.
4. Restart the process. The loader picks the tool up at import time.

The LLM learns about your new tool **automatically** — the SDK passes
the `@tool` name + description + schema as part of the tool list on
every run. You do **not** need to update `config/prompts/analyst.md`
just to make the model aware the tool exists.

**Special semantics live in the tool's `description` string.** If
your tool is async (ends the agent's turn), has irreversible
side-effects, can only be called once, must be sequenced after
another tool, or returns data the model should treat as untrusted —
spell that out in the `description` arg of `@tool(...)`. The LLM
reads it on every run; the prompt doesn't have to repeat it. See
`dm_user.py` for an example: its description carries the "**THIS IS
ASYNC** — END YOUR TURN after calling" semantics, so `analyst.md`
doesn't need to.

The only time you edit `analyst.md` is to add **cross-tool strategy**
that doesn't belong in any single tool's description: ordering rules
that span multiple tools ("find_chat_user_by_name BEFORE dm_user"),
budget rules ("one dm_user per run"), or workflow gates ("re-read the
ticket before calling submit_plan"). Most simple fetch-and-return
tools need none of that.

## Removing a tool

Delete the file. That's it. If you also added strategy text in
`analyst.md` about it, drop those rules.

## Conventions

* **One tool per file.** Don't bundle two unrelated tools into one
  module — it makes the registry harder to scan.
* **Description is the LLM's only API doc** for your tool. Spell out
  when to use it, what it returns, and what NOT to use it for.
* **Wrap responses with `wrap_text(...)`** — Claude expects MCP
  content blocks, not raw dicts.
* **Use `args.get(...)` defensively** — the SDK passes whatever JSON
  the LLM produced, including missing fields and surprising types.
* **No business state in module-level code.** All mutable state lives
  on `ToolContext`. Module load must be side-effect-free, since the
  loader imports every submodule on every agent start.
* **Tests:** unit-test the implementation directly when behaviour is
  non-trivial. The loader is covered by `tests/unit/test_tools_loader.py`.
