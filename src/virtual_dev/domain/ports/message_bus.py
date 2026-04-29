"""Port for inter-agent messaging.

Delivery semantics: **at-least-once** with explicit ack.

* ``subscribe()`` yields a message and reserves it under a lease.
* The consumer is expected to call ``ack(message)`` after the handler
  succeeds; on crash / timeout the lease expires and the bus
  redelivers — handlers must therefore be idempotent. (Application
  models already enforce this via UNIQUE constraints on ``TaskRow``,
  ``MergeRequestRow``, ``AnalystConversationFragmentRow``.)
* In-memory adapters MAY treat ``ack`` as a no-op; the port still
  requires the call so the contract is consistent across backends.
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
    # Backend-internal handle (e.g. SQLite row id) used by ``ack`` to
    # finalise the right row. Adapters set this when yielding; consumers
    # treat it as opaque.
    _row_id: int | None = None


class MessageBusPort(ABC):
    """Abstraction over an inter-agent message bus."""

    @abstractmethod
    async def publish(self, message: AgentMessage) -> None:
        """Publish a message."""

    @abstractmethod
    async def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        """Register a durable subscription for ``agent_key`` and return
        an iterator over its messages.

        Awaiting completes registration (durable backends commit a row)
        — broadcasts published immediately after the await must reach
        this subscriber. Each yielded message holds a lease until
        ``ack`` (or expiry)."""

    @abstractmethod
    async def ack(self, message: AgentMessage) -> None:
        """Mark ``message`` as fully handled. After ack the bus must
        not redeliver it. No-op in non-durable backends."""
