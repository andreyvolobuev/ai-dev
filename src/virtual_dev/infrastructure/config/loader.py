"""YAML config loader.

All YAML files are committed to the repository — there are no
uncommitted overlays. Deploy-specific values (lead handle, default
channel, per-machine repo paths) live in ``.env`` and are applied
on top of the YAML config inside ``build_container``. See
:func:`virtual_dev.infrastructure.config.settings.Settings`.
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
