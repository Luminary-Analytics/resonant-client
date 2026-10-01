"""Actual native planner loops with scripted providers and durable evidence."""

from dataclasses import replace

import pytest

from lumi.engine.swarming import AttemptContext, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.coordinator import CoordinatorPlans
from lumi.engine.swarming.models import ScopeDenied
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.tools import SWARM_TOOL_NAMES
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import done, text_delta, tool_call
from tests.test_swarm_coordinator import response
from tests.test_swarm_workers import Backend, GatedBackend, command, until


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("Native planner inspected this source", encoding="utf-8")
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "state.sqlite"))
    allowed = frozenset({"file_read", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"), supervisor_id="host",
        objective="Investigate two independent questions", request_limit=12,
        policy=PolicyProfile(1, allowed, frozenset({"ollama"})), lease_seconds=300)
    assigned = command(supervisor, authority, "start_coordinator", {"worker_id": "coordinator", "requests": 4,
        "model": {"provider": "ollama", "model": "chosen"}, "tools": sorted(allowed - {"swarm_submit"}),
        "read_roots": ["."]}).result
    context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], "coordinator", authority.epoch)
    plans = CoordinatorPlans(supervisor, authority, allowed_criteria=frozenset({"owner_review"}))
    return supervisor, authority, context, workspace, plans


def snapshot(setup):
    supervisor, authority, *_ = setup
    return supervisor.store.snapshot(authority.scope, authority.run_id)


def start(setup, backend):
    supervisor, authority, context, workspace, plans = setup
    runner = SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda spec: backend)
    runner.start_coordinator(context, BackendSpec("ollama", "chosen"), plans)
    return runner


def test_native_planner_reads_then_proposes_with_exact_generated_input(setup):
    backend = Backend(scripts=[
        [tool_call("file_read", {"path": "evidence.txt"}), done()],
        [text_delta(response(summary="Review résumé and 日本語 source")), done()],
    ])
    runner = start(setup, backend)
    context = setup[2]
    try:
        until(lambda: not runner.inspect(context.attempt_id)["alive"])
        state = snapshot(setup)
        assert state["attempts"][0]["state"] == "completed", runner.inspect_all()
        assert backend.closed and len(backend.captured) == 2
        assert not state["work_items"] and not state["submissions"] and not state["check_receipts"]
        assert len(state["coordinator_inputs"]) == 2 and len(state["coordinator_proposals"]) == 1
        proposal = state["coordinator_proposals"][0]
        assert proposal["request_id"] == state["model_requests"][-1]["id"]
        assert proposal["input_revision"] == 2
        for captured in backend.captured:
            assert any(row.get("input_origin") == "generated" and "Captured planning data" in row.get("content", "")
                       for row in captured["conversation_history"])
            assert not any(tool["function"]["name"] == "swarm_submit" for tool in captured["tools"])
        assert state["action_receipts"][0]["request_id"] == state["model_requests"][0]["id"]
        assert state["action_receipts"][0]["state"] == "completed"
        command(setup[0], setup[1], "decide_proposal", {"proposal_id": proposal["id"], "sha256": proposal["sha256"],
            "accept": True, "evidence": "Fixture owner reviewed the exact bounded plan"})
        assert len(snapshot(setup)["work_items"]) == 2
        assert snapshot(setup)["reservations"][0]["used"] == 2
    finally:
        runner.close()


def test_malformed_native_plan_is_failed_without_graph_or_synthetic_submission(setup):
    runner = start(setup, Backend(events=[text_delta("I have completed every feature"), done()]))
    try:
        until(lambda: not runner.inspect(setup[2].attempt_id)["alive"])
        state = snapshot(setup)
        assert state["attempts"][0]["state"] == "failed"
        assert not state["coordinator_proposals"] and not state["submissions"] and not state["work_items"]
        assert state["reservations"][0]["used"] == 1
    finally:
        runner.close()


def test_stop_during_planner_generation_does_not_admit_late_proposal(setup):
    backend = GatedBackend(events=[text_delta(response()), done()])
    runner = start(setup, backend)
    try:
        assert backend.entered.wait(20)
        runner.stop()
        assert runner.inspect(setup[2].attempt_id)["alive"]
        backend.release.set()
        until(lambda: not runner.inspect(setup[2].attempt_id)["alive"])
        state = snapshot(setup)
        assert not state["coordinator_proposals"] and not state["work_items"]
        assert state["model_requests"][0]["state"] == "uncertain"
        assert state["reservations"][0]["state"] == "uncertain"
    finally:
        backend.release.set()
        runner.close()


def test_coordinator_entry_point_rejects_other_scope_and_normal_worker_start(setup):
    supervisor, authority, context, workspace, plans = setup
    runner = SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda _: pytest.fail("Denied startup called provider"))
    try:
        with pytest.raises(ScopeDenied):
            runner.start(context, BackendSpec("ollama", "chosen"))
        foreign = CoordinatorPlans(supervisor, replace(authority, scope=replace(authority.scope, owner_id="foreign")),
                                   allowed_criteria=frozenset({"owner_review"}))
        with pytest.raises(ScopeDenied):
            runner.start_coordinator(context, BackendSpec("ollama", "chosen"), foreign)
        assert not snapshot(setup)["model_requests"]
    finally:
        runner.close()
