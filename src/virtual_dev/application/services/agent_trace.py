"""AgentTrace — live event broadcaster for the test-analyst UI.

A pub-sub channel into which agents emit fine-grained activity:
``agent_started``, ``tool_use``, ``tool_result``, ``llm_text``,
``agent_finished``, ``chat_post``, ``orchestrator``. The browser
test-analyst page subscribes and renders everything in real time so
operators can see exactly what the Analyst is doing, what prompts it
sends to Claude, and how it decides to ask clarifying questions.

In production code-paths the trace is left at ``None`` — it's strictly
a debugging tool. Adapters check ``self._trace is not None`` before
emitting, so the cost is zero when unused.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger


@dataclass
class AgentTraceEvent:
    """One observable event in the agent pipeline."""

    type: str           # 'agent_started' | 'tool_use' | 'llm_text' | etc.
    agent_key: str      # e.g. 'analyst', 'answer-classifier'
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "agent_key": self.agent_key,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
        }


class AgentTrace:
    """Fan-out broadcaster + ring-buffer of recent events.

    A new subscriber receives the last ``history_size`` events first
    (so refreshing the test-analyst page doesn't lose the activity
    feed) and then keeps streaming live. ``clear()`` wipes the
    history — use it when "reset" is hit on the UI so the next page
    load starts fresh.
    """

    def __init__(
        self,
        *,
        queue_size: int = 1000,
        history_size: int = 500,
    ) -> None:
        self._subscribers: list[asyncio.Queue[AgentTraceEvent]] = []
        self._queue_size = queue_size
        self._history: deque[AgentTraceEvent] = deque(maxlen=history_size)

    async def emit(self, event: AgentTraceEvent) -> None:
        """Push to every subscriber (and append to history). Drop if
        any subscriber queue is full (slow subscriber's problem)."""
        self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug(
                    "AgentTrace: queue full for one subscriber, event dropped: {}",
                    event.type,
                )

    def clear(self) -> None:
        """Wipe history. Live subscribers keep their queue contents."""
        self._history.clear()

    def subscribe(self) -> AsyncIterator[AgentTraceEvent]:
        """Yield history first, then every future event until cancelled."""
        queue: asyncio.Queue[AgentTraceEvent] = asyncio.Queue(maxsize=self._queue_size)
        # Front-load history before any new emit can land. We don't
        # bother growing the queue — history_size <= queue_size is the
        # invariant we keep at construction time.
        for past in list(self._history):
            try:
                queue.put_nowait(past)
            except asyncio.QueueFull:
                break
        self._subscribers.append(queue)

        async def _iter() -> AsyncIterator[AgentTraceEvent]:
            try:
                while True:
                    event = await queue.get()
                    yield event
            finally:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

        return _iter()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def history_count(self) -> int:
        return len(self._history)


# Convenience helpers — agents/services use these and pass `None` to
# silently no-op when no trace is wired.


async def emit_if(trace: AgentTrace | None, event: AgentTraceEvent) -> None:
    if trace is not None:
        await trace.emit(event)


__all__ = ["AgentTrace", "AgentTraceEvent", "emit_if"]
