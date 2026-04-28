"""AGENTS.md / RESONANT.md / CLAUDE.md precedence and write-back behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.gui.project_instructions import (
    INSTRUCTION_FILES,
    find_instruction_file,
    load_project_instructions,
)


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_priority_order_lists_agents_first():
    assert INSTRUCTION_FILES[0] == "AGENTS.md"
    # CLAUDE.md is the courtesy fallback — should still be present
    assert "CLAUDE.md" in INSTRUCTION_FILES
    # Legacy RESONANT.md still recognized
    assert "RESONANT.md" in INSTRUCTION_FILES


def test_agents_md_takes_precedence_over_resonant_md(project_dir):
    (project_dir / "AGENTS.md").write_text("# from AGENTS.md\n", encoding="utf-8")
    (project_dir / "RESONANT.md").write_text("# from RESONANT.md\n", encoding="utf-8")
    found = find_instruction_file(str(project_dir))
    assert found is not None
    assert found.name == "AGENTS.md"
    assert "AGENTS.md" in load_project_instructions(str(project_dir))


def test_resonant_md_used_when_agents_md_absent(project_dir):
    (project_dir / "RESONANT.md").write_text("# legacy convention\n", encoding="utf-8")
    found = find_instruction_file(str(project_dir))
    assert found is not None
    assert found.name == "RESONANT.md"


def test_claude_md_used_as_final_fallback(project_dir):
    (project_dir / "CLAUDE.md").write_text("# Claude Code project\n", encoding="utf-8")
    found = find_instruction_file(str(project_dir))
    assert found is not None
    assert found.name == "CLAUDE.md"


def test_no_instructions_file_returns_none(project_dir):
    assert find_instruction_file(str(project_dir)) is None
    assert load_project_instructions(str(project_dir)) is None


def test_save_writes_agents_md_for_new_projects(project_dir, monkeypatch):
    """New projects (no existing instructions file) get an AGENTS.md."""
    from resonant_client.gui.app import _save_resonant_md
    _save_resonant_md(str(project_dir), "# fresh project conventions\n")
    assert (project_dir / "AGENTS.md").read_text(encoding="utf-8") == "# fresh project conventions\n"
    assert not (project_dir / "RESONANT.md").exists()


def test_save_preserves_existing_resonant_md_filename(project_dir):
    """Projects already using RESONANT.md keep writing to RESONANT.md."""
    from resonant_client.gui.app import _save_resonant_md
    (project_dir / "RESONANT.md").write_text("# legacy\n", encoding="utf-8")
    _save_resonant_md(str(project_dir), "# updated legacy\n")
    assert (project_dir / "RESONANT.md").read_text(encoding="utf-8") == "# updated legacy\n"
    assert not (project_dir / "AGENTS.md").exists()


def test_save_preserves_existing_claude_md_filename(project_dir):
    """Projects with CLAUDE.md keep using it (don't fork to AGENTS.md silently)."""
    from resonant_client.gui.app import _save_resonant_md
    (project_dir / "CLAUDE.md").write_text("# claude code\n", encoding="utf-8")
    _save_resonant_md(str(project_dir), "# updated claude code\n")
    assert (project_dir / "CLAUDE.md").read_text(encoding="utf-8") == "# updated claude code\n"
    assert not (project_dir / "AGENTS.md").exists()
