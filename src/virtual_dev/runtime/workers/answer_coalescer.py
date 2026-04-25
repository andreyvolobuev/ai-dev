"""Background worker: idle-flush + deadline-sweep for clarification questions.

Both jobs are owned by :class:`ClarificationOrchestrator`. This module
is just the wiring that turns them into a periodic background task in
the FastAPI lifespan.
"""

from __future__ import annotations

from virtual_dev.application.services.clarification import ClarificationOrchestrator
from virtual_dev.runtime.workers.poller import PollerWorker


def make_answer_coalescer_worker(
    *,
    orchestrator: ClarificationOrchestrator,
    interval_seconds: int,
) -> PollerWorker:
    """Build a :class:`PollerWorker` that ticks the orchestrator's
    ``flush_idle`` and ``sweep_deadlines`` every ``interval_seconds``.

    The two ticks share one worker because they're both cheap, both
    operate on the same ``questions`` table, and we want a single
    cadence-knob in settings (``answer_coalesce_poll_interval_seconds``).
    """
    return PollerWorker(
        name="answer-coalescer",
        interval_seconds=interval_seconds,
        ticks={
            "flush_idle": orchestrator.flush_idle,
            "deadline_sweep": orchestrator.sweep_deadlines,
        },
    )


__all__ = ["make_answer_coalescer_worker"]
