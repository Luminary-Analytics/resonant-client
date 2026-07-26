"""Tests for v0.5.0a5 — `gui/autonomous_loop.py::AutonomousMissionDaemon`.

The daemon is the outer loop for an Autonomous Mission. It picks
roadmap items, dispatches them as Phase-1 sub-missions, marks them
complete in the roadmap, runs a full REFLECT pass every K iterations,
and stops when one of the priority-ordered stopping rules fires.

Every external dependency (sub-mission dispatch, git, REFLECT model
session, CheckContext) is injected via `DaemonHooks`, so these tests
run without a real subprocess, real LLM, or real wall-clock waiting.
The trick: `tick_pause_seconds=0.0` and stub callables that complete
in microseconds keep the whole suite under a second.

Coverage:
- Lifecycle (idempotent start, stop, is_running, state_snapshot)
- All 7 stopping rules in priority order
- Iteration loop happy path + failure path + check_failed_streak
- Full reflect every K iterations + on roadmap empty
- Cross-check: verdict=satisfied while roadmap not converged → override
- SHA validation (valid passes, invalid → "<empty>")
- needs_model_session() == False path (pure-bash converged)
- Events emitted in the right order with the right payloads
- Misconfigured roadmap (no criteria → loud stop)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
    RoadmapItem,
)
from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
    VisionRunner,
)
from resonant_client.orchestration.reflect import ReflectPassResult


# ── Test helpers ────────────────────────────────────────────────────────


def _build_roadmap_on_disk(
    tmp_path: Path,
    items: list[tuple[int, str, str]],
    criteria: list[tuple[str, str]],
) -> Path:
    """Construct a Roadmap in memory and save it to `tmp_path/roadmap.md`.
    Returns the path. `items` are `(tier, title, description)`; criteria
    are `(type, text)`."""
    rm = Roadmap(
        feature="test mission",
        intent_id="test-intent",
        time_budget_label="1h",
        status="running",
    )
    for tier, title, desc in items:
        roadmap_module.add_item(rm, tier=tier, title=title, description=desc)
    for ctype, text in criteria:
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type=ctype, text=text)
        )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


@dataclass
class _StubCallTracker:
    """Records what the daemon called on its hooks. Useful for
    verifying iteration ordering + stopping-rule enforcement."""
    dispatched_items: list[str] = field(default_factory=list)
    waited_handles: list[Any] = field(default_factory=list)
    cancelled_handles: list[Any] = field(default_factory=list)
    sha_reads: int = 0
    sha_validations: list[str] = field(default_factory=list)
    full_reflects: int = 0


def _make_hooks(
    tracker: _StubCallTracker,
    *,
    dispatch_succeeds: bool = True,
    dispatch_error: str = "",
    sha_to_return: Optional[str] = "abc123def456",
    sha_is_valid: bool = True,
    reflect_outcome: Optional[FullReflectOutcome] = None,
    flip_criteria_on_reflect: bool = False,
    roadmap_path: Optional[Path] = None,
    bash_runner: Optional[BashRunner] = None,
    vision_runner: Optional[VisionRunner] = None,
    checkpoint_hook=None,
) -> DaemonHooks:
    """Build a DaemonHooks with simple stubs. Each stub records its
    invocation in `tracker` so tests can assert the right calls
    happened in the right order."""

    handles = iter(range(10000))

    def dispatch_item(item: RoadmapItem) -> int:
        tracker.dispatched_items.append(item.id)
        return next(handles)

    def wait_for_dispatch(handle: int) -> DispatchOutcome:
        tracker.waited_handles.append(handle)
        return DispatchOutcome(
            success=dispatch_succeeds,
            error=dispatch_error,
            handle=handle,
        )

    def cancel_dispatch(handle: int) -> None:
        tracker.cancelled_handles.append(handle)

    def get_commit_sha() -> Optional[str]:
        tracker.sha_reads += 1
        return sha_to_return

    def validate_sha(sha: str) -> bool:
        tracker.sha_validations.append(sha)
        return sha_is_valid

    def run_full_reflect(
        roadmap: Roadmap, pass_result: ReflectPassResult
    ) -> FullReflectOutcome:
        tracker.full_reflects += 1
        if flip_criteria_on_reflect and roadmap_path is not None:
            # Simulate the REFLECT model session validating every
            # outstanding criterion via file_edit. Used by tests
            # that want a `satisfied` verdict to survive the
            # daemon's cross-check (which re-loads the roadmap
            # from disk and refuses to honor satisfied if any
            # criterion is still unpassed).
            for c in roadmap.acceptance_criteria:
                if c.passed is not True and c.type != "manual":
                    c.passed = True
                    c.evidence = "PASS: stub flip"
            roadmap_module.save(roadmap, roadmap_path)
        if reflect_outcome is not None:
            # Re-attach the actual pass_result so cross-check sees
            # current roadmap state.
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict=reflect_outcome.verdict,
                chrome_results=reflect_outcome.chrome_results,
                added_items=reflect_outcome.added_items,
                blocked_items=reflect_outcome.blocked_items,
                manual_pending=reflect_outcome.manual_pending,
                summary=reflect_outcome.summary,
                estimated_remaining_minutes=reflect_outcome.estimated_remaining_minutes,
                error=reflect_outcome.error,
                # Carried through so the decision-park path is reachable from
                # tests at all. Dropping it here silently made the whole
                # park / auto-apply branch untestable through this helper.
                decision_request=reflect_outcome.decision_request,
            )
        return FullReflectOutcome(pass_result=pass_result, verdict="continue")

    def check_context_factory(roadmap: Roadmap) -> CheckContext:
        return CheckContext(
            bash_runner=bash_runner,
            vision_runner=vision_runner,
        )

    return DaemonHooks(
        dispatch_item=dispatch_item,
        wait_for_dispatch=wait_for_dispatch,
        cancel_dispatch=cancel_dispatch,
        get_commit_sha=get_commit_sha,
        validate_sha=validate_sha,
        run_full_reflect=run_full_reflect,
        check_context_factory=check_context_factory,
        checkpoint_hook=checkpoint_hook,
    )


def _make_daemon(
    roadmap_path: Path,
    hooks: DaemonHooks,
    *,
    time_budget_seconds: Optional[float] = None,
    max_iterations: int = 100,
    full_reflect_cadence: int = 3,
    tick_pause_seconds: float = 0.0,
    intent_id: str = "test-intent",
    decision_timeout_seconds: Optional[float] = None,
) -> tuple[AutonomousMissionDaemon, list[dict]]:
    """Build a daemon with an event-collecting on_event callback.
    Returns `(daemon, events_list)` so tests can inspect event order."""
    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)

    config = AutonomousMissionConfig(
        intent_id=intent_id,
        roadmap_path=roadmap_path,
        time_budget_seconds=time_budget_seconds,
        max_iterations=max_iterations,
        full_reflect_cadence=full_reflect_cadence,
        tick_pause_seconds=tick_pause_seconds,
        decision_timeout_seconds=decision_timeout_seconds,
    )
    daemon = AutonomousMissionDaemon(config, hooks, on_event=on_event)
    return daemon, events


def _run_daemon_to_completion(
    daemon: AutonomousMissionDaemon, timeout: float = 5.0
) -> None:
    """Start, join with timeout, fail loudly if the daemon hung."""
    daemon.start()
    daemon.join(timeout=timeout)
    if daemon.is_running():
        daemon.stop()
        daemon.join(timeout=2.0)
        raise AssertionError(
            f"Daemon did not exit within {timeout}s — likely an "
            f"infinite loop in the test setup. State: "
            f"{daemon.state_snapshot()}"
        )


def _events_of_kind(events: list[dict], kind: str) -> list[dict]:
    return [ev for ev in events if ev.get("event") == kind]


# ── Stall detection (v0.6.5 long-running hardening) ──────────────────


class TestStallDetection:
    """The daemon-side wait monitor emits heartbeats during a blocking
    dispatch and enforces a ceiling — on timeout it cancels the stuck
    sub-mission.

    This is the work-stall half of the mission's WaitPolicy. It is NOT the
    human-decision wait: a sub-mission cannot block on a person, because
    `LocalSpecialistRunner` runs it without an `on_user_input` callback and
    `await_user` returns immediately. What this catches is a hung subprocess,
    a non-terminating tool loop, or a model that never stops."""

    def test_timeout_cancels_stuck_dispatch(self, tmp_path):
        tracker = _StubCallTracker()
        base = _make_hooks(tracker)
        released = threading.Event()

        def wait_block(handle):
            tracker.waited_handles.append(handle)
            # Simulate a hung sub-mission: block until the cancel fires.
            released.wait(timeout=5.0)
            return DispatchOutcome(success=False, error="cancelled", handle=handle)

        def cancel_block(handle):
            tracker.cancelled_handles.append(handle)
            released.set()  # a real cancel_dispatch unblocks the wait

        hooks = DaemonHooks(
            dispatch_item=base.dispatch_item,
            wait_for_dispatch=wait_block,
            cancel_dispatch=cancel_block,
            get_commit_sha=base.get_commit_sha,
            validate_sha=base.validate_sha,
            run_full_reflect=base.run_full_reflect,
            check_context_factory=base.check_context_factory,
        )
        daemon, events = _make_daemon(tmp_path / "roadmap.md", hooks)
        daemon.config.heartbeat_seconds = 0.05
        daemon.config.dispatch_timeout_seconds = 0.3
        daemon._iter_count = 1

        item = RoadmapItem(id="T1.1", tier=1, title="stuck item")
        outcome = daemon._wait_with_monitor(item, handle=7, started=time.time())

        assert outcome.success is False
        assert "timed out" in outcome.error
        assert 7 in tracker.cancelled_handles
        assert _events_of_kind(events, "autonomous_iteration_timeout")
        assert _events_of_kind(events, "autonomous_heartbeat")
        # A stalled sub-mission reports through the same vocabulary as an
        # expired human wait, tagged with which kind of wait ended and how it
        # recovered — cancelling here, proceeding at the decision park.
        expired = _events_of_kind(events, "autonomous_wait_expired")
        assert expired, "stall ceiling must report through the shared wait vocabulary"
        assert expired[0]["kind"] == "dispatch"
        assert expired[0]["outcome"] == "cancelled"
        assert expired[0]["item_id"] == "T1.1"
        assert expired[0]["policy"]["dispatch_seconds"] == 0.3

    def test_fast_dispatch_no_timeout_no_cancel(self, tmp_path):
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)  # returns immediately, success=True
        daemon, events = _make_daemon(tmp_path / "roadmap.md", hooks)
        daemon.config.heartbeat_seconds = 5.0
        daemon.config.dispatch_timeout_seconds = 5.0
        daemon._iter_count = 1

        item = RoadmapItem(id="T1.1", tier=1, title="quick item")
        outcome = daemon._wait_with_monitor(item, handle=1, started=time.time())

        assert outcome.success is True
        assert tracker.cancelled_handles == []
        assert not _events_of_kind(events, "autonomous_iteration_timeout")

    def test_monitor_disabled_when_heartbeat_and_ceiling_off(self, tmp_path):
        # heartbeat=0 + ceiling=None → no monitor thread; plain passthrough.
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(tmp_path / "roadmap.md", hooks)
        daemon.config.heartbeat_seconds = 0.0
        daemon.config.dispatch_timeout_seconds = None
        daemon._iter_count = 1

        item = RoadmapItem(id="T1.1", tier=1, title="x")
        outcome = daemon._wait_with_monitor(item, handle=2, started=time.time())
        assert outcome.success is True
        assert not _events_of_kind(events, "autonomous_heartbeat")


# ── Lifecycle ───────────────────────────────────────────────────────────


class TestDaemonLifecycle:
    def test_start_is_idempotent(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path, items=[(1, "T1", "do thing")],
            criteria=[("bash", "`true` exits 0")],
        )
        tracker = _StubCallTracker()
        # Make dispatch hang briefly so we can observe is_running=True.
        slow = threading.Event()

        def wait_slow(handle):
            slow.wait(timeout=0.5)
            return DispatchOutcome(success=True, handle=handle)

        hooks = _make_hooks(tracker)
        hooks = DaemonHooks(
            dispatch_item=hooks.dispatch_item,
            wait_for_dispatch=wait_slow,
            cancel_dispatch=hooks.cancel_dispatch,
            get_commit_sha=hooks.get_commit_sha,
            validate_sha=hooks.validate_sha,
            run_full_reflect=hooks.run_full_reflect,
            check_context_factory=hooks.check_context_factory,
        )
        daemon, events = _make_daemon(path, hooks, max_iterations=1)

        daemon.start()
        # Calling start again is a no-op.
        daemon.start()
        assert daemon.is_running()
        slow.set()
        daemon.join(timeout=2.0)
        assert not daemon.is_running()

    def test_stop_before_start_is_safe(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path, items=[], criteria=[("bash", "x")],
        )
        daemon, _ = _make_daemon(path, _make_hooks(_StubCallTracker()))
        # Doesn't raise.
        daemon.stop()
        assert not daemon.is_running()

    def test_state_snapshot_before_start(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path, items=[], criteria=[("bash", "x")],
        )
        daemon, _ = _make_daemon(path, _make_hooks(_StubCallTracker()))
        snap = daemon.state_snapshot()
        assert snap["iter_count"] == 0
        assert snap["is_running"] is False


# ── The human half of the wait policy ───────────────────────────────────


class TestParkedDecisionExpiry:
    """The decision park, driven end to end rather than through the wait
    primitive alone.

    Opposite recovery from the stall ceiling above: an expired human wait
    PROCEEDS with the option REFLECT nominated, because the work is fine and
    only the decision was missing. Cancelling here would throw away good work
    for the sake of an unanswered question.
    """

    def _hooks_with_decision(self, tracker, path, *, request):
        return _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
                decision_request=request,
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )

    def test_an_expired_park_proceeds_with_the_nominated_option(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = self._hooks_with_decision(tracker, path, request={
            "question": "Move the file or update the criterion?",
            "default_option_id": "update",
            "options": [
                {"id": "move", "label": "Move the file"},
                {"id": "update", "label": "Update the criterion"},
            ],
        })
        daemon, events = _make_daemon(
            path, hooks, max_iterations=1, full_reflect_cadence=1,
            decision_timeout_seconds=0.2,
        )

        _run_daemon_to_completion(daemon, timeout=10.0)

        applied = _events_of_kind(events, "autonomous_decision_auto_applied")
        assert applied, "an expired park must proceed rather than hang"
        assert applied[0]["option_id"] == "update"

        expired = _events_of_kind(events, "autonomous_wait_expired")
        assert expired[0]["kind"] == "human"
        # The decisive contrast with the stall ceiling, which cancels.
        assert expired[0]["outcome"] == "proceeded"
        assert expired[0]["policy"]["human_seconds"] == 0.2

    def test_a_park_with_no_usable_option_is_not_guessed_at(self, tmp_path):
        """Bounding the wait must never mean inventing an answer."""
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = self._hooks_with_decision(tracker, path, request={
            "question": "Something unanswerable",
            "options": [],
        })
        daemon, events = _make_daemon(
            path, hooks, max_iterations=1, full_reflect_cadence=1,
            decision_timeout_seconds=0.2,
        )
        daemon.start()
        # It must still be parked well after the deadline would have expired.
        time.sleep(0.6)
        still_parked = daemon.state_snapshot()["activity"]["phase"] == "parked"
        daemon.stop("user_stop")
        daemon.join(timeout=5.0)

        assert still_parked
        assert not _events_of_kind(events, "autonomous_decision_auto_applied")


# ── Iteration happy path ────────────────────────────────────────────────


class TestIterationHappyPath:
    def test_picks_first_unchecked_item(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "first item"), (1, "T1.2 — second", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks, max_iterations=10)

        _run_daemon_to_completion(daemon)

        # First iteration should have dispatched T1.1 (not T1.2).
        assert tracker.dispatched_items[0] == "T1.1"

    def test_marks_item_complete_with_sha(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            sha_to_return="deadbeef",
            sha_is_valid=True,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        # Re-load the roadmap to confirm the item was checked.
        rm = roadmap_module.load(path)
        item = next(i for i in rm.items if i.id == "T1.1")
        assert item.checked is True
        assert item.commit_sha == "deadbeef"

    def test_invalid_sha_marked_as_empty(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            sha_to_return="bogus_sha",
            sha_is_valid=False,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        rm = roadmap_module.load(path)
        item = next(i for i in rm.items if i.id == "T1.1")
        assert item.checked is True
        # The roadmap parser only writes the `*(shipped at sha)*`
        # suffix when commit_sha is a real 6-40-hex SHA, so an
        # invalid SHA round-trips as commit_sha="" with no item
        # suffix. The iteration log is where the `<empty>` marker
        # lives so the user can see WHICH iteration shipped without
        # a verifiable commit.
        assert item.commit_sha == ""
        assert len(rm.iteration_log) >= 1
        assert "<empty>" in rm.iteration_log[0].note

    def test_emits_iteration_lifecycle_events(self, tmp_path):
        # Use a [chrome] criterion so the deterministic prelude
        # produces chrome_pending=[criterion] → needs_model_session()
        # is True → the daemon dispatches the model session, which
        # uses our stubbed reflect_outcome to return `satisfied`.
        # Also flip `criterion.passed=True` in the stub so the
        # cross-check (re-load from disk) sees a converged roadmap
        # and doesn't override the satisfied verdict.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1 — first", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()

        def reflect_with_chrome_marked(rm, pass_result):
            # Simulate the REFLECT model session flipping the
            # [chrome] criterion to passed via file_edit, then
            # save + return satisfied.
            rm.acceptance_criteria[0].passed = True
            rm.acceptance_criteria[0].evidence = "PASS: model validated"
            roadmap_module.save(rm, path)
            tracker.full_reflects += 1
            return FullReflectOutcome(
                pass_result=pass_result, verdict="satisfied",
                summary="all chrome criteria passed",
            )

        base = _make_hooks(tracker)
        hooks = DaemonHooks(
            dispatch_item=base.dispatch_item,
            wait_for_dispatch=base.wait_for_dispatch,
            cancel_dispatch=base.cancel_dispatch,
            get_commit_sha=base.get_commit_sha,
            validate_sha=base.validate_sha,
            run_full_reflect=reflect_with_chrome_marked,
            check_context_factory=base.check_context_factory,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        # Expected event sequence:
        # autonomous_mission_started → autonomous_iteration_started →
        # autonomous_iteration_complete → autonomous_reflection
        # → autonomous_mission_complete
        kinds = [ev["event"] for ev in events]
        assert kinds[0] == "autonomous_mission_started"
        assert "autonomous_iteration_started" in kinds
        assert "autonomous_iteration_complete" in kinds
        assert "autonomous_reflection" in kinds
        assert kinds[-1] == "autonomous_mission_complete"

    def test_creates_checkpoint_before_iteration_dispatch(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "checkpoint me", "")],
            criteria=[("bash", "`ok` exits 0")],
        )
        tracker = _StubCallTracker()
        calls = []

        def checkpoint_hook(**kwargs):
            calls.append(kwargs)
            return {"ref": "refs/resonant/checkpoints/test/0001", "commit": "abc"}

        hooks = _make_hooks(
            tracker,
            checkpoint_hook=checkpoint_hook,
            bash_runner=BashRunner(_run=lambda *args, **kwargs: (0, "ok", "")),
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        assert calls == [{
            "intent_id": "test-intent",
            "iteration": 1,
            "item_id": "T1.1",
        }]
        kinds = [event["event"] for event in events]
        assert kinds.index("autonomous_iteration_checkpoint") < kinds.index(
            "autonomous_iteration_started"
        )


# ── Failed iteration ────────────────────────────────────────────────────


class TestFailedIteration:
    def test_failed_dispatch_increments_streak(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "first"), (1, "T1.2", "second"), (1, "T1.3", "third")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            dispatch_succeeds=False,
            dispatch_error="implementer broke",
        )
        daemon, events = _make_daemon(
            path, hooks,
            full_reflect_cadence=999,  # don't trigger full reflect
        )

        _run_daemon_to_completion(daemon)

        # Default check_failed_streak_limit=2, so 2 failures stop us.
        failed_events = _events_of_kind(events, "autonomous_iteration_failed")
        assert len(failed_events) >= 2

        # Final event should be paused with reason=check_failed.
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "check_failed"

    def test_successful_iteration_resets_streak(self, tmp_path):
        # 3 items + 3 dispatch results: fail, success, fail. With
        # check_failed_streak_limit=2, if streak DIDN'T reset on the
        # success in iter 2, we'd stop after iter 1+3 with
        # `check_failed`. Because it DOES reset, we run all 3 iters,
        # then stop on iteration_cap (3).
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", ""), (1, "T1.2", ""), (1, "T1.3", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        results = iter([
            DispatchOutcome(success=False, error="boom1"),
            DispatchOutcome(success=True),
            DispatchOutcome(success=False, error="boom3"),
        ])

        def wait(handle):
            return next(results)

        base = _make_hooks(tracker)
        hooks = DaemonHooks(
            dispatch_item=base.dispatch_item,
            wait_for_dispatch=wait,
            cancel_dispatch=base.cancel_dispatch,
            get_commit_sha=base.get_commit_sha,
            validate_sha=base.validate_sha,
            run_full_reflect=base.run_full_reflect,
            check_context_factory=base.check_context_factory,
        )
        daemon, events = _make_daemon(
            path, hooks,
            max_iterations=3,
            full_reflect_cadence=999,  # don't trigger reflect
        )

        _run_daemon_to_completion(daemon)

        # Stop reason should be iteration_cap (not check_failed) —
        # proves the success reset the streak so the second failure
        # didn't trip the limit.
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "iteration_cap"

        # All 3 dispatches happened (proves we didn't stop early).
        assert len(tracker.dispatched_items) == 3


# ── Stopping rules ──────────────────────────────────────────────────────


class TestStoppingRules:
    def test_user_stop_takes_priority(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        # Hang in dispatch so we have time to call stop().
        block = threading.Event()

        def wait_block(handle):
            block.wait(timeout=2.0)
            return DispatchOutcome(success=True, handle=handle)

        base = _make_hooks(tracker)
        hooks = DaemonHooks(
            dispatch_item=base.dispatch_item,
            wait_for_dispatch=wait_block,
            cancel_dispatch=base.cancel_dispatch,
            get_commit_sha=base.get_commit_sha,
            validate_sha=base.validate_sha,
            run_full_reflect=base.run_full_reflect,
            check_context_factory=base.check_context_factory,
        )
        daemon, events = _make_daemon(path, hooks)
        daemon.start()
        # Wait for the daemon to enter wait_for_dispatch.
        time.sleep(0.05)
        daemon.stop("user_stop", "user clicked stop")
        block.set()
        daemon.join(timeout=2.0)

        # cancel_dispatch should have been called on the in-flight handle.
        assert len(tracker.cancelled_handles) == 1

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "user_stop"

    def test_time_budget_stops_loop(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 20)],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        # 0.05s budget — should fire after the first iteration.
        daemon, events = _make_daemon(
            path, hooks,
            time_budget_seconds=0.05,
            tick_pause_seconds=0.05,
            full_reflect_cadence=999,
        )

        _run_daemon_to_completion(daemon, timeout=3.0)

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "time_budget_exhausted"

    def test_iteration_cap_stops_loop(self, tmp_path):
        # Many items, no time budget, low iteration cap.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 10)],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(
            path, hooks,
            max_iterations=3,
            full_reflect_cadence=999,
        )

        _run_daemon_to_completion(daemon)

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "iteration_cap"

    def test_satisfied_verdict_completes_mission(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        assert complete[0]["stop_reason"] == "satisfied"

    def test_blocked_streak_stops_loop(self, tmp_path):
        # Four items, full_reflect_cadence=1 so we reflect after each.
        # Each reflect returns blocked. Default streak_limit=3 →
        # stop after the third blocked verdict (in iter 3).
        # Use [chrome] criterion so the model session fires (and
        # uses our stubbed reflect_outcome=blocked).
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 5)],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="blocked",
            ),
        )
        daemon, events = _make_daemon(
            path, hooks, full_reflect_cadence=1,
        )

        _run_daemon_to_completion(daemon)

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "blocked"

    def test_misconfigured_roadmap_stops_loud(self, tmp_path):
        # No acceptance criteria → daemon should refuse to run.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["stop_reason"] == "misconfigured"
        # Daemon should NOT have dispatched anything.
        assert tracker.dispatched_items == []


# ── Full reflect cadence ────────────────────────────────────────────────


class TestFullReflectCadence:
    def test_full_reflect_runs_every_k_iterations(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 8)],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="continue",
            ),
        )
        daemon, events = _make_daemon(
            path, hooks,
            full_reflect_cadence=3,
            max_iterations=6,
        )

        _run_daemon_to_completion(daemon)

        # 6 iterations with cadence 3 → reflect after iter 3 and 6.
        # Tracker counts model-session reflects only (when
        # `needs_model_session()` is True). We use a non-empty
        # criterion list so deterministic pass returns
        # `chrome_pending=[]` AND `manual_pending=[]` AND has bash
        # criteria → after the deterministic pass, those criteria
        # have `passed=...` written, so re-runs skip them. With our
        # stub bash runner being None (no _run), the dispatch falls
        # through to subprocess — which will likely fail. So the
        # criterion passed=False → not converged → not skip.
        #
        # Just count the autonomous_reflection events that fired.
        reflections = _events_of_kind(events, "autonomous_reflection")
        # iter 3 + iter 6 = 2 reflections
        assert len(reflections) == 2

    def test_full_reflect_runs_on_empty_roadmap(self, tmp_path):
        # No items → first action is full reflect.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        reflections = _events_of_kind(events, "autonomous_reflection")
        assert len(reflections) == 1


# ── Cross-check (the "convergence is real" guard) ───────────────────────


class TestConvergenceCrossCheck:
    def test_satisfied_verdict_overridden_when_roadmap_disagrees(
        self, tmp_path
    ):
        # A roadmap with a [chrome] criterion that's NOT marked
        # passed=True. Model claims `verdict=satisfied` (lying).
        # Daemon should override to `continue`.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        # Model claims satisfied but doesn't actually flip the
        # criterion's passed field in the file.
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
                summary="all done!",
            ),
        )
        daemon, events = _make_daemon(
            path, hooks,
            max_iterations=2,  # let one iter run, then reflect, then stop
        )

        _run_daemon_to_completion(daemon)

        # The autonomous_reflection event should show verdict=continue
        # (overridden), not satisfied.
        reflections = _events_of_kind(events, "autonomous_reflection")
        assert len(reflections) >= 1
        for r in reflections:
            assert r["verdict"] != "satisfied", (
                "Daemon failed to override model's bogus satisfied verdict"
            )

    def test_satisfied_verdict_kept_when_roadmap_agrees(self, tmp_path):
        # Roadmap with one [bash] criterion. Stub runs it → passed=True.
        # needs_model_session() is False (no chrome/manual). Daemon
        # mechanically declares satisfied. Verdict should stick.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("bash", "`true` exits 0")],
        )

        def good_run(cmd, **kw):
            return (0, "ok\n", "")

        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            bash_runner=BashRunner(_run=good_run),
        )
        daemon, events = _make_daemon(path, hooks, full_reflect_cadence=1)

        _run_daemon_to_completion(daemon)

        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        # The model session shouldn't have been called at all —
        # pure-bash converged path skips it.
        assert tracker.full_reflects == 0


# ── Pure-bash spec converges without model session ──────────────────────


class TestPureBashConvergence:
    def test_no_model_session_when_all_bash_pass(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[
                ("bash", "`true` exits 0"),
                ("bash", "`echo hi` exits 0"),
            ],
        )

        def good_run(cmd, **kw):
            return (0, "ok\n", "")

        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            bash_runner=BashRunner(_run=good_run),
        )
        daemon, events = _make_daemon(path, hooks, full_reflect_cadence=1)

        _run_daemon_to_completion(daemon)

        # full_reflects counts model-session calls, NOT deterministic
        # passes. For pure-bash all-passing, model session is skipped.
        assert tracker.full_reflects == 0

        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        assert complete[0]["stop_reason"] == "satisfied"


class TestAcceptanceRepairItems:
    def test_failed_criterion_becomes_deduplicated_repair_work(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[],
            criteria=[("bash", "`repair-check` exits 0")],
        )
        tracker = _StubCallTracker()

        def fail_until_repair_dispatched(cmd, **kw):
            if tracker.dispatched_items:
                return (0, "fixed\n", "")
            return (1, "", "still broken\n")

        hooks = _make_hooks(
            tracker,
            roadmap_path=path,
            bash_runner=BashRunner(_run=fail_until_repair_dispatched),
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        repair_events = _events_of_kind(
            events, "autonomous_repair_items_added"
        )
        assert len(repair_events) == 1
        assert repair_events[0]["count"] == 1
        assert len(tracker.dispatched_items) == 1
        assert tracker.dispatched_items[0].startswith("T1.")

        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        assert complete[0]["stop_reason"] == "satisfied"

        persisted = roadmap_module.load(path)
        assert len(persisted.items) == 1
        assert persisted.items[0].checked is True
        assert "acceptance-repair:" in persisted.items[0].description

    def test_existing_repair_marker_prevents_duplicate(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[],
            criteria=[("bash", "`repair-check` exits 0")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, _ = _make_daemon(path, hooks)
        rm = roadmap_module.load(path)
        rm.acceptance_criteria[0].passed = False
        rm.acceptance_criteria[0].evidence = "exit=1"

        first = daemon._ensure_acceptance_repair_items(rm)
        second = daemon._ensure_acceptance_repair_items(rm)

        assert len(first) == 1
        assert second == []
        assert len(rm.items) == 1


# ── State snapshot ──────────────────────────────────────────────────────


class TestStateSnapshot:
    def test_snapshot_reflects_iteration_progress(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", ""), (1, "T1.2", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
        )
        daemon, events = _make_daemon(
            path, hooks, full_reflect_cadence=1,
            time_budget_seconds=60.0,
        )

        _run_daemon_to_completion(daemon)

        snap = daemon.state_snapshot()
        assert snap["intent_id"] == "test-intent"
        assert snap["iter_count"] >= 1
        assert snap["is_running"] is False
        assert snap["time_budget_seconds"] == 60.0


# ── Iteration log appended ──────────────────────────────────────────────


class TestIterationLogPersisted:
    def test_iteration_log_appended_on_disk(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            sha_to_return="abc1234",
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, _ = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        rm = roadmap_module.load(path)
        assert len(rm.iteration_log) >= 1
        first = rm.iteration_log[0]
        # The roadmap markdown serializes iter_num + timestamp +
        # duration + note as the structured triplet; item_id and
        # commit_sha are encoded INSIDE the note prose ("shipped
        # T1.1"). The parser doesn't sub-parse the note back out,
        # so assertions on the note text are what survives.
        assert first.iter_num == 1
        assert "T1.1" in first.note  # daemon wrote "shipped T1.1"


# ── v0.5.6a3 — Atomic terminal-state transition ─────────────────────────


class TestAtomicTerminalStateTransition:
    """v0.5.6a3 — `_emit_stop` must update roadmap.md `**Status:**` to
    match the terminal state (complete/paused/failed) BEFORE emitting
    the WS event. Without this, the GUI badge clears (response to the
    event) but the on-disk roadmap keeps `running`, so the next-launch
    orphan-detection scanner falsely offers to resume a satisfied or
    stuck mission. Linux-bridge field-observation #6.

    The terminal event payload must also include `new_phase` so the
    WS handler in app.py can update session.mission_state.phase
    atomically with the badge transition.
    """

    def test_satisfied_writes_complete_to_roadmap_status(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        # Confirm starting state.
        assert roadmap_module.load(path).status == "running"
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        # On-disk roadmap status must be `complete` after the daemon
        # exits with a `satisfied` verdict.
        assert roadmap_module.load(path).status == "complete"

        # The terminal event must carry `new_phase` so the WS handler
        # can update the session mission_state.
        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        assert complete[0]["new_phase"] == "autonomous_complete"

    def test_iteration_cap_writes_paused_to_roadmap_status(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 10)],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(
            path, hooks,
            max_iterations=2,
            full_reflect_cadence=999,
        )

        _run_daemon_to_completion(daemon)

        # Stop-reason that ISN'T in _COMPLETE_REASONS → paused state.
        assert roadmap_module.load(path).status == "paused"
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["new_phase"] == "autonomous_paused"

    def test_blocked_writes_paused_to_roadmap_status(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, f"T1.{i}", "") for i in range(1, 5)],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="blocked",
            ),
        )
        daemon, events = _make_daemon(
            path, hooks, full_reflect_cadence=1,
        )

        _run_daemon_to_completion(daemon)

        # `blocked` is the field-observation #6 stuck case — must
        # write `paused` so orphan detection doesn't re-offer it.
        assert roadmap_module.load(path).status == "paused"
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["new_phase"] == "autonomous_paused"

    def test_misconfigured_writes_paused_to_roadmap_status(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[],  # no criteria → misconfigured
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        assert roadmap_module.load(path).status == "paused"
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["new_phase"] == "autonomous_paused"

    def test_status_update_is_idempotent_when_already_at_target(
        self, tmp_path,
    ):
        # Pre-set roadmap to `complete` so `_emit_stop` should no-op
        # the write but still emit the event. No exception, no
        # duplicate save side effects.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        rm = roadmap_module.load(path)
        rm.status = "complete"
        roadmap_module.save(rm, path)
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,  # this WILL re-save mid-run
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        _run_daemon_to_completion(daemon)

        # Idempotency check: status remains `complete` (didn't get
        # downgraded to `running` then re-upgraded). The mtime
        # changes due to the criterion flip + iteration log append,
        # so we can't assert mtime equality — just status correctness.
        assert roadmap_module.load(path).status == "complete"

    def test_roadmap_save_failure_does_not_block_terminal_event(
        self, tmp_path, monkeypatch,
    ):
        # If `_update_roadmap_status_safely` can't write (disk full,
        # locked file, whatever), the daemon must still emit the
        # terminal WS event so the GUI badge updates. Drift is logged
        # but doesn't cascade into a crash.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("chrome", "click toggle")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(
            tracker,
            reflect_outcome=FullReflectOutcome(
                pass_result=ReflectPassResult(),
                verdict="satisfied",
            ),
            flip_criteria_on_reflect=True,
            roadmap_path=path,
        )
        daemon, events = _make_daemon(path, hooks)

        # Patch save() so the FINAL status-update write blows up.
        # Earlier in-loop saves still work (otherwise the daemon
        # can't function), so we only fail when called from
        # `_update_roadmap_status_safely` — detect by call site.
        from resonant_client.gui import autonomous_loop as al

        original_save = roadmap_module.save
        save_calls = {"count": 0, "last_failed": False}

        def picky_save(rm, target_path):
            save_calls["count"] += 1
            # Terminal status writes set status to one of the terminal
            # values — refuse those, allow everything else.
            if rm.status in ("complete", "paused", "failed"):
                save_calls["last_failed"] = True
                raise OSError("simulated disk failure")
            return original_save(rm, target_path)

        monkeypatch.setattr(al.roadmap_module, "save", picky_save)

        _run_daemon_to_completion(daemon)

        # Even though the status-write failed, the WS terminal event
        # MUST have fired (GUI gets to update its badge).
        complete = _events_of_kind(events, "autonomous_mission_complete")
        assert len(complete) == 1
        # Confirms our patch actually fired on the terminal write.
        assert save_calls["last_failed"] is True

    def test_failed_path_writes_failed_status_and_new_phase(
        self, tmp_path, monkeypatch,
    ):
        # Force the daemon's run-loop to crash so it hits the
        # `autonomous_mission_failed` emit path. `dispatch_item` and
        # similar hooks have internal try/except — they don't bubble.
        # `_load_roadmap` does NOT have a wrapper; monkey-patching
        # it to raise hits the daemon's outer except.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(path, hooks, full_reflect_cadence=999)

        # Patch the instance's _load_roadmap to raise on first call.
        def boom_load():
            raise RuntimeError("simulated unrecoverable load failure")

        monkeypatch.setattr(daemon, "_load_roadmap", boom_load)

        _run_daemon_to_completion(daemon)

        # Crash → failed event → roadmap status `failed`.
        failed = _events_of_kind(events, "autonomous_mission_failed")
        assert len(failed) == 1
        assert failed[0]["new_phase"] == "autonomous_failed"
        # The `_update_roadmap_status_safely` call in the failed path
        # ALSO calls `_load_roadmap`, which we patched to raise — so
        # the safe-update silently logs+returns. The on-disk roadmap
        # remains at its previous status (in this test, `running`).
        # The contract is: terminal event still fires + new_phase set
        # so the WS-handler half can still update session state.
        # Disk-side drift is acceptable when the disk is the thing
        # that's broken in the first place.
        assert roadmap_module.load(path).status in ("running", "failed")

    def test_failed_path_writes_failed_status_when_disk_works(
        self, tmp_path, monkeypatch,
    ):
        # Like the above, but with a more surgical injection — only
        # the FIRST load (in run loop) raises, subsequent loads (from
        # _update_roadmap_status_safely) succeed. Verifies the happy
        # case where the loop dies but the disk is healthy enough to
        # write the failed status.
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        hooks = _make_hooks(tracker)
        daemon, events = _make_daemon(path, hooks, full_reflect_cadence=999)

        original_load = daemon._load_roadmap
        load_calls = {"count": 0}

        def conditional_boom():
            load_calls["count"] += 1
            if load_calls["count"] == 1:
                raise RuntimeError("first load fails")
            return original_load()

        monkeypatch.setattr(daemon, "_load_roadmap", conditional_boom)

        _run_daemon_to_completion(daemon)

        failed = _events_of_kind(events, "autonomous_mission_failed")
        assert len(failed) == 1
        assert failed[0]["new_phase"] == "autonomous_failed"
        # Second load (from _update_roadmap_status_safely) succeeds,
        # so the failed status DOES get written.
        assert roadmap_module.load(path).status == "failed"

    def test_user_stop_writes_paused_to_roadmap_status(self, tmp_path):
        path = _build_roadmap_on_disk(
            tmp_path,
            items=[(1, "T1.1", "")],
            criteria=[("bash", "x")],
        )
        tracker = _StubCallTracker()
        block = threading.Event()

        def wait_block(handle):
            block.wait(timeout=2.0)
            return DispatchOutcome(success=True, handle=handle)

        base = _make_hooks(tracker)
        hooks = DaemonHooks(
            dispatch_item=base.dispatch_item,
            wait_for_dispatch=wait_block,
            cancel_dispatch=base.cancel_dispatch,
            get_commit_sha=base.get_commit_sha,
            validate_sha=base.validate_sha,
            run_full_reflect=base.run_full_reflect,
            check_context_factory=base.check_context_factory,
        )
        daemon, events = _make_daemon(path, hooks)
        daemon.start()
        time.sleep(0.05)
        daemon.stop("user_stop", "user clicked stop")
        block.set()
        daemon.join(timeout=2.0)

        # User-initiated stop → paused on disk (so it CAN be resumed
        # — `paused` is the only status orphan-detection picks up).
        assert roadmap_module.load(path).status == "paused"
        paused = _events_of_kind(events, "autonomous_mission_paused")
        assert len(paused) == 1
        assert paused[0]["new_phase"] == "autonomous_paused"


# ══════════════════════════════════════════════════════════════════════
# Park deadline
#
# A park blocks the entire mission on a person. Unattended, that is
# indefinite dead wall-clock time that looks identical to healthy work.
# ══════════════════════════════════════════════════════════════════════


class TestParkDeadline:
    def _request(self, **overrides):
        request = {
            "question": "Move the file or update the criterion?",
            "options": [
                {"id": "update", "label": "Update the criterion"},
                {"id": "move", "label": "Move the file"},
            ],
        }
        request.update(overrides)
        return request

    def test_default_option_prefers_the_declared_one(self):
        pick = AutonomousMissionDaemon._default_decision_option(
            self._request(default_option_id="move")
        )
        assert pick == "move"

    def test_default_option_falls_back_to_the_first_offered(self):
        pick = AutonomousMissionDaemon._default_decision_option(self._request())
        assert pick == "update"

    def test_declared_default_must_be_a_real_option(self):
        """A nominated id that isn't offered must not be applied verbatim."""
        pick = AutonomousMissionDaemon._default_decision_option(
            self._request(default_option_id="not-an-option")
        )
        assert pick == "update"

    def test_no_options_yields_no_default(self):
        assert AutonomousMissionDaemon._default_decision_option({"options": []}) == ""
        assert AutonomousMissionDaemon._default_decision_option({}) == ""

    def test_wait_returns_after_the_deadline_without_a_response(self, tmp_path):
        daemon, _ = _make_daemon(
            tmp_path / "roadmap.md",
            _make_hooks(_StubCallTracker()),
            decision_timeout_seconds=0.2,
        )
        started = time.time()

        proceeded = daemon._wait_for_decision(0.2)

        assert proceeded is True
        assert time.time() - started >= 0.2
        # No human answered, so nothing is queued — that emptiness is the
        # signal the caller uses to apply the declared default.
        assert daemon._consume_pending_decision() == {}

    def test_a_real_decision_still_wins_before_the_deadline(self, tmp_path):
        daemon, _ = _make_daemon(
            tmp_path / "roadmap.md",
            _make_hooks(_StubCallTracker()),
            decision_timeout_seconds=30.0,
        )
        threading.Timer(0.05, lambda: daemon.provide_decision("move", "go")).start()
        started = time.time()

        proceeded = daemon._wait_for_decision(30.0)

        assert proceeded is True
        assert time.time() - started < 5.0
        assert daemon._consume_pending_decision()["option_id"] == "move"

    def test_stop_still_beats_the_deadline(self, tmp_path):
        daemon, _ = _make_daemon(
            tmp_path / "roadmap.md",
            _make_hooks(_StubCallTracker()),
            decision_timeout_seconds=30.0,
        )
        threading.Timer(0.05, lambda: daemon.stop("user_stop")).start()

        assert daemon._wait_for_decision(30.0) is False

    def test_without_a_deadline_the_wait_is_still_unbounded(self, tmp_path):
        """The opt-in must not silently change existing missions."""
        daemon, _ = _make_daemon(
            tmp_path / "roadmap.md", _make_hooks(_StubCallTracker()),
        )
        assert daemon.config.decision_timeout_seconds is None

        # With no deadline the wait only ends on a decision or a stop.
        threading.Timer(0.05, lambda: daemon.stop("user_stop")).start()
        assert daemon._wait_for_decision(None) is False
