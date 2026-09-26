"""Tests for IntentService — drives intents through GraphWalker on a worker thread."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from lumi.orchestration import (
    IntentService,
    NodeSpecialization,
    NodeStatus,
    SpecialistResult,
    load_graph,
    read_audit_events,
    list_skills,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LUMI_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _make_service(project_dir, on_event=None) -> IntentService:
    """Service with a stub backend + minimal tools."""
    return IntentService(
        project_path=str(project_dir),
        backend=MagicMock(),
        all_tools=[{"function": {"name": "file_read"}}, {"function": {"name": "bash"}}],
        project_instructions="",
        settings=None,
        on_event=on_event or (lambda ev: None),
    )


def _scripted_runner(by_specialization: dict[str, SpecialistResult]):
    """Patchable runner that returns scripted results based on node specialization."""
    def runner(node, graph):
        return by_specialization.get(node.specialization, SpecialistResult(
            status=NodeStatus.DONE, confidence=1.0, summary="ok",
        ))
    return runner


def _wait_for_completion(service: IntentService, intent_id: str, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        active = service._get(intent_id)
        if not active or not active.thread.is_alive():
            return
        time.sleep(0.02)
    raise AssertionError(f"intent {intent_id} did not complete within {timeout}s")


# ── start_intent flow ──────────────────────────────────────────────────


def test_start_intent_returns_immediately_with_id(state_home, project_dir):
    events: list = []
    service = _make_service(project_dir, on_event=events.append)

    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.9,
            subgoals=[{"goal": "do thing", "specialization": "implement"}],
        ),
        NodeSpecialization.IMPLEMENT: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.95, summary="done",
        ),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("ship a small feature")

    assert isinstance(intent_id, str) and len(intent_id) > 0
    _wait_for_completion(service, intent_id)

    # Initial snapshot + intent.started fired before any node ran
    kinds = [e.get("event") for e in events]
    assert "plan.snapshot" in kinds
    assert "intent.started" in kinds


def test_start_intent_persists_graph_to_disk(state_home, project_dir):
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("test intent")
    _wait_for_completion(service, intent_id)

    loaded = load_graph(intent_id, str(project_dir))
    assert loaded is not None
    assert loaded.intent == "test intent"


def test_start_intent_rejects_blank_text(state_home, project_dir):
    service = _make_service(project_dir)
    with pytest.raises(ValueError):
        service.start_intent("")
    with pytest.raises(ValueError):
        service.start_intent("   ")


# ── Walker event forwarding ────────────────────────────────────────────


def test_walker_events_forwarded_through_on_event(state_home, project_dir):
    events: list = []
    service = _make_service(project_dir, on_event=events.append)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.9,
            subgoals=[{"goal": "x", "specialization": "implement"}],
        ),
        NodeSpecialization.IMPLEMENT: SpecialistResult(status=NodeStatus.DONE, confidence=0.95),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("test")
    _wait_for_completion(service, intent_id)

    plan_events = [e for e in events if e.get("event") == "plan.event"]
    assert plan_events, "expected plan.event forwards"
    kinds = [e["event_payload"]["kind"] for e in plan_events]
    assert "node.start" in kinds
    assert "node.done" in kinds
    assert "plan.complete" in kinds


def test_specialist_session_events_are_tagged_and_fall_within_their_node(state_home, project_dir):
    """The GUI keeps a specialist's session out of the conversation's turn by
    its `_source` tag, and draws each event under the node whose node.start
    came before it (app.js, "Plan activity")."""
    events: list = []
    originals: list[dict] = []
    service = _make_service(project_dir, on_event=events.append)

    def runner_factory(**kwargs):
        forward = kwargs["on_session_event"]

        def runner(node, graph):
            for event in (
                {"event": "session.start", "model": "stub"},
                {"event": "tool.call", "name": "file_read", "call_id": "call_1", "arguments": {"path": "a.txt"}},
                {"event": "session.end", "outcome": "incomplete"},
            ):
                originals.append(event)
                forward(event)
            if node.specialization == NodeSpecialization.PLAN:
                return SpecialistResult(
                    status=NodeStatus.DONE, confidence=0.9,
                    subgoals=[{"goal": "edit a.txt", "specialization": "implement"}],
                )
            return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="done")

        return runner

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=runner_factory,
    ):
        intent_id = service.start_intent("ship a small feature")
    _wait_for_completion(service, intent_id)

    session_kinds = {"session.start", "tool.call", "session.end"}
    session = [e for e in events if e.get("event") in session_kinds]
    assert len(session) == 6, "two specialists, three events each"
    assert all(e["_source"] == "intent" and e["intent_id"] == intent_id for e in session)
    assert session[1]["arguments"] == {"path": "a.txt"}
    assert all("_source" not in e and "intent_id" not in e for e in originals), "the session's own events stay as they were"

    # The walker thread sends a node's start, its session, then its done.
    running = None
    seen: dict[str, int] = {}
    for event in events:
        if event.get("event") == "plan.event":
            payload = event["event_payload"]
            if payload["kind"] == "node.start":
                assert running is None
                running = payload["node_id"]
            elif payload["kind"] == "node.done":
                assert payload["node_id"] == running
                running = None
        elif event.get("event") in session_kinds:
            assert running is not None, f"{event['event']} outside a node"
            seen[running] = seen.get(running, 0) + 1
    assert sorted(seen.values()) == [3, 3]


def test_listeners_keep_the_events_when_on_event_is_rebound(state_home, project_dir):
    # The app rebinds on_event to each intent command's connection. A
    # listener, such as an autonomous mission's tracker, keeps every event.
    first_page: list = []
    second_page: list = []
    listened: list = []
    service = _make_service(project_dir, on_event=first_page.append)
    service.add_listener(listened.append)
    release = threading.Event()

    def runner(node, graph):
        release.wait(timeout=5)
        return SpecialistResult(status=NodeStatus.DONE, confidence=1.0, summary="ok")

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: runner,
    ):
        intent_id = service.start_intent("ship a small feature")
        service.on_event = second_page.append
        release.set()
        _wait_for_completion(service, intent_id)

    kinds = [e["event"] for e in listened]
    assert {"plan.snapshot", "intent.started", "plan.event"} <= set(kinds)
    assert kinds[-1] == "intent.complete"
    first_kinds = [e["event"] for e in first_page]
    assert {"plan.snapshot", "intent.started"} <= set(first_kinds)
    assert "intent.complete" not in first_kinds
    assert [e["event"] for e in second_page][-1] == "intent.complete"


def test_a_failing_or_removed_listener_does_not_cost_the_others(state_home, project_dir):
    page: list = []
    kept: list = []
    removed: list = []
    service = _make_service(project_dir, on_event=page.append)

    def broken(_event):
        raise RuntimeError("listener bug")

    service.add_listener(broken)
    service.add_listener(kept.append)
    service.add_listener(kept.append)  # already listening: no second copy
    service.add_listener(removed.append)
    service.remove_listener(removed.append)

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner({}),
    ):
        intent_id = service.start_intent("ship a small feature")
        _wait_for_completion(service, intent_id)

    # The same events, one copy each; two threads emit, so the order of
    # the two lists can differ.
    assert sorted(map(id, kept)) == sorted(map(id, page))
    assert kept[-1]["event"] == "intent.complete"
    assert removed == []


# ── Cancellation ───────────────────────────────────────────────────────


def test_cancel_terminates_walker_promptly(state_home, project_dir):
    """A cancelled intent should bail before running more nodes."""
    barrier = threading.Event()
    service = _make_service(project_dir)

    def slow_runner(node, graph):
        # Block until released so we can race a cancel against the running node
        barrier.wait(timeout=2.0)
        return SpecialistResult(status=NodeStatus.DONE, confidence=0.9)

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: slow_runner,
    ):
        intent_id = service.start_intent("test")
        # Give the worker a moment to start
        time.sleep(0.05)
        ok = service.cancel(intent_id)
        assert ok is True
        barrier.set()
        _wait_for_completion(service, intent_id, timeout=3.0)

    active = service._get(intent_id)
    assert active.status == "cancelled"


def test_cancel_unknown_intent_returns_false(state_home, project_dir):
    service = _make_service(project_dir)
    assert service.cancel("does-not-exist") is False


def _stoppable_runner(started: list, implementing: threading.Event, cancel_event: threading.Event):
    """A planner that plans two steps, then an implementer that works until
    the plan is stopped, as a specialist's Session sharing the event does."""
    def runner(node, graph):
        started.append(node.goal)
        if node.specialization == NodeSpecialization.PLAN:
            return SpecialistResult(status=NodeStatus.DONE, confidence=0.9, subgoals=[
                {"goal": "write the toggle", "specialization": "implement"},
                {"goal": "wire it up", "specialization": "implement", "depends_on": [0]},
            ])
        implementing.set()
        if cancel_event.wait(timeout=5):
            return SpecialistResult(status=NodeStatus.ABANDONED, confidence=0.0,
                                    summary="Stopped before this step finished.")
        return SpecialistResult(status=NodeStatus.DONE, confidence=1.0)
    return runner


