"""Unit tests for DevAgent.

Same shape as Analyst tests: subclass DevAgent and override `_call_model` so
`claude-agent-sdk` is never invoked. The VCS port is a lightweight fake that
tracks calls and simulates a real workspace against tmp_path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import DevAgent, DevOutcome, DevSkipReason
from virtual_dev.application.services import RulesLoader
from virtual_dev.domain.models.merge_request import MergeRequest, MRStatus, PipelineStatus
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import Settings
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope


# --- Fakes ---


class _FakeVcs(VcsPort):
    """Minimal VcsPort fake driving a tmp_path as a checkout."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._branch = "main"
        self._dirty_files: set[str] = set()
        self.mr_payload: dict[str, Any] = {}

    async def ensure_clone(self, repo_key: str) -> str:
        self.calls.append(("ensure_clone", (repo_key,)))
        self._workspace.mkdir(parents=True, exist_ok=True)
        return str(self._workspace)

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:
        self.calls.append(("fetch_and_checkout", (repo_key, branch)))
        self._branch = branch

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
        self.calls.append(("create_branch", (repo_key, branch, base)))
        self._branch = branch

    async def commit_all(self, repo_key: str, message: str) -> str:
        self.calls.append(("commit_all", (repo_key, message)))
        if not self._dirty_files:
            return ""
        self._dirty_files.clear()
        return "abc123"

    async def push(self, repo_key: str, branch: str) -> None:
        self.calls.append(("push", (repo_key, branch)))

    async def current_branch(self, repo_key: str) -> str:
        return self._branch

    async def has_uncommitted_changes(self, repo_key: str) -> bool:
        return bool(self._dirty_files)

    async def create_merge_request(
        self, repo_key: str, source_branch: str, target_branch: str,
        title: str, description: str, draft: bool = False,
    ) -> MergeRequest:
        self.calls.append((
            "create_merge_request",
            (repo_key, source_branch, target_branch, title, description, draft),
        ))
        return MergeRequest(
            id="mrid-1", iid=42, project_id="p1",
            title=title, description=description,
            source_branch=source_branch, target_branch=target_branch,
            author_username="virtual-dev",
            web_url="https://gitlab.example/p1/-/merge_requests/42",
            status=MRStatus.DRAFT if draft else MRStatus.OPEN,
            pipeline_status=PipelineStatus.UNKNOWN,
        )

    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:  # pragma: no cover
        raise NotImplementedError

    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError

    async def reply_to_comment(self, repo_key: str, iid: int, comment_id: str, body: str) -> None:
        # pragma: no cover
        raise NotImplementedError

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    # --- test helpers ---

    def simulate_edit(self, path: str) -> None:
        self._dirty_files.add(path)


class _FakeCodeAgent(CodeAgentPort):
    def __init__(self, result: CodeAgentResult) -> None:
        self.result = result
        self.requests: list[CodeAgentRequest] = []

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        self.requests.append(request)
        return self.result

    def stream_task(self, request: CodeAgentRequest) -> Any:  # pragma: no cover
        raise NotImplementedError


class _TestDev(DevAgent):
    """DevAgent with an overridden `_call_model` and a hook that can dirty the fake VCS."""

    def __init__(
        self, *args: Any,
        preset_submission: dict[str, Any] | None,
        edits: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._preset_submission = preset_submission
        self._edits = edits or []
        self.last_request: CodeAgentRequest | None = None

    async def _call_model(
        self, request: CodeAgentRequest
    ) -> tuple[dict[str, Any], CodeAgentResult]:
        self.last_request = request
        vcs = cast(_FakeVcs, self._vcs)
        for path in self._edits:
            vcs.simulate_edit(path)
        result = await self._code_agent.run_task(request)
        return (self._preset_submission or {}), result


# --- Fixtures / helpers ---


def _cfg(repo_key: str = "bellingshausen") -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(
            key=repo_key,
            url=f"git@example:{repo_key}.git",
            default_branch="master",
            primary_language="python",
            tests_cmd="pytest",
            lint_cmd="ruff check .",
        )],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )


async def _insert_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target_repo_key: str | None = "bellingshausen",
    title: str = "Add users endpoint",
) -> int:
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id="DM-7",
            title=title, description="desc", url="https://jira/DM-7",
            components_json=[], labels_json=[], links_json=[],
            priority="medium", external_status="In Progress",
            internal_status="ready", dor_satisfied=False,
            target_repo_key=target_repo_key,
        )
        session.add(row)
        await session.flush()
        return cast(int, row.id)


async def _insert_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: PlanStatus = PlanStatus.READY,
    target_repo_key: str | None = "bellingshausen",
) -> None:
    async with session_scope(session_factory) as session:
        session.add(PlanRow(
            tracker="jira", task_external_id="DM-7",
            summary="Add an endpoint", steps_json=[
                {"order": 1, "summary": "write test", "details": "",
                 "repo_key": None, "files_touched": []},
                {"order": 2, "summary": "implement",  "details": "",
                 "repo_key": None, "files_touched": []},
            ],
            open_questions_json=[], risks_json=[], confidence=0.9,
            status=status.value, target_repo_key=target_repo_key,
            cost_usd=0.0, iterations=5, model="m", agent_key="analyst",
        ))


