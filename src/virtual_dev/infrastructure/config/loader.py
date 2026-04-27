"""YAML config loader.

All YAML files are committed to the repository — there are no
uncommitted overlays. Deploy-specific values (lead handle, default
channel, per-machine repo paths) live in ``.env`` and are applied
on top of the YAML config via :func:`apply_settings_overrides`. Both
the production wiring (``build_container``) and the test-analyst UI
call it. See
:func:`virtual_dev.infrastructure.config.settings.Settings` for the
list of overridable env vars.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    NotificationsCfg,
    RepositoriesCfg,
)
from virtual_dev.infrastructure.config.settings import Settings


class ConfigError(RuntimeError):
    """Raised when config files are missing or malformed."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must be a mapping at the top level")
    return cast(dict[str, Any], data)


def load_config(config_dir: Path | str = "config") -> AppConfig:
    """Read all YAML configs from ``config_dir``.

    Fails loudly if required files are absent. Deploy-specific values
    are NOT applied here — see ``apply_settings_overrides`` (called
    from :mod:`container`) for the env-driven layer.
    """
    root = Path(config_dir)

    repositories_raw = _read_yaml(root / "repositories.yaml")
    agents_raw = _read_yaml(root / "agents.yaml")
    mappings_raw = _read_yaml(root / "mappings.yaml")
    # notifications.yaml is optional during the transition; missing file
    # falls back to empty templates (which the schema fills with defaults).
    notifications_path = root / "notifications.yaml"
    notifications_raw: dict[str, Any] = (
        _read_yaml(notifications_path) if notifications_path.exists() else {}
    )

    repositories = RepositoriesCfg.model_validate(repositories_raw).repositories
    agents = AgentsCfg.model_validate(agents_raw)
    mappings = MappingsCfg.model_validate(mappings_raw)
    notifications = NotificationsCfg.model_validate(notifications_raw)

    return AppConfig(
        repositories=repositories, agents=agents,
        mappings=mappings, notifications=notifications,
    )


def apply_settings_overrides(config: AppConfig, settings: Settings) -> None:
    """Layer deploy-specific values from ``.env`` on top of the YAML
    config. Replaces the old ``config/local.yaml`` overlay so all
    deploy-specific values live in one place (the environment).

    Empty env values fall through to whatever's in YAML. Call from
    every entry point that builds an ``AppConfig`` for live use —
    both ``build_container`` (production) and the test-analyst UI
    must call this, or env values like ``REPO_LOCAL_PATHS`` silently
    do nothing.
    """
    if settings.escalation_user:
        config.agents.escalation.mattermost_user = settings.escalation_user
    if settings.default_team_channel:
        config.mappings.team_channels["default"] = settings.default_team_channel
    if settings.repo_local_paths:
        for repo in config.repositories:
            override = settings.repo_local_paths.get(repo.key)
            if override:
                repo.local_path = override
