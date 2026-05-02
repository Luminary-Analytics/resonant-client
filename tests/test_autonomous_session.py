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
    build_roadmap_from_spec,
    cleanup_finished_daemons,
    get_autonomous_daemon,
    parse_time_budget,
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
    def __init__(self, project_path: str):
        self.project_path = project_path


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
