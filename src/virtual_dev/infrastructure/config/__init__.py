"""Configuration layer: pydantic-settings for env, YAML loader for everything else."""

from virtual_dev.infrastructure.config.loader import ConfigError, load_config
from virtual_dev.infrastructure.config.schema import (
    AgentCfg,
    AgentsCfg,
    AppConfig,
    JiraTemplatesCfg,
    MappingsCfg,
    MmTemplatesCfg,
    MrTemplatesCfg,
    NotificationsCfg,
    RepositoryCfg,
    WorkingHoursCfg,
)
from virtual_dev.infrastructure.config.settings import Settings

__all__ = [
    "AgentCfg",
    "AgentsCfg",
    "AppConfig",
    "ConfigError",
    "JiraTemplatesCfg",
    "MappingsCfg",
    "MmTemplatesCfg",
    "MrTemplatesCfg",
    "NotificationsCfg",
    "RepositoryCfg",
    "Settings",
    "WorkingHoursCfg",
    "load_config",
]