def test_stopping_a_plan_ends_its_step_starts_no_other_and_says_so_once(state_home, project_dir):
    events: list = []
    service = _make_service(project_dir, on_event=events.append)
    started: list = []
    implementing = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _stoppable_runner(started, implementing, kw["cancel_event"]),
    ):
        intent_id = service.start_intent("add a dark mode toggle")
        assert implementing.wait(timeout=5)
        assert service.cancel(intent_id) is True
        _wait_for_completion(service, intent_id)

    assert started == ["add a dark mode toggle", "write the toggle"]
    kinds = [e.get("event") for e in events]
    # Accepted at once; reported stopped once, after the walk ended.
    assert kinds.count("intent.cancelling") == 1
    assert kinds.count("intent.cancelled") == 1
    assert "intent.complete" not in kinds
    walk = [(i, e["event_payload"]["kind"]) for i, e in enumerate(events) if e.get("event") == "plan.event"]
    stopped_at = next(i for i, kind in walk if kind == "plan.stopped")
    assert kinds.index("intent.cancelling") < stopped_at < kinds.index("intent.cancelled")
    assert "plan.complete" not in [kind for _, kind in walk]
    # The saved graph shows the stopped step and the one that never started.
    statuses = {n.goal: n.status for n in load_graph(intent_id, str(project_dir)).nodes.values()}
    assert statuses == {
        "add a dark mode toggle": NodeStatus.DONE,
        "write the toggle": NodeStatus.ABANDONED,
        "wire it up": NodeStatus.ABANDONED,
    }
    summaries = [e["payload"].get("summary") for e in read_audit_events(str(project_dir), intent_id)
                 if e["kind"] == "decision"]
    assert "intent cancel requested" in summaries
    assert "plan stopped" in summaries
    assert service.cancel(intent_id) is False


