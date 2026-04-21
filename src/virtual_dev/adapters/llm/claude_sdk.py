"""LlmPort backed by ``claude-agent-sdk``, tools disabled.

For lightweight single-shot calls (e.g. Communicator thread summaries).
Same subprocess mechanism as :class:`ClaudeAgentSdkCodeAgent` — just locks
down the agent loop to one turn with no tools.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from virtual_dev.domain.ports.llm import LlmMessage, LlmPort, LlmResponse


class ClaudeAgentSdkLlm(LlmPort):
    """Single-shot completions via the Claude Agent SDK.

    The SDK's prompt API is string-based (one turn), so multi-message
    ``messages`` lists are flattened to a single prompt. System text is
    passed separately via ``ClaudeAgentOptions.system_prompt``.
    """

    def __init__(
        self,
        *,
        permission_mode: str = "bypassPermissions",
        cli_path: str | None = None,
    ) -> None:
        self._permission_mode = permission_mode
        self._cli_path = cli_path

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        system: str | None = None,
    ) -> LlmResponse:
        prompt = _render_prompt(messages)
        options = self._options(model=model, system=system)

        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = "unknown"

        async for event in query(prompt=prompt, options=options):
            if isinstance(event, AssistantMessage):
                for block in event.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(event, ResultMessage):
                stop_reason = event.stop_reason or ("error" if event.is_error else "end_turn")
                usage = event.usage or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)

        return LlmResponse(
            text="".join(text_parts),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            model=model,
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        prompt = _render_prompt(messages)
        options = self._options(model=model, system=system)

        async def _iter() -> AsyncIterator[str]:
            async for event in query(prompt=prompt, options=options):
                if isinstance(event, AssistantMessage):
                    for block in event.content:
                        if isinstance(block, TextBlock):
                            yield block.text

        return _iter()

    def _options(self, *, model: str, system: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=system,
            model=model,
            permission_mode=self._permission_mode,
            max_turns=1,
            allowed_tools=[],          # no tools; this is a plain completion
            cli_path=self._cli_path,
        )


def _render_prompt(messages: list[LlmMessage]) -> str:
    """Flatten a conversation into a single string.

    ``system`` messages are expected to be hoisted into
    ``ClaudeAgentOptions.system_prompt`` by the caller, so we ignore them here.
    """
    lines: list[str] = []
    for m in messages:
        role = m.role.lower()
        if role == "system":
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {m.content}")
    return "\n\n".join(lines).strip()
