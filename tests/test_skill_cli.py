"""Tests for v0.6.1a2 — resonant-skill CLI.

The CLI is a thin wrapper around the existing skills.py +
skill_curator.py public API. Tests verify the argparse parser
shape + each subcommand's behavior end-to-end via main().
"""
from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path

import pytest

from resonant_client.orchestration.skill_cli import build_parser, main
from resonant_client.orchestration.skills import (
    Skill,
    save_skill,
    skill_dir,
)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "fakeproj"
    project.mkdir()
    return project


def _seed(
    *,
    skill_id: str,
    scope: str = "global",
    project_path=None,
    created_by: str = "agent",
    pinned: bool = False,
    description: str = "",
    last_used_days_ago: float = 0.0,
):
    last_used = time.time() - (last_used_days_ago * 86400) if last_used_days_ago else time.time()
    s = Skill(
        id=skill_id,
        name=skill_id.replace("-", " ").title(),
        description=description or f"Test skill {skill_id}",
        scope=scope,
        created_by=created_by,
        pinned=pinned,
        last_used_at=last_used,
    )
    save_skill(s, procedure_md=f"# {skill_id}\n\nProcedure body for {skill_id}.",
               project_path=str(project_path) if project_path else None)
    return s


# ── Parser shape ──────────────────────────────────────────────────────


class TestParserShape:
    def test_list_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--pinned", "--created-by", "agent"])
        assert args.command == "list"
        assert args.pinned is True
        assert args.created_by == "agent"

    def test_view_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["view", "my-skill", "--json"])
        assert args.command == "view"
        assert args.skill_id == "my-skill"
        assert args.json is True

    def test_pin_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["pin", "my-skill"])
        assert args.command == "pin"
        assert args.skill_id == "my-skill"

    def test_unpin_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["unpin", "my-skill"])
        assert args.command == "unpin"

    def test_archive_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["archive", "my-skill", "--reason", "obsolete"])
        assert args.command == "archive"
        assert args.reason == "obsolete"

    def test_curate_with_positional(self):
        parser = build_parser()
        args = parser.parse_args(["curate", "/path/to/project"])
        assert args.command == "curate"
        assert args.project_path == "/path/to/project"

    def test_no_command_required(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ── list ──────────────────────────────────────────────────────────────


class TestListCommand:
    def test_list_empty(self, state_home, capsys):
        rc = main(["list"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "No skills" in captured.out

    def test_list_shows_seeded_skills(self, state_home, capsys):
        _seed(skill_id="a", scope="global")
        _seed(skill_id="b", scope="global", pinned=True)
        rc = main(["list"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "a" in captured.out
        assert "b" in captured.out
        # Pinned marker (ASCII to keep Windows cp1252 console happy).
        assert "[PIN]" in captured.out

    def test_list_filter_by_pinned(self, state_home, capsys):
        _seed(skill_id="unpinned", scope="global", pinned=False)
        _seed(skill_id="pinned-x", scope="global", pinned=True)
        main(["list", "--pinned"])
        captured = capsys.readouterr()
        assert "pinned-x" in captured.out
        assert "unpinned" not in captured.out

    def test_list_filter_by_created_by(self, state_home, capsys):
        _seed(skill_id="agent-x", scope="global", created_by="agent")
        _seed(skill_id="bundled-x", scope="global", created_by="bundled")
        main(["list", "--created-by", "bundled"])
        captured = capsys.readouterr()
        assert "bundled-x" in captured.out
        assert "agent-x" not in captured.out

    def test_list_json_output(self, state_home, capsys):
        _seed(skill_id="x", scope="global")
        main(["list", "--json"])
        captured = capsys.readouterr()
        # Parses as JSON.
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert any(s["id"] == "x" for s in data)


# ── view ──────────────────────────────────────────────────────────────


class TestViewCommand:
    def test_view_existing_skill(self, state_home, capsys):
        _seed(skill_id="my-skill", scope="global", description="A test")
        rc = main(["view", "my-skill"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "my-skill" in captured.out
        assert "A test" in captured.out
        # Procedure body included.
        assert "Procedure body for my-skill" in captured.out

    def test_view_unknown_skill_returns_1(self, state_home, capsys):
        rc = main(["view", "does-not-exist"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "not found" in captured.err.lower()

    def test_view_json_output(self, state_home, capsys):
        _seed(skill_id="json-x", scope="global", description="JSON view")
        main(["view", "json-x", "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == "json-x"
        assert data["description"] == "JSON view"
        assert data["_resolved_scope"] == "global"


# ── pin / unpin ───────────────────────────────────────────────────────


class TestPinUnpinCommands:
    def test_pin_changes_state(self, state_home, capsys):
        _seed(skill_id="x", scope="global", pinned=False)
        rc = main(["pin", "x"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "pinned" in captured.out
        # Persisted on disk.
        from resonant_client.orchestration.skills import load_skill
        assert load_skill("x").pinned is True

    def test_unpin_changes_state(self, state_home, capsys):
        _seed(skill_id="x", scope="global", pinned=True)
        rc = main(["unpin", "x"])
        from resonant_client.orchestration.skills import load_skill
        assert load_skill("x").pinned is False

    def test_pin_unknown_skill_fails(self, state_home, capsys):
        rc = main(["pin", "missing"])
        assert rc == 1


# ── archive ──────────────────────────────────────────────────────────


class TestArchiveCommand:
    def test_archive_agent_skill_succeeds(self, state_home, capsys):
        _seed(skill_id="agent-x", scope="global", created_by="agent")
        rc = main(["archive", "agent-x", "--reason", "obsolete"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Archived" in captured.out

    def test_archive_bundled_skill_refused(self, state_home, capsys):
        _seed(skill_id="bundled-x", scope="global", created_by="bundled")
        rc = main(["archive", "bundled-x"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "Refused" in captured.err

    def test_archive_pinned_agent_skill_refused(self, state_home, capsys):
        _seed(skill_id="pinned-x", scope="global", created_by="agent", pinned=True)
        rc = main(["archive", "pinned-x"])
        assert rc == 2

    def test_archive_unknown_skill_fails(self, state_home, capsys):
        rc = main(["archive", "missing"])
        assert rc == 1


# ── curate ────────────────────────────────────────────────────────────


class TestCurateCommand:
    def test_curate_requires_project_path(self, state_home, capsys):
        rc = main(["curate"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "requires --project-path" in captured.err

    def test_curate_runs_pass_archives_stale(self, state_home, project_dir, capsys):
        # Stale agent skill in this project.
        _seed(
            skill_id="stale", scope="project", project_path=project_dir,
            created_by="agent", last_used_days_ago=100,
        )
        rc = main(["curate", str(project_dir)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Archived: 1" in captured.out
        assert "stale" in captured.out

    def test_curate_dry_run_does_not_archive(self, state_home, project_dir, capsys):
        _seed(
            skill_id="dry", scope="project", project_path=project_dir,
            created_by="agent", last_used_days_ago=100,
        )
        rc = main(["curate", str(project_dir), "--dry-run"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "dry-run" in captured.out
        # Skill still on disk.
        assert skill_dir("dry", scope="project", project_path=str(project_dir)).exists()

    def test_curate_via_flag_path(self, state_home, project_dir, capsys):
        # Flag form: --project-path instead of positional.
        _seed(
            skill_id="flag", scope="project", project_path=project_dir,
            created_by="agent", last_used_days_ago=100,
        )
        rc = main(["curate", "--project-path", str(project_dir)])
        assert rc == 0
