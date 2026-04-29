"""Loader for agent system prompts (``config/prompts/<name>.md``).

System prompts are the LLM's "constitution" — long, multi-paragraph,
prone to wording iterations. Keeping them as Python string literals
makes editing painful (multi-line escaping, recompile to test). This
loader reads each prompt from a markdown file at startup-time so
operators can tune the wording in YAML/MD without touching code.

Files may use ``{untrusted_warning}`` as a placeholder; the caller
substitutes the canonical injection-filter notice. Other placeholders
the caller doesn't know about are left literal so a missing key
shouldn't crash the run.

Cache strategy: keyed by ``(name, mtime_ns)``. When the file on disk
changes, ``stat()`` returns a new mtime and we re-read — operators can
edit prompts and pick up changes within the next agent run, no restart.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


class PromptsLoader:
    def __init__(self, prompts_dir: str | Path) -> None:
        self._prompts_dir = Path(prompts_dir)
        # name → (mtime_ns, text). mtime_ns == -1 means "file missing,
        # fallback cached".
        self._cache: dict[str, tuple[int, str]] = {}

    def load(self, name: str, fallback: str = "") -> str:
        """Return the contents of ``<prompts_dir>/<name>.md`` or ``fallback``.

        Re-reads if the on-disk mtime has changed since the last cache
        entry (allows operators to tune prompts without restarting).
        """
        path = self._prompts_dir / f"{name}.md"
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = -1

        cached = self._cache.get(name)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        if mtime_ns == -1:
            logger.warning(
                "PromptsLoader: prompt file missing — {} (using fallback)", path,
            )
            self._cache[name] = (-1, fallback)
            return fallback

        text = path.read_text(encoding="utf-8").strip()
        if cached is not None:
            logger.info("PromptsLoader: reloaded prompt {!r} (file changed)", name)
        self._cache[name] = (mtime_ns, text)
        return text

    def render(self, name: str, fallback: str = "", **kwargs: str) -> str:
        """Like :meth:`load` but applies ``str.format`` with the given vars.

        Uses a tolerant formatter so unknown placeholders ({foo}) survive
        as literal text instead of crashing — and so a stray ``{`` in
        the prompt body (e.g. a JSON example) doesn't silently fall back
        to RAW text with unsubstituted ``{untrusted_warning}``, which
        would disable injection-warning text in the system prompt.
        """
        template = self.load(name, fallback=fallback)
        try:
            return template.format_map(_TolerantMap(kwargs))
        except (ValueError, IndexError) as exc:
            # ValueError covers malformed format-spec ({:!}); IndexError
            # covers positional refs we can't resolve. Both are operator
            # typos, not missing kwargs (which _TolerantMap absorbs).
            logger.warning(
                "PromptsLoader: format failed for {!r}: {} — returning raw text",
                name, exc,
            )
            return template


class _TolerantMap(dict[str, str]):
    """``format_map`` lookup that returns ``{key}`` unchanged for unknown
    keys instead of raising KeyError. Lets a prompt file contain literal
    ``{anything}`` (JSON examples, inline curly placeholders) without
    crashing the format step or losing the substitutions we DO have."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
