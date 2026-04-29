"""Web app shutdown drains background tasks in parallel, not serially.

The previous loop did ``for task in tasks: await asyncio.wait_for(task,
timeout=5)`` — 8 hung workers × 5s = 40s before uvicorn could release
the port. Cancellation also wasn't awaited, leaving "task exception
was never retrieved" warnings on Ctrl+C. The drain helper now cancels
everyone first, gathers them under a single timeout, and surfaces a
worst-case bounded shutdown.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from virtual_dev.presentation.web.app import _drain_background_tasks


@pytest.mark.asyncio
async def test_drain_runs_in_parallel_under_one_timeout() -> None:
    """Two slow tasks (each sleeping past the timeout) must finish
    draining within roughly one timeout window — not two."""

    async def slow() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Simulate a worker doing a tiny amount of cleanup before
            # honouring cancel — should not extend the shutdown.
            await asyncio.sleep(0.05)
            raise

    tasks = [asyncio.create_task(slow()) for _ in range(3)]
    start = time.monotonic()
    await _drain_background_tasks(tasks, timeout=2.0)
    elapsed = time.monotonic() - start

    # Sequential would have taken 3 * 2 = 6+ seconds. Parallel cancel
    # finishes in ~one timeout window plus the cleanup tail.
    assert elapsed < 3.0, f"shutdown took {elapsed:.2f}s, expected < 3s"
    assert all(t.done() for t in tasks)


@pytest.mark.asyncio
async def test_drain_does_not_swallow_quick_completion() -> None:
    """If tasks complete on their own quickly, drain returns fast and
    does NOT cancel them."""

    async def quick() -> None:
        await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(quick()) for _ in range(3)]
    start = time.monotonic()
    await _drain_background_tasks(tasks, timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    # Quick tasks completed normally, weren't cancelled.
    assert all(t.done() and not t.cancelled() for t in tasks)


@pytest.mark.asyncio
async def test_drain_tolerates_task_exceptions() -> None:
    """A worker that crashed during shutdown must not propagate up and
    block other workers from being drained."""

    async def crasher() -> None:
        raise RuntimeError("boom")

    async def slow() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    tasks = [
        asyncio.create_task(crasher()),
        asyncio.create_task(slow()),
    ]
    # Should not raise.
    await _drain_background_tasks(tasks, timeout=1.0)
    assert all(t.done() for t in tasks)
