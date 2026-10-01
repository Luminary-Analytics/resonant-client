"""Behavioral proofs for the local swarming storage/protocol foundation."""

from __future__ import annotations

import multiprocessing
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from lumi.engine.swarming import Scope, SwarmStore
from lumi.engine.swarming.models import (
    AdmissionClosed,
    AllowanceExceeded,
    Conflict,
    IdempotencyConflict,
    SchemaVersionError,
    ScopeDenied,
    StaleAuthority,
)
from lumi.engine.swarming.store import SCHEMA_VERSION


@pytest.fixture
def setup(tmp_path):
    store = SwarmStore(tmp_path / "swarm" / "state.sqlite")
    scope = Scope.personal("owner", "project", "session")
    authority = store.create_run(scope, supervisor_id="supervisor", objective="Fixture", request_limit=8)
    for item in ("a", "b", "c"):
        store.add_work_item(authority, work_item_id=item, objective=f"Inspect {item}")
    return store, scope, authority


def claim(store, authority, item="a", *, requests=2):
    return store.claim(authority, work_item_id=item, worker_id=f"worker-{item}",
                       requests=requests, command_id=f"claim-{item}")


def _claim_process(path, authority, item, requests, barrier, results):
    """Independent process, connection and command competing for allocation."""
    store = SwarmStore(path)
    barrier.wait(timeout=15)
    try:
        result = store.claim(authority, work_item_id=item, worker_id=f"worker-{multiprocessing.current_process().pid}",
                             requests=requests, command_id=f"command-{multiprocessing.current_process().pid}")
        results.put(("claimed", result.context.attempt_id))
    except Conflict as error:
        results.put((type(error).__name__, str(error)))


def _race(store, authority, items, requests):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(items))
    results = context.Queue()
    processes = [context.Process(target=_claim_process,
                                 args=(str(store.path), authority, item, requests, barrier, results))
                 for item in items]
    try:
        for process in processes:
            process.start()
        observations = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        return observations
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()
        results.join_thread()


def test_commit_reopen_replay_and_request_allowance(setup):
    store, scope, authority = setup
    assigned = claim(store, authority)
    reopened = SwarmStore(store.path)
    assert claim(reopened, authority) == assigned
    snapshot = reopened.snapshot(scope, authority.run_id)
    assert snapshot["remaining_requests"] == 6
    assert len(snapshot["attempts"]) == len(snapshot["reservations"]) == len(snapshot["dispatches"]) == 1
    assert snapshot["attempts"][0]["state"] == "leased"
    assert snapshot["dispatches"][0]["state"] == "pending"
    assert [event.kind for event in reopened.events(scope, authority.run_id)].count("assignment_claimed") == 1
    with pytest.raises(IdempotencyConflict):
        claim(reopened, authority, requests=3)
    with pytest.raises(AllowanceExceeded):
        claim(reopened, authority, "b", requests=7)
    assert reopened.snapshot(scope, authority.run_id) == snapshot


def test_event_observation_times_survive_replay_and_backup_without_clock_rewriting(tmp_path):
    now = [1000.25]
    store = SwarmStore(tmp_path / "timed.sqlite", clock=lambda: now[0])
    scope = Scope.personal("owner", "project", "session")
    authority = store.create_run(scope, supervisor_id="supervisor", objective="Observe timestamps", request_limit=4)
    now[0] = 1002.5
    store.add_work_item(authority, work_item_id="a", objective="Inspect")
    now[0] = 1003.75
    original = claim(store, authority)
    events = store.events(scope, authority.run_id)
    assert [event.occurred_at for event in events] == [1000.25, 1002.5, 1003.75]
    assert all(event.run_state == "running" for event in events)
    now[0] = 2000
    assert claim(store, authority) == original
    assert store.events(scope, authority.run_id) == events
    backup = tmp_path / "observed.sqlite"
    SwarmStore.backup_database(store.path, backup)
    assert SwarmStore(backup, clock=lambda: 9000).events(scope, authority.run_id) == events


def test_two_processes_cannot_claim_same_work_item(setup):
    store, scope, authority = setup
    outcomes = _race(store, authority, ["a", "a"], 2)
    assert sorted(result[0] for result in outcomes) == ["Conflict", "claimed"]
    snapshot = store.snapshot(scope, authority.run_id)
    assert len(snapshot["attempts"]) == 1
    assert snapshot["remaining_requests"] == 6


