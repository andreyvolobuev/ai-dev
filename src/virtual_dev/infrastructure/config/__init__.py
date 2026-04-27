"""Configuration layer: pydantic-settings for env, YAML loader for everything else."""

from virtual_dev.infrastructure.config.loader import (
    ConfigError,
    apply_settings_overrides,
    load_config,
)
from virtual_dev.infrastructure.config.schema import (
    AgentCfg,
    AgentsCfg,
    AppConfig,
    ClarificationCfg,
    EscalationCfg,
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
    "ClarificationCfg",
    "ConfigError",
    "EscalationCfg",
    "JiraTemplatesCfg",
    "MappingsCfg",
    "MmTemplatesCfg",
    "MrTemplatesCfg",
    "NotificationsCfg",
    "RepositoryCfg",
    "Settings",
    "WorkingHoursCfg",
    "apply_settings_overrides",
    "load_config",
]
