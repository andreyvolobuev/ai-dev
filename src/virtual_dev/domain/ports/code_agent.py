"""Port that encapsulates a full Claude Agent SDK session.

This is the high-level way agents interact with the world: instead of calling
LLM + VCS + shell manually, they hand a task to the ``CodeAgentPort`` and get
back a structured result. Adapters may wrap the Claude Agent SDK, a local
alternative, or a deterministic mock for tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field


@dataclass
class CodeAgentTool:
    """Tool exposed to the agent (shell, file-edit, ...)."""

    name: str
    # Kept intentionally loose: concrete shape depends on the adapter.
    spec: dict[str, object] = field(default_factory=dict)


@dataclass
class CodeAgentRequest:
    """Inputs for a single agent run.

    No token / dollar budget fields by design: this project runs on the
    user's Claude Max subscription via `claude-agent-sdk`, not on a
    metered API, so output length and per-call spend are not things we
    enforce. ``max_turns`` is the only cycle guard.
    """

    agent_key: str                       # e.g. "dev-bellingshausen-backend"
    system_prompt: str
    user_prompt: str
    working_dir: str | None = None       # where the agent runs shell/files
    allowed_tools: Sequence[CodeAgentTool] = field(default_factory=list)
    max_turns: int = 30                  # runaway-loop guard, NOT a billing cap
    model: str | None = None             # override default model per run
    # Adapter-specific escape hatch. Keeps the abstract signature clean while
    # letting callers pass adapter knobs (e.g. MCP server handles for the
    # Claude Agent SDK). Adapter implementations document the keys they read.
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class CodeAgentResult:
    """Outputs of an agent run."""

    final_text: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stopped_reason: str                  # "end_turn" | "max_turns" | "error" | "killed"
    # True iff the underlying SDK reported is_error on its terminal
    # message, OR the CLI subprocess died mid-stream. Callers treat
    # this as an *infrastructure* failure (network / SDK / CLI), not
    # as the model giving up — re-raising is the contract so the
    # message bus's lease redelivers when infra is healthy again.
    is_error: bool = False


class CodeAgentPort(ABC):
    """Abstraction over a full agent loop (Claude Agent SDK or similar)."""

    @abstractmethod
    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        """Run the agent to completion and return its result."""

    @abstractmethod
    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:
        """Run the agent and stream raw text events (for dashboard live view)."""
