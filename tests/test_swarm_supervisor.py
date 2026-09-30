"""Deterministic supervisor contracts, without model calls or process effects."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import hashlib
import sqlite3

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.models import (
    AdmissionClosed, AllowanceExceeded, Conflict, IdempotencyConflict, LeaseExpired,
    RevisionConflict, SchemaVersionError, ScopeDenied, StaleAuthority,
)
from lumi.engine.swarming.policy import PolicyDenied, PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read", "file_write"}),
                           allowed_providers=frozenset({"ollama", "sonn"}),
                           read_roots=(".",), write_roots=(".",))
    authority = supervisor.create(scope, supervisor_id="supervisor", objective="Fixture", request_limit=10, policy=policy)
    return store, supervisor, scope, authority, now


def command(supervisor, authority, kind, payload=None, *, key=None, revision=None):
    if revision is None:
        revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(key or f"command-{revision}", authority.run_id, revision,
                                      authority.epoch, kind, payload or {}), authority)


def spec(item="a", *, dependencies=(), write=(), read=(".",)):
    return {"id": item, "objective": f"Inspect {item}", "dependencies": list(dependencies),
            "role": "implement" if write else "explore", "read_roots": list(read),
            "write_roots": list(write), "tools": ["file_read"], "criteria": ["proof"]}


def plan(supervisor, authority, items=None):
    return command(supervisor, authority, "plan", {"work_items": items or [spec()]})


def assign(supervisor, authority, item="a", *, requests=3):
    return command(supervisor, authority, "assign", {"work_item_id": item, "worker_id": f"worker-{item}",
                                                    "requests": requests, "model": {"provider": "ollama", "model": "chosen"}}).result


def start(supervisor, authority, item="a", *, requests=3):
    result = assign(supervisor, authority, item, requests=requests)
    command(supervisor, authority, "worker_started", {"attempt_id": result["attempt_id"], "attempt_epoch": result["epoch"]})
    return result


def attempt_payload(attempt, **extra):
    return {"attempt_id": attempt["attempt_id"], "attempt_epoch": attempt["epoch"], **extra}


def request(supervisor, authority, attempt, request_id="request", purpose="main"):
    command(supervisor, authority, "reserve_request", attempt_payload(attempt, request_id=request_id, purpose=purpose))
    command(supervisor, authority, "start_request", {"request_id": request_id, "attempt_epoch": attempt["epoch"]})


def submit_and_stop(supervisor, authority, attempt, candidate="candidate"):
    command(supervisor, authority, "submit", attempt_payload(attempt, candidate_revision=candidate, handoff="Observed findings"))
    command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="submitted", evidence="fixture process join"))


def check(supervisor, authority, attempt, *, candidate="candidate", exit_code=0, check_id="check"):
    command(supervisor, authority, "record_check", attempt_payload(attempt, check_id=check_id, criterion_id="proof",
             candidate_revision=candidate, executor_id="trusted-fixture", check_name="compare fixture", exit_code=exit_code, evidence="fixture output"))


def test_owner_review_is_atomic_exact_and_idempotent(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [{**spec(), "criteria": ["owner_review"]}])
    attempt = start(supervisor, authority)
    submit_and_stop(supervisor, authority, attempt)
    revision = store.snapshot(scope, authority.run_id)["run"]["revision"]
    envelope = Command("review", authority.run_id, revision, authority.epoch, "review_read_result",
                       attempt_payload(attempt, candidate_revision="candidate", evidence="Compared the retained findings to source"))
    receipt = supervisor.handle(envelope, authority)
    assert supervisor.handle(envelope, authority) == receipt
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["work_items"][0]["state"] == "accepted"
    assert len(snapshot["check_receipts"]) == 1
    check_receipt = snapshot["check_receipts"][0]
    assert check_receipt["executor_id"] == "owner:owner"
    assert check_receipt["candidate_revision"] == "candidate"
    assert check_receipt["criterion_id"] == "owner_review"
    assert check_receipt["check_name"] == "Explicit owner review"
    with pytest.raises(IdempotencyConflict):
        supervisor.handle(replace(envelope, payload={**envelope.payload, "evidence": "Different decision"}), authority)
    command(supervisor, authority, "complete")
    assert store.snapshot(scope, authority.run_id)["run"]["state"] == "completed"


@pytest.mark.parametrize("problem", ["criteria", "writer", "candidate", "running", "empty", "oversized"])
def test_owner_review_refuses_other_criteria_writer_or_unobserved_result(setup, problem):
    store, supervisor, scope, authority, now = setup
    item = {**spec(write=("a.txt",) if problem == "writer" else ()), "criteria": ["owner_review"]}
    if problem == "criteria":
        item["criteria"].append("independent_check")
    plan(supervisor, authority, [item])
    attempt = start(supervisor, authority)
    if problem != "running":
        submit_and_stop(supervisor, authority, attempt)
    payload = attempt_payload(attempt, candidate_revision="wrong" if problem == "candidate" else "candidate",
                              evidence="" if problem == "empty" else "x" * 65537 if problem == "oversized" else "Reviewed")
    before = store.snapshot(scope, authority.run_id)
    with pytest.raises((Conflict, ValueError)):
        command(supervisor, authority, "review_read_result", payload)
    after = store.snapshot(scope, authority.run_id)
    assert after["check_receipts"] == []
    assert after["run"]["revision"] == before["run"]["revision"]
    assert after["work_items"] == before["work_items"]


def test_owner_review_rolls_back_receipt_when_acceptance_fails(setup, monkeypatch):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [{**spec(), "criteria": ["owner_review"]}])
    attempt = start(supervisor, authority)
    submit_and_stop(supervisor, authority, attempt)
    def fail_accept(*args, **kwargs):
        raise RuntimeError("injected acceptance failure")
    monkeypatch.setattr(supervisor, "_accept_read_submission", fail_accept)
    with pytest.raises(RuntimeError, match="injected"):
        command(supervisor, authority, "review_read_result",
                attempt_payload(attempt, candidate_revision="candidate", evidence="Reviewed"))
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["check_receipts"] == []
    assert snapshot["work_items"][0]["state"] == "submitted"


def test_command_revision_full_idempotency_and_scope_precede_replay(setup):
    store, supervisor, scope, authority, now = setup
    envelope = Command("plan", authority.run_id, 0, 1, "plan", {"work_items": [spec()]})
    receipt = supervisor.handle(envelope, authority)
    assert receipt.revision == 1
    assert supervisor.handle(envelope, authority) == receipt
    with pytest.raises(IdempotencyConflict):
        supervisor.handle(replace(envelope, payload={"work_items": [spec("other")]}), authority)
    with pytest.raises(RevisionConflict):
        command(supervisor, authority, "pause", revision=0, key="different")
    with pytest.raises(ScopeDenied):
        supervisor.handle(envelope, replace(authority, scope=replace(scope, owner_id="other")))
    with pytest.raises(StaleAuthority):
        supervisor.handle(envelope, replace(authority, supervisor_id="other"))
    with pytest.raises(SchemaVersionError):
        supervisor.handle(replace(envelope, version=999), authority)
    assert len(store.snapshot(scope, authority.run_id)["work_items"]) == 1


def test_lowered_concurrency_drains_workers_without_changing_grants_or_coordinator_capacity(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec(name) for name in ("a", "b", "c", "d")])
    planner = coordinator(supervisor, authority, roots=())
    first = start(supervisor, authority, "a", requests=2)
    second = start(supervisor, authority, "b", requests=2)
    before = store.snapshot(scope, authority.run_id)
    command(supervisor, authority, "set_concurrency", {"max_workers": 1})
    limited = store.snapshot(scope, authority.run_id)
    assert limited["run"]["worker_limit"] == 1
    assert limited["run"]["policy_json"] == before["run"]["policy_json"]
    assert limited["attempts"] == before["attempts"]
    with pytest.raises(Conflict, match="capacity"):
        assign(supervisor, authority, "c", requests=2)
    submit_and_stop(supervisor, authority, first)
    with pytest.raises(Conflict, match="capacity"):
        assign(supervisor, authority, "c", requests=2)
    # A submitted result still owns its slot until process cleanup is observed.
    command(supervisor, authority, "submit", attempt_payload(second, candidate_revision="candidate", handoff="Retained findings"))
    with pytest.raises(Conflict, match="capacity"):
        assign(supervisor, authority, "c", requests=2)
    command(supervisor, authority, "worker_stopped", attempt_payload(second, outcome="submitted", evidence="Observed resource cleanup"))
    third = start(supervisor, authority, "c", requests=2)
    assert third["grant"]["policy_digest"] == first["grant"]["policy_digest"]
    assert any(row["id"] == planner["attempt_id"] and row["state"] == "leased" for row in store.snapshot(scope, authority.run_id)["attempts"])
    command(supervisor, authority, "set_concurrency", {"max_workers": 2})
    assert start(supervisor, authority, "d", requests=2)["grant"]["policy_digest"] == first["grant"]["policy_digest"]


def test_concurrency_command_is_revision_scoped_idempotent_and_does_not_change_policy(setup):
    store, supervisor, scope, authority, now = setup
    assert store.snapshot(scope, authority.run_id)["run"]["worker_limit"] == 2
    envelope = Command("limit", authority.run_id, 0, 1, "set_concurrency", {"max_workers": 1})
    result = supervisor.handle(envelope, authority)
    assert result.result == {"max_workers": 1, "policy_max_workers": 2}
    assert supervisor.handle(envelope, authority) == result
    with pytest.raises(IdempotencyConflict):
        supervisor.handle(replace(envelope, payload={"max_workers": 2}), authority)
    with pytest.raises(RevisionConflict):
        command(supervisor, authority, "set_concurrency", {"max_workers": 2}, key="stale-revision", revision=0)
    with pytest.raises(ScopeDenied):
        supervisor.handle(envelope, replace(authority, scope=replace(scope, owner_id="foreign")))
    with pytest.raises(StaleAuthority):
        supervisor.handle(envelope, replace(authority, supervisor_id="foreign"))
    for invalid in (0, 3, True, 1.5, None, "1"):
        with pytest.raises(ValueError):
            command(supervisor, authority, "set_concurrency", {"max_workers": invalid})
    command(supervisor, authority, "pause")
    command(supervisor, authority, "set_concurrency", {"max_workers": 2})
    assert store.snapshot(scope, authority.run_id)["run"]["state"] == "paused"
    command(supervisor, authority, "stop")
    with pytest.raises(AdmissionClosed):
        command(supervisor, authority, "set_concurrency", {"max_workers": 1})


def test_concurrency_preserves_explicit_policy_cap_and_recovery_fence(setup):
    store, supervisor, scope, authority, now = setup
    policy = PolicyProfile.from_dict(json.loads(store.snapshot(scope, authority.run_id)["run"]["policy_json"]))
    four = supervisor.create(scope, supervisor_id="four", objective="Four-worker fixture", request_limit=20,
                             policy=replace(policy, max_workers=4))
    assert store.snapshot(scope, four.run_id)["run"]["worker_limit"] == 4
    command(supervisor, four, "set_concurrency", {"max_workers": 1})
    command(supervisor, four, "set_concurrency", {"max_workers": 4})
    with pytest.raises(ValueError):
        command(supervisor, four, "set_concurrency", {"max_workers": 5})
    now[0] = 1031
    fresh = supervisor.acquire(scope, four.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    with pytest.raises(StaleAuthority):
        command(supervisor, four, "set_concurrency", {"max_workers": 1})
    with pytest.raises(AdmissionClosed):
        command(supervisor, fresh, "set_concurrency", {"max_workers": 1})
    command(supervisor, fresh, "recover", {"retry_work_items": []})
    assert command(supervisor, fresh, "set_concurrency", {"max_workers": 1}).result["policy_max_workers"] == 4


def test_dag_readiness_cycles_and_assignment_contract_immutability(setup):
    store, supervisor, scope, authority, now = setup
    cyclic = [spec("a", dependencies=("b",)), spec("b", dependencies=("a",))]
    with pytest.raises(Conflict, match="cycle"):
        plan(supervisor, authority, cyclic)
    assert store.snapshot(scope, authority.run_id)["work_items"] == []
    plan(supervisor, authority, [spec("a"), spec("b", dependencies=("a",))])
    assert [row["state"] for row in store.snapshot(scope, authority.run_id)["work_items"]] == ["ready", "pending"]
    with pytest.raises(Conflict, match="not ready"):
        assign(supervisor, authority, "b")
    attempt = start(supervisor, authority)
    with pytest.raises(Conflict, match="immutable"):
        plan(supervisor, authority, [{**spec(), "objective": "Changed"}, spec("b", dependencies=("a",))])
    submit_and_stop(supervisor, authority, attempt)
    with pytest.raises(Conflict, match="passing evidence"):
        command(supervisor, authority, "accept", attempt_payload(attempt))
    check(supervisor, authority, attempt)
    command(supervisor, authority, "accept", attempt_payload(attempt))
    assert [row["state"] for row in store.snapshot(scope, authority.run_id)["work_items"]] == ["accepted", "ready"]


def test_default_capacity_and_explicit_provider_model_admission(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec("a"), spec("b"), spec("c")])
    for model in ({"provider": "codex", "model": "chosen"}, {"provider": "ollama", "model": ""}):
        with pytest.raises((PolicyDenied, ValueError)):
            command(supervisor, authority, "assign", {"work_item_id": "a", "worker_id": "a", "requests": 1, "model": model})
    first = assign(supervisor, authority)
    second = assign(supervisor, authority, "b")
    with pytest.raises(Conflict, match="capacity"):
        assign(supervisor, authority, "c")
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["remaining_requests"] == 4
    assert first["grant"]["model"] == second["grant"]["model"] == {"provider": "ollama", "model": "chosen"}
    with pytest.raises(Conflict, match="versioned"):
        store.claim(authority, work_item_id="c", worker_id="bypass", requests=1, command_id="bypass")


def test_scope_conflicts_use_path_segments_and_windows_case(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec("a", read=("src",), write=("src",)), spec("b", read=("SRC/file.py",)), spec("c", read=("src-extra",))])
    assign(supervisor, authority)
    with pytest.raises(Conflict, match="scope conflicts"):
        assign(supervisor, authority, "b")
    assign(supervisor, authority, "c")
    assert len(store.snapshot(scope, authority.run_id)["attempts"]) == 2


def test_concurrent_command_copies_commit_once_and_different_commands_revise_once(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    envelope = Command("assign", authority.run_id, 1, 1, "assign",
                       {"work_item_id": "a", "worker_id": "worker", "requests": 3,
                        "model": {"provider": "ollama", "model": "chosen"}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: SwarmSupervisor(SwarmStore(store.path, clock=lambda: 1000)).handle(envelope, authority), range(2)))
    assert results[0] == results[1]
    assert len(store.snapshot(scope, authority.run_id)["attempts"]) == 1
    with pytest.raises(RevisionConflict):
        supervisor.handle(replace(envelope, command_id="other"), authority)


def test_lease_expiry_does_not_replay_or_release_unknown_work(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    request(supervisor, authority, attempt)
    with pytest.raises(Conflict, match="active supervisor"):
        supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="new", command_id="takeover")
    now[0] = 1031
    with pytest.raises(LeaseExpired):
        command(supervisor, authority, "renew")
    context = AttemptContext(scope, authority.run_id, attempt["attempt_id"], attempt["worker_id"], 1)
    with pytest.raises(LeaseExpired):
        store.receive(context)
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="new", command_id="takeover")
    assert recovered.epoch == 2
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["remaining_requests"] == 7
    assert snapshot["run"]["state"] == "recovery_required"
    assert snapshot["model_requests"][0]["state"] == "uncertain"
    with pytest.raises(StaleAuthority):
        command(supervisor, authority, "pause")
    with pytest.raises(Conflict, match="unresolved"):
        command(supervisor, recovered, "recover", {"retry_work_items": ["a"]})
    command(supervisor, recovered, "worker_stopped", attempt_payload(attempt, outcome="failed", evidence="fixture PID joined"))
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 7
    with pytest.raises(Conflict, match="unresolved"):
        command(supervisor, recovered, "recover", {"retry_work_items": ["a"]})
    command(supervisor, recovered, "reconcile_request", {"request_id": "request", "attempt_epoch": 1,
            "outcome": "failed", "used": 1, "evidence": "fixture provider request receipt"})
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 9
    command(supervisor, recovered, "recover", {"retry_work_items": ["a"]})
    retry = assign(supervisor, recovered)
    assert retry["attempt_id"] != attempt["attempt_id"]
    assert retry["epoch"] == 2
    assert len(store.snapshot(scope, authority.run_id)["attempts"]) == 2


def test_renew_requires_live_ownership_and_creation_retry_cannot_renew(setup):
    store, supervisor, scope, authority, now = setup
    now[0] = 1020
    receipt = command(supervisor, authority, "renew", key="renew")  # keeps the revision: its own key
    assert receipt.result["lease_until"] == 1050
    now[0] = 1035
    command(supervisor, authority, "pause")
    now[0] = 1051
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="supervisor", command_id="takeover")
    policy = PolicyProfile.from_dict(json.loads(store.snapshot(scope, authority.run_id)["run"]["policy_json"]))
    with pytest.raises(StaleAuthority):
        supervisor.create(scope, supervisor_id="supervisor", objective="Fixture", request_limit=10,
                          policy=policy, run_id=authority.run_id)
    with pytest.raises(Conflict, match="active supervisor"):
        supervisor.acquire(scope, authority.run_id, expected_epoch=2, supervisor_id="third", command_id="steal")
    assert recovered.epoch == 2


def test_a_lease_renewal_keeps_the_revision_a_person_read(setup):
    """A renewal (every 5 s while a team runs) changes only the lease.

    It advanced the revision, so a Stop or a decision sent from the view read
    just before it was refused ("Run revision changed; refresh the snapshot").
    """
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    read = store.snapshot(scope, authority.run_id)["run"]
    now[0] = 1010
    renewed = command(supervisor, authority, "renew", key="renewal")
    after = store.snapshot(scope, authority.run_id)["run"]
    assert renewed.revision == after["revision"] == read["revision"]
    assert after["lease_until"] == 1040 and after["event_sequence"] == read["event_sequence"] + 1
    stopped = command(supervisor, authority, "stop", key="person-stop", revision=read["revision"])
    assert (stopped.revision, stopped.state) == (read["revision"] + 1, "cancelled")


def test_a_renewal_that_moves_the_run_on_advances_the_revision(setup):
    """Its checkpoint ends a stopping run whose last effect resolved without one of its own."""
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    command(supervisor, authority, "stop", key="stop")
    assert store.snapshot(scope, authority.run_id)["run"]["state"] == "stopping"
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE attempts SET process_state='stopped' WHERE id=?", (attempt["attempt_id"],))
        connection.execute("UPDATE reservations SET state='settled' WHERE attempt_id=?", (attempt["attempt_id"],))
    before = store.snapshot(scope, authority.run_id)["run"]["revision"]
    renewed = command(supervisor, authority, "renew", key="renewal")
    assert (renewed.state, renewed.revision) == ("cancelled", before + 1)


def test_request_ids_purposes_accounting_and_unknown_spend(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority, requests=2)
    request(supervisor, authority, attempt)
    command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1, "outcome": "completed", "used": 1})
    request(supervisor, authority, attempt, "aux", "auxiliary")
    with pytest.raises(AllowanceExceeded):
        command(supervisor, authority, "reserve_request", attempt_payload(attempt, request_id="over-limit", purpose="main"))
    command(supervisor, authority, "settle_request", {"request_id": "aux", "attempt_epoch": 1, "outcome": "uncertain", "used": None})
    with pytest.raises(Conflict, match="Unknown accounting"):
        command(supervisor, authority, "reserve_request", attempt_payload(attempt, request_id="extra", purpose="main"))
    with pytest.raises(Conflict, match="explicit request reconciliation"):
        command(supervisor, authority, "settle_request", {"request_id": "aux", "attempt_epoch": 1, "outcome": "not_started", "used": 0})
    command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="failed", evidence="fixture terminated"))
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 8
    assert [row["purpose"] for row in store.snapshot(scope, authority.run_id)["model_requests"]] == ["main", "auxiliary"]


def test_pause_stop_and_request_settlement_are_distinct(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    request(supervisor, authority, attempt)
    assert command(supervisor, authority, "pause").state == "pausing"
    with pytest.raises(AdmissionClosed):
        command(supervisor, authority, "reserve_request", attempt_payload(attempt, request_id="new", purpose="main"))
    with pytest.raises(Conflict):
        command(supervisor, authority, "resume")
    assert command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1,
                   "outcome": "completed", "used": 1}).state == "paused"
    assert command(supervisor, authority, "resume").state == "running"
    assert command(supervisor, authority, "stop").state == "stopping"
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 7
    assert command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="cancelled", evidence="fixture PID joined")).state == "cancelled"
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 9


def test_submission_is_immutable_and_acceptance_binds_exact_checks(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    command(supervisor, authority, "submit", attempt_payload(attempt, candidate_revision="candidate", handoff="All checks pass (untrusted claim)"))
    with pytest.raises(Conflict, match="immutable"):
        command(supervisor, authority, "submit", attempt_payload(attempt, candidate_revision="other", handoff="Revised"))
    with pytest.raises(Conflict, match="termination"):
        command(supervisor, authority, "accept", attempt_payload(attempt))
    command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="submitted", evidence="fixture joined"))
    check(supervisor, authority, attempt, candidate="wrong")
    with pytest.raises(Conflict, match="passing evidence"):
        command(supervisor, authority, "accept", attempt_payload(attempt))
    check(supervisor, authority, attempt, exit_code=1, check_id="failed")
    with pytest.raises(Conflict):
        command(supervisor, authority, "accept", attempt_payload(attempt))
    check(supervisor, authority, attempt, check_id="passed")
    command(supervisor, authority, "accept", attempt_payload(attempt))
    assert command(supervisor, authority, "complete").state == "completed"
    assert store.snapshot(scope, authority.run_id)["submissions"][0]["handoff"].startswith("All checks pass")


def test_writer_submission_cannot_skip_candidate_integration(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec(write=("src",), read=("src",))])
    attempt = start(supervisor, authority)
    submit_and_stop(supervisor, authority, attempt)
    check(supervisor, authority, attempt)
    with pytest.raises(Conflict, match="integration protocol"):
        command(supervisor, authority, "accept", attempt_payload(attempt))


def test_command_event_write_failure_rolls_back_graph_and_revision(setup):
    store, supervisor, scope, authority, now = setup
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_command BEFORE INSERT ON events WHEN NEW.kind='command_plan' "
                           "BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture write failure"):
        plan(supervisor, authority)
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["run"]["revision"] == 0
    assert snapshot["work_items"] == snapshot["dependencies"] == []


def test_unknown_tool_action_blocks_pause_stop_and_recovery_until_observed(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    request(supervisor, authority, attempt)
    command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1, "outcome": "completed", "used": 1})
    # The execution guard commits this record before invoking a tool.
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO action_receipts(id,attempt_id,epoch,request_id,call_id,tool_name,arguments_sha256,state) "
                           "VALUES('action',?,1,'request','call','file_read','digest','admitted')", (attempt["attempt_id"],))
    assert command(supervisor, authority, "pause").state == "pausing"
    assert command(supervisor, authority, "stop").state == "stopping"
    command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="cancelled", evidence="fixture process joined"))
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["run"]["state"] == "stopping"
    assert snapshot["action_receipts"][0]["state"] == "uncertain"
    assert snapshot["attempts"][0]["state"] == "uncertain"
    assert command(supervisor, authority, "reconcile_action", {"action_id": "action", "attempt_epoch": 1,
                   "outcome": "failed", "evidence": "fixture tool output records termination"}).state == "cancelled"


def test_stop_intent_survives_lease_takeover_without_restart(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    command(supervisor, authority, "stop")
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="recovery")
    command(supervisor, recovered, "worker_stopped", attempt_payload(attempt, outcome="cancelled", evidence="fixture joined"))
    with pytest.raises(Conflict, match="stopped run"):
        command(supervisor, recovered, "recover", {"retry_work_items": ["a"]})
    assert command(supervisor, recovered, "recover", {"retry_work_items": []}).state == "cancelled"


def test_managed_context_receipt_cannot_bypass_durable_model_input_assignment(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec("a"), spec("b")])
    first = start(supervisor, authority)
    second = start(supervisor, authority, "b")
    sender = AttemptContext(scope, authority.run_id, first["attempt_id"], first["worker_id"], 1)
    recipient = AttemptContext(scope, authority.run_id, second["attempt_id"], second["worker_id"], 1)
    message = store.send(sender, recipient_attempt_id=recipient.attempt_id, kind="finding", body="Evidence", command_id="message")
    store.acknowledge(recipient, message.id, stage="runtime")
    with pytest.raises(Conflict, match="atomic mailbox"):
        store.acknowledge(recipient, message.id, stage="context", model_request_id="forged-request", input_revision=1)
    assert len(store.snapshot(scope, authority.run_id)["receipts"]) == 1


def test_reject_and_retry_keep_previous_attempt_and_submission_immutable(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    with pytest.raises(Conflict):
        command(supervisor, authority, "retry", {"work_item_id": "a", "evidence": "Premature"})
    submit_and_stop(supervisor, authority, attempt)
    command(supervisor, authority, "reject", attempt_payload(attempt, evidence="Trusted exact-candidate check failed"))
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["attempts"][0]["state"] == "failed"
    handoff = snapshot["submissions"][0]
    command(supervisor, authority, "retry", {"work_item_id": "a", "evidence": "Repair observed failure"})
    replacement = assign(supervisor, authority)
    assert replacement["attempt_id"] != attempt["attempt_id"]
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["submissions"] == [handoff]
    assert len(snapshot["attempts"]) == 2


def test_retry_rejects_unknown_effect_even_if_process_is_stopped(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    request(supervisor, authority, attempt)
    command(supervisor, authority, "worker_stopped", attempt_payload(attempt, outcome="failed", evidence="fixture process joined"))
    with pytest.raises(Conflict):
        command(supervisor, authority, "retry", {"work_item_id": "a", "evidence": "Unsafe retry"})
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 7


def test_recovery_owner_can_explicitly_reject_retained_submission_for_new_attempt(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    submit_and_stop(supervisor, authority, attempt)
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="new", command_id="takeover")
    command(supervisor, recovered, "reject", attempt_payload(attempt, evidence="Repeat verification with new ownership"))
    command(supervisor, recovered, "recover", {"retry_work_items": ["a"]})
    replacement = assign(supervisor, recovered)
    assert replacement["epoch"] == 2
    assert replacement["attempt_id"] != attempt["attempt_id"]
    assert len(store.snapshot(scope, authority.run_id)["submissions"]) == 1


def coordinator(supervisor, authority, *, identity="coordinator", requests=2, roots=()):
    return command(supervisor, authority, "start_coordinator", {
        "worker_id": identity, "requests": requests, "model": {"provider": "ollama", "model": "planner"},
        "tools": ["file_read"], "read_roots": list(roots),
    }).result


def test_coordinator_accounts_requests_without_worker_capacity_or_fake_work(setup):
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    assert store.snapshot(scope, authority.run_id)["work_items"] == []
    plan(supervisor, authority, [spec("a"), spec("b"), spec("c")])
    assign(supervisor, authority, "a", requests=2)
    assign(supervisor, authority, "b", requests=2)
    with pytest.raises(Conflict, match="capacity"):
        assign(supervisor, authority, "c", requests=1)
    with pytest.raises(Conflict, match="coordinator"):
        coordinator(supervisor, authority, identity="second")
    command(supervisor, authority, "worker_started", attempt_payload(planner))
    request(supervisor, authority, planner)
    command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1, "outcome": "completed", "used": 1})
    command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="completed", evidence="Planner transport joined"))
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["attempts"][0]["kind"] == "coordinator"
    assert snapshot["attempts"][0]["work_item_id"] is None
    assert snapshot["attempts"][0]["state"] == "completed"
    assert snapshot["remaining_requests"] == 5
    assert [work["state"] for work in snapshot["work_items"]] == ["leased", "leased", "ready"]
    replacement = coordinator(supervisor, authority, identity="next-planner")
    assert replacement["attempt_id"] != planner["attempt_id"]


@pytest.mark.parametrize("tool", ["file_write", "file_edit", "swarm_submit", "shell", "spawn_worker"])
def test_coordinator_has_no_write_submission_or_dispatch_capability(setup, tool):
    store, supervisor, scope, authority, now = setup
    with pytest.raises(ScopeDenied, match="only native read"):
        command(supervisor, authority, "start_coordinator", {"worker_id": "coordinator", "requests": 1,
                "model": {"provider": "ollama", "model": "planner"}, "tools": [tool], "read_roots": []})
    assert store.snapshot(scope, authority.run_id)["attempts"] == []


def test_coordinator_proposals_cannot_be_submitted_or_accepted_as_work(setup):
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    command(supervisor, authority, "worker_started", attempt_payload(planner))
    for kind, extra in [("submit", {"candidate_revision": "prose", "handoff": "Trust me"}), ("accept", {}),
                        ("review_read_result", {"candidate_revision": "prose", "evidence": "Reviewed"}),
                        ("accept_writer", {"candidate_id": "prose", "evidence": "Reviewed"}), ("reject", {"evidence": "Reviewed"})]:
        with pytest.raises(Conflict, match="Coordinator"):
            command(supervisor, authority, kind, attempt_payload(planner, **extra))
    with pytest.raises(ValueError, match="Termination"):
        command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="submitted", evidence="Prose"))
    command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="completed", evidence="Planner returned"))
    with pytest.raises(Conflict, match="all work accepted"):
        command(supervisor, authority, "complete")
    assert store.snapshot(scope, authority.run_id)["submissions"] == []


def test_coordinator_read_scope_conflicts_both_directions_with_writers(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec(write=("src",), read=("src",))])
    planner = coordinator(supervisor, authority, roots=("SRC",))
    with pytest.raises(Conflict, match="scope conflicts"):
        assign(supervisor, authority)
    command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="cancelled", evidence="Never started dispatcher joined"))
    assign(supervisor, authority)
    with pytest.raises(Conflict, match="scope conflicts"):
        coordinator(supervisor, authority, roots=("src",))
    assert coordinator(supervisor, authority, roots=())["kind"] == "coordinator"


def test_stop_and_recovery_retain_coordinator_process_and_accounting_uncertainty(setup):
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    command(supervisor, authority, "worker_started", attempt_payload(planner))
    request(supervisor, authority, planner)
    command(supervisor, authority, "stop")
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="acquire")
    with pytest.raises(Conflict, match="unresolved"):
        command(supervisor, recovered, "recover", {"retry_work_items": []})
    command(supervisor, recovered, "worker_stopped", attempt_payload(planner, outcome="cancelled", evidence="Planner process joined"))
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["attempts"][0]["state"] == "uncertain"
    assert snapshot["remaining_requests"] == 8
    with pytest.raises(Conflict, match="unresolved"):
        command(supervisor, recovered, "recover", {"retry_work_items": []})
    command(supervisor, recovered, "reconcile_request", {"request_id": "request", "attempt_epoch": 1,
            "outcome": "failed", "used": 1, "evidence": "Provider observed failed request"})
    assert command(supervisor, recovered, "recover", {"retry_work_items": []}).state == "cancelled"
    assert store.snapshot(scope, authority.run_id)["work_items"] == []


def test_completion_waits_for_coordinator_without_requiring_its_acceptance(setup):
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    plan(supervisor, authority)
    worker = start(supervisor, authority)
    submit_and_stop(supervisor, authority, worker)
    check(supervisor, authority, worker)
    command(supervisor, authority, "accept", attempt_payload(worker))
    with pytest.raises(Conflict, match="all effects"):
        command(supervisor, authority, "complete")
    command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="completed", evidence="Planning dispatcher joined"))
    assert command(supervisor, authority, "complete").state == "completed"


def test_coordinator_admission_race_preserves_one_claim_and_allowance(setup):
    store, supervisor, scope, authority, now = setup
    def admit(identity):
        try:
            return supervisor.handle(Command(identity, authority.run_id, 0, 1, "start_coordinator", {
                "worker_id": identity, "requests": 6, "model": {"provider": "ollama", "model": "planner"},
                "tools": [], "read_roots": [],
            }), authority)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ["first", "second"]))
    assert sum(result is not None for result in results) == 1
    snapshot = store.snapshot(scope, authority.run_id)
    assert len(snapshot["attempts"]) == len(snapshot["reservations"]) == len(snapshot["dispatches"]) == 1
    assert snapshot["remaining_requests"] == 4


def test_owned_process_observation_blocks_stop_and_allowance_release(setup):
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    command(supervisor, authority, "worker_started", attempt_payload(planner))
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO process_observations VALUES(?,?,?,?,?,?,?,'started',NULL)",
                           (planner["attempt_id"], authority.run_id, 1, "host", 123, 1.0, "token"))
    request(supervisor, authority, planner)
    command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1, "outcome": "completed", "used": 1})
    assert store.snapshot(scope, authority.run_id)["reservations"][0]["state"] == "reserved"
    command(supervisor, authority, "stop")
    receipt = command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="cancelled", evidence="Root exited, descendants unknown"))
    assert receipt.state == "stopping"
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["attempts"][0]["state"] == "uncertain"
    assert snapshot["reservations"][0]["state"] == "uncertain"
    assert snapshot["remaining_requests"] == 8
    with store._connection(write=True) as connection:
        connection.execute("UPDATE process_observations SET state='stopped'")
        supervisor._refresh_reservation(connection, planner["attempt_id"])
    assert command(supervisor, authority, "checkpoint").state == "cancelled"


def retained_proposal(setup, *, unicode_text=False):
    """Trusted receipt fixture, independent of proposal parsing/model execution."""
    store, supervisor, scope, authority, now = setup
    planner = coordinator(supervisor, authority)
    command(supervisor, authority, "worker_started", attempt_payload(planner))
    request(supervisor, authority, planner)
    command(supervisor, authority, "settle_request", {"request_id": "request", "attempt_epoch": 1, "outcome": "completed", "used": 1})
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    with store._connection(write=True) as connection:
        graph = [dict(row) for row in connection.execute("SELECT id,specification,revision,state FROM work_items WHERE run_id=? ORDER BY id", (authority.run_id,))]
        item = {**spec(), "objective": "調査 café 🌍", "criteria": ["検証"]} if unicode_text else spec()
        envelope = {"plan": {"summary": "Résumé 検証" if unicode_text else "Investigate the fixture", "use_team": False, "work_items": [item]},
                    "source_sha256": "a" * 64, "allowed_criteria": item["criteria"], "policy_digest": planner["grant"]["policy_digest"],
                    "input_sha256": "b" * 64, "graph_sha256": hashlib.sha256(encode(graph).encode()).hexdigest()}
        digest = hashlib.sha256(encode(envelope).encode()).hexdigest()
        connection.execute("INSERT INTO request_inputs(request_id,input_sha256,purpose,observation_outcome) VALUES('request',?,'primary','completed')", (envelope["input_sha256"],))
        connection.execute("INSERT INTO coordinator_inputs VALUES('request',?,?,?,?,?,?)",
                           (planner["attempt_id"], envelope["input_sha256"], "c" * 64, envelope["graph_sha256"], envelope["policy_digest"], encode(item["criteria"])))
        connection.execute("INSERT INTO coordinator_proposals VALUES('proposal',?,?,'request',1,1,?,?,'pending','')",
                           (authority.run_id, planner["attempt_id"], digest, encode(envelope)))
    command(supervisor, authority, "worker_stopped", attempt_payload(planner, outcome="completed", evidence="Trusted fixture process joined"))
    return {"proposal_id": "proposal", "sha256": digest, "accept": True, "evidence": "Owner reviewed the retained proposal"}


def test_proposal_decision_merges_retained_graph_atomically_and_replays(setup, monkeypatch):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [spec("retained")])
    decision = retained_proposal(setup)
    original = store._event
    def fail_event(connection, run_id, kind, payload):
        if kind == "command_decide_proposal":
            raise sqlite3.OperationalError("injected decision failure")
        return original(connection, run_id, kind, payload)
    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", fail_event)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            command(supervisor, authority, "decide_proposal", decision)
    snapshot = store.snapshot(scope, authority.run_id)
    assert [work["id"] for work in snapshot["work_items"]] == ["retained"]
    assert snapshot["coordinator_proposals"][0]["state"] == "pending"
    envelope = Command("decision", authority.run_id, snapshot["run"]["revision"], 1, "decide_proposal", decision)
    result = supervisor.handle(envelope, authority)
    assert supervisor.handle(envelope, authority) == result
    snapshot = store.snapshot(scope, authority.run_id)
    assert {work["id"] for work in snapshot["work_items"]} == {"a", "retained"}
    assert snapshot["coordinator_proposals"][0]["state"] == "accepted"
    assert all(work["state"] == "ready" for work in snapshot["work_items"])


@pytest.mark.parametrize("problem", ["digest", "graph", "input", "input_receipt", "ordinal", "process"])
def test_proposal_decision_rechecks_exact_dispatched_input_and_current_graph(setup, problem):
    store, supervisor, scope, authority, now = setup
    decision = retained_proposal(setup)
    if problem == "digest":
        decision["sha256"] = "wrong"
    if problem == "graph":
        plan(supervisor, authority, [spec("changed")])
    with store._connection(write=True) as connection:
        if problem == "input":
            connection.execute("UPDATE request_inputs SET input_sha256='different'")
        elif problem == "input_receipt":
            connection.execute("DELETE FROM coordinator_inputs")
        elif problem == "ordinal":
            connection.execute("UPDATE coordinator_proposals SET input_revision=2")
        elif problem == "process":
            attempt = connection.execute("SELECT id FROM attempts").fetchone()[0]
            connection.execute("INSERT INTO process_observations VALUES(?,?,?,?,?,?,?,'unknown',NULL)", (attempt, authority.run_id, 1, "host", 123, 1.0, "token"))
    before = store.snapshot(scope, authority.run_id)
    with pytest.raises(Conflict):
        command(supervisor, authority, "decide_proposal", decision)
    after = store.snapshot(scope, authority.run_id)
    assert after["work_items"] == before["work_items"]
    assert after["coordinator_proposals"][0]["state"] == "pending"


def test_explicit_proposal_rejection_retains_evidence_without_changing_graph(setup):
    store, supervisor, scope, authority, now = setup
    decision = retained_proposal(setup)
    command(supervisor, authority, "decide_proposal", {**decision, "accept": False, "evidence": "No useful parallel work"})
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["work_items"] == []
    assert snapshot["coordinator_proposals"][0]["state"] == "rejected"
    assert snapshot["coordinator_proposals"][0]["decision_evidence"] == "No useful parallel work"


def test_proposal_fingerprints_preserve_unicode_summary_criteria_and_graph(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [{**spec("既存"), "objective": "保持する résumé"}])
    decision = retained_proposal(setup, unicode_text=True)
    command(supervisor, authority, "decide_proposal", decision)
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["coordinator_proposals"][0]["state"] == "accepted"
    works = {work["id"]: json.loads(work["specification"]) for work in snapshot["work_items"]}
    assert works["a"]["objective"] == "調査 café 🌍"
    assert works["a"]["criteria"] == ["検証"]
    assert works["既存"]["objective"] == "保持する résumé"


@pytest.mark.parametrize("outcome", ["not_started", "failed"])
def test_started_request_cannot_be_refunded_after_takeover_reopen_and_backup(setup, tmp_path, outcome):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    request(supervisor, authority, attempt)
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    backup = tmp_path / "recovered.sqlite"
    SwarmStore.backup_database(store.path, backup)
    reopened = SwarmStore(backup, clock=lambda: now[0])
    supervisor = SwarmSupervisor(reopened)
    assert reopened.snapshot(scope, authority.run_id)["model_requests"][0]["state"] == "uncertain"
    assert any(event.kind == "command_start_request" for event in reopened.events(scope, authority.run_id))
    with pytest.raises(Conflict, match="refunded|one request unit"):
        command(supervisor, recovered, "reconcile_request", {"request_id": "request", "attempt_epoch": 1,
                "outcome": outcome, "used": 0, "evidence": "Owner prose says the error should be free"})
    snapshot = reopened.snapshot(scope, authority.run_id)
    assert snapshot["model_requests"][0]["used"] is None
    assert snapshot["remaining_requests"] == 7
    command(supervisor, recovered, "reconcile_request", {"request_id": "request", "attempt_epoch": 1,
            "outcome": "failed", "used": 1, "evidence": "Provider receipt confirms a failed invocation"})
    assert reopened.snapshot(scope, authority.run_id)["model_requests"][0]["used"] == 1


def test_never_started_reservation_can_be_reconciled_unused_with_explicit_evidence(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority)
    attempt = start(supervisor, authority)
    command(supervisor, authority, "reserve_request", attempt_payload(attempt, request_id="reserved", purpose="main"))
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    command(supervisor, recovered, "worker_stopped", attempt_payload(attempt, outcome="failed", evidence="Owned process termination observed"))
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 7
    with pytest.raises(Conflict, match="observed start"):
        command(supervisor, recovered, "reconcile_request", {"request_id": "reserved", "attempt_epoch": 1,
                "outcome": "completed", "used": 1, "evidence": "Unattributed owner prose cannot invent invocation"})
    command(supervisor, recovered, "reconcile_request", {"request_id": "reserved", "attempt_epoch": 1,
            "outcome": "not_started", "used": 0, "evidence": "Reservation committed, start was never admitted, and captured process is stopped"})
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 10


def test_current_owner_can_review_retained_read_result_without_reviving_old_worker(setup):
    store, supervisor, scope, authority, now = setup
    plan(supervisor, authority, [{**spec(), "criteria": ["owner_review"]}])
    attempt = start(supervisor, authority)
    submit_and_stop(supervisor, authority, attempt)
    handoff = store.snapshot(scope, authority.run_id)["submissions"][0]
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    command(supervisor, recovered, "recover", {"retry_work_items": []})
    with pytest.raises(StaleAuthority):
        check(supervisor, recovered, attempt)
    with pytest.raises(StaleAuthority):
        command(supervisor, recovered, "submit", attempt_payload(attempt, candidate_revision="new", handoff="Stale worker text"))
    with pytest.raises(Conflict, match="exact submitted"):
        command(supervisor, recovered, "review_read_result", attempt_payload(attempt, candidate_revision="different", evidence="Reviewed"))
    with pytest.raises(StaleAuthority):
        command(supervisor, authority, "review_read_result", attempt_payload(attempt, candidate_revision="candidate", evidence="Old owner"))
    command(supervisor, recovered, "review_read_result", attempt_payload(attempt, candidate_revision="candidate", evidence="New owner reviewed retained source findings"))
    assert command(supervisor, recovered, "complete").state == "completed"
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["submissions"] == [handoff]
    assert snapshot["attempts"][0]["epoch"] == 1
    assert snapshot["run"]["epoch"] == 2
    assert snapshot["check_receipts"][0]["executor_id"] == "owner:owner"


@pytest.mark.parametrize("accept", [True, False])
def test_fresh_owner_decides_retained_proposal_without_reviving_coordinator(setup, accept):
    store, supervisor, scope, authority, now = setup
    decision = {**retained_proposal(setup), "accept": accept, "evidence": "Replacement owner reviewed the original captured plan"}
    original = store.snapshot(scope, authority.run_id)
    planner = original["attempts"][0]
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    with pytest.raises(Conflict):
        command(supervisor, recovered, "decide_proposal", decision)
    command(supervisor, recovered, "recover", {"retry_work_items": []})
    with pytest.raises(StaleAuthority):
        command(supervisor, authority, "decide_proposal", decision)
    with pytest.raises(StaleAuthority):
        command(supervisor, recovered, "worker_started", {"attempt_id": planner["id"], "attempt_epoch": 1})
    snapshot = store.snapshot(scope, authority.run_id)
    envelope = Command("historical-decision", authority.run_id, snapshot["run"]["revision"], recovered.epoch,
                       "decide_proposal", decision)
    result = supervisor.handle(envelope, recovered)
    assert supervisor.handle(envelope, recovered) == result
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["attempts"] == original["attempts"]
    assert snapshot["model_requests"] == original["model_requests"]
    assert snapshot["coordinator_inputs"] == original["coordinator_inputs"]
    proposal = snapshot["coordinator_proposals"][0]
    assert proposal["epoch"] == 1 and snapshot["run"]["epoch"] == 2
    assert proposal["state"] == ("accepted" if accept else "rejected")
    assert proposal["sha256"] == decision["sha256"]
    assert proposal["decision_evidence"] == decision["evidence"]
    assert len(snapshot["work_items"]) == int(accept)
    assert len([event for event in store.events(scope, authority.run_id) if event.kind == "command_decide_proposal"]) == 1


@pytest.mark.parametrize("problem", ["digest", "graph", "policy", "input", "criteria", "process"])
def test_historical_proposal_decision_rechecks_current_owner_contract(setup, problem):
    store, supervisor, scope, authority, now = setup
    decision = retained_proposal(setup)
    now[0] = 1031
    recovered = supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    command(supervisor, recovered, "recover", {"retry_work_items": []})
    if problem == "digest":
        decision["sha256"] = "wrong"
    elif problem == "graph":
        plan(supervisor, recovered, [spec("new-owner-work")])
    with store._connection(write=True) as connection:
        if problem == "policy":
            policy = json.loads(connection.execute("SELECT policy_json FROM runs").fetchone()[0])
            policy["max_workers"] = 3
            connection.execute("UPDATE runs SET policy_json=?", (json.dumps(policy),))
        elif problem == "input":
            connection.execute("UPDATE request_inputs SET input_sha256='unrelated'")
        elif problem == "criteria":
            connection.execute("UPDATE coordinator_inputs SET criteria_json='[\"unrelated\"]'")
        elif problem == "process":
            planner = connection.execute("SELECT id FROM attempts").fetchone()[0]
            connection.execute("INSERT INTO process_observations VALUES(?,?,?,?,?,?,?,'unknown',NULL)",
                               (planner, authority.run_id, 1, "host", 123, 1.0, "token"))
    before = store.snapshot(scope, authority.run_id)
    with pytest.raises(Conflict):
        command(supervisor, recovered, "decide_proposal", decision)
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["work_items"] == before["work_items"]
    assert snapshot["coordinator_proposals"][0]["state"] == "pending"