def test_two_processes_cannot_allocate_same_remaining_allowance(setup):
    store, scope, authority = setup
    outcomes = _race(store, authority, ["a", "b"], 6)
    assert sorted(result[0] for result in outcomes) == ["AllowanceExceeded", "claimed"]
    snapshot = store.snapshot(scope, authority.run_id)
    assert len(snapshot["attempts"]) == 1
    assert snapshot["remaining_requests"] == 2


def test_durable_event_failure_rolls_back_entire_claim(setup):
    store, scope, authority = setup
    before = store.snapshot(scope, authority.run_id)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON events "
                           "WHEN NEW.kind='assignment_claimed' BEGIN SELECT RAISE(ABORT, 'fixture disk write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture disk write failure"):
        claim(store, authority)
    reopened = SwarmStore(store.path)
    assert reopened.snapshot(scope, authority.run_id) == before
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_ack")
    assert claim(reopened, authority).reserved_requests == 2


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_process_loss_at_commit_boundary_retains_correct_uncertainty(setup, crash_after_commit):
    store, scope, authority = setup
    # os._exit deliberately bypasses Python cleanup to exercise SQLite recovery.
    script = """
import json, os, sys
from lumi.engine.swarming import Scope, SwarmStore, RunAuthority
path, run_id, after = sys.argv[1:]
store = SwarmStore(path)
scope = Scope.personal('owner', 'project', 'session')
authority = RunAuthority(scope, run_id, 'supervisor', 1)
if after == 'False':
    store._event = lambda *args, **kwargs: os._exit(73)
store.claim(authority, work_item_id='a', worker_id='worker-a', requests=2, command_id='claim-a')
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-c", script, str(store.path), authority.run_id,
                             str(crash_after_commit)], timeout=20, capture_output=True, text=True)
    assert result.returncode == 73, result.stderr
    reopened = SwarmStore(store.path)
    snapshot = reopened.snapshot(scope, authority.run_id)
    assert len(snapshot["attempts"]) == int(crash_after_commit)
    assert snapshot["remaining_requests"] == (6 if crash_after_commit else 8)
    recovered = reopened.reconcile(authority, supervisor_id="new-supervisor")
    after = reopened.snapshot(scope, authority.run_id)
    assert after["remaining_requests"] == snapshot["remaining_requests"]
    assert after["run"]["state"] == "recovery_required"
    if crash_after_commit:
        assert after["attempts"][0]["state"] == "uncertain"
        assert after["dispatches"][0]["state"] == "uncertain"
        assert after["reservations"][0]["state"] == "uncertain"
        assert after["reservations"][0]["used"] is None
    with pytest.raises(AdmissionClosed):
        claim(reopened, recovered, "b")


def test_old_supervisor_worker_and_duplicate_commands_are_fenced(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="finding", body="Inspect this", command_id="message")
    store.acknowledge(second.context, message.id, stage="runtime")
    wrong_supervisor = replace(authority, supervisor_id="other-supervisor")
    with pytest.raises(StaleAuthority):
        claim(store, wrong_supervisor)
    recovered = store.reconcile(authority, supervisor_id="new-supervisor")
    assert recovered.epoch == 2
    with pytest.raises(StaleAuthority):
        claim(store, authority)
    with pytest.raises(StaleAuthority):
        store.stop(authority, command_id="old-stop")
    with pytest.raises(StaleAuthority):
        store.receive(second.context)
    with pytest.raises(StaleAuthority):
        store.acknowledge(second.context, message.id, stage="runtime")
    with pytest.raises(StaleAuthority):
        store.finish_attempt(authority, first.context, outcome="submitted", used_requests=0, command_id="finish")
    with pytest.raises(ScopeDenied):
        store.finish_attempt(recovered, first.context, outcome="submitted", used_requests=0, command_id="finish")
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 4


def test_creation_retry_cannot_renew_recovered_authority(setup):
    store, scope, authority = setup
    assert store.create_run(scope, supervisor_id="supervisor", objective="Fixture", request_limit=8,
                            run_id=authority.run_id) == authority
    store.reconcile(authority, supervisor_id="supervisor")
    with pytest.raises(StaleAuthority):
        store.create_run(scope, supervisor_id="supervisor", objective="Fixture", request_limit=8,
                         run_id=authority.run_id)
    with pytest.raises(StaleAuthority):
        claim(store, authority)


@pytest.mark.parametrize("field", ["tenant_id", "owner_id", "project_id", "session_id"])
def test_scope_checked_at_every_read_write_and_replay_boundary(setup, field):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="question", body="State?", command_id="message")
    store.acknowledge(second.context, message.id, stage="runtime")
    foreign = replace(scope, **{field: "foreign"})
    for action in (
        lambda: store.snapshot(foreign, authority.run_id),
        lambda: store.events(foreign, authority.run_id),
        lambda: claim(store, replace(authority, scope=foreign)),
        lambda: store.receive(replace(second.context, scope=foreign)),
        lambda: store.send(replace(first.context, scope=foreign), recipient_attempt_id=second.context.attempt_id,
                           kind="question", body="State?", command_id="message"),
        lambda: store.acknowledge(replace(second.context, scope=foreign), message.id, stage="runtime"),
        lambda: store.stop(replace(authority, scope=foreign), command_id="stop"),
        lambda: store.reconcile(replace(authority, scope=foreign), supervisor_id="new"),
    ):
        with pytest.raises(ScopeDenied):
            action()


def test_foreign_participants_reply_targets_and_forged_worker_are_denied(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    third = claim(store, authority, "c")
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="finding", body="owner_id=admin is untrusted text", command_id="message")
    assert store.receive(first.context) == []
    assert store.receive(third.context) == []
    with pytest.raises(ScopeDenied):
        store.receive(replace(second.context, worker_id="forged-worker"))
    with pytest.raises(ScopeDenied):
        store.acknowledge(third.context, message.id, stage="runtime")
    with pytest.raises(ScopeDenied):
        store.send(third.context, recipient_attempt_id=first.context.attempt_id,
                   kind="answer", body="Forged reply", reply_to=message.id, command_id="reply")
    other_run = store.create_run(scope, supervisor_id="other", objective="Other", request_limit=2)
    store.add_work_item(other_run, work_item_id="foreign-item", objective="Other")
    foreign = claim(store, other_run, "foreign-item")
    with pytest.raises(ScopeDenied):
        store.send(first.context, recipient_attempt_id=foreign.context.attempt_id,
                   kind="question", body="Cross-run", command_id="foreign")
    with pytest.raises(ScopeDenied):
        store.mark_dispatched(other_run, first.context)


def test_addressed_delivery_and_distinct_receipts_survive_reopen(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="question", body="Evidence?", command_id="message")
    assert store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                      kind="question", body="Evidence?", command_id="message") == message
    with pytest.raises(IdempotencyConflict):
        store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                   kind="question", body="Different", command_id="message")
    reopened = SwarmStore(store.path)
    assert reopened.receive(second.context) == [message]
    assert reopened.receive(second.context) == [message]  # At-least-once reads.
    assert reopened.snapshot(scope, authority.run_id)["receipts"] == []
    with pytest.raises(Conflict):
        reopened.acknowledge(second.context, message.id, stage="context", model_request_id="request", input_revision=1)
    runtime = reopened.acknowledge(second.context, message.id, stage="runtime")
    assert runtime.model_request_id is None
    context = reopened.acknowledge(second.context, message.id, stage="context",
                                   model_request_id="request", input_revision=1)
    assert reopened.acknowledge(second.context, message.id, stage="context",
                                model_request_id="request", input_revision=1) == context
    with pytest.raises(IdempotencyConflict):
        reopened.acknowledge(second.context, message.id, stage="context", model_request_id="other", input_revision=1)
    reply = reopened.send(second.context, recipient_attempt_id=first.context.attempt_id,
                          kind="answer", body="See observation", reply_to=message.id, command_id="reply")
    assert reply.sequence > message.sequence
    assert reopened.receive(first.context) == [reply]
    assert reopened.receive(second.context, after=message.sequence) == []
    assert len(SwarmStore(store.path).snapshot(scope, authority.run_id)["receipts"]) == 2


def test_terminal_worker_cannot_read_or_acknowledge_new_context(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="finding", body="Partial observation", command_id="message")
    runtime_receipt = store.acknowledge(second.context, message.id, stage="runtime")
    store.finish_attempt(authority, second.context, outcome="cancelled", used_requests=0, command_id="finish")
    with pytest.raises(Conflict, match="no longer active"):
        store.receive(second.context)
    with pytest.raises(Conflict, match="no longer active"):
        store.acknowledge(second.context, message.id, stage="context", model_request_id="late", input_revision=1)
    # Reading a previously committed receipt creates no new input assignment.
    assert store.acknowledge(second.context, message.id, stage="runtime") == runtime_receipt
    assert store.snapshot(scope, authority.run_id)["messages"][0]["body"] == "Partial observation"


def test_message_backpressure_does_not_block_stop(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    for index in range(100):
        store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                   kind="finding", body=f"Finding {index}", command_id=f"message-{index}")
    with pytest.raises(Conflict, match="100 messages"):
        store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                   kind="finding", body="Over capacity", command_id="extra")
    with pytest.raises(ValueError, match="8 KiB"):
        store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                   kind="finding", body="x" * 8193, command_id="large")
    store.stop(authority, command_id="stop")
    assert store.snapshot(scope, authority.run_id)["run"]["state"] == "stopping"


def test_stop_closes_new_admission_but_is_not_termination_or_rollback(setup):
    store, scope, authority = setup
    first = claim(store, authority)
    second = claim(store, authority, "b")
    store.mark_dispatched(authority, first.context)
    message = store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                         kind="finding", body="Partial observation", command_id="message")
    store.stop(authority, command_id="stop")
    store.stop(authority, command_id="stop")
    for action in (
        lambda: claim(store, authority, "c"),
        lambda: store.mark_dispatched(authority, second.context),
        lambda: store.receive(second.context),
        lambda: store.acknowledge(second.context, message.id, stage="runtime"),
        lambda: store.send(first.context, recipient_attempt_id=second.context.attempt_id,
                           kind="finding", body="New", command_id="new"),
    ):
        with pytest.raises(AdmissionClosed):
            action()
    assert store.snapshot(scope, authority.run_id)["remaining_requests"] == 4
    store.finish_attempt(authority, first.context, outcome="submitted", used_requests=1, command_id="finished-a")
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["run"]["state"] == "stopping"
    assert snapshot["attempts"][0]["state"] == "submitted"
    assert snapshot["remaining_requests"] == 5
    assert snapshot["run"]["worker_limit"] == 2
    store.finish_attempt(authority, second.context, outcome="cancelled", used_requests=0, command_id="finished-b")
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["run"]["state"] == "cancelled"
    assert [item["state"] for item in snapshot["work_items"]] == ["submitted", "cancelled", "cancelled"]
    assert snapshot["messages"][0]["body"] == "Partial observation"
    assert snapshot["remaining_requests"] == 7


@pytest.mark.parametrize("outcome", ["submitted", "failed", "cancelled"])
def test_terminal_result_is_immutable_and_never_implies_run_completion(setup, outcome):
    store, scope, authority = setup
    assigned = claim(store, authority)
    store.finish_attempt(authority, assigned.context, outcome=outcome, used_requests=1, command_id="finish")
    store.finish_attempt(authority, assigned.context, outcome=outcome, used_requests=1, command_id="finish")
    with pytest.raises(IdempotencyConflict):
        store.finish_attempt(authority, assigned.context, outcome=outcome, used_requests=0, command_id="finish")
    with pytest.raises(Conflict):
        store.finish_attempt(authority, assigned.context, outcome=outcome, used_requests=0, command_id="other")
    snapshot = store.snapshot(scope, authority.run_id)
    assert snapshot["run"]["state"] == "running"
    assert snapshot["attempts"][0]["state"] == outcome
    assert snapshot["reservations"][0]["used"] == 1


def test_monotonic_events_are_per_run_and_replayable_with_snapshot_cursor(setup):
    store, scope, authority = setup
    snapshot = store.snapshot(scope, authority.run_id)
    assigned = claim(store, authority)
    store.mark_dispatched(authority, assigned.context)
    store.mark_dispatched(authority, assigned.context)
    events = store.events(scope, authority.run_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.kind for event in store.events(scope, authority.run_id, after=snapshot["run"]["event_sequence"])] == [
        "assignment_claimed", "worker_started",
    ]
    other = store.create_run(scope, supervisor_id="other", objective="Other", request_limit=0)
    assert store.events(scope, other.run_id)[0].sequence == 1
    assert len(store.events(scope, authority.run_id, limit=2)) == 2


def test_read_only_inspection_backup_and_version_rejection(setup, tmp_path):
    store, scope, authority = setup
    claim(store, authority)
    assert SwarmStore.inspect_database(store.path) == {
        "schema_version": SCHEMA_VERSION, "application_id": 0x53574152,
        "journal_mode": "delete", "integrity": ["ok"],
    }
    with store._connection() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    backup = SwarmStore.backup_database(store.path, tmp_path / "before-upgrade.sqlite")
    assert SwarmStore(backup, read_only=True).snapshot(scope, authority.run_id) == store.snapshot(scope, authority.run_id)
    with pytest.raises(FileExistsError):
        SwarmStore.backup_database(store.path, backup)
    with pytest.raises(PermissionError):
        SwarmStore(backup, read_only=True).stop(authority, command_id="stop")
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    before = store.path.read_bytes()
    for read_only in (False, True):
        with pytest.raises(SchemaVersionError):
            SwarmStore(store.path, read_only=read_only)
    with pytest.raises(SchemaVersionError):
        store.snapshot(scope, authority.run_id)
    assert SwarmStore.inspect_database(store.path)["schema_version"] == SCHEMA_VERSION + 1
    unknown_backup = SwarmStore.backup_database(store.path, tmp_path / "unknown-schema.sqlite")
    assert SwarmStore.inspect_database(unknown_backup)["schema_version"] == SCHEMA_VERSION + 1
    assert store.path.read_bytes() == before


def test_unrelated_database_is_never_reinitialized(tmp_path):
    path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE precious(value TEXT)")
        connection.execute("INSERT INTO precious VALUES('Keep me')")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        SwarmStore(path)
    assert path.read_bytes() == before


def test_administrative_inspection_and_backup_release_windows_file_handles(tmp_path):
    source = tmp_path / "source.sqlite"
    SwarmStore(source)
    SwarmStore.inspect_database(source)
    renamed = source.rename(tmp_path / "renamed.sqlite")
    backup = SwarmStore.backup_database(renamed, tmp_path / "backup.sqlite")
    renamed.unlink()
    backup.rename(tmp_path / "moved-backup.sqlite").unlink()
    assert not list(tmp_path.iterdir())


def test_empty_scope_invalid_counts_and_unsupported_protocol_are_rejected(setup):
    store, scope, authority = setup
    with pytest.raises(ValueError):
        Scope("", "owner", "project", "session")
    with pytest.raises(ValueError):
        claim(store, authority, requests=True)
    with pytest.raises(ValueError):
        store.events(scope, authority.run_id, after=-1)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE runs SET protocol_version=999 WHERE id=?", (authority.run_id,))
    with pytest.raises(SchemaVersionError):
        store.snapshot(scope, authority.run_id)


# Frozen schema-1 fixture: deliberately independent of the current schema
# constructor, so migration checks do not merely recreate today's tables.
_V1_FIXTURE_SQL = """
PRAGMA application_id=1398227282;
PRAGMA user_version=1;
CREATE TABLE runs(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 project_id TEXT NOT NULL, session_id TEXT NOT NULL, supervisor_id TEXT NOT NULL,
 epoch INTEGER NOT NULL, protocol_version INTEGER NOT NULL, objective TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','stopping','cancelled','recovery_required')),
 request_limit INTEGER NOT NULL, event_sequence INTEGER NOT NULL DEFAULT 0);
