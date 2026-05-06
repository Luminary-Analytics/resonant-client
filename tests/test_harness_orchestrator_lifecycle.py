"""Tests for v0.5.18a1 — HarnessOrchestrator public lifecycle.

Static helper coverage was added in v0.5.14a2 (the dataclass to_dict
methods + the classification helpers like _choose_next_role,
_repairable_generator_failure, etc.). The PUBLIC lifecycle methods
that drive the background cycle — start_cycle, list_runs, get_run,
cancel — were untested.

This alpha covers the lifecycle by passing stub callables so the
background cycle runs to completion deterministically without
needing a real backend or filesystem state. The deep _run_cycle
state-machine branches are deferred to v0.5.18a2+ where a richer
stub harness covers the planner→generator→evaluator transitions.

Coverage delta target on harness/orchestrator.py: 45% → ~70%.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from resonant_client.harness.orchestrator import (
    HarnessCycleRun,
    HarnessCycleStatus,
    HarnessOrchestrator,
)


# ── Builder for a "no-op" orchestrator ─────────────────────────────────


def _make_orchestrator(
    *,
    summary_getter: Callable[[str], dict[str, Any]] | None = None,
    role_runner: Callable[..., dict[str, Any]] | None = None,
    teacher_escalator: Callable[..., dict[str, Any]] | None = None,
    max_concurrent: int = 1,
    max_teacher_recoveries: int = 2,
) -> HarnessOrchestrator:
    """Construct an orchestrator with stub callables. Defaults make
    _run_cycle exit immediately (next_role=None because contract_status
    is "passed")."""
    default_summary = summary_getter or (
        lambda path: {
            # Status "passed" means _choose_next_role returns None →
            # cycle completes immediately.
            "contract_status": "passed",
            "active_sprint_id": "sp-1",
        }
    )
    default_runner = role_runner or (
        lambda **kw: {"result": "stub", "error": "", "steps": 1}
    )
    return HarnessOrchestrator(
        summary_getter=default_summary,
        prompt_builder=lambda role, path, objective: f"prompt-{role}",
        backend_selector=lambda role, path: ("ollama", "stub-model"),
        role_runner=default_runner,
        teacher_escalator=teacher_escalator,
        max_concurrent=max_concurrent,
        max_teacher_recoveries=max_teacher_recoveries,
    )


def _wait_for_status(run: HarnessCycleRun, target: HarnessCycleStatus, timeout=2.0):
    """Spin until the run reaches the target status (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run.status == target:
            return True
        time.sleep(0.01)
    return False


# ── start_cycle ─────────────────────────────────────────────────────────


class TestStartCycle:
    def test_returns_run_with_expected_shape(self, tmp_path):
        # The default summary_getter says contract_status="passed" so
        # the cycle completes immediately. start_cycle returns the
        # HarnessCycleRun BEFORE the background thread completes.
        orch = _make_orchestrator()
        run = orch.start_cycle(
            project_path=str(tmp_path), name="t1", objective="o", max_loops=3,
        )
        assert run.name == "t1"
        assert run.project_path == str(tmp_path)
        assert run.objective == "o"
        assert run.max_loops == 3
        # ID is 12 hex chars.
        assert len(run.id) == 12
        # Initially PENDING (run.status flips to RUNNING in _run_cycle).
        assert run.status in {HarnessCycleStatus.PENDING, HarnessCycleStatus.RUNNING, HarnessCycleStatus.COMPLETED}

    def test_max_loops_enforced_min_1(self, tmp_path):
        # max_loops=0 gets clamped to 1.
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path), max_loops=0)
        assert run.max_loops == 1

    def test_default_name_is_harness_cycle(self, tmp_path):
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path))
        assert run.name == "Harness Cycle"

    def test_objective_stripped_of_whitespace(self, tmp_path):
        orch = _make_orchestrator()
        run = orch.start_cycle(
            project_path=str(tmp_path), objective="  some objective  ",
        )
        assert run.objective == "some objective"

    def test_duplicate_project_rejected(self, tmp_path):
        # If a cycle is already PENDING/RUNNING for this project,
        # start_cycle raises ValueError.
        # Use a never-completing role_runner so the first cycle stays
        # in RUNNING state.
        slow_runner = lambda **kw: time.sleep(0.5) or {"result": "x", "error": ""}
        orch = HarnessOrchestrator(
            summary_getter=lambda path: {
                "contract_status": "approved",  # → next_role="generator"
                "active_sprint_id": "sp-1",
            },
            prompt_builder=lambda role, path, obj: "p",
            backend_selector=lambda role, path: ("ollama", "m"),
            role_runner=slow_runner,
            max_concurrent=1,
        )
        run1 = orch.start_cycle(project_path=str(tmp_path))
        # Try to start a second cycle for the same path — must raise.
        with pytest.raises(ValueError, match="already running"):
            orch.start_cycle(project_path=str(tmp_path))
        # Cancel cleanly.
        orch.cancel(run1.id)

    def test_cycle_completes_when_summary_passed(self, tmp_path):
        # The default summary returns "passed" → _choose_next_role
        # returns None → cycle marks itself COMPLETED.
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path))
        assert _wait_for_status(run, HarnessCycleStatus.COMPLETED), (
            f"cycle did not complete; status={run.status}"
        )
        assert "Sprint already passed" in run.message


