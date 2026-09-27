"""Recovery ownership and real host observations without model calls or replay."""

from dataclasses import replace
import multiprocessing
import os
import sys
import threading
import time
import uuid

import pytest
import psutil

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.models import Conflict, IdempotencyConflict, RevisionConflict, ScopeDenied, StaleAuthority
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.processes import ProcessObservations
from lumi.engine.swarming.recovery import SwarmRecovery, record_run_host


def send(supervisor, authority, kind, **payload):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision, authority.epoch, kind, payload), authority)


def decide(recovery, kind, **payload):
    return recovery.command(kind, payload, command_id=uuid.uuid4().hex,
                            expected_revision=recovery.inspect()["run"]["revision"])


@pytest.fixture
def fixture(tmp_path):
    now = [1000.0]
    store = SwarmStore(tmp_path / "swarm.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read"}),
                           allowed_providers=frozenset({"ollama"}), read_roots=(".",))
    authority = supervisor.create(scope, supervisor_id="original", objective="Fixture", request_limit=5, policy=policy)
    observations = ProcessObservations(store, host_id="fixture-host")
    recovery = SwarmRecovery(supervisor, scope, authority.run_id, process_observations=observations)
    yield store, supervisor, authority, observations, recovery, now
    recovery.close()


def participant(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    send(supervisor, authority, "plan", work_items=[{"id": "work", "objective": "Inspect fixture", "tools": ["file_read"], "criteria": ["proof"]}])
    assignment = send(supervisor, authority, "assign", work_item_id="work", worker_id="worker", requests=3,
                      model={"provider": "ollama", "model": "chosen"}).result
    send(supervisor, authority, "worker_started", attempt_id=assignment["attempt_id"], attempt_epoch=1)
    return AttemptContext(authority.scope, authority.run_id, assignment["attempt_id"], "worker", 1)


def pending_request(fixture, context):
    _, supervisor, authority, _, _, _ = fixture
    send(supervisor, authority, "reserve_request", attempt_id=context.attempt_id, attempt_epoch=1, request_id="request", purpose="main")
    send(supervisor, authority, "start_request", request_id="request", attempt_epoch=1)


def owned_process(fixture, context):
    _, _, authority, observations, _, _ = fixture
    process = ManagedWorkerProcess(command=[sys.executable, "-c", "import time; time.sleep(30)"], cancel_grace=.01)
    process._spawn()  # Fixture stops before init; no backend or tool can run.
    observations.started(authority, context, pid=process.pid, created_at=process.created_at, launch_token=process.launch_token)
    return process


def takeover(fixture):
    _, _, _, _, recovery, now = fixture
    now[0] = 1031
    return recovery.acquire(expected_epoch=1)


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "Fixture did not reach expected recovery state"


def test_takeover_is_explicit_scoped_and_retained_without_replay(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    with pytest.raises(Conflict, match="active supervisor"):
        recovery.acquire(expected_epoch=1)
    replacement = takeover(fixture)
    assert replacement == recovery.authority
    assert recovery.acquire(expected_epoch=1) == replacement
    assert replacement.supervisor_id != authority.supervisor_id
    assert replacement.epoch == 2
    with pytest.raises(Conflict, match="different takeover"):
        recovery.acquire(expected_epoch=2)
    with pytest.raises(StaleAuthority):
        send(supervisor, authority, "stop")
    with pytest.raises(ScopeDenied):
        SwarmRecovery(supervisor, replace(authority.scope, session_id="foreign"), authority.run_id, process_observations=observations)
    snapshot = recovery.inspect()
    assert snapshot["run"]["state"] == "recovery_required"
    assert len(snapshot["attempts"]) == len(snapshot["dispatches"]) == 1
    assert snapshot["attempts"][0]["id"] == context.attempt_id
    assert snapshot["attempts"][0]["state"] == "uncertain"
    recovery.close()
    assert not recovery.inspect()["recovery"]["owns_lease"]
    assert store.snapshot(authority.scope, authority.run_id)["run"]["state"] == "recovery_required"
    with pytest.raises(Conflict, match="closed"):
        decide(recovery, "stop")


def test_recovery_lease_renews_without_viewers_or_workers(tmp_path):
    store = SwarmStore(tmp_path / "clock.sqlite")
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset(), allowed_providers=frozenset({"ollama"}))
    authority = supervisor.create(scope, supervisor_id="original", objective="Lease fixture", request_limit=1, policy=policy, lease_seconds=.1)
    time.sleep(.15)
    with SwarmRecovery(supervisor, scope, authority.run_id, process_observations=ProcessObservations(store, host_id="fixture")) as recovery:
        recovery.acquire(expected_epoch=1, lease_seconds=.45)
        initial = recovery.inspect()["run"]["lease_until"]
        time.sleep(1.0)  # More than two whole lease periods without polling.
        snapshot = recovery.inspect()
        assert snapshot["run"]["lease_until"] > initial
        assert snapshot["recovery"]["owns_lease"]
        assert snapshot["recovery"]["lease_maintenance_alive"]
        assert snapshot["attempts"] == []


def test_stopped_owned_process_preserves_request_action_uncertainty_until_explicit_decisions(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    process = owned_process(fixture, context)
    try:
        pending_request(fixture, context)
        with store._connection(write=True) as connection:
            connection.execute("INSERT INTO action_receipts(id,attempt_id,epoch,request_id,call_id,tool_name,arguments_sha256,state) "
                               "VALUES('action',?,1,'request','call','file_read','digest','admitted')", (context.attempt_id,))
        takeover(fixture)
        observed = recovery.reconcile_process(context.attempt_id)
        assert observed["observation"] == "running"
        assert not observed["termination_recorded"]
        with pytest.raises(Conflict, match="unresolved"):
            decide(recovery, "recover", retry_work_items=["work"])
        process.close()  # OS cleanup; deliberately no original-runtime receipt.
        assert process.cleanup_confirmed
        observed = recovery.reconcile_process(context.attempt_id)
        assert observed["observation"] == "stopped" and observed["termination_recorded"]
        assert observed["outcome"] == "uncertain"
        snapshot = recovery.inspect()
        assert snapshot["remaining_requests"] == 2
        assert snapshot["model_requests"][0]["used"] is None
        assert snapshot["action_receipts"][0]["state"] == "uncertain"
        assert "launch_token" not in snapshot["process_observations"][0]
        with pytest.raises(ValueError, match="evidence"):
            decide(recovery, "reconcile_request", request_id="request", outcome="failed", used=1, evidence="")
        decide(recovery, "reconcile_request", request_id="request", outcome="failed", used=1,
               evidence="Retained provider receipt confirms the request was attempted and failed")
        with pytest.raises(Conflict, match="unresolved"):
            decide(recovery, "recover", retry_work_items=["work"])
        decide(recovery, "reconcile_action", action_id="action", outcome="failed", evidence="Retained tool output confirms failure before result delivery")
        assert decide(recovery, "recover", retry_work_items=["work"]).state == "running"
        snapshot = recovery.inspect()
        assert snapshot["work_items"][0]["state"] == "ready"
        assert snapshot["attempts"][0]["state"] == "failed"
        assert len(snapshot["attempts"]) == 1  # Recovery itself dispatches nothing.
        assert snapshot["remaining_requests"] == 4
        assert snapshot["submissions"] == []
    finally:
        process.close()


def test_missing_historical_thread_identity_remains_unknown_and_cannot_be_asserted_stopped(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    takeover(fixture)
    observed = recovery.reconcile_process(context.attempt_id)
    assert observed["observation"] == "unknown" and not observed["termination_recorded"]
    assert "durable owned-process identities" in observed["next_step"]
    with pytest.raises(Conflict, match="unresolved"):
        decide(recovery, "recover", retry_work_items=[])
    for kind, payload in [("worker_stopped", {"attempt_id": context.attempt_id, "outcome": "cancelled"}),
                          ("reconcile_process", {"pid": 123, "stopped": True}), ("stop", {"supervisor_id": "forged"})]:
        with pytest.raises(ValueError, match="Unsupported recovery"):
            recovery.command(kind, payload, command_id="forged", expected_revision=recovery.inspect()["run"]["revision"])
    with pytest.raises(ScopeDenied):
        recovery.inspect_process("foreign-attempt")
    assert recovery.inspect()["attempts"][0]["process_state"] == "unknown"


def test_explicit_stop_stays_sticky_after_process_and_request_observations(fixture):
    _, _, _, _, recovery, _ = fixture
    context = participant(fixture)
    process = owned_process(fixture, context)
    try:
        pending_request(fixture, context)
        takeover(fixture)
        decide(recovery, "stop")
        process.close()
        recovery.reconcile_process(context.attempt_id)
        with pytest.raises(Conflict, match="unresolved"):
            decide(recovery, "recover", retry_work_items=[])
        decide(recovery, "reconcile_request", request_id="request", outcome="failed", used=1, evidence="Observed provider request receipt")
        with pytest.raises(Conflict, match="stopped run"):
            decide(recovery, "recover", retry_work_items=["work"])
        assert decide(recovery, "recover", retry_work_items=[]).state == "cancelled"
    finally:
        process.close()


def test_uncertain_writer_finalization_is_retained_and_blocks_recovery(fixture, tmp_path):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    process = owned_process(fixture, context)
    retained = tmp_path / "retained-writer"
    retained.mkdir()
    (retained / "partial.txt").write_text("Unverified partial work")
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO writer_worktrees(id,run_id,attempt_id,epoch,repo_key,path,base_revision,state,manifest_json) "
                           "VALUES('writer',?,?,1,'fixture',?,'base','finalizing','{}')", (authority.run_id, context.attempt_id, str(retained)))
    try:
        takeover(fixture)
        process.close()
        recovery.reconcile_process(context.attempt_id)
        with pytest.raises(Conflict, match="unresolved"):
            decide(recovery, "recover", retry_work_items=["work"])
        assert recovery.inspect()["writer_worktrees"][0]["state"] == "uncertain"
        assert (retained / "partial.txt").read_text() == "Unverified partial work"
    finally:
        process.close()


def test_recovery_command_revision_and_full_semantics_are_preserved(fixture):
    _, _, _, _, recovery, _ = fixture
    takeover(fixture)
    revision = recovery.inspect()["run"]["revision"]
    first = recovery.command("stop", {}, command_id="stop", expected_revision=revision)
    assert recovery.command("stop", {}, command_id="stop", expected_revision=revision) == first
    with pytest.raises(RevisionConflict):
        recovery.command("stop", {}, command_id="different", expected_revision=revision)
    with pytest.raises(IdempotencyConflict):
        recovery.command("recover", {"retry_work_items": []}, command_id="stop", expected_revision=revision)


def test_ambiguous_takeover_reuses_captured_identity_without_advancing_epoch(fixture, monkeypatch):
    _, supervisor, _, _, recovery, now = fixture
    now[0] = 1031
    original = supervisor.acquire
    def ambiguous(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("fixture acknowledgement lost after commit")
    with monkeypatch.context() as patch:
        patch.setattr(supervisor, "acquire", ambiguous)
        with pytest.raises(OSError, match="acknowledgement"):
            recovery.acquire(expected_epoch=1)
    status = recovery.inspect()["recovery"]
    assert status["requested_epoch"] == 1 and status["acquired_epoch"] is None
    authority = recovery.acquire(expected_epoch=1)
    assert authority.epoch == 2
    assert recovery.inspect()["recovery"]["requested_epoch"] == 1
    assert recovery.inspect()["recovery"]["acquired_epoch"] == 2
    assert recovery.inspect()["recovery"]["owns_lease"]


def test_renewal_failure_closes_control_without_automatic_takeover(fixture, monkeypatch):
    _, supervisor, _, _, recovery, now = fixture
    now[0] = 1031
    authority = recovery.acquire(expected_epoch=1, lease_seconds=.3)
    original = supervisor.handle
    def fail_renew(command, owner):
        if command.kind == "renew":
            raise OSError("fixture durable storage unavailable")
        return original(command, owner)
    monkeypatch.setattr(supervisor, "handle", fail_renew)
    until(lambda: bool(recovery.inspect()["recovery"]["renewal_error"]))
    assert recovery.inspect()["run"]["epoch"] == authority.epoch
    with pytest.raises(Conflict, match="maintenance failed"):
        decide(recovery, "stop")


def test_foreign_host_cannot_reconcile_captured_process(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    process = owned_process(fixture, context)
    recovery.close()
    foreign = SwarmRecovery(supervisor, authority.scope, authority.run_id,
                            process_observations=ProcessObservations(store, host_id="foreign-host"))
    try:
        now[0] = 1031
        foreign.acquire(expected_epoch=1)
        process.close()
        assert foreign.inspect_process(context.attempt_id)["observation"] == "unknown"
        with pytest.raises(ScopeDenied, match="host"):
            foreign.reconcile_process(context.attempt_id)
        assert foreign.inspect()["attempts"][0]["process_state"] == "unknown"
    finally:
        process.close()
        foreign.close()


def test_run_host_records_actual_process_before_dispatch_and_replays(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    record = record_run_host(store, authority, process_observations=observations)
    assert record["pid"] == os.getpid()
    assert record["created_at"] == psutil.Process(os.getpid()).create_time()
    assert record_run_host(store, authority, process_observations=observations) == record
    context = participant(fixture)
    assert record_run_host(store, authority, process_observations=observations) == record
    with pytest.raises(Conflict, match="different captured"):
        record_run_host(store, authority, process_observations=ProcessObservations(store, host_id="other"))
    with pytest.raises(ScopeDenied):
        record_run_host(store, replace(authority, scope=replace(authority.scope, owner_id="foreign")), process_observations=observations)
    takeover(fixture)
    observed = recovery.reconcile_process(context.attempt_id)
    assert observed["source"] == "run_host" and observed["observation"] == "unknown"
    assert not observed["termination_recorded"]
    assert "live" in observed["reason"]
    assert len(recovery.inspect()["run_hosts"]) == 1


def test_missing_run_host_cannot_be_backfilled_after_dispatch(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    participant(fixture)
    with pytest.raises(Conflict, match="before participant dispatch"):
        record_run_host(store, authority, process_observations=observations)
    assert recovery.inspect()["run_hosts"] == []


def test_replacement_host_can_record_identity_before_explicit_recovery_without_resuming(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    context = participant(fixture)
    replacement = takeover(fixture)
    record = record_run_host(store, replacement, process_observations=observations)
    assert record["epoch"] == 2 and record["pid"] == os.getpid()
    snapshot = recovery.inspect()
    assert snapshot["run"]["state"] == "recovery_required"
    assert snapshot["attempts"][0]["id"] == context.attempt_id
    assert snapshot["attempts"][0]["process_state"] == "unknown"
    # A new epoch's host is not evidence about an old epoch's lost thread.
    assert recovery.reconcile_process(context.attempt_id)["observation"] == "unknown"
    with pytest.raises(Conflict, match="unresolved"):
        decide(recovery, "recover", retry_work_items=["work"])


def _thread_host_fixture(path, ready, coordinator_kind):
    """An actual separate app process records itself, then owns a live thread."""
    store = SwarmStore(path, clock=lambda: 1000)
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read", "grep"}), allowed_providers=frozenset({"ollama"}))
    authority = supervisor.create(scope, supervisor_id="original-app", objective="Thread fixture", request_limit=5,
                                  policy=policy, run_id="thread-host-run")
    record_run_host(store, authority, process_observations=ProcessObservations(store, host_id="fixture-host"))
    if coordinator_kind:
        assignment = send(supervisor, authority, "start_coordinator", worker_id="planner", requests=3,
                          model={"provider": "ollama", "model": "chosen"}, tools=["file_read", "grep"], read_roots=["."]).result
    else:
        send(supervisor, authority, "plan", work_items=[{"id": "work", "objective": "Read fixture", "tools": ["file_read", "grep"], "criteria": ["proof"]}])
        assignment = send(supervisor, authority, "assign", work_item_id="work", worker_id="reader", requests=3,
                          model={"provider": "ollama", "model": "chosen"}).result
    send(supervisor, authority, "worker_started", attempt_id=assignment["attempt_id"], attempt_epoch=1)
    threading.Thread(target=lambda: threading.Event().wait(60), daemon=True).start()
    ready.set()
    threading.Event().wait(60)


@pytest.fixture
def thread_host(tmp_path, request):
    coordinator_kind = getattr(request, "param", False)
    path = tmp_path / "thread-host.sqlite"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_thread_host_fixture, args=(str(path), ready, coordinator_kind))
    process.start()
    try:
        assert ready.wait(10), f"Fixture app failed to record its host (exit={process.exitcode})"
        store = SwarmStore(path, clock=lambda: 1031)
        supervisor = SwarmSupervisor(store)
        scope = Scope.personal("owner", "project", "session")
        observations = ProcessObservations(store, host_id="fixture-host")
        with SwarmRecovery(supervisor, scope, "thread-host-run", process_observations=observations) as recovery:
            recovery.acquire(expected_epoch=1)
            yield process, store, recovery
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


@pytest.mark.parametrize("thread_host", [False, True], indirect=True)
def test_actual_original_app_exit_proves_reader_or_coordinator_threads_ended(thread_host):
    process, store, recovery = thread_host
    snapshot = recovery.inspect()
    attempt = snapshot["attempts"][0]
    assert snapshot["run_hosts"][0]["pid"] == process.pid
    assert recovery.inspect_process(attempt["id"])["observation"] == "unknown"
    process.kill()
    process.join(5)
    assert not process.is_alive()
    observed = recovery.reconcile_process(attempt["id"])
    assert observed["source"] == "run_host"
    assert observed["observation"] == "stopped" and observed["termination_recorded"]
    assert observed["outcome"] == "failed"  # App exit never proves task success.
    retry = [] if attempt["kind"] == "coordinator" else ["work"]
    assert decide(recovery, "recover", retry_work_items=retry).state == "running"
    snapshot = recovery.inspect()
    assert len(snapshot["attempts"]) == 1 and snapshot["submissions"] == []


@pytest.mark.parametrize("action_state", ["uncertain", "completed"])
def test_thread_host_exit_never_proves_unobserved_grep_child_cleanup(thread_host, action_state):
    process, store, recovery = thread_host
    attempt = recovery.inspect()["attempts"][0]
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO model_requests VALUES('grep-request',?,1,'main','completed',1)", (attempt["id"],))
        connection.execute("INSERT INTO action_receipts(id,attempt_id,epoch,request_id,call_id,tool_name,arguments_sha256,state) "
                           "VALUES('grep-action',?,1,'grep-request','call','grep','hash',?)", (attempt["id"], action_state))
    process.kill()
    process.join(5)
    observed = recovery.reconcile_process(attempt["id"])
    if action_state == "uncertain":
        assert observed["observation"] == "unknown" and not observed["termination_recorded"]
        assert "search subprocess" in observed["reason"]
        with pytest.raises(Conflict, match="unresolved"):
            decide(recovery, "recover", retry_work_items=["work"])
    else:
        assert observed["observation"] == "stopped" and observed["termination_recorded"]
        assert recovery.inspect()["attempts"][0]["state"] == "failed"


def test_thread_host_observation_preserves_unknown_provider_request(thread_host):
    process, store, recovery = thread_host
    attempt = recovery.inspect()["attempts"][0]
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO model_requests VALUES('request',?,1,'main','uncertain',NULL)", (attempt["id"],))
    process.kill()
    process.join(5)
    assert recovery.reconcile_process(attempt["id"])["outcome"] == "uncertain"
    snapshot = recovery.inspect()
    assert snapshot["remaining_requests"] == 2
    assert snapshot["model_requests"][0]["used"] is None
    with pytest.raises(Conflict, match="unresolved"):
        decide(recovery, "recover", retry_work_items=["work"])


def test_managed_child_identity_always_takes_precedence_over_run_host(fixture):
    store, supervisor, authority, observations, recovery, now = fixture
    record_run_host(store, authority, process_observations=observations)
    context = participant(fixture)
    process = owned_process(fixture, context)
    try:
        takeover(fixture)
        observed = recovery.inspect_process(context.attempt_id)
        assert observed["source"] == "managed_process" and observed["pid"] == process.pid
        assert observed["observation"] == "running"
    finally:
        process.close()


def test_writer_without_managed_identity_cannot_use_app_exit_proof(thread_host):
    process, store, recovery = thread_host
    attempt = recovery.inspect()["attempts"][0]
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO writer_worktrees(id,run_id,attempt_id,epoch,repo_key,path,base_revision,state,manifest_json) "
                           "VALUES('writer',?,?,1,'fixture','unavailable','base','uncertain','{}')", (recovery.run_id, attempt["id"]))
    process.kill()
    process.join(5)
    observed = recovery.reconcile_process(attempt["id"])
    assert observed["observation"] == "unknown" and not observed["termination_recorded"]
    assert "managed-process" in observed["reason"]


def test_foreign_run_host_and_access_denied_never_prove_thread_exit(thread_host, monkeypatch):
    process, store, recovery = thread_host
    attempt = recovery.inspect()["attempts"][0]
    original = psutil.Process
    def denied(pid):
        if pid == process.pid:
            raise psutil.AccessDenied(pid)
        return original(pid)
    with monkeypatch.context() as patch:
        patch.setattr(psutil, "Process", denied)
        assert recovery.reconcile_process(attempt["id"])["observation"] == "unknown"
    with store._connection(write=True) as connection:
        connection.execute("UPDATE run_hosts SET host_id='foreign'")
    process.kill()
    process.join(5)
    assert recovery.reconcile_process(attempt["id"])["observation"] == "unknown"
    assert recovery.inspect()["attempts"][0]["process_state"] == "unknown"
