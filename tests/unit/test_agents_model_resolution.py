"""AgentsCfg.model_for resolves per-agent ``model`` refs to concrete ids."""

from __future__ import annotations

from virtual_dev.infrastructure.config.schema import AgentCfg, AgentsCfg, ModelsCfg


def _agents(**agents: AgentCfg) -> AgentsCfg:
    return AgentsCfg(
        models=ModelsCfg(default="model-default", lightweight="model-light"),
        agents=dict(agents),
    )


def test_default_ref_resolves_to_models_default() -> None:
    cfg = _agents(analyst=AgentCfg(model="default"))
    assert cfg.model_for("analyst") == "model-default"


def test_lightweight_ref_resolves_to_models_lightweight() -> None:
    cfg = _agents(reviewer=AgentCfg(model="lightweight"))
    assert cfg.model_for("reviewer") == "model-light"


def test_literal_model_id_is_returned_verbatim() -> None:
    cfg = _agents(developer=AgentCfg(model="claude-opus-4-8"))
    assert cfg.model_for("developer") == "claude-opus-4-8"


def test_missing_agent_falls_back_to_default() -> None:
    # No config entry for the key — resolves to the default model, not an error.
    cfg = _agents()
    assert cfg.model_for("thread_responder") == "model-default"


def test_agentcfg_default_model_ref_is_default() -> None:
    # AgentCfg() with no explicit model defaults to the "default" ref.
    cfg = _agents(developer=AgentCfg())
    assert cfg.model_for("developer") == "model-default"
