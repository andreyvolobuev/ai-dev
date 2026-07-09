"""Read a GitLab merge request (title, status, description, diff) via API.

The web URL of an MR is useless to ``fetch_url`` — GitLab answers HTML
pages with a login redirect for token auth. This tool goes through the
authenticated VCS port instead, so the analyst can inspect the MRs that
tickets and chat threads link to.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext
from virtual_dev.tools._helpers import error_text, text_result

TOOL_GROUP = "shared"

_MAX_DESCRIPTION_CHARS = 4_000
_MAX_DIFF_CHARS = 16_000

_MR_URL_RE = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)")


def _project_path_of(repo_url: str) -> str:
    """``git@host:group/proj.git`` / ``https://host/group/proj.git`` →
    ``group/proj``. Mirrors the GitLab adapter's mapping."""
    if ":" in repo_url and "@" in repo_url and not repo_url.startswith(("http://", "https://")):
        path = repo_url.split(":", 1)[1]
    else:
        path = urlparse(repo_url).path.lstrip("/")
    return path.removesuffix(".git")


def resolve_mr_url(url: str, repositories: Any) -> tuple[str, int] | None:
    """Map an MR web URL onto ``(repo_key, iid)`` using the configured
    repositories. None when the URL isn't an MR link of a known repo."""
    parsed = urlparse(url)
    match = _MR_URL_RE.match(parsed.path or "")
    if not match:
        return None
    project = match.group("project").removesuffix(".git")
    for repo in repositories:
        if _project_path_of(repo.url) == project:
            return repo.key, int(match.group("iid"))
    return None


def build(ctx: ToolContext):
    researcher = ctx.researcher
    if researcher is None or researcher.vcs is None:
        return None

    @tool(
        "read_merge_request",
        "Read a GitLab merge request through the authenticated API: title, "
        "status, branches, description and the current diff. Pass either "
        "`url` (the MR link, e.g. https://gitlab.../-/merge_requests/869) "
        "or `repo_key` + `iid`. Use this instead of fetch_url for MR links "
        "— the web page only serves a login redirect.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "repo_key": {"type": "string"},
                "iid": {"type": "integer"},
            },
        },
    )
    async def _read_merge_request(args: dict[str, Any]) -> dict[str, Any]:
        return await run(researcher, args)

    return _read_merge_request


async def run(researcher, args: dict[str, Any]) -> dict[str, Any]:
    vcs = researcher.vcs
    if vcs is None:
        return error_text("GitLab is not configured — cannot read MRs.")

    repo_key = str(args.get("repo_key") or "")
    iid = int(args.get("iid") or 0)
    url = str(args.get("url") or "").strip()
    if url and not (repo_key and iid):
        resolved = resolve_mr_url(url, researcher.config.repositories)
        if resolved is None:
            known = ", ".join(sorted(researcher.repos))
            return error_text(
                f"Couldn't map {url!r} onto a configured repository "
                f"(known repos: {known}). Pass repo_key + iid explicitly."
            )
        repo_key, iid = resolved
    if not repo_key or not iid:
        return error_text("Pass either `url` or both `repo_key` and `iid`.")
    if repo_key not in researcher.repos:
        return error_text(f"Unknown repo: {repo_key!r}")

    try:
        mr = await vcs.get_merge_request(repo_key, iid)
        diff = await vcs.get_mr_diff(repo_key, iid)
    except Exception as exc:
        return error_text(f"GitLab API error for {repo_key}!{iid}: {exc}")

    description = (mr.description or "")[:_MAX_DESCRIPTION_CHARS]
    diff_out = diff or "(empty diff)"
    truncated = len(diff_out) > _MAX_DIFF_CHARS
    if truncated:
        diff_out = diff_out[:_MAX_DIFF_CHARS]

    body = (
        f"# {repo_key}!{mr.iid} — {mr.title}\n"
        f"status={mr.status.value}  author={mr.author_username}  "
        f"{mr.source_branch} → {mr.target_branch}\n"
        f"pipeline={mr.pipeline_status.value}  url: {mr.web_url}\n\n"
        f"## Description\n{description or '(empty)'}\n\n"
        f"## Diff\n{diff_out}"
        + ("\n\n(…diff truncated)" if truncated else "")
    )
    # MR titles/descriptions/diffs are human- and repo-supplied content —
    # untrusted input for the LLM, same as chat and MR-history output.
    wrapped = researcher.filter.wrap(body, source=f"merge_request:{repo_key}!{iid}")
    return text_result(wrapped.wrapped_text)
