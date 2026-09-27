"""Coordinator proposals retain host provenance and never execute themselves."""

from dataclasses import replace
import json

import pytest

from lumi.engine.swarming import AttemptContext, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.coordinator import CoordinatorPlans
from lumi.engine.swarming.execution import SwarmExecutionGuard
from lumi.engine.swarming.mailbox import SwarmMailbox
from lumi.engine.swarming.models import Conflict, IdempotencyConflict, ScopeDenied
from lumi.engine.swarming.policy import AssignmentGrant, PolicyProfile
from tests.test_swarm_supervisor import command


def response(**changes):
    value = {"summary": "Inspect two independent areas", "use_team": True, "work_items": [
        {"id": name, "objective": "Inspect " + name, "role": "explore", "dependencies": [],
         "read_roots": ["."], "write_roots": [], "criteria": ["owner_review"]} for name in ("api", "ui")]}
    value.update(changes)
    return json.dumps(value)


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: 100)
    supervisor = SwarmSupervisor(store)
    authority = supervisor.create(Scope.personal("owner", "project", "session"), supervisor_id="host",
        objective="Investigate source evidence", request_limit=20,
        policy=PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama"})))
    assigned = command(supervisor, authority, "start_coordinator", {"worker_id": "planner", "requests": 4,
        "model": {"provider": "ollama", "model": "fixture"}, "tools": ["file_read"], "read_roots": ["."]}).result
    context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], "planner", authority.epoch)
    planner = CoordinatorPlans(supervisor, authority, allowed_criteria=frozenset({"owner_review"}))
    prompt = planner.prompt(context)
    command(supervisor, authority, "worker_started", {"attempt_id": context.attempt_id, "attempt_epoch": context.epoch})
    guard = SwarmExecutionGuard(supervisor, authority, context, workspace, AssignmentGrant.from_dict(assigned["grant"]),
                               input_observer=planner.record_input)
    return store, supervisor, authority, context, planner, guard, prompt


def request(setup, *, purpose="primary", completed=True):
    _, _, _, _, _, guard, prompt = setup
    request_id = guard.begin_request(purpose=purpose, inputs={"user_msg": prompt,
        "conversation_history": [{"role": "user", "input_origin": "generated", "content": prompt}],
        "_model_selection": {"provider": "ollama", "model": "fixture"}})
    if completed:
        guard.end_request(request_id, outcome="completed", usage=None, error="")
    return request_id


def stop(setup):
    _, supervisor, authority, context, _, _, _ = setup
    command(supervisor, authority, "worker_stopped", {"attempt_id": context.attempt_id,
        "attempt_epoch": context.epoch, "outcome": "completed", "evidence": "Fixture host observed closed native resources"})


def decide(setup, proposal, **changes):
    _, supervisor, authority, _, _, _, _ = setup
    return command(supervisor, authority, "decide_proposal", {"proposal_id": proposal["id"],
        "sha256": proposal["sha256"], "accept": True, "evidence": "Owner reviewed the bounded plan", **changes})


def test_coordinator_is_visible_peer_without_fabricated_feature_work(setup):
    store, _, authority, context, _, _, prompt = setup
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["work_items"] == []
    assert snapshot["attempts"][0]["kind"] == "coordinator"
    peers = SwarmMailbox(store, context).status()["participants"]
    assert peers[0]["kind"] == "coordinator" and peers[0]["work_item_id"] is None
    assert "runtime commands" in prompt and "host" not in prompt and "supervisor_id" not in prompt


def test_recorded_proposal_does_not_start_work_then_owner_admits_exact_graph(setup):
    store, _, authority, context, planner, _, _ = setup
    request_id = request(setup)
    proposal = planner.record(context, request_id=request_id, text=response())
    envelope = json.loads(proposal["payload_json"])
    assert proposal["state"] == "pending" and proposal["input_revision"] == 1
    assert envelope["plan"]["use_team"] and len(envelope["plan"]["work_items"]) == 2
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["work_items"] == [] and len(snapshot["attempts"]) == 1
    with pytest.raises(Conflict):
        decide(setup, proposal)
    stop(setup)
    decision = decide(setup, proposal)
    assert decision.result["state"] == "accepted"
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert len(snapshot["work_items"]) == 2 and len(snapshot["attempts"]) == 1
    assert all(row["state"] == "ready" for row in snapshot["work_items"])
    assert snapshot["run"]["state"] == "running" and not snapshot["check_receipts"]


