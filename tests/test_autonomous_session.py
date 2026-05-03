"""Tests for `gui/autonomous_session.py` — the glue that turns a
rigorous-grill spec into a running autonomous mission.

Two parts under test:
1. **Pure helpers** — `parse_time_budget`, `build_roadmap_from_spec`.
   Easy to test directly.
2. **Lifecycle wiring** — `start_autonomous_mission` /
   `stop_autonomous_mission` / `cleanup_finished_daemons`. Tested
   with a fake AppState + spec, asserting that the daemon is
   constructed, registered, and then stoppable.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from resonant_client.gui import roadmap as roadmap_module
from resonant_client.gui.autonomous_session import (
    _seed_item_from_intent,
    _smart_title,
    build_roadmap_from_spec,
    build_roadmap_inspector_payload,
    cleanup_finished_daemons,
    find_orphaned_autonomous_missions,
    get_autonomous_daemon,
    parse_time_budget,
    resume_autonomous_mission,
    start_autonomous_mission,
    stop_autonomous_mission,
)


# ── Sample rigorous-grill spec for fixtures ────────────────────────────


_SPEC_MD = """\
## Final spec

**Refined intent:** Build a counter web component.

**Key assumptions:**
- Vite dev server

**In scope:**
- Counter increments

**Out of scope:**
- Persistence

**Time budget:** 4h

**Technical constraints:**
- TypeScript strict

**Acceptance criteria:**
- `[bash]` `npm run build` exits 0
- `[bash]` `npx tsc --noEmit` exits 0
- `[chrome]` Counter button increments via DOM event
- `[vision]` Counter rendered in centered position

**Open risks:**
- WebKit rendering parity
"""


_SPEC_MD_NO_CRITERIA = """\
## Final spec

**Refined intent:** Misconfigured spec without any typed criteria.

