"""In-memory message bus for Phase 0.

A single ``asyncio.Queue`` per subscriber. Good enough while there's only one
agent and we want zero external dependencies. Will be replaced by a
SQLite-backed bus once multiple agents start exchanging messages.
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
        targets: list[str]
        if message.to_agent == "*":
            targets = list(self._queues.keys())
        else:
            targets = [message.to_agent]
        for target in targets:
            await self._queue_for(target).put(message)

    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        queue = self._queue_for(agent_key)

        async def _iter() -> AsyncIterator[AgentMessage]:
            while True:
                yield await queue.get()

        return _iter()
