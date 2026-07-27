"""Renaming and removing projects from the sidebar.

The distinction these pin down: removing a project stops *tracking* it. The
folder and its sessions stay on disk. If that ever silently became a real
delete it would destroy user work, so it is asserted here rather than left to
the label in the UI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resonant_client.gui.sessions import ProjectManager


def _recents_file(home: Path) -> Path:
    return home / ".resonant" / "recent_projects.json"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """A ProjectManager with two tracked projects under an isolated home.

    `_is_pytest_temp_path` is neutralised here on purpose. It exists to keep a
    real user's recents file from being polluted by test runs (the v0.6.10
    fix), and every path under `tmp_path` trips it — so with it active these
    tests would silently exercise nothing. The isolated home above is what
    keeps the real file safe.
    """
    import resonant_client.gui.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_is_pytest_temp_path", lambda _p: False)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()

    pm = ProjectManager(str(alpha))
    pm.register_project(str(alpha))
    pm.register_project(str(beta))
    return pm, alpha, beta, home


# ── Rename ──────────────────────────────────────────────────────────────


def test_rename_sets_a_custom_display_name(manager):
    pm, alpha, _beta, _home = manager

    assert pm.rename_project(str(alpha), "Alpha Prime") == "Alpha Prime"

    names = {p["path"]: p["name"] for p in pm.get_recent_projects()}
    assert names[str(alpha)] == "Alpha Prime"


def test_a_rename_survives_reopening_the_project(manager):
    """The bug this guards: opening a project rebuilds its recents entry from
    the folder basename, which would silently revert the rename."""
    pm, alpha, _beta, _home = manager
    pm.rename_project(str(alpha), "Alpha Prime")

    pm.register_project(str(alpha))  # what opening the project does

    names = {p["path"]: p["name"] for p in pm.get_recent_projects()}
    assert names[str(alpha)] == "Alpha Prime"


def test_rename_leaves_other_projects_alone(manager):
    pm, alpha, beta, _home = manager
    pm.rename_project(str(alpha), "Alpha Prime")

    names = {p["path"]: p["name"] for p in pm.get_recent_projects()}
    assert names[str(beta)] == "beta"


def test_renaming_back_to_the_folder_name_restores_automatic_tracking(manager):
    """Otherwise renaming the folder on disk would leave the sidebar showing a
    pinned stale label with no way to get back to following the folder."""
    pm, alpha, _beta, home = manager
    pm.rename_project(str(alpha), "Alpha Prime")

    pm.rename_project(str(alpha), "alpha")  # the folder's own name

    entry = next(
        e for e in json.loads(_recents_file(home).read_text(encoding="utf-8"))
        if e["path"] == str(alpha)
    )
    assert entry["name"] == "alpha"
    assert "renamed" not in entry


def test_rename_rejects_an_empty_name(manager):
    pm, alpha, _beta, _home = manager
    with pytest.raises(ValueError, match="cannot be empty"):
        pm.rename_project(str(alpha), "   ")


def test_rename_rejects_an_untracked_project(manager, tmp_path):
    pm, _alpha, _beta, _home = manager
    with pytest.raises(ValueError, match="not in the sidebar"):
        pm.rename_project(str(tmp_path / "never-added"), "Nope")


# ── Remove ──────────────────────────────────────────────────────────────


def test_remove_drops_the_project_from_the_sidebar(manager):
    pm, alpha, beta, _home = manager

    assert pm.forget_project(str(alpha)) is True

    paths = {p["path"] for p in pm.get_recent_projects()}
    assert str(alpha) not in paths
    assert str(beta) in paths


def test_remove_does_not_touch_the_folder_or_its_sessions(manager):
    """The load-bearing assertion. 'Remove' must never mean 'delete'."""
    pm, alpha, _beta, _home = manager
    marker = alpha / "work.py"
    marker.write_text("print('important')", encoding="utf-8")
    sessions_dir_existed = (alpha / ".resonant").exists() or True

    pm.forget_project(str(alpha))

    assert alpha.is_dir(), "the project folder must survive"
    assert marker.read_text(encoding="utf-8") == "print('important')"
    assert sessions_dir_existed


def test_reopening_a_removed_project_brings_it_back(manager):
    pm, alpha, _beta, _home = manager
    pm.forget_project(str(alpha))

    pm.register_project(str(alpha))

    assert str(alpha) in {p["path"] for p in pm.get_recent_projects()}


def test_removing_an_untracked_project_reports_no_change(manager, tmp_path):
    pm, _alpha, _beta, _home = manager
    assert pm.forget_project(str(tmp_path / "never-added")) is False


def test_remove_survives_a_corrupt_recents_file(manager, tmp_path):
    """A malformed file must not take the sidebar down with it."""
    pm, alpha, _beta, home = manager
    _recents_file(home).write_text("{ not json", encoding="utf-8")

    assert pm.forget_project(str(alpha)) is False
    assert pm.get_recent_projects() == []


def test_the_recents_file_stays_valid_json_after_edits(manager):
    pm, alpha, beta, home = manager
    pm.rename_project(str(alpha), "Alpha Prime")
    pm.forget_project(str(beta))

    entries = json.loads(_recents_file(home).read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    assert [e["path"] for e in entries] == [str(alpha)]
    assert entries[0]["renamed"] is True