**Acceptance criteria:**
- The thing should work
"""


# ── parse_time_budget ──────────────────────────────────────────────────


class TestParseTimeBudget:
    @pytest.mark.parametrize("label,expected", [
        ("1h", 3600.0),
        ("4h", 14400.0),
        ("6h", 21600.0),
        ("8h", 28800.0),
        ("12h", 43200.0),
        ("24h", 86400.0),
        ("48h", 172800.0),
    ])
    def test_known_presets(self, label, expected):
        assert parse_time_budget(label) == expected

    def test_full_auto_returns_none(self):
        assert parse_time_budget("full auto") is None
        assert parse_time_budget("Full Auto") is None
        assert parse_time_budget("full-auto") is None

    def test_empty_string_returns_none(self):
        assert parse_time_budget("") is None

    def test_unknown_label_returns_none_for_unparseable(self):
        # Something like "until tomorrow" — no integer parse.
        assert parse_time_budget("until tomorrow") is None

    def test_arbitrary_hour_count_parses(self):
        # Power-user fallback: `3h` should parse even though the
        # presets only have 1/4/6/8/12/24/48.
        assert parse_time_budget("3h") == 10800.0

    def test_minutes_parse(self):
        assert parse_time_budget("30m") == 1800.0


# ── build_roadmap_from_spec ─────────────────────────────────────────────


class TestBuildRoadmapFromSpec:
    def test_constructs_roadmap_with_typed_criteria(self, tmp_path):
        rm, path = build_roadmap_from_spec(
            feature="counter component",
            intent_id="test-intent-1",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        assert len(rm.acceptance_criteria) == 4
        assert rm.intent_id == "test-intent-1"
        assert rm.feature == "counter component"
        assert rm.time_budget_label == "4h"

    def test_persists_to_disk(self, tmp_path):
        _, path = build_roadmap_from_spec(
            feature="x",
            intent_id="i",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        assert path.exists()
        # Round-trip works: load + check criteria preserved
        loaded = roadmap_module.load(path)
        assert len(loaded.acceptance_criteria) == 4

    def test_default_path_under_dot_resonant(self, tmp_path):
        _, path = build_roadmap_from_spec(
            feature="x",
            intent_id="abc-123",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        assert ".resonant" in str(path)
        assert "abc-123" in str(path)

    def test_creates_dot_resonant_dir(self, tmp_path):
        # Project doesn't have .resonant yet — function must create it.
        _, path = build_roadmap_from_spec(
            feature="x",
            intent_id="i",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        assert (tmp_path / ".resonant").is_dir()

    def test_empty_spec_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Final spec"):
            build_roadmap_from_spec(
                feature="x",
                intent_id="i",
                spec_markdown="no spec here",
                project_path=str(tmp_path),
            )

    def test_spec_without_typed_criteria_raises(self, tmp_path):
        with pytest.raises(ValueError, match="no typed acceptance criteria"):
            build_roadmap_from_spec(
                feature="x",
                intent_id="i",
                spec_markdown=_SPEC_MD_NO_CRITERIA,
                project_path=str(tmp_path),
            )

    def test_feature_truncated_to_120_chars(self, tmp_path):
        # Long feature name shouldn't bloat the H1 title.
        long_feature = "x" * 200
        rm, _ = build_roadmap_from_spec(
            feature=long_feature,
            intent_id="i",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        assert len(rm.feature) <= 120


# ── Lifecycle wiring (with a stub AppState) ─────────────────────────────


class _StubProject:
    def __init__(self, project_path: str, sessions: Optional[list[dict]] = None):
        self.project_path = project_path
        self._sessions = sessions or []

    def list_all_sessions(self) -> list[dict]:
        # Return a defensive copy so tests can't mutate the backing
        # store via the returned list.
        return list(self._sessions)


class _StubIntentService:
    """Minimal IntentService stand-in. Records start_intent calls
    and emits intent.complete via on_event so the dispatch tracker
    unblocks. Does NOT actually run a sub-mission."""

    def __init__(self):
        self.on_event: Any = None
        self.starts: list[str] = []
        self._next_id = 0

    def start_intent(self, text: str) -> str:
        self._next_id += 1
        intent_id = f"sub-intent-{self._next_id}"
        self.starts.append(text)
        # Immediately emit terminal event so wait_for_dispatch unblocks.
        if self.on_event:
            self.on_event({
                "event": "intent.complete",
                "intent_id": intent_id,
                "extracted_skill_id": None,
            })
        return intent_id

    def cancel(self, intent_id: str) -> bool:
        if self.on_event:
            self.on_event({
                "event": "intent.cancelled",
                "intent_id": intent_id,
            })
        return True


@dataclass
class _StubAppState:
    project: _StubProject
    backend: Any = None
    settings: Any = None
    _project_instructions: str = ""
    _intent_service: Optional[_StubIntentService] = None
    _autonomous_daemons: dict = field(default_factory=dict)

    def get_intent_service(self, *, on_event=None) -> _StubIntentService:
        if self._intent_service is None:
            self._intent_service = _StubIntentService()
        if on_event is not None:
            self._intent_service.on_event = on_event
        return self._intent_service


# ── Title extraction (v0.5.1a1 fix) ────────────────────────────────────


class TestSmartTitle:
    """Pin _smart_title's behavior on the cases that motivated the
    v0.5.1a1 fix. The regression: v0.5.0's `intent.split(".", 1)[0]`
    broke any intent containing a filename like `wordcount.py` because
    it split inside the filename. Pro's planner explicitly flagged it
    in the GA smoke ("the goal is cut off")."""

    def test_filename_period_is_not_a_sentence_boundary(self):
        # The ORIGINAL bug: this exact intent from the GA smoke.
        intent = "Build a Python CLI utility `wordcount.py` at the project root."
        title = _smart_title(intent, max_len=80)
        # Critical: the title must include `wordcount.py`, not stop
        # at the period inside the filename.
        assert "wordcount" in title
        assert "project root" in title or "project" in title
        # Must NOT be the broken "Build a Python CLI utility `wordcount"
        assert not title.endswith("`wordcount")

    def test_version_number_periods_handled(self):
        intent = "Upgrade httpx from 0.27.0 to 0.28.1 in pyproject.toml."
        title = _smart_title(intent, max_len=80)
        assert "0.27.0" in title
        assert "0.28.1" in title

    def test_abbreviation_periods_handled(self):
        intent = "Add a logger module that uses e.g. structlog or stdlib logging. Configure via env var."
        # First "sentence" should NOT end at "e.g." — the regex requires
        # a capital letter after the period for it to be a sentence
        # break.
        title = _smart_title(intent, max_len=120)
        assert "e.g." in title
        assert "stdlib" in title  # full first sentence preserved

    def test_real_sentence_boundary_used_when_short(self):
        intent = "Add the dark-mode toggle. Use CSS variables. Persist via localStorage."
        title = _smart_title(intent, max_len=80)
        # First sentence is short enough — use it as-is.
        assert title == "Add the dark-mode toggle"

    def test_long_intent_truncates_on_word_boundary(self):
        intent = (
            "Build a comprehensive end-to-end testing framework with "
            "screenshot diffing, network request mocking, and "
            "deterministic time control for the entire app."
        )
        title = _smart_title(intent, max_len=60)
        assert len(title) <= 60
        # Did not slice mid-word
        assert not title.endswith("netwo") and not title.endswith("scre")

    def test_empty_intent_returns_default(self):
        assert _smart_title("", max_len=80) == "Implement the feature"
        assert _smart_title("   ", max_len=80) == "Implement the feature"

    def test_multiline_intent_collapses_to_single_line_title(self):
        # v0.5.1a2 GA-prep regression: an intent that spans multiple
        # lines (real grill-spec output is wrapped) was producing a
        # title with embedded newlines, which broke the single-line
        # roadmap item parser silently — the bootstrapped T1.1 didn't
        # show up in the parsed roadmap and the daemon went straight
        # to "stuck" on the first iteration.
        intent = (
            "Build a Python CLI utility `wordcount.py` at the\n"
            "project root. It takes a single file path argument."
        )
        title = _smart_title(intent, max_len=120)
        assert "\n" not in title, f"newline in title: {title!r}"
        assert "\t" not in title, f"tab in title: {title!r}"
        # Confirm the actual content survived — no info loss
        assert "wordcount" in title
        assert "project root" in title

    def test_short_clean_intent_unchanged_except_trailing_punctuation(self):
        assert _smart_title("Quick fix", max_len=80) == "Quick fix"
        assert _smart_title("Quick fix.", max_len=80) == "Quick fix"
        assert _smart_title("Add /export command,", max_len=80) == "Add /export command"


class TestSeedItemFromIntent:
    def test_passes_full_intent_as_description(self):
        # Even when the title gets truncated/refined, the description
        # MUST carry the full intent so the implementer's planner has
        # complete context.
        intent = (
            "Build a Python CLI utility `wordcount.py` at the project "
            "root. It takes a single file path argument and prints "
            "space-separated <lines> <words> <chars> to stdout."
        )
        title, desc = _seed_item_from_intent(intent, "fallback feature")
        assert desc == intent
        # Title respects the smart-title rules
        assert "wordcount" in title

    def test_falls_back_to_feature_when_intent_empty(self):
        title, desc = _seed_item_from_intent("", "Add /export command")
        assert "export" in title.lower()
        assert desc == "Add /export command"

    def test_default_when_both_empty(self):
        title, desc = _seed_item_from_intent("", "")
        # Default fallback runs through _smart_title which strips
        # trailing punctuation.
        assert title.startswith("Implement the feature")


class TestStartStopAutonomousMission:
    def test_start_constructs_and_registers_daemon(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        events: list[dict] = []

        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="counter component",
            spec_markdown=_SPEC_MD,
            on_event=events.append,
        )

        # Registered on state.
        assert get_autonomous_daemon(state, "auto-1") is daemon

        # Stop it before assertions on events to avoid races. The
        # daemon may emit autonomous_mission_started already; we just
        # care that it was constructed.
        daemon.stop()
        daemon.join(timeout=3.0)
        assert not daemon.is_running()

    def test_stop_signals_running_daemon(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        events: list[dict] = []

        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="x",
            spec_markdown=_SPEC_MD,
            on_event=events.append,
        )

        ok = stop_autonomous_mission(state, "auto-1")
        assert ok is True

        daemon.join(timeout=3.0)
        assert not daemon.is_running()

    def test_stop_unknown_intent_returns_false(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        ok = stop_autonomous_mission(state, "does-not-exist")
        assert ok is False

    def test_get_autonomous_daemon_returns_none_when_unregistered(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        assert get_autonomous_daemon(state, "nope") is None

    def test_cleanup_drops_finished_daemons(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))

        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="x",
            spec_markdown=_SPEC_MD,
            on_event=lambda ev: None,
        )

        # Stop and join.
        daemon.stop()
        daemon.join(timeout=3.0)

        before = len(state._autonomous_daemons)
        cleaned = cleanup_finished_daemons(state)
        after = len(state._autonomous_daemons)
        assert before == 1
        assert cleaned == 1
        assert after == 0

    def test_misconfigured_spec_raises_value_error(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        with pytest.raises(ValueError):
            start_autonomous_mission(
                state=state,
                intent_id="auto-1",
                feature="x",
                spec_markdown=_SPEC_MD_NO_CRITERIA,
                on_event=lambda ev: None,
            )


# ── v0.5.3a1: resume + orphan detection ────────────────────────────────


class TestResumeAutonomousMission:
    """`resume_autonomous_mission` is the recovery path for missions
    interrupted by a server restart, app crash, or laptop sleep. It
    loads the EXISTING roadmap from disk so any progress already made
    (checked items, validated criteria, iteration log) is preserved —
    in contrast to `start_autonomous_mission`, which builds a fresh
    roadmap from a spec."""

    def test_raises_when_no_roadmap_on_disk(self, tmp_path):
        # No roadmap file exists at the expected path → resume should
        # fail loudly. The original mission either never persisted its
        # roadmap or the project root changed.
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        with pytest.raises(ValueError, match="no roadmap"):
            resume_autonomous_mission(
                state=state,
                intent_id="never-existed",
                on_event=lambda ev: None,
            )

    def test_raises_when_roadmap_has_no_acceptance_criteria(self, tmp_path):
        # Hand-crafted misconfigured roadmap on disk with no criteria.
        # Resume should refuse rather than spawn a daemon that would
        # immediately stop with `misconfigured`.
        path = roadmap_module.default_path(str(tmp_path), "no-criteria")
        path.parent.mkdir(parents=True, exist_ok=True)
        rm = roadmap_module.Roadmap(
            feature="empty mission",
            intent_id="no-criteria",
            status="running",
        )
        roadmap_module.add_item(rm, tier=1, title="Do something", description="")
        roadmap_module.save(rm, path)

        state = _StubAppState(project=_StubProject(str(tmp_path)))
        with pytest.raises(ValueError, match="no acceptance criteria"):
            resume_autonomous_mission(
                state=state,
                intent_id="no-criteria",
                on_event=lambda ev: None,
            )

    def test_raises_when_daemon_already_running(self, tmp_path):
        # Bootstrap a roadmap via start_autonomous_mission so a live
        # daemon exists, then try to resume the same intent_id. Should
        # raise RuntimeError — double-spawn would be a logic bug.
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="counter",
            spec_markdown=_SPEC_MD,
            on_event=lambda ev: None,
        )
        try:
            with pytest.raises(RuntimeError, match="already running"):
                resume_autonomous_mission(
                    state=state,
                    intent_id="auto-1",
                    on_event=lambda ev: None,
                )
        finally:
            daemon.stop()
            daemon.join(timeout=3.0)

    def test_resume_after_finished_daemon_works(self, tmp_path):
        # If the previous daemon already exited (server restart between
        # iterations), resume should succeed even though the registry
        # still has the old daemon entry. Only LIVE daemons block resume.
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        first = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="counter",
            spec_markdown=_SPEC_MD,
            on_event=lambda ev: None,
        )
        first.stop()
        first.join(timeout=3.0)
        assert not first.is_running()

        # The dead daemon is still in state._autonomous_daemons; resume
        # should overwrite it with a new live one rather than refuse.
        second = resume_autonomous_mission(
            state=state,
            intent_id="auto-1",
            on_event=lambda ev: None,
        )
        try:
            assert second is not first
            assert get_autonomous_daemon(state, "auto-1") is second
        finally:
            second.stop()
            second.join(timeout=3.0)

    def test_resume_loads_existing_roadmap_preserving_state(self, tmp_path):
        # Build a roadmap, then mark T1.1 checked + add an iteration log
        # entry to simulate "the mission had made progress before the
        # server restarted". After resume, the on-disk roadmap should
        # STILL show that progress — resume must not overwrite it.
        rm, path = build_roadmap_from_spec(
            feature="counter",
            intent_id="auto-1",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        # Simulate progress: mark the bootstrapped item complete.
        rm.items[0].checked = True
        rm.items[0].commit_sha = "abc1234"
        rm.items[0].note = "shipped before restart"
        roadmap_module.append_iteration_log(
            rm, iter_num=1, duration_label="2m",
            note="bootstrap shipped",
            item_id=rm.items[0].id, commit_sha="abc1234",
        )
        # Mark one criterion as passed too.
        rm.acceptance_criteria[0].passed = True
        rm.acceptance_criteria[0].evidence = "build exited 0"
        roadmap_module.save(rm, path)

        # Now resume — daemon should pick up the existing roadmap.
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        daemon = resume_autonomous_mission(
            state=state,
            intent_id="auto-1",
            on_event=lambda ev: None,
        )
        try:
            assert get_autonomous_daemon(state, "auto-1") is daemon

            # The on-disk roadmap should still reflect the pre-resume
            # state (resume MUST NOT clobber checked items or evidence).
            reloaded = roadmap_module.load(path)
            assert reloaded.items[0].checked is True
            assert reloaded.items[0].commit_sha == "abc1234"
            assert reloaded.items[0].note == "shipped before restart"
            assert len(reloaded.iteration_log) == 1
            # The first criterion should still be marked passed.
            passed_count, total = reloaded.acceptance_summary()
            assert passed_count == 1
            assert total == 4
        finally:
            daemon.stop()
            daemon.join(timeout=3.0)


class TestFindOrphanedAutonomousMissions:
    """An orphan = a session whose `mission_state.phase` is
    `autonomous_running` but whose intent_id has NO live daemon in the
    AppState. These are the missions a "Resume" button should surface."""

    def test_returns_empty_when_no_project(self):
        state = _StubAppState(project=None)  # type: ignore[arg-type]
        assert find_orphaned_autonomous_missions(state) == []

    def test_returns_empty_when_no_sessions(self, tmp_path):
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=[]))
        assert find_orphaned_autonomous_missions(state) == []

    def test_returns_empty_when_no_autonomous_phase_sessions(self, tmp_path):
        # Sessions exist but none are in autonomous_running phase.
        sessions = [
            {"id": "s1", "title": "drafting one",
             "mission_state": {"phase": "drafting", "intent_id": "i1"}},
            {"id": "s2", "title": "regular chat", "mission_state": None},
            {"id": "s3", "title": "complete one",
             "mission_state": {"phase": "autonomous_complete", "intent_id": "i3"}},
        ]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        assert find_orphaned_autonomous_missions(state) == []

    def test_finds_orphan_with_phase_autonomous_running(self, tmp_path):
        # The classic case: session says autonomous_running but no live
        # daemon for that intent_id. Surface it as a resume candidate.
        sessions = [
            {
                "id": "session-abc",
                "title": "Counter component",
                "mission_state": {
                    "phase": "autonomous_running",
                    "intent_id": "auto-orphan-1",
                    "seed_feature": "Build a counter web component",
                    "autonomous_started_at": 1714600000.0,
                },
            },
        ]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        orphans = find_orphaned_autonomous_missions(state)
        assert len(orphans) == 1
        o = orphans[0]
        assert o["session_id"] == "session-abc"
        assert o["intent_id"] == "auto-orphan-1"
        assert o["feature"] == "Build a counter web component"
        assert o["autonomous_started_at"] == 1714600000.0
        # roadmap_path is the canonical default path
        expected_path = roadmap_module.default_path(str(tmp_path), "auto-orphan-1")
        assert o["roadmap_path"] == str(expected_path)
        assert o["roadmap_exists"] is False  # we never wrote it

    def test_orphan_roadmap_exists_flag_reflects_disk(self, tmp_path):
        # When the roadmap file IS on disk, roadmap_exists should be True
        # (the typical case — the daemon got far enough to persist).
        rm, _ = build_roadmap_from_spec(
            feature="x",
            intent_id="auto-orphan-2",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        sessions = [{
            "id": "s",
            "title": "x",
            "mission_state": {
                "phase": "autonomous_running",
                "intent_id": "auto-orphan-2",
                "seed_feature": "x",
            },
        }]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        orphans = find_orphaned_autonomous_missions(state)
        assert len(orphans) == 1
        assert orphans[0]["roadmap_exists"] is True

    def test_excludes_intent_ids_with_live_daemons(self, tmp_path):
        # Two autonomous_running sessions; one has a live daemon (started
        # this session), the other doesn't. Only the daemonless one is
        # an orphan.
        sessions = [
            {"id": "s-live", "title": "live one",
             "mission_state": {
                 "phase": "autonomous_running",
                 "intent_id": "auto-1",  # matches the daemon below
                 "seed_feature": "live",
             }},
            {"id": "s-orphan", "title": "orphan one",
             "mission_state": {
                 "phase": "autonomous_running",
                 "intent_id": "auto-2",  # no daemon
                 "seed_feature": "orphan",
             }},
        ]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        # Spin up a real daemon for auto-1 so it's "live".
        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="live",
            spec_markdown=_SPEC_MD,
            on_event=lambda ev: None,
        )
        try:
            orphans = find_orphaned_autonomous_missions(state)
            assert len(orphans) == 1
            assert orphans[0]["intent_id"] == "auto-2"
        finally:
            daemon.stop()
            daemon.join(timeout=3.0)

    def test_finished_daemon_does_not_protect_session_from_being_orphaned(self, tmp_path):
        # A daemon that's REGISTERED but not running anymore still leaves
        # its session in `autonomous_running` phase from the daemon's POV
        # (the session-state writer hasn't transitioned it). The orphan
        # check should treat finished daemons as absent → the session
        # IS an orphan candidate.
        sessions = [{
            "id": "s",
            "title": "x",
            "mission_state": {
                "phase": "autonomous_running",
                "intent_id": "auto-1",
                "seed_feature": "x",
            },
        }]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        daemon = start_autonomous_mission(
            state=state,
            intent_id="auto-1",
            feature="x",
            spec_markdown=_SPEC_MD,
            on_event=lambda ev: None,
        )
        daemon.stop()
        daemon.join(timeout=3.0)
        assert not daemon.is_running()
        # Even with the dead daemon still in the registry, the session
        # should still surface as an orphan ready for resume.
        orphans = find_orphaned_autonomous_missions(state)
        assert len(orphans) == 1
        assert orphans[0]["intent_id"] == "auto-1"

    def test_skips_session_with_missing_intent_id(self, tmp_path):
        # Defensive: a malformed mission_state (no intent_id) should
        # be silently skipped rather than crashing or emitting a bogus
        # orphan.
        sessions = [{
            "id": "s", "title": "broken",
            "mission_state": {
                "phase": "autonomous_running",
                # intent_id missing
                "seed_feature": "x",
            },
        }]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        assert find_orphaned_autonomous_missions(state) == []

    def test_falls_back_to_session_title_when_seed_feature_missing(self, tmp_path):
        # Old sessions written before seed_feature was added should
        # still show a sensible feature label — fall back to the
        # session title.
        sessions = [{
            "id": "s", "title": "Counter component (old session)",
            "mission_state": {
                "phase": "autonomous_running",
                "intent_id": "auto-old",
                # no seed_feature
            },
        }]
        state = _StubAppState(project=_StubProject(str(tmp_path), sessions=sessions))
        orphans = find_orphaned_autonomous_missions(state)
        assert len(orphans) == 1
        assert orphans[0]["feature"] == "Counter component (old session)"


# ── v0.5.3a3: roadmap inspector payload ────────────────────────────────


class TestBuildRoadmapInspectorPayload:
    """`build_roadmap_inspector_payload` is the data-marshaling layer
    behind the sidebar inspector. The WS handler is a thin wrapper
    around it; tests here pin the payload shape + edge-case handling
    so the frontend's render code can rely on stable field semantics."""

    def _fresh_roadmap_with_one_item_and_criteria(self, tmp_path):
        rm, path = build_roadmap_from_spec(
            feature="counter web component",
            intent_id="inspect-1",
            spec_markdown=_SPEC_MD,
            project_path=str(tmp_path),
        )
        return rm, path

    def test_includes_top_level_metadata(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        payload = build_roadmap_inspector_payload(
            intent_id="inspect-1", roadmap=rm, roadmap_path=path,
        )
        assert payload["intent_id"] == "inspect-1"
        assert payload["roadmap_exists"] is True
        assert payload["roadmap_path"] == str(path)
        assert payload["feature"] == "counter web component"
        assert payload["status"] == "running"
        assert payload["time_budget_label"] == "4h"

    def test_acceptance_summary_initial_state(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # Before any REFLECT pass: 0 passed, 4 total blocking, all
        # criteria.passed is None, is_blocking is True for all (none
        # are [manual]).
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        s = payload["acceptance_summary"]
        assert s["passed"] == 0
        assert s["total_blocking"] == 4
        assert len(s["criteria"]) == 4
        for c in s["criteria"]:
            assert c["passed"] is None
            assert c["is_blocking"] is True
        assert payload["is_converged"] is False

    def test_acceptance_summary_after_partial_pass(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # Mark 2 criteria as passed.
        rm.acceptance_criteria[0].passed = True
        rm.acceptance_criteria[1].passed = True
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        s = payload["acceptance_summary"]
        assert s["passed"] == 2
        assert s["total_blocking"] == 4
        assert payload["is_converged"] is False
        # Per-criterion `passed` booleans round-trip.
        passed_states = [c["passed"] for c in s["criteria"]]
        assert passed_states == [True, True, None, None]

    def test_is_converged_when_all_blocking_pass(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        for c in rm.acceptance_criteria:
            c.passed = True
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["acceptance_summary"]["passed"] == 4
        assert payload["is_converged"] is True

    def test_manual_criteria_marked_non_blocking(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # Inject a manual criterion. The summary should NOT count it
        # against `total_blocking` and is_converged should ignore it.
        from resonant_client.gui.roadmap import AcceptanceCriterion
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="manual", text="Reviewer approval")
        )
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        s = payload["acceptance_summary"]
        # 4 blocking + 1 manual = 5 total criteria, 4 blocking total
        assert s["total_blocking"] == 4
        assert len(s["criteria"]) == 5
        manual = s["criteria"][-1]
        assert manual["type"] == "manual"
        assert manual["is_blocking"] is False

    def test_items_serialized_with_id_tier_title_checked(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # The bootstrap T1.1 item is already there from build_roadmap_from_spec.
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["id"] == "T1.1"
        assert item["tier"] == 1
        assert item["checked"] is False
        assert item["title"]  # non-empty

    def test_next_item_is_first_unchecked(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # Add a second item; check the first; next_item should be the
        # second (still unchecked).
        rm.items[0].checked = True
        roadmap_module.add_item(rm, tier=1, title="Second task", description="")
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["next_item"] is not None
        assert payload["next_item"]["id"] == "T1.2"
        assert payload["next_item"]["title"] == "Second task"

    def test_next_item_is_none_when_all_checked(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        rm.items[0].checked = True
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["next_item"] is None

    def test_iteration_count_reflects_log_length(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        roadmap_module.append_iteration_log(
            rm, iter_num=1, duration_label="2m", note="bootstrap done",
        )
        roadmap_module.append_iteration_log(
            rm, iter_num=2, duration_label="3m", note="reflect pass",
        )
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["iteration_count"] == 2

    def test_reflection_summary_trimmed_when_too_long(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        rm.reflection_summary = "x" * 1000
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
            reflection_max_chars=200,
        )
        # Trimmed text + ellipsis sentinel
        assert payload["reflection_summary"].endswith("…")
        assert len(payload["reflection_summary"]) <= 201

    def test_reflection_summary_untrimmed_when_short(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        rm.reflection_summary = "All four criteria green; mission complete."
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["reflection_summary"] == "All four criteria green; mission complete."

    def test_empty_reflection_summary_round_trips(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        # Default — never assigned.
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
        )
        assert payload["reflection_summary"] == ""

    def test_reflection_max_chars_zero_disables_trimming(self, tmp_path):
        rm, path = self._fresh_roadmap_with_one_item_and_criteria(tmp_path)
        rm.reflection_summary = "x" * 5000
        payload = build_roadmap_inspector_payload(
            intent_id="i", roadmap=rm, roadmap_path=path,
            reflection_max_chars=0,
        )
        assert payload["reflection_summary"] == "x" * 5000
