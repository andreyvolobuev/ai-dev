"""Port for low-level LLM access.

Most agents go through :class:`CodeAgentPort`, but a handful of lightweight
calls (thread summarisation, JSON-only structured extraction) use a simpler
completion interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class LlmMessage:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass
class LlmResponse:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    model: str


class LlmPort(ABC):
    """Abstraction over a single-shot LLM call (no tool use, no agent loop)."""

    @abstractmethod
    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float = 1.0,
    ) -> LlmResponse:
        """Return a single completion."""

    @abstractmethod
    def stream(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float = 1.0,
    ) -> AsyncIterator[str]:
        """Stream completion chunks as they arrive."""
