"""Tests for IntentService — drives intents through GraphWalker on a worker thread."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from resonant_client.orchestration import (
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
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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


# ── Pause / resume ─────────────────────────────────────────────────────


def test_pause_then_resume(state_home, project_dir):
    service = _make_service(project_dir)
    runner_results = {
        NodeSpecialization.PLAN: SpecialistResult(status=NodeStatus.DONE, confidence=0.9),
    }
    with patch(
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("test")
        # Pause + resume don't have to land at any particular moment for the test,
        # we just want to exercise the API. The intent likely completes before
        # the pause arrives — that's fine, the methods are still valid.
        service.pause(intent_id)
        service.resume(intent_id)
        _wait_for_completion(service, intent_id)


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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
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
        "resonant_client.orchestration.intent_service.LocalSpecialistRunner",
        side_effect=lambda **kw: _scripted_runner(runner_results),
    ):
        intent_id = service.start_intent("x")
    _wait_for_completion(service, intent_id)

    g = service.get_graph(intent_id)
    assert g is not None
    assert g.intent_id == intent_id
