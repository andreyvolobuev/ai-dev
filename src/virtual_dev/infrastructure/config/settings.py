"""Env-based settings (secrets + runtime knobs)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Runtime ---
    db_url: str = "sqlite+aiosqlite:///./data/virtual_dev.db"
    workspaces_dir: str = "./workspaces"

    # --- Bot identity used when committing / opening MRs.
    # Push is done with the GitLab token above (same account as the user),
    # but the commit author is always the bot — so humans see at a glance
    # who wrote the code.
    dev_git_author_name: str = "Virtual Dev"
    dev_git_author_email: str = "virtual-dev@datamining.2gis.ru"
    dev_branch_prefix: str = "ai-dev"
    dev_mr_draft: bool = True   # open MRs as draft by default

    web_host: str = "127.0.0.1"
    web_port: int = 8080

    log_level: str = Field(default="INFO")

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