CREATE TABLE work_items(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 objective TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(run_id,id));
CREATE TABLE attempts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 work_item_id TEXT NOT NULL, worker_id TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL,
 FOREIGN KEY(run_id,work_item_id) REFERENCES work_items(run_id,id), UNIQUE(run_id,id));
CREATE TABLE reservations(id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 amount INTEGER NOT NULL, used INTEGER, state TEXT NOT NULL);
CREATE TABLE dispatches(id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), state TEXT NOT NULL);
CREATE TABLE events(run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
 epoch INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE commands(run_id TEXT NOT NULL REFERENCES runs(id), actor TEXT NOT NULL, key TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id,actor,key));
CREATE TABLE messages(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
 sender_attempt_id TEXT NOT NULL, recipient_attempt_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 kind TEXT NOT NULL, body TEXT NOT NULL, reply_to TEXT,
 FOREIGN KEY(run_id,sender_attempt_id) REFERENCES attempts(run_id,id),
 FOREIGN KEY(run_id,recipient_attempt_id) REFERENCES attempts(run_id,id), FOREIGN KEY(reply_to) REFERENCES messages(id));
CREATE TABLE receipts(message_id TEXT NOT NULL REFERENCES messages(id), recipient_attempt_id TEXT NOT NULL REFERENCES attempts(id),
 epoch INTEGER NOT NULL, stage TEXT NOT NULL, model_request_id TEXT, input_revision INTEGER, PRIMARY KEY(message_id,stage));
