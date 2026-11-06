"""Unit tests for DevAgent.

Same shape as Analyst tests: subclass DevAgent and override `_call_model` so
`claude-agent-sdk` is never invoked. The VCS port is a lightweight fake that
tracks calls and simulates a real workspace against tmp_path.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.vcs.gitlab import VcsError, VcsRogueCommitError
from virtual_dev.application.agents import DevAgent, DevOutcome, DevSkipReason
from virtual_dev.application.agents.dev import CodeAgentPermanentError
from virtual_dev.application.services import PromptsLoader, RulesLoader
from virtual_dev.domain.models.merge_request import MergeRequest, MRStatus, PipelineStatus
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.models.wiki_article import WikiArticle
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.domain.ports.professor_wiki import (
    MANDATORY_REVIEW_TOPIC,
    ProfessorWikiPort,
    WikiUnavailableError,
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
from virtual_dev.infrastructure.db.mappers import row_to_plan

# --- Fakes ---


class _FakeVcs(VcsPort):
    """Minimal VcsPort fake driving a tmp_path as a checkout."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._branch = "main"
        self._dirty_files: set[str] = set()
        self.mr_payload: dict[str, Any] = {}
        self.head = "0ld0000000000"

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

    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None:
        self.calls.append(("checkout_existing_branch", (repo_key, branch)))
        self._branch = branch

    async def commit_all(self, repo_key: str, message: str) -> str:
        self.calls.append(("commit_all", (repo_key, message)))
        if not self._dirty_files:
            return ""
        self._dirty_files.clear()
        self.head = "abc123"
        return "abc123"

    async def head_sha(self, repo_key: str) -> str:
        return self.head

    async def push(self, repo_key: str, branch: str, *, force: bool = False) -> None:
        self.calls.append(("push", (repo_key, branch, force)))

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

    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500
    ) -> Sequence[MergeRequest]:  # pragma: no cover
        raise NotImplementedError

    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError

    async def add_mr_comment(self, repo_key: str, iid: int, body: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def reply_to_comment(self, repo_key: str, iid: int, comment_id: str, body: str) -> None:
        # pragma: no cover
        raise NotImplementedError

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def merge(self, repo_key: str, iid: int) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_mr_approvals(self, repo_key: str, iid: int) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80
    ) -> Sequence[Any]:  # pragma: no cover
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
    trace: object | None = None,
    wiki: ProfessorWikiPort | None = None,
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
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        session_factory=session_factory,
        config=cfg,
        settings=Settings(),
        trace=trace,
        wiki=wiki,
        preset_submission=preset_submission,
        edits=edits,
    )


class _WikiStub(ProfessorWikiPort):
    """Подменный порт обязательной статьи: фиксированный результат или ошибка."""

    def __init__(
        self,
        article: WikiArticle | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._article = article
        self._error = error
        self.calls: list[str] = []

    async def fetch_mandatory_article(self, topic: str) -> WikiArticle:
        self.calls.append(topic)
        if self._error is not None:
            raise self._error
        assert self._article is not None
        return self._article


_MANDATORY_ARTICLE = WikiArticle(
    topic=MANDATORY_REVIEW_TOPIC,
    text=(
        '<untrusted_content source="lotsiya:krusenstern/chris-review">\n'
        "Лоция — статья «Chris Review» (тема: krusenstern/chris-review).\n"
        "Статус: проверено 2026-11-02.\n"
        "- ревью честное: отсутствие данных не выдаётся за факт\n"
        "</untrusted_content>"
    ),
)


def _dummy_code_agent() -> _FakeCodeAgent:
    return _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))


