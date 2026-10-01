"""Durable managed-client boundaries without network, models, or user sessions."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from lumi.engine.swarming.managed_journal import JournalConflict, ManagedBinding, ManagedJournal


def uid():
    return str(uuid4())


@pytest.fixture
def setup(tmp_path):
    binding = ManagedBinding("https://governance.example", "a" * 64, uid(), uid(), uid(), 1,
                             "f" * 64, "local-project", "native_session_opaque", "native_run_opaque", 3)
    now = [1800000000.0]
    journal = ManagedJournal(tmp_path / "managed.sqlite", binding, clock=lambda: now[0])
    policy = {"policy_version": 1, "allowed_tools": ["file_read"]}
    lease = {"tenant_id": binding.tenant_id, "project_id": binding.project_id, "host_id": binding.host_id,
             "host_generation": 1, "owner_id": binding.owner_id, "lease_id": uid(), "protocol_version": 1, "runner_protocol": 2,
             "policy_revision": 2, "policy": policy,
             "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
             "expires_at": datetime.fromtimestamp(now[0] + 60, tz=timezone.utc).isoformat(), "offline_request_allowance": 0}
    return journal, lease, now


def semantics():
    return {"attempt_id": "attempt", "attempt_epoch": 3, "purpose": "primary", "model": {"provider": "sonn", "model": "explicit"},
            "input_sha256": "b" * 64, "policy_revision": 2}


def ensure_worker(journal, lease):
    if not journal.inspect()["remote_binding_id"]:
        journal.bind_remote(uid())
    worker = journal.worker("attempt", 3)
    if worker["phase"] == "prepared":
        assert journal.begin_worker("attempt", 3, lease)
        journal.worker_receipt("attempt", 3, lease, {"worker_id": worker["remote_worker_id"], "state": "held", "dispatch_permitted": True})
        assert journal.claim_worker("attempt", 3)
    return worker["remote_worker_id"]


def bind_request(journal, lease, local_id, remote):
    worker_id = ensure_worker(journal, lease)
    journal.bind_request_receipt(local_id, worker_id, {"request_id": remote, "worker_id": worker_id, "purpose": "primary", "bound": True})


def reserve(journal, lease, local_id="native-request"):
    row = journal.request(local_id, semantics())
    assert journal.mark_reserve_pending(local_id, lease)
    journal.reserve_receipt(local_id, lease, {"request_id": row["remote_request_id"], "lease_id": lease["lease_id"], "units": 1, "state": "reserved"})
    bind_request(journal, lease, local_id, row["remote_request_id"])
    return row["remote_request_id"]


def permit(journal, lease, local_id="native-request"):
    remote = reserve(journal, lease, local_id)
    assert journal.begin_start(local_id)
    journal.start_receipt(local_id, {"request_id": remote, "state": "started", "dispatch_permitted": True})
    return remote


def control(journal, lease):
    journal.bind_remote(uid())
    value = {"control_id": uid(), "operation": "stop", "expected_epoch": 3, "expected_local_revision": 8,
             "policy_revision": 2, "expires_at": lease["expires_at"]}
    row = journal.prepare_control(value)
    assert journal.begin_control_receive(value["control_id"])
    journal.control_receipt(value["control_id"], {**value, "dispatch_permitted": True})
    return value, row


def projection(epoch=3):
    return {"version": 1, "epoch": epoch, "local_revision": 8, "state": "running", "alert": "none",
            "counts": dict.fromkeys(("workers_active", "workers_pending", "requests_known", "requests_held", "requests_unknown", "checks_passed", "checks_failed"), 0)}


def test_request_mapping_and_full_semantics_survive_reopen_without_permit(setup):
    journal, lease, _ = setup
    remote = permit(journal, lease)
    reopened = ManagedJournal(journal.path, journal.binding)
    assert reopened.request("native-request", semantics())["remote_request_id"] == remote
    assert not reopened.claim_dispatch("native-request")
    assert not reopened.begin_start("native-request")
    with pytest.raises(JournalConflict, match="different semantics"):
        reopened.request("native-request", {**semantics(), "purpose": "compression"})
    with pytest.raises(JournalConflict, match="different captured binding"):
        ManagedJournal(journal.path, replace(journal.binding, session_id="foreign"))


def fence_receipt(intent):
    return {"kind": intent["resource_kind"], "resource_id": intent["resource_id"], "lease_id": intent["lease_id"],
            "state": "fenced_absent", "source": "server_non_admission_fence", "dispatch_permitted": False, "fence_id": uid()}


def test_stable_session_identity_spans_runs_epochs_and_hosts_without_rewriting_history(setup, tmp_path):
    journal, lease, _ = setup
    original = journal.registration(lease)["session_id"]
    replacement = replace(journal.binding, run_id="another-run", epoch=4, host_id=uid(), certificate_sha256="c" * 64)
    next_run = ManagedJournal(tmp_path / "other.sqlite", replacement)
    with next_run._connection() as connection:
        assert connection.execute("SELECT wire_session_id FROM binding").fetchone()[0] == original
    assert journal.sharing_eligible and next_run.sharing_eligible
    old_random = uid()
    with journal._connection(write=True) as connection:
        connection.execute("UPDATE binding SET wire_session_id=?", (old_random,))
    reopened = ManagedJournal(journal.path, journal.binding)
    assert not reopened.sharing_eligible
    with reopened._connection() as connection:
        assert connection.execute("SELECT wire_session_id FROM binding").fetchone()[0] == old_random


@pytest.mark.parametrize("kind", ["request", "worker", "action"])
def test_absence_fence_retains_distinct_receipt_and_closes_unclaimed_admission(setup, kind):
    journal, lease, _ = setup
    if kind == "request":
        journal.request("request", semantics())
        journal.mark_reserve_pending("request", lease)
        key = "request"
    elif kind == "worker":
        journal.bind_remote(uid())
        journal.worker("attempt", 3)
        journal.begin_worker("attempt", 3, lease)
        key = journal._worker_key("attempt", 3)
    else:
        remote = permit(journal, lease)
        journal.claim_dispatch("native-request")
        journal.observe("native-request", "completed")
        journal.action("action", {"worker_id": ensure_worker(journal, lease), "request_id": remote,
                                   "tool_name": "file_read", "arguments_sha256": "a" * 64})
        journal.begin_action("action", lease)
        journal.observe_worker("attempt", 3, "stopped")
        for item in journal.pending():
            journal.acknowledge(item["id"], {**item["payload"], "state": item["payload"]["outcome"]})
        key = "action"
    with pytest.raises(JournalConflict, match="old unclaimed"):
        journal.absence_intent(kind, key)
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    intent = reopened.absence_intent(kind, key)
    receipt = fence_receipt(intent)
    with pytest.raises(JournalConflict):
        reopened.record_absence(kind, key, {**receipt, "lease_id": uid()})
    with pytest.raises(JournalConflict):
        reopened.record_absence(kind, key, {**receipt, "state": "present"})
    reopened.record_absence(kind, key, receipt)
    reopened.record_absence(kind, key, receipt)
    assert reopened.pending() == []
    with reopened._connection() as connection:
        evidence = connection.execute("SELECT * FROM outbox WHERE kind='non_admission_fence'").fetchone()
        old = connection.execute("SELECT * FROM outbox WHERE kind<>'non_admission_fence' AND receipt IS NULL").fetchone()
        assert json.loads(evidence["receipt"]) == receipt
        assert old["receipt"] is None and old["superseded_by"] == evidence["id"]
    with pytest.raises(JournalConflict, match="receipt changed"):
        reopened.record_absence(kind, key, {**receipt, "fence_id": uid()})
    if kind == "request":
        assert not journal.begin_start(key)
    elif kind == "worker":
        assert not journal.claim_worker("attempt", 3)
    else:
        assert not journal.claim_action(key)


def test_absence_cannot_refund_original_remote_start_or_claim(setup):
    journal, lease, _ = setup
    permit(journal, lease)
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    with pytest.raises(JournalConflict, match="old unclaimed"):
        reopened.absence_intent("request", "native-request")
    with pytest.raises(JournalConflict, match="old unclaimed"):
        reopened.absence_intent("worker", journal._worker_key("attempt", 3))


def test_dispatch_is_single_use_across_concurrent_independent_connections(setup):
    journal, lease, _ = setup
    permit(journal, lease)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: journal.claim_dispatch("native-request"), range(24)))
    assert results.count(True) == 1
    assert journal.inspect()["requests"][0]["phase"] == "invoked"


@pytest.mark.parametrize("phase", ["reserve_pending", "remote_reserved", "start_pending", "permitted", "invoked"])
def test_restart_retains_uncertainty_and_fences_old_object(setup, phase):
    journal, lease, _ = setup
    row = journal.request("request", semantics())
    journal.mark_reserve_pending("request", lease)
    if phase != "reserve_pending":
        journal.reserve_receipt("request", lease, {"request_id": row["remote_request_id"], "lease_id": lease["lease_id"], "units": 1, "state": "reserved"})
    if phase in {"start_pending", "permitted", "invoked"}:
        bind_request(journal, lease, "request", row["remote_request_id"])
        journal.begin_start("request")
    if phase in {"permitted", "invoked"}:
        journal.start_receipt("request", {"request_id": row["remote_request_id"], "state": "started", "dispatch_permitted": True})
    if phase == "invoked":
        assert journal.claim_dispatch("request")
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    assert not journal.claim_dispatch("request")
    assert reopened.inspect()["requests"][0]["outcome"] == "uncertain"
    assert reopened.pending()[0]["payload"] == {"request_id": row["remote_request_id"], "outcome": "uncertain"}
    with pytest.raises(JournalConflict, match="refunded"):
        reopened.observe("request", "never_started")
    reopened.fence_restart()
    assert len([row for row in reopened.pending() if row["kind"] == "settle"]) == 1


def test_first_start_reply_lost_or_replayed_never_invokes(setup):
    journal, lease, _ = setup
    remote = reserve(journal, lease)
    assert journal.begin_start("native-request")
    assert not journal.begin_start("native-request")
    journal.start_receipt("native-request", {"request_id": remote, "state": "started", "dispatch_permitted": False})
    assert not journal.claim_dispatch("native-request")
    assert journal.pending()[0]["payload"]["outcome"] == "uncertain"
    with pytest.raises(JournalConflict):
        journal.start_receipt("native-request", {"request_id": remote, "state": "started", "dispatch_permitted": True})


def test_unstarted_and_definitely_denied_have_no_fabricated_remote_effect(setup):
    journal, lease, _ = setup
    journal.request("pure-local", semantics())
    journal.observe("pure-local", "never_started")
    journal.request("denied", semantics())
    assert journal.mark_reserve_pending("denied", lease)
    journal.reserve_rejected("denied")
    assert not journal.mark_reserve_pending("denied", lease)
    assert not journal.pending()
    assert {row["outcome"] for row in journal.inspect()["requests"]} == {"never_started"}


def test_lease_expiry_foreign_generation_and_protocol_fail_closed(setup):
    journal, lease, now = setup
    row = journal.request("request", semantics())
    journal.mark_reserve_pending("request", lease)
    receipt = {"request_id": row["remote_request_id"], "lease_id": lease["lease_id"], "units": 1, "state": "reserved"}
    for bad in ({**lease, "host_generation": 2}, {**lease, "tenant_id": uid()}, {**lease, "runner_protocol": 1},
                {**lease, "offline_request_allowance": 2}, {**lease, "policy_sha256": "e" * 64}):
        with pytest.raises(JournalConflict):
            journal.reserve_receipt("request", bad, receipt)
    journal.reserve_receipt("request", lease, receipt)
    bind_request(journal, lease, "request", row["remote_request_id"])
    journal.begin_start("request")
    journal.start_receipt("request", {"request_id": row["remote_request_id"], "state": "started", "dispatch_permitted": True})
    now[0] += 60
    with pytest.raises(JournalConflict, match="expired"):
        journal.claim_dispatch("request")
    assert journal.inspect()["requests"][0]["phase"] == "permitted"


def test_settlement_and_outbox_are_atomic_on_persistence_failure(setup):
    journal, lease, _ = setup
    permit(journal, lease)
    journal.claim_dispatch("native-request")
    with sqlite3.connect(journal.path) as connection:
        connection.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture"):
        journal.observe("native-request", "completed")
    assert journal.inspect()["requests"][0]["phase"] == "invoked"
    assert not journal.pending()
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DROP TRIGGER reject_outbox")
    journal.observe("native-request", "completed")
    journal.observe("native-request", "completed")
    assert len(journal.pending()) == 1
    with pytest.raises(JournalConflict, match="immutable"):
        journal.observe("native-request", "uncertain")


def test_control_exact_authority_and_claim_is_not_process_stop_proof(setup):
    journal, lease, _ = setup
    value, row = control(journal, lease)
    with pytest.raises(JournalConflict, match="different semantics"):
        journal.prepare_control({**value, "expected_local_revision": 9})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: journal.claim_control(value["control_id"]), range(12)))
    claims = [result for result in results if result]
    assert len(claims) == 1
    assert claims[0]["command_id"] == row["local_command_id"]
    assert claims[0]["expected_local_revision"] == 8
    journal.observe_control(value["control_id"], "applied", processes_stopped=False)
    pending = journal.pending()[0]
    assert pending["payload"]["processes_stopped"] is False
    with pytest.raises(JournalConflict, match="immutable"):
        journal.observe_control(value["control_id"], "applied", processes_stopped=True)


def test_control_restart_fences_old_permit_retains_uncertain_receipt(setup):
    journal, lease, _ = setup
    value, _ = control(journal, lease)
    reopened = ManagedJournal(journal.path, journal.binding)
    assert reopened.claim_control(value["control_id"]) is None
    reopened.fence_restart()
    assert journal.claim_control(value["control_id"]) is None
    assert reopened.pending()[0]["payload"]["outcome"] == "uncertain"


def test_reporting_sequence_replay_metadata_allowlist_and_independent_ack(setup):
    journal, lease, _ = setup
    journal.bind_remote(uid())
    journal.request("lost-reserve", semantics())
    journal.mark_reserve_pending("lost-reserve", lease)
    journal.observe("lost-reserve", "uncertain")
    first = journal.enqueue_report("one", projection())
    assert journal.enqueue_report("one", projection()) == first
    second = journal.enqueue_report("two", {**projection(), "local_revision": 9})
    assert second != first
    pending = journal.pending()
    assert [row["payload"]["sequence"] for row in pending[1:]] == [1, 2]
    with pytest.raises(ValueError):
        journal.enqueue_report("private", {**projection(), "prompt": "private content"})
    with pytest.raises(ValueError):
        journal.enqueue_report("bool", {**projection(), "counts": {**projection()["counts"], "workers_active": True}})
    with pytest.raises(JournalConflict):
        journal.enqueue_report("one", {**projection(), "state": "paused"})
    report = pending[1]
    journal.acknowledge(report["id"], {"binding_id": report["payload"]["binding_id"], "sequence": 1})
    assert len(journal.pending()) == 2  # Unknown reserve cannot block unrelated observations.


def test_registration_wire_ids_and_original_lease_are_durable(setup):
    journal, lease, _ = setup
    intent = journal.registration(lease)
    assert intent["local_run_id"] != journal.binding.run_id
    reopened = ManagedJournal(journal.path, journal.binding, clock=journal.clock)
    assert reopened.registration(lease) == intent
    with pytest.raises(JournalConflict, match="original lease"):
        reopened.registration({**lease, "lease_id": uid()})
    journal.bind_remote(uid())
    with pytest.raises(JournalConflict, match="cannot change"):
        journal.bind_remote(uid())


def _request_race(path, binding, lease, barrier, output):
    journal = ManagedJournal(path, binding)
    barrier.wait(timeout=15)
    result = journal.request("shared", semantics())
    output.put((result["remote_request_id"], journal.mark_reserve_pending("shared", lease)))


def test_processes_share_one_immutable_mapping_and_one_reservation_sender(setup):
    journal, lease, _ = setup
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_request_race, args=(journal.path, journal.binding, lease, barrier, output)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        rows = [output.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert len({row[0] for row in rows}) == 1
        assert sum(row[1] for row in rows) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_unknown_schema_rejected_and_sqlite_handles_closed(setup):
    journal, _, _ = setup
    journal.inspect()
    moved = journal.path.with_suffix(".moved")
    journal.path.rename(moved)
    moved.rename(journal.path)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(JournalConflict, match="unsupported"):
        ManagedJournal(journal.path, journal.binding)


def test_worker_slot_once_and_reopen_never_launches_or_automatically_releases(setup):
    journal, lease, _ = setup
    remote = ensure_worker(journal, lease)
    assert not journal.claim_worker("attempt", 3)
    reopened = ManagedJournal(journal.path, journal.binding)
    assert reopened.worker("attempt", 3)["remote_worker_id"] == remote
    assert not reopened.claim_worker("attempt", 3)
    reopened.fence_restart()
    assert reopened.inspect()["effects"][0]["outcome"] == "uncertain"
    assert reopened.pending()[0]["payload"] == {"worker_id": remote, "outcome": "uncertain"}
    with pytest.raises(JournalConflict, match="never started"):
        reopened.observe_worker("attempt", 3, "never_started")
    # Only a separate explicit trusted OS observation can discharge the slot.
    reopened.observe_worker("attempt", 3, "stopped")
    pending = reopened.pending()
    assert len(pending) == 1 and pending[0]["payload"]["outcome"] == "stopped"
    with sqlite3.connect(journal.path) as connection:
        history = connection.execute("SELECT payload,receipt,superseded_by FROM outbox ORDER BY ordinal").fetchall()
    assert len(history) == 2
    assert history[0][1] is None and history[0][2] == pending[0]["id"]
    assert json.loads(history[0][0])["outcome"] == "uncertain"


def test_request_start_requires_exact_current_worker_binding(setup):
    journal, lease, _ = setup
    remote = journal.request("request", semantics())["remote_request_id"]
    journal.mark_reserve_pending("request", lease)
    journal.reserve_receipt("request", lease, {"request_id": remote, "lease_id": lease["lease_id"], "units": 1, "state": "reserved"})
    with pytest.raises(JournalConflict, match="originating worker"):
        journal.begin_start("request")
    worker_id = ensure_worker(journal, lease)
    receipt = {"request_id": remote, "worker_id": worker_id, "purpose": "primary", "bound": True}
    with pytest.raises(JournalConflict):
        journal.bind_request_receipt("request", uid(), receipt)
    with pytest.raises(JournalConflict):
        journal.bind_request_receipt("request", worker_id, {**receipt, "purpose": "compression"})
    journal.bind_request_receipt("request", worker_id, receipt)
    assert journal.begin_start("request")


def test_action_requires_completed_primary_and_exact_worker_and_arguments(setup):
    journal, lease, _ = setup
    remote = permit(journal, lease)
    worker_id = ensure_worker(journal, lease)
    metadata = {"worker_id": worker_id, "request_id": remote, "tool_name": "file_read", "arguments_sha256": "c" * 64}
    with pytest.raises(JournalConflict, match="completed originating"):
        journal.action("call-1", metadata)
    journal.claim_dispatch("native-request")
    journal.observe("native-request", "completed")
    action = journal.action("call-1", metadata)
    with pytest.raises(JournalConflict, match="different semantics"):
        journal.action("call-1", {**metadata, "arguments_sha256": "d" * 64})
    with pytest.raises(JournalConflict):
        journal.action("foreign", {**metadata, "worker_id": uid()})
    assert journal.begin_action("call-1", lease)
    assert not journal.begin_action("call-1", lease)
    journal.action_receipt("call-1", lease, {"action_id": action["remote_action_id"], "state": "admitted", "dispatch_permitted": True})
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: journal.claim_action("call-1"), range(15)))
    assert claims.count(True) == 1
    journal.observe_action("call-1", "completed")
    receipt = [row for row in journal.pending() if row["kind"] == "observe_tool"][0]
    assert receipt["payload"] == {"action_id": action["remote_action_id"], "outcome": "completed"}


@pytest.mark.parametrize("kind", ["worker", "action"])
def test_effect_delayed_receipt_cannot_borrow_replacement_lease(setup, kind):
    journal, lease, _ = setup
    journal.bind_remote(uid())
    if kind == "worker":
        effect = journal.worker("other", 3)
        journal.begin_worker("other", 3, lease)
        receipt = {"worker_id": effect["remote_worker_id"], "state": "held", "dispatch_permitted": True}
        with pytest.raises(JournalConflict, match="pending send"):
            journal.worker_receipt("other", 3, {**lease, "lease_id": uid()}, receipt)
    else:
        remote = permit(journal, lease)
        journal.claim_dispatch("native-request")
        journal.observe("native-request", "completed")
        effect = journal.action("call", {"worker_id": ensure_worker(journal, lease), "request_id": remote,
                                         "tool_name": "file_read", "arguments_sha256": "c" * 64})
        journal.begin_action("call", lease)
        receipt = {"action_id": effect["remote_action_id"], "state": "admitted", "dispatch_permitted": True}
        with pytest.raises(JournalConflict, match="pending send"):
            journal.action_receipt("call", {**lease, "lease_id": uid()}, receipt)


def test_worker_replayed_permit_cannot_launch_and_known_unused_permit_can_release(setup):
    journal, lease, _ = setup
    journal.bind_remote(uid())
    row = journal.worker("never", 3)
    journal.begin_worker("never", 3, lease)
    journal.worker_receipt("never", 3, lease, {"worker_id": row["remote_worker_id"], "state": "held", "dispatch_permitted": False})
    assert not journal.claim_worker("never", 3)
    journal.observe_worker("never", 3, "never_started")
    assert journal.pending()[0]["payload"] == {"worker_id": row["remote_worker_id"], "outcome": "never_started"}


def test_unknown_request_retries_cannot_starve_later_real_worker_cleanup_after_restart(setup):
    journal, lease, _ = setup
    worker_id = ensure_worker(journal, lease)
    for index in range(9):
        local_id = f"ambiguous-{index}"
        journal.request(local_id, semantics())
        journal.mark_reserve_pending(local_id, lease)
        journal.observe(local_id, "uncertain")
    journal.observe_worker("attempt", 3, "stopped")
    first = journal.pending(limit=8)
    assert all(row["kind"] == "settle" for row in first)
    for row in first:
        journal.mark_delivery_attempt(row["id"])
    reopened = ManagedJournal(journal.path, journal.binding)
    second = reopened.pending(limit=2)
    assert second[1]["payload"] == {"worker_id": worker_id, "outcome": "stopped"}
    reopened.acknowledge(second[1]["id"], {"worker_id": worker_id, "state": "stopped"})
    assert reopened.inspect()["pending_observations"] == 9


def test_recovery_preserves_remote_start_marker_before_local_dispatch(setup):
    journal, lease, _ = setup
    reserve(journal, lease)
    assert journal.begin_start("native-request")
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    row = reopened.recovery_records("requests")["records"][0]
    assert row["start_attempted"] == 1 and row["dispatch_claimed"] == 0
    with pytest.raises(JournalConflict, match="cannot be refunded"):
        reopened.reconcile_request("native-request", "never_started", "a" * 64)
    with pytest.raises(JournalConflict, match="original dispatch"):
        reopened.reconcile_request("native-request", "completed", "a" * 64)
    # A definite failed local request remains a consumed unit, not completion.
    reopened.reconcile_request("native-request", "failed", "b" * 64)
    record = [row for row in reopened.pending() if row["kind"] == "settle"][0]
    assert record["payload"]["outcome"] == "failed"
    with pytest.raises(JournalConflict, match="immutable"):
        reopened.reconcile_request("native-request", "completed", "c" * 64)


def test_recovery_exact_observations_preserve_prior_uncertainty_provenance(setup):
    journal, lease, _ = setup
    permit(journal, lease)
    journal.claim_dispatch("native-request")
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    reopened.reconcile_request("native-request", "uncertain", "a" * 64)
    reopened.reconcile_request("native-request", "completed", "b" * 64)
    reopened.reconcile_request("native-request", "completed", "b" * 64)
    with pytest.raises(JournalConflict, match="immutable"):
        reopened.reconcile_request("native-request", "completed", "c" * 64)
    with sqlite3.connect(journal.path) as connection:
        records = connection.execute("SELECT outcome,evidence_sha256 FROM recovery_receipts WHERE kind='request' ORDER BY outcome").fetchall()
    assert records == [("completed", "b" * 64), ("uncertain", "a" * 64)]
    assert len([row for row in reopened.pending() if row["kind"] == "settle"]) == 1
    reopened.reconcile_worker("attempt", 3, "stopped", "d" * 64)
    assert reopened.recovery_summary()["requests_unknown"] == 0
    assert reopened.recovery_summary()["effects_unknown"] == 0
    assert not reopened.claim_dispatch("native-request")


def test_recovery_accounting_pages_cover_more_than_inspection_limit(setup):
    journal, _, _ = setup
    for index in range(125):
        journal.request(f"request-{index:03}", semantics())
    assert len(journal.inspect()["requests"]) == 100
    assert journal.recovery_summary()["requests_unknown"] == 125
    first = journal.recovery_records("requests")
    second = journal.recovery_records("requests", after=first["next_cursor"])
    assert len(first["records"]) == 100 and len(second["records"]) == 25
    assert second["next_cursor"] is None
    assert len({row["local_id"] for row in first["records"] + second["records"]}) == 125
    reopened = ManagedJournal(journal.path, journal.binding)
    reopened.fence_restart()
    reopened.reconcile_request("request-000", "never_started", "a" * 64)
    assert reopened.recovery_summary()["requests_unknown"] == 0
    assert not reopened.pending()  # Purely local intents have no remote row.
