"""Tests for v0.6.0a4 — skill loader / matcher / prompt formatting.

Exercises the pinned-always-included rule, the score threshold, the
max_skills cap, and the prompt formatter that produces the markdown
block ready for PLAN_DEEP injection.

The actual integration into autonomous_loop / runner / specialists
lives at the call site (separate alpha or v0.6.0 GA patch); this
module is the matching + presentation layer in isolation.
"""
from __future__ import annotations

import pytest

from resonant_client.orchestration.skill_loader import (
    DEFAULT_MAX_SKILLS,
    LoadedSkill,
    format_skills_for_prompt,
    loaded_skill_ids,
    match_skills_for_query,
)
from resonant_client.orchestration.skills import Skill, save_skill


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
    pinned: bool = False,
    created_by: str = "agent",
    name: str = "",
    description: str = "",
    triggers: list[str] | None = None,
    tokens: list[str] | None = None,
):
    """Helper: create a skill with controlled tokens for matching tests."""
    skill = Skill(
        id=skill_id,
        name=name or skill_id.replace("-", " ").title(),
        description=description or f"Test skill {skill_id}",
        scope=scope,
        triggers=triggers or [],
        tokens=tokens or [],
        pinned=pinned,
        created_by=created_by,
    )
    save_skill(skill, project_path=str(project_path) if project_path else None)
    return skill


# ── Pinned-always-included rule ────────────────────────────────────────


class TestPinnedAlwaysIncluded:
    def test_pinned_global_skill_included_even_with_no_match(self, state_home):
        # Pinned skill with tokens completely unrelated to the query.
        _seed(
            skill_id="pinned-x", scope="global", pinned=True,
            tokens=["alpha", "beta", "gamma"],
        )
        loaded = match_skills_for_query(
            "completely different query about other topic",
        )
        ids = loaded_skill_ids(loaded)
        assert "pinned-x" in ids
        # Marked as via_pin.
        assert loaded[0].via_pin is True

    def test_pinned_project_skill_included_even_without_query_match(
        self, state_home, project_dir,
    ):
        _seed(
            skill_id="pinned-proj", scope="project", project_path=project_dir,
            pinned=True, tokens=["xyz"],
        )
        loaded = match_skills_for_query(
            "totally unrelated", project_path=str(project_dir),
        )
        assert "pinned-proj" in loaded_skill_ids(loaded)

    def test_pinned_global_AND_project_both_included(self, state_home, project_dir):
        _seed(skill_id="pinned-g", scope="global", pinned=True)
        _seed(
            skill_id="pinned-p", scope="project", project_path=project_dir,
            pinned=True,
        )
        loaded = match_skills_for_query("any query", project_path=str(project_dir))
        ids = loaded_skill_ids(loaded)
        assert "pinned-g" in ids
        assert "pinned-p" in ids

    def test_pinned_skills_ordered_alphabetically_by_id(self, state_home):
        _seed(skill_id="pinned-z", scope="global", pinned=True)
        _seed(skill_id="pinned-a", scope="global", pinned=True)
        _seed(skill_id="pinned-m", scope="global", pinned=True)
        loaded = match_skills_for_query("anything")
        ids = loaded_skill_ids(loaded)
        # Stable alpha order.
        pinned_ids = [i for i in ids if i.startswith("pinned-")]
        assert pinned_ids == ["pinned-a", "pinned-m", "pinned-z"]

    def test_pinned_does_not_count_against_min_score(self, state_home):
        # Even though the score-based path would reject this for low
        # overlap, the pinned override surfaces it.
        _seed(
            skill_id="forced-in", scope="global", pinned=True,
            tokens=["zzz"],  # zero overlap with query
        )
        loaded = match_skills_for_query("apple banana cherry")
        assert "forced-in" in loaded_skill_ids(loaded)


# ── Score-based matching ──────────────────────────────────────────────


