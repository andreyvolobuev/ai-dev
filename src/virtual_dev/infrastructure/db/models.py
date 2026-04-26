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


class GoalRow(Base):
    """A clarification goal: the information the bot needs to learn.

    Phase 3.9 replaces the Q-tree with a goal-driven model. The
    planner agent reads the goal description + full step history on
    each tick and decides one next action. The state machine is a
    direct projection of the
    :class:`virtual_dev.domain.models.clarification_goal.GoalState`
    enum.
    """

    __tablename__ = "clarification_goals"
    __table_args__ = (
        Index("ix_goals_state_lastfrag", "state", "last_fragment_at"),
        Index("ix_goals_state_nextrun", "state", "next_planner_run_at"),
        Index("ix_goals_state_deadline", "state", "deadline_at"),
        Index("ix_goals_tracker_extid", "tracker", "task_external_id"),
        Index("ix_goals_parent_state", "parent_goal_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tracker: Mapped[str] = mapped_column(String(32))
    task_external_id: Mapped[str] = mapped_column(String(64))

    # Sub-goal linkage. NULL on top-level goals; non-NULL when this
    # goal was spawned by a parent's planner via SPAWN_SUBGOALS.
    parent_goal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    description: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text, default="")
    initial_contact_hint: Mapped[str] = mapped_column(String(256), default="")

    state: Mapped[str] = mapped_column(String(32), default="pending")
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Currently-outstanding DM (set when state is in
    # AWAITING_REPLY|COALESCING|READY_TO_REPLAN|REPLANNING).
    current_target_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_target_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_asked_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    current_asked_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_fragment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    coalesce_window_seconds: Mapped[int] = mapped_column(Integer, default=600)

    asked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_planner_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    planner_calls_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_planning_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GoalStepRow(Base):
    """Append-only history entry for a goal.

    ``seq`` is monotonic per-goal; ``UNIQUE(goal_id, seq)`` guarantees
    we never get two steps with the same ordinal even under racing
    inserts (we serialise inside the orchestrator, but the constraint
    is the safety net).
    """

    __tablename__ = "goal_steps"
    __table_args__ = (UniqueConstraint("goal_id", "seq", name="uq_goal_step_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    text: Mapped[str] = mapped_column(Text, default="")
    target_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class GoalFragmentRow(Base):
    """Raw MM message buffered for the goal's CURRENT outstanding question.

    ``UNIQUE(goal_id, mm_post_id)`` (not global) — two goals waiting
    in the same DM channel can legitimately see the same human reply
    if the human accidentally hits both threads, and we want each
    goal's coalescer to see it independently.

    On a new ASK from the planner the buffered fragments either
    coalesce into the previous question's HUMAN_REPLIED step or are
    archived as STALE_FRAGMENT (when the new ask invalidates them as
    contextual evidence).
    """

    __tablename__ = "goal_fragments"
    __table_args__ = (UniqueConstraint("goal_id", "mm_post_id", name="uq_goal_fragment_post"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[int] = mapped_column(Integer, index=True)
    mm_post_id: Mapped[str] = mapped_column(String(64))
    asked_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    flushed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


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
