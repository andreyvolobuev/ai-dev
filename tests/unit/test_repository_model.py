"""Unit tests for the Repository domain model."""

from __future__ import annotations

from virtual_dev.domain.models.repository import Repository, RepositoryAgents


def test_dev_agent_keys_full_house() -> None:
    repo = Repository(
        key="bellingshausen",
        url="git@example:x.git",
        agents=RepositoryAgents(backend=True, frontend=True, devops=True),
    )
    assert repo.dev_agent_keys() == [
        "dev-bellingshausen-backend",
        "dev-bellingshausen-frontend",
        "dev-bellingshausen-devops",
    ]


def test_dev_agent_keys_only_backend() -> None:
    repo = Repository(
        key="rainbow",
        url="git@example:rainbow.git",
        agents=RepositoryAgents(backend=True),
    )
    assert repo.dev_agent_keys() == ["dev-rainbow-backend"]


def test_dev_agent_keys_empty() -> None:
    repo = Repository(key="x", url="git@example:x.git")
    assert repo.dev_agent_keys() == []
