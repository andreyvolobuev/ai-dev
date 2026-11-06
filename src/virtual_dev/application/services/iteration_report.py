"""Run a review iteration and tell the thread what came of it.

Shared by the MM review-thread listener and the GitLab review path.

Two lessons from rainbow!1031:

* A failed run used to end with «упёрлась в проблему на своей стороне.
  Пните меня здесь ещё раз» — six times in one afternoon. Nobody should
  have to kick the bot: transient failures are retried here, and what
  still fails is reported as a one-line blocker with an offer to explain
  («Затык: … Рассказать подробнее?»). The details are kept on the MR row
  so the answer to «да» is real, not reconstructed.
* Every outcome had one fixed template, so a rebuilt history was
  announced as «код уже соответствует замечанию» or «поправила описание
  MR». The model's own ``thread_note`` says what was actually done.
"""

from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.ports.professor_wiki import WikiUnavailableError
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope

if TYPE_CHECKING:
    from virtual_dev.application.agents.dev import DevResult, IterationInProgress

# Failures a fresh run can get past on its own: the run broke off, the
# CLI crashed, or someone pushed to the branch meanwhile (the next run
# starts from the new origin head). A model that consciously answered
# status=failed is not retried — it said what it needs.
_RETRYABLE_REASONS = frozenset({"model-did-not-submit", "push-rejected", "crashed"})
MAX_ATTEMPTS = 3

_DETAIL_MAX_CHARS = 4_000


async def run_iteration_with_retry(
    run: Callable[[], Awaitable[DevResult]],
) -> DevResult:
    """``run()`` until it succeeds or fails for a non-transient reason,
    at most :data:`MAX_ATTEMPTS` times. Never raises except
    :class:`WikiUnavailableError`: a crash on the last attempt comes back
    as a FAILED result with the traceback as detail."""
    from virtual_dev.application.agents.dev import DevOutcome, DevResult

    result: DevResult | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await run()
        except WikiUnavailableError:
            # LLM-Wiki outage is retryable infra, not a model/push crash:
            # never convert it into a FAILED result (that would mark the
            # comment handled and lose the work). The coordinating handler
            # releases the claim and retries after the wiki recovers.
            raise
        except Exception as exc:
            logger.exception("Iteration attempt {}/{} crashed", attempt, MAX_ATTEMPTS)
            result = DevResult(
                outcome=DevOutcome.FAILED,
                stopped_reason="crashed",
                blocker=f"прогон падает с ошибкой {type(exc).__name__}",
                note=traceback.format_exc()[-_DETAIL_MAX_CHARS:],
            )
        if result.outcome is not DevOutcome.FAILED:
            return result
        if result.stopped_reason not in _RETRYABLE_REASONS:
            return result
        logger.warning(
            "Iteration attempt {}/{} failed ({}): {}",
            attempt, MAX_ATTEMPTS, result.stopped_reason, result.blocker,
        )
    assert result is not None
    if MAX_ATTEMPTS > 1:
        result.blocker = f"{result.blocker} (пробовала {MAX_ATTEMPTS} раза)"
    return result


def iteration_reply(result: DevResult, templates: Any) -> str | None:
    """The thread message for an iteration that finished without a pushed
    commit, or None when a commit was pushed (its ack waits for CI) or
    there's nothing to say (a run discarded by /reset)."""
    from virtual_dev.application.agents.dev import DevOutcome

    if result.outcome is DevOutcome.FAILED:
        return _format(
            templates.thread_reply_iteration_blocked,
            "Затык: {blocker}. Рассказать подробнее?",
            blocker=result.blocker or "не поняла, что пошло не так",
        )
    if result.outcome is DevOutcome.STAGED:
        return _format(
            templates.thread_reply_iteration_staged,
            "{note} Собрала в `{staged_branch}` (`{commit_sha_short}`), ветку MR "
            "не трогала — запушу после твоего ок.",
            note=_sentence(result.note),
            staged_branch=result.staged_branch,
            commit_sha_short=(result.commit_sha or "")[:12],
        )
    if result.outcome is DevOutcome.METADATA_UPDATED:
        return result.note or templates.thread_reply_iteration_metadata_only
    if result.outcome is DevOutcome.NO_CHANGES:
        return result.note or templates.thread_reply_iteration_no_changes
    return None


