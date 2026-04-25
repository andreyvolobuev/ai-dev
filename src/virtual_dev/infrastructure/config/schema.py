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


class PipelinePolicyCfg(_StrictModel):
    """How aggressive auto-fix on red pipelines is.

    DevOps detects red CI and dispatches Dev-iteration with the full job
    logs. After ``max_autofix_attempts`` failed attempts (each followed
    by another red pipeline), DevOps gives up and DMs
    ``escalation.mattermost_user``. CI failure events are NEVER posted
    to team channels — fixing one's own CI is the developer's job, and
    the bot IS the developer here.
    """

    max_autofix_attempts: int = 3


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
    # Auto-fix path. Pipeline failures are NEVER announced in a team
    # channel — only DM'd to escalation.mattermost_user when the bot
    # has exhausted its auto-fix attempts.
    pipeline_autofix_gave_up_dm: str = ""
    thread_reply_no_dev_agent: str = ""
    thread_reply_no_task: str = ""
    thread_reply_iteration_crashed: str = ""
    thread_reply_iteration_done: str = ""
    thread_reply_iteration_no_changes: str = ""
    # Used when the review feedback came in a GitLab MR comment instead
    # of a Mattermost thread — bot answers in the same medium.
    gitlab_reply_iteration_done: str = ""
    # Clarification flow (Analyst → DM → answer → re-plan). The first is
    # the question DM body itself; the others are sent as thread replies
    # under the question DM when the user answers. All can use
    # str.format placeholders documented in config/notifications.yaml.
    clarifier_question: str = ""
    clarifier_answer_ack: str = ""
    clarifier_all_answered_ack: str = ""
    # Phase 3.8 — tree-aware clarification templates.
    clarifier_redirect_ack: str = ""           # "Спасибо, перенаправил на @{handle}"
    clarifier_handle_request: str = ""         # "Подскажи MM-ник {raw_name}?"
    clarifier_counter_factual_intro: str = ""  # bot's self-answer to a factual counter-Q
    clarifier_out_of_scope_ack: str = ""       # OUT_OF_SCOPE acknowledgement
    clarifier_escalation_to_lead: str = ""     # DM to team-lead with the chain
    clarifier_dont_know_ack: str = ""          # DONT_KNOW acknowledgement to respondent


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


class ClarificationCfg(_StrictModel):
    """Tunables for the clarification subsystem (Phase 3.8).

    All durations are in seconds except ``max_question_age_hours``.
    Defaults are conservative — long enough that someone in a meeting
    won't time-out, short enough that stuck Issues escalate within a
    working day.
    """

    coalesce_window_seconds: int = 600              # idle before flushing fragments to LLM
    poll_interval_seconds: int = 60                 # how often the worker ticks
    max_chain_depth: int = 4                        # redirect-chain depth guard
    max_question_age_hours: int = 48                # per-Question timeout
    max_subquestions_per_root: int = 10             # tree-size guard
    counter_question_confidence_threshold: float = 0.6   # FACTUAL→bot vs fallback


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
    pipeline_policy: PipelinePolicyCfg = Field(default_factory=PipelinePolicyCfg)
    escalation: EscalationCfg = Field(default_factory=EscalationCfg)
    clarification: ClarificationCfg = Field(default_factory=ClarificationCfg)


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
