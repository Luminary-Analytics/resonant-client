"""Tests for v0.5.10a2 — cross-feature integration.

Each v0.5.8 + v0.5.9 feature has its own unit-level tests. This module
exercises the *interactions* between them — the combinations a real
multi-hour mission with human-in-the-loop forks will trigger:

- Pause-during-park: a Pause request while the daemon is parked
  waiting for a human decision must NOT unblock the park, but must be
  honored at the next top-of-loop after the user provides the decision.
- Stop-during-park: must override any queued pause and exit cleanly.
- Decision-then-lying-satisfied: when REFLECT re-runs after the user
  picks an option and claims `satisfied` on still-unpassed criteria,
  the v0.5.9a3 verdict-override fires WITH structured provenance, and
  the v0.5.8a2 decision_required + decision_received events ALSO appear
  in the same iter.
- Activity phase sequence: `parked` activity is emitted when the
  decision_request lands; `tick_pause` / `picking` resume after the
  iter completes.

These are NOT redoing what unit tests already cover — they assert that
multiple features fired together produce the right combined event
sequence, which is what the GUI consumes during a real run.
"""
from __future__ import annotations

import threading
import time

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
from resonant_client.orchestration.acceptance_check import CheckContext


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_roadmap_with_chrome(tmp_path, n_items=2):
    """Roadmap with a chrome criterion → forces the model-session
    branch in `_run_full_reflect` so decision_request can actually
    fire (the no-model deterministic branch never reaches the hook)."""
    rm = Roadmap(
        feature="t",
        intent_id="i",
        time_budget_label="1h",
        status="running",
    )
    for i in range(1, n_items + 1):
        roadmap_module.add_item(rm, tier=1, title=f"T1.{i}", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="chrome", text="click toggle"),
    )
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="bash", text="`true` exits 0"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


def _events_of_kind(events, kind):
    return [e for e in events if e.get("event") == kind]


def _activity_phases(events):
    return [
        e.get("phase", "")
        for e in events
        if e.get("event") == "autonomous_activity"
    ]