def test_a_plan_cannot_be_stopped_once_its_end_is_announced(state_home, project_dir):
    # The worker thread is still alive while it sends its final event. A stop
    # accepted then would say "stopping" with no intent.cancelled to follow.
    events: list = []
    answers: list = []

    def on_event(event):
        events.append(event)
        if event.get("event") == "intent.complete":
            answers.append(service.cancel(event["intent_id"]))

    service = _make_service(project_dir, on_event=on_event)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("test")
        _wait_for_completion(service, intent_id)

    assert answers == [False]
    assert "intent.cancelling" not in [e.get("event") for e in events]
    assert service._get(intent_id).status == "completed"


def test_a_stopped_plan_is_not_saved_as_a_skill(state_home, project_dir):
    """Even when every step that ran had finished (Stop at the very end)."""
    events: list = []
    service = _make_service(project_dir, on_event=events.append)
    # In sequence, so step c is the last to run.
    steps = [{"goal": f"step {name}", "specialization": "implement", "depends_on": deps}
             for name, deps in (("a", []), ("b", [0]), ("c", [1]))]

    def make_runner(cancel_event):
        def runner(node, graph):
            if node.specialization == NodeSpecialization.PLAN:
                return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, subgoals=steps)
            if node.goal == "step c":
                cancel_event.set()
            return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="implemented")
        return runner

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: make_runner(kw["cancel_event"]),
    ):
        intent_id = service.start_intent("a successful three-step task")
    _wait_for_completion(service, intent_id)

    cancelled = [e for e in events if e.get("event") == "intent.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["extracted_skill_id"] is None
    assert not any("successful" in s.id for s in list_skills())


def test_a_rebuilt_service_still_reaches_a_running_plan(state_home, project_dir):
    """The app rebuilds its service on a model switch; Stop must still work."""
    first_events: list = []
    first = _make_service(project_dir, on_event=first_events.append)
    started: list = []
    implementing = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _stoppable_runner(started, implementing, kw["cancel_event"]),
    ):
        intent_id = first.start_intent("add a dark mode toggle")
        assert implementing.wait(timeout=5)
        second_events: list = []
        second = _make_service(project_dir, on_event=second_events.append)
        second.adopt_running(first)
        assert second.pause(intent_id) is True
        assert second.cancel(intent_id) is True
        _wait_for_completion(first, intent_id)

    assert started == ["add a dark mode toggle", "write the toggle"]
    assert [e["event"] for e in second_events] == ["intent.paused", "intent.cancelling"]
    # The walk's own service reports its end.
    assert [e["event"] for e in first_events].count("intent.cancelled") == 1
    # Only running intents carry over.
    third = _make_service(project_dir)
    third.adopt_running(second)
    assert third._get(intent_id) is None
    assert third.cancel(intent_id) is False


