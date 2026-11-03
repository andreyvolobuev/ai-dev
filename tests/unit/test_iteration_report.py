"""Iteration outcomes as the review thread hears them (rainbow!1031)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from virtual_dev.application.agents.dev import DevOutcome, DevResult
from virtual_dev.application.services.gitlab_links import read_linked_notes
from virtual_dev.application.services.iteration_report import (
    MAX_ATTEMPTS,
    iteration_done_ack,
    iteration_reply,
    pending_ack_note,
    run_iteration_with_retry,
)
from virtual_dev.domain.models.merge_request import ReviewComment
from virtual_dev.infrastructure.config import MmTemplatesCfg, RepositoryCfg

_TEMPLATES = MmTemplatesCfg(
    thread_reply_iteration_blocked="Затык: {blocker}. Рассказать подробнее?",
    thread_reply_iteration_staged="{note} Ждёт ок в `{staged_branch}` (`{commit_sha_short}`).",
    thread_reply_iteration_done="✅ {summary} — `{commit_sha_short}`, CI зелёный.",
    thread_reply_iteration_no_changes="без изменений",
    thread_reply_iteration_metadata_only="метаданные",
)


class _Runs:
    def __init__(self, *results: DevResult | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> DevResult:
        self.calls += 1
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_transient_failures_are_retried_until_success() -> None:
    runs = _Runs(
        RuntimeError("cli died"),
        DevResult(outcome=DevOutcome.FAILED, stopped_reason="model-did-not-submit"),
        DevResult(outcome=DevOutcome.MR_OPENED, commit_sha="abc"),
    )

    result = await run_iteration_with_retry(runs)

    assert result.outcome is DevOutcome.MR_OPENED
    assert runs.calls == 3


@pytest.mark.asyncio
async def test_conscious_failure_is_not_retried() -> None:
    runs = _Runs(DevResult(
        outcome=DevOutcome.FAILED, stopped_reason="model-returned-failed",
        blocker="нужен доступ к стенду",
    ))

    result = await run_iteration_with_retry(runs)

    assert runs.calls == 1
    assert iteration_reply(result, _TEMPLATES) == (
        "Затык: нужен доступ к стенду. Рассказать подробнее?"
    )


@pytest.mark.asyncio
async def test_crash_on_every_attempt_becomes_a_blocker_not_a_kick_request() -> None:
    runs = _Runs(*[RuntimeError("boom")] * MAX_ATTEMPTS)

    result = await run_iteration_with_retry(runs)
    reply = iteration_reply(result, _TEMPLATES) or ""

    assert runs.calls == MAX_ATTEMPTS
    assert reply.startswith("Затык: прогон падает с ошибкой RuntimeError")
    assert "пни" not in reply.lower()
    assert "RuntimeError: boom" in result.note


def test_outcomes_are_told_in_the_models_words() -> None:
    staged = DevResult(
        outcome=DevOutcome.STAGED, commit_sha="5ta9ed00000000",
        staged_branch="staged/ai-dev/dm-1", note="Пересобрала ветку: 4 коммита",
    )
    assert iteration_reply(staged, _TEMPLATES) == (
        "Пересобрала ветку: 4 коммита. Ждёт ок в `staged/ai-dev/dm-1` (`5ta9ed000000`)."
    )
    no_changes = DevResult(outcome=DevOutcome.NO_CHANGES, note="Тест уже проверяет 409.")
    assert iteration_reply(no_changes, _TEMPLATES) == "Тест уже проверяет 409."
    assert iteration_reply(DevResult(outcome=DevOutcome.NO_CHANGES), _TEMPLATES) == "без изменений"
    pushed = DevResult(outcome=DevOutcome.MR_OPENED, commit_sha="abc")
    assert iteration_reply(pushed, _TEMPLATES) is None


def test_done_ack_says_what_the_commit_did() -> None:
    rewritten = DevResult(
        outcome=DevOutcome.MR_OPENED, commit_sha="c120ee86e644",
        note="Пересобрала ветку в 7 коммитов", backup_branch="backup/x-5f383ff4",
    )
    note = pending_ack_note(rewritten)

    ack = iteration_done_ack(_TEMPLATES, note=note, sha="c120ee86e64408784c", branch="x")

    assert ack == (
        "✅ Пересобрала ветку в 7 коммитов. Старая история — в `backup/x-5f383ff4` — "
        "`c120ee86e644`, CI зелёный."
    )
    assert iteration_done_ack(_TEMPLATES, note="", sha="abc", branch="x").startswith(
        "✅ Внесла правки"
    )


class _NotesVcs:
    def __init__(self, comments: Sequence[ReviewComment]) -> None:
        self._comments = comments
        self.calls: list[tuple[str, int]] = []

    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[ReviewComment]:
        self.calls.append((repo_key, iid))
        return self._comments


def _repo() -> RepositoryCfg:
    return RepositoryCfg(
        key="rainbow", url="git@gitlab.2gis.ru:sd-data-mining/rainbow.git",
        default_branch="master", primary_language="python",
    )


@pytest.mark.asyncio
async def test_linked_gitlab_note_is_read_with_its_discussion() -> None:
    """«доступен этот коммент? <link>#note_5163279» — the bot answered
    «текст сюда не дошёл, напиши суть». It reads it through the API."""
    vcs = _NotesVcs([
        ReviewComment(
            id="5163279", mr_id="1031", author_username="e.yakovlev",
            body="Тест на 409 не нужен", file_path="rapid/app/x.py", line=12,
            discussion_id="d1",
        ),
        ReviewComment(
            id="5163300", mr_id="1031", author_username="a.nejronova",
            body="Уберу", discussion_id="d1",
        ),
        ReviewComment(id="1", mr_id="1031", author_username="x", body="other", discussion_id="d2"),
    ])
    text = (
        "@ai-dev доступен [этот коммент](https://gitlab.2gis.ru/sd-data-mining/rainbow"
        "/-/merge_requests/1031#note_5163279)? И https://gitlab.2gis.ru/other/repo"
        "/-/merge_requests/5#note_9"
    )

    notes = await read_linked_notes(vcs, [_repo()], [text])  # type: ignore[arg-type]

    assert len(notes) == 1
    url, rendered = notes[0]
    assert url.endswith("#note_5163279")
    assert "(rapid/app/x.py:12)" in rendered
    assert "→ @e.yakovlev: Тест на 409 не нужен" in rendered
    assert "@a.nejronova: Уберу" in rendered
    assert "other" not in rendered
    assert vcs.calls == [("rainbow", 1031)]


@pytest.mark.asyncio
async def test_unknown_note_says_so_instead_of_vanishing() -> None:
    vcs: Any = _NotesVcs([])
    url = "https://gitlab.2gis.ru/sd-data-mining/rainbow/-/merge_requests/1031#note_7"

    notes = await read_linked_notes(vcs, [_repo()], [url])

    assert notes == [(url, "(такого комментария в MR нет — удалён?)")]
