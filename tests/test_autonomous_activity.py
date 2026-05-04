"""Tests for v0.5.9a1 — live daemon activity inspector.

The daemon transitions through a small set of phases per iteration
(picking → dispatching → waiting_dispatch → reflecting → tick_pause,
plus parked for human-decision-required). Each transition emits an
`autonomous_activity` event so the GUI can render "Currently:
<phase> · <ago>" in real time. Solves "is it stuck or just slow?"
during long runs.

Coverage:
- _set_activity emits an event by default with the right payload shape
- _set_activity stashes activity in state_snapshot
- _set_activity with emit=False updates state silently
- One full happy-path iter emits the expected phase sequence
- Reflecting phase fires when the cadence triggers
- Parked phase fires when REFLECT emits a decision_request
- Activity is included in state_snapshot()
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from resonant_client.gui import roadmap as roadmap_module
from resonant_client.gui.autonomous_loop import (
    AutonomousMissionConfig,
    AutonomousMissionDaemon,
    DaemonHooks,
    DispatchOutcome,
    FullReflectOutcome,
)
from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
)
from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
)
from resonant_client.orchestration.reflect import ReflectPassResult


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_roadmap(tmp_path, n_items=1):
    rm = Roadmap(
        feature="test mission",
        intent_id="test-intent",
        time_budget_label="1h",
        status="running",
    )
    for i in range(1, n_items + 1):
        roadmap_module.add_item(rm, tier=1, title=f"T1.{i}", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="bash", text="`true` exits 0"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


def _events_of_kind(events, kind):
    return [e for e in events if e.get("event") == kind]


def _activity_phases(events):
    """Just the phase strings from autonomous_activity events, in
    order. Useful for asserting phase sequences."""
    return [
        e.get("phase", "")
        for e in events
        if e.get("event") == "autonomous_activity"
    ]


# ── _set_activity unit behavior ────────────────────────────────────────


class TestSetActivityEmits:
    def test_emit_default_fires_event(self, tmp_path):
        path = _build_roadmap(tmp_path)
        events = []
        config = AutonomousMissionConfig(
            intent_id="test-intent",
            roadmap_path=path,
            max_iterations=1,
            full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda i: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: None,
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(pass_result=pr),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        daemon._set_activity("picking", detail="loading roadmap")

        activity_events = _events_of_kind(events, "autonomous_activity")
        assert len(activity_events) == 1
        ev = activity_events[0]
        assert ev["phase"] == "picking"
        assert ev["detail"] == "loading roadmap"
        assert ev["specialist"] == ""
        assert ev["started_iso"]
        assert ev["intent_id"] == "test-intent"

    def test_emit_false_updates_silently(self, tmp_path):
        path = _build_roadmap(tmp_path)
        events = []
        config = AutonomousMissionConfig(
            intent_id="test-intent", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda i: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: None,
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(pass_result=pr),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        daemon._set_activity("idle", emit=False)

        activity_events = _events_of_kind(events, "autonomous_activity")
        assert activity_events == []
        # But state_snapshot still reflects the change.
        snap = daemon.state_snapshot()
        assert snap["activity"]["phase"] == "idle"

    def test_specialist_field_threaded_through(self, tmp_path):
        path = _build_roadmap(tmp_path)
        events = []
        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda i: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: None,
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(pass_result=pr),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        daemon._set_activity("reflecting", specialist="reflect", detail="K cadence")

        ev = _events_of_kind(events, "autonomous_activity")[0]
        assert ev["specialist"] == "reflect"
        assert ev["phase"] == "reflecting"


class TestActivityInStateSnapshot:
    def test_initial_phase_is_idle(self, tmp_path):
        path = _build_roadmap(tmp_path)
        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda i: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: None,
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(pass_result=pr),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks)
        snap = daemon.state_snapshot()
        assert "activity" in snap
        assert snap["activity"]["phase"] == "idle"

    def test_snapshot_returns_a_copy(self, tmp_path):
        path = _build_roadmap(tmp_path)
        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda i: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: None,
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(pass_result=pr),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks)
        snap = daemon.state_snapshot()
        # Mutating the snapshot's activity dict shouldn't affect
        # the daemon's own state.
        snap["activity"]["phase"] = "MUTATED"
        snap2 = daemon.state_snapshot()
        assert snap2["activity"]["phase"] == "idle"


# ── Full-iteration phase sequence ──────────────────────────────────────


class TestPhaseSequenceHappyPath:
    """One successful iter should produce a recognizable sequence:
    picking → dispatching → waiting_dispatch → tick_pause."""

    def test_happy_path_phases_in_order(self, tmp_path):
        # Pure-bash spec converges deterministically without REFLECT.
        path = _build_roadmap(tmp_path)
        events = []

        def good_run(cmd, **kw):
            return (0, "ok\n", "")

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(
                bash_runner=BashRunner(_run=good_run),
            ),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        daemon.start()
        daemon.join(timeout=3.0)

        phases = _activity_phases(events)
        # Expected ordered sequence (a subset — the actual sequence
        # may include duplicates as the daemon transitions internally):
        # picking → dispatching → waiting_dispatch → reflecting
        expected_subset = [
            "picking",
            "dispatching",
            "waiting_dispatch",
            "reflecting",
        ]
        # Each expected phase appears at least once and in this order.
        idx = 0
        for phase in phases:
            if idx < len(expected_subset) and phase == expected_subset[idx]:
                idx += 1
        assert idx == len(expected_subset), (
            f"expected to see {expected_subset} in order; got {phases}"
        )

    def test_reflecting_phase_specialist_is_reflect(self, tmp_path):
        path = _build_roadmap(tmp_path)
        events = []

        def good_run(cmd, **kw):
            return (0, "ok\n", "")

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(
                bash_runner=BashRunner(_run=good_run),
            ),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        daemon.start()
        daemon.join(timeout=3.0)

        # Each reflecting-phase event should carry specialist="reflect".
        reflecting = [
            e for e in events
            if e.get("event") == "autonomous_activity"
            and e.get("phase") == "reflecting"
        ]
        assert len(reflecting) >= 1
        for r in reflecting:
            assert r["specialist"] == "reflect"


class TestParkedPhase:
    """When REFLECT emits a decision_request, the daemon should
    transition to phase=parked and stay there until the user
    answers."""

    def test_parked_phase_emitted_on_decision_request(self, tmp_path):
        # Use chrome criterion so the model session fires.
        rm = Roadmap(
            feature="park-test", intent_id="i",
            time_budget_label="1h", status="running",
        )
        roadmap_module.add_item(rm, tier=1, title="T1.1", description="x")
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="chrome", text="click toggle"),
        )
        path = tmp_path / "roadmap.md"
        roadmap_module.save(rm, path)
        events = []

        call_count = {"n": 0}

        def reflect_with_decision(roadmap, pass_result, *, decision_context=""):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "?",
                        "options": [{"id": "a", "label": "A"}],
                    },
                )
            return FullReflectOutcome(pass_result=pass_result, verdict="continue")

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_with_decision,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        # Drive _run_full_reflect directly.
        rm_loaded = roadmap_module.load(path)
        result_holder = {}

        def run():
            result_holder["o"] = daemon._run_full_reflect(rm_loaded)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # Wait for decision-required to land.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(
                e.get("event") == "autonomous_human_decision_required"
                for e in events
            ):
                break
            time.sleep(0.01)

        # At this point the daemon is parked.
        phases_so_far = _activity_phases(events)
        assert "parked" in phases_so_far, (
            f"expected parked phase before decision; got {phases_so_far}"
        )
        # Provide decision so daemon unwinds.
        daemon.provide_decision("a", "")
        t.join(timeout=2.0)