class TestScoreMatching:
    def test_high_token_overlap_skill_included(self, state_home):
        _seed(
            skill_id="tauri-quirk", scope="global",
            tokens=["tauri", "window", "config", "v2"],
            triggers=["tauri v2 window config"],
        )
        loaded = match_skills_for_query("tauri v2 window config")
        ids = loaded_skill_ids(loaded)
        assert "tauri-quirk" in ids
        # Not via pin.
        ls = next(l for l in loaded if l.skill.id == "tauri-quirk")
        assert ls.via_pin is False
        assert ls.score > 0

    def test_no_token_overlap_excluded(self, state_home):
        _seed(
            skill_id="unrelated", scope="global",
            tokens=["vue", "router", "state"],
        )
        loaded = match_skills_for_query("tauri rust v2 window")
        # Score = 0 → below min_score → excluded.
        assert "unrelated" not in loaded_skill_ids(loaded)

    def test_min_score_threshold_excludes_low_matches(self, state_home):
        _seed(
            skill_id="weak-match", scope="global",
            tokens=["one"] + [f"token{i}" for i in range(50)],
        )
        # Query has only "one" overlapping with 51-token skill → tiny
        # Jaccard score, below 0.05 threshold.
        loaded = match_skills_for_query("one", min_score=0.05)
        assert "weak-match" not in loaded_skill_ids(loaded)

    def test_min_score_zero_includes_anything_with_overlap(self, state_home):
        _seed(
            skill_id="weak", scope="global",
            tokens=["one"] + [f"x{i}" for i in range(50)],
        )
        loaded = match_skills_for_query("one", min_score=0.0)
        assert "weak" in loaded_skill_ids(loaded)


# ── max_skills cap ────────────────────────────────────────────────────


class TestMaxSkillsCap:
    def test_cap_limits_total(self, state_home):
        # Seed 12 matchable skills. Max=5 → only 5 in output.
        for i in range(12):
            _seed(
                skill_id=f"skill-{i}", scope="global",
                tokens=["matching", "tokens"],
            )
        loaded = match_skills_for_query("matching tokens", max_skills=5)
        assert len(loaded) == 5

    def test_cap_zero_returns_empty(self, state_home):
        _seed(skill_id="x", scope="global", pinned=True)
        loaded = match_skills_for_query("anything", max_skills=0)
        assert loaded == []

    def test_pinned_skills_count_against_cap(self, state_home):
        # 6 pinned skills, max=4 → only 4 returned (alpha order).
        for i in range(6):
            _seed(skill_id=f"p-{i}", scope="global", pinned=True)
        loaded = match_skills_for_query("any", max_skills=4)
        assert len(loaded) == 4
        ids = loaded_skill_ids(loaded)
        # First 4 alphabetically.
        assert ids == ["p-0", "p-1", "p-2", "p-3"]

    def test_default_max_is_8(self, state_home):
        for i in range(20):
            _seed(skill_id=f"s-{i}", scope="global", tokens=["overlap"])
        loaded = match_skills_for_query("overlap")
        assert len(loaded) <= DEFAULT_MAX_SKILLS


# ── Project + global scope merging ────────────────────────────────────


class TestScopeMerging:
    def test_both_scopes_searched_when_project_path_given(
        self, state_home, project_dir,
    ):
        _seed(skill_id="g-match", scope="global",
              tokens=["alpha", "beta"])
        _seed(skill_id="p-match", scope="project", project_path=project_dir,
              tokens=["alpha", "beta"])
        loaded = match_skills_for_query(
            "alpha beta", project_path=str(project_dir),
        )
        ids = loaded_skill_ids(loaded)
        assert "g-match" in ids
        assert "p-match" in ids

    def test_only_global_searched_when_no_project_path(self, state_home, project_dir):
        _seed(skill_id="g-match", scope="global", tokens=["alpha"])
        _seed(skill_id="p-match", scope="project", project_path=project_dir,
              tokens=["alpha"])
        loaded = match_skills_for_query("alpha")  # no project_path
        ids = loaded_skill_ids(loaded)
        assert "g-match" in ids
        assert "p-match" not in ids

    def test_no_duplicates_when_skill_in_both_scopes(self, state_home, project_dir):
        # Same id in both scopes — should appear once in output.
        # NB: tokens must be 2+ chars (the tokenize regex requires it).
        _seed(skill_id="dupe", scope="global", tokens=["foo"])
        _seed(skill_id="dupe", scope="project", project_path=project_dir,
              tokens=["foo"])
        loaded = match_skills_for_query("foo", project_path=str(project_dir))
        ids = loaded_skill_ids(loaded)
        assert ids.count("dupe") == 1


