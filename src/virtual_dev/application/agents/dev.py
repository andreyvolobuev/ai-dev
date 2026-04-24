"""Dev agent — turns a Plan into code and a draft MR.

Flow for one task:

    1. Load task + latest READY Plan from DB.
    2. Refuse early if plan.status != READY, task.dor_satisfied != True,
       target_repo_key is missing, or no adapter-level prerequisites
       (VCS configured, repo in the allowlist, etc.) are met.
    3. Prepare the workspace:
         * ``vcs.ensure_clone(repo_key)``
         * ``vcs.create_branch(task_branch, base=default_branch)``
    4. Build a user prompt from the Plan. Inject the per-agent rule file
       (``config/rules/<agent_key>.md``) into the system prompt.
    5. Hand the request to ``CodeAgentPort`` with full Claude Code tools
       (Read / Edit / Write / Bash / Glob / Grep) in ``cwd = workspace``.
       Mount a private MCP server with a single ``submit_mr`` tool that
       captures the final title / description / notes.
    6. If submit_mr was called and the working tree has changes:
         * commit + push the branch
         * create a draft MR via the VCS port
         * persist ``MergeRequestRow`` and link it to the task
    7. Bubble up a structured :class:`DevResult` to the caller (the inbox
       decides side-effects in Jira / message bus).

Side-effects that reach a team-visible surface (Jira transitions, MR
comments) are NOT done here — the inbox handles those. Keeps the agent
testable and local.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services import ResearcherToolkit, RulesLoader
from virtual_dev.domain.models.merge_request import MergeRequest, MRStatus
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig, Settings
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan


_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")


class DevSkipReason(str, Enum):
    NO_TASK = "no_task"
    NO_READY_PLAN = "no_ready_plan"
    NO_TARGET_REPO = "no_target_repo"
    ALREADY_HAS_MR = "already_has_mr"


class DevOutcome(str, Enum):
    SKIPPED = "skipped"
    NO_CHANGES = "no_changes"           # Claude submitted but did not modify anything
    MR_OPENED = "mr_opened"
    FAILED = "failed"


@dataclass
class DevResult:
    outcome: DevOutcome
    skip_reason: DevSkipReason | None = None
    merge_request: MergeRequest | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    cost_usd: float = 0.0
    iterations: int = 0
    stopped_reason: str = ""
    submission: dict[str, Any] = field(default_factory=dict)


_DEV_SYSTEM_BASE = (
    "You are a Dev agent of a multi-agent AI developer. Your job: given a "
    "plan that an Analyst already built, implement it in the repository "
    "you're running in and open a Merge Request.\n"
    "\n"
    "Process:\n"
    "  1. You are already on a fresh branch based on the repo's default "
    "branch. Don't create more branches.\n"
    "  2. Use the built-in tools (Read / Glob / Grep / Edit / Write / Bash) "
    "to implement the plan step by step.\n"
    "  3. Run the repository's test suite (see `tests_cmd` in the plan "
    "instructions). Keep iterating until tests pass, OR until you are "
    "convinced they can't be made to pass within the scope of the plan.\n"
    "  4. When you are done, call the `submit_mr` tool exactly once with "
    "the MR title and description. Do NOT commit / push / create MR "
    "yourself — the runtime does that after you call submit_mr.\n"
    "  5. If you cannot make progress (e.g. the plan is unworkable, or "
    "external prerequisites are missing), still call `submit_mr` but set "
    "`status=\"failed\"` and explain in `notes`.\n"
    "\n"
    "Coding style (always enforced):\n"
    "  * Comments explain WHY, not WHAT. A well-named identifier already "
    "tells the reader what the code does; only write a comment when the "
    "reason is non-obvious (hidden constraint, workaround, invariant).\n"
    "  * Default is NO comment. Before writing one, ask: would a competent "
    "reader need this to avoid a wrong assumption? If no — drop it.\n"
    "  * Do not reference the ticket / this session / \"added for X\" in "
    "code comments — that belongs in the MR description.\n"
    "  * Do not add error handling, fallbacks or validation for scenarios "
    "that can't happen. Trust internal code.\n"
    "\n"
    "MR submission:\n"
    "  * The runtime prepends the ticket key to your title (e.g. "
    "\"DM-123: ...\"); do NOT include the key yourself.\n"
    "  * Title is a concise one-liner (<70 chars). Put details in "
    "description.\n"
)


def _strip_ticket_prefix(title: str, external_id: str) -> str:
    """Remove a leading ticket key the model might have prepended.

    Models love echoing ``[DM-123]`` / ``DM-123:`` into the MR title even
    when told not to; we strip it so the final MR title isn't doubled after
    the runtime's own ``{key}: `` prefix.
    """
    stripped = title.strip()
    for candidate in (f"[{external_id}]", f"{external_id}:", external_id):
        if stripped.lower().startswith(candidate.lower()):
            stripped = stripped[len(candidate):].lstrip(" :")
            break
    return stripped


class DevAgent:
    """Bellingshausen-backend-scoped Dev agent (Phase 2)."""

    def __init__(
        self,
        *,
        agent_key: str,
        repo_key: str,
        specialisation: str,              # "backend" | "frontend" | "devops"
        vcs: VcsPort,
        code_agent: CodeAgentPort,
        rules_loader: RulesLoader,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        settings: Settings,
        researcher: ResearcherToolkit | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._agent_key = agent_key
        self._repo_key = repo_key
        self._specialisation = specialisation
        self._vcs = vcs
        self._code_agent = code_agent
        self._rules = rules_loader
        self._researcher = researcher
        self._session_factory = session_factory
        self._config = config
        self._settings = settings
        self._max_turns = max_turns or _dev_max_turns(config) or 30

    @property
    def agent_key(self) -> str:
        return self._agent_key

    # --- entry ---

    async def handle_plan(self, tracker: str, external_id: str) -> DevResult:
        task_row, plan_row = await self._load(tracker, external_id)

        skip = self._precheck(task_row, plan_row)
        if skip is not None:
            logger.info(
                "Dev[{}] skipping {}: {}", self._agent_key, external_id, skip.value
            )
            return DevResult(outcome=DevOutcome.SKIPPED, skip_reason=skip)

        assert task_row is not None and plan_row is not None
        plan = row_to_plan(plan_row)

        # Prepare workspace — clone + fresh task branch off default.
        workspace_path = await self._vcs.ensure_clone(self._repo_key)
        branch_name = self._branch_name(task_row)
        base_branch = self._default_branch()
        await self._vcs.create_branch(self._repo_key, branch_name, base_branch)

        # Transition task.
        await self._set_internal_status(task_row.id, TaskStatus.CODING)

        request = self._build_request(
            plan=plan, task_row=task_row, workspace_path=workspace_path,
        )
        try:
            captured, result = await self._call_model(request)
        except Exception:
            logger.exception("Dev[{}] model call failed for {}", self._agent_key, external_id)
            await self._set_internal_status(task_row.id, TaskStatus.FAILED)
            raise

        if not captured:
            logger.warning(
                "Dev[{}] model finished without calling submit_mr for {}; stop={}",
                self._agent_key, external_id, result.stopped_reason,
            )
            await self._set_internal_status(task_row.id, TaskStatus.FAILED)
            return DevResult(
                outcome=DevOutcome.FAILED,
                branch_name=branch_name,
                cost_usd=result.cost_usd,
                iterations=result.turns,
                stopped_reason=result.stopped_reason,
            )

        status_val = str(captured.get("status") or "success").lower()
        if status_val == "failed":
            await self._set_internal_status(task_row.id, TaskStatus.FAILED)
            return DevResult(
                outcome=DevOutcome.FAILED,
                branch_name=branch_name,
                cost_usd=result.cost_usd,
                iterations=result.turns,
                stopped_reason=result.stopped_reason,
                submission=captured,
            )

        # Commit → push → MR.
        commit_message = self._render_commit_message(task_row, captured)
        commit_sha = await self._vcs.commit_all(self._repo_key, commit_message)

        if not commit_sha:
            logger.info(
                "Dev[{}] submit_mr called but working tree was clean for {}",
                self._agent_key, external_id,
            )
            await self._set_internal_status(task_row.id, TaskStatus.FAILED)
            return DevResult(
                outcome=DevOutcome.NO_CHANGES,
                branch_name=branch_name,
                cost_usd=result.cost_usd,
                iterations=result.turns,
                stopped_reason=result.stopped_reason,
                submission=captured,
            )

        await self._vcs.push(self._repo_key, branch_name)

        mr = await self._vcs.create_merge_request(
            repo_key=self._repo_key,
            source_branch=branch_name,
            target_branch=base_branch,
            title=self._render_mr_title(task_row, captured),
            description=self._render_mr_description(task_row, plan, captured),
            draft=self._settings.dev_mr_draft,
        )
        logger.info(
            "Dev[{}] opened MR !{} for {}: {}",
            self._agent_key, mr.iid, external_id, mr.web_url,
        )

        await self._persist_mr(task_row=task_row, mr=mr, branch=branch_name)
        await self._set_internal_status(task_row.id, TaskStatus.MR_OPEN)

        return DevResult(
            outcome=DevOutcome.MR_OPENED,
            merge_request=mr,
            branch_name=branch_name,
            commit_sha=commit_sha,
            cost_usd=result.cost_usd,
            iterations=result.turns,
            stopped_reason=result.stopped_reason,
            submission=captured,
        )

    # --- Model call (overridable in tests) ---

    async def _call_model(
        self, request: CodeAgentRequest
    ) -> tuple[dict[str, Any], CodeAgentResult]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_mr",
            "Call this exactly once at the end. Provide the MR title "
            "and a detailed description of what was done.",
            _SUBMIT_MR_SCHEMA,
        )
        async def _submit_mr(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "MR submission recorded."}]}

        mr_server = create_sdk_mcp_server(
            name="virtual_dev_dev_submit", version="0.1.0", tools=[_submit_mr]
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_dev_submit": mr_server,
        }
        allowed_tool_names = [
            "mcp__virtual_dev_dev_submit__submit_mr",
            # Full Claude Code tool surface in the workspace.
            "Read", "Glob", "Grep", "Edit", "Write", "Bash",
        ]
        # MR-history search is the one piece the Dev doesn't get from
        # built-in tools: let it peek at how similar changes were done before.
        if self._researcher is not None:
            mcp_servers["virtual_dev_researcher"] = self._researcher.build_mcp_server()
            allowed_tool_names.append("mcp__virtual_dev_researcher__search_mr_history")
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed_tool_names

        result = await self._code_agent.run_task(request)
        return captured, result

    # --- request building ---

    def _build_request(
        self, *, plan: Plan, task_row: TaskRow, workspace_path: str
    ) -> CodeAgentRequest:
        system_prompt = self._compose_system_prompt(task_row)
        user_prompt = self._render_user_prompt(task_row=task_row, plan=plan)
        return CodeAgentRequest(
            agent_key=self._agent_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=workspace_path,
            max_turns=self._max_turns,
            model=self._config.agents.models.default,
        )

    def _compose_system_prompt(self, task_row: TaskRow) -> str:
        parts: list[str] = [_DEV_SYSTEM_BASE]
        repo_cfg = self._config.get_repository(self._repo_key)
        if repo_cfg is not None:
            parts.append("")
            parts.append("## Repository context")
            parts.append(f"- key: {repo_cfg.key}")
            parts.append(f"- primary_language: {repo_cfg.primary_language or 'unknown'}")
            if repo_cfg.tests_cmd:
                parts.append(f"- tests_cmd: `{repo_cfg.tests_cmd}`")
            if repo_cfg.lint_cmd:
                parts.append(f"- lint_cmd: `{repo_cfg.lint_cmd}`")

        rules = self._rules.load(self._agent_key)
        if rules:
            parts.append("")
            parts.append("## Rules for this agent")
            parts.append(rules)
        return "\n".join(parts)

    def _render_user_prompt(self, *, task_row: TaskRow, plan: Plan) -> str:
        parts: list[str] = []
        parts.append(f"# Ticket {task_row.tracker}:{task_row.external_id}")
        parts.append(f"**Title:** {task_row.title}")
        if task_row.url:
            parts.append(f"**Tracker URL:** {task_row.url}")
        parts.append("")
        parts.append("## Plan (from Analyst)")
        parts.append(plan.summary or "(empty summary)")
        if plan.steps:
            parts.append("")
            parts.append("### Steps")
            for step in plan.steps:
                line = f"{step.order}. **{step.summary}**"
                if step.details:
                    line += f"\n   {step.details}"
                if step.files_touched:
                    line += f"\n   Files: {', '.join(step.files_touched)}"
                parts.append(line)
        if plan.risks:
            parts.append("")
            parts.append("### Known risks")
            for risk in plan.risks:
                parts.append(f"- {risk}")
        parts.append("")
        parts.append(
            "When you are done (or convinced you can't proceed), call "
            "`submit_mr` with: title, description, status "
            '(one of "success" or "failed"), notes (string, optional).'
        )
        return "\n".join(parts)

    def _render_commit_message(self, task_row: TaskRow, submission: dict[str, Any]) -> str:
        title = _strip_ticket_prefix(
            str(submission.get("title") or task_row.title),
            task_row.external_id,
        )
        return f"[{task_row.external_id}] {title}"

    def _render_mr_title(self, task_row: TaskRow, submission: dict[str, Any]) -> str:
        title = _strip_ticket_prefix(
            str(submission.get("title") or task_row.title),
            task_row.external_id,
        )
        return f"{task_row.external_id}: {title}"

    def _render_mr_description(
        self, task_row: TaskRow, plan: Plan, submission: dict[str, Any]
    ) -> str:
        description = str(submission.get("description") or "").strip()
        notes = str(submission.get("notes") or "").strip()

        parts: list[str] = []
        parts.append(f"**Ticket:** [{task_row.external_id}]({task_row.url})")
        parts.append("")
        parts.append("## What changed")
        parts.append(description or "(no description provided)")
        if plan.steps:
            parts.append("")
            parts.append("## Plan (from Analyst)")
            for step in plan.steps:
                parts.append(f"- {step.summary}")
        if notes:
            parts.append("")
            parts.append("## Notes from the Dev agent")
            parts.append(notes)
        parts.append("")
        parts.append("---")
        parts.append("_Opened by Virtual Dev. Please review before merging._")
        return "\n".join(parts)

    # --- DB helpers ---

    async def _load(
        self, tracker: str, external_id: str
    ) -> tuple[TaskRow | None, PlanRow | None]:
        async with self._session_factory() as session:
            task_row = (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == tracker,
                    TaskRow.external_id == external_id,
                )
            )).scalar_one_or_none()
            plan_row = None
            if task_row is not None:
                plan_row = (await session.execute(
                    select(PlanRow)
                    .where(
                        PlanRow.tracker == tracker,
                        PlanRow.task_external_id == external_id,
                        PlanRow.status == PlanStatus.READY.value,
                    )
                    .order_by(PlanRow.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
        return task_row, plan_row

    def _precheck(
        self, task_row: TaskRow | None, plan_row: PlanRow | None
    ) -> DevSkipReason | None:
        """Entry gates for the Dev-agent.

        Three checks in order:
            1. Task exists in our DB (orchestrator must have seen it).
            2. A READY plan exists (Analyst judged the ticket actionable;
               CLARIFYING plans wait for a human to answer open questions).
            3. The plan's target_repo_key matches this Dev-agent's repo —
               otherwise another Dev-agent will pick it up.

        The human gate is at a higher level: a ticket only reaches the
        orchestrator if it has the configured Jira label (`ai-dev` by
        default). Once that's set, the rest of the pipeline is automatic.
        """
        if task_row is None:
            return DevSkipReason.NO_TASK
        if plan_row is None:
            return DevSkipReason.NO_READY_PLAN
        target = plan_row.target_repo_key or task_row.target_repo_key
        if target != self._repo_key:
            return DevSkipReason.NO_TARGET_REPO
        return None

    async def _persist_mr(
        self, *, task_row: TaskRow, mr: MergeRequest, branch: str
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = MergeRequestRow(
                repo_key=self._repo_key,
                iid=mr.iid,
                external_id=str(mr.id),
                task_external_id=task_row.external_id,
                title=mr.title,
                description=mr.description,
                source_branch=branch,
                target_branch=mr.target_branch,
                author_username=mr.author_username,
                web_url=mr.web_url,
                status=(MRStatus.DRAFT.value if self._settings.dev_mr_draft else MRStatus.OPEN.value),
                approvals_count=mr.approvals_count,
                approvals_required=mr.approvals_required,
                pipeline_status=mr.pipeline_status.value,
                pipeline_url=mr.pipeline_url,
            )
            session.add(row)

    async def _set_internal_status(self, task_row_id: int, status: TaskStatus) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_row_id)
            )).scalar_one_or_none()
            if row is not None:
                row.internal_status = status.value

    # --- misc ---

    def _branch_name(self, task_row: TaskRow) -> str:
        external_slug = _slugify(task_row.external_id)
        title_slug = _slugify(task_row.title)[:40].strip("-")
        prefix = self._settings.dev_branch_prefix.strip("/")
        if title_slug:
            return f"{prefix}/{external_slug}-{title_slug}"
        return f"{prefix}/{external_slug}"

    def _default_branch(self) -> str:
        repo = self._config.get_repository(self._repo_key)
        return repo.default_branch if repo is not None else "main"


# --- helpers ---


_SUBMIT_MR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": ["success", "failed"]},
        "notes": {"type": "string"},
    },
    "required": ["title", "description", "status"],
}


def _slugify(text: str) -> str:
    return _SLUG_SAFE_RE.sub("-", (text or "").lower()).strip("-")


def _dev_max_turns(config: AppConfig) -> int | None:
    cfg = config.agents.agents.get("developer")
    return cfg.max_iterations_per_task if cfg is not None else None


__all__ = ["DevAgent", "DevOutcome", "DevResult", "DevSkipReason"]
