"""Tests for HarnessWorkspace path resolution and legacy-layout migration."""

from __future__ import annotations

import json

import pytest

from resonant_client.harness.state import (
    LEGACY_HARNESS_DIRNAME,
    HarnessWorkspace,
    ProgressState,
)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """Redirect ~/.resonant/ to a tmp dir so tests don't pollute the real home."""
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "fakeproj"
    project.mkdir()
    (project / "README.md").write_text("# fakeproj\n", encoding="utf-8")
    return project


def test_root_lives_under_state_home_not_in_project(state_home, project_dir):
    """Harness state must NOT land inside the user's project repo."""
    ws = HarnessWorkspace(project_dir)
    assert state_home in ws.root.parents, f"root {ws.root} should be under {state_home}"
    assert project_dir not in ws.root.parents, "harness root must not be inside the project"
    # Predictable layout: <state_home>/projects/<hash>/harness/
    assert ws.root.parent.parent == state_home / "projects"
    assert ws.root.name == "harness"


def test_ensure_layout_creates_files_in_new_root(state_home, project_dir):
    ws = HarnessWorkspace(project_dir)
    ws.ensure_layout()
    for p in (
        ws.spec_path, ws.progress_path, ws.sprint_contract_path,
        ws.evaluator_report_path, ws.handoff_path,
        ws.run_history_path, ws.teacher_escalations_path,
    ):
        assert p.exists(), f"{p} should exist after ensure_layout"
        assert state_home in p.parents
    # Repo stays clean — no .resonant-harness/ in project_dir
    assert not (project_dir / LEGACY_HARNESS_DIRNAME).exists()


def test_legacy_migration_copies_files(state_home, project_dir):
    """A pre-existing `.resonant-harness/` is copied into the new root."""
    legacy = project_dir / LEGACY_HARNESS_DIRNAME
    legacy.mkdir()
    (legacy / "spec.json").write_text(json.dumps({"title": "old project"}), encoding="utf-8")
    (legacy / "handoff.md").write_text("# legacy handoff\n", encoding="utf-8")

    ws = HarnessWorkspace(project_dir)
    copied = ws.maybe_migrate_legacy_layout()
    assert copied == 2

    # Files copied with content preserved
    assert ws.spec_path.exists()
    assert json.loads(ws.spec_path.read_text(encoding="utf-8"))["title"] == "old project"
    assert ws.handoff_path.read_text(encoding="utf-8") == "# legacy handoff\n"

    # Legacy left in place — user removes it explicitly with `git rm`.
    assert legacy.exists()
    assert (legacy / "spec.json").exists()


def test_legacy_migration_is_idempotent(state_home, project_dir):
    """Running migration twice doesn't overwrite or re-copy."""
    legacy = project_dir / LEGACY_HARNESS_DIRNAME
    legacy.mkdir()
    (legacy / "spec.json").write_text(json.dumps({"title": "v1"}), encoding="utf-8")

    ws = HarnessWorkspace(project_dir)
    assert ws.maybe_migrate_legacy_layout() == 1

    # Modify the new copy; second migration must not clobber it
    ws.spec_path.write_text(json.dumps({"title": "v2-edited"}), encoding="utf-8")
    assert ws.maybe_migrate_legacy_layout() == 0
    assert json.loads(ws.spec_path.read_text(encoding="utf-8"))["title"] == "v2-edited"


def test_legacy_migration_no_op_when_no_legacy_dir(state_home, project_dir):
    ws = HarnessWorkspace(project_dir)
    assert ws.maybe_migrate_legacy_layout() == 0
    assert not (project_dir / LEGACY_HARNESS_DIRNAME).exists()


def test_ensure_layout_runs_migration(state_home, project_dir):
    """ensure_layout() should pick up legacy files automatically."""
    legacy = project_dir / LEGACY_HARNESS_DIRNAME
    legacy.mkdir()
    payload = {"sprint_id": "sp-1", "objective": "ship it", "status": "approved"}
    (legacy / "sprint_contract.json").write_text(json.dumps(payload), encoding="utf-8")

    ws = HarnessWorkspace(project_dir)
    ws.ensure_layout()
    contract = ws.read_sprint_contract()
    assert contract.sprint_id == "sp-1"
    assert contract.objective == "ship it"
    assert contract.status == "approved"


def test_distinct_projects_get_distinct_roots(state_home, tmp_path):
    """Two project paths hash to distinct harness roots."""
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    ws_a = HarnessWorkspace(a)
    ws_b = HarnessWorkspace(b)
    assert ws_a.root != ws_b.root
    # Both still under state_home/projects/
    assert ws_a.root.parent.parent == state_home / "projects"
    assert ws_b.root.parent.parent == state_home / "projects"


def test_round_trip_progress(state_home, project_dir):
    ws = HarnessWorkspace(project_dir)
    ws.ensure_layout()
    progress = ProgressState(active_sprint_id="sp-42", current_phase="implementing", summary="halfway")
    ws.write_progress(progress)
    loaded = ws.read_progress()
    assert loaded.active_sprint_id == "sp-42"
    assert loaded.current_phase == "implementing"
    assert loaded.summary == "halfway"
