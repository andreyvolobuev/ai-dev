"""Tool-surface enforcement in the shared Claude Agent SDK adapter.

``allowed_tools`` is an allow-RULE list, not a tool filter: under
``permission_mode="bypassPermissions"`` the CLI keeps every built-in
callable whatever it contains. Verified empirically against the live CLI
with an intake-shaped surface (only the submit MCP tool allowed): ``Bash
echo hello``, ``Bash pwd`` and ``Read <abs path>`` all went through, and
``pwd`` printed the repo root. ``disallowed_tools`` → ``--disallowedTools``
is what actually blocks them; the same probe with the deny list came back
"No such tool available: Bash".

The adapter is shared with analyst / dev / responder, which pass no deny
list. Their options must therefore come out exactly as before the knob
existed — that is what the "untouched" test below pins.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from virtual_dev.adapters.code_agent.claude_sdk import ClaudeAgentSdkCodeAgent
from virtual_dev.domain.ports.code_agent import CodeAgentRequest

_DENY = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]


def _agent() -> ClaudeAgentSdkCodeAgent:
    return ClaudeAgentSdkCodeAgent(default_model="claude-sonnet-4-6")


def _request(**extras: object) -> CodeAgentRequest:
    request = CodeAgentRequest(
        agent_key="dev-x",
        system_prompt="sys",
        user_prompt="do the thing",
        working_dir="/tmp/workspace",
        max_turns=7,
    )
    request.extras.update(extras)
    return request


def test_deny_list_reaches_the_sdk_options() -> None:
    options = _agent()._build_options(
        _request(), None, ["mcp__x__submit"], None, denied_tool_names=_DENY,
    )

    assert options.disallowed_tools == _DENY
    # The allow-list is still passed — the deny list narrows, not replaces.
    assert options.allowed_tools == ["mcp__x__submit"]


def test_extras_key_is_the_wire_into_the_deny_list() -> None:
    """``run_task`` reads ``extras["disallowed_tool_names"]`` — the name the
    agents set. A typo there would silently leave the surface wide open."""
    request = _request(
        allowed_tool_names=["mcp__x__submit"],
        disallowed_tool_names=_DENY,
    )
    options = _agent()._build_options(
        request, None, ["mcp__x__submit"], None,
        denied_tool_names=list(request.extras["disallowed_tool_names"]),  # type: ignore[arg-type]
    )

    assert options.disallowed_tools == _DENY


def test_options_for_an_agent_without_a_deny_list_are_unchanged() -> None:
    """Analyst / dev / responder pass no deny list. Their options must be
    identical to what the pre-change adapter produced: the key is omitted,
    never set to ``[]``, so nothing about their run shifts."""
    agent = _agent()
    options = agent._build_options(_request(), None, ["Read", "Bash"], None)

    expected = ClaudeAgentOptions(
        system_prompt="sys",
        max_turns=7,
        permission_mode="bypassPermissions",
        model="claude-sonnet-4-6",
        cwd="/tmp/workspace",
        allowed_tools=["Read", "Bash"],
    )
    assert vars(options) == vars(expected)
    # The SDK default: an empty list means "no --disallowedTools flag".
    assert options.disallowed_tools == []


def test_explicit_empty_deny_list_adds_no_flag() -> None:
    """An empty list is "no opinion", not "deny nothing explicitly" —
    otherwise a caller building the list dynamically would start emitting
    a flag the pre-change adapter never emitted."""
    options = _agent()._build_options(
        _request(), None, ["Read"], None, denied_tool_names=[],
    )

    assert options.disallowed_tools == []
