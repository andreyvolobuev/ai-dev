"""Доменные модели для VCS: Merge Request, ревью-комментарии."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MRStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class PipelineStatus(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ReviewComment:
    """Комментарий в MR. Всегда trusted=False (пришёл от человека)."""

    id: str
    mr_id: str
    author_username: str
    body: str
    file_path: str | None = None
    line: int | None = None
    created_at: datetime | None = None
    resolved: bool = False
    # GitLab помечает свои авто-нотки (`added 1 commit`, `left review
    # comments`, marked draft → ready и т.п.) как system=True. Reviewer
    # их не маршрутизирует в LLM — это шум, не запрос от человека.
    system: bool = False


@dataclass
class ApprovalInfo:
    """State of approvals on one MR.

    Flat list of usernames because our reviewer workflow asks simple
    questions: "how many approved", "did `foo` approve". We don't need
    the per-approval metadata (timestamps, etc.).
    """

    approved_by: list[str] = field(default_factory=list)
    required: int = 1

    @property
    def count(self) -> int:
        return len(self.approved_by)


@dataclass
class PipelineJob:
    """One CI job in an MR's latest pipeline (for DevOpsAgent diagnosis)."""

    id: int
    name: str
    stage: str
    status: str               # raw GitLab status ("failed", "success", ...)
    web_url: str
    log_excerpt: str = ""     # trailing N lines of the job log, populated on demand


@dataclass
class MergeRequest:
    """Merge Request / Pull Request."""

    id: str                       # внутренний ID в VCS
    iid: int                      # "номер в проекте", как в GitLab (в URL)
    project_id: str
    title: str
    description: str
    source_branch: str
    target_branch: str
    author_username: str
    web_url: str
    status: MRStatus = MRStatus.OPEN
    approvals_count: int = 0
    approvals_required: int = 1
    pipeline_status: PipelineStatus = PipelineStatus.UNKNOWN
    pipeline_url: str | None = None
    comments: list[ReviewComment] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