# ── Viewers: the page a plan's events go to ────────────────────────────


def _held_runner(started: list, release: threading.Event):
    """A planner that plans one step, and an implementer that works until
    released, so the plan is still running while the test looks at it."""
    def runner(node, graph):
        started.append((graph.intent, node.specialization))
        if node.specialization == NodeSpecialization.PLAN:
            return SpecialistResult(status=NodeStatus.DONE, confidence=0.9, subgoals=[
                {"goal": "write the toggle", "specialization": "implement"},
            ])
        release.wait(timeout=5)
        return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="written")
    return runner


def _wait_until(condition, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not condition():
        assert time.time() < deadline, "timed out waiting"
        time.sleep(0.02)


def _implementing(started: list, count: int):
    return lambda: sum(spec == NodeSpecialization.IMPLEMENT for _, spec in started) >= count


def test_a_page_that_connects_takes_over_the_running_plans(state_home, project_dir):
    """After a reload, the page that started a plan is gone. The page that
    connects gets the plan's state and graph, and its events from then on;
    an autonomous session's plan keeps sending its own to on_event."""
    service_events: list = []
    service = _make_service(project_dir, on_event=service_events.append)
    first_page: list = []
    started: list = []
    release = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _held_runner(started, release),
    ):
        plan = service.start_intent("add a dark mode toggle", viewer=first_page.append)
        iteration = service.start_intent("iteration 1 of a mission")
        _wait_until(_implementing(started, 2))
        assert service.pause(plan) is True

        second_page: list = []
        plans = service.attach_viewer(second_page.append, project_path=str(project_dir))

        assert [described["intent_id"] for described in plans] == [plan]
        described = plans[0]
        assert described["text"] == "add a dark mode toggle"
        assert described["paused"] is True
        assert described["stopping"] is False
        assert described["snapshot"]["intent_id"] == plan
        assert {n["goal"]: n["status"] for n in described["snapshot"]["nodes"]} == {
            "add a dark mode toggle": NodeStatus.DONE,
            "write the toggle": NodeStatus.RUNNING,
        }
        seen_by_first_page = len(first_page)
        assert service.resume(plan) is True
        release.set()
        _wait_for_completion(service, plan)
        _wait_for_completion(service, iteration)

    assert "intent.paused" in [e["event"] for e in first_page]
    # From the switch on, the plan's events went to the new page alone.
    assert len(first_page) == seen_by_first_page
    kinds = [e["event"] for e in second_page]
    assert kinds[0] == "intent.resumed"
    assert kinds[-1] == "intent.complete"
    assert "plan.event" in kinds
    assert {e["intent_id"] for e in second_page} == {plan}
    # The plan never reached on_event; the autonomous one only ever did.
    assert {e["intent_id"] for e in service_events} == {iteration}
    assert [e["event"] for e in service_events][-1] == "intent.complete"
    # A plan that has ended isn't handed to the next page.
    assert service.attach_viewer(lambda event: None) == []