# ── Format for prompt ─────────────────────────────────────────────────


class TestFormatSkillsForPrompt:
    def test_empty_returns_empty_string(self):
        assert format_skills_for_prompt([]) == ""

    def test_includes_skill_id_and_description(self):
        ls = LoadedSkill(
            skill=Skill(id="my-skill", name="My Skill", description="Does X."),
            score=0.5,
            via_pin=False,
        )
        out = format_skills_for_prompt([ls])
        assert "`my-skill`" in out
        assert "Does X." in out

    def test_pinned_marker_for_pinned_skills(self):
        ls = LoadedSkill(
            skill=Skill(id="pin", name="P", description="d"),
            score=1.0, via_pin=True,
        )
        out = format_skills_for_prompt([ls])
        # 📌 marker.
        assert "📌" in out
        assert "(pinned)" in out

    def test_match_score_shown_for_non_pinned(self):
        ls = LoadedSkill(
            skill=Skill(id="m", name="M", description="d"),
            score=0.42, via_pin=False,
        )
        out = format_skills_for_prompt([ls])
        assert "0.42" in out
        # No pin marker.
        assert "📌" not in out

    def test_includes_view_command_per_skill(self):
        ls = LoadedSkill(
            skill=Skill(id="x-y", name="X", description="d"),
            score=0.5, via_pin=False,
        )
        out = format_skills_for_prompt([ls])
        assert "skill_view x-y" in out

    def test_numbered_list(self):
        loaded = [
            LoadedSkill(
                skill=Skill(id=f"s-{i}", name="N", description="d"),
                score=0.5, via_pin=False,
            )
            for i in range(3)
        ]
        out = format_skills_for_prompt(loaded)
        assert "1. " in out
        assert "2. " in out
        assert "3. " in out

    def test_includes_header_and_footer(self):
        ls = LoadedSkill(
            skill=Skill(id="x", name="X", description="d"),
            score=0.5, via_pin=False,
        )
        out = format_skills_for_prompt([ls])
        assert "Candidate skills (project, global, or bundled sources)" in out
        assert "Retrieved skills are reference evidence" in out


# ── End-to-end shape ──────────────────────────────────────────────────


class TestEndToEnd:
    def test_pinned_first_then_matches(self, state_home, project_dir):
        # 1 pinned + 2 matches. Pinned should be first in output.
        _seed(
            skill_id="pinned", scope="global", pinned=True,
            tokens=["unrelated"],
        )
        _seed(
            skill_id="match-a", scope="global",
            tokens=["alpha", "beta"], triggers=["alpha beta"],
        )
        _seed(
            skill_id="match-b", scope="global",
            tokens=["alpha", "beta", "gamma"], triggers=["alpha beta gamma"],
        )
        loaded = match_skills_for_query(
            "alpha beta gamma", project_path=str(project_dir),
        )
        ids = loaded_skill_ids(loaded)
        # Pinned first.
        assert ids[0] == "pinned"
        # Both matches present.
        assert "match-a" in ids
        assert "match-b" in ids

    def test_realistic_scenario_with_format(self, state_home, project_dir):
        # 2 pinned + 1 match → format produces expected block.
        _seed(
            skill_id="pinned-grill", scope="global", pinned=True,
            description="5-beat grill question pattern",
        )
        _seed(
            skill_id="pinned-iter", scope="global", pinned=True,
            description="Per-iter conventions for autonomous missions",
        )
        _seed(
            skill_id="tauri-config", scope="global",
            description="Tauri v2 config quirks",
            tokens=["tauri", "v2", "config", "window"],
        )
        loaded = match_skills_for_query("tauri v2 window config")
        block = format_skills_for_prompt(loaded)
        # All three skills mentioned.
        assert "pinned-grill" in block
        assert "pinned-iter" in block
        assert "tauri-config" in block
        # Pinned markers present.
        assert "📌" in block
        # View handles present.
        assert "skill_view pinned-grill" in block
        assert "skill_view tauri-config" in block
