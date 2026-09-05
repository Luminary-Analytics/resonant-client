"""Tests for v0.6.3a2 — skill loader wired into the autonomous runtime.

v0.6.0a4 built `match_skills_for_query` + `format_skills_for_prompt`
but the v0.6.2 field run found they were never CALLED from the
autonomous mission daemon — the read side of the self-improvement
loop was dead code. v0.6.3a2 wires them in via:

- `build_skill_context` — one-shot match+format helper (skill_loader)
- `mark_skill_surfaced` — staleness-clock touch without count
  inflation (skills.py)
- `dispatch_item` in autonomous_factory — appends the skills block to
  each iter's planner goal, marks surfaced, emits a
  `skill_context_loaded` telemetry event

These tests cover all three layers.
"""
from __future__ import annotations

import threading
import time

import pytest

from resonant_client.gui.autonomous_factory import (
    DispatchTracker,
    build_autonomous_mission_hooks,
)
from resonant_client.gui.roadmap import RoadmapItem
from resonant_client.orchestration.skill_loader import (
    SkillContext,
    build_skill_context,
)
from resonant_client.orchestration.skills import (
    Skill,
    load_skill,
    mark_skill_surfaced,
    save_skill,
)


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


def _seed(*, skill_id, scope="global", project_path=None, pinned=False,
          description="", tokens=None, triggers=None, last_used_days_ago=0.0):
    last_used = (
        time.time() - last_used_days_ago * 86400
        if last_used_days_ago else time.time()
    )
    s = Skill(
        id=skill_id,
        name=skill_id,
        description=description or f"Skill {skill_id}",
        scope=scope,
        pinned=pinned,
        created_by="agent",
        last_used_at=last_used,
        tokens=tokens or [],
        triggers=triggers or [],
    )
    save_skill(s, procedure_md=f"# {skill_id}",
               project_path=str(project_path) if project_path else None)
    return s


# ── build_skill_context ───────────────────────────────────────────────


class TestBuildSkillContext:
    def test_empty_when_no_skills(self, state_home):
        ctx = build_skill_context("anything at all")
        assert isinstance(ctx, SkillContext)
        assert ctx.block == ""
        assert ctx.skill_ids == []
        assert ctx.loaded == []

    def test_pinned_skill_always_surfaces(self, state_home):
        _seed(skill_id="pinned-one", pinned=True)
        ctx = build_skill_context("totally unrelated query text")
        # Pinned skills come through regardless of token overlap.
        assert "pinned-one" in ctx.skill_ids
        assert "pinned-one" in ctx.block
        assert ctx.loaded

    def test_block_has_injectable_header(self, state_home):
        _seed(skill_id="pinned-x", pinned=True)
        ctx = build_skill_context("query")
        # The formatted block carries the prompt header so the planner
        # knows it's reference material, not a goal.
        assert "Candidate skills" in ctx.block
        assert "skill_view" in ctx.block

    def test_token_match_surfaces_relevant_skill(self, state_home):
        _seed(
            skill_id="pytest-fixture-skill",
            description="how to write pytest fixtures with tmp_path",
            tokens=["pytest", "fixture", "tmp_path", "conftest"],
            triggers=["pytest fixtures", "test setup"],
        )
        ctx = build_skill_context("add a pytest fixture for the conftest")
        assert "pytest-fixture-skill" in ctx.skill_ids

    def test_irrelevant_skill_not_surfaced(self, state_home):
        _seed(
            skill_id="kubernetes-skill",
            description="deploying to kubernetes clusters",
            tokens=["kubernetes", "helm", "kubectl", "cluster"],
        )
        ctx = build_skill_context("rename a python variable")
        assert "kubernetes-skill" not in ctx.skill_ids

    def test_best_effort_returns_empty_on_matcher_failure(
        self, state_home, monkeypatch,
    ):
        # If the matcher raises, build_skill_context must return an
        # empty context (not propagate) — dispatch must never break.
        import resonant_client.orchestration.skill_loader as loader

        def boom(*a, **kw):
            raise RuntimeError("matcher exploded")

        monkeypatch.setattr(loader, "match_skills_for_query", boom)
        ctx = build_skill_context("query")
        assert ctx.block == ""
        assert ctx.skill_ids == []


# ── mark_skill_surfaced ───────────────────────────────────────────────


class TestMarkSkillSurfaced:
    def test_bumps_last_used_at(self, state_home):
        s = _seed(skill_id="x", last_used_days_ago=30)
        old = load_skill("x").last_used_at
        mark_skill_surfaced(s)
        new = load_skill("x").last_used_at
        assert new > old

    def test_does_not_bump_success_count(self, state_home):
        s = _seed(skill_id="y")
        s.success_count = 3
        s.fail_count = 1
        save_skill(s)
        mark_skill_surfaced(s)
        reloaded = load_skill("y")
        # Counts untouched — surfacing is not a quality signal.
        assert reloaded.success_count == 3
        assert reloaded.fail_count == 1

    def test_rescues_skill_from_staleness(self, state_home):
        # A skill 100 days unused is auto-deprecated. Surfacing it
        # resets the staleness clock so the curator leaves it alone.
        s = _seed(skill_id="stale", last_used_days_ago=100)
        assert load_skill("stale").is_deprecated() is True
        mark_skill_surfaced(s)
        assert load_skill("stale").is_deprecated() is False


