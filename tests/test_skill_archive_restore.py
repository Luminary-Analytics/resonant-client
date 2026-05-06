"""Tests for v0.6.2a4 — archive list + restore.

The user-facing surface is `resonant-skill list --archived` +
`resonant-skill restore <id>`. These tests cover both the underlying
public API (`list_archived_skills`, `restore_skill`) and the CLI
wiring on top of it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from resonant_client.orchestration.skills import (
    Skill,
    archive_skill,
    list_archived_skills,
    load_skill,
    restore_skill,
    save_skill,
    skill_dir,
)
from resonant_client.orchestration.skill_cli import build_parser, main


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "fakeproj"
    p.mkdir()
    return p


def _seed_archivable(*, skill_id: str, project_path=None, scope: str = "global") -> Skill:
    """Save a skill that is curator-touchable (agent + not pinned)."""
    s = Skill(
        id=skill_id,
        name=skill_id,
        description=f"Test {skill_id}",
        scope=scope,
        created_by="agent",
        pinned=False,
        last_used_at=time.time(),
        success_count=1,
    )
    save_skill(
        s,
        procedure_md=f"# {skill_id}\n\nBody for {skill_id}.",
        project_path=str(project_path) if project_path else None,
    )
    return s


# ── list_archived_skills ──────────────────────────────────────────────


class TestListArchivedSkills:
    def test_empty_state_returns_empty_list(self, state_home):
        assert list_archived_skills() == []

    def test_archived_skill_appears(self, state_home):
        s = _seed_archivable(skill_id="alpha")
        archive_skill(s, reason="testing")
        entries = list_archived_skills()
        assert len(entries) == 1
        e = entries[0]
        assert e["skill"].id == "alpha"
        assert e["scope"] == "global"
        assert e["reason"] == "testing"
        assert isinstance(e["archived_at"], int)
        assert e["archive_dir"].exists()

    def test_most_recent_first(self, state_home):
        # Two archives in sequence with distinct ts. Use sleep_safe by
        # bumping mtime via the underlying ts arithmetic (archive_skill
        # uses `int(time.time())` so 1s sleep is enough).
        s1 = _seed_archivable(skill_id="first")
        archive_skill(s1, reason="r1")
        time.sleep(1.1)
        s2 = _seed_archivable(skill_id="second")
        archive_skill(s2, reason="r2")
        entries = list_archived_skills()
        assert [e["skill"].id for e in entries] == ["second", "first"]

    def test_filter_by_scope(self, state_home, project_dir):
        s_global = _seed_archivable(skill_id="g-x", scope="global")
        archive_skill(s_global, reason="global archive")
        s_proj = _seed_archivable(skill_id="p-x", scope="project", project_path=project_dir)
        archive_skill(s_proj, project_path=str(project_dir), reason="proj archive")
        global_only = list_archived_skills(scope="global")
        assert all(e["scope"] == "global" for e in global_only)
        assert any(e["skill"].id == "g-x" for e in global_only)
        proj_only = list_archived_skills(scope="project")
        assert all(e["scope"] == "project" for e in proj_only)
        assert any(e["skill"].id == "p-x" for e in proj_only)

    def test_malformed_archive_dir_skipped(self, state_home):
        # Create a directory with the wrong shape inside _archive/global
        from resonant_client.orchestration.skills import _skills_root
        bad = _skills_root() / "_archive" / "global" / "no-double-underscore"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "skill.json").write_text("{}", encoding="utf-8")
        entries = list_archived_skills()
        # Should be empty (or at least not contain the malformed entry).
        assert all(e["archive_dir"] != bad for e in entries)

    def test_missing_skill_json_skipped(self, state_home):
        from resonant_client.orchestration.skills import _skills_root
        bad = _skills_root() / "_archive" / "global" / "1700000000__incomplete"
        bad.mkdir(parents=True, exist_ok=True)
        # No skill.json at all
        entries = list_archived_skills()
        assert all(e["archive_dir"] != bad for e in entries)


# ── restore_skill ─────────────────────────────────────────────────────


class TestRestoreSkill:
    def test_restore_unknown_returns_none(self, state_home):
        assert restore_skill("does-not-exist") is None

    def test_restore_brings_back_live_skill(self, state_home):
        s = _seed_archivable(skill_id="x")
        archive_skill(s, reason="testing")
        # Live skill is gone now.
        assert load_skill("x") is None
        # Restore.
        dest = restore_skill("x")
        assert dest is not None
        assert dest.exists()
        # And it's loadable again.
        restored = load_skill("x")
        assert restored is not None
        assert restored.id == "x"

    def test_restore_drops_archive_reason_sidecar(self, state_home):
        s = _seed_archivable(skill_id="y")
        archive_skill(s, reason="should-not-survive-restore")
        dest = restore_skill("y")
        assert dest is not None
        assert not (dest / "_archive_reason.txt").exists()

    def test_restore_refuses_when_live_exists(self, state_home):
        s = _seed_archivable(skill_id="z")
        archive_skill(s, reason="test")
        # Re-seed a fresh live skill with same id
        _seed_archivable(skill_id="z")
        # Now restore should refuse without force
        result = restore_skill("z")
        assert result is None

    def test_restore_force_overwrites(self, state_home):
        s = _seed_archivable(skill_id="zz")
        archive_skill(s, reason="test")
        # Re-seed fresh live skill of same id with a different description
        live = Skill(
            id="zz", name="zz", description="LIVE VERSION",
            scope="global", created_by="agent", pinned=False,
            last_used_at=time.time(), success_count=99,
        )
        save_skill(live, procedure_md="# Live body")
        # Force restore
        dest = restore_skill("zz", force=True)
        assert dest is not None
        # Confirm the restored skill has the ARCHIVED success_count, not the live one
        restored = load_skill("zz")
        assert restored is not None
        assert restored.success_count == 1  # archived had 1, live had 99

    def test_most_recent_archive_wins(self, state_home):
        s1 = _seed_archivable(skill_id="dup")
        archive_skill(s1, reason="archive-1")
        time.sleep(1.1)
        s2 = _seed_archivable(skill_id="dup")
        # Modify s2 so we can tell archives apart on restore
        from resonant_client.orchestration.skills import save_skill
        s2.description = "second-archive-version"
        save_skill(s2, procedure_md="# v2 body")
        archive_skill(s2, reason="archive-2")
        # Restore should bring back the SECOND (most recent) archive.
        restore_skill("dup")
        restored = load_skill("dup")
        assert restored is not None
        assert restored.description == "second-archive-version"


# ── CLI: list --archived ──────────────────────────────────────────────


class TestCliListArchived:
    def test_archived_flag_in_parser(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--archived"])
        assert args.archived is True

    def test_list_archived_empty(self, state_home, capsys):
        rc = main(["list", "--archived"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "No archived skills" in captured.out

    def test_list_archived_shows_archived(self, state_home, capsys):
        s = _seed_archivable(skill_id="show-arch")
        archive_skill(s, reason="just because")
        rc = main(["list", "--archived"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "show-arch" in captured.out
        assert "just because" in captured.out

    def test_list_archived_json(self, state_home, capsys):
        s = _seed_archivable(skill_id="json-arch")
        archive_skill(s, reason="json-test")
        rc = main(["list", "--archived", "--json"])
        captured = capsys.readouterr()
        assert rc == 0
        data = json.loads(captured.out)
        assert any(e["id"] == "json-arch" for e in data)
        # JSON includes the archive metadata + the embedded skill
        for e in data:
            if e["id"] == "json-arch":
                assert e["reason"] == "json-test"
                assert "skill" in e
                assert e["skill"]["id"] == "json-arch"


# ── CLI: restore ──────────────────────────────────────────────────────


class TestCliRestore:
    def test_restore_subcommand_in_parser(self):
        parser = build_parser()
        args = parser.parse_args(["restore", "my-skill"])
        assert args.command == "restore"
        assert args.skill_id == "my-skill"
        assert args.force is False

    def test_restore_with_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["restore", "my-skill", "--force"])
        assert args.force is True

    def test_restore_unknown_returns_1(self, state_home, capsys):
        rc = main(["restore", "no-such-skill"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "No archive found" in captured.err

    def test_restore_succeeds_for_existing_archive(self, state_home, capsys):
        s = _seed_archivable(skill_id="arch-x")
        archive_skill(s, reason="test")
        # Live skill is gone after archive.
        assert load_skill("arch-x") is None
        rc = main(["restore", "arch-x"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Restored arch-x" in captured.out
        # Skill is back.
        assert load_skill("arch-x") is not None

    def test_restore_refused_when_live_exists(self, state_home, capsys):
        s = _seed_archivable(skill_id="conflict")
        archive_skill(s, reason="test")
        # Re-seed live skill
        _seed_archivable(skill_id="conflict")
        rc = main(["restore", "conflict"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "live skill" in captured.err
        assert "--force" in captured.err

    def test_restore_force_succeeds(self, state_home, capsys):
        s = _seed_archivable(skill_id="conflict-2")
        archive_skill(s, reason="test")
        _seed_archivable(skill_id="conflict-2")
        rc = main(["restore", "conflict-2", "--force"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Restored" in captured.out
