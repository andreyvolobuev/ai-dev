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

import asyncio
import re
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

from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
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
        env: dict[str, str] | None = None,
        rate_limit_max_retries: int = 2,
        rate_limit_initial_backoff_seconds: int = 60,
        trace: AgentTrace | None = None,
    ) -> None:
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._cli_path = cli_path
        # Extra env for the spawned `claude` CLI (e.g. corporate gateway:
        # ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS).
        # Empty → CLI inherits the parent env and uses the local Claude Max login.
        self._env = env or {}
        self._rate_limit_max_retries = max(0, rate_limit_max_retries)
        self._rate_limit_initial_backoff = max(1, rate_limit_initial_backoff_seconds)
        # Optional debug-trace channel — UI subscribers see every
        # tool_use, every TextBlock, and the system+user prompts.
        # In production code-paths trace=None and the cost is zero.
        self._trace = trace

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        # Wrap the actual run in a retry loop that catches Claude Max
        # rate-limit signals (#4 in techdebt). The Claude Max session
        # blocks on rate limits in-CLI and surfaces "rate" / "429" /
        # "rate_limit" in stderr or in the exception message.
        attempt = 0
        backoff = self._rate_limit_initial_backoff
        while True:
            try:
                return await self._run_task_once(request)
            except Exception as exc:
                if attempt >= self._rate_limit_max_retries:
                    raise
                if not _looks_like_rate_limit(str(exc)):
                    raise
                attempt += 1
                logger.warning(
                    "[{}] Claude rate-limited (attempt {}/{}); sleeping {}s before retry: {}",
                    request.agent_key, attempt, self._rate_limit_max_retries,
                    backoff, str(exc)[:200],
                )
                await asyncio.sleep(backoff)
                backoff *= 3

    async def _run_task_once(self, request: CodeAgentRequest) -> CodeAgentResult:
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
        is_error = False

        # Trace: announce start with both prompts so the UI can show
        # exactly what we sent to the model. We don't truncate here —
        # the browser side decides how to render long blocks.
        await emit_if(self._trace, AgentTraceEvent(
            type="agent_started",
            agent_key=request.agent_key,
            payload={
                "model": request.model or self._default_model or "(default)",
                "max_turns": request.max_turns,
                "system_prompt": request.system_prompt or "",
                "user_prompt": request.user_prompt,
                "working_dir": request.working_dir,
            },
        ))

        tool_use_count = 0
        try:
            async for event in query(prompt=request.user_prompt, options=options):
                if isinstance(event, AssistantMessage):
                    for block in event.content:
                        if isinstance(block, TextBlock):
                            final_text_parts.append(block.text)
                            await emit_if(self._trace, AgentTraceEvent(
                                type="llm_text",
                                agent_key=request.agent_key,
                                payload={"text": block.text},
                            ))
                        elif isinstance(block, ToolUseBlock):
                            tool_use_count += 1
                            logger.info(
                                "[{}] tool_use #{}: {} {}",
                                request.agent_key,
                                tool_use_count,
                                block.name,
                                _tool_input_preview(block.input),
                            )
                            await emit_if(self._trace, AgentTraceEvent(
                                type="tool_use",
                                agent_key=request.agent_key,
                                payload={
                                    "n": tool_use_count,
                                    "name": block.name,
                                    "input": _tool_input_for_trace(block.input),
                                },
                            ))
                elif isinstance(event, ResultMessage):
                    got_result = True
                    cost_usd = float(event.total_cost_usd or 0.0)
                    turns = int(event.num_turns or 0)
                    stop_reason = event.stop_reason or ("error" if event.is_error else "end_turn")
                    usage = cast(dict[str, Any], event.usage or {})
                    input_tokens = int(usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output_tokens") or 0)
                    if event.is_error:
                        is_error = True
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
                # Surface a rate-limit signal to the run_task wrapper so
                # it can back off and retry. Detected from stderr because
                # the CLI exits with a generic "Command failed" otherwise.
                stderr_blob = "\n".join(stderr_lines[-50:])
                if _looks_like_rate_limit(stderr_blob):
                    raise RuntimeError(
                        f"claude rate limit hit: {stderr_blob[-400:]}"
                    ) from exc
                raise
        finally:
            if stderr_lines:
                logger.debug(
                    "claude CLI stderr for agent={} ({} lines captured)",
                    request.agent_key,
                    len(stderr_lines),
                )

        await emit_if(self._trace, AgentTraceEvent(
            type="agent_finished",
            agent_key=request.agent_key,
            payload={
                "turns": turns,
                "stop_reason": stop_reason,
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_use_count": tool_use_count,
            },
        ))

        return CodeAgentResult(
            final_text="\n".join(final_text_parts),
            turns=turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            stopped_reason=stop_reason,
            is_error=is_error,
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
        if self._env:
            kwargs["env"] = self._env
        if stderr_sink is not None:
            # Capture CLI subprocess stderr line-by-line. Surfaced in logs on
            # failure so "Command failed with exit code 1 — check stderr" is
            # actually actionable.
            def _on_stderr(line: str) -> None:
                stderr_sink.append(line)
            kwargs["stderr"] = _on_stderr
        return ClaudeAgentOptions(**kwargs)


_RATE_LIMIT_RE = re.compile(
    r"\b(rate[ _-]?limit|429\b|too many requests|rate limited|"
    r"5h limit|usage limit reached|limit exceeded)\b",
    re.IGNORECASE,
)


def _looks_like_rate_limit(text: str) -> bool:
    """Heuristic: does this exception / stderr look like a rate-limit?"""
    if not text:
        return False
    return bool(_RATE_LIMIT_RE.search(text))


def _tool_input_for_trace(tool_input: Any) -> Any:
    """Sanitise tool_input for the AgentTrace JSON payload.

    Claude Agent SDK passes plain dicts for most tools; we pass them
    through untouched so the UI can render full text. Non-dict values
    are stringified to keep ``json.dumps`` happy.
    """
    if isinstance(tool_input, dict):
        return tool_input
    return str(tool_input)


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