def test_a_page_is_handed_its_projects_running_plans_oldest_first(state_home, project_dir, tmp_path):
    """A rebuilt service adopts every running plan, even after a project
    switch. A page gets those of the project it shows, including the end of
    an adopted plan, which the service that started it still sends."""
    other_project = tmp_path / "other"
    other_project.mkdir()
    started: list = []
    release = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _held_runner(started, release),
    ):
        before_switch = _make_service(other_project)
        elsewhere = before_switch.start_intent("tidy the README", viewer=lambda event: None)
        _wait_until(_implementing(started, 1))
        service = _make_service(project_dir)
        service.adopt_running(before_switch)
        first = service.start_intent("add a toggle", viewer=lambda event: None)
        second = service.start_intent("add a menu", viewer=lambda event: None)
        _wait_until(_implementing(started, 3))

        here: list = []
        assert [p["intent_id"] for p in service.attach_viewer(here.append, project_path=str(project_dir))] \
            == [first, second]
        there: list = []
        assert [p["intent_id"] for p in service.attach_viewer(there.append, project_path=str(other_project))] \
            == [elsewhere]
        release.set()
        for intent_id in (elsewhere, first, second):
            _wait_for_completion(service, intent_id)

    assert [e["event"] for e in there if e["intent_id"] == elsewhere][-1] == "intent.complete"
    assert {e["intent_id"] for e in there} == {elsewhere}
    assert {e["intent_id"] for e in here} == {first, second}


