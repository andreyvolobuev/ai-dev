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
    created_at_external: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at_external: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Our view of the task
    internal_status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    target_repo_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dor_satisfied: Mapped[bool] = mapped_column(default=False)

    # Bookkeeping
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


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
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_pipeline_notified_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_escalation_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ping_reviewers_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    # Set by both MM-driven and autofix iteration paths after a successful
    # push. The Reviewer poll ack-posts to the thread when CI for this sha
    # turns green, then clears the field. Means: "we have an unannounced
    # iteration commit waiting for CI confirmation".
    iteration_pending_ci_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where to post the "✅ CI зелёный" ack: 'mm' for the review thread,
    # 'gitlab' for a top-level MR comment. Set when iteration is
    # triggered, cleared together with iteration_pending_ci_sha.
    iteration_ack_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class AgentMessageRow(Base):
    """Persistent inter-agent message (SQLite-backed message bus)."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    from_agent: Mapped[str] = mapped_column(String(128), index=True)
    to_agent: Mapped[str] = mapped_column(String(128), index=True)
    topic: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlanRow(Base):
    """Persistent projection of :class:`virtual_dev.domain.models.plan.Plan`."""

    __tablename__ = "plans"

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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


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
    merged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    embed_model: Mapped[str] = mapped_column(String(256))
    embed_dim: Mapped[int] = mapped_column(Integer)
    embed_norm: Mapped[float] = mapped_column(Float)   # precomputed L2 norm
    embedding_blob: Mapped[bytes] = mapped_column(LargeBinary)

    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class QuestionRow(Base):
    """One node in the clarification Q-tree.

    Phase 3.8 replaces the flat ``clarifications`` table with a tree
    that supports redirects (a→b→c…), counter-questions (where we owe
    the asker a reply), and "ask back for the missing handle" loops.
    The application source of truth is :class:`virtual_dev.domain.
    models.clarification.Question`; this row is its projection.
    """

    __tablename__ = "questions"
    __table_args__ = (Index("ix_questions_state_lastfrag", "state", "last_fragment_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Tree shape. ``root_id == id`` for roots. ``chain_depth`` is the
    # redirect depth (root=0); used by the loop-guard.
    root_id: Mapped[int] = mapped_column(Integer, index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    chain_depth: Mapped[int] = mapped_column(Integer, default=0)

    tracker: Mapped[str] = mapped_column(String(32), index=True)
    task_external_id: Mapped[str] = mapped_column(String(64), index=True)
    # Only populated on the root. Children leave it null — they belong
    # to the same plan via the root.
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    text: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text, default="")

    state: Mapped[str] = mapped_column(String(32), index=True, default="pending")

    stakeholder_kind: Mapped[str] = mapped_column(String(32), default="explicit_handle")
    stakeholder_raw_hint: Mapped[str] = mapped_column(String(256), default="")
    stakeholder_resolved_mm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stakeholder_resolved_mm_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stakeholder_display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # MM bookkeeping. ``asked_post_id`` is the bot's question DM —
    # used as the thread root for incoming replies and for posting acks.
    mm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mm_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asked_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Coalescer hot-path: refreshed on every incoming fragment.
    last_fragment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    coalesce_window_seconds: Mapped[int] = mapped_column(Integer, default=600)

    asked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuestionFragmentRow(Base):
    """Raw MM message buffered for coalescing.

    ``mm_post_id`` is UNIQUE so duplicate WebSocket deliveries collapse
    cleanly; this is what lets us safely append on every event without
    worrying about double-counting after a reconnect.
    """

    __tablename__ = "question_fragments"
    __table_args__ = (UniqueConstraint("mm_post_id", name="uq_fragment_post_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    mm_post_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    flushed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class QuestionAnswerRow(Base):
    """Final classified answer for one Question.

    Separate from ``questions`` because (a) the row only exists once
    classification has run, (b) it's the audit trail of what the LLM
    decided. ``extracted_json`` carries the structured payload from
    ``submit_classification`` — the orchestrator reads from it when
    spawning child questions, posting acks, etc.
    """

    __tablename__ = "question_answers"
    __table_args__ = (UniqueConstraint("question_id", name="uq_question_answer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    coalesced_text: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(32), index=True)
    extracted_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    classified_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
