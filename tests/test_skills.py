"""Tests for the skill library — storage, similarity, extraction, deprecation."""

from __future__ import annotations

import time

import pytest

from resonant_client.orchestration import (
    NodeSpecialization,
    PlanGraph,
    PlanNode,
    Skill,
    classify_match,
    deprecate_skill,
    extract_skill,
    find_matching_skills,
    is_extraction_candidate,
    list_skills,
    load_skill,
    new_node_id,
    record_skill_use,
    save_skill,
    similarity,
    tokenize,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


def _build_completed_graph(intent: str = "add dark mode toggle"):
    """Helper: build a small completed plan-graph with all DONE nodes."""
    g = PlanGraph.new(intent)
    a = PlanNode(id=new_node_id(), intent_id=g.intent_id,
                 goal="research prefers-color-scheme",
                 specialization=NodeSpecialization.RESEARCH)
    g.add_node(a)
    b = PlanNode(id=new_node_id(), intent_id=g.intent_id,
                 goal="add CSS variables",
                 specialization=NodeSpecialization.IMPLEMENT,
                 depends_on=[a.id])
    g.add_node(b)
    c = PlanNode(id=new_node_id(), intent_id=g.intent_id,
                 goal="verify in browser",
                 specialization=NodeSpecialization.VERIFY,
                 depends_on=[b.id])
    g.add_node(c)
    g.mark_done(a.id, confidence=0.95)
    g.mark_done(b.id, confidence=0.9)
    g.mark_done(c.id, result={"summary": "looks good", "verdict": "pass"}, confidence=0.95)
    return g


# ── Token / similarity primitives ───────────────────────────────────────


def test_tokenize_lowercases_and_drops_short():
    assert tokenize("Add a Dark Mode Toggle") == ["add", "dark", "mode", "toggle"]
    # Single chars dropped
    assert tokenize("a b cd") == ["cd"]


def test_similarity_jaccard_known_values():
    assert similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert similarity(["a"], ["b"]) == 0.0
    # 1 shared / 3 unique = 1/3
    assert abs(similarity(["a", "b"], ["a", "c"]) - 1 / 3) < 1e-9
    # Empty inputs → 0.0
    assert similarity([], ["a"]) == 0.0


def test_classify_match_tiers():
    assert classify_match(0.9) == "high"
    assert classify_match(0.7) == "partial"
    assert classify_match(0.4) == "none"


# ── Skill round-trip ───────────────────────────────────────────────────


def test_save_then_load_round_trip(state_home):
    skill = Skill(
        id="add-dark-mode",
        name="Add dark mode",
        description="Toggle dark mode in a web project",
        triggers=["dark mode toggle", "prefers-color-scheme"],
        tokens=["dark", "mode", "toggle"],
        success_count=2,
        last_used_at=time.time(),
    )
    save_skill(skill, procedure_md="# steps\n1. ...\n", verification_md="# checks\n")
    loaded = load_skill("add-dark-mode")
    assert loaded is not None
    assert loaded.id == "add-dark-mode"
    assert loaded.success_count == 2
    assert "dark mode toggle" in loaded.triggers


def test_load_missing_returns_none(state_home):
    assert load_skill("does-not-exist") is None


def test_list_skills_excludes_deprecated_by_default(state_home):
    fresh = Skill(id="fresh", name="Fresh", description="", last_used_at=time.time())
    stale = Skill(
        id="stale", name="Stale", description="",
        last_used_at=time.time() - 200 * 86400,  # 200 days ago
    )
    save_skill(fresh)
    save_skill(stale)
    visible = {s.id for s in list_skills()}
    assert "fresh" in visible
    assert "stale" not in visible
    # But explicit include_deprecated does surface it
    all_ids = {s.id for s in list_skills(include_deprecated=True)}
    assert "stale" in all_ids


def test_deprecate_skill_moves_folder(state_home):
    skill = Skill(id="goner", name="Goner", description="", last_used_at=time.time())
    save_skill(skill)
    moved = deprecate_skill(skill)
    assert moved is not None
    assert moved.exists()
    # Original gone from active list
    assert load_skill("goner") is None


# ── Match discovery ───────────────────────────────────────────────────


def test_find_matching_skills_returns_high_match(state_home):
    skill = Skill(
        id="add-dark-mode",
        name="Add dark mode",
        description="",
        triggers=["add dark mode toggle to a web app"],
        tokens=["add", "dark", "mode", "toggle", "web"],
        last_used_at=time.time(),
    )
    save_skill(skill)
    matches = find_matching_skills("add dark mode toggle")
    assert matches, "expected at least one match"
    assert matches[0].skill.id == "add-dark-mode"
    assert classify_match(matches[0].score) in ("high", "partial")


def test_find_matching_skills_returns_empty_for_unrelated_query(state_home):
    skill = Skill(
        id="something-else",
        name="Something",
        description="",
        triggers=["completely unrelated stuff"],
        tokens=["completely", "unrelated", "stuff"],
        last_used_at=time.time(),
    )
    save_skill(skill)
    matches = find_matching_skills("write a database migration")
    assert matches == []


# ── Usage tracking ─────────────────────────────────────────────────────


def test_record_skill_use_bumps_counts(state_home):
    skill = Skill(id="track-me", name="t", description="", last_used_at=time.time())
    save_skill(skill)
    record_skill_use(skill, success=True)
    record_skill_use(skill, success=False)
    record_skill_use(skill, success=True)
    reloaded = load_skill("track-me")
    assert reloaded.success_count == 2
    assert reloaded.fail_count == 1


# ── Auto-extraction ────────────────────────────────────────────────────


def test_is_extraction_candidate_requires_completion_and_size():
    g = PlanGraph.new("intent")
    # Empty graph
    assert not is_extraction_candidate(g)
    # Add 3 nodes but leave one pending
    a = PlanNode(id=new_node_id(), intent_id=g.intent_id, goal="a")
    b = PlanNode(id=new_node_id(), intent_id=g.intent_id, goal="b")
    c = PlanNode(id=new_node_id(), intent_id=g.intent_id, goal="c")
    for n in (a, b, c):
        g.add_node(n)
    g.mark_done(a.id)
    g.mark_done(b.id)
    # c still pending → not complete
    assert not is_extraction_candidate(g)


def test_is_extraction_candidate_requires_high_confidence():
    g = _build_completed_graph()
    # Drop one node's confidence to fail the threshold
    nid = next(iter(g.nodes))
    g.nodes[nid].confidence = 0.1
    assert not is_extraction_candidate(g)


def test_extract_skill_persists_metadata_and_files(state_home):
    g = _build_completed_graph(intent="add dark mode toggle")
    skill = extract_skill(g)
    assert skill is not None
    assert skill.id == "add-dark-mode-toggle"
    # Round-trip: skill.json was written
    loaded = load_skill(skill.id)
    assert loaded is not None
    assert loaded.success_count == 1
    # Procedure should reference each node's goal
    skill_dir_path = skill_path_for(skill.id)
    proc_md = (skill_dir_path / "procedure.md").read_text(encoding="utf-8")
    assert "add CSS variables" in proc_md
    assert "research prefers-color-scheme" in proc_md
    # Verification md captures the verify node
    verify_md = (skill_dir_path / "verification.md").read_text(encoding="utf-8")
    assert "Verdict: **pass**" in verify_md


def test_extract_skill_skips_non_candidates(state_home):
    g = PlanGraph.new("trivial")
    a = PlanNode(id=new_node_id(), intent_id=g.intent_id, goal="x")
    g.add_node(a)
    g.mark_done(a.id, confidence=0.95)
    # Single-node graph — too small to extract
    assert extract_skill(g) is None


def test_extract_skill_includes_intent_tokens(state_home):
    g = _build_completed_graph(intent="set up github actions for python testing")
    skill = extract_skill(g)
    assert skill is not None
    # Tokens drawn from intent + node goals
    assert "github" in skill.tokens
    assert "actions" in skill.tokens
    assert "python" in skill.tokens


# ── Helpers ────────────────────────────────────────────────────────────


def skill_path_for(skill_id: str):
    """Convenience: resolve the global-scope skill dir."""
    from resonant_client.orchestration.skills import skill_dir
    return skill_dir(skill_id)
