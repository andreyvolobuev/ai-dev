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

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import DevAgent, ResponderAction, ThreadResponderAgent
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.ports.chat import ChatPort
from pathlib import Path

from virtual_dev.infrastructure.config import AppConfig, Settings
from virtual_dev.infrastructure.db import MergeRequestRow, PlanRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import row_to_plan

_PROCESSED_REACTION = "white_check_mark"


@dataclass
class MmListenerStats:
    events_seen: int = 0
    events_routed: int = 0
    replies_posted: int = 0
    iterations_dispatched: int = 0
    errors: int = 0


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
    ) -> None:
        self._chat = chat
        self._communicator = communicator
        self._responder = responder
        self._dev_agents = dev_agents
        self._session_factory = session_factory
        self._config = config
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._running = False
        self.stats = MmListenerStats()

    @property
    def is_running(self) -> bool:
        return self._running

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if self._running:
            raise RuntimeError("MmThreadListener already running")
        self._running = True
        self._stop_event.clear()
        logger.info("MmThreadListener started")
        try:
            subscription = self._chat.subscribe()
            pending = asyncio.create_task(_anext(subscription))
            stopper = asyncio.create_task(self._stop_event.wait())
            try:
                while not self._stop_event.is_set():
                    done, _ = await asyncio.wait(
                        {pending, stopper}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopper in done:
                        break
                    if pending in done:
                        try:
                            event = pending.result()
                        except StopAsyncIteration:
                            break
                        except Exception:
                            logger.exception("MmThreadListener: subscribe raised")
                            self.stats.errors += 1
                            break
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
        finally:
            self._running = False
            logger.info("MmThreadListener stopped")

    async def _dispatch(self, event: ChatMessage) -> None:
        # Filters: must be a threaded reply, must not be ours.
        if not event.thread_root_id:
            return
        if event.trusted:
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
        decision = await self._responder.decide(
            mr_title=row.title,
            mr_description=row.description or "",
            mr_web_url=row.web_url,
            plan=plan,
            thread=transcript,
            latest_reply=event,
            repo_workspace=self._resolve_repo_workspace(row.repo_key),
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

    async def _run_iteration(
        self,
        row: MergeRequestRow,
        feedback: str,
        channel_id: str,
        root_id: str,
    ) -> None:
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
            try:
                done_text = templates.thread_reply_iteration_done.format(
                    commit_sha_short=result.commit_sha[:12],
                    branch=row.source_branch,
                )
            except (KeyError, IndexError):
                done_text = templates.thread_reply_iteration_done
            await self._post_reply(channel_id, root_id, done_text)
        else:
            await self._post_reply(
                channel_id, root_id, templates.thread_reply_iteration_no_changes,
            )

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


__all__ = ["MmThreadListener", "MmListenerStats"]
