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
    ToolUseBlock,
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
        stderr_lines: list[str] = []
        options = self._build_options(
            request, mcp_servers, allowed_tool_names, stderr_lines,
        )

        final_text_parts: list[str] = []
        cost_usd = 0.0
        turns = 0
        input_tokens = 0
        output_tokens = 0
        stop_reason = "unknown"
        got_result = False

        tool_use_count = 0
        try:
            async for event in query(prompt=request.user_prompt, options=options):
                if isinstance(event, AssistantMessage):
                    for block in event.content:
                        if isinstance(block, TextBlock):
                            final_text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_use_count += 1
                            logger.info(
                                "[{}] tool_use #{}: {} {}",
                                request.agent_key,
                                tool_use_count,
                                block.name,
                                _tool_input_preview(block.input),
                            )
                elif isinstance(event, ResultMessage):
                    got_result = True
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
        except Exception as exc:
            # When the CLI hits max_turns mid-tool-use, it emits a final
            # ResultMessage and THEN exits with code 1 — which the SDK surfaces
            # as a generic "Command failed with exit code 1" exception with no
            # stderr content. We've already captured the ResultMessage above,
            # so this trailing exit is a soft timeout, not a crash: swallow it
            # and let the caller see stop_reason=max_turns.
            if got_result:
                logger.info(
                    "claude CLI exited after ResultMessage for agent={} "
                    "(likely max_turns soft-timeout; stop={}): {}",
                    request.agent_key, stop_reason, exc,
                )
                if stop_reason in ("tool_use", "unknown"):
                    stop_reason = "max_turns"
            else:
                if stderr_lines:
                    logger.error(
                        "claude CLI stderr for agent={} (last {} lines):\n{}",
                        request.agent_key,
                        len(stderr_lines),
                        "\n".join(stderr_lines[-200:]),
                    )
                raise
        finally:
            if stderr_lines:
                logger.debug(
                    "claude CLI stderr for agent={} ({} lines captured)",
                    request.agent_key,
                    len(stderr_lines),
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
        options = self._build_options(request, mcp_servers, allowed_tool_names, None)

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
        stderr_sink: list[str] | None,
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
        if stderr_sink is not None:
            # Capture CLI subprocess stderr line-by-line. Surfaced in logs on
            # failure so "Command failed with exit code 1 — check stderr" is
            # actually actionable.
            def _on_stderr(line: str) -> None:
                stderr_sink.append(line)
            kwargs["stderr"] = _on_stderr
        return ClaudeAgentOptions(**kwargs)


def _tool_input_preview(tool_input: Any) -> str:
    """One-line preview of tool args for progress logs.

    Trims long values so a grep pattern or a file path is readable but a
    full ``read_file`` result payload doesn't explode a log line.
    """
    if not isinstance(tool_input, dict):
        return str(tool_input)[:120]
    parts: list[str] = []
    for key, value in tool_input.items():
        text = str(value)
        if len(text) > 80:
            text = text[:80] + "…"
        parts.append(f"{key}={text!r}")
    joined = " ".join(parts)
    return joined[:240] + ("…" if len(joined) > 240 else "")


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
