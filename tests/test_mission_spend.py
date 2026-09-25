"""A mission's spending limit: the roadmap, the daemon's stop rule and the launch wiring."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lumi.gui import roadmap as roadmap_module
from lumi.gui.autonomous_iter_cost import AutonomousIterCostTracker
from lumi.gui.autonomous_loop import (
    AutonomousMissionConfig,
    AutonomousMissionDaemon,
    DaemonHooks,
    DispatchOutcome,
)
from lumi.gui.autonomous_session import (
    build_roadmap_from_spec,
    parse_spend_limit,
    resume_autonomous_mission,
    start_autonomous_mission,
)
from tests.test_autonomous_loop import (
    _build_roadmap_on_disk,
    _events_of_kind,
    _make_hooks,
    _run_daemon_to_completion,
    _StubCallTracker,
    _tracked_daemon,
)
from tests.test_autonomous_session import _SPEC_MD, _StubAppState, _StubProject


def _daemon(path: Path, hooks: DaemonHooks, **config) -> tuple[AutonomousMissionDaemon, list[dict]]:
    # Tracked, so the runs below end on the daemon's terminal event rather
    # than a wall-clock budget (see tests/test_autonomous_loop.py).
    settings = {"tick_pause_seconds": 0.0, "full_reflect_cadence": 999, **config}
    return _tracked_daemon(
        AutonomousMissionConfig(intent_id="test-intent", roadmap_path=path, **settings), hooks)


def test_the_limit_and_the_spend_are_kept_in_the_roadmap(tmp_path):
    rm = roadmap_module.Roadmap(feature="f", intent_id="i", time_budget_label="4h",
                                spend_limit_label="$25.00", spent_label="$3.20")
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    text = path.read_text(encoding="utf-8")
    assert "**Spending limit:** $25.00" in text and "**Spent so far:** $3.20" in text
    loaded = roadmap_module.load(path)
    assert (loaded.spend_limit_label, loaded.spent_label) == ("$25.00", "$3.20")
    # A roadmap without them is written as before.
    roadmap_module.save(roadmap_module.Roadmap(feature="f", intent_id="i"), path)
    assert "Spending limit" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("label,expected", [
    ("$25", 25.0), ("25.5", 25.5), (" $1,000 ", 1000.0), ("", None), ("abc", None), ("0", None), ("-5", None),
])
def test_parsing_a_limit(label, expected):
    assert parse_spend_limit(label) == expected


def test_the_mission_stops_before_an_iteration_past_the_limit(tmp_path):
    path = _build_roadmap_on_disk(tmp_path, items=[(1, f"T1.{i}", "") for i in range(1, 6)],
                                  criteria=[("bash", "x")])
    calls = _StubCallTracker()
    base = _make_hooks(calls)
    # Each sub-mission costs $2.
    base.spent_usd = lambda: 2.0 * len(calls.waited_handles)
    daemon, events = _daemon(path, base, spend_limit_usd=3.0)
    _run_daemon_to_completion(daemon)

    assert len(calls.dispatched_items) == 2  # $2 after the first, $4 after the second
    paused = _events_of_kind(events, "autonomous_mission_paused")
    assert paused[0]["stop_reason"] == "spend_limit_reached"
    assert paused[0]["stop_message"] == "spent $4.00 of the $3.00 limit"
    assert (paused[0]["spent_usd"], paused[0]["spend_limit_usd"]) == (4.0, 3.0)
    assert _events_of_kind(events, "autonomous_mission_started")[0]["spend_limit_usd"] == 3.0
    saved = roadmap_module.load(path)
    assert saved.spent_label == "$4.00" and saved.status == "paused"


def test_a_running_sub_mission_is_stopped_at_the_limit(tmp_path):
    path = _build_roadmap_on_disk(tmp_path, items=[(1, "T1.1", "")], criteria=[("bash", "x")])
    calls = _StubCallTracker()
    base = _make_hooks(calls)
    cancelled = threading.Event()
    spent = {"usd": 0.0}

    def wait_until_cancelled(handle):
        spent["usd"] = 12.5  # the sub-mission spends while it runs
        assert cancelled.wait(5.0), "the sub-mission was never cancelled"
        return DispatchOutcome(success=False, error="cancelled", handle=handle)

    def cancel(handle):
        calls.cancelled_handles.append(handle)
        cancelled.set()

    hooks = DaemonHooks(
        dispatch_item=base.dispatch_item, wait_for_dispatch=wait_until_cancelled, cancel_dispatch=cancel,
        get_commit_sha=base.get_commit_sha, validate_sha=base.validate_sha,
        run_full_reflect=base.run_full_reflect, check_context_factory=base.check_context_factory,
        spent_usd=lambda: spent["usd"])
    daemon, events = _daemon(path, hooks, spend_limit_usd=10.0, heartbeat_seconds=0.05)
    _run_daemon_to_completion(daemon)

    assert calls.cancelled_handles  # stopped mid-run, not at the next iteration
    assert _events_of_kind(events, "autonomous_spend_limit")[0]["spent_usd"] == 12.5
    assert _events_of_kind(events, "autonomous_mission_paused")[0]["stop_reason"] == "spend_limit_reached"


def test_without_a_limit_spending_doesnt_stop_the_mission(tmp_path):
    path = _build_roadmap_on_disk(tmp_path, items=[(1, "T1.1", ""), (1, "T1.2", "")], criteria=[("bash", "x")])
    calls = _StubCallTracker()
    hooks = _make_hooks(calls)
    hooks.spent_usd = lambda: 1_000.0
    daemon, events = _daemon(path, hooks, max_iterations=2)
    _run_daemon_to_completion(daemon)
    assert len(calls.dispatched_items) == 2
    assert _events_of_kind(events, "autonomous_mission_paused")[0]["stop_reason"] == "iteration_cap"
    snapshot = daemon.state_snapshot()
    assert (snapshot["spend_limit_usd"], snapshot["spent_usd"]) == (None, 1000.0)


def test_the_tracker_counts_requests_between_iterations():
    tracker = AutonomousIterCostTracker()
    tracker.record_status("m1", "model-a", 100, 10, 0.25)  # before the first iteration (REFLECT)
    tracker.on_iteration_started("m1", 1)
    tracker.record_status("m1", "model-a", 100, 10, 0.5)
    snapshot = tracker.on_iteration_finalized("m1", 1)
    assert snapshot.cost_usd == 0.5  # the iteration's own
    assert tracker.mission_total("m1") == 0.75  # everything, for the limit
    assert tracker.mission_total("other") == 0.0
    tracker.reset_intent("m1")
    assert tracker.mission_total("m1") == 0.0


@dataclass
class _State(_StubAppState):
    iter_cost_tracker: AutonomousIterCostTracker = field(default_factory=AutonomousIterCostTracker)


def test_starting_and_resuming_keep_the_limit_and_the_spend(tmp_path):
    state = _State(project=_StubProject(str(tmp_path)))
    daemon = start_autonomous_mission(state=state, intent_id="auto-1", feature="counter",
                                      spec_markdown=_SPEC_MD, on_event=lambda event: None,
                                      spend_limit_label="25")
    try:
        assert daemon.config.spend_limit_usd == 25.0
        state.iter_cost_tracker.record_status("auto-1", "model-a", 10, 10, 1.5)
        assert daemon.hooks.spent_usd() == 1.5
    finally:
        daemon.stop()
        daemon.join(timeout=3.0)
    rm, path = build_roadmap_from_spec(feature="counter", intent_id="auto-2", spec_markdown=_SPEC_MD,
                                       project_path=str(tmp_path), spend_limit_label="$5")
    assert rm.spend_limit_label == "$5.00"

    # Resumed after a restart with more spent than the limit: it stops at once.
    rm.spent_label, rm.status = "$6.00", "paused"
    roadmap_module.save(rm, path)
    events: list[dict] = []
    resumed = resume_autonomous_mission(state=state, intent_id="auto-2", on_event=events.append)
    resumed.join(timeout=5.0)
    assert not resumed.is_running()
    paused = [event for event in events if event.get("event") == "autonomous_mission_paused"]
    assert paused and paused[0]["stop_reason"] == "spend_limit_reached"


def test_spec_criteria_in_the_prompts_form_or_the_roadmaps():
    from lumi.orchestration.grill_me import extract_acceptance_criteria

    spec = ("**Acceptance criteria:**\n- `[bash]` `npm test` exits 0\n- [ ] `[chrome]` Button works\n"
            "- [x] `[vision]` Centered\n- Not typed\n\n**Open risks:**\n- `[bash]` not a criterion\n")
    assert [(c.type, c.text) for c in extract_acceptance_criteria(spec)] == [
        ("bash", "`npm test` exits 0"), ("chrome", "Button works"), ("vision", "Centered")]
