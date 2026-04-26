"""Built-in tools shipped with virtual-dev (task-driven model).

To add a new tool:

1. Drop a file here with a ``@tool_`` decorator on one async function.
2. Pick a ``ToolMode``:
   * ``SYNC`` — handler returns a ``ToolOutcome`` with ``result``;
     orchestrator runs the validator inline.
   * ``ASYNC`` — handler returns a ``ToolOutcome`` with ``pending``;
     orchestrator installs awaiting_* on the task and resumes when
     a reply coalesces.
   * ``META`` — handler returns a ``ToolOutcome`` with ``meta_action``
     in {"decompose", "escalate_to_lead", "abandon"} and a payload.
3. Restart — the discovery walks this dir at startup.
"""
