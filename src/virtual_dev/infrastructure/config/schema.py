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
    # NB: the corporate gateway (see Settings.anthropic_base_url) accepts only
    # ids from /anthropic/v1/models — bare aliases for 4.6+/fable-5, but ONLY
    # dated ids for 4.5 and haiku (bare claude-sonnet-4-5 / claude-haiku-4-5
    # → 404 there). Keep these gateway-valid.
    default: str = "claude-sonnet-4-6"
    lightweight: str = "claude-haiku-4-5-20251001"


class TaskSourceCfg(_StrictModel):
    jql: str
    poll_interval_seconds: int = 120


class JiraTransitionsCfg(_StrictModel):
    to_in_progress: str = "In Progress"
    to_review: str = "Review"
    to_testing: str = "Testing"
    to_done: str = "Done"
    to_pending: str = "Waiting For Response"


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
    # Red pipelines whose failed jobs ALL look like CI infrastructure
    # (registry 5xx, package-proxy timeouts, runner failures) are not a
    # code problem — DevOps retries the pipeline via the GitLab API
    # instead of burning a Dev iteration. This caps how many such
    # retries happen before DevOps escalates to the lead.
    max_infra_retries: int = 2


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
    # Same DM slot, but for pipelines that keep failing on CI
    # *infrastructure* (registry 5xx / proxy timeouts) — the bot retried
    # the pipeline itself and never touched the code, so the text must
    # not claim "auto-fix attempts" that never happened.
    pipeline_infra_gave_up_dm: str = ""
    # Confirmation the bot posts when the team-lead replies `/restart` in
    # the give-up DM thread and the autofix counter is reset.
    pipeline_autofix_restart_ack: str = ""
    thread_reply_no_dev_agent: str = ""
    thread_reply_no_task: str = ""
    thread_reply_iteration_crashed: str = ""
    thread_reply_iteration_done: str = ""
    thread_reply_iteration_no_changes: str = ""
    # Lead-escalation DMs. Two flavours:
    # * stuck — agent ran out of angles and asks the lead for help
    #   (ticket stays in "In Progress").
    # * blocked — agent decided the ticket is blocked / unworkable;
    #   ticket has just been transitioned to "Waiting For Response" and
    #   commented in Jira; the lead is told what happened.
    stuck_escalation_to_lead: str = ""
    blocked_escalation_to_lead: str = ""


class JiraTemplatesCfg(_StrictModel):
    plan_comment: str = ""
    mr_link_comment: str = ""
    failure_comment: str = ""
    blocked_comment: str = ""


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
    """Tunables for the goal-driven clarification subsystem (Phase 3.9).

    All durations are in seconds except ``max_goal_age_hours``.
    Defaults are conservative — long enough that someone in a meeting
    won't time-out, short enough that stuck Issues escalate within a
    working day.
    """

    coalesce_window_seconds: int = 600              # idle before invoking planner on collected fragments
    poll_interval_seconds: int = 60                 # how often the worker ticks
    max_goal_age_hours: int = 48                    # per-Goal hard deadline (overrides anything else)

    # Planner circuit breaker: how many decisions per goal before we
    # forcibly escalate to lead. Prevents runaway loops.
    max_planner_calls_per_goal: int = 8

    # SEND_PENDING retry: how many times to retry a DM that
    # Communicator refused (rate-limited / outside working hours)
    # before giving up the goal.
    send_retry_max: int = 5

    # REPLANNING soft-lock recovery: if a goal sat in REPLANNING longer
    # than this without progress, assume the planner crashed and
    # revert to READY_TO_REPLAN for retry.
    replanning_stuck_after_minutes: int = 10

    # Sub-goal recursion guard. A goal at depth ``max_subgoal_depth``
    # can no longer spawn children — orchestrator escalates instead.
    # Prevents runaway recursive decomposition by the planner.
    max_subgoal_depth: int = 4


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

    def model_for(self, agent_key: str) -> str:
        """Resolve an agent's configured ``model`` reference to a concrete id.

        ``agents.<key>.model`` is a *reference*: ``"default"`` / ``"lightweight"``
        map into :class:`ModelsCfg`; any other value is treated as a literal
        model id and returned verbatim. An agent with no config entry (or an
        empty ``model``) falls back to ``default``.
        """
        cfg = self.agents.get(agent_key)
        ref = (cfg.model if cfg else None) or "default"
        if ref == "default":
            return self.models.default
        if ref == "lightweight":
            return self.models.lightweight
        return ref


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

    def repo_for_components(self, components: list[str] | None) -> str | None:
        """Resolve a ticket's Jira components to a repo key.

        Per component (first match wins): an explicit
        ``mappings.component_to_repo`` entry overrides; otherwise the repo
        whose ``repositories.yaml`` ``jira_components`` lists that component.
        Matching is exact and case-sensitive (component names come straight
        from Jira). Returns None when nothing matches — callers apply their
        own fallbacks (single-repo, target_repo_key, Analyst guess).
        """
        override = self.mappings.component_to_repo
        for component in components or []:
            if component in override:
                return override[component]
            for repo in self.repositories:
                if component in repo.jira_components:
                    return repo.key
        return None