def _make_dev(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vcs: _FakeVcs,
    code_agent: CodeAgentPort,
    preset_submission: dict[str, Any] | None,
    edits: list[str] | None = None,
    rules_dir: Path | None = None,
) -> _TestDev:
    cfg = _cfg()
    rules_loader = RulesLoader(rules_dir or Path("/nonexistent_rules"))
    return _TestDev(
        agent_key="dev-bellingshausen-backend",
        repo_key="bellingshausen",
        specialisation="backend",
        vcs=vcs,
        code_agent=code_agent,
        rules_loader=rules_loader,
        session_factory=session_factory,
        config=cfg,
        settings=Settings(),
        preset_submission=preset_submission,
        edits=edits,
    )


# --- Tests ---


@pytest.mark.asyncio
async def test_dev_happy_path_opens_mr(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=8, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={
            "title": "Add users endpoint",
            "description": "Implemented per plan.",
            "status": "success",
            "notes": "Tests pass.",
        },
        edits=["app/users.py"],
    )
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.MR_OPENED
    assert result.merge_request is not None
    assert result.merge_request.iid == 42
    assert result.commit_sha == "abc123"
    assert result.branch_name is not None and result.branch_name.startswith("ai-dev/")

    # VCS call sequence includes the essentials in order.
    kinds = [c[0] for c in vcs.calls]
    assert "ensure_clone" in kinds
    assert "create_branch" in kinds
    assert "commit_all" in kinds
    assert "push" in kinds
    assert "create_merge_request" in kinds
    assert kinds.index("commit_all") < kinds.index("push") < kinds.index("create_merge_request")

    # DB state: task is MR_OPEN and MergeRequestRow persisted.
    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
        mr_row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.iid == 42)
        )).scalar_one()
    assert task_row.internal_status == TaskStatus.MR_OPEN.value
    assert mr_row.task_external_id == "DM-7"
    assert mr_row.source_branch == result.branch_name
    assert mr_row.status == MRStatus.DRAFT.value


@pytest.mark.asyncio
async def test_dev_skips_without_ready_plan(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory, status=PlanStatus.CLARIFYING)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None)
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.SKIPPED
    assert result.skip_reason is DevSkipReason.NO_READY_PLAN


@pytest.mark.asyncio
async def test_dev_fails_when_no_submission(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=30, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="max_turns",
    ))
    dev = _make_dev(session_factory, vcs=vcs, code_agent=code_agent, preset_submission={})
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.FAILED
    # No commit / push / MR should have been attempted.
    assert not any(c[0] in ("commit_all", "push", "create_merge_request") for c in vcs.calls)

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert task_row.internal_status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_dev_no_changes_does_not_open_mr(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=3, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    # edits=[] → clean tree; submit_mr called.
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "", "status": "success"},
    )
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.NO_CHANGES
    # commit_all was attempted, but no push / MR.
    kinds = [c[0] for c in vcs.calls]
    assert "commit_all" in kinds
    assert "push" not in kinds
    assert "create_merge_request" not in kinds


@pytest.mark.asyncio
async def test_dev_failed_submission_status(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=5, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={
            "title": "x", "description": "could not implement",
            "status": "failed", "notes": "missing upstream dep",
        },
        edits=["x.py"],   # irrelevant; failed status short-circuits before commit
    )
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.FAILED
    kinds = [c[0] for c in vcs.calls]
    assert "commit_all" not in kinds
    assert "create_merge_request" not in kinds


@pytest.mark.asyncio
async def test_system_prompt_includes_rules(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "dev-bellingshausen-backend.md").write_text(
        "## Style\n- Use double quotes.\n- Prefer pytest.approx for floats.\n"
    )

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "d", "status": "success"},
        edits=["x.py"],
        rules_dir=rules_dir,
    )
    await dev.handle_plan("jira", "DM-7")

    assert dev.last_request is not None
    prompt = dev.last_request.system_prompt
    assert "Use double quotes" in prompt
    assert "pytest.approx" in prompt
    assert "tests_cmd" in prompt  # repo context injected


def test_rules_loader_reads_agent_file(tmp_path: Path) -> None:
    (tmp_path / "foo.md").write_text("hello rules")
    loader = RulesLoader(tmp_path)
    assert loader.load("foo") == "hello rules"
    assert loader.load("missing") == ""
    assert loader.exists("foo") is True
    assert loader.exists("missing") is False


@pytest.mark.asyncio
async def test_dev_branch_name_contains_task_id(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _insert_task(session_factory, title="Very long title with lots of words")
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "d", "status": "success"},
        edits=["x.py"],
    )
    result = await dev.handle_plan("jira", "DM-7")
    assert result.branch_name is not None
    assert result.branch_name.startswith("ai-dev/dm-7-")
    # Title slug clamped at 40 chars (plus prefix and id).
    assert len(result.branch_name) <= len("ai-dev/dm-7-") + 40