# ── list_runs ─────────────────────────────────────────────────────────


class TestListRuns:
    def test_empty_returns_empty_list(self):
        orch = _make_orchestrator()
        assert orch.list_runs() == []

    def test_returns_started_runs_in_reverse_chronological(self, tmp_path):
        orch = _make_orchestrator()
        # Start cycles for three different paths so duplicate-rejection
        # doesn't fire. They each complete immediately.
        path1 = tmp_path / "a"
        path2 = tmp_path / "b"
        path3 = tmp_path / "c"
        path1.mkdir(); path2.mkdir(); path3.mkdir()
        run1 = orch.start_cycle(project_path=str(path1), name="first")
        time.sleep(0.05)
        run2 = orch.start_cycle(project_path=str(path2), name="second")
        time.sleep(0.05)
        run3 = orch.start_cycle(project_path=str(path3), name="third")
        # Wait for all to complete.
        for r in (run1, run2, run3):
            _wait_for_status(r, HarnessCycleStatus.COMPLETED)

        listed = orch.list_runs()
        assert len(listed) == 3
        # Most recent first.
        assert listed[0]["name"] == "third"
        assert listed[1]["name"] == "second"
        assert listed[2]["name"] == "first"

    def test_limit_caps_returned_count(self, tmp_path):
        orch = _make_orchestrator()
        # Start 5 cycles, limit to 2.
        for i in range(5):
            sub = tmp_path / f"p{i}"
            sub.mkdir()
            run = orch.start_cycle(project_path=str(sub))
            _wait_for_status(run, HarnessCycleStatus.COMPLETED)
        listed = orch.list_runs(limit=2)
        assert len(listed) == 2

    def test_returns_dicts_via_to_dict(self, tmp_path):
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path))
        _wait_for_status(run, HarnessCycleStatus.COMPLETED)
        listed = orch.list_runs()
        assert isinstance(listed[0], dict)
        # The standard HarnessCycleRun.to_dict shape.
        assert "id" in listed[0]
        assert "name" in listed[0]
        assert "status" in listed[0]


# ── get_run ───────────────────────────────────────────────────────────


class TestGetRun:
    def test_unknown_id_returns_none(self):
        orch = _make_orchestrator()
        assert orch.get_run("nope") is None

    def test_known_id_returns_run(self, tmp_path):
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path))
        assert orch.get_run(run.id) is run


# ── cancel ────────────────────────────────────────────────────────────


class TestCancel:
    def test_unknown_id_returns_false(self):
        orch = _make_orchestrator()
        assert orch.cancel("nope") is False

    def test_already_completed_run_cannot_be_cancelled(self, tmp_path):
        orch = _make_orchestrator()
        run = orch.start_cycle(project_path=str(tmp_path))
        _wait_for_status(run, HarnessCycleStatus.COMPLETED)
        # cancel returns False because status is no longer PENDING/RUNNING.
        assert orch.cancel(run.id) is False

    def test_running_cycle_cancel_sets_event_and_returns_true(self, tmp_path):
        # Use a slow-running role_runner so the cycle stays in RUNNING.
        slow_runner = lambda **kw: time.sleep(0.5) or {"result": "x", "error": ""}
        orch = HarnessOrchestrator(
            summary_getter=lambda path: {
                "contract_status": "approved",  # → keeps the cycle going
                "active_sprint_id": "sp-1",
            },
            prompt_builder=lambda role, path, obj: "p",
            backend_selector=lambda role, path: ("ollama", "m"),
            role_runner=slow_runner,
        )
        run = orch.start_cycle(project_path=str(tmp_path))
        # Give the cycle a moment to enter RUNNING.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if run.status == HarnessCycleStatus.RUNNING:
                break
            time.sleep(0.02)

        # Now cancel.
        assert orch.cancel(run.id) is True
        assert run.cancel_event.is_set()


