"""Tests for AgentTrace correlation-id + log-sink behaviour.

The motivating scenario: when something goes wrong in prod, the
operator wants to read a single log file and reconstruct the activity
feed (LLM prompts, tool_use, tool_result, agent_started, …) without
attaching to the UI. This requires:

* Each agent run gets a stable ``run_id`` that propagates to every
  ``AgentTraceEvent`` emitted within its async context.
* A log sink subscribes to the trace and emits one DEBUG line per
  event, with the run_id bound so loguru's format can include it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from loguru import logger

from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    bind_run_id,
    consume_trace_to_logs,
)


@pytest.mark.asyncio
async def test_event_picks_up_run_id_from_contextvar() -> None:
    with bind_run_id("run-42"):
        event = AgentTraceEvent(type="tool_use", agent_key="analyst")
    assert event.run_id == "run-42"


@pytest.mark.asyncio
async def test_event_outside_bind_has_no_run_id() -> None:
    event = AgentTraceEvent(type="tool_use", agent_key="analyst")
    assert event.run_id is None


@pytest.mark.asyncio
async def test_explicit_run_id_overrides_contextvar() -> None:
    with bind_run_id("run-ctx"):
        event = AgentTraceEvent(
            type="tool_use", agent_key="analyst", run_id="run-explicit",
        )
    assert event.run_id == "run-explicit"


@pytest.mark.asyncio
async def test_bind_run_id_is_reset_after_context_exits() -> None:
    with bind_run_id("inner"):
        e1 = AgentTraceEvent(type="t", agent_key="a")
        assert e1.run_id == "inner"
    e2 = AgentTraceEvent(type="t", agent_key="a")
    assert e2.run_id is None


@pytest.mark.asyncio
async def test_to_json_includes_run_id() -> None:
    with bind_run_id("run-X"):
        event = AgentTraceEvent(type="tool_use", agent_key="analyst")
    payload = event.to_json()
    assert payload["run_id"] == "run-X"


@pytest.mark.asyncio
async def test_consume_trace_to_logs_emits_debug_line_per_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log sink should emit one DEBUG record per event with the
    run_id bound (loguru's contextualize) and a compact summary."""
    trace = AgentTrace()
    handler_id = logger.add(
        _LoguruToCaplog(caplog),
        level="DEBUG",
        format="{message}",
    )
    try:
        sink_task = asyncio.create_task(consume_trace_to_logs(trace))
        with caplog.at_level(logging.DEBUG):
            with bind_run_id("run-77"):
                await trace.emit(AgentTraceEvent(
                    type="tool_use",
                    agent_key="analyst",
                    payload={"tool": "Read", "input": {"file_path": "/a"}},
                ))
            # Give the consumer a tick to drain.
            await asyncio.sleep(0.05)
        sink_task.cancel()
        try:
            await sink_task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        logger.remove(handler_id)

    matches = [r for r in caplog.records if "tool_use" in r.getMessage()]
    assert matches, f"no tool_use line found in log records: {caplog.records}"
    # The run_id must be present somewhere on the line so an operator
    # can grep one run end-to-end.
    assert any("run-77" in r.getMessage() for r in matches), matches


class _LoguruToCaplog:
    """Forward loguru records to the standard logging caplog so pytest
    can assert on them. Uses a normal logging.Logger underneath."""

    def __init__(self, caplog: pytest.LogCaptureFixture) -> None:
        self._caplog = caplog
        self._stdlogger = logging.getLogger("test.loguru-bridge")

    def write(self, message: str) -> None:  # loguru sink protocol
        record = self._stdlogger.makeRecord(
            "test.loguru-bridge", logging.DEBUG, "loguru", 0,
            message.rstrip("\n"), None, None,
        )
        self._caplog.records.append(record)
