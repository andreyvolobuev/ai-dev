"""CodeAgentPort backed by ``claude-agent-sdk``.

The SDK spawns the ``claude`` CLI as a subprocess and reuses the logged-in
Claude Code (Claude Max) session — no API key needed, no per-task budget
to enforce.

We translate :class:`CodeAgentRequest` into ``ClaudeAgentOptions``, drain
the message stream, and produce a :class:`CodeAgentResult` from the final
``ResultMessage``. ``max_turns`` is wired through as a runaway-loop guard;
token / dollar fields in the result are informational estimates, never
used for enforcement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from claude_agent_sdk.types import (
    McpSdkServerConfig,  # type: ignore[attr-defined]
)
from loguru import logger

from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)


class ClaudeAgentSdkCodeAgent(CodeAgentPort):
    """Drive a Claude Code session via ``claude-agent-sdk``.

    ``mcp_servers`` lets callers inject in-process Python tools (see the
    Researcher service). If not supplied, the SDK defaults apply.
    """

    def __init__(
        self,
        *,
        default_model: str | None = None,
        permission_mode: str = "bypassPermissions",
        cli_path: str | None = None,
    ) -> None:
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._cli_path = cli_path

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        mcp_servers = _as_mcp_servers(request.extras.get("mcp_servers"))
        allowed_tool_names = _as_allowed_tools(request.extras.get("allowed_tool_names"))
        options = self._build_options(request, mcp_servers, allowed_tool_names)

        final_text_parts: list[str] = []
        cost_usd = 0.0
        turns = 0
        input_tokens = 0
        output_tokens = 0
        stop_reason = "unknown"

        async for event in query(prompt=request.user_prompt, options=options):
            if isinstance(event, AssistantMessage):
                for block in event.content:
                    if isinstance(block, TextBlock):
                        final_text_parts.append(block.text)
            elif isinstance(event, ResultMessage):
                cost_usd = float(event.total_cost_usd or 0.0)
                turns = int(event.num_turns or 0)
                stop_reason = event.stop_reason or ("error" if event.is_error else "end_turn")
                usage = cast(dict[str, Any], event.usage or {})
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                if event.is_error:
                    logger.warning(
                        "Claude Agent SDK reported error for agent={} stop={}",
                        request.agent_key,
                        stop_reason,
                    )

        return CodeAgentResult(
            final_text="\n".join(final_text_parts),
            turns=turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            stopped_reason=stop_reason,
        )

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:
        mcp_servers = _as_mcp_servers(request.extras.get("mcp_servers"))
        allowed_tool_names = _as_allowed_tools(request.extras.get("allowed_tool_names"))
        options = self._build_options(request, mcp_servers, allowed_tool_names)

        async def _iter() -> AsyncIterator[str]:
            async for event in query(prompt=request.user_prompt, options=options):
                if isinstance(event, AssistantMessage):
                    for block in event.content:
                        if isinstance(block, TextBlock):
                            yield block.text

        return _iter()

    # --- helpers ---

    def _build_options(
        self,
        request: CodeAgentRequest,
        mcp_servers: dict[str, McpSdkServerConfig] | None,
        allowed_tool_names: Iterable[str] | None,
    ) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "system_prompt": request.system_prompt or None,
            "max_turns": request.max_turns,
            "permission_mode": self._permission_mode,
            "model": request.model or self._default_model,
        }
        if request.working_dir:
            kwargs["cwd"] = request.working_dir
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if allowed_tool_names is not None:
            kwargs["allowed_tools"] = list(allowed_tool_names)
        if self._cli_path:
            kwargs["cli_path"] = self._cli_path
        return ClaudeAgentOptions(**kwargs)


def _as_mcp_servers(value: object) -> dict[str, McpSdkServerConfig] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(
            f"extras['mcp_servers'] must be a dict, got {type(value).__name__}"
        )
    return cast(dict[str, McpSdkServerConfig], value)


def _as_allowed_tools(value: object) -> Iterable[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise TypeError(
            f"extras['allowed_tool_names'] must be list/tuple, got {type(value).__name__}"
        )
    return cast(list[str], list(value))
