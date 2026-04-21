"""Доменная модель репозитория."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RepositoryAgents:
    """Какие агенты работают с этим репо."""

    backend: bool = False
    frontend: bool = False
    devops: bool = False


@dataclass
class Repository:
    """Репозиторий в VCS (GitLab / GitHub / Bitbucket)."""

    key: str                          # уникальный ID (snake_case), как в repositories.yaml
    url: str                          # SSH/HTTPS URL
    description: str = ""
    local_path: str | None = None     # где лежит клон локально; если None — клонируем в WORKSPACES_DIR/<key>
    default_branch: str = "main"
    jira_components: list[str] = field(default_factory=list)
    primary_language: str | None = None
    frontend_stack: str | None = None
    tests_cmd: str | None = None
    lint_cmd: str | None = None
    ci_provider: str = "gitlab_ci"
    agents: RepositoryAgents = field(default_factory=RepositoryAgents)

    def dev_agent_keys(self) -> list[str]:
        """Список ключей dev-агентов, которых нужно поднять для этого репо."""
        keys: list[str] = []
        if self.agents.backend:
            keys.append(f"dev-{self.key}-backend")
        if self.agents.frontend:
            keys.append(f"dev-{self.key}-frontend")
        if self.agents.devops:
            keys.append(f"dev-{self.key}-devops")
        return keys
