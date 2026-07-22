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
from virtual_dev.application.services.ticket_reset import reset_ticket_state
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.ports.chat import ChannelReadDeniedError, ChatPort
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
    # Counted separately from ``replies_posted``: the bot pushed back
    # on a request via ``propose_alternative`` rather than just
    # answering. Tracking it lets us see how often the bot disagrees.
    alternatives_proposed: int = 0
    catchup_posts_replayed: int = 0
    catchup_runs: int = 0
    errors: int = 0
    subscription_restarts: int = 0


# How far back catch-up will fetch a channel's history when a
# question/MR was created longer ago than this. Keeps the API call
# bounded for very busy channels after a long downtime.
_CATCHUP_MAX_LOOKBACK = timedelta(days=7)

# How far back the catch-up sweep looks in the lead's DM channel for
# missed commands (/reset). Deliberately narrower than the general
# lookback: replaying week-old destructive commands after a long outage
# is worse than asking the lead to repeat one.
_DM_COMMAND_LOOKBACK = timedelta(hours=24)

# How long a channel that answered 403 (read denied) is excluded from
# catch-up sweeps. Permission errors don't heal on their own — retrying
# every tick floods the log with the MM gateway's HTML error page. One
# probe an hour is enough to notice the bot got invited.
_DENIED_CHANNEL_COOLDOWN = timedelta(hours=1)


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
        # Lead DM channel for command replay — resolved lazily, cached
        # only on success (see _lead_dm_channel_id).
        self._lead_dm_channel: str | None = None
        self._lead_dm_resolved = False
        # channel_id → don't sweep again until this time (403 backoff).
        self._denied_channels: dict[str, datetime] = {}
        self._stop_event = asyncio.Event()
        self._running = False
        # Reconnect cadence after subscribe() crashes. Defaults are
        # 5s..5min exponential. Tests pass tiny values to keep wall-time
        # short. Operationally these match the WS-level backoff in
        # _ServerAuthSSLWebsocket so we don't have two layers fighting.
        self._sub_initial_backoff = subscription_initial_backoff
        self._sub_max_backoff = subscription_max_backoff
        # Post ids currently being dispatched. Guards against the WS
        # listener and the catch-up poller processing the same post
        # concurrently (the ✅-reaction marker is only set after the slow
        # responder.decide(), so it can't close that window on its own).
        self._inflight_posts: set[str] = set()
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
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        # Real bugs in subscribe()/cleanup must surface,
                        # not vanish with the cancelled marker.
                        logger.exception(
                            "MmThreadListener: cleanup of inflight task raised",
                        )

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
        now = datetime.now(timezone.utc)
        for channel_id, since in cursors.items():
            denied_until = self._denied_channels.get(channel_id)
            if denied_until is not None:
                if now < denied_until:
                    continue
                del self._denied_channels[channel_id]
            try:
                posts = await self._chat.read_channel_since(
                    channel_id, since=since,
                )
            except ChannelReadDeniedError:
                self._denied_channels[channel_id] = now + _DENIED_CHANNEL_COOLDOWN
                logger.warning(
                    "MmThreadListener: no read access to channel {} (403) — "
                    "skipping catch-up for {}; invite the bot to the channel "
                    "or fix the stale reference (lead DM / review thread)",
                    channel_id, _DENIED_CHANNEL_COOLDOWN,
                )
                continue
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

        # Lead DM commands (/reset). Without this cursor a command sent
        # while the WS was down is lost forever — the lead expects the
        # bot to obey once it's back, so the DM channel must be swept
        # too. Idempotency: _dispatch_inner skips ✅-marked commands.
        lead_dm = await self._lead_dm_channel_id()
        if lead_dm:
            cursor = now - _DM_COMMAND_LOOKBACK
            existing = out.get(lead_dm)
            if existing is None or cursor < existing:
                out[lead_dm] = cursor

        return out

    # ----------------------------------------------------- main dispatch

    async def _dispatch(self, event: ChatMessage) -> None:
        if event.trusted:
            return

        # Idempotency across concurrent delivery paths. The WS listener
        # and the catch-up poller can hand us the SAME post at the same
        # time; the ✅-reaction guard inside _dispatch_inner is only set
        # AFTER the multi-second responder.decide(), so on its own it
        # leaves a window where both dispatches run the LLM and the bot
        # posts two divergent replies to one message. The claim below is
        # synchronous (no await between the membership test and add), so
        # two coroutines on one event loop cannot both win it.
        if event.id and event.id in self._inflight_posts:
            logger.debug(
                "MmThreadListener: post {} already being dispatched, skipping",
                event.id,
            )
            return
        if event.id:
            self._inflight_posts.add(event.id)
        try:
            await self._dispatch_inner(event)
        finally:
            self._inflight_posts.discard(event.id)

    async def _dispatch_inner(self, event: ChatMessage) -> None:
        # Team-lead DM command: `/reset <TICKET>` wipes the bot's stored state
        # for a ticket so it re-processes from scratch (does NOT touch Jira or
        # GitLab). Checked before any routing so it can't be mistaken for a
        # reply to the analyst's pending question.
        if self._is_reset_command(event.text):
            # Idempotency for catch-up replay: a ✅-marked command was
            # already executed (wiping twice re-clears freshly rebuilt
            # state, so this guard matters more here than for replies).
            if event.id and self._chat is not None:
                fresh = await self._chat.get_post(event.id)
                if fresh is not None and _PROCESSED_REACTION in fresh.bot_reactions:
                    return
            await self._handle_reset_command(event)
            return

        # Team-lead command: a reply in the autofix give-up DM thread.
        # `/restart` there resets this MR's auto-fix counter so the bot
        # tries again. Checked first because the escalation root is a
        # specific stored post id — a match is unambiguous.
        if event.thread_root_id:
            escalated = await self._load_mr_by_escalation_thread(event.thread_root_id)
            if escalated is not None:
                # Idempotency for catch-up replay: the lead-DM sweep uses a
                # fixed lookback cursor, so it re-delivers this post every
                # tick. A ✅-marked /restart was already executed — without
                # this guard the bot re-acks and re-resets the counter on
                # every sweep (seen live: dozens of identical acks).
                if event.id and self._chat is not None:
                    fresh = await self._chat.get_post(event.id)
                    if fresh is not None and _PROCESSED_REACTION in fresh.bot_reactions:
                        return
                await self._handle_autofix_restart(escalated, event)
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

        # A post is only marked ✅-processed once we've actually delivered
        # our response. IGNORE has nothing to send, so it counts as
        # delivered; a dropped reply leaves the post unreacted so the
        # catch-up sweep retries it rather than silently swallowing it.
        delivered = True

        if decision.action in (
            ResponderAction.REPLY, ResponderAction.PROPOSE_ALTERNATIVE,
        ) and not decision.reply_text:
            # Model glitch: reply-class decision with no text. Marking the
            # post ✅ would silently ghost the human — leave it unreacted
            # so the catch-up sweep retries the decision.
            logger.warning(
                "MmThreadListener: {} decision without reply_text on post {} — "
                "leaving unprocessed for retry",
                decision.action.value, event.id,
            )
            delivered = False

        elif decision.action == ResponderAction.REPLY and decision.reply_text:
            delivered = await self._post_reply(channel_id, root_id, decision.reply_text)
            if delivered:
                self.stats.replies_posted += 1

        elif (
            decision.action == ResponderAction.PROPOSE_ALTERNATIVE
            and decision.reply_text
        ):
            # Same chat side-effect as REPLY (post the text in the
            # thread) but counted separately so push-back rate is
            # observable. No dev iteration: we're waiting for the
            # reviewer to confirm the alternative before changing code.
            delivered = await self._post_reply(channel_id, root_id, decision.reply_text)
            if delivered:
                self.stats.alternatives_proposed += 1

        elif decision.action == ResponderAction.ITERATE:
            # Acknowledge BEFORE touching code. If we can't even tell the
            # humans we're on it, don't change code we can't announce and
            # don't mark the post processed — leave it for catch-up to
            # retry (mirrors the GitLab reviewer path).
            if decision.reply_text and not await self._post_reply(
                channel_id, root_id, decision.reply_text,
            ):
                return
            if decision.reply_text:
                self.stats.replies_posted += 1
            # Dev sees the full thread (transcript + the triggering reply
            # itself). The responder splits "history" vs "latest" because
            # it's deciding whether to act on the latest message; the dev
            # just needs the conversation as the humans wrote it.
            await self._run_iteration(
                row, decision.iteration_feedback, channel_id, root_id,
                thread=[*transcript, event],
            )
            self.stats.iterations_dispatched += 1

        # React ✅ only when the response was delivered (or nothing needed
        # sending) — a dropped reply stays unreacted so catch-up retries.
        if not delivered:
            return
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
        *,
        thread: list[ChatMessage] | None = None,
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
                thread=thread,
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
                row.pipeline_infra_retries = 0
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

    async def _post_reply(self, channel_id: str, root_id: str, text: str) -> bool:
        """Post into the thread; return whether it was actually delivered
        (False on rate-limit / outside-hours / send error)."""
        outcome = await self._communicator.send_channel(
            channel_id, text, thread_root_id=root_id,
        )
        return outcome.sent

    async def _load_mr_by_thread(self, thread_root_id: str) -> MergeRequestRow | None:
        async with self._session_factory() as session:
            stmt = select(MergeRequestRow).where(
                MergeRequestRow.review_thread_root_id == thread_root_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _load_mr_by_escalation_thread(
        self, thread_root_id: str,
    ) -> MergeRequestRow | None:
        async with self._session_factory() as session:
            stmt = select(MergeRequestRow).where(
                MergeRequestRow.autofix_escalation_root_id == thread_root_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _is_restart_command(text: str) -> bool:
        """True when the message is the `/restart` command — first token,
        case-insensitive. A literal command, not NL classification."""
        tokens = (text or "").strip().lower().split()
        return bool(tokens) and tokens[0] == "/restart"

    @staticmethod
    def _is_reset_command(text: str) -> bool:
        """True when the message starts with the `/reset` command — first
        token, case-insensitive. A literal command, not NL classification."""
        tokens = (text or "").strip().lower().split()
        return bool(tokens) and tokens[0] == "/reset"

    async def _lead_dm_channel_id(self) -> str | None:
        """DM channel between bot and team-lead, cached per process.

        Failures are NOT cached — the next catch-up tick retries, so a
        transient MM error at startup doesn't disable command replay for
        the whole process lifetime."""
        if self._lead_dm_resolved:
            return self._lead_dm_channel
        if self._chat is None:
            return None
        try:
            lead_id = await self._resolve_lead_user_id()
            channel = (
                await self._chat.direct_channel_id(lead_id) if lead_id else None
            )
        except Exception:
            logger.warning("MmThreadListener: resolving lead DM channel failed")
            return None
        self._lead_dm_channel = channel
        self._lead_dm_resolved = True
        return channel

    async def _resolve_lead_user_id(self) -> str | None:
        """Mattermost user id of the configured team-lead (ESCALATION_USER),
        or None when unset / unresolvable. Mirrors the reviewer/devops path."""
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    async def _handle_reset_command(self, event: ChatMessage) -> None:
        """`/reset <TICKET>` from the team-lead: wipe the bot's DB state for
        the ticket so it starts fresh. Restricted to the team-lead because it
        is destructive. Jira/GitLab are untouched."""
        lead_id = await self._resolve_lead_user_id()
        if lead_id is None or event.author_id != lead_id:
            logger.warning(
                "MmThreadListener: /reset from non-lead {!r} ignored",
                event.author_id,
            )
            await self._reply_in_dm(event, "Команда /reset доступна только тимлиду.")
            return

        tokens = (event.text or "").strip().split()
        flags = {t.lower() for t in tokens[2:]}
        if len(tokens) < 2 or tokens[1].startswith("--"):
            await self._reply_in_dm(
                event, "Формат: `/reset DM-1234` (опционально `--with-mr` — "
                       "закрыть открытый MR бота и удалить его ветку в GitLab)",
            )
            return
        ticket = tokens[1].strip().upper()
        with_mr = "--with-mr" in flags

        # Snapshot the bot's open MRs BEFORE the wipe — the reset deletes
        # the projections we need (repo_key/iid/branch) to close them.
        open_mrs: list[tuple[str, int, str]] = []
        if with_mr:
            async with self._session_factory() as session:
                rows = (await session.execute(
                    select(MergeRequestRow).where(
                        MergeRequestRow.task_external_id == ticket,
                        MergeRequestRow.status.in_(["open", "draft"]),
                    )
                )).scalars().all()
                open_mrs = [
                    (r.repo_key, r.iid, r.source_branch or "") for r in rows
                ]

        async with session_scope(self._session_factory) as session:
            summary = await reset_ticket_state(
                session, tracker="jira", external_id=ticket,
            )

        if not summary.found:
            await self._reply_in_dm(
                event, f"Тикет {ticket} в базе не найден — чистить нечего.",
            )
            return

        mr_report = ""
        if with_mr:
            mr_report = await self._close_reset_mrs(ticket, open_mrs)

        logger.info(
            "MmThreadListener: /reset by lead cleared {} row(s) for {} "
            "(task={}, plans={}, mrs={}, conv={}, bus={})",
            summary.total, ticket, summary.tasks, summary.plans,
            summary.merge_requests,
            summary.conversation_steps + summary.conversation_fragments,
            summary.bus_messages,
        )
        await self._reply_in_dm(
            event,
            f"Готово, очистила состояние {ticket}: задача {summary.tasks}, "
            f"план(ы) {summary.plans}, MR {summary.merge_requests}, "
            f"память диалога "
            f"{summary.conversation_steps + summary.conversation_fragments}."
            f"{mr_report} "
            f"Возьму заново, когда тикет вернётся в «To Do».",
        )

    async def _close_reset_mrs(
        self, ticket: str, open_mrs: list[tuple[str, int, str]],
    ) -> str:
        """Close the bot's open MRs (and delete their branches) in GitLab
        after a `--with-mr` reset. Best-effort per MR; returns a report
        fragment for the DM reply."""
        if self._vcs is None:
            return " GitLab не подключён — MR/ветки не тронула."
        if not open_mrs:
            return " Открытых MR в GitLab не было."
        closed: list[str] = []
        failed: list[str] = []
        for repo_key, iid, branch in open_mrs:
            try:
                await self._vcs.close_merge_request(repo_key, iid)
                if branch:
                    await self._vcs.delete_remote_branch(repo_key, branch)
                closed.append(f"{repo_key}!{iid}")
            except Exception:
                logger.exception(
                    "MmThreadListener: /reset --with-mr failed to close {}!{} for {}",
                    repo_key, iid, ticket,
                )
                failed.append(f"{repo_key}!{iid}")
        parts = []
        if closed:
            parts.append(f"Закрыла MR и удалила ветки: {', '.join(closed)}.")
        if failed:
            parts.append(
                f"Не смогла закрыть: {', '.join(failed)} — посмотри руками."
            )
        return " " + " ".join(parts)

    async def _reply_in_dm(self, event: ChatMessage, text: str) -> None:
        """Reply to a DM command in-thread and mark it processed."""
        await self._post_reply(
            event.channel_id, event.thread_root_id or event.id, text,
        )
        try:
            await self._chat.add_reaction(event.id, _PROCESSED_REACTION)
        except Exception:
            logger.warning(
                "MmThreadListener: add_reaction failed for command post {}",
                event.id,
            )

    async def _handle_autofix_restart(
        self, row: MergeRequestRow, event: ChatMessage,
    ) -> None:
        """The team-lead replied in the give-up DM thread. On `/restart`,
        reset this MR's auto-fix counter so DevOps retries; ack and mark
        the command processed. Other chatter in the thread is ignored."""
        if not self._is_restart_command(event.text):
            return
        async with session_scope(self._session_factory) as session:
            fresh = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row.id)
            )).scalar_one_or_none()
            if fresh is None:
                return
            fresh.pipeline_autofix_attempts = 0
            fresh.pipeline_infra_retries = 0
            fresh.pipeline_autofix_escalated = False
        logger.info(
            "MmThreadListener: /restart reset autofix counter for {}!{}",
            row.repo_key, row.iid,
        )
        ack = (
            self._config.notifications.mattermost.pipeline_autofix_restart_ack
            or "Ок, сбросила счётчик попыток — пробую починить заново."
        )
        await self._post_reply(event.channel_id, event.thread_root_id or event.id, ack)
        try:
            await self._chat.add_reaction(event.id, _PROCESSED_REACTION)
        except Exception:
            logger.warning(
                "MmThreadListener: add_reaction failed for /restart post {}", event.id,
            )

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