class _RecordingTrace:
    """Minimal AgentTrace stand-in collecting emitted events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


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
    assert mr_row.status == MRStatus.OPEN.value


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
async def test_dev_skips_when_open_mr_already_exists(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A re-published plan.ready (manual `dev-task` while inbox is live,
    or a duplicate delivery) must not open a second MR for work already
    in flight."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    async with session_scope(session_factory) as session:
        session.add(MergeRequestRow(
            repo_key="bellingshausen", iid=99,
            external_id="999", task_external_id="DM-7",
            title="prior MR", description="", source_branch="ai-dev/dm-7",
            target_branch="main", author_username="bot",
            web_url="https://gl/mr/999",
            status=MRStatus.DRAFT.value,
            approvals_count=0, approvals_required=1,
            pipeline_status=PipelineStatus.UNKNOWN.value, pipeline_url="",
        ))

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None)
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.SKIPPED
    assert result.skip_reason is DevSkipReason.ALREADY_HAS_MR
    # No VCS side effects.
    assert vcs.calls == []


@pytest.mark.asyncio
async def test_dev_does_not_raise_when_submission_captured_despite_is_error(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Real prod regression: SDK saw a stream blip mid-run and set
    is_error=True, BUT the model recovered, called submit_mr, and the
    capture is set. Earlier code raised CodeAgentInfraError anyway,
    bus redelivered, dev re-ran, model got confused and never
    submitted the second time around — work was already done but
    looked stuck.

    Rule: a present capture is the ground truth. If the model
    submitted, we proceed with commit/push/MR regardless of any
    transient is_error from earlier in the stream."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    # Simulate a real edit so commit_all returns a sha (mirrors what
    # the real model would have done before submit_mr).
    vcs._dirty_files.add("stub.py")
    vcs.mr_payload = {"iid": 99, "id": 999, "web_url": "https://gl/mr/999"}

    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=22, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
        is_error=True,           # transient mid-stream error
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={
            "title": "fix: blah",
            "description": "details",
            "status": "success",
        },
    )

    result = await dev.handle_plan("jira", "DM-7")
    # Did NOT raise; treat capture as authoritative.
    assert result.outcome is DevOutcome.MR_OPENED


@pytest.mark.asyncio
async def test_dev_raises_on_infra_failure_so_bus_redelivers(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When Claude CLI dies mid-run (network drop, stream idle timeout,
    SDK reports is_error=True), dev must NOT swallow it as a normal
    FAILED outcome — that gets ack'd and the work is silently lost.
    Raise instead so AgentRunner skips the ack and the bus lease
    expires → message is redelivered when the CLI is healthy again."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=22, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="stop_sequence",
        is_error=True,
    ))
    dev = _make_dev(session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None)

    with pytest.raises(Exception) as exc_info:
        await dev.handle_plan("jira", "DM-7")
    assert "infra" in str(exc_info.value).lower() or "stream" in str(exc_info.value).lower() \
        or "claude" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_dev_raises_permanent_on_dirty_working_tree(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If the local checkout has uncommitted changes (operator left
    debris from a previous run), ``ensure_clone`` raises ``VcsError``.
    Retrying won't fix it — we must mark the task FAILED, raise
    ``CodeAgentPermanentError`` so the bus acks (no redelivery) and the
    inbox can post a tracker comment so a human steps in."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _DirtyVcs(_FakeVcs):
        async def ensure_clone(self, repo_key: str) -> str:
            raise VcsError(
                f"local_path for {repo_key!r} (/tmp) has uncommitted changes; "
                f"stash or commit before running the Dev-agent"
            )

    vcs = _DirtyVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )

    with pytest.raises(CodeAgentPermanentError) as exc_info:
        await dev.handle_plan("jira", "DM-7")
    assert "uncommitted" in str(exc_info.value).lower()

    # Task must be FAILED so the recovery sweep doesn't re-publish
    # plan.ready every 600s.
    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert task_row.internal_status == TaskStatus.FAILED.value
    # No model call, no commit/push.
    kinds = [c[0] for c in vcs.calls]
    assert "create_branch" not in kinds
    assert "commit_all" not in kinds


@pytest.mark.asyncio
async def test_iteration_refuses_to_push_a_rogue_commit(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A commit authored by someone else (``commit_all`` raises
    ``VcsRogueCommitError``) is never pushed. The MR is alive, so the task
    is not failed — the iteration reports what's in the way instead."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _RogueVcs(_FakeVcs):
        async def commit_all(self, repo_key: str, message: str) -> str:
            self.calls.append(("commit_all", (repo_key, message)))
            raise VcsRogueCommitError(
                "branch 'ai-dev/dm-7' HEAD ab29843d authored by "
                "'e.yakovlev@2gis.ru' (not bot identity)"
            )

    vcs = _RogueVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=3, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={
            "status": "success",
            "title": "address review: rename foo",
            "description": "rename foo to bar per reviewer ask",
        },
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7",
        branch_name="ai-dev/dm-7",
        feedback="please rename foo",
    )

    assert result.outcome is DevOutcome.FAILED
    assert result.stopped_reason == "rogue-commit"
    assert "не от моего имени" in result.blocker
    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert task_row.internal_status != TaskStatus.FAILED.value
    kinds = [c[0] for c in vcs.calls]
    assert "commit_all" in kinds
    assert "push" not in kinds


@pytest.mark.asyncio
async def test_iteration_applies_mr_title_and_description_update(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A reviewer ask like «поправь заголовок MR» can't be a commit —
    when the model passes mr_title/mr_description, the iteration must
    push them to the VCS and mirror them into the MR row. Before this,
    the bot promised the rename, committed nothing relevant, and the MR
    title never changed."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    async with session_scope(session_factory) as session:
        session.add(MergeRequestRow(
            repo_key="bellingshausen", iid=99,
            external_id="999", task_external_id="DM-7",
            title="DM-7: Терпимость к падениям", description="старое описание",
            source_branch="ai-dev/dm-7",
            target_branch="main", author_username="bot",
            web_url="https://gl/mr/999",
            status=MRStatus.OPEN.value,
            approvals_count=0, approvals_required=1,
            pipeline_status=PipelineStatus.UNKNOWN.value, pipeline_url="",
        ))

    class _UpdatableVcs(_FakeVcs):
        async def update_merge_request(
            self, repo_key: str, iid: int, *,
            title: str | None = None, description: str | None = None,
        ) -> bool:
            self.calls.append(("update_merge_request", (repo_key, iid, title, description)))
            return True

    vcs = _UpdatableVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=2, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={
            "status": "success",
            "title": "address review: retitle MR",
            "description": "retitle only",
            "mr_title": "[DM-7] Tolerate per-source failures in GC",
            "mr_description": "English description per review",
        },
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7",
        branch_name="ai-dev/dm-7",
        feedback="поправь заголовок MR: тикет в скобки, текст на английском",
    )

    # Metadata update reached the VCS even though nothing was committed —
    # and the outcome says so. Seen live: a successful description-only
    # iteration came back as NO_CHANGES and the thread got the
    # "не смогла внести правку, смотри логи" failure template.
    assert result.outcome is DevOutcome.METADATA_UPDATED
    assert ("update_merge_request", (
        "bellingshausen", 99,
        "[DM-7] Tolerate per-source failures in GC",
        "English description per review",
    )) in vcs.calls

    # And the local MR row mirrors the new metadata.
    async with session_factory() as session:
        row = (await session.execute(
            select(MergeRequestRow).where(MergeRequestRow.iid == 99)
        )).scalar_one()
    assert row.title == "[DM-7] Tolerate per-source failures in GC"
    assert row.description == "English description per review"


@pytest.mark.asyncio
async def test_iteration_tells_the_model_about_base_conflicts_instead_of_merging(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Merging master before every run put merge commits into a history
    the team keeps clean, and a conflict failed the run before the model
    could resolve it (rainbow!1031). Conflicts are now a fact in the
    prompt; nothing is merged up front."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _ConflictVcs(_FakeVcs):
        async def base_conflicts(self, repo_key: str, base: str) -> list[str]:
            return ["rapid/app/schemas.py"]

        async def merge_base_into_current(
            self, repo_key: str, base: str,
        ) -> str | None:  # pragma: no cover - must not be called
            self.calls.append(("merge_base_into_current", (repo_key, base)))
            return None

    vcs = _ConflictVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"status": "success", "title": "t", "description": ""},
    )

    await dev.handle_iteration(
        tracker="jira", external_id="DM-7",
        branch_name="ai-dev/dm-7", feedback="почини",
    )

    assert "merge_base_into_current" not in [c[0] for c in vcs.calls]
    prompt = dev.last_request.user_prompt if dev.last_request else ""
    assert "rapid/app/schemas.py" in prompt


@pytest.mark.asyncio
async def test_iteration_model_failure_is_traced(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A model-side failure (status=failed / no submission) must also land
    in the activity feed with the model's own notes as the detail."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=30, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="max_turns",
    ))
    trace = _RecordingTrace()
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"status": "failed", "notes": "CI инфраструктура лежит"},
        trace=trace,
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7",
        branch_name="ai-dev/dm-7", feedback="почини",
    )

    assert result.outcome is DevOutcome.FAILED
    failed = [e for e in trace.events if e.type == "iteration_failed"]
    assert failed
    assert "CI инфраструктура лежит" in failed[0].payload["detail"]


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


def test_dev_tool_surface_has_all_expected_tools(
    tmp_path: Path,
) -> None:
    """Regression: every tool a dev-agent is supposed to use must
    actually register in the MCP surface. A tool whose ``build()``
    returns None (because ToolContext is missing a required field)
    silently drops out of the allow-list and the agent never finds
    it via the auto-rendered catalogue.

    For dev that means:
      * ``submit_mr`` — terminal tool. Needs submit_capture + run_state.
      * ``search_mr_history`` — only shared-group tool the dev is
        allowed to see (other shared tools are stripped from the
        allow-list intentionally). Needs researcher.

    Filesystem builtins (Read/Glob/Grep/Edit/Write/Bash) are SDK-side,
    not MCP — checked separately as a sanity assertion."""
    from virtual_dev.application.services import PromptsLoader, RulesLoader
    from virtual_dev.application.agents.dev import DevAgent
    from virtual_dev.application.services import ResearcherToolkit

    cfg = _cfg()
    dev = DevAgent(
        agent_key="dev-bellingshausen-backend",
        repo_key="bellingshausen",
        specialisation="backend",
        vcs=cast(VcsPort, _FakeVcs(tmp_path / "ws")),
        code_agent=cast(CodeAgentPort, _FakeCodeAgent(CodeAgentResult(
            final_text="", turns=0, input_tokens=0, output_tokens=0,
            cost_usd=0.0, stopped_reason="end_turn",
        ))),
        rules_loader=RulesLoader(Path("/nonexistent_rules")),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        session_factory=cast(Any, None),
        config=cfg,
        settings=Settings(),
        researcher=cast(ResearcherToolkit, object()),
    )

    _, allowed_tool_names, groups, captured = dev._build_tool_surface()

    # Tools the dev MUST be able to call.
    expected_mcp = {
        "mcp__virtual_dev_dev__submit_mr",
        "mcp__virtual_dev_shared__search_mr_history",
    }
    missing = expected_mcp - set(allowed_tool_names)
    assert not missing, f"missing required dev tools: {missing}"

    # The terminal tool must be in the dev-group catalogue so it
    # surfaces in the auto-rendered tools_catalog.
    assert any(t.name == "submit_mr" for t in groups.get("dev", []))

    # SDK filesystem builtins are appended in addition.
    for builtin in ("Read", "Glob", "Grep", "Edit", "Write", "Bash"):
        assert builtin in allowed_tool_names

    # captured is the same dict the runtime reads back after the
    # model calls submit_mr; must be a mutable dict.
    assert isinstance(captured, dict)


def test_dev_tool_surface_adds_configured_professor_wiki(
    tmp_path: Path,
) -> None:
    """When Professor is configured, the dev surface keeps its local
    tools and gains exactly the two read-only wiki tools."""
    import os
    from unittest import mock

    from virtual_dev.application.services import ResearcherToolkit
    from virtual_dev.tools import (
        PROFESSOR_SERVER_KEY,
        PROFESSOR_WIKI_GET_PAGE_TOOL,
        PROFESSOR_WIKI_SEARCH_TOOL,
    )

    fake_env = {
        "PROFESSOR_MCP_URL": "https://professor.example/mcp",
        "PROFESSOR_MCP_TOKEN": "prof-token",
    }
    with mock.patch.dict(os.environ, fake_env, clear=False):
        settings = Settings()

    dev = DevAgent(
        agent_key="dev-bellingshausen-backend",
        repo_key="bellingshausen",
        specialisation="backend",
        vcs=cast(VcsPort, _FakeVcs(tmp_path / "ws")),
        code_agent=cast(CodeAgentPort, _FakeCodeAgent(CodeAgentResult(
            final_text="", turns=0, input_tokens=0, output_tokens=0,
            cost_usd=0.0, stopped_reason="end_turn",
        ))),
        rules_loader=RulesLoader(Path("/nonexistent_rules")),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        session_factory=cast(Any, None),
        config=_cfg(),
        settings=settings,
        researcher=cast(ResearcherToolkit, object()),
    )

    servers, allowed_tool_names, _groups, _captured = dev._build_tool_surface()

    assert PROFESSOR_SERVER_KEY in servers
    assert servers[PROFESSOR_SERVER_KEY]["type"] == "http"
    assert servers[PROFESSOR_SERVER_KEY]["headers"] == {
        "Authorization": "Bearer prof-token",
    }
    assert PROFESSOR_WIKI_SEARCH_TOOL in allowed_tool_names
    assert PROFESSOR_WIKI_GET_PAGE_TOOL in allowed_tool_names
    # The shared-group filter must not affect the remote server.
    assert "mcp__virtual_dev_shared__search_mr_history" in allowed_tool_names


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


@pytest.mark.asyncio
async def test_iteration_prompt_for_mr_review_extracts_rule_into_claude_md(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When iterating on MR-review feedback, dev must be told to also
    add a generalised rule to the target repo's CLAUDE.md (so the next
    Claude Code session on that repo doesn't repeat the mistake).

    The instruction must mention CLAUDE.md by name, tell the model to
    create the file if it doesn't exist, and set the quality bar that
    keeps the file a convention book rather than a review log: rules
    must pay off in a future *unrelated* task, must not pin
    point-in-time values, and must not restate what a linter already
    enforces. The old prompt's "use double quotes → ## Style" example
    taught the model to log linter-grade nits — teams complained, so
    its absence is part of the contract."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )

    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    prompt = dev._render_iteration_prompt(
        task_row=task_row,
        plan=plan,
        feedback="please rename some_var to user_count",
        feedback_kind="mr_review",
    )

    assert "CLAUDE.md" in prompt
    # Should tell the model to create the file if missing.
    assert "create" in prompt.lower() or "doesn't exist" in prompt.lower()
    # Reusability bar: a rule must matter in a future, unrelated task.
    assert "unrelated task" in prompt
    # Durability bar: no point-in-time values that rot.
    assert "point-in-time" in prompt
    # No duplicating what tooling already enforces.
    assert "linter" in prompt.lower()
    # The linter-nit example must stay gone.
    assert "use double quotes" not in prompt.lower()
    assert "## Style" not in prompt


@pytest.mark.asyncio
async def test_iteration_prompt_claude_md_hygiene_and_complaint_handling(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The CLAUDE.md instruction must also cover file hygiene (refine
    an overlapping bullet in place instead of appending a near-twin)
    and how to react when a human complains about the entries: comply
    with the specific request, but never swear off maintaining the
    file — that maintenance is operator policy, not the reviewer's
    call. Аида once promised «больше этот файл трогать не буду», which
    contradicts why she exists."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )

    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    prompt = dev._render_iteration_prompt(
        task_row=task_row,
        plan=plan,
        feedback="всё про style удали и больше сама в этот файл ничего не добавляй",
        feedback_kind="mr_review",
    )

    assert "instead of appending" in prompt
    assert "never promise to stop" in prompt
    # When the feedback IS a CLAUDE.md edit request, that edit is the
    # whole deliverable. Without this the model deletes the bullet,
    # "appends" an equivalent one back, the tree ends up clean and the
    # run is misreported as NO_CHANGES (see Denis's review of the old
    # instruction).
    assert "do not add a replacement" in prompt


@pytest.mark.asyncio
async def test_iteration_prompt_for_ci_failure_does_not_pollute_claude_md(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """CI failure feedback is about a transient bug, not a project
    convention — extracting it into CLAUDE.md would pollute the rule
    book with one-off pipeline noise."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )

    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    prompt = dev._render_iteration_prompt(
        task_row=task_row,
        plan=plan,
        feedback="job tests failed: AssertionError on line 12",
        feedback_kind="ci_failure",
    )

    assert "CLAUDE.md" not in prompt


