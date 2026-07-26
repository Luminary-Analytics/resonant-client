"""Tests for v0.6.1a3 — skill promote/demote + CLI auto-install.

Three things ship in this alpha:
1. promote_skill: project → global elevation.
2. demote_skill: global → project (the inverse).
3. CLI now auto-installs bundled skills on every invocation
   (idempotent, cheap), so `resonant-skill list --created-by bundled`
   works out of the box without a manual install step.
"""
from __future__ import annotations


import pytest

from resonant_client.orchestration.skill_cli import main as cli_main
from resonant_client.orchestration.skills import (
    Skill,
    demote_skill,
    load_skill,
    promote_skill,
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


def _seed(*, skill_id, scope, project_path=None, created_by="agent",
          pinned=False, procedure_md="default body", verification_md=""):
    s = Skill(
        id=skill_id,
        name=skill_id.replace("-", " ").title(),
        description=f"Test {skill_id}",
        scope=scope,
        created_by=created_by,
        pinned=pinned,
        success_count=3,
        fail_count=1,
        triggers=["x", "y"],
        tokens=["alpha", "beta"],
    )
    save_skill(
        s,
        procedure_md=procedure_md,
        verification_md=verification_md,
        project_path=str(project_path) if project_path else None,
    )
    return s


# ── promote_skill (project → global) ──────────────────────────────────


class TestPromoteSkill:
    def test_promotes_project_skill_to_global(self, state_home, project_dir):
        _seed(skill_id="x", scope="project", project_path=project_dir)
        promoted = promote_skill("x", project_path=str(project_dir))
        assert promoted is not None
        assert promoted.scope == "global"
        # Now exists in global scope.
        assert load_skill("x", scope="global") is not None

    def test_default_archives_project_copy(self, state_home, project_dir):
        _seed(skill_id="x", scope="project", project_path=project_dir)
        promote_skill("x", project_path=str(project_dir))
        # Project source moved to _deprecated/.
        assert not skill_dir(
            "x", scope="project", project_path=str(project_dir),
        ).exists()

    def test_keep_project_copy_preserves_source(self, state_home, project_dir):
        _seed(skill_id="x", scope="project", project_path=project_dir)
        promote_skill(
            "x", project_path=str(project_dir),
            keep_project_copy=True,
        )
        # Both copies on disk.
        assert load_skill("x", scope="global") is not None
        assert load_skill(
            "x", scope="project", project_path=str(project_dir),
        ) is not None

    def test_preserves_provenance_and_pinned(self, state_home, project_dir):
        _seed(
            skill_id="pinned-user", scope="project", project_path=project_dir,
            created_by="user", pinned=True,
        )
        promoted = promote_skill(
            "pinned-user", project_path=str(project_dir),
        )
        assert promoted.created_by == "user"
        assert promoted.pinned is True

    def test_preserves_procedure_md(self, state_home, project_dir):
        _seed(
            skill_id="x", scope="project", project_path=project_dir,
            procedure_md="# x\n\nProject body.",
        )
        promote_skill("x", project_path=str(project_dir))
        global_proc = skill_dir("x", scope="global") / "procedure.md"
        assert global_proc.read_text(encoding="utf-8") == "# x\n\nProject body."

    def test_refuses_bundled_skill(self, state_home, project_dir):
        # Bundled skills shouldn't be in project scope to begin with,
        # but just in case — refuse to promote.
        _seed(
            skill_id="b", scope="project", project_path=project_dir,
            created_by="bundled",
        )
        result = promote_skill("b", project_path=str(project_dir))
        assert result is None
        # Project source still on disk.
        assert load_skill(
            "b", scope="project", project_path=str(project_dir),
        ) is not None

    def test_refuses_when_global_collision(self, state_home, project_dir):
        # Same id already exists in global → refuse the promotion.
        _seed(skill_id="dupe", scope="global")
        _seed(skill_id="dupe", scope="project", project_path=project_dir)
        result = promote_skill("dupe", project_path=str(project_dir))
        assert result is None
        # Project source untouched.
        assert load_skill(
            "dupe", scope="project", project_path=str(project_dir),
        ) is not None

    def test_missing_source_returns_none(self, state_home, project_dir):
        result = promote_skill("never-existed", project_path=str(project_dir))
        assert result is None


# ── demote_skill (global → project) ───────────────────────────────────


class TestDemoteSkill:
    def test_demotes_global_skill_to_project(self, state_home, project_dir):
        _seed(skill_id="x", scope="global")
        demoted = demote_skill("x", target_project_path=str(project_dir))
        assert demoted is not None
        assert demoted.scope == "project"
        assert load_skill(
            "x", scope="project", project_path=str(project_dir),
        ) is not None

    def test_default_archives_global_copy(self, state_home, project_dir):
        _seed(skill_id="x", scope="global")
        demote_skill("x", target_project_path=str(project_dir))
        assert not skill_dir("x", scope="global").exists()

    def test_keep_global_copy_preserves_source(self, state_home, project_dir):
        _seed(skill_id="x", scope="global")
        demote_skill(
            "x", target_project_path=str(project_dir),
            keep_global_copy=True,
        )
        assert load_skill("x", scope="global") is not None
        assert load_skill(
            "x", scope="project", project_path=str(project_dir),
        ) is not None

    def test_refuses_bundled_skill(self, state_home, project_dir):
        _seed(skill_id="b", scope="global", created_by="bundled")
        result = demote_skill("b", target_project_path=str(project_dir))
        assert result is None
        assert load_skill("b", scope="global") is not None

    def test_refuses_when_project_collision(self, state_home, project_dir):
        _seed(skill_id="dupe", scope="global")
        _seed(skill_id="dupe", scope="project", project_path=project_dir)
        result = demote_skill("dupe", target_project_path=str(project_dir))
        assert result is None

    def test_missing_source_returns_none(self, state_home, project_dir):
        result = demote_skill("nope", target_project_path=str(project_dir))
        assert result is None


# ── CLI promote/demote ────────────────────────────────────────────────


class TestPromoteDemoteCLI:
    def test_cli_promote_succeeds(self, state_home, project_dir, capsys):
        _seed(skill_id="x", scope="project", project_path=project_dir)
        rc = cli_main(["promote", "x", "--project-path", str(project_dir)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Promoted" in captured.out
        assert "global scope" in captured.out

    def test_cli_promote_keep_flag(self, state_home, project_dir, capsys):
        _seed(skill_id="x", scope="project", project_path=project_dir)
        rc = cli_main([
            "promote", "x", "--project-path", str(project_dir), "--keep",
        ])
        captured = capsys.readouterr()
        assert rc == 0
        assert "kept in place" in captured.out

    def test_cli_promote_refused_returns_2(self, state_home, project_dir, capsys):
        # Collision → exit 2.
        _seed(skill_id="dupe", scope="global")
        _seed(skill_id="dupe", scope="project", project_path=project_dir)
        rc = cli_main(["promote", "dupe", "--project-path", str(project_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "Could not promote" in captured.err

    def test_cli_demote_succeeds(self, state_home, project_dir, capsys):
        _seed(skill_id="x", scope="global")
        rc = cli_main(["demote", "x", "--project-path", str(project_dir)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Demoted" in captured.out

    def test_cli_demote_refused_returns_2(self, state_home, project_dir, capsys):
        _seed(skill_id="b", scope="global", created_by="bundled")
        rc = cli_main(["demote", "b", "--project-path", str(project_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "Could not demote" in captured.err


# ── CLI auto-install bundled skills ───────────────────────────────────


class TestAutoInstallBundled:
    def test_first_cli_run_installs_bundled(self, state_home, capsys):
        # Fresh state_home: no skills exist. Running the CLI should
        # install the bundled ones automatically.
        cli_main(["list", "--created-by", "bundled"])
        captured = capsys.readouterr()
        # Both bundled skills now appear in the listing.
        assert "rigorous-grill-spec-refinement" in captured.out
        assert "autonomous-mission-iter-discipline" in captured.out

    def test_cli_run_idempotent(self, state_home):
        # Running the CLI multiple times shouldn't duplicate or
        # re-install bundled skills.
        cli_main(["list"])
        cli_main(["list"])
        # Each bundled skill exists exactly once on disk.
        from resonant_client.orchestration.bundled_skills import bundled_skill_ids
        for sid in bundled_skill_ids():
            target = skill_dir(sid, scope="global")
            assert target.exists()
            assert (target / "skill.json").exists()