def test_proposal_replay_is_immutable_even_after_coordinator_stops(setup):
    store, _, authority, context, planner, _, _ = setup
    request_id = request(setup)
    first = planner.record(context, request_id=request_id, text=response())
    stop(setup)
    assert planner.record(context, request_id=request_id, text=response()) == first
    with pytest.raises(IdempotencyConflict):
        planner.record(context, request_id=request_id, text=response(summary="Altered model result"))
    assert len(store.snapshot(authority.scope, authority.run_id)["coordinator_proposals"]) == 1


@pytest.mark.parametrize("kind", ["unobserved", "auxiliary", "absent"])
def test_proposal_requires_own_completed_primary_request(setup, kind):
    store, _, authority, context, planner, _, _ = setup
    request_id = request(setup, purpose="compression" if kind == "auxiliary" else "primary",
                         completed=kind != "unobserved")
    with pytest.raises(ScopeDenied):
        planner.record(context, request_id="foreign" if kind == "absent" else request_id, text=response())
    assert not store.snapshot(authority.scope, authority.run_id)["coordinator_proposals"]


def test_captured_input_is_required_after_local_context_loss(setup):
    _, supervisor, authority, context, _, _, _ = setup
    request_id = request(setup)
    lost = CoordinatorPlans(supervisor, authority, allowed_criteria=frozenset({"owner_review"}))
    with pytest.raises(Conflict, match="captured planning input"):
        lost.record(context, request_id=request_id, text=response())


@pytest.mark.parametrize("history", [[], [{"role": "tool", "input_origin": "generated"}],
                                    [{"role": "assistant", "input_origin": "generated"}],
                                    [{"role": "user", "input_origin": "human"}]])
def test_prepared_prompt_cannot_be_substituted_with_unrelated_or_quoted_request(setup, history):
    store, _, authority, context, _, guard, prompt = setup
    for entry in history:
        entry["content"] = prompt
    with pytest.raises(ScopeDenied, match="exact generated"):
        guard.begin_request(purpose="primary", inputs={"user_msg": "An unrelated task", "conversation_history": history,
            "_model_selection": {"provider": "ollama", "model": "fixture"}})
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert not snapshot["coordinator_inputs"] and not snapshot["coordinator_proposals"]
    assert not snapshot["request_inputs"]
    assert snapshot["model_requests"][0]["state"] == "reserved"  # No provider start.


def test_guard_without_planning_observer_cannot_produce_a_plan(setup):
    store, _, authority, context, planner, guard, _ = setup
    guard._input_observer = None
    request_id = request(setup)
    with pytest.raises(ScopeDenied, match="planning input receipt"):
        planner.record(context, request_id=request_id, text=response())
    assert not store.snapshot(authority.scope, authority.run_id)["coordinator_proposals"]


def test_foreign_scope_cannot_prepare_or_record_proposal(setup):
    _, _, _, context, planner, _, _ = setup
    foreign = replace(context, scope=replace(context.scope, owner_id="another-owner"))
    with pytest.raises(ScopeDenied):
        planner.prompt(foreign)
    with pytest.raises(ScopeDenied):
        planner.record(foreign, request_id="foreign", text=response())


def test_graph_change_requires_new_proposal_before_admission(setup):
    store, supervisor, authority, context, planner, _, _ = setup
    proposal = planner.record(context, request_id=request(setup), text=response())
    stop(setup)
    command(supervisor, authority, "plan", {"work_items": [{"id": "owner-added", "objective": "Owner changed requirements"}]})
    with pytest.raises(Conflict, match="graph"):
        decide(setup, proposal)
    assert [row["id"] for row in store.snapshot(authority.scope, authority.run_id)["work_items"]] == ["owner-added"]


def test_rejected_proposal_never_adds_graph_work(setup):
    store, _, authority, context, planner, _, _ = setup
    proposal = planner.record(context, request_id=request(setup), text=response())
    stop(setup)
    with pytest.raises(Conflict):
        decide(setup, proposal, sha256="0" * 64)
    assert decide(setup, proposal, accept=False).result["state"] == "rejected"
    assert not store.snapshot(authority.scope, authority.run_id)["work_items"]