INSERT INTO runs VALUES('run','personal:owner','owner','project','session','supervisor',1,1,'Retain history','recovery_required',8,3);
INSERT INTO work_items VALUES('a','run','Investigate','uncertain');
INSERT INTO attempts VALUES('attempt','run','a','worker',1,'uncertain');
INSERT INTO reservations VALUES('reservation','attempt',3,NULL,'uncertain');
INSERT INTO dispatches VALUES('dispatch','attempt','uncertain');
INSERT INTO events VALUES('run',1,1,'assignment_claimed','{}');
INSERT INTO events VALUES('run',2,1,'message_accepted','{}');
INSERT INTO events VALUES('run',3,1,'message_runtime','{}');
INSERT INTO commands VALUES('run','supervisor:supervisor','key','{"kind":"old"}','{}');
INSERT INTO messages VALUES('message','run',2,'attempt','attempt',1,'finding','Retained evidence',NULL);
INSERT INTO receipts VALUES('message','attempt',1,'runtime',NULL,NULL);
"""


def test_v1_upgrade_backs_up_before_migration_and_preserves_history(tmp_path):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(_V1_FIXTURE_SQL)
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        SwarmStore(path, read_only=True)
    assert not list(tmp_path.glob("*.backup"))
    store = SwarmStore(path)
    backups = list(tmp_path.glob("*.backup"))
    assert len(backups) == 1
    assert SwarmStore.inspect_database(backups[0])["schema_version"] == 1
    # Backup is a coherent old-schema database with all history and uncertainty.
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT body FROM messages").fetchone()[0] == "Retained evidence"
        assert connection.execute("SELECT state,used FROM reservations").fetchone() == ("uncertain", None)
    scope = Scope.personal("owner", "project", "session")
    snapshot = store.snapshot(scope, "run")
    assert snapshot["remaining_requests"] == 5
    assert snapshot["attempts"][0]["process_state"] == "unknown"
    assert snapshot["attempts"][0]["kind"] == "worker"
    assert snapshot["attempts"][0]["pause_requested"] == 0
    assert snapshot["attempts"][0]["cancel_requested"] == 0
    assert snapshot["owner_directives"] == snapshot["owner_directive_receipts"] == []
    assert snapshot["messages"][0]["body"] == "Retained evidence"
    assert snapshot["receipts"][0]["stage"] == "runtime"
    assert [event.sequence for event in store.events(scope, "run")] == [1, 2, 3]
    assert all(event.occurred_at is None for event in store.events(scope, "run"))
    assert all(event.run_state is None for event in store.events(scope, "run"))
    with store._connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT payload FROM commands").fetchone()[0] == '{"kind":"old"}'
    assert SwarmStore.inspect_database(path)["schema_version"] == SCHEMA_VERSION
    assert path.read_bytes() != before
    SwarmStore(path)
    assert len(list(tmp_path.glob("*.backup"))) == 1


def test_failed_v1_upgrade_rolls_back_and_keeps_recovery_backup(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(_V1_FIXTURE_SQL)
    migrate = SwarmStore._migrate_v1

    def fail_after_schema_change(connection):
        migrate(connection)
        raise sqlite3.OperationalError("fixture migration interruption")

    monkeypatch.setattr(SwarmStore, "_migrate_v1", staticmethod(fail_after_schema_change))
    with pytest.raises(sqlite3.OperationalError, match="migration interruption"):
        SwarmStore(path)
    assert SwarmStore.inspect_database(path)["schema_version"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT body FROM messages").fetchone()[0] == "Retained evidence"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list(tmp_path.glob("*.backup"))
    assert len(backups) == 1
    assert SwarmStore.inspect_database(backups[0])["schema_version"] == 1