def _thread_msg(*, id: str, author: str, text: str) -> Any:
    from datetime import UTC, datetime

    from virtual_dev.domain.models.chat import ChatMessage
    return ChatMessage(
        id=id, channel_id="team-chan", author_id=author, text=text,
        timestamp=datetime.now(UTC), thread_root_id="root-x",
        trusted=False,
    )


@pytest.mark.asyncio
async def test_iteration_prompt_includes_thread_when_provided(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The thread responder boils a human conversation down to a single
    ``iteration_feedback`` blob — but the original human words are the
    source of truth. Forwarding the raw transcript lets the dev agent
    sanity-check that interpretation against what reviewers actually
    wrote."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )

    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    thread = [
        _thread_msg(id="p1", author="alice", text="please rename foo to bar"),
        _thread_msg(id="p2", author="bob",   text="actually let's call it baz"),
    ]
    prompt = dev._render_iteration_prompt(
        task_row=task_row, plan=plan,
        feedback="Rename foo to bar.",
        feedback_kind="mr_review",
        thread=thread,
    )

    assert "please rename foo to bar" in prompt
    assert "actually let's call it baz" in prompt
    # Wrapped in an untrusted-content envelope sourced from MM thread,
    # consistent with how feedback itself is wrapped today.
    assert 'source="mm:thread"' in prompt


@pytest.mark.asyncio
async def test_iteration_prompt_omits_thread_section_when_thread_empty(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """CI-failure iterations have no human thread; the prompt should
    not render an empty 'Reviewer thread' section that would only
    confuse the model into hallucinating context that isn't there."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )
    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    prompt_no_thread = dev._render_iteration_prompt(
        task_row=task_row, plan=plan,
        feedback="job tests failed", feedback_kind="ci_failure",
    )
    prompt_empty_thread = dev._render_iteration_prompt(
        task_row=task_row, plan=plan,
        feedback="job tests failed", feedback_kind="ci_failure",
        thread=[],
    )

    for prompt in (prompt_no_thread, prompt_empty_thread):
        assert "Reviewer thread" not in prompt
        assert "Original thread" not in prompt


@pytest.mark.asyncio
async def test_iteration_prompt_tells_dev_to_prefer_thread_on_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When feedback (responder's interpretation) and the raw thread
    disagree, the dev must NOT silently guess. Doctrine: trust the
    humans' actual words, and if still ambiguous, submit ``failed``
    with a clarifying note rather than fabricate intent."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent, preset_submission=None,
    )
    task_row, plan_row = await dev._load("jira", "DM-7")
    assert task_row is not None and plan_row is not None
    plan = row_to_plan(plan_row)

    thread = [_thread_msg(id="p1", author="alice", text="anything")]
    prompt = dev._render_iteration_prompt(
        task_row=task_row, plan=plan,
        feedback="Do X.", feedback_kind="mr_review", thread=thread,
    )

    lower = prompt.lower()
    # Doctrine signal: model must be told to prefer the raw thread
    # over the interpreted feedback when they disagree.
    assert "conflict" in lower or "disagree" in lower or "differ" in lower
    # And it must be told NOT to guess — set status=failed instead.
    assert "guess" in lower or "fabricate" in lower or "status=\"failed\"" in lower or "status='failed'" in lower


@pytest.mark.asyncio
async def test_dev_discards_run_when_ticket_reset_mid_run(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """/reset issued while the model works: the finished run must NOT
    push or open an MR — that would resurrect the wiped ticket."""
    from virtual_dev.application.agents.dev import DevSkipReason
    from virtual_dev.application.services.ticket_reset import reset_ticket_state

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _ResetMidRunDev(_TestDev):
        async def _call_model(self, request: CodeAgentRequest) -> tuple[dict[str, Any], CodeAgentResult]:
            async with session_scope(self._session_factory) as session:
                await reset_ticket_state(session, tracker="jira", external_id="DM-7")
            return await super()._call_model(request)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=8, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    cfg = _cfg()
    dev = _ResetMidRunDev(
        agent_key="dev-bellingshausen-backend",
        repo_key="bellingshausen",
        specialisation="backend",
        vcs=vcs,
        code_agent=code_agent,
        rules_loader=RulesLoader(Path("/nonexistent_rules")),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        session_factory=session_factory,
        config=cfg,
        settings=Settings(),
        preset_submission={
            "title": "t", "description": "d", "status": "success", "notes": "",
        },
        edits=["app/users.py"],
    )
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.SKIPPED
    assert result.skip_reason is DevSkipReason.RESET_DURING_RUN
    kinds = [c[0] for c in vcs.calls]
    assert "push" not in kinds
    assert "create_merge_request" not in kinds


@pytest.mark.asyncio
async def test_fresh_plan_run_force_pushes_over_stale_branch(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """handle_plan rebuilds the bot branch from the default branch, so its
    push must be forced: a leftover remote branch from a run that died
    between push and MR creation otherwise rejects every retry forever."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    vcs = _FakeVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=8, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "d", "status": "success"},
        edits=["app/users.py"],
    )
    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.MR_OPENED
    pushes = [args for kind, args in vcs.calls if kind == "push"]
    assert len(pushes) == 1
    assert pushes[0][2] is True  # force


@pytest.mark.asyncio
async def test_transient_fetch_error_does_not_fail_the_task(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A GitLab 503 during branch prep is a network hiccup, not a config
    problem: the task must NOT flip to FAILED (prod incident: one 503
    permanently failed a healthy ticket). Infra error → recovery retries."""
    from virtual_dev.application.agents.dev import CodeAgentInfraError

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _FlakyGitlabVcs(_FakeVcs):
        async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
            raise VcsError(
                "git fetch failed (exit=128): fatal: unable to access "
                "'https://gitlab/x.git/': The requested URL returned error: 503"
            )

    vcs = _FlakyGitlabVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission=None,
    )
    with pytest.raises(CodeAgentInfraError):
        await dev.handle_plan("jira", "DM-7")

    from sqlalchemy import select
    async with session_factory() as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert row.internal_status != TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_push_permission_error_fails_task_on_first_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A 403 on push (bot lacks the Developer role) is permanent: fail
    the task immediately — permanent failures DM the lead via the inbox —
    instead of burning two more model cycles rediscovering the same 403."""
    from virtual_dev.application.agents.dev import CodeAgentPermanentError

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _NoPushVcs(_FakeVcs):
        async def push(self, repo_key: str, branch: str, *, force: bool = False) -> None:
            raise VcsError(
                "git push failed (exit=128): remote: You are not allowed to "
                "push code to this project. fatal: unable to access "
                "'https://gitlab/x.git/': The requested URL returned error: 403"
            )

    vcs = _NoPushVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "d", "status": "success"},
        edits=["app/users.py"],
    )
    with pytest.raises(CodeAgentPermanentError, match="Developer role"):
        await dev.handle_plan("jira", "DM-7")

    from sqlalchemy import select
    async with session_factory() as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert row.internal_status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_transient_push_error_leaves_task_for_retry(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from virtual_dev.application.agents.dev import CodeAgentInfraError

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _FlakyPushVcs(_FakeVcs):
        async def push(self, repo_key: str, branch: str, *, force: bool = False) -> None:
            raise VcsError("git push failed: The requested URL returned error: 502")

    vcs = _FlakyPushVcs(tmp_path / "workspace")
    code_agent = _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "t", "description": "d", "status": "success"},
        edits=["app/users.py"],
    )
    with pytest.raises(CodeAgentInfraError):
        await dev.handle_plan("jira", "DM-7")

    from sqlalchemy import select
    async with session_factory() as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert row.internal_status != TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_concurrent_iterations_on_one_repo_serialize(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Seen live (Jul 31, bellingshausen!1114): DevOps auto-fix and a
    Reviewer-driven iterate fired within a minute of each other and ran
    CONCURRENTLY in the same working copy — interleaved checkouts, one run
    overwriting garbage_collector.py under the other mid-read. A repo has
    exactly one workspace, so all model-driven work on it must serialize."""
    import asyncio

    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    class _TrackingCodeAgent(CodeAgentPort):
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return CodeAgentResult(
                final_text="", turns=1, input_tokens=0, output_tokens=0,
                cost_usd=0.0, stopped_reason="end_turn",
            )

        def stream_task(self, request: CodeAgentRequest) -> Any:  # pragma: no cover
            raise NotImplementedError

    vcs = _FakeVcs(tmp_path / "ws")
    code_agent = _TrackingCodeAgent()
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=code_agent,
        preset_submission={"title": "fix ci", "status": "success"},
    )

    await asyncio.gather(
        dev.handle_iteration(
            tracker="jira", external_id="DM-7",
            branch_name="ai-dev/dm-7", feedback="fix the linter",
        ),
        dev.handle_iteration(
            tracker="jira", external_id="DM-7",
            branch_name="ai-dev/dm-7", feedback="address the review",
        ),
    )

    assert code_agent.max_active == 1, (
        "two iterations ran concurrently in the same workspace"
    )


@pytest.mark.asyncio
async def test_mr_description_blocks_are_russian_and_hide_internal_roles(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Reviewers read the MR description in Russian; the section headers the
    # runtime injects around the model's text must not drop back to English
    # — nor name the internal Dev/Analyst roles the bot presents as one person.
    await _insert_task(session_factory)
    await _insert_plan(session_factory)

    dev = _make_dev(
        session_factory, vcs=_FakeVcs(tmp_path / "ws"),
        code_agent=_FakeCodeAgent(CodeAgentResult(
            final_text="", turns=1, input_tokens=0, output_tokens=0,
            cost_usd=0.0, stopped_reason="end_turn",
        )),
        preset_submission=None,
    )
    dev._config.notifications.merge_request.description = (
        "**Задача:** [{key}]({url})\n\n{description}\n{plan_block}\n{notes_block}"
    )
    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
        plan_row = (await session.execute(
            select(PlanRow).where(PlanRow.task_external_id == "DM-7")
        )).scalar_one()
    plan = row_to_plan(plan_row)

    description = dev._render_mr_description(
        task_row, plan,
        {"description": "Переделала формат тегов.", "notes": "Тесты зелёные."},
    )

    assert "## Plan (from Analyst)" not in description
    assert "## Notes from the Dev agent" not in description
    assert "Analyst" not in description and "Dev agent" not in description
    assert "## План" in description
    assert "## Заметки" in description


def test_dev_prompt_requires_russian_mr_title_and_description() -> None:
    # The root cause of MRs shipping in English: the prompt used to demand it.
    prompt = (Path(__file__).parents[2] / "config" / "prompts" / "dev.md").read_text("utf-8")
    mr_section = prompt.split("## MR submission", 1)[1]

    assert "**in English**" not in mr_section
    assert "description is English too" not in mr_section
    assert "по-русски" in mr_section


def test_dev_prompt_forbids_inventing_mr_settings() -> None:
    # The dev wrote "Target branch: tags-dev" into the description of an
    # MR the runtime had opened into master — a setting it does not
    # control, stated as fact.
    prompt = (Path(__file__).parents[2] / "config" / "prompts" / "dev.md").read_text("utf-8")
    mr_section = prompt.split("## MR submission", 1)[1]

    assert "Не выдумывай настройки MR" in mr_section


class _HistoryVcs(_FakeVcs):
    """The model rewrote history: the new HEAD doesn't descend from the
    old one. Records how the runtime publishes it."""

    def __init__(self, workspace: Path, *, staged_sha: str | None = None) -> None:
        super().__init__(workspace)
        self.staged_sha = staged_sha
        self.moved_to: str | None = None

    def commit_identity(self) -> tuple[str, str] | None:
        return "Аида Нейронова", "virtual-dev@example"

    async def commit_all(self, repo_key: str, message: str) -> str:
        self.calls.append(("commit_all", (repo_key, message)))
        self.head = self.moved_to or "rebuilt0000000"
        return self.head

    async def is_ancestor(self, repo_key: str, ancestor: str, descendant: str = "HEAD") -> bool:
        return False

    async def move_branch_to(self, repo_key: str, sha: str) -> None:
        self.calls.append(("move_branch_to", (repo_key, sha)))
        self.moved_to = sha

    async def remote_branch_sha(self, repo_key: str, branch: str) -> str | None:
        return self.staged_sha if branch.startswith("staged/") else None

    async def push_head_to(self, repo_key: str, branch: str) -> None:
        self.calls.append(("push_head_to", (repo_key, branch)))

    async def push_rewritten(self, repo_key: str, branch: str, *, old_sha: str) -> str:
        self.calls.append(("push_rewritten", (repo_key, branch, old_sha)))
        return f"backup/{branch}-{old_sha[:8]}"

    async def delete_remote_branch(self, repo_key: str, branch: str) -> None:
        self.calls.append(("delete_remote_branch", (repo_key, branch)))


def _ok_agent() -> _FakeCodeAgent:
    return _FakeCodeAgent(CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=0.0, stopped_reason="end_turn",
    ))


@pytest.mark.asyncio
async def test_rewritten_history_is_force_pushed_with_backup_and_identity(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    """«Пересобери ветку» on rainbow!1031 died on a plain non-fast-forward
    push and on commits carrying a colleague's identity. The model's own
    commits get the bot's identity through the env; a rewritten history
    goes out with a lease and a backup of the old head."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _HistoryVcs(tmp_path / "workspace")
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_ok_agent(),
        preset_submission={
            "status": "success", "title": "пересборка", "description": "",
            "thread_note": "Пересобрала ветку: 3 коммита кода и 2 «Тесты: …»",
        },
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7", branch_name="ai-dev/dm-7",
        feedback="пересобери ветку",
    )

    assert result.outcome is DevOutcome.MR_OPENED
    assert result.backup_branch == "backup/ai-dev/dm-7-0ld00000"
    assert result.note.startswith("Пересобрала ветку")
    kinds = [c[0] for c in vcs.calls]
    assert "push_rewritten" in kinds and "push" not in kinds
    assert dev.last_request is not None
    env = dev.last_request.extras["env"]
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "virtual-dev@example"