# ── End-to-end: a single-loop cycle that runs one role and exits ───────


class TestSingleRoleCycle:
    def test_cycle_runs_role_and_completes(self, tmp_path):
        # A summary_getter that returns "approved" on first call (so
        # _choose_next_role picks "generator") and "passed" thereafter
        # (so the cycle exits). role_runner just records its calls.
        call_count = {"summary": 0, "runner": 0}

        def summary_getter(path):
            call_count["summary"] += 1
            if call_count["summary"] <= 1:
                return {
                    "contract_status": "approved",
                    "active_sprint_id": "sp-1",
                }
            return {
                "contract_status": "passed",
                "active_sprint_id": "sp-1",
            }

        runner_calls = []

        def role_runner(**kw):
            runner_calls.append(kw)
            return {"result": "ran ok", "error": "", "steps": 5}

        orch = HarnessOrchestrator(
            summary_getter=summary_getter,
            prompt_builder=lambda role, path, obj: f"prompt-for-{role}",
            backend_selector=lambda role, path: ("ollama", "deepseek-flash"),
            role_runner=role_runner,
        )
        run = orch.start_cycle(project_path=str(tmp_path), name="t")
        assert _wait_for_status(run, HarnessCycleStatus.COMPLETED, timeout=3.0)

        # Role runner called once (one iteration).
        assert len(runner_calls) == 1
        assert runner_calls[0]["session_role"] == "generator"
        assert runner_calls[0]["backend_type"] == "ollama"
        assert runner_calls[0]["model"] == "deepseek-flash"
        # Step recorded.
        assert len(run.steps) == 1
        assert run.steps[0].role == "generator"
        assert run.steps[0].status == "completed"
        assert run.steps[0].steps == 5

    def test_role_runner_exception_marks_step_failed(self, tmp_path):
        def boom(**kw):
            raise RuntimeError("simulated runner crash")

        # First call: approved → generator dispatched. After failure,
        # _attempt_role_retry will try (we stub no retry backend so
        # it skips). Loop increments. Second call: passed → exit.
        summary_calls = {"n": 0}

        def summary_getter(path):
            summary_calls["n"] += 1
            if summary_calls["n"] <= 1:
                return {
                    "contract_status": "approved",
                    "active_sprint_id": "sp-1",
                }
            return {"contract_status": "passed", "active_sprint_id": "sp-1"}

        orch = HarnessOrchestrator(
            summary_getter=summary_getter,
            prompt_builder=lambda role, path, obj: "p",
            backend_selector=lambda role, path: ("ollama", "m"),
            role_runner=boom,
            max_teacher_recoveries=0,  # No teacher escalation.
        )
        run = orch.start_cycle(project_path=str(tmp_path))
        # When role_runner raises and no recovery succeeds, the cycle
        # ends with status=FAILED (not COMPLETED). The step records
        # the failure regardless.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if run.status in (
                HarnessCycleStatus.FAILED,
                HarnessCycleStatus.COMPLETED,
                HarnessCycleStatus.CANCELLED,
            ):
                break
            time.sleep(0.02)
        assert run.status == HarnessCycleStatus.FAILED

        # The step recorded the failure.
        assert len(run.steps) >= 1
        assert run.steps[0].status == "failed"
        assert "simulated runner crash" in run.steps[0].error

    def test_cancel_event_set_stops_loop(self, tmp_path):
        # Use a summary_getter that always returns "approved" so the
        # cycle would loop forever — but we cancel after the first
        # iteration via the role_runner.

        def cancelling_runner(*, cancel_event, **kw):
            cancel_event.set()
            return {"result": "ran but cancelling", "error": ""}

        orch = HarnessOrchestrator(
            summary_getter=lambda path: {
                "contract_status": "approved",
                "active_sprint_id": "sp-1",
            },
            prompt_builder=lambda role, path, obj: "p",
            backend_selector=lambda role, path: ("ollama", "m"),
            role_runner=cancelling_runner,
        )
        run = orch.start_cycle(project_path=str(tmp_path))
        # Wait for some terminal status.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if run.status in (
                HarnessCycleStatus.COMPLETED,
                HarnessCycleStatus.CANCELLED,
                HarnessCycleStatus.FAILED,
            ):
                break
            time.sleep(0.02)
        # Loop only ran once because cancel was set inside the runner.
        assert len(run.steps) == 1