def test_adopted_plan_keeps_its_project_audit_and_snapshots(state_home, project_dir, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    original = _make_service(project_dir)
    replacement = _make_service(other)
    started = []
    release = threading.Event()
    with patch("lumi.orchestration.intent_service.LocalSpecialistRunner",
               side_effect=lambda **kw: _held_runner(started, release)):
        intent_id = original.start_intent("keep the original project")
        try:
            _wait_until(_implementing(started, 1))
            replacement.adopt_running(original)
            assert replacement.pause(intent_id)
            assert replacement.resume(intent_id)
            snapshots = original.list_snapshots(intent_id)
            assert snapshots
            assert replacement.list_snapshots(intent_id) == snapshots
            assert replacement.cancel(intent_id)
        finally:
            release.set()
            _wait_for_completion(original, intent_id)

    assert replacement.restore_snapshot(intent_id, snapshots[-1]["ts_ms"])
    decisions = {event["payload"].get("summary") for event in read_audit_events(str(project_dir), intent_id)
                 if event["kind"] == "decision"}
    assert {"intent paused", "intent resumed", "intent cancel requested", "snapshot restored"} <= decisions
    assert read_audit_events(str(other), intent_id) == []
    assert load_graph(intent_id, str(other)) is None
    assert replacement.get_graph(intent_id).to_dict() == load_graph(intent_id, str(project_dir)).to_dict()


def test_a_page_acting_on_a_plan_gets_its_events_but_never_an_autonomous_plans(state_home, project_dir):
    service_events: list = []
    service = _make_service(project_dir, on_event=service_events.append)
    connected_page: list = []
    started: list = []
    release = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _held_runner(started, release),
    ):
        plan = service.start_intent("add a toggle", viewer=lambda event: None)
        iteration = service.start_intent("iteration 1 of a mission")
        _wait_until(_implementing(started, 2))
        service.attach_viewer(connected_page.append)

        # The page that started the plan presses Stop after another page
        # connected: the result is reported to the page that pressed it.
        acting_page: list = []
        assert service.route_to(plan, acting_page.append) is True
        assert service.route_to(iteration, acting_page.append) is False
        assert service.route_to("no-such-plan", acting_page.append) is False
        assert service.cancel(plan) is True
        assert service.cancel(iteration) is True
        release.set()
        _wait_for_completion(service, plan)
        _wait_for_completion(service, iteration)

    assert connected_page == []
    assert [e["event"] for e in acting_page if e["event"].startswith("intent.")] == [
        "intent.cancelling", "intent.cancelled",
    ]
    assert {e["intent_id"] for e in acting_page} == {plan}
    assert [e["event"] for e in service_events if e["event"].startswith("intent.")][-2:] == [
        "intent.cancelling", "intent.cancelled",
    ]
    assert {e["intent_id"] for e in service_events} == {iteration}


def test_a_page_reloaded_while_its_plan_stops_shows_stopping_then_gets_the_end(state_home, project_dir):
    service = _make_service(project_dir)
    started: list = []
    release = threading.Event()

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _held_runner(started, release),
    ):
        plan = service.start_intent("add a toggle", viewer=lambda event: None)
        _wait_until(_implementing(started, 1))
        assert service.cancel(plan) is True
        page: list = []
        [described] = service.attach_viewer(page.append, project_path=str(project_dir))
        assert described["stopping"] is True
        assert described["paused"] is False
        release.set()
        _wait_for_completion(service, plan)

    assert [e["event"] for e in page if e["event"].startswith("intent.")] == ["intent.cancelled"]


def test_a_graph_changing_while_it_is_read_is_read_again():
    from lumi.orchestration import intent_service

    reads: list = []

    class Graph:
        def to_dict(self):
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("dictionary changed size during iteration")
            return {"nodes": []}

    assert intent_service._graph_dict(Graph()) == {"nodes": []}
    assert len(reads) == 2


# ── Pause / resume ─────────────────────────────────────────────────────


def test_pause_holds_the_next_node_until_resume(state_home, project_dir):
    events: list = []
    service = _make_service(project_dir, on_event=events.append)
    started: list = []
    planning, release_planner = threading.Event(), threading.Event()

    def runner(node, graph):
        started.append(node.specialization)
        if node.specialization == NodeSpecialization.PLAN:
            planning.set()
            release_planner.wait(timeout=5)
            return SpecialistResult(
                status=NodeStatus.DONE, confidence=0.9,
                subgoals=[{"goal": "do thing", "specialization": "implement"}],
            )
        return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="done")

    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: runner,
    ):
        intent_id = service.start_intent("test")
        assert planning.wait(timeout=5)
        assert service.pause(intent_id) is True
        release_planner.set()
        # The running planner finishes; the implementer it planned must wait.
        deadline = time.time() + 5
        while not any(e.get("event") == "plan.event" and e["event_payload"]["kind"] == "node.done" for e in events):
            assert time.time() < deadline, "the planner never finished"
            time.sleep(0.02)
        time.sleep(0.4)
        assert started == [NodeSpecialization.PLAN]
        assert service._get(intent_id).status == "paused"

        assert service.resume(intent_id) is True
        _wait_for_completion(service, intent_id)

    assert NodeSpecialization.IMPLEMENT in started
    kinds = [e.get("event") for e in events]
    assert kinds.index("intent.paused") < kinds.index("intent.resumed") < kinds.index("intent.complete")


