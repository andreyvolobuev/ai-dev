"""Long-running worker: MM WebSocket → ThreadResponder → Dev iteration.

Flow per incoming ``posted`` event:

    1. Drop anything that isn't a threaded reply (no ``root_id``) or that
       we authored ourselves — bot-to-self round-trips would be silly.
    2. Look up the MR whose ``review_thread_root_id`` matches. If none,
       this thread isn't ours, skip.
    3. Check whether the bot already reacted ✅ on the post — idempotency
       marker, prevents double-processing after a reconnect.
    4. Fetch the full thread transcript via ``read_thread`` (gives us
       context for the LLM).
    5. Ask ThreadResponderAgent for a decision.
    6. Dispatch:
         * reply: post the text in the thread.
         * iterate: post acknowledgement, call DevAgent.handle_iteration,
                    post a follow-up ("done, new commit pushed").
         * ignore: no reply, just set ✅.
    7. React ✅ on the source post so we never redo it.

Errors in individual events are logged and swallowed — one bad message
should not stop the listener.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import DevAgent, ResponderAction, ThreadResponderAgent
from virtual_dev.runtime.workers.analyst_inbox import AnalystInbox
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.vcs import VcsPort
from pathlib import Path

from virtual_dev.infrastructure.config import AppConfig, Settings
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan

_PROCESSED_REACTION = "white_check_mark"


@dataclass
class MmListenerStats:
    events_seen: int = 0
    events_routed: int = 0
    replies_posted: int = 0
    iterations_dispatched: int = 0
    catchup_posts_replayed: int = 0
    catchup_runs: int = 0
    errors: int = 0
    subscription_restarts: int = 0


# How far back catch-up will fetch a channel's history when a
# question/MR was created longer ago than this. Keeps the API call
# bounded for very busy channels after a long downtime.
_CATCHUP_MAX_LOOKBACK = timedelta(days=7)


class MmThreadListener:
    """Drives the ChatPort.subscribe() async iterator in the background."""

    def __init__(
        self,
        *,
        chat: ChatPort,
        communicator: CommunicatorService,
        responder: ThreadResponderAgent,
        dev_agents: dict[str, DevAgent],     # repo_key → DevAgent
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        settings: Settings,
        vcs: VcsPort | None = None,
        analyst_inbox: AnalystInbox | None = None,
        subscription_initial_backoff: float = 5.0,
        subscription_max_backoff: float = 300.0,
    ) -> None:
        self._chat = chat
        self._communicator = communicator
        self._responder = responder
        self._dev_agents = dev_agents
        self._session_factory = session_factory
        self._config = config
        self._settings = settings
        self._vcs = vcs
        self._analyst_inbox = analyst_inbox
        self._stop_event = asyncio.Event()
        self._running = False
        # Reconnect cadence after subscribe() crashes. Defaults are
        # 5s..5min exponential. Tests pass tiny values to keep wall-time
        # short. Operationally these match the WS-level backoff in
        # _ServerAuthSSLWebsocket so we don't have two layers fighting.
        self._sub_initial_backoff = subscription_initial_backoff
        self._sub_max_backoff = subscription_max_backoff
        self.stats = MmListenerStats()

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Drive the WS subscription, restart on crash with backoff.

        The underlying Mattermost WebSocket already reconnects internally
        on transient drops (see ``_ServerAuthSSLWebsocket``). But if
        ``subscribe()`` itself raises — e.g. a parse error in
        ``_parse_posted_event``, or the queue task dies — we used to
        ``break`` and the listener stayed dead until the process
        restarted.

        This outer loop re-subscribes with exponential backoff. The
        catch-up worker (separate PollerWorker) covers any messages
        missed during the gap, so we don't lose data even if the
        backoff is several minutes.
        """
        if self._running:
            raise RuntimeError("MmThreadListener already running")
        self._running = True
        self._stop_event.clear()
        logger.info("MmThreadListener started")

        backoff = self._sub_initial_backoff
        max_backoff = self._sub_max_backoff
        try:
            while not self._stop_event.is_set():
                try:
                    await self._consume_subscription()
                    # Clean exit (StopAsyncIteration / stop_event) — fall through.
                    if self._stop_event.is_set():
                        break
                    # Subscription ended without an error AND we weren't asked
                    # to stop: the underlying iterator ran out. Treat as a
                    # transient and retry.
                    logger.warning(
                        "MmThreadListener: subscription ended cleanly; restarting"
                    )
                except Exception:
                    self.stats.errors += 1
                    logger.exception(
                        "MmThreadListener: subscription crashed; restarting in {}s",
                        backoff,
                    )
                self.stats.subscription_restarts += 1

                # Wait before reconnecting; bail early if stop was signalled.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)
        finally:
            self._running = False
            logger.info("MmThreadListener stopped")

    async def _consume_subscription(self) -> None:
        """Single subscribe() lifetime. Returns cleanly when iterator ends
        or stop_event fires; raises on subscription crash so the outer
        loop can backoff + retry.
        """
        subscription = self._chat.subscribe()
        pending = asyncio.create_task(_anext(subscription))
        stopper = asyncio.create_task(self._stop_event.wait())
        try:
            while not self._stop_event.is_set():
                done, _ = await asyncio.wait(
                    {pending, stopper}, return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done:
                    return
                if pending in done:
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    # Other exceptions propagate — outer loop logs + backs off.
                    self.stats.events_seen += 1
                    try:
                        await self._dispatch(event)
                    except Exception:
                        logger.exception("MmThreadListener: dispatch raised")
                        self.stats.errors += 1
                    pending = asyncio.create_task(_anext(subscription))
        finally:
            for task in (pending, stopper):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    # ----------------------------------------------------- catch-up tick

    async def catch_up(self) -> int:
        """Replay missed MM posts via REST.

        For each channel we care about (active clarification questions
        + open MR review threads), pull posts since the channel's
        earliest relevant cursor and replay through ``_dispatch``.
        Idempotency comes for free:

        * Clarification fragments: ``UNIQUE(mm_post_id)`` on
          ``question_fragments`` collapses duplicates.
        * Review thread comments: ``_dispatch`` checks the bot's
          ✅-reaction before doing anything.

        This makes the WebSocket subscription a *latency optimisation*
        rather than a correctness requirement — even if the WS dies for
        an hour, a single catch-up sweep restores state.

        Returns total posts dispatched (mostly for tests / dashboard).
        """
        if self._chat is None:
            return 0
        try:
            cursors = await self._gather_channel_cursors()
        except Exception:
            logger.exception("MmThreadListener: gather catch-up cursors failed")
            return 0
        self.stats.catchup_runs += 1

        total = 0
        for channel_id, since in cursors.items():
            try:
                posts = await self._chat.read_channel_since(
                    channel_id, since=since,
                )
            except Exception:
                logger.exception(
                    "MmThreadListener: catch-up fetch failed for {} since {}",
                    channel_id, since.isoformat(),
                )
                continue
            for post in posts:
                if post.trusted:
                    continue
                try:
                    await self._dispatch(post)
                    total += 1
                except Exception:
                    logger.exception(
                        "MmThreadListener: catch-up dispatch raised for {}",
                        post.id,
                    )
                    self.stats.errors += 1

        if total:
            self.stats.catchup_posts_replayed += total
            logger.info(
                "MmThreadListener: catch-up replayed {} post(s) across {} channel(s)",
                total, len(cursors),
            )
        return total

    async def _gather_channel_cursors(self) -> dict[str, datetime]:
        """For each channel of interest, return the oldest relevant since.

        Earliest cursor across all our active state in that channel.
        Bounded by ``_CATCHUP_MAX_LOOKBACK`` so a long-stale question
        doesn't trigger a multi-month REST pull.
        """
        out: dict[str, datetime] = {}
        now = datetime.now(timezone.utc)
        floor = now - _CATCHUP_MAX_LOOKBACK

        async with self._session_factory() as session:
            q_rows = list((await session.execute(
                select(MergeRequestRow).where(
                    MergeRequestRow.status.in_(["open", "draft"]),
                    MergeRequestRow.review_thread_channel_id.is_not(None),
                )
            )).scalars().all())
            qs = list((await session.execute(
                select(TaskRow).where(
                    TaskRow.awaiting_channel_id.is_not(None),
                )
            )).scalars().all())

        # Active analyst sessions awaiting a reply.
        for q in qs:
            ch = q.awaiting_channel_id
            if not ch:
                continue
            cursor = _aware(q.last_fragment_at or q.discovered_at) or now
            if cursor < floor:
                cursor = floor
            existing = out.get(ch)
            if existing is None or cursor < existing:
                out[ch] = cursor

        # Open MR review threads — channel + thread root id are kept on the row.
        for mr in q_rows:
            ch = mr.review_thread_channel_id
            if not ch:
                continue
            # Use the latest of last_activity_at / created_at; the WS
            # death we're recovering from is at most a few minutes
            # back, so anchoring at last_activity_at narrows the
            # pull window and avoids re-processing weeks of channel
            # chatter on long-lived MRs.
            anchor = _aware(mr.last_activity_at) or _aware(mr.created_at) or now
            if anchor < floor:
                anchor = floor
            existing = out.get(ch)
            if existing is None or anchor < existing:
                out[ch] = anchor

        return out

    # ----------------------------------------------------- main dispatch

    async def _dispatch(self, event: ChatMessage) -> None:
        if event.trusted:
            return

        # Phase 5.0 (analyst-driven): an MM event under a bot-asked
        # post is one fragment of the analyst's pending question. We
        # append it; the coalescer flushes after the idle window and
        # re-runs the analyst with the merged reply in its prompt.
        # No immediate ack — mid-message reactions read as
        # interruption.
        #
        # Routing:
        #   * Thread-reply → match by tasks.awaiting_post_id.
        #   * Plain DM (no thread root) → most-recent task awaiting
        #     this channel+user.
        if self._analyst_inbox is not None:
            task_row: TaskRow | None = None
            if event.thread_root_id:
                task_row = await self._analyst_inbox.find_task_by_thread(
                    event.thread_root_id,
                )
            if task_row is None:
                task_row = await self._analyst_inbox.find_task_by_channel(
                    mm_channel_id=event.channel_id,
                    mm_user_id=event.author_id,
                )
            if task_row is not None:
                await self._handle_task_fragment(task_row, event)
                return

        # Below this point we only act on threaded replies (review thread routing).
        if not event.thread_root_id:
            return

        row = await self._load_mr_by_thread(event.thread_root_id)
        if row is None:
            return

        self.stats.events_routed += 1

        # Idempotency: skip if we already reacted ✅.
        fresh_post = await self._chat.get_post(event.id)
        if fresh_post is not None and _PROCESSED_REACTION in fresh_post.bot_reactions:
            logger.debug(
                "MmThreadListener: skipping already-processed post {}", event.id,
            )
            return

        plan = await self._load_plan(row)
        thread = list(await self._chat.read_thread(event.thread_root_id))
        # Drop anything that comes AFTER the current event in the transcript
        # (shouldn't happen with WS, but read_thread could race) and the
        # current event itself — we pass it separately as "latest".
        transcript = [m for m in thread if m.id != event.id]

        logger.info(
            "MmThreadListener: routing reply on {}!{} from {!r}: {!r}",
            row.repo_key, row.iid, event.author_id, event.text[:160],
        )
        mr_diff = ""
        if self._vcs is not None:
            try:
                mr_diff = await self._vcs.get_mr_diff(row.repo_key, row.iid)
            except Exception:
                logger.exception(
                    "MmThreadListener: get_mr_diff failed for {}!{}",
                    row.repo_key, row.iid,
                )

        decision = await self._responder.decide(
            mr_title=row.title,
            mr_description=row.description or "",
            mr_web_url=row.web_url,
            plan=plan,
            thread=transcript,
            latest_reply=event,
            repo_workspace=self._resolve_repo_workspace(row.repo_key),
            mr_diff=mr_diff,
        )
        logger.info(
            "MmThreadListener: decision={} reasoning={!r}",
            decision.action.value, decision.reasoning,
        )

        channel_id = row.review_thread_channel_id or event.channel_id
        root_id = event.thread_root_id

        if decision.action == ResponderAction.REPLY and decision.reply_text:
            await self._post_reply(channel_id, root_id, decision.reply_text)
            self.stats.replies_posted += 1

        elif decision.action == ResponderAction.ITERATE:
            # Acknowledge immediately so the humans see activity.
            if decision.reply_text:
                await self._post_reply(channel_id, root_id, decision.reply_text)
                self.stats.replies_posted += 1
            await self._run_iteration(row, decision.iteration_feedback, channel_id, root_id)
            self.stats.iterations_dispatched += 1

        # React ✅ — always, including ignore, so we never reprocess.
        try:
            await self._chat.add_reaction(event.id, _PROCESSED_REACTION)
        except Exception:
            logger.warning("MmThreadListener: add_reaction failed for post {}", event.id)

    async def _handle_task_fragment(
        self, task_row: TaskRow, event: ChatMessage,
    ) -> None:
        """Append the incoming MM event as a fragment of this ticket.

        Phase 5.0: don't classify here. The AnswerCoalescer merges
        fragments after the idle window and re-runs the analyst.
        Idempotency:
        * Bot's ✅-reaction acts as a fast no-op for replays.
        * ``analyst_conversation_fragments`` UNIQUE(task_id, mm_post_id)
          makes a duplicate WS-delivery a silent no-op.
        """
        assert self._analyst_inbox is not None

        fresh_post = await self._chat.get_post(event.id)
        if fresh_post is not None and _PROCESSED_REACTION in fresh_post.bot_reactions:
            logger.debug(
                "MmThreadListener: fragment {} already processed", event.id,
            )
            return

        try:
            await self._analyst_inbox.append_fragment(task_row.id, event)
        except Exception:
            logger.exception(
                "MmThreadListener: append_fragment crashed for task {}",
                task_row.id,
            )
            return
        # No ✅-reaction here — that goes on the LAST fragment when
        # the coalescer flushes (see AnalystInbox._coalesce_and_resume).

    async def _run_iteration(
        self,
        row: MergeRequestRow,
        feedback: str,
        channel_id: str,
        root_id: str,
    ) -> None:
        """Run a Dev iteration in response to a thread comment.

        On a successful push we DON'T immediately announce in the thread —
        reviewers don't want to hear "I changed something" until CI has
        actually confirmed the change works. We just set
        ``iteration_pending_ci_sha`` on the MR; Reviewer will see CI go
        green on the next tick and post the ack then.

        We also reset the autofix counter, because user-driven iteration
        is fresh intent that shouldn't inherit prior CI budget.
        """
        templates = self._config.notifications.mattermost
        dev = self._dev_agents.get(row.repo_key)
        if dev is None:
            logger.warning(
                "MmThreadListener: no Dev-agent for repo {!r}; cannot iterate",
                row.repo_key,
            )
            await self._post_reply(channel_id, root_id, templates.thread_reply_no_dev_agent)
            return
        if not row.task_external_id:
            await self._post_reply(channel_id, root_id, templates.thread_reply_no_task)
            return

        tracker = "jira"   # Phase 3.5: single-tracker assumption
        try:
            result = await dev.handle_iteration(
                tracker=tracker,
                external_id=row.task_external_id,
                branch_name=row.source_branch,
                feedback=feedback,
            )
        except Exception:
            logger.exception("MmThreadListener: iteration crashed")
            await self._post_reply(channel_id, root_id, templates.thread_reply_iteration_crashed)
            return

        if result.commit_sha:
            # Silent push. Mark the MR as "iteration pending CI" so the
            # Reviewer poll announces in the thread when CI flips green.
            await self._mark_iteration_pending(
                row.id, sha=result.commit_sha, reset_autofix=True,
            )
            logger.info(
                "MmThreadListener: iteration pushed silently {}!{} sha={}, "
                "thread ack will follow once CI is green",
                row.repo_key, row.iid, result.commit_sha[:12],
            )
        else:
            # Nothing to push → nothing to wait for. Tell the user we
            # didn't change anything; no CI gate involved.
            await self._post_reply(
                channel_id, root_id, templates.thread_reply_iteration_no_changes,
            )

    async def _mark_iteration_pending(
        self, row_id: int, *, sha: str, reset_autofix: bool,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.iteration_pending_ci_sha = sha
            row.iteration_ack_target = "mm"
            if reset_autofix:
                row.pipeline_autofix_attempts = 0
                row.pipeline_autofix_escalated = False

    def _resolve_repo_workspace(self, repo_key: str) -> str | None:
        """Resolve the on-disk workspace for a repo so ThreadResponder's
        Read/Glob/Grep tools actually look at the right code.

        Honours ``repositories.yaml.local_path`` first (re-uses the user's
        existing checkout), falls back to ``settings.workspaces_dir/<key>``.
        """
        repo_cfg = self._config.get_repository(repo_key)
        if repo_cfg is None:
            return None
        if repo_cfg.local_path:
            return str(Path(repo_cfg.local_path).expanduser().resolve())
        return str(Path(self._settings.workspaces_dir).resolve() / repo_key)

    async def _post_reply(self, channel_id: str, root_id: str, text: str) -> None:
        await self._communicator.send_channel(channel_id, text, thread_root_id=root_id)

    async def _load_mr_by_thread(self, thread_root_id: str) -> MergeRequestRow | None:
        async with self._session_factory() as session:
            stmt = select(MergeRequestRow).where(
                MergeRequestRow.review_thread_root_id == thread_root_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _load_plan(self, row: MergeRequestRow):
        if not row.task_external_id:
            return None
        async with self._session_factory() as session:
            stmt = (
                select(PlanRow)
                .where(
                    PlanRow.task_external_id == row.task_external_id,
                    PlanRow.status != PlanStatus.SUPERSEDED.value,
                )
                .order_by(PlanRow.created_at.desc())
                .limit(1)
            )
            plan_row = (await session.execute(stmt)).scalar_one_or_none()
        return row_to_plan(plan_row) if plan_row is not None else None


async def _anext(iterator):
    return await iterator.__anext__()


def _aware(dt: datetime | None) -> datetime | None:
    """Normalise possibly-naive SQLite datetimes to UTC-aware ones.

    Returns None for None input so callers can ``or`` it with a default.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["MmThreadListener", "MmListenerStats"]
