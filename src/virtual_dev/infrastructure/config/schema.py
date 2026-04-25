"""Pydantic schemas for YAML config files.

Kept separate from :mod:`virtual_dev.infrastructure.config.settings` (env-based)
because YAML describes *what we work on* (repos, agents, mappings) while env
holds *how we connect to them* (URLs, tokens).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --- repositories.yaml ---


class RepoAgentsCfg(_StrictModel):
    backend: bool = False
    frontend: bool = False
    devops: bool = False


class RepositoryCfg(_StrictModel):
    key: str
    url: str
    description: str = ""
    local_path: str | None = None
    default_branch: str = "main"
    jira_components: list[str] = Field(default_factory=list)
    agents: RepoAgentsCfg = Field(default_factory=RepoAgentsCfg)
    primary_language: str | None = None
    frontend_stack: str | None = None
    tests_cmd: str | None = None
    lint_cmd: str | None = None
    ci_provider: str = "gitlab_ci"


class RepositoriesCfg(_StrictModel):
    repositories: list[RepositoryCfg] = Field(default_factory=list)


# --- agents.yaml ---


class ModelsCfg(_StrictModel):
    default: str = "claude-sonnet-4-5"
    lightweight: str = "claude-haiku-4-5"


class TaskSourceCfg(_StrictModel):
    jql: str
    poll_interval_seconds: int = 120


class JiraTransitionsCfg(_StrictModel):
    to_in_progress: str = "In Progress"
    to_review: str = "Review"
    to_testing: str = "Testing"
    to_done: str = "Done"


class WorkingHoursCfg(_StrictModel):
    timezone: str = "Europe/Moscow"
    start_hour: int = 10
    end_hour: int = 20
    weekdays_only: bool = True


class AgentCfg(_StrictModel):
    model: str = "default"                  # reference into ModelsCfg
    # Cycle guard, NOT a billing cap. Claude Max has no per-task budget —
    # this is only to stop runaway agent loops.
    max_iterations_per_task: int | None = None
    rate_limit_per_hour: int | None = None  # used by Communicator in Phase 3


class ReviewPolicyCfg(_StrictModel):
    required_approvals: int = 1
    ping_reviewers_after_hours: int = 4
    escalate_after_hours: int = 24


class EscalationCfg(_StrictModel):
    mattermost_user: str = ""


# See config/notifications.yaml for the canonical defaults + placeholder
# documentation. The schema here just enumerates which keys must be
# present.
class MmTemplatesCfg(_StrictModel):
    review_ping: str = ""
    merge_ping: str = ""
    stale_ping: str = ""
    escalation_dm: str = ""
    pipeline_failed_short: str = ""
    pipeline_failed_full: str = ""
    thread_reply_no_dev_agent: str = ""
    thread_reply_no_task: str = ""
    thread_reply_iteration_crashed: str = ""
    thread_reply_iteration_done: str = ""
    thread_reply_iteration_no_changes: str = ""


class JiraTemplatesCfg(_StrictModel):
    plan_comment: str = ""
    mr_link_comment: str = ""
    failure_comment: str = ""


class MrTemplatesCfg(_StrictModel):
    title: str = "{key}: {title}"
    commit_message: str = "[{key}] {title}"
    description: str = ""


class NotificationsCfg(_StrictModel):
    """All bot-authored, human-facing message templates.

    LLM system prompts are NOT here — they live in code with the agent
    that owns them. Anything templated (str.format) and shipped to a
    human via MM / Jira / GitLab MR fields is in this config.
    """

    mattermost: MmTemplatesCfg = Field(default_factory=MmTemplatesCfg)
    jira: JiraTemplatesCfg = Field(default_factory=JiraTemplatesCfg)
    merge_request: MrTemplatesCfg = Field(default_factory=MrTemplatesCfg)


class AgentsCfg(_StrictModel):
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    task_source: TaskSourceCfg = Field(
        default_factory=lambda: TaskSourceCfg(
            jql='assignee = currentUser() AND labels = "ai-dev" AND status = "To Do"'
        )
    )
    jira_transitions: JiraTransitionsCfg = Field(default_factory=JiraTransitionsCfg)
    working_hours: WorkingHoursCfg = Field(default_factory=WorkingHoursCfg)
    agents: dict[str, AgentCfg] = Field(default_factory=dict)
    review_policy: ReviewPolicyCfg = Field(default_factory=ReviewPolicyCfg)
    escalation: EscalationCfg = Field(default_factory=EscalationCfg)


# --- mappings.yaml ---


class MappingsCfg(_StrictModel):
    email_to_mattermost: dict[str, str] = Field(default_factory=dict)
    component_to_repo: dict[str, str] = Field(default_factory=dict)
    team_channels: dict[str, str] = Field(default_factory=dict)
    disclaimer_template: str = ""

    @field_validator("email_to_mattermost", "component_to_repo", "team_channels", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        # A YAML key with no children (`foo:`) parses to None; treat it as {}.
        return {} if value is None else value


# --- Aggregate ---


class AppConfig(_StrictModel):
    """All YAML config merged together."""

    repositories: list[RepositoryCfg]
    agents: AgentsCfg
    mappings: MappingsCfg
    notifications: NotificationsCfg = Field(default_factory=NotificationsCfg)

    def get_repository(self, key: str) -> RepositoryCfg | None:
        for repo in self.repositories:
            if repo.key == key:
                return repo
        return None
