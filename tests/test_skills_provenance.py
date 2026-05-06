"""Tests for v0.6.0a1 — skill provenance + pinning + bundled-skill installation.

The pre-v0.6 skills.py module had no concept of "who created this skill" —
all skills were equal, all candidates for auto-deprecation. v0.6.0a1 adds
the `created_by` provenance gate (bundled / agent / user) + the `pinned`
durability flag, which together let the curator (v0.6.0a3) safely touch
ONLY agent-created skills while leaving the user's hand-curated and
shipped reference skills alone.

Covered:
- Skill dataclass: new `created_by` + `pinned` fields default correctly.
- Skill.is_curator_touchable: provenance gate (only "agent" + not pinned).
- Skill.is_deprecated: pinned skills are exempt.
- archive_skill: refuses non-touchable skills, writes _archive_reason.txt.
- list_skills_filtered: created_by + pinned filters.
- set_pinned: round-trip pin/unpin via skill.json mutation.
- Bundled skill installation: idempotent, force=True override, frontmatter
  parsing, created_by="bundled" forced regardless of frontmatter.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from resonant_client.orchestration.bundled_skills import (
    _parse_frontmatter,
    bundled_skill_ids,
    install_bundled_skills,
)
from resonant_client.orchestration.skills import (
    Skill,
    archive_skill,
    list_skills_filtered,
    load_skill,
    save_skill,
    set_pinned,
)


# ── State-home fixture so tests don't pollute the real ~/.resonant ──────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


# ── New Skill dataclass fields ──────────────────────────────────────────


class TestSkillProvenanceFields:
    def test_default_created_by_is_agent(self):
        # Default for backward compatibility — pre-v0.6 saves came from
        # the auto-extractor, which was de facto "agent" provenance.
        s = Skill(id="x", name="x", description="x")
        assert s.created_by == "agent"

    def test_default_pinned_is_false(self):
        s = Skill(id="x", name="x", description="x")
        assert s.pinned is False

    def test_created_by_round_trips_through_json(self, state_home):
        s = Skill(id="bundled-x", name="x", description="x", created_by="bundled")
        save_skill(s)
        loaded = load_skill("bundled-x")
        assert loaded.created_by == "bundled"

    def test_pinned_round_trips_through_json(self, state_home):
        s = Skill(id="pinned-x", name="x", description="x", pinned=True)
        save_skill(s)
        loaded = load_skill("pinned-x")
        assert loaded.pinned is True

    def test_from_dict_handles_pre_v06_skills(self):
        # Old skill.json files won't have created_by/pinned. The
        # dataclass should tolerate that via the from_dict allowed-
        # field filter + dataclass defaults.
        legacy_payload = {
            "id": "old", "name": "Old", "description": "pre-v0.6",
            # No created_by, no pinned.
        }
        s = Skill.from_dict(legacy_payload)
        assert s.id == "old"
        # Defaults kick in.
        assert s.created_by == "agent"
        assert s.pinned is False


# ── is_curator_touchable gate ──────────────────────────────────────────


class TestIsCuratorTouchable:
    def test_agent_unpinned_is_touchable(self):
        s = Skill(id="x", name="x", description="x", created_by="agent", pinned=False)
        assert s.is_curator_touchable() is True

    def test_agent_pinned_is_NOT_touchable(self):
        s = Skill(id="x", name="x", description="x", created_by="agent", pinned=True)
        assert s.is_curator_touchable() is False

    def test_bundled_is_NOT_touchable(self):
        s = Skill(id="x", name="x", description="x", created_by="bundled", pinned=False)
        assert s.is_curator_touchable() is False

    def test_user_is_NOT_touchable(self):
        s = Skill(id="x", name="x", description="x", created_by="user", pinned=False)
        assert s.is_curator_touchable() is False

    def test_unknown_provenance_is_NOT_touchable(self):
        # Defensive: anything other than exactly "agent" is off-limits.
        s = Skill(id="x", name="x", description="x", created_by="external-tool")
        assert s.is_curator_touchable() is False


# ── is_deprecated pinned exemption ──────────────────────────────────────


class TestIsDeprecatedPinnedExemption:
    def test_pinned_skill_with_old_last_used_is_NOT_deprecated(self):
        # 100 days ago; without pinning this would be deprecated.
        old_ts = time.time() - (100 * 86400)
        s = Skill(
            id="x", name="x", description="x",
            last_used_at=old_ts, pinned=True,
        )
        assert s.is_deprecated() is False

    def test_pinned_skill_with_high_fail_rate_is_NOT_deprecated(self):
        # 80% fail rate, well above the 50% threshold; pinned protects.
        s = Skill(
            id="x", name="x", description="x",
            success_count=2, fail_count=8, pinned=True,
        )
        assert s.is_deprecated() is False

    def test_unpinned_old_skill_IS_deprecated(self):
        # Sanity: pinning is the only thing keeping the old + failing
        # skill alive.
        old_ts = time.time() - (100 * 86400)
        s = Skill(
            id="x", name="x", description="x",
            last_used_at=old_ts, pinned=False,
        )
        assert s.is_deprecated() is True


# ── archive_skill (curator-driven) ─────────────────────────────────────


class TestArchiveSkill:
    def test_archives_agent_skill(self, state_home):
        s = Skill(id="agent-x", name="x", description="x", created_by="agent")
        save_skill(s)
        dest = archive_skill(s, reason="merged into umbrella-x")
        assert dest is not None
        assert dest.exists()
        # Goes under _archive/ (NOT _deprecated/).
        assert "_archive" in str(dest)
        # Reason file written.
        reason_file = dest / "_archive_reason.txt"
        assert reason_file.exists()
        assert "merged into umbrella-x" in reason_file.read_text()

    def test_refuses_to_archive_bundled_skill(self, state_home):
        s = Skill(id="bundled-x", name="x", description="x", created_by="bundled")
        save_skill(s)
        dest = archive_skill(s, reason="curator misbehaved")
        assert dest is None
        # Source still exists — refusal is silent + safe.
        from resonant_client.orchestration.skills import skill_dir
        assert skill_dir("bundled-x").exists()

    def test_refuses_to_archive_user_skill(self, state_home):
        s = Skill(id="user-x", name="x", description="x", created_by="user")
        save_skill(s)
        dest = archive_skill(s)
        assert dest is None

    def test_refuses_to_archive_pinned_agent_skill(self, state_home):
        s = Skill(
            id="pinned-agent-x", name="x", description="x",
            created_by="agent", pinned=True,
        )
        save_skill(s)
        dest = archive_skill(s, reason="should be refused")
        assert dest is None

    def test_archive_without_reason_does_not_write_reason_file(self, state_home):
        s = Skill(id="agent-y", name="y", description="y", created_by="agent")
        save_skill(s)
        dest = archive_skill(s)  # no reason
        assert dest is not None
        assert not (dest / "_archive_reason.txt").exists()

    def test_archive_missing_skill_returns_none(self, state_home):
        s = Skill(id="never-saved", name="x", description="x", created_by="agent")
        # NOT saved — source dir doesn't exist.
        assert archive_skill(s) is None


# ── list_skills_filtered ───────────────────────────────────────────────


class TestListSkillsFiltered:
    def _seed(self, state_home):
        save_skill(Skill(id="bundled-1", name="b1", description="b",
                         created_by="bundled"))
        save_skill(Skill(id="agent-1", name="a1", description="a",
                         created_by="agent"))
        save_skill(Skill(id="agent-pinned", name="ap", description="ap",
                         created_by="agent", pinned=True))
        save_skill(Skill(id="user-1", name="u1", description="u",
                         created_by="user"))

    def test_filter_by_created_by_agent(self, state_home):
        self._seed(state_home)
        agent_skills = list_skills_filtered(created_by="agent")
        ids = sorted(s.id for s in agent_skills)
        assert ids == ["agent-1", "agent-pinned"]

    def test_filter_by_created_by_bundled(self, state_home):
        self._seed(state_home)
        bundled = list_skills_filtered(created_by="bundled")
        ids = [s.id for s in bundled]
        assert ids == ["bundled-1"]

    def test_filter_by_pinned_true(self, state_home):
        self._seed(state_home)
        pinned = list_skills_filtered(pinned=True)
        ids = [s.id for s in pinned]
        assert ids == ["agent-pinned"]

    def test_filter_by_pinned_false(self, state_home):
        self._seed(state_home)
        unpinned = list_skills_filtered(pinned=False)
        ids = sorted(s.id for s in unpinned)
        assert ids == ["agent-1", "bundled-1", "user-1"]

    def test_compound_filter_agent_unpinned(self, state_home):
        # The curator's exact target: agent + not pinned = curator_touchable.
        self._seed(state_home)
        targets = list_skills_filtered(created_by="agent", pinned=False)
        ids = [s.id for s in targets]
        assert ids == ["agent-1"]
        # Sanity: matches is_curator_touchable.
        assert all(s.is_curator_touchable() for s in targets)


# ── set_pinned ─────────────────────────────────────────────────────────


class TestSetPinned:
    def test_pin_unpinned_skill(self, state_home):
        s = Skill(id="x", name="x", description="x", pinned=False)
        save_skill(s)
        updated = set_pinned("x", True)
        assert updated is not None
        assert updated.pinned is True
        # Persisted on disk.
        loaded = load_skill("x")
        assert loaded.pinned is True

    def test_unpin_pinned_skill(self, state_home):
        s = Skill(id="x", name="x", description="x", pinned=True)
        save_skill(s)
        updated = set_pinned("x", False)
        assert updated.pinned is False
        loaded = load_skill("x")
        assert loaded.pinned is False

    def test_set_pinned_on_missing_skill_returns_none(self, state_home):
        result = set_pinned("never-existed", True)
        assert result is None


# ── Bundled-skill frontmatter parsing ──────────────────────────────────


class TestParseFrontmatter:
    def test_extracts_yaml_frontmatter_and_body(self):
        text = "---\nname: Test\nversion: 2.0.0\n---\nbody content here"
        fm, body = _parse_frontmatter(text)
        assert fm == {"name": "Test", "version": "2.0.0"}
        assert body == "body content here"

    def test_no_frontmatter_returns_empty_dict_and_full_text(self):
        text = "just markdown body"
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_strips_quoted_values(self):
        text = '---\nname: "Quoted Name"\ndescription: \'single quoted\'\n---\nbody'
        fm, _ = _parse_frontmatter(text)
        assert fm["name"] == "Quoted Name"
        assert fm["description"] == "single quoted"

    def test_parses_booleans(self):
        text = "---\npinned: true\nstale: false\n---\nbody"
        fm, _ = _parse_frontmatter(text)
        assert fm["pinned"] is True
        assert fm["stale"] is False

    def test_parses_lists(self):
        text = "---\ntriggers: [a, b, c]\n---\nbody"
        fm, _ = _parse_frontmatter(text)
        assert fm["triggers"] == ["a", "b", "c"]

    def test_parses_quoted_list_items(self):
        text = '---\ntriggers: ["with spaces", \'also\']\n---\nbody'
        fm, _ = _parse_frontmatter(text)
        assert fm["triggers"] == ["with spaces", "also"]

    def test_empty_list_returns_empty(self):
        text = "---\ntriggers: []\n---\nbody"
        fm, _ = _parse_frontmatter(text)
        assert fm["triggers"] == []


# ── install_bundled_skills ─────────────────────────────────────────────


class TestInstallBundledSkills:
    def test_first_run_installs_all_bundled(self, state_home):
        installed = install_bundled_skills()
        # Two reference skills shipped with v0.6.0a1.
        assert len(installed) >= 2
        # All have created_by="bundled" (forced regardless of frontmatter).
        assert all(s.created_by == "bundled" for s in installed)

    def test_bundled_skill_ids_are_stable(self, state_home):
        ids = bundled_skill_ids()
        assert "rigorous-grill-spec-refinement" in ids
        assert "autonomous-mission-iter-discipline" in ids

    def test_idempotent_second_call_skips_existing(self, state_home):
        first = install_bundled_skills()
        second = install_bundled_skills()
        # First install returns N; second returns 0 (all already there).
        assert len(second) == 0
        assert len(first) >= 1

    def test_force_reinstalls_existing(self, state_home):
        install_bundled_skills()
        forced = install_bundled_skills(force=True)
        # With force=True, every bundled skill reinstalls.
        assert len(forced) == len(bundled_skill_ids())

    def test_installed_skill_has_procedure_md(self, state_home):
        install_bundled_skills()
        from resonant_client.orchestration.skills import skill_dir
        target = skill_dir("rigorous-grill-spec-refinement")
        procedure = target / "procedure.md"
        assert procedure.exists()
        body = procedure.read_text(encoding="utf-8")
        # Sanity: the body content is preserved.
        assert "5-beat" in body or "Acknowledge" in body

    def test_installed_skill_has_tokens_for_matching(self, state_home):
        install_bundled_skills()
        skill = load_skill("rigorous-grill-spec-refinement")
        # Tokens populated from name + description + triggers + body sample.
        assert len(skill.tokens) > 5
        # Should include keywords from the body.
        assert "grill" in skill.tokens or "spec" in skill.tokens

    def test_installed_pinned_skill_is_pinned(self, state_home):
        # Both bundled reference skills have pinned: true in their
        # frontmatter — they're durable defaults.
        install_bundled_skills()
        skill = load_skill("rigorous-grill-spec-refinement")
        assert skill.pinned is True

    def test_bundled_skill_not_curator_touchable(self, state_home):
        # The whole point of bundled provenance: curator can't touch it.
        install_bundled_skills()
        skill = load_skill("rigorous-grill-spec-refinement")
        assert skill.is_curator_touchable() is False
