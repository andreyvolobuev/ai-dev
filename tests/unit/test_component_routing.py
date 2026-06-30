"""AppConfig.repo_for_components routes Jira components to a repo key."""

from __future__ import annotations

from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)


def _repo(key: str, components: list[str]) -> RepositoryCfg:
    return RepositoryCfg(key=key, url=f"git@x/{key}.git", jira_components=components)


def _cfg(*, repos: list[RepositoryCfg], component_to_repo: dict[str, str] | None = None) -> AppConfig:
    return AppConfig(
        repositories=repos,
        agents=AgentsCfg(),
        mappings=MappingsCfg(component_to_repo=component_to_repo or {}),
    )


def test_matches_via_repository_jira_components() -> None:
    cfg = _cfg(repos=[_repo("bellingshausen", ["Krusenstern"])])
    assert cfg.repo_for_components(["Krusenstern"]) == "bellingshausen"


def test_mappings_override_wins_over_repo_declaration() -> None:
    cfg = _cfg(
        repos=[_repo("bellingshausen", ["Krusenstern"])],
        component_to_repo={"Krusenstern": "other"},
    )
    assert cfg.repo_for_components(["Krusenstern"]) == "other"


def test_first_matching_component_wins() -> None:
    cfg = _cfg(repos=[_repo("a", ["X"]), _repo("b", ["Y"])])
    assert cfg.repo_for_components(["Y", "X"]) == "b"


def test_match_is_case_sensitive() -> None:
    cfg = _cfg(repos=[_repo("a", ["Krusenstern"])])
    assert cfg.repo_for_components(["krusenstern"]) is None


def test_no_match_returns_none() -> None:
    cfg = _cfg(repos=[_repo("a", ["X"]), _repo("b", ["Y"])])
    assert cfg.repo_for_components(["Z"]) is None


def test_none_or_empty_components_returns_none() -> None:
    cfg = _cfg(repos=[_repo("a", ["X"])])
    assert cfg.repo_for_components(None) is None
    assert cfg.repo_for_components([]) is None
