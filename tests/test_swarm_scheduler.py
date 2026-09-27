"""A ready graph advances as slots free, while dependencies require acceptance."""

import threading

import pytest

from lumi.engine.swarming import Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.scheduler import SwarmScheduler
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import done, text_delta
from tests.test_swarm_workers import Backend, GatedBackend, command, until


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "state.sqlite"))
    authority = supervisor.create(Scope.personal("owner", "project", "session"), supervisor_id="host",
        objective="Three independent readers and one dependent reader", request_limit=12,
        policy=PolicyProfile(1, frozenset({"file_read", "file_write"}), frozenset({"ollama"}),
                             max_workers=2, write_roots=(".",)), lease_seconds=300)
    specs = [{"id": name, "objective": "Inspect " + name, "tools": ["file_read"],
              "criteria": ["owner_review"], "dependencies": ["a"] if name == "dependent" else []}
             for name in ("a", "b", "c", "dependent")]
    command(supervisor, authority, "plan", {"work_items": specs})
    return supervisor, authority, workspace


def state(setup):
    return setup[0].store.snapshot(setup[1].scope, setup[1].run_id)


def test_ready_work_fills_freed_slots_and_dependency_waits_for_exact_review(setup):
    providers = []
    def factory(_):
        provider = GatedBackend(events=[text_delta("Read source finding"), done()])
        providers.append(provider)
        return provider
    runner = SwarmWorkerRunner(*setup, backend_factory=factory)
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    try:
        first = scheduler.dispatch_ready()
        assert len(first) == 2
        until(lambda: len(providers) == 2)
        assert all(provider.entered.wait(3) for provider in providers)
        assert len(state(setup)["attempts"]) == 2
        for provider in providers:
            provider.release.set()
        until(lambda: all(not row["alive"] for row in runner.inspect_all()))
        third = scheduler.dispatch_ready()
        assert len(third) == 1
        assert len(state(setup)["attempts"]) == 3
        until(lambda: len(providers) == 3)
        providers[-1].release.set()
        until(lambda: all(not row["alive"] for row in runner.inspect_all()))
        assert scheduler.dispatch_ready() == []
        current = state(setup)
        parent = next(row for row in current["attempts"] if row["work_item_id"] == "a")
        handoff = next(row for row in current["submissions"] if row["attempt_id"] == parent["id"])
        command(setup[0], setup[1], "review_read_result", {"attempt_id": parent["id"], "attempt_epoch": parent["epoch"],
            "candidate_revision": handoff["candidate_revision"], "evidence": "Owner checked parent evidence"})
        assert len(scheduler.dispatch_ready()) == 1
        until(lambda: len(providers) == 4)
        providers[-1].release.set()
        until(lambda: all(not row["alive"] for row in runner.inspect_all()))
        assert len(state(setup)["attempts"]) == 4
        assert state(setup)["run"]["state"] == "running"  # Submission does not complete the team.
    finally:
        scheduler.close()
        for provider in providers:
            provider.release.set()
        runner.close()


def test_scheduling_runs_without_panel_polling_and_stop_closes_admission(setup):
    runner = SwarmWorkerRunner(*setup, backend_factory=lambda _: Backend(events=[text_delta("Finding"), done()]))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    try:
        scheduler.start()
        until(lambda: len(state(setup)["submissions"]) == 3)
        runner.stop()
        before = len(state(setup)["attempts"])
        assert scheduler.dispatch_ready() == []
        assert len(state(setup)["attempts"]) == before
    finally:
        scheduler.close()
        runner.close()


def test_writer_without_isolation_is_blocked_before_reserving_or_launching(setup):
    command(setup[0], setup[1], "plan", {"work_items": [
        {"id": name, "objective": name, "role": "implement", "tools": ["file_write"],
         "write_roots": [name], "criteria": ["checks"]} for name in ("a", "b", "c", "dependent")]})
    runner = SwarmWorkerRunner(*setup, backend_factory=lambda _: pytest.fail("Writer must not use shared checkout"))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    try:
        assert scheduler.dispatch_ready() == []
        assert not state(setup)["attempts"] and len(scheduler.inspect()["blocked"]) == 4
    finally:
        scheduler.close()
        runner.close()


def test_slow_dispatch_does_not_block_inspection_or_stop(setup, monkeypatch):
    runner = SwarmWorkerRunner(*setup, backend_factory=lambda _: Backend(events=[text_delta("Finding"), done()]))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    entered, release = threading.Event(), threading.Event()
    original = scheduler._command
    def blocked(kind, payload):
        entered.set()
        assert release.wait(5)
        return original(kind, payload)
    monkeypatch.setattr(scheduler, "_command", blocked)
    try:
        scheduler.start()
        assert entered.wait(3)
        assert scheduler.inspect()["active"]
        runner.stop()
        release.set()
        until(lambda: not scheduler.inspect()["blocked"] or state(setup)["run"]["state"] == "cancelled")
        assert not state(setup)["attempts"]
    finally:
        release.set()
        scheduler.close()
        runner.close()


def test_close_after_assignment_commit_never_invokes_worker_launcher(setup, monkeypatch):
    runner = SwarmWorkerRunner(*setup, backend_factory=lambda _: pytest.fail("Closed dispatch cannot invoke inference"))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    committed, release = threading.Event(), threading.Event()
    original = scheduler._command
    def held_reply(kind, payload):
        result = original(kind, payload)
        if kind == "assign":
            committed.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(scheduler, "_command", held_reply)
    monkeypatch.setattr(runner, "start", lambda *a, **k: pytest.fail("Launcher ran after scheduling closed"))
    try:
        scheduler.start()
        assert committed.wait(3)
        scheduler.close()
        release.set()
        until(lambda: not scheduler.inspect()["active"])
        current = state(setup)
        assert len(current["attempts"]) == 1
        assert current["attempts"][0]["process_state"] == "stopped"
        assert current["attempts"][0]["state"] == "cancelled"
        assert current["reservations"][0]["state"] == "settled"
        assert not current["model_requests"] and not scheduler.inspect()["error"]
    finally:
        release.set()
        scheduler.close()
        runner.close()


def test_terminal_team_retires_background_dispatch(setup):
    runner = SwarmWorkerRunner(*setup, backend_factory=lambda _: pytest.fail("Terminal team cannot launch"))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    try:
        runner.stop()
        scheduler.start()
        until(lambda: not scheduler.inspect()["active"])
        assert scheduler._stop.is_set()
        assert scheduler.dispatch_ready() == []
        assert not state(setup)["attempts"]
    finally:
        scheduler.close()
        runner.close()
