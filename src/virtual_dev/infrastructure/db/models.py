"""ORM models (SQLAlchemy 2.0)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from virtual_dev.infrastructure.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRow(Base):
    """Persistent projection of :class:`virtual_dev.domain.models.task.Task`.

    The domain model is always the source of truth in application code; this
    row exists so the dashboard and the scheduler can see task state across
    restarts and so we have an audit trail.
    """

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("tracker", "external_id", name="uq_tracker_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity
    tracker: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)

    # Payload
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    assignee_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reporter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    components_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    labels_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    priority: Mapped[str] = mapped_column(String(16), default="medium")
    external_status: Mapped[str] = mapped_column(String(64), default="")
    created_at_external: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at_external: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Our view of the task
    internal_status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    target_repo_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dor_satisfied: Mapped[bool] = mapped_column(default=False)

    # Bookkeeping
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Phase 5.0: the analyst's session state per ticket. ``awaiting_*``
    # fields are filled when the analyst dispatches an ASK and is
    # waiting on a human reply; the listener routes reply fragments
    # by ``awaiting_post_id`` / ``awaiting_channel_id``.
    awaiting_post_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    awaiting_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    awaiting_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    awaiting_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    awaiting_dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_fragment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    coalesce_window_seconds: Mapped[int] = mapped_column(Integer, default=60)
    analyst_iteration_count: Mapped[int] = mapped_column(Integer, default=0)
    last_analyst_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    analyst_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MergeRequestRow(Base):
    """Persistent projection of a VCS merge request."""

    __tablename__ = "merge_requests"
    __table_args__ = (
        UniqueConstraint("repo_key", "iid", name="uq_repo_iid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_key: Mapped[str] = mapped_column(String(128), index=True)
    iid: Mapped[int] = mapped_column(Integer, index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    task_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    source_branch: Mapped[str] = mapped_column(String(256))
    target_branch: Mapped[str] = mapped_column(String(256))
    author_username: Mapped[str] = mapped_column(String(128))
    web_url: Mapped[str] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    approvals_count: Mapped[int] = mapped_column(Integer, default=0)
    approvals_required: Mapped[int] = mapped_column(Integer, default=1)
    pipeline_status: Mapped[str] = mapped_column(String(16), default="unknown")
    pipeline_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Review bookkeeping (Phase 3). last_seen_comment_id is the id of the last
    # comment we processed; last_activity_at is the last time anything
    # happened (new comment, approval, pipeline flip) — used by the
    # escalation policy. last_pipeline_notified_status lets DevOps avoid
    # re-posting when a red pipeline reruns and stays red.
    last_seen_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Actionable comments (questions / change-requests) we've classified but
    # not yet managed to *deliver a reply* to. last_seen_comment_id advances
    # past them (so we don't re-classify), so this set is what keeps an
    # unanswered comment alive: every tick we retry each pending comment's
    # thread until the reply lands, then drop it. Prevents "fixed it but
    # never replied, yet marked the comment done".
    pending_comment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pipeline_notified_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_escalation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ping_reviewers_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Whether we've already posted "please review" to the team channel for
    # this MR. Sent once when the MR transitions out of draft.
    review_ping_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When we post the "please review" ping, we remember the MM channel +
    # post id so the thread listener knows which thread roots belong to us
    # and can route replies back to the Reviewer / Dev iteration path.
    review_thread_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_thread_root_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # CI auto-fix state. counter increments after each Dev iteration the
    # DevOps agent dispatches against this MR; resets to 0 on a green
    # pipeline. ``escalated`` is set once when attempts run out and we DM
    # the team-lead, so we don't keep DMing on every subsequent tick.
    pipeline_autofix_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pipeline_autofix_escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # MM post id of the "I gave up after N attempts" DM to the team-lead.
    # A `/restart` reply in this post's thread resets the autofix counter
    # for this MR (the lead nudging the bot to try again).
    autofix_escalation_root_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Set by both MM-driven and autofix iteration paths after a successful
    # push. The Reviewer poll ack-posts to the thread when CI for this sha
    # turns green, then clears the field. Means: "we have an unannounced
    # iteration commit waiting for CI confirmation".
    iteration_pending_ci_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where to post the "✅ CI зелёный" ack: 'mm' for the review thread,
    # 'gitlab' for a top-level MR comment. Set when iteration is
    # triggered, cleared together with iteration_pending_ci_sha.
    iteration_ack_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AgentMessageRow(Base):
    """Persistent inter-agent message (SQLite-backed message bus).

    Lifecycle: ``consumed_at IS NULL AND claimed_until IS NULL`` → free.
    A ``_claim_next`` call sets ``claimed_until = now + lease`` (lease
    reservation, not consumption). Successful handler → ``ack()`` sets
    ``consumed_at``. Crashed handler / process kill → lease expires,
    next poll's reaper resets ``claimed_until = NULL`` and the row
    becomes free again. At-least-once delivery; handlers must be
    idempotent (already true via the UNIQUE constraints on TaskRow,
    MergeRequestRow, AnalystConversationFragmentRow)."""

    __tablename__ = "agent_messages"
    __table_args__ = (
        # Hot path is "next free message for this consumer" — composite
        # index lets SQLite skip rows that are leased / consumed without
        # scanning the whole inbox.
        Index(
            "ix_agent_messages_to_agent_consumed_claimed",
            "to_agent", "consumed_at", "claimed_until",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    from_agent: Mapped[str] = mapped_column(String(128), index=True)
    to_agent: Mapped[str] = mapped_column(String(128), index=True)
    topic: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lease deadline. NULL = unclaimed; future = in flight; past = lease
    # expired (reaper resets to NULL on next poll).
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BusSubscriptionRow(Base):
    """Durable list of agent_keys that have ever subscribed to the bus.

    Survives process restart: a broadcast (``to_agent="*"``) fans out to
    everyone in this table, so an orchestrator can publish before the
    consumer process has come up and the message still lands in the
    right inbox. Re-registered idempotently by every ``subscribe()``
    call. Pruning of long-stale subscribers is operator territory."""

    __tablename__ = "bus_subscriptions"

    agent_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )


class PlanRow(Base):
    """Persistent projection of :class:`virtual_dev.domain.models.plan.Plan`."""

    __tablename__ = "plans"
    __table_args__ = (
        # Hot query: "active plan for (tracker, task_external_id)" —
        # filtered by ``status != SUPERSEDED``. Compound index lets
        # SQLite skip superseded rows for the lookup directly.
        Index(
            "ix_plans_tracker_task_status",
            "tracker", "task_external_id", "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    tracker: Mapped[str] = mapped_column(String(32), index=True)
    task_external_id: Mapped[str] = mapped_column(String(64), index=True)

    summary: Mapped[str] = mapped_column(Text, default="")
    steps_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    open_questions_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    risks_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(default=0.5)

    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    target_repo_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    cost_usd: Mapped[float] = mapped_column(default=0.0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(128), default="")
    agent_key: Mapped[str] = mapped_column(String(128), default="", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class MrHistoryRow(Base):
    """One indexed MR in the RAG corpus.

    Embedding is stored as a raw float32 little-endian blob (cheap,
    no dependency on pickle). Dimensionality is implied by ``embed_dim``
    so we can detect and drop rows if the embedding model changes.
    """

    __tablename__ = "mr_history"
    __table_args__ = (UniqueConstraint("repo_key", "iid", name="uq_mr_history_repo_iid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_key: Mapped[str] = mapped_column(String(128), index=True)
    iid: Mapped[int] = mapped_column(Integer, index=True)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    author_username: Mapped[str] = mapped_column(String(128), default="")
    web_url: Mapped[str] = mapped_column(String(1024), default="")
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    embed_model: Mapped[str] = mapped_column(String(256))
    embed_dim: Mapped[int] = mapped_column(Integer)
    embed_norm: Mapped[float] = mapped_column(Float)  # precomputed L2 norm
    embedding_blob: Mapped[bytes] = mapped_column(LargeBinary)

    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class AnalystConversationStepRow(Base):
    """Append-only conversation log per ticket (Phase 5.0).

    The analyst is now a continuous-reasoning agent — its memory of
    "what I've done on this ticket so far" lives here. Steps are
    PLANNER_DECIDED (run summary), BOT_ASKED (DM dispatched),
    HUMAN_REPLIED (coalesced reply), NOTE / STALE_FRAGMENT.
    """

    __tablename__ = "analyst_conversation_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_analyst_conv_step_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    text: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class AnalystConversationFragmentRow(Base):
    """Raw MM message buffered while the analyst waits on a reply.

    Per-task UNIQUE on ``(task_id, mm_post_id)`` so a duplicate WS
    delivery is a silent no-op but two tickets sharing a DM channel
    can each see the same post.
    """

    __tablename__ = "analyst_conversation_fragments"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "mm_post_id", name="uq_analyst_conv_fragment_post",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    mm_post_id: Mapped[str] = mapped_column(String(64))
    asked_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    # File attachments on the MM post — list of {id, name, url,
    # mime_type, extension, size}. Persisted so the prompt builder
    # can surface them with read_<format>_url hints; without this
    # the analyst sees only the text body and misses screenshots /
    # PDFs the reporter attached as the actual answer.
    files_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    flushed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TicketResetRow(Base):
    """Marker left by the team-lead's ``/reset <TICKET>`` command.

    A run (analyst/dev) that STARTED before ``reset_at`` must discard its
    late writes — without this, a run in flight during the reset would
    re-create plan/MR rows after the wipe and the ticket would not start
    from scratch. Upserted on every reset (one row per ticket, latest
    reset wins); never deleted — writers compare ``reset_at`` against
    their own start time."""

    __tablename__ = "ticket_resets"
    __table_args__ = (
        UniqueConstraint("tracker", "external_id", name="uq_ticket_resets_ticket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracker: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EventRow(Base):
    """Generic audit/event log (anything interesting that happened)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    actor: Mapped[str] = mapped_column(String(128), default="")
    subject: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
