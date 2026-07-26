"""Tests for v0.5.9a4 — pause-after-current-iter.

Distinct from `stop()` which cancels mid-flight. `pause_after_iter`
sets a flag the next top-of-loop stop-rule check picks up; the
in-flight iteration completes naturally, then the daemon exits with
stop_reason="user_pause".
"""
from __future__ import annotations

import threading
import time


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
    CheckContext,
)


def _build_roadmap(tmp_path, n_items=3):
    rm = Roadmap(
        feature="t", intent_id="i",
        time_budget_label="1h", status="running",
    )
    for i in range(1, n_items + 1):
        roadmap_module.add_item(rm, tier=1, title=f"T1.{i}", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="bash", text="`true` exits 0"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


class TestPauseAfterIter:
    def test_pause_flag_lets_in_flight_iter_complete(self, tmp_path):
        # 3 items, big iteration cap, no time budget. Pause after
        # iter 1 starts; daemon should complete iter 1 then exit
        # with stop_reason="user_pause" — NOT cancel mid-iter.
        path = _build_roadmap(tmp_path, n_items=3)
        events = []
        # Block in wait_for_dispatch so we can observe the in-flight
        # state, then unblock to let it complete.
        block = threading.Event()

        def wait_block(handle):
            block.wait(timeout=2.0)
            return DispatchOutcome(success=True, handle=handle)

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=10, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=wait_block,
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()

        # Wait until iter 1 is in flight (waiting_dispatch phase).
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if any(
                e.get("event") == "autonomous_iteration_started"
                for e in events
            ):
                break
            time.sleep(0.01)

        # Now request pause and unblock. Daemon should complete
        # iter 1 then exit with user_pause.
        daemon.pause_after_iter("user clicked Pause")
        block.set()
        daemon.join(timeout=3.0)

        # The dispatched iter must have completed (got an
        # iteration_complete event).
        complete_events = [
            e for e in events
            if e.get("event") == "autonomous_iteration_complete"
        ]
        assert len(complete_events) >= 1, (
            "iter 1 should have completed before pause fired"
        )

        # Final event = mission_paused with stop_reason=user_pause.
        paused = [
            e for e in events
            if e.get("event") == "autonomous_mission_paused"
        ]
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_pause"
        assert "user clicked Pause" in paused[0]["stop_message"]

    def test_pause_does_not_dispatch_more_iters(self, tmp_path):
        # 3 items, pause requested before iter 2 fires. Daemon should
        # exit after iter 1 — iter 2 should NOT be dispatched.
        path = _build_roadmap(tmp_path, n_items=3)
        events = []
        dispatched = []

        def dispatch(item):
            dispatched.append(item.id)
            return len(dispatched)

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=10, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=dispatch,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        # Request pause BEFORE start so the first stop-rule check
        # at the top of iter 1 sees the flag and returns user_pause
        # immediately. Daemon dispatches 0 iters in this scenario.
        daemon.pause_after_iter("pre-start pause")
        daemon.start()
        daemon.join(timeout=3.0)

        # Daemon exited cleanly; user_pause is the stop reason.
        paused = [
            e for e in events
            if e.get("event") == "autonomous_mission_paused"
        ]
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_pause"
        # No iters dispatched (pause caught at top of first iter).
        assert dispatched == []

    def test_pause_idempotent(self, tmp_path):
        # Calling pause_after_iter twice should not crash and only
        # the first message should win.
        path = _build_roadmap(tmp_path, n_items=1)
        events = []
        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=10, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)

        daemon.pause_after_iter("first")
        daemon.pause_after_iter("second")  # Second call is no-op
        daemon.start()
        daemon.join(timeout=3.0)

        paused = [
            e for e in events
            if e.get("event") == "autonomous_mission_paused"
        ]
        assert len(paused) == 1
        # First message wins.
        assert "first" in paused[0]["stop_message"]
        assert "second" not in paused[0]["stop_message"]

    def test_stop_takes_priority_over_pause(self, tmp_path):
        # If both pause and stop are signalled, stop wins (higher
        # priority in the stop-rules check). The daemon's stop_reason
        # should reflect stop, not pause.
        path = _build_roadmap(tmp_path, n_items=3)
        events = []
        block = threading.Event()

        def wait_block(handle):
            block.wait(timeout=2.0)
            return DispatchOutcome(success=True, handle=handle)

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=10, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=wait_block,
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()

        # Wait until iter 1 is in flight.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if any(
                e.get("event") == "autonomous_iteration_started"
                for e in events
            ):
                break
            time.sleep(0.01)

        # Both signals — pause first, then stop. Stop should win.
        daemon.pause_after_iter("pausing")
        daemon.stop("user_stop", "user clicked Stop")
        block.set()
        daemon.join(timeout=3.0)

        paused = [
            e for e in events
            if e.get("event") == "autonomous_mission_paused"
        ]
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_stop"
        assert "Stop" in paused[0]["stop_message"]
