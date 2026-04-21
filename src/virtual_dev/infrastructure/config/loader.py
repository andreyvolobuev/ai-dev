"""YAML config loader with ``local.yaml`` overrides."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
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


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``. Lists replace, not append."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_dir: Path | str = "config") -> AppConfig:
    """Read all YAML configs from ``config_dir`` and merge ``local.yaml`` on top.

    Fails loudly if required files are absent.
    """
    root = Path(config_dir)

    repositories_raw = _read_yaml(root / "repositories.yaml")
    agents_raw = _read_yaml(root / "agents.yaml")
    mappings_raw = _read_yaml(root / "mappings.yaml")

    local_path = root / "local.yaml"
    if local_path.exists():
        local_raw = _read_yaml(local_path)
        repositories_raw = _deep_merge(repositories_raw, local_raw.get("repositories_override", {}))
        agents_raw = _deep_merge(agents_raw, local_raw.get("agents_override", local_raw))
        mappings_raw = _deep_merge(mappings_raw, local_raw.get("mappings_override", {}))

    repositories = RepositoriesCfg.model_validate(repositories_raw).repositories
    agents = AgentsCfg.model_validate(agents_raw)
    mappings = MappingsCfg.model_validate(mappings_raw)

    return AppConfig(repositories=repositories, agents=agents, mappings=mappings)
