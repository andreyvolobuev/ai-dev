"""Port for inter-agent messaging.

Phase 0 has only the Orchestrator, so this is mostly a placeholder — but the
port already has to exist so later phases can plug in a real bus (SQLite →
Redis → RabbitMQ) without touching application code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AgentMessage:
    """Message exchanged between agents."""

    id: str
    from_agent: str
    to_agent: str                  # "*" for broadcast
    topic: str                     # e.g. "task.ready_for_coding"
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None   # ties a request/response pair
    created_at: datetime | None = None


class MessageBusPort(ABC):
    """Abstraction over an inter-agent message bus."""

    @abstractmethod
    async def publish(self, message: AgentMessage) -> None:
        """Publish a message."""

    @abstractmethod
    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        """Stream messages addressed to ``agent_key`` (or broadcast)."""
