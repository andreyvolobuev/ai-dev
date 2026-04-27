"""Shared helpers for tool implementations.

Tool files import from here when they need:

* the ``content``-block wrapping shape Claude expects (``text_result``,
  ``error_text``);
* the ``git grep`` shell-out used by code-search tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from loguru import logger


def text_result(text: str) -> dict[str, Any]:
    """Wrap a plain string into an MCP tool result block."""
    return {"content": [{"type": "text", "text": text}]}


def error_text(msg: str) -> dict[str, Any]:
    """Return an MCP error block. ``is_error`` lights up the failure
    indicator on the LLM side."""
    return {
        "content": [{"type": "text", "text": f"ERROR: {msg}"}],
        "is_error": True,
    }


def git_grep(repo_path: Path, pattern: str, max_results: int) -> str:
    """Run ``git grep -nI`` in ``repo_path``.

    Falls back to a plain message if the path is not a git repo. Output
    is capped at ``max_results`` lines; stderr is suppressed.
    """
    try:
        proc = subprocess.run(
            [
                "git", "grep", "-nI",
                "--max-depth", "20",
                "-e", pattern,
            ],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return "git is not installed"
    except subprocess.TimeoutExpired:
        return f"git grep timed out for pattern {pattern!r}"
    except Exception as exc:  # fail loud, but let the Analyst continue
        logger.exception("git grep failed")
        return f"git grep error: {exc}"

    lines = (proc.stdout or "").splitlines()
    if not lines:
        return f"no matches for pattern {pattern!r}"
    if len(lines) > max_results:
        truncated = len(lines) - max_results
        lines = lines[:max_results]
        lines.append(f"... ({truncated} more matches truncated)")
    return "\n".join(lines)


__all__ = ["error_text", "git_grep", "text_result"]