def _wait_for_event(events, kind, timeout=5.0):
    """Spin-wait for the first event of `kind`; return True if it
    landed in time, False on timeout.

    The full suite uses many xdist workers. On a loaded Windows runner the
    daemon thread can spend more than two seconds waiting to be scheduled,
    even though the decision event itself is immediate once it runs.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(e.get("event") == kind for e in events):
            return True
        time.sleep(0.01)
    return False


# ── Pause-during-park interactions ──────────────────────────────────────


class TestPauseDuringDecisionPark:
    """v0.5.9a4 (pause) × v0.5.8a2 (decision-request).

    A pause request while the daemon is parked waiting for a human
    decision must:
      1. Not unblock the parked wait (only `provide_decision` or
         `stop` does that).
      2. Be honored at the next top-of-loop check after the user
         provides the decision and the iter completes.
      3. Be overridden by `stop` if both signals arrive while parked.
    """

    def _make_daemon_that_parks_once(
        self, tmp_path, *, full_reflect_cadence=999, max_iterations=5,
    ):
        path = _build_roadmap_with_chrome(tmp_path)
        events: list[dict] = []
        call_count = {"n": 0}

        def reflect_hook(rm, pass_result, *, decision_context=""):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Park first.
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "Move file or update criterion?",
                        "options": [
                            {"id": "move", "label": "Move file"},
                            {"id": "update", "label": "Update criterion"},
                        ],
                    },
                )
            # Post-decision: ordinary continue verdict.
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                summary="acted on choice",
            )

        config = AutonomousMissionConfig(
            intent_id="i",
            roadmap_path=path,
            max_iterations=max_iterations,
            full_reflect_cadence=full_reflect_cadence,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        return daemon, events, path, call_count

    def test_pause_does_not_unblock_park(self, tmp_path):
        # Drive _run_full_reflect on a thread; once parked, signal
        # pause. The daemon must stay parked. Then provide_decision
        # to unblock so the test can finish without leaking the
        # thread.
        daemon, events, path, call_count = self._make_daemon_that_parks_once(
            tmp_path,
        )
        rm = roadmap_module.load(path)
        result_holder: dict = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for_event(events, "autonomous_human_decision_required"), (
            "daemon never parked"
        )

        # Signal pause. The thread should stay alive and parked.
        daemon.pause_after_iter("user clicked Pause while parked")
        time.sleep(0.2)  # give it a chance to (incorrectly) wake

        assert t.is_alive(), (
            "pause_after_iter must NOT unblock a parked daemon"
        )
        # No decision_received yet — pause didn't fake a decision.
        assert not _events_of_kind(events, "autonomous_human_decision_received")

        # Now actually provide the decision so the daemon proceeds.
        daemon.provide_decision("move", "")
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert call_count["n"] == 2  # parked + post-decision retry

    def test_decision_then_pause_completes_iter_then_exits(self, tmp_path):
        # Full lifecycle: start daemon, let iter 1 dispatch, REFLECT
        # parks, signal pause, provide decision, daemon resumes
        # iter 1, then exits at next top-of-loop with user_pause.
        daemon, events, path, call_count = self._make_daemon_that_parks_once(
            tmp_path, full_reflect_cadence=1, max_iterations=5,
        )

        daemon.start()
        assert _wait_for_event(events, "autonomous_human_decision_required"), (
            "daemon never parked"
        )

        # Pause queued while parked.
        daemon.pause_after_iter("graceful pause during park")
        # Provide decision — daemon resumes, finishes iter, then exits.
        daemon.provide_decision("update", "")
        daemon.join(timeout=5.0)
        assert not daemon.is_running()

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_pause"
        assert "graceful pause" in paused[0]["stop_message"]
        # The decision flow really happened — both events fired.
        assert _events_of_kind(events, "autonomous_human_decision_required")
        assert _events_of_kind(events, "autonomous_human_decision_received")

    def test_stop_during_park_overrides_queued_pause(self, tmp_path):
        # Both pause and stop arrive while parked. Stop must win:
        # daemon wakes (because stop sets _decision_event explicitly),
        # exits at next stop-rule check with user_stop, NOT user_pause.
        daemon, events, path, _ = self._make_daemon_that_parks_once(
            tmp_path, full_reflect_cadence=1, max_iterations=5,
        )

        daemon.start()
        assert _wait_for_event(events, "autonomous_human_decision_required"), (
            "daemon never parked"
        )

        daemon.pause_after_iter("queued pause")
        daemon.stop("user_stop", "user clicked Stop")
        daemon.join(timeout=3.0)
        assert not daemon.is_running()

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_stop"
        # No decision_received — daemon woke via stop, not via decision.
        assert not _events_of_kind(events, "autonomous_human_decision_received")


# ── Activity phase sequence across the decision flow ────────────────────


class TestActivityPhasesAcrossDecisionFlow:
    """v0.5.9a1 (activity inspector) × v0.5.8a2 (decision-request).

    The activity inspector should report `parked` when a decision is
    pending, distinct from `waiting_dispatch` (sub-mission running)
    and `tick_pause` (between iters). This lets the GUI tell the user
    "YOU are the bottleneck" rather than "the daemon is stuck."
    """

    def test_parked_phase_emitted_with_question_detail(self, tmp_path):
        path = _build_roadmap_with_chrome(tmp_path)
        events: list[dict] = []

        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                decision_request={
                    "question": "Move the file or update the criterion path?",
                    "options": [
                        {"id": "move", "label": "Move"},
                        {"id": "update", "label": "Update"},
                    ],
                },
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        rm = roadmap_module.load(path)
        result_holder: dict = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for_event(events, "autonomous_human_decision_required")

        # Find the parked-activity event.
        parked_events = [
            e for e in events
            if e.get("event") == "autonomous_activity"
            and e.get("phase") == "parked"
        ]
        assert len(parked_events) == 1, (
            f"expected exactly one parked activity event; got "
            f"{len(parked_events)}"
        )
        parked = parked_events[0]
        assert parked["specialist"] == "reflect"
        # The detail should reflect the question (truncated to 80 chars).
        assert "Move the file or update" in parked["detail"]
        # state_snapshot should mirror.
        snap = daemon.state_snapshot()
        assert snap["activity"]["phase"] == "parked"

        # Cleanup: unblock the parked thread.
        daemon.provide_decision("move", "")
        t.join(timeout=2.0)

    def test_no_parked_phase_when_no_decision_request(self, tmp_path):
        # Sanity check: the parked phase ONLY fires when REFLECT emits
        # a decision_request. A normal continue verdict must not emit
        # parked even though the daemon goes through reflecting →
        # tick_pause.
        path = _build_roadmap_with_chrome(tmp_path)
        events: list[dict] = []

        def normal_reflect(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="continue",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=normal_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        daemon.start()
        daemon.join(timeout=3.0)

        phases = _activity_phases(events)
        assert "parked" not in phases
        # Sanity: the normal phases did fire.
        assert "picking" in phases
        assert "reflecting" in phases


# ── Verdict-override reached via the decision-request path ──────────────


class TestOverrideAfterDecisionPath:
    """v0.5.9a3 (verdict-override provenance) × v0.5.8a2 (decision-request).

    Real scenario: model emits decision_request for a path-mismatch.
    User picks "update criterion." Model re-runs and (incorrectly)
    claims `satisfied` because it considers the criterion fixed —
    but the chrome criterion is still unpassed. Daemon must:
      1. Emit BOTH decision_required AND decision_received events.
      2. Apply the cross-check on the post-decision verdict.
      3. Override to `continue` and emit the structured provenance
         fields.
    All in the same iteration.
    """

    def test_decision_then_lying_satisfied_triggers_override(self, tmp_path):
        path = _build_roadmap_with_chrome(tmp_path, n_items=1)
        events: list[dict] = []
        call_count = {"n": 0}

        def reflect_hook(rm, pass_result, *, decision_context=""):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Park.
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "Move file or update criterion?",
                        "options": [
                            {"id": "move", "label": "Move"},
                            {"id": "update", "label": "Update"},
                        ],
                    },
                )
            # Post-decision: lie about satisfaction.
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="satisfied",
                summary="all good!",
            )

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
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        rm = roadmap_module.load(path)
        result_holder: dict = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for_event(events, "autonomous_human_decision_required")

        daemon.provide_decision("update", "use the criterion-path")
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert call_count["n"] == 2

        # Both decision events fired.
        assert _events_of_kind(events, "autonomous_human_decision_required")
        assert _events_of_kind(events, "autonomous_human_decision_received")

        # Reflection event has override provenance.
        reflections = _events_of_kind(events, "autonomous_reflection")
        assert len(reflections) == 1
        r = reflections[0]
        assert r["model_verdict"] == "satisfied"
        assert r["verdict"] == "continue"  # daemon overrode
        assert r["verdict_overridden"] is True
        assert "claimed `satisfied`" in r["override_reason"]
        # The unpassed chrome criterion shows up in the structured list.
        assert any(
            "click toggle" in c for c in r["unpassed_criteria"]
        ), f"chrome criterion missing from unpassed list: {r['unpassed_criteria']}"

        # The outcome returned reflects the override.
        outcome = result_holder["outcome"]
        assert outcome.verdict == "continue"

    def test_no_override_when_post_decision_verdict_is_continue(
        self, tmp_path,
    ):
        # Negative: if the model honestly says `continue` after the
        # user's choice, the override path is NOT taken.
        path = _build_roadmap_with_chrome(tmp_path, n_items=1)
        events: list[dict] = []

        def honest_reflect(rm, pass_result, *, decision_context=""):
            if not decision_context:
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "?",
                        "options": [
                            {"id": "a", "label": "A"},
                            {"id": "b", "label": "B"},
                        ],
                    },
                )
            return FullReflectOutcome(
                pass_result=pass_result, verdict="continue",
                summary="picked b",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=honest_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        rm = roadmap_module.load(path)
        result_holder: dict = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert _wait_for_event(events, "autonomous_human_decision_required")
        daemon.provide_decision("b", "")
        t.join(timeout=3.0)

        reflections = _events_of_kind(events, "autonomous_reflection")
        assert len(reflections) == 1
        r = reflections[0]
        assert r["verdict_overridden"] is False
        assert r["model_verdict"] == "continue"
        assert r["verdict"] == "continue"
        assert r["unpassed_criteria"] == []