@pytest.mark.asyncio
async def test_hold_push_parks_the_history_on_a_staged_branch(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    """The team approves a rebuilt history before it replaces the MR
    branch: it waits on staged/<branch>, the MR branch is untouched."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _HistoryVcs(tmp_path / "workspace")
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_ok_agent(),
        preset_submission={
            "status": "success", "title": "пересборка", "description": "",
            "thread_note": "Пересобрала ветку", "hold_push": True,
        },
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7", branch_name="ai-dev/dm-7",
        feedback="пересобери ветку, пуш после моего ок",
    )

    assert result.outcome is DevOutcome.STAGED
    assert result.staged_branch == "staged/ai-dev/dm-7"
    kinds = [c[0] for c in vcs.calls]
    assert ("push_head_to", ("bellingshausen", "staged/ai-dev/dm-7")) in vcs.calls
    assert "push_rewritten" not in kinds and "push" not in kinds


@pytest.mark.asyncio
async def test_approved_staged_history_is_offered_then_pushed_and_cleaned_up(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    """On the next ask the model is told the staged history exists; once
    it's pushed to the MR branch the staged branch goes away."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _HistoryVcs(tmp_path / "workspace", staged_sha="5ta9ed0000000")
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_ok_agent(),
        preset_submission={"status": "success", "title": "push", "description": ""},
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7", branch_name="ai-dev/dm-7", feedback="ок, пушь",
    )

    assert dev.last_request is not None
    assert "origin/staged/ai-dev/dm-7" in dev.last_request.user_prompt
    assert result.outcome is DevOutcome.MR_OPENED
    assert ("delete_remote_branch", ("bellingshausen", "staged/ai-dev/dm-7")) in vcs.calls