def test_a_finished_intent_cannot_be_paused_resumed_or_cancelled(state_home, project_dir):
    """Otherwise the plan-graph would announce "Intent paused." for work that is over."""
    events: list = []
    service = _make_service(project_dir, on_event=events.append)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("test")
        _wait_for_completion(service, intent_id)
    events.clear()

    assert service.pause(intent_id) is False
    assert service.resume(intent_id) is False
    assert service.cancel(intent_id) is False
    assert events == []
    assert service._get(intent_id).status == "completed"


# ── Audit log integration ──────────────────────────────────────────────


def test_audit_log_captures_intent_lifecycle(state_home, project_dir):
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.9,
            subgoals=[{"goal": "do x", "specialization": "implement"}],
        ),
        NodeSpecialization.IMPLEMENT: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.95,
        ),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("audit me")
    _wait_for_completion(service, intent_id)

    events = read_audit_events(str(project_dir), intent_id)
    kinds = {e["kind"] for e in events}
    assert "decision" in kinds
    assert "plan_change" in kinds
    summaries = {e["payload"].get("summary") for e in events if e["kind"] == "decision"}
    assert "intent started" in summaries
    assert any("plan complete" in (s or "") for s in summaries)


# ── Skill auto-extraction ──────────────────────────────────────────────


def test_skill_auto_extracted_on_successful_completion(state_home, project_dir):
    """A 4-node graph that all completes DONE with high confidence yields a skill."""
    events: list = []
    service = _make_service(project_dir, on_event=events.append)

    # Plan returns 3 subgoals → 4 total nodes (root plan + 3 children).
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.95,
            subgoals=[
                {"goal": "step a", "specialization": "implement"},
                {"goal": "step b", "specialization": "implement"},
                {"goal": "step c", "specialization": "implement"},
            ],
        ),
        NodeSpecialization.IMPLEMENT: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.95, summary="implemented",
        ),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("a successful three-step task")
    _wait_for_completion(service, intent_id)

    # intent.complete carries the extracted skill id
    complete_events = [e for e in events if e.get("event") == "intent.complete"]
    assert complete_events
    assert complete_events[0].get("extracted_skill_id"), "expected a skill auto-extracted"

    skills = list_skills()
    assert any("successful" in s.id for s in skills)


def test_no_skill_extracted_on_failed_completion(state_home, project_dir):
    """A graph with abandoned/blocked nodes should not generate a skill."""
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.BLOCKED, confidence=0.0, summary="couldn't plan",
        ),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("a failure")
    _wait_for_completion(service, intent_id)

    skills = list_skills()
    assert not any("failure" in s.id for s in skills)


# ── Snapshot restore ───────────────────────────────────────────────────


def test_list_snapshots_returns_history(state_home, project_dir):
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(
            status=NodeStatus.DONE, confidence=0.9,
            subgoals=[{"goal": "x", "specialization": "implement"}],
        ),
        NodeSpecialization.IMPLEMENT: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("x")
    _wait_for_completion(service, intent_id)

    snaps = service.list_snapshots(intent_id)
    # The walker emits plan.rewrite when subgoals expand → that snapshots first;
    # the worker also snapshots the final state after completion.
    assert len(snaps) >= 1


# ── get_graph ──────────────────────────────────────────────────────────


def test_get_graph_returns_active_then_falls_back_to_disk(state_home, project_dir):
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "lumi.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("x")
    _wait_for_completion(service, intent_id)

    g = service.get_graph(intent_id)
    assert g is not None
    assert g.intent_id == intent_id
