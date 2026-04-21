"""Glue loop that consumes the message bus and feeds an agent.

Phase 1 uses one runner per agent (currently only the Analyst). Each runner
subscribes to its inbox on the bus, dispatches messages to the agent
callable, and logs failures without crashing the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort

Handler = Callable[[AgentMessage], Awaitable[None]]


@dataclass
class AgentRunnerStats:
    processed: int = 0
    failed: int = 0


class AgentRunner:
    """Owns the subscribe-and-dispatch loop for a single agent key."""

    def __init__(
        self,
        *,
        agent_key: str,
        message_bus: MessageBusPort,
        handlers: dict[str, Handler],
    ) -> None:
        self._agent_key = agent_key
        self._bus = message_bus
        self._handlers = handlers
        self._stop_event = asyncio.Event()
        self._running = False
        self.stats = AgentRunnerStats()

    @property
    def agent_key(self) -> str:
        return self._agent_key

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if self._running:
            raise RuntimeError(f"AgentRunner[{self._agent_key}] is already running")
        self._running = True
        logger.info("AgentRunner[{}] subscribing to bus", self._agent_key)
        try:
            subscription = self._bus.subscribe(self._agent_key)
            pending = asyncio.create_task(_anext(subscription))
            stopper = asyncio.create_task(self._stop_event.wait())
            try:
                while not self._stop_event.is_set():
                    done, _ = await asyncio.wait(
                        {pending, stopper},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopper in done:
                        break
                    if pending in done:
                        try:
                            message = pending.result()
                        except StopAsyncIteration:
                            break
                        await self._dispatch(message)
                        pending = asyncio.create_task(_anext(subscription))
            finally:
                for task in (pending, stopper):
                    if not task.done():
                        task.cancel()
                        with _suppress_cancel():
                            await task
        finally:
            self._running = False
            logger.info("AgentRunner[{}] stopped", self._agent_key)

    async def _dispatch(self, message: AgentMessage) -> None:
        handler = self._handlers.get(message.topic)
        if handler is None:
            logger.debug(
                "AgentRunner[{}] ignoring topic {!r} (no handler)",
                self._agent_key, message.topic,
            )
            return
        try:
            await handler(message)
            self.stats.processed += 1
        except Exception:
            self.stats.failed += 1
            logger.exception(
                "AgentRunner[{}] handler for {!r} raised",
                self._agent_key, message.topic,
            )


async def _anext(iterator: Any) -> AgentMessage:
    return await iterator.__anext__()


class _suppress_cancel:
    def __enter__(self) -> "_suppress_cancel":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return isinstance(exc, asyncio.CancelledError)