@pytest.mark.asyncio
async def test_result_built_in_a_worktree_is_brought_to_the_branch(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    """Recipes in the thread built the result in ~/wt/…; the runtime only
    pushes the workspace branch, so the run read as «no changes» and the
    rebuilt history never reached origin (rainbow!1031, c8c42cb2)."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _HistoryVcs(tmp_path / "workspace")
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_ok_agent(),
        preset_submission={
            "status": "success", "title": "fold", "description": "",
            "result_sha": "c8c42cb20000",
        },
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7", branch_name="ai-dev/dm-7", feedback="влей коммит",
    )

    assert ("move_branch_to", ("bellingshausen", "c8c42cb20000")) in vcs.calls
    assert result.commit_sha == "c8c42cb20000"


@pytest.mark.asyncio
async def test_in_progress_is_visible_while_an_iteration_runs(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    """«В чём затык?» asked mid-run is answered from what actually runs."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    seen: list[object] = []

    class _Peeking(_FakeCodeAgent):
        async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
            seen.append(dev.in_progress("ai-dev/dm-7"))
            return await super().run_task(request)

    dev = _make_dev(
        session_factory, vcs=_FakeVcs(tmp_path / "workspace"),
        code_agent=_Peeking(_ok_agent().result),
        preset_submission={"status": "success", "title": "t", "description": ""},
    )
    await dev.handle_iteration(
        tracker="jira", external_id="DM-7", branch_name="ai-dev/dm-7", feedback="почини тест",
    )

    running = seen[0]
    assert running is not None and getattr(running, "feedback", "") == "почини тест"
    assert dev.in_progress("ai-dev/dm-7") is None
    assert dev.queued("ai-dev/dm-7") == 0


# --- Mandatory LLM-Wiki article (Этап 4) ---


@pytest.mark.asyncio
async def test_dev_injects_mandatory_wiki_article_into_system_prompt(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Обязательная статья Лоции попадает в system prompt первичного запуска —
    независимо от того, решит ли модель вызывать wiki-инструменты."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    wiki = _WikiStub(_MANDATORY_ARTICLE)
    dev = _make_dev(
        session_factory, vcs=_FakeVcs(tmp_path / "workspace"),
        code_agent=_dummy_code_agent(),
        preset_submission={
            "title": "Add users endpoint",
            "description": "Implemented per plan.",
            "status": "success",
        },
        edits=["app/users.py"],
        wiki=wiki,
    )

    result = await dev.handle_plan("jira", "DM-7")

    assert result.outcome is DevOutcome.MR_OPENED
    assert wiki.calls == [MANDATORY_REVIEW_TOPIC]
    request = dev.last_request
    assert request is not None
    assert "<mandatory_wiki_context>" in request.system_prompt
    assert _MANDATORY_ARTICLE.text in request.system_prompt
    # Блок — последняя секция системного промпта.
    assert request.system_prompt.rstrip().endswith("</mandatory_wiki_context>")


@pytest.mark.asyncio
async def test_iteration_injects_mandatory_wiki_article(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Каждая итерация (review/CI) тоже получает свежую статью."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    wiki = _WikiStub(_MANDATORY_ARTICLE)
    dev = _make_dev(
        session_factory, vcs=_FakeVcs(tmp_path / "workspace"),
        code_agent=_dummy_code_agent(),
        preset_submission={
            "status": "success",
            "title": "address review: rename foo",
            "description": "rename foo to bar per reviewer ask",
        },
        edits=["app/users.py"],
        wiki=wiki,
    )

    result = await dev.handle_iteration(
        tracker="jira", external_id="DM-7",
        branch_name="ai-dev/dm-7",
        feedback="please rename foo",
        feedback_kind="ci_failure",
    )

    assert result.outcome is DevOutcome.MR_OPENED
    assert wiki.calls == [MANDATORY_REVIEW_TOPIC]
    request = dev.last_request
    assert request is not None
    assert "<mandatory_wiki_context>" in request.system_prompt
    assert _MANDATORY_ARTICLE.text in request.system_prompt


@pytest.mark.asyncio
async def test_dev_wiki_outage_blocks_initial_run_without_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Wiki недоступна → запуск не начинается: нет клона, нет ветки, нет
    смены статуса, модель не стартует. Ошибка уходит наверх как
    повторяемая — обработчик не сделает ack."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    wiki = _WikiStub(error=WikiUnavailableError("wiki down"))
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_dummy_code_agent(),
        preset_submission=None, wiki=wiki,
    )

    with pytest.raises(WikiUnavailableError):
        await dev.handle_plan("jira", "DM-7")

    assert dev.last_request is None
    kinds = [c[0] for c in vcs.calls]
    assert "ensure_clone" not in kinds
    assert "create_branch" not in kinds
    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-7")
        )).scalar_one()
    assert task_row.internal_status == "ready"


@pytest.mark.asyncio
async def test_dev_wiki_outage_blocks_iteration_before_vcs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Итерация при недоступной Wiki тоже не стартует и не эмитит
    iteration_failed — событие «итерация упала» означает, что модель
    реально работала и упала, а тут она даже не начинала."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    vcs = _FakeVcs(tmp_path / "workspace")
    wiki = _WikiStub(error=WikiUnavailableError("wiki down"))
    trace = _RecordingTrace()
    dev = _make_dev(
        session_factory, vcs=vcs, code_agent=_dummy_code_agent(),
        preset_submission=None, trace=trace, wiki=wiki,
    )

    with pytest.raises(WikiUnavailableError):
        await dev.handle_iteration(
            tracker="jira", external_id="DM-7",
            branch_name="ai-dev/dm-7",
            feedback="please rename foo",
        )

    assert dev.last_request is None
    kinds = [c[0] for c in vcs.calls]
    assert "ensure_clone" not in kinds
    assert "checkout_existing_branch" not in kinds
    assert all(getattr(e, "type", None) != "iteration_failed" for e in trace.events)
