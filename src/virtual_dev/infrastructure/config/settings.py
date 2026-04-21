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

    # --- Confluence ---
    confluence_url: str = ""
    confluence_user: str = ""
    confluence_token: str = ""

    # --- Runtime ---
    db_url: str = "sqlite+aiosqlite:///./data/virtual_dev.db"
    workspaces_dir: str = "./workspaces"

    web_host: str = "127.0.0.1"
    web_port: int = 8080

    log_level: str = Field(default="INFO")
