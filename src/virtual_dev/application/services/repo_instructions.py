"""Target-repo agent instructions (``AGENTS.md`` + ``.opencode/context/``).

The ``claude`` CLI auto-loads the target repo's ``CLAUDE.md``, but teams
also keep agent rules in the opencode layout: ``AGENTS.md`` in the root
and reference docs under ``.opencode/context/``. Claude Code reads
neither, so without this block the bot silently ignored mandatory repo
rules (e.g. bellingshausen's «Крис-ревью»).

Asking the model to "go read AGENTS.md" is not a guarantee, so the
rules are inlined into the system prompt: ``AGENTS.md`` plus every
context file whose opencode header says ``Priority: critical``. The
rest of ``.opencode/context/`` is listed by path for on-demand reads.
``.opencode/agents/`` holds opencode subagent definitions — tooling
config, not rules — and is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

_AGENTS_MD = "AGENTS.md"
_CONTEXT_DIR = Path(".opencode") / "context"
# opencode header: ``<!-- Context: x | Priority: critical | Version: ... -->``
_PRIORITY_RE = re.compile(r"Priority:\s*(\w+)", re.IGNORECASE)
_HEADER_SCAN_CHARS = 300

# Bellingshausen's AGENTS.md + critical context is ~28k chars today, so
# this leaves room to grow. Anything past the budget is still named as a
# mandatory read rather than dropped.
DEFAULT_MAX_INLINE_CHARS = 80_000

_PREAMBLE = (
    "## Target repository instructions (AGENTS.md / .opencode)\n"
    "The repository you work in ships its own instructions for AI agents. "
    "They are binding on HOW you write code here: style, structure, tests, "
    "linting, comments, language of docs and commit messages. Apply them "
    "while implementing and check your final diff against them.\n"
    "Precedence: on workflow mechanics this runtime's rules win — the "
    "runtime commits, pushes and opens the MR for you, and you report via "
    "your terminal tool instead of waiting for a user's approval in chat. "
    "Repo rules like \"commit only when the user asks\" are about "
    "interactive sessions; do not stall on them. A rule to push a "
    "rewritten history only after a human's ok is honoured with "
    "`hold_push` (see «Rewriting history»)."
)

# Repo docs that AGENTS.md points to by a relative path («пересобирают по
# `docs/branch-rebuild.md`»). A pointer alone was not enough: the model
# read the rule but not the procedure behind it.
_DOC_REF_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)*[\w.-]+\.md)\b")


def render_repo_instructions(
    workspace: str | Path | None,
    *,
    max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS,
) -> str:
    """Return the system-prompt block for ``workspace`` or ``""``."""
    if not workspace:
        return ""
    root = Path(workspace)
    if not root.is_dir():
        return ""

    inline: list[tuple[str, str]] = []
    must_read: list[str] = []
    optional: list[str] = []
    budget = max_inline_chars

    def _take(rel: str, text: str) -> None:
        nonlocal budget
        if len(text) <= budget:
            inline.append((rel, text))
            budget -= len(text)
        else:
            must_read.append(rel)

    agents_md = root / _AGENTS_MD
    if agents_md.is_file():
        agents_text = _read(agents_md)
        _take(_AGENTS_MD, agents_text)
        for rel in _referenced_docs(root, agents_text):
            _take(rel, _read(root / rel))

    context_dir = root / _CONTEXT_DIR
    if context_dir.is_dir():
        for path in sorted(context_dir.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            text = _read(path)
            priority = _priority(text)
            if priority == "critical":
                _take(rel, text)
            elif priority:
                optional.append(f"`{rel}` (priority: {priority})")
            else:
                optional.append(f"`{rel}`")

    if not (inline or must_read or optional):
        return ""

    parts: list[str] = [_PREAMBLE]
    for rel, text in inline:
        parts.append(f"\n### `{rel}`\n{text}")
    if must_read:
        parts.append(
            "\n### Mandatory reads (too long to inline)\n"
            "Before changing any code you MUST `Read` each of these in full "
            "and follow them:\n"
            + "\n".join(f"- `{rel}`" for rel in must_read)
        )
    if optional:
        parts.append(
            "\n### More project context\n"
            "Reference docs under `.opencode/context/`. `Read` the ones "
            "relevant to your task (the inlined navigation files above say "
            "which is which):\n"
            + "\n".join(f"- {item}" for item in optional)
        )
    return "\n".join(parts)


def _referenced_docs(root: Path, text: str) -> list[str]:
    """Markdown files inside the repo that ``text`` mentions, in order.
    ``.opencode/context`` is left out — it has its own priority rules."""
    resolved_root = root.resolve()
    seen: list[str] = []
    for match in _DOC_REF_RE.finditer(text):
        rel = match.group(1)
        if rel in seen or rel in (_AGENTS_MD, "CLAUDE.md"):
            continue
        path = (root / rel).resolve()
        if not path.is_file() or not path.is_relative_to(resolved_root):
            continue
        if path.is_relative_to((root / _CONTEXT_DIR).resolve()):
            continue
        seen.append(rel)
    return seen


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _priority(text: str) -> str:
    match = _PRIORITY_RE.search(text[:_HEADER_SCAN_CHARS])
    return match.group(1).lower() if match else ""
