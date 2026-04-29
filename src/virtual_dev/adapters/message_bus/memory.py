"""In-memory message bus — used by tests and small CLIs.

One ``asyncio.Queue`` per subscriber. ``ack`` is a no-op (the queue
already implements at-most-once via ``get``); the method exists to
satisfy the port. Targeted messages to an unknown agent_key are queued
under that key so a later ``subscribe`` still receives them, matching
the durable behaviour of the SQLite adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort


class InMemoryMessageBus(MessageBusPort):
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}

    def _queue_for(self, agent_key: str) -> asyncio.Queue[AgentMessage]:
        if agent_key not in self._queues:
            self._queues[agent_key] = asyncio.Queue()
        return self._queues[agent_key]

    async def publish(self, message: AgentMessage) -> None:
        if message.to_agent == "*":
            # Mirror the SQLite adapter: broadcast lands in every known
            # inbox, where "known" means anyone who's ever subscribed.
            # Tests may depend on the order being deterministic.
            targets = list(self._queues.keys())
        else:
            targets = [message.to_agent]
        for target in targets:
            await self._queue_for(target).put(message)

    async def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        queue = self._queue_for(agent_key)

        async def _iter() -> AsyncIterator[AgentMessage]:
            while True:
                yield await queue.get()

        return _iter()

    async def ack(self, message: AgentMessage) -> None:
        # No-op: the queue's get() already removes the item.
        return None
