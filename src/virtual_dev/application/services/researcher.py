"""Researcher service — code grep + KB search, exposed as in-process MCP tools.

The Analyst agent cannot safely call adapters directly: it runs inside a
``claude-agent-sdk`` subprocess and only reaches Python code through MCP
tools. :class:`ResearcherToolkit` converts our ports into a small MCP server
that the Analyst can use during its reasoning loop.

Tools exposed:
    * ``search_code(pattern, repo_key, max_results)`` — ripgrep-style search
      inside one of the configured repositories. Reads are local and capped
      to avoid blowing up the prompt.
    * ``read_file(path, repo_key, max_bytes)`` — display a small window of
      a file from the same repository.
    * ``kb_search(query, limit)`` — Confluence full-text search.
    * ``kb_fetch_page_by_url(url)`` — Confluence page fetch by URL.

All tool outputs are wrapped in ``<untrusted_content>`` via :class:`InjectionFilter`
before being returned, so the Analyst treats them as data.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.domain.ports.mr_history import MrHistoryPort
from virtual_dev.infrastructure.config import AppConfig


@dataclass
class RepoHandle:
    """Resolved pointer to a local checkout of a repository."""

    key: str
    local_path: Path


def _build_repo_handles(config: AppConfig, workspaces_dir: str | Path) -> dict[str, RepoHandle]:
    handles: dict[str, RepoHandle] = {}
    ws_root = Path(workspaces_dir)
    for repo in config.repositories:
        if repo.local_path:
            path = Path(repo.local_path)
        else:
            path = ws_root / repo.key
        handles[repo.key] = RepoHandle(key=repo.key, local_path=path)
    return handles


class ResearcherToolkit:
    """Factory for the MCP server that exposes research tools to the Analyst."""

    _DEFAULT_MAX_GREP_RESULTS = 30
    _DEFAULT_MAX_FILE_BYTES = 12_000

    def __init__(
        self,
        *,
        config: AppConfig,
        workspaces_dir: str | Path,
        knowledge_base: KnowledgeBasePort | None,
        injection_filter: InjectionFilter,
        mr_history: MrHistoryPort | None = None,
    ) -> None:
        self._repos = _build_repo_handles(config, workspaces_dir)
        self._kb = knowledge_base
        self._filter = injection_filter
        self._mr_history = mr_history

    def build_mcp_server(self) -> McpSdkServerConfig:
        """Create an in-process MCP server with the research tools bound.

        Tools are defined inside this method so they close over ``self`` and
        remain wired to the toolkit's ports / config / filter.
        """

        @tool(
            "search_code",
            "Search the codebase for a regex pattern. Returns matching lines "
            "grouped by file. Large results are truncated.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "repo_key": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["pattern", "repo_key"],
            },
        )
        async def _search_code(args: dict[str, Any]) -> dict[str, Any]:
            return await self._run_search_code(args)

        @tool(
            "read_file",
            "Read a small window of a file in a repository. Returns up to "
            "max_bytes characters, defaults to 12000.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "repo_key": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["path", "repo_key"],
            },
        )
        async def _read_file(args: dict[str, Any]) -> dict[str, Any]:
            return await self._run_read_file(args)

        @tool(
            "kb_search",
            "Full-text search in the knowledge base (Confluence). Returns up to `limit` pages.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        )
        async def _kb_search(args: dict[str, Any]) -> dict[str, Any]:
            return await self._run_kb_search(args)

        @tool(
            "kb_fetch_page_by_url",
            "Fetch a specific knowledge-base page by its URL.",
            {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )
        async def _kb_fetch(args: dict[str, Any]) -> dict[str, Any]:
            return await self._run_kb_fetch(args)

        @tool(
            "search_mr_history",
            "Search past merged MRs of this repository for ones similar to "
            "`query`. Returns up to `k` hits (default 5) with title, "
            "description, URL, author and a similarity score. Useful to see "
            "how comparable changes were done before.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "repo_key": {"type": "string"},
                    "k": {"type": "integer"},
                },
                "required": ["query", "repo_key"],
            },
        )
        async def _search_mr_history(args: dict[str, Any]) -> dict[str, Any]:
            return await self._run_search_mr_history(args)

        return create_sdk_mcp_server(
            name="virtual_dev_researcher",
            version="0.1.0",
            tools=[_search_code, _read_file, _kb_search, _kb_fetch, _search_mr_history],
        )

    # --- tool implementations (public for testing) ---

    async def _run_search_code(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        repo_key = str(args.get("repo_key") or "")
        max_results = int(args.get("max_results") or self._DEFAULT_MAX_GREP_RESULTS)

        handle = self._repos.get(repo_key)
        if handle is None or not handle.local_path.exists():
            return _error_text(f"Unknown or missing repo: {repo_key!r}")
        if not pattern:
            return _error_text("Empty search pattern")

        text = await asyncio.to_thread(
            _git_grep, handle.local_path, pattern, max_results
        )
        wrapped = self._filter.wrap(text, source=f"code:{repo_key}:grep")
        return _text_result(wrapped.wrapped_text)

    async def _run_read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or "")
        repo_key = str(args.get("repo_key") or "")
        max_bytes = int(args.get("max_bytes") or self._DEFAULT_MAX_FILE_BYTES)

        handle = self._repos.get(repo_key)
        if handle is None:
            return _error_text(f"Unknown repo: {repo_key!r}")

        full = (handle.local_path / path).resolve()
        try:
            full.relative_to(handle.local_path.resolve())
        except ValueError:
            return _error_text(f"Path escape blocked: {path!r}")
        if not full.is_file():
            return _error_text(f"File not found: {path!r}")

        raw = await asyncio.to_thread(full.read_text, "utf-8", "replace")
        if len(raw) > max_bytes:
            raw = raw[:max_bytes] + f"\n... (truncated, {len(raw)} bytes total)"
        wrapped = self._filter.wrap(raw, source=f"code:{repo_key}:{path}")
        return _text_result(wrapped.wrapped_text)

    async def _run_kb_search(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._kb is None:
            return _error_text("Knowledge base is not configured")
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 5)
        if not query:
            return _error_text("Empty search query")
        pages = await self._kb.search(query, limit=limit)
        rendered = "\n\n".join(
            f"# {p.title}\n{p.url}\n\n{p.content_text[:2000]}" for p in pages
        )
        wrapped = self._filter.wrap(rendered or "(no results)", source=f"kb:search:{query[:40]}")
        return _text_result(wrapped.wrapped_text)

    async def _run_kb_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._kb is None:
            return _error_text("Knowledge base is not configured")
        url = str(args.get("url") or "")
        if not url:
            return _error_text("Empty URL")
        page = await self._kb.fetch_page_by_url(url)
        rendered = f"# {page.title}\n{page.url}\n\n{page.content_text}"
        wrapped = self._filter.wrap(rendered, source=f"kb:page:{page.id}")
        return _text_result(wrapped.wrapped_text)

    async def _run_search_mr_history(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._mr_history is None:
            return _error_text(
                "MR history index is not configured. Run `virtual-dev index-mrs --repo <key>` first."
            )
        repo_key = str(args.get("repo_key") or "")
        query = str(args.get("query") or "")
        k = int(args.get("k") or 5)
        if not repo_key:
            return _error_text("repo_key is required")
        if not query:
            return _error_text("query is required")
        if repo_key not in self._repos:
            return _error_text(f"Unknown repo: {repo_key!r}")

        hits = await self._mr_history.search(repo_key, query, k=k)
        if not hits:
            return _text_result(
                f"(no MR-history matches for query {query!r}; "
                f"index may be empty — run `virtual-dev index-mrs --repo {repo_key}`)"
            )
        parts: list[str] = []
        for hit in hits:
            parts.append(
                f"## !{hit.iid} — {hit.title}\n"
                f"score={hit.score:.3f}  author={hit.author_username}  "
                f"merged_at={hit.merged_at.isoformat() if hit.merged_at else '—'}\n"
                f"url: {hit.web_url}\n\n"
                f"{(hit.description or '')[:1200]}"
            )
        wrapped = self._filter.wrap("\n\n---\n\n".join(parts), source=f"mr_history:{repo_key}")
        return _text_result(wrapped.wrapped_text)


def _git_grep(repo_path: Path, pattern: str, max_results: int) -> str:
    """Run ``git grep -nI`` in ``repo_path``.

    Falls back to a plain message if the path is not a git repo. Output is
    capped at ``max_results`` lines; stderr is suppressed.
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
        lines = lines[:max_results]
        lines.append(f"... ({len(lines) - max_results} more matches truncated)")
    return "\n".join(lines)


def _text_result(text: str) -> dict[str, Any]:
    """Build an MCP-style tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _error_text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "is_error": True}
