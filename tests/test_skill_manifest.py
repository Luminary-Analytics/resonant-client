"""Tests for `.resonant/skills.toml` manifest reader/writer + status check."""

from __future__ import annotations

import time

import pytest

from resonant_client.orchestration import (
    Skill,
    SkillManifest,
    SkillRequirement,
    check_manifest_status,
    manifest_path,
    read_manifest,
    save_current_skill_set,
    save_skill,
    write_manifest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


# ── Parsing ─────────────────────────────────────────────────────────────


def test_parse_at_version_syntax():
    r = SkillRequirement.parse("deploy-to-vercel@>=1.2")
    assert r.skill_id == "deploy-to-vercel"
    assert r.version_spec == ">=1.2"
    plain = SkillRequirement.parse("fix-imports")
    assert plain.skill_id == "fix-imports"
    assert plain.version_spec == ""


def test_read_manifest_returns_none_when_absent(project_dir):
    assert read_manifest(project_dir) is None


def test_read_manifest_round_trip(project_dir):
    manifest = SkillManifest(
        required=[
            SkillRequirement(skill_id="fix-imports"),
            SkillRequirement(skill_id="deploy-vercel", version_spec=">=1.2"),
        ],
        optional=[SkillRequirement(skill_id="extra-thing")],
        auto_install=False,
        warn_on_missing=True,
    )
    write_manifest(project_dir, manifest)
    loaded = read_manifest(project_dir)
    assert loaded is not None
    assert {r.skill_id for r in loaded.required} == {"fix-imports", "deploy-vercel"}
    assert loaded.required[1].version_spec == ">=1.2"
    assert {r.skill_id for r in loaded.optional} == {"extra-thing"}
    assert loaded.auto_install is False
    assert loaded.warn_on_missing is True


def test_manifest_path_uses_dotresonant_subdir(project_dir):
    path = manifest_path(project_dir)
    assert path.name == "skills.toml"
    assert path.parent.name == ".resonant"


# ── Status check ────────────────────────────────────────────────────────


def test_check_status_returns_none_manifest_when_absent(project_dir):
    status = check_manifest_status(project_dir)
    assert status.manifest is None
    assert status.installed == []


def test_check_status_flags_missing_required(state_home, project_dir):
    write_manifest(project_dir, SkillManifest(
        required=[SkillRequirement(skill_id="installed-skill"),
                  SkillRequirement(skill_id="missing-skill")],
    ))
    save_skill(Skill(id="installed-skill", name="ok", description="",
                     last_used_at=time.time()))
    status = check_manifest_status(project_dir)
    assert {s.id for s in status.installed} == {"installed-skill"}
    assert [r.skill_id for r in status.missing_required] == ["missing-skill"]
    assert status.has_gaps()


def test_check_status_no_gaps_when_all_installed(state_home, project_dir):
    write_manifest(project_dir, SkillManifest(
        required=[SkillRequirement(skill_id="all-good")],
    ))
    save_skill(Skill(id="all-good", name="ok", description="",
                     last_used_at=time.time()))
    status = check_manifest_status(project_dir)
    assert not status.has_gaps()
    assert status.missing_required == []


def test_check_status_separates_required_from_optional(state_home, project_dir):
    write_manifest(project_dir, SkillManifest(
        required=[SkillRequirement(skill_id="missing-required")],
        optional=[SkillRequirement(skill_id="missing-optional")],
    ))
    status = check_manifest_status(project_dir)
    assert [r.skill_id for r in status.missing_required] == ["missing-required"]
    assert [r.skill_id for r in status.missing_optional] == ["missing-optional"]


# ── save_current_skill_set ─────────────────────────────────────────────


def test_save_current_skill_set_writes_required_section(project_dir):
    save_current_skill_set(project_dir, used_skill_ids=["a", "b", "a"])
    manifest = read_manifest(project_dir)
    assert manifest is not None
    assert [r.skill_id for r in manifest.required] == ["a", "b"]  # deduped + sorted


def test_save_current_skill_set_preserves_existing_optional(project_dir):
    write_manifest(project_dir, SkillManifest(
        required=[SkillRequirement(skill_id="old")],
        optional=[SkillRequirement(skill_id="opt-survives")],
        auto_install=False,
    ))
    save_current_skill_set(project_dir, used_skill_ids=["new1", "new2"])
    manifest = read_manifest(project_dir)
    assert [r.skill_id for r in manifest.required] == ["new1", "new2"]
    assert [r.skill_id for r in manifest.optional] == ["opt-survives"]
    assert manifest.auto_install is False  # install settings preserved


# ── Robustness ──────────────────────────────────────────────────────────


def test_malformed_manifest_returns_none_not_crash(project_dir):
    path = manifest_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not valid TOML [[[\n", encoding="utf-8")
    assert read_manifest(project_dir) is None
