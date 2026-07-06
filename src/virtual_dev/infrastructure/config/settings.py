"""Env-based settings (secrets + runtime knobs)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _ChatProvider:
    MATTERMOST = "mattermost"
    SLACK = "slack"  # placeholder — adapter not yet implemented
    TELEGRAM = "telegram"  # placeholder — adapter not yet implemented


class Settings(BaseSettings):
    """Secrets and per-machine runtime config.

    Values are loaded from the process env, with a fallback to ``.env`` at the
    project root. YAML configs are loaded separately (see :mod:`loader`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Jira ---
    jira_url: str = ""
    jira_user: str = ""
    jira_token: str = ""

    # --- GitLab ---
    gitlab_url: str = ""
    gitlab_token: str = ""
    # Override for the bot's GitLab username. Project / group access
    # tokens (and most bot-account PATs without ``api`` scope) can't
    # call GET /api/v4/user, so the auto-resolve falls back to a
    # warning and the Reviewer can't filter the bot's own comments.
    # Set this explicitly to skip the auth() probe entirely.
    gitlab_bot_username: str = ""

    # --- Chat provider ---
    # Which chat adapter to wire. Default mattermost; "slack" /
    # "telegram" are placeholders pending adapter implementations.
    chat_provider: str = _ChatProvider.MATTERMOST

    # --- Mattermost ---
    mattermost_url: str = ""
    mattermost_token: str = ""
    mattermost_bot_username: str = ""
    # Корпоративный MM часто за self-signed / чужим CA — requests по дефолту
    # отдаёт SSLError. Выключение verify возвращает прежнее поведение; для
    # правильного пути укажи путь до корпоративного CA-bundle в CA_FILE.
    mattermost_ssl_verify: bool = True
    mattermost_ssl_ca_file: str = ""

    # --- Confluence ---
    confluence_url: str = ""
    confluence_user: str = ""
    confluence_token: str = ""

    # --- LLM / Claude gateway ---
    # By default the bot uses the local `claude` CLI login (Claude Max) — leave
    # both empty. To route Claude Agent SDK through a corporate Anthropic-
    # compatible gateway, set:
    #   ANTHROPIC_BASE_URL — base BEFORE /v1/messages
    #                        (e.g. https://ai-openai-proxy.k8s.n3.2gis.io/anthropic)
    #   ANTHROPIC_API_KEY  — sent as x-api-key (this gateway rejects Bearer)
    # When base_url is set, the container also forces
    # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1, because the strict gateway
    # rejects Claude Code's beta request fields (e.g. context_management →
    # 400 "Extra inputs are not permitted"). Do NOT set ANTHROPIC_AUTH_TOKEN.
    anthropic_base_url: str = ""
    anthropic_api_key: str = ""

    # --- Runtime ---
    # DSN format: postgresql+asyncpg://user:pass@host:5432/dbname
    # For local dev with docker-compose: see docker-compose.yaml
    # Tests use sqlite+aiosqlite:///:memory: directly (not via settings).
    db_dsn: str = "postgresql+asyncpg://sd_bots:qwerty123@localhost:5432/ai_dev"
    workspaces_dir: str = "./workspaces"

    # --- Bot identity used when committing / opening MRs.
    # Commits are authored with this name + email. GitLab attributes a commit
    # to the account whose VERIFIED email matches dev_git_author_email — so
    # DEV_GIT_AUTHOR_EMAIL must be the email registered on the bot's own GitLab
    # account (Аида / @uk.datamining.aidev). If it points at another account's
    # email, commits show up under that account (e.g. Uk.DM.GitLab.Bot).
    dev_git_author_name: str = "Аида Нейронова"
    dev_git_author_email: str = "uk.datamining.aidev@2gis.ru"
    dev_branch_prefix: str = "ai-dev"
    dev_mr_draft: bool = False  # open MRs as open; set DEV_MR_DRAFT=true for draft

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # Bearer token required for destructive dashboard endpoints (currently
    # POST /kill). When empty, those endpoints are allowed only from
    # loopback — so the default localhost setup keeps working without any
    # config, but the moment WEB_HOST is set to 0.0.0.0 / a routable IP,
    # ADMIN_TOKEN must be set or the kill-switch refuses.
    admin_token: str = ""

    log_level: str = Field(default="INFO")
    # Rotated file sink alongside stderr. Empty disables file logging.
    # Path is created on demand; rotation is by size, retained for a
    # week — enough to debug the bot's recent decisions without filling
    # the disk on a long-running deploy.
    log_file: str = ""
    log_file_rotation: str = "20 MB"
    log_file_retention: str = "7 days"

    # --- Deploy-specific overrides (replaces config/local.yaml) ---
    # All deploy-specific values now live in the environment alongside
    # the secrets — there's no second uncommitted file to track.
    #
    # Chat handle of the team-lead the bot escalates to. Falls back
    # to whatever's in agents.yaml; in practice that's empty so set
    # this in your .env.
    escalation_user: str = ""
    # Default team channel for broadcasts (e.g. "Plan ready" pings).
    # Same fallback contract as escalation_user.
    default_team_channel: str = ""
    # Per-developer-machine paths to repo checkouts. JSON-encoded:
    # ``REPO_LOCAL_PATHS={"bellingshausen": "/Users/x/bellingshausen"}``
    # Empty value → fall back to ``workspaces_dir/<key>`` (for cloned
    # repos) or whatever's in repositories.yaml.
    repo_local_paths: dict[str, str] = Field(default_factory=dict)

    # --- Phase 3 ---
    # How often Reviewer / DevOps scan open MRs. Tight enough for feedback,
    # loose enough not to hammer the GitLab API.
    review_poll_interval_seconds: int = 180
    pipeline_poll_interval_seconds: int = 120
    # When False, Communicator sends messages outside working hours anyway.
    # Useful for demos / manual smoke tests; production should keep True.
    communicator_respect_working_hours: bool = False

    # --- Phase 3.8 (clarification tree) ---
    # AnswerCoalescer poll cadence + idle-window before triggering LLM
    # classification. The window also lives in agents.yaml so it's
    # tunable per-environment, but the env value here is the runtime
    # default we use when constructing the worker.
    answer_coalesce_poll_interval_seconds: int = 60
    answer_coalesce_window_seconds: int = 600
    # MM REST catch-up cadence. The bot's WebSocket can miss events
    # during reconnects; this safety-net replays missed posts via
    # ``GET /channels/{id}/posts?since=...`` — both clarification
    # fragments (idempotent on mm_post_id UNIQUE) and review-thread
    # comments (idempotent via the bot's ✅-reaction). Tighter than
    # the WS reconnect cap so we close the gap within ~1 min even
    # when the WS is fully down.
    mm_catchup_poll_interval_seconds: int = 60
    # Recovery sweep cadence — looks for tasks stuck in CODING longer
    # than ~30 min and re-publishes plan.ready. Tight enough to recover
    # within a coffee break, loose enough not to thrash on a long
    # legitimate dev run.
    recovery_sweep_interval_seconds: int = 600
