"""Background worker: idle-flush + deadline-sweep for analyst sessions.

Both jobs are owned by :class:`AnalystInbox`. This module wires them
into a periodic poller.
"""

from __future__ import annotations

from virtual_dev.runtime.workers.analyst_inbox import AnalystInbox
from virtual_dev.runtime.workers.poller import PollerWorker


def make_answer_coalescer_worker(
    *,
    orchestrator: AnalystInbox,
    interval_seconds: int,
) -> PollerWorker:
    """Build a :class:`PollerWorker` that ticks the analyst inbox's
    ``flush_idle`` and ``sweep_deadlines`` every ``interval_seconds``.
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
