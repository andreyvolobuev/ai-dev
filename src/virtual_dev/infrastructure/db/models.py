"""ORM models (SQLAlchemy 2.0)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
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
