from pathlib import Path

from virtual_dev.application.services.repo_instructions import render_repo_instructions


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_workspace_or_no_files_gives_empty_block(tmp_path: Path) -> None:
    assert render_repo_instructions(None) == ""
    assert render_repo_instructions(tmp_path) == ""
    assert render_repo_instructions(tmp_path / "missing") == ""


def test_agents_md_is_inlined(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "# Rules\n- line length 120\n")

    block = render_repo_instructions(tmp_path)

    assert "line length 120" in block
    assert "AGENTS.md" in block


def test_critical_context_inlined_rest_listed(tmp_path: Path) -> None:
    _write(
        tmp_path, ".opencode/context/chris-review.md",
        "<!-- Context: chris-review | Priority: critical | Version: 1 -->\nCHRIS RULE\n",
    )
    _write(
        tmp_path, ".opencode/context/project-intelligence/principles.md",
        "<!-- Context: principles | Priority: high | Version: 1 -->\nPRINCIPLES BODY\n",
    )
    _write(tmp_path, ".opencode/context/api_guidelines.md", "# API\nAPI BODY\n")
    # opencode subagent definitions are tooling config, not repo rules.
    _write(tmp_path, ".opencode/agents/context-scout.md", "SCOUT BODY\n")

    block = render_repo_instructions(tmp_path)

    assert "CHRIS RULE" in block
    assert "PRINCIPLES BODY" not in block
    assert "API BODY" not in block
    assert "`.opencode/context/project-intelligence/principles.md` (priority: high)" in block
    assert "`.opencode/context/api_guidelines.md`" in block
    assert "SCOUT" not in block


def test_critical_files_over_budget_become_mandatory_reads(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "A" * 100)
    _write(
        tmp_path, ".opencode/context/chris-review.md",
        "<!-- Priority: critical -->\n" + "C" * 100,
    )

    block = render_repo_instructions(tmp_path, max_inline_chars=150)

    assert "A" * 100 in block
    assert "C" * 100 not in block
    assert "MUST `Read`" in block
    assert "`.opencode/context/chris-review.md`" in block
