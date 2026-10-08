"""Конфиг интейка задач и шаблоны ответов бота.

Тест ходит в реальный config/ - он же и уезжает в прод, так что
опечатка в ключе или потерянный шаблон видны сразу.
"""

from __future__ import annotations

from pathlib import Path

from virtual_dev.infrastructure.config import load_config
from virtual_dev.infrastructure.config.schema import AgentsCfg

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_shipped_config_enables_intake_with_dmp_sup_label() -> None:
    config = load_config(_CONFIG_DIR)
    intake = config.agents.task_intake

    assert intake.enabled is True
    assert intake.project == "DM"
    # Project DM names its issue types in Russian and has no type called
    # "Task" — that is what the intake hit in production, with Jira
    # answering "The issue type selected is invalid".
    assert intake.issue_type == "Задача"
    assert intake.labels == ["dmp-sup"]
    # Tickets go to the grooming queue, never into the running sprint.
    assert intake.sprint_name == "DM. Распределительная пещера"
    assert intake.components == ["DM-Common"]
    assert intake.summary_prefix == "[SUPPORT] "
    assert intake.customer_field == "customfield_32545"


def test_intake_agent_has_a_model() -> None:
    """model_for падает в default, если ключа нет - но модель интейка
    задана явно, чтобы её можно было крутить отдельно."""
    config = load_config(_CONFIG_DIR)
    assert "task_intake" in config.agents.agents
    assert config.agents.model_for("task_intake")


def test_shipped_templates_present_and_feminine() -> None:
    templates = load_config(_CONFIG_DIR).notifications.mattermost

    assert "{key}" in templates.intake_created
    assert "{url}" in templates.intake_created
    assert "Завела" in templates.intake_created
    assert templates.intake_created.strip() == "Завела [{key}]({url}){warnings_block}"
    assert templates.intake_updated
    assert templates.intake_failed
    assert templates.intake_busy_fallback
    assert templates.intake_warning_sprint_not_found
    assert templates.intake_warning_sprint_failed
    assert not hasattr(templates, "intake_warning_no_active_sprint")
    assert not hasattr(templates, "intake_warning_assignee_not_found")
    assert templates.intake_warning_assignee_hint_unresolved


def test_intake_defaults_are_safe_without_yaml() -> None:
    """Пустой agents.yaml не должен внезапно включать запись в Jira."""
    cfg = AgentsCfg()
    assert cfg.task_intake.enabled is False
    assert cfg.task_intake.labels == ["dmp-sup"]


def test_intake_shape_defaults_do_not_file_into_a_sprint() -> None:
    cfg = AgentsCfg().task_intake
    assert cfg.sprint_name == ""
    assert cfg.components == []
    assert cfg.summary_prefix == ""
    assert cfg.customer_field == ""


def test_prompt_pins_the_description_framework_and_the_no_invention_rule() -> None:
    raw = (_CONFIG_DIR / "prompts" / "task_intake.md").read_text(encoding="utf-8")
    prompt = " ".join(raw.split())  # the prompt wraps lines mid-phrase

    for marker in (
        "Текущая ситуация:",
        "Суть задачи:",
        "DoDs:",
        "Не понимаю суть задачи, нужно уточнение",  # noqa: RUF001
        "Не могу определить DoDs",  # noqa: RUF001
        "выдуманные DoDs хуже отсутствующих",
    ):
        assert marker in prompt
