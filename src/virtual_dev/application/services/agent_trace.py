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
import json
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

# Holds the current agent-run correlation id. Set via ``bind_run_id``
# at the start of an agent invocation (e.g. ``AnalystAgent.run``); all
# AgentTraceEvents emitted within that async context auto-stamp it.
# Lets the operator grep a single run end-to-end across log lines.
_RUN_ID_CTX: ContextVar[str | None] = ContextVar("agent_run_id", default=None)


@contextmanager
def bind_run_id(run_id: str) -> Iterator[None]:
    """Bind a correlation id to the current async context. Exiting the
    block resets back to the prior value so nested binds work."""
    token = _RUN_ID_CTX.set(run_id)
    # Loguru's contextualize threads run_id into every logger.* call
    # made inside the block, so format strings can include it.
    with logger.contextualize(run_id=run_id):
        try:
            yield
        finally:
            _RUN_ID_CTX.reset(token)


@dataclass
class AgentTraceEvent:
    """One observable event in the agent pipeline."""

    type: str           # 'agent_started' | 'tool_use' | 'llm_text' | etc.
    agent_key: str      # e.g. 'analyst', 'answer-classifier'
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.run_id is None:
            self.run_id = _RUN_ID_CTX.get()

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "agent_key": self.agent_key,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "run_id": self.run_id,
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


# How much of any one text payload to dump into logs. The UI already
# has the full text via the websocket; logs are for grep-friendly
# post-mortem, not full prompt archives.
_TRACE_LOG_TEXT_LIMIT = 500


def _format_event_for_log(event: AgentTraceEvent) -> str:
    """One-line, grep-friendly digest of an event. Long fields are
    truncated; the UI keeps the full original."""
    parts: list[str] = [
        f"trace[{event.run_id or '-'}] {event.type} agent={event.agent_key}",
    ]
    payload = event.payload or {}
    for key, value in payload.items():
        if isinstance(value, str):
            short = value if len(value) <= _TRACE_LOG_TEXT_LIMIT else (
                value[:_TRACE_LOG_TEXT_LIMIT] + f"…[+{len(value) - _TRACE_LOG_TEXT_LIMIT}]"
            )
            parts.append(f"{key}={short!r}")
        elif isinstance(value, (int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        else:
            try:
                serialised = json.dumps(value, ensure_ascii=False)
            except TypeError:
                serialised = repr(value)
            short = serialised if len(serialised) <= _TRACE_LOG_TEXT_LIMIT else (
                serialised[:_TRACE_LOG_TEXT_LIMIT]
                + f"…[+{len(serialised) - _TRACE_LOG_TEXT_LIMIT}]"
            )
            parts.append(f"{key}={short}")
    return " ".join(parts)


async def consume_trace_to_logs(trace: AgentTrace) -> None:
    """Drain ``trace`` into loguru DEBUG, one record per event.

    Long-running coroutine — start it as a background task in the app
    lifespan. Cancelling the task stops the drain. Each emit binds the
    event's ``run_id`` into loguru context so format strings including
    ``{extra[run_id]}`` light up.
    """
    sub = trace.subscribe()
    try:
        async for event in sub:
            line = _format_event_for_log(event)
            with logger.contextualize(run_id=event.run_id or "-"):
                logger.debug(line)
    except asyncio.CancelledError:
        pass


__all__ = [
    "AgentTrace",
    "AgentTraceEvent",
    "bind_run_id",
    "consume_trace_to_logs",
    "emit_if",
]