# ── dispatch_item wiring ──────────────────────────────────────────────


class _CapturingIntentService:
    """Stub that records the goal text passed to start_intent."""
    def __init__(self):
        self.captured_text = None
        self.counter = 0

    def start_intent(self, text, *, planner_specialization=None):
        self.captured_text = text
        self.counter += 1
        return f"intent-{self.counter}"

    def cancel(self, intent_id):
        pass


class _StubBackend:
    name = "ollama"
    model = "deepseek-v4-pro:cloud"
    base_url = "http://test"
    api_key = None
    tool_mode = "native"

    def stream(self, **kw):
        if False:
            yield


def _build_hooks(project_path, intent_service, events, **kwargs):
    defaults = dict(
        intent_service=intent_service,
        dispatch_tracker=DispatchTracker(),
        project_path=str(project_path),
        backend=_StubBackend(),
        project_instructions="",
        settings=None,
        roadmap_path=str(project_path / "roadmap.md"),
        daemon_stop_event=threading.Event(),
        on_session_event=events.append,
    )
    defaults.update(kwargs)
    return build_autonomous_mission_hooks(**defaults)


class TestDispatchItemSkillWiring:
    def test_skills_appended_to_goal_text(self, state_home, project_dir):
        _seed(skill_id="pinned-ref", pinned=True,
              description="a pinned reference skill")
        svc = _CapturingIntentService()
        events = []
        hooks = _build_hooks(project_dir, svc, events)
        item = RoadmapItem(id="T1.1", tier=1, title="Do a thing",
                            description="with details")
        hooks.dispatch_item(item)
        # The goal text the planner sees includes the skills block.
        assert svc.captured_text is not None
        assert "Do a thing" in svc.captured_text
        assert "Candidate skills" in svc.captured_text
        assert "pinned-ref" in svc.captured_text

    def test_no_skills_means_clean_goal_text(self, state_home, project_dir):
        # No skills seeded — goal text is just the item, no block.
        svc = _CapturingIntentService()
        events = []
        hooks = _build_hooks(project_dir, svc, events)
        item = RoadmapItem(id="T1.1", tier=1, title="Do a thing",
                            description="with details")
        hooks.dispatch_item(item)
        assert svc.captured_text == "Do a thing\n\nwith details"
        assert "Relevant skills" not in svc.captured_text

    def test_skill_context_loaded_event_emitted(self, state_home, project_dir):
        _seed(skill_id="pinned-ref", pinned=True)
        svc = _CapturingIntentService()
        events = []
        hooks = _build_hooks(project_dir, svc, events)
        item = RoadmapItem(id="T1.2", tier=1, title="Task")
        hooks.dispatch_item(item)
        skill_events = [e for e in events if e.get("event") == "skill_context_loaded"]
        assert len(skill_events) == 1
        ev = skill_events[0]
        assert ev["intent_id"] == "intent-1"
        assert ev["item_id"] == "T1.2"
        assert "pinned-ref" in ev["skill_ids"]
        assert ev["skill_count"] == len(ev["skill_ids"])

    def test_no_event_when_no_skills(self, state_home, project_dir):
        svc = _CapturingIntentService()
        events = []
        hooks = _build_hooks(project_dir, svc, events)
        item = RoadmapItem(id="T1.1", tier=1, title="Task")
        hooks.dispatch_item(item)
        assert not [e for e in events if e.get("event") == "skill_context_loaded"]

    def test_surfaced_skill_last_used_bumped(self, state_home, project_dir):
        _seed(skill_id="pinned-ref", pinned=True, last_used_days_ago=50)
        before = load_skill("pinned-ref").last_used_at
        svc = _CapturingIntentService()
        hooks = _build_hooks(project_dir, svc, [])
        item = RoadmapItem(id="T1.1", tier=1, title="Task")
        hooks.dispatch_item(item)
        after = load_skill("pinned-ref").last_used_at
        assert after > before

    def test_loader_disabled_skips_skills(self, state_home, project_dir):
        _seed(skill_id="pinned-ref", pinned=True)
        svc = _CapturingIntentService()
        events = []
        hooks = _build_hooks(project_dir, svc, events, enable_skill_loader=False)
        item = RoadmapItem(id="T1.1", tier=1, title="Task")
        hooks.dispatch_item(item)
        # Loader off → no skills block, no event.
        assert "Relevant skills" not in svc.captured_text
        assert not [e for e in events if e.get("event") == "skill_context_loaded"]

    def test_dispatch_still_works_if_loader_raises(
        self, state_home, project_dir, monkeypatch,
    ):
        # Even if the skill loader explodes, dispatch_item must still
        # return a valid intent_id.
        import resonant_client.orchestration.skill_loader as loader

        def boom(*a, **kw):
            raise RuntimeError("loader exploded")

        # build_skill_context catches internally, but patch it directly
        # to simulate a failure that escapes — the factory's own
        # try/except is the last line of defense.
        monkeypatch.setattr(loader, "build_skill_context", boom)
        svc = _CapturingIntentService()
        hooks = _build_hooks(project_dir, svc, [])
        item = RoadmapItem(id="T1.1", tier=1, title="Task")
        intent_id = hooks.dispatch_item(item)
        assert intent_id == "intent-1"
