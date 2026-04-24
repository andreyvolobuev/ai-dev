"""Periodic tick for agents that scan state rather than consume bus events.

ReviewerAgent and DevOpsAgent both loop over open MRs on a timer — the
message bus is not the natural driver (nobody publishes a "scan now"
event). PollerWorker wraps a list of ``tick()`` callables with a simple
sleep + cancellable stop semantics, mirroring the Orchestrator's loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

TickFn = Callable[[], Awaitable[object]]


@dataclass
class PollerStats:
    ticks: int = 0
    failures: int = 0
    last_errors: list[str] = field(default_factory=list)


class PollerWorker:
    """Runs a set of named tick callables at a fixed interval."""

    def __init__(
        self,
        *,
        name: str,
        interval_seconds: int,
        ticks: dict[str, TickFn],
    ) -> None:
        self._name = name
        self._interval = max(5, interval_seconds)
        self._ticks = ticks
        self._stop_event = asyncio.Event()
        self._running = False
        self.stats = PollerStats()

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if self._running:
            raise RuntimeError(f"PollerWorker[{self._name}] is already running")
        self._running = True
        self._stop_event.clear()
        logger.info(
            "PollerWorker[{}] started, interval={}s, ticks={}",
            self._name, self._interval, list(self._ticks),
        )
        try:
            while not self._stop_event.is_set():
                self.stats.ticks += 1
                for tick_name, fn in self._ticks.items():
                    try:
                        result = await fn()
                        logger.debug("PollerWorker[{}] {}: {}", self._name, tick_name, result)
                    except Exception as exc:
                        self.stats.failures += 1
                        self.stats.last_errors.append(f"{tick_name}: {exc}")
                        self.stats.last_errors = self.stats.last_errors[-10:]
                        logger.exception("PollerWorker[{}] tick {} raised", self._name, tick_name)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            logger.info("PollerWorker[{}] stopped", self._name)