def pending_ack_note(result: DevResult) -> str:
    """What the CI-green ack will say the pushed commit did."""
    note = _sentence(result.note)
    if result.backup_branch:
        note = f"{note} Старая история — в `{result.backup_branch}`.".strip()
    return note


def iteration_done_ack(templates: Any, *, note: str, sha: str, branch: str) -> str:
    return _format(
        templates.thread_reply_iteration_done,
        "✅ {summary} — `{commit_sha_short}`, CI зелёный. Можно смотреть 🙏",
        summary=(note or "Внесла правки").rstrip("."),
        commit_sha_short=sha[:12],
        branch=branch,
    )


async def record_iteration(
    session_factory: async_sessionmaker[AsyncSession],
    mr_row_id: int,
    result: DevResult,
    *,
    posted: str | None,
) -> None:
    """Remember what the last iteration did on the MR row."""
    from virtual_dev.application.agents.dev import DevOutcome

    if result.outcome is DevOutcome.SKIPPED:
        return
    if result.outcome is DevOutcome.FAILED:
        summary = f"не вышло: {result.blocker}"
        detail = result.note or result.stopped_reason
    elif result.outcome is DevOutcome.MR_OPENED:
        summary = (
            f"запушила `{(result.commit_sha or '')[:12]}`: "
            f"{pending_ack_note(result) or 'правки'}"
        )
        detail = ""
    else:
        summary = posted or result.note
        detail = ""
    try:
        async with session_scope(session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == mr_row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.last_iteration_outcome = result.outcome.value
            row.last_iteration_summary = summary
            row.last_iteration_detail = (detail or "")[:_DETAIL_MAX_CHARS] or None
            row.last_iteration_at = datetime.now(timezone.utc)
    except Exception:
        logger.exception("record_iteration failed for MR row {}", mr_row_id)


def render_work_state(
    row: MergeRequestRow,
    *,
    in_progress: IterationInProgress | None,
    queued: int,
    staged_sha: str | None,
    timezone_name: str,
) -> str:
    """Facts for the responder about the bot's own work on this MR: what
    runs now, what the last run did and why it failed. Without them a
    question like «в чём затык?» got a story assembled from the bot's
    earlier chat lines, and «какие коммиты получились?» got none."""
    from virtual_dev.application.agents.dev import staged_branch_for
    from virtual_dev.application.services.transcript import local_time

    lines: list[str] = []
    if in_progress is not None:
        lines.append(
            f"- Сейчас идёт правка (с {local_time(in_progress.started_at, timezone_name)}): "
            f"{_one_line(in_progress.feedback, 300)}"
        )
    if queued:
        lines.append(f"- В очереди за ней ещё запросов: {queued}.")
    if row.last_iteration_at is not None and row.last_iteration_summary:
        lines.append(
            f"- Последняя правка ({local_time(row.last_iteration_at, timezone_name)}): "
            f"{row.last_iteration_summary}"
        )
        if row.last_iteration_detail:
            lines.append(
                "- Подробности затыка (отдай их, когда спросят, своими словами: "
                "где именно застряла и что нужно, чтобы продвинуться):\n"
                f"```\n{row.last_iteration_detail[-_DETAIL_MAX_CHARS:]}\n```"
            )
    if row.iteration_pending_ci_sha:
        lines.append(
            f"- Запушила `{row.iteration_pending_ci_sha[:12]}`, жду CI — "
            "отчитаюсь в треде, когда он позеленеет."
        )
    if staged_sha:
        lines.append(
            f"- Переписанная история ждёт ок: `{staged_branch_for(row.source_branch)}` "
            f"(`{staged_sha[:12]}`), ветка MR не тронута. «Ок» на неё — это iterate: "
            "запушить её в ветку MR."
        )
    return "\n".join(lines)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def _format(template: str, fallback: str, **values: str) -> str:
    try:
        return (template or fallback).format(**values).strip()
    except (KeyError, IndexError, ValueError):
        return fallback.format(**values).strip()


__all__ = [
    "MAX_ATTEMPTS",
    "iteration_done_ack",
    "iteration_reply",
    "pending_ack_note",
    "record_iteration",
    "render_work_state",
    "run_iteration_with_retry",
]
