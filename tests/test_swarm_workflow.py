"""Asynchronous owner integration against isolated Git and real check children."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

from lumi.engine.swarming.integration_processes import IntegrationProcesses
from lumi.engine.swarming.models import Conflict, IdempotencyConflict, RevisionConflict, StaleAuthority
from lumi.engine.swarming.workflow import DispatchClosed, IntegrationWorkflow
from tests.test_swarm_integration import command, finish, git, setup as integration_setup, writers  # noqa: F401


def wait_for(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    pytest.fail("Fixture operation did not reach its observed state")


@pytest.fixture
def setup(integration_setup):  # noqa: F811 - imported pytest fixture
    store, supervisor, authority, integration, project, base = integration_setup
    context, writer = writers(integration_setup, names=("a",))[0]
    (Path(writer["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(integration_setup, context, writer)
    workflow = IntegrationWorkflow(supervisor, authority, integration)
    payload = {"writer_ids": [manifest["id"]], "checks": [{"key": "combined",
        "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('a.txt').read_text() == 'new-a\\n'"],
        "timeout_seconds": 10}], "criterion_checks": {"a": {"combined": "combined"}}}
    yield integration_setup, workflow, payload
    workflow.close(timeout=2)


def submit(workflow, kind, payload, *, key):
    revision = workflow.store.snapshot(workflow.authority.scope, workflow.authority.run_id)["run"]["revision"]
    return workflow.submit(kind, payload, command_id=key, expected_revision=revision)


def settled(workflow, operation):
    return wait_for(lambda: (row if row["state"] not in {"queued", "running"} else None)
                    if (row := workflow.inspect(operation["id"])) else None)


def prepared(workflow, payload):
    operation = submit(workflow, "prepare_candidate", payload, key="prepare")
    assert settled(workflow, operation)["state"] == "completed"
    return operation["effect_id"]


def verified(workflow, payload):
    candidate_id = prepared(workflow, payload)
    operation = submit(workflow, "run_check", {"candidate_id": candidate_id, "check_key": "combined"}, key="check")
    assert settled(workflow, operation)["state"] == "completed"
    return workflow.store.snapshot(workflow.authority.scope, workflow.authority.run_id)["integration_candidates"][0]


def test_admission_returns_promptly_exact_racing_retries_launch_once_and_apply_exact_candidate(setup, monkeypatch):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, project, base = fixture
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = integration.prepare_candidate
    def blocked(*args, **kwargs):
        calls.append(kwargs["candidate_id"])
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(integration, "prepare_candidate", blocked)
    revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    other = IntegrationWorkflow(supervisor, authority, integration)
    try:
        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(host.submit, "prepare_candidate", payload, command_id="prepare", expected_revision=revision)
                       for host in (workflow, other)]
            receipts = [future.result(timeout=2) for future in futures]
        assert time.monotonic() - began < 2
        assert receipts[0]["id"] == receipts[1]["id"]
        assert entered.wait(2) and len(calls) == 1
        assert git(project, "rev-parse", "HEAD") == base
        with pytest.raises(IdempotencyConflict):
            workflow.submit("prepare_candidate", {**payload, "checks": [{**payload["checks"][0], "argv": [sys.executable, "-c", "print('different')"]}]},
                            command_id="prepare", expected_revision=revision)
        with pytest.raises(RevisionConflict):
            workflow.submit("prepare_candidate", payload, command_id="another", expected_revision=revision)
        release.set()
        wait_for(lambda: workflow.inspect(receipts[0]["id"])["state"] == "completed")
        candidate_id = receipts[0]["effect_id"]
        checked = submit(workflow, "run_check", {"candidate_id": candidate_id, "check_key": "combined"}, key="check")
        assert settled(workflow, checked)["state"] == "completed"
        candidate = store.snapshot(authority.scope, authority.run_id)["integration_candidates"][0]
        approval = {"candidate_id": candidate_id, "expected_base": base, "target_revision": candidate["result_revision"],
                    "evidence": "Owner reviewed the exact combined revision and successful named check", "approval_seconds": 90}
        with pytest.raises(Conflict, match="exactly verified"):
            submit(workflow, "apply", {**approval, "expected_base": "0" * 40}, key="wrong-base")
        (project / "a.txt").write_text("Owner fixture edits must survive\n")
        deferred = submit(workflow, "apply", approval, key="dirty-apply")
        assert settled(workflow, deferred)["state"] == "failed"
        assert (project / "a.txt").read_text() == "Owner fixture edits must survive\n"
        assert not store.snapshot(authority.scope, authority.run_id)["integration_applications"]
        (project / "a.txt").write_text("base-a\n")
        apply_revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
        applied = workflow.submit("apply", approval, command_id="apply", expected_revision=apply_revision)
        assert settled(workflow, applied)["state"] == "completed"
        assert git(project, "rev-parse", "HEAD") == candidate["result_revision"]
        assert (project / "a.txt").read_text() == "new-a\n"
        store.clock = lambda: 1005
        assert workflow.submit("apply", approval, command_id="apply", expected_revision=apply_revision)["approval_expires_at"] == 1090
        snapshot = store.snapshot(authority.scope, authority.run_id)
        assert snapshot["work_items"][0]["state"] == "submitted" and not snapshot["writer_acceptances"]
        assert len(snapshot["integration_applications"]) == 1
        assert snapshot["integration_applications"][0]["id"] == applied["effect_id"]
        safe = json.dumps(workflow.inspect())
        assert "payload_json" not in safe and "argv" not in safe and "supervisor_id" not in safe
    finally:
        release.set()
        other.close(timeout=2)


def test_stop_waits_for_running_named_check_and_workflow_observation(setup):
    fixture, workflow, payload = setup
    store, supervisor, authority, _, _, _ = fixture
    payload["checks"][0]["argv"] = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"]
    payload["checks"][0]["timeout_seconds"] = 60
    candidate_id = prepared(workflow, payload)
    operation = submit(workflow, "run_check", {"candidate_id": candidate_id, "check_key": "combined"}, key="check")
    def running_check():
        snapshot = store.snapshot(authority.scope, authority.run_id)
        return next((row for row in snapshot["integration_checks"] if row["job_id"]), None)
    check = wait_for(running_check)
    assert workflow.inspect(operation["id"])["state"] == "running"
    assert command(supervisor, authority, "stop").state == "stopping"
    assert settled(workflow, operation)["state"] == "cancelled"
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["run"]["state"] == "cancelled"
    process = next(row for row in snapshot["integration_processes"] if row["id"] == check["job_id"])
    assert process["state"] == "stopped"
    observation = IntegrationProcesses(store).inspect_effect(authority.scope, authority.run_id, "check", check["id"])
    assert all(row["observation"] == "stopped" for row in observation["processes"])
    assert snapshot["integration_checks"][0]["state"] == "cancelled"


def test_close_revokes_queued_launch_and_does_not_claim_thread_termination(setup, monkeypatch):
    fixture, workflow, payload = setup
    entered, release = threading.Event(), threading.Event()
    original = workflow._run
    def delayed(record):
        entered.set()
        assert release.wait(5)
        original(record)
    monkeypatch.setattr(workflow, "_run", delayed)
    operation = submit(workflow, "prepare_candidate", payload, key="prepare")
    assert entered.wait(2)
    try:
        status = workflow.close(timeout=.01)[0]
        assert status["state"] == "queued" and status["active"]
        with pytest.raises(DispatchClosed):
            submit(workflow, "prepare_candidate", payload, key="after-close")
    finally:
        release.set()
    assert settled(workflow, operation)["state"] == "cancelled"
    assert not fixture[0].snapshot(fixture[2].scope, fixture[2].run_id)["integration_candidates"]


def test_admission_rollback_does_not_launch_or_consume_revision(setup, monkeypatch):
    fixture, workflow, payload = setup
    store, _, authority, _, _, _ = fixture
    before = store.snapshot(authority.scope, authority.run_id)
    original = store._event
    def failing(connection, run_id, kind, data):
        if kind == "integration_operation_queued":
            raise sqlite3.OperationalError("Fixture durable admission rollback")
        return original(connection, run_id, kind, data)
    monkeypatch.setattr(store, "_event", failing)
    with pytest.raises(sqlite3.OperationalError, match="rollback"):
        submit(workflow, "prepare_candidate", payload, key="prepare")
    after = store.snapshot(authority.scope, authority.run_id)
    assert after["run"]["revision"] == before["run"]["revision"]
    assert not after["integration_operations"] and not after["integration_candidates"]
    assert not workflow._threads


def test_reopened_workflow_never_replays_an_unobserved_dispatch_and_blocks_terminal_state(setup, monkeypatch):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, _, _ = fixture
    # Simulate host loss immediately after the durable intent, before any
    # worker observation. No process-exit or effect-success assertion is made.
    monkeypatch.setattr(workflow, "_run", lambda record: None)
    revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    operation = workflow.submit("prepare_candidate", payload, command_id="prepare", expected_revision=revision)
    wait_for(lambda: not workflow.inspect(operation["id"])["active"])
    reopened = IntegrationWorkflow(supervisor, authority, integration)
    try:
        repeated = reopened.submit("prepare_candidate", payload, command_id="prepare", expected_revision=revision)
        assert repeated["id"] == operation["id"] and repeated["requires_reconciliation"]
        with pytest.raises(Conflict, match="fenced historical"):
            submit(reopened, "reconcile_operation", {"operation_id": operation["id"], "evidence": "No effect is visible yet"}, key="too-early")
        assert not store.snapshot(authority.scope, authority.run_id)["integration_candidates"]
        assert command(supervisor, authority, "pause").state == "pausing"
        assert command(supervisor, authority, "stop").state == "stopping"
        store.clock = lambda: 1031
        replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
        with pytest.raises(StaleAuthority):
            reopened.submit("prepare_candidate", payload, command_id="prepare", expected_revision=revision)
        snapshot = store.snapshot(authority.scope, authority.run_id)
        assert snapshot["integration_operations"][0]["state"] == "uncertain"
        with pytest.raises(Conflict, match="unresolved"):
            command(supervisor, replacement, "recover", retry_work_items=[])
        observer = IntegrationWorkflow(supervisor, replacement, integration)
        try:
            observed = submit(observer, "reconcile_operation", {"operation_id": operation["id"],
                "evidence": "Review the original fenced dispatch and its unique effect intent"}, key="observe-unused")
            assert settled(observer, observed)["state"] == "cancelled"
            assert observer.inspect(operation["id"])["result"]["state"] == "not_started"
            assert command(supervisor, replacement, "recover", retry_work_items=[]).state == "cancelled"
            assert not store.snapshot(authority.scope, authority.run_id)["integration_candidates"]
        finally:
            observer.close(timeout=2)
    finally:
        reopened.close(timeout=.1)


def test_apply_uncertainty_reconciles_observed_exact_ref_without_replaying_merge(setup, monkeypatch):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, project, base = fixture
    candidate = verified(workflow, payload)
    original_admit, original_git = integration._admit, integration._git
    merges = []
    def tracked(path, *args, **kwargs):
        result = original_git(path, *args, **kwargs)
        if args and args[0] == "merge":
            # The managed launch rechecks admission before invoking Git. Lose
            # acknowledgement only after the real merge has returned.
            merges.append(args)
        return result
    def interrupted(owner):
        if merges:
            raise sqlite3.OperationalError("Fixture loses acknowledgement after Git application")
        return original_admit(owner)
    monkeypatch.setattr(integration, "_git", tracked)
    monkeypatch.setattr(integration, "_admit", interrupted)
    operation = submit(workflow, "apply", {"candidate_id": candidate["id"], "expected_base": base,
        "target_revision": candidate["result_revision"], "evidence": "Reviewed exact verified change"}, key="apply")
    assert settled(workflow, operation)["state"] == "uncertain"
    assert git(project, "rev-parse", "HEAD") == candidate["result_revision"] and len(merges) == 1
    assert command(supervisor, authority, "stop").state == "stopping"
    monkeypatch.setattr(integration, "_admit", original_admit)
    store.clock = lambda: 1031
    replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    recovered = IntegrationWorkflow(supervisor, replacement, integration)
    try:
        observed = submit(recovered, "reconcile_application", {"approval_id": operation["effect_id"]}, key="observe")
        assert settled(recovered, observed)["state"] == "completed"
        assert recovered.inspect(operation["id"])["state"] == "completed"
        assert command(supervisor, replacement, "recover", retry_work_items=[]).state == "cancelled"
        assert len(merges) == 1
    finally:
        recovered.close(timeout=2)


def test_fresh_owner_accepts_historical_applied_writer_without_new_effect_authority(setup):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, project, base = fixture
    candidate = verified(workflow, payload)
    applied = submit(workflow, "apply", {"candidate_id": candidate["id"], "expected_base": base,
        "target_revision": candidate["result_revision"], "evidence": "Owner approved exact checked result"}, key="apply")
    assert settled(workflow, applied)["state"] == "completed"
    original = store.snapshot(authority.scope, authority.run_id)
    attempt = original["attempts"][0]
    store.clock = lambda: 1031
    replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    command(supervisor, replacement, "recover", retry_work_items=[])
    (project / "a.txt").write_text("Subsequent owner edits stay untouched\n")
    with pytest.raises(StaleAuthority):
        command(supervisor, authority, "accept_writer", attempt_id=attempt["id"], attempt_epoch=1,
                candidate_id=candidate["id"], evidence="Old authority cannot approve")
    with pytest.raises(StaleAuthority):
        command(supervisor, replacement, "submit", attempt_id=attempt["id"], attempt_epoch=1,
                candidate_revision="forged", handoff="Cannot revive old execution")
    command(supervisor, replacement, "accept_writer", attempt_id=attempt["id"], attempt_epoch=1,
            candidate_id=candidate["id"], evidence="Current owner reviewed retained application and independent checks")
    assert command(supervisor, replacement, "complete").state == "completed"
    final = store.snapshot(authority.scope, authority.run_id)
    for table in ("submissions", "writer_worktrees", "integration_candidates", "integration_checks", "integration_applications"):
        assert final[table] == original[table]
    assert final["writer_acceptances"][0]["epoch"] == 2 and attempt["epoch"] == 1
    assert (project / "a.txt").read_text() == "Subsequent owner edits stay untouched\n"


@pytest.mark.parametrize("continued", [False, True])
def test_owner_rejects_historical_unapplied_writer_then_explicit_redo_has_fresh_identity(setup, continued):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, _, _ = fixture
    candidate_id = prepared(workflow, payload)
    original = store.snapshot(authority.scope, authority.run_id)
    attempt = original["attempts"][0]
    store.clock = lambda: 1031
    replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    if continued:
        command(supervisor, replacement, "recover", retry_work_items=[])
    command(supervisor, replacement, "reject", attempt_id=attempt["id"], attempt_epoch=1,
            evidence="Owner explicitly requests a new execution; retained result is not adopted")
    if continued:
        command(supervisor, replacement, "retry", work_item_id="a", evidence="Owner requested a fresh isolated attempt")
    else:
        command(supervisor, replacement, "recover", retry_work_items=["a"])
    assigned = command(supervisor, replacement, "assign", work_item_id="a", worker_id="replacement-worker", requests=2,
                       model={"provider": "ollama", "model": "fixture"}).result
    assert assigned["attempt_id"] != attempt["id"] and assigned["epoch"] == 2
    current = store.snapshot(authority.scope, authority.run_id)
    assert current["submissions"] == original["submissions"]
    assert current["writer_worktrees"] == original["writer_worktrees"]
    assert current["integration_candidates"][0]["id"] == candidate_id
    assert current["integration_candidates"][0]["state"] == "superseded"
    observer = IntegrationWorkflow(supervisor, replacement, integration)
    try:
        with pytest.raises(Conflict, match="current finalized"):
            submit(observer, "prepare_candidate", payload, key="cannot-adopt")
    finally:
        observer.close(timeout=1)


def test_fenced_operation_with_terminal_effect_receipt_reconciles_without_reexecution(setup, monkeypatch):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, _, _ = fixture
    def missing_ack(record, state, result, error):
        raise sqlite3.OperationalError("Fixture lost workflow observation acknowledgement")
    monkeypatch.setattr(workflow, "_finish", missing_ack)
    operation = submit(workflow, "prepare_candidate", payload, key="prepare")
    wait_for(lambda: not workflow.inspect(operation["id"])["active"])
    original = store.snapshot(authority.scope, authority.run_id)
    assert original["integration_candidates"][0]["state"] == "ready"
    assert original["integration_operations"][0]["state"] == "running"
    store.clock = lambda: 1031
    replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    observer = IntegrationWorkflow(supervisor, replacement, integration)
    try:
        intent = {"operation_id": operation["id"], "evidence": "Inspect the exact already-recorded candidate outcome"}
        with pytest.raises(ValueError):
            submit(observer, "reconcile_operation", {**intent, "outcome": "completed"}, key="forged-outcome")
        revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
        observation = observer.submit("reconcile_operation", intent, command_id="observe", expected_revision=revision)
        assert settled(observer, observation)["state"] == "completed"
        assert observer.submit("reconcile_operation", intent, command_id="observe", expected_revision=revision)["id"] == observation["id"]
        with pytest.raises(IdempotencyConflict):
            observer.submit("reconcile_operation", {**intent, "evidence": "Different semantics"}, command_id="observe", expected_revision=revision)
        assert observer.inspect(operation["id"])["state"] == "completed"
        assert command(supervisor, replacement, "recover", retry_work_items=[]).state == "running"
        final = store.snapshot(authority.scope, authority.run_id)
        assert final["integration_candidates"] == original["integration_candidates"]
        projected = IntegrationWorkflow.inspect_row(final["integration_operations"][0])
        assert projected["state"] == "completed" and not projected["active"]
        assert "supervisor_id" not in projected and "payload_json" not in projected
    finally:
        observer.close(timeout=2)


@pytest.mark.parametrize("legacy", [False, True])
def test_check_after_takeover_uses_cleanup_evidence_and_never_owner_prose_as_pass(setup, legacy):
    fixture, workflow, payload = setup
    store, supervisor, authority, integration, _, _ = fixture
    payload["checks"][0]["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
    payload["checks"][0]["timeout_seconds"] = 60
    candidate_id = prepared(workflow, payload)
    operation = submit(workflow, "run_check", {"candidate_id": candidate_id, "check_key": "combined"}, key="check")
    wait_for(lambda: any(row["job_id"] for row in store.snapshot(authority.scope, authority.run_id)["integration_checks"]))
    # job_id identifies the owned launcher before executable argv is released.
    # This case exercises cancellation of an invoked check; takeover at the
    # earlier gate legitimately produces the distinct not_started result.
    wait_for(lambda: any(row["effect_id"] == operation["effect_id"] and row["invoked"]
                         for row in store.snapshot(authority.scope, authority.run_id)["integration_processes"]))
    if legacy:
        with store._connection(write=True) as connection:
            # Historical intent did not attest complete gated coverage. Even
            # available individual process records cannot fabricate that claim.
            connection.execute("UPDATE integration_checks SET process_protocol=0")
    store.clock = lambda: 1031
    replacement = supervisor.acquire(authority.scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")
    wait_for(lambda: not workflow.inspect(operation["id"])["active"])
    observer = IntegrationWorkflow(supervisor, replacement, integration)
    try:
        observed = submit(observer, "reconcile_operation", {"operation_id": operation["id"],
            "evidence": "Owner prose claims the check probably completed"}, key="observe")
        assert settled(observer, observed)["state"] == ("failed" if legacy else "cancelled")
        assert observer.inspect(operation["id"])["state"] == ("uncertain" if legacy else "cancelled")
        if legacy:
            with pytest.raises(Conflict, match="unresolved"):
                command(supervisor, replacement, "recover", retry_work_items=[])
        else:
            assert command(supervisor, replacement, "recover", retry_work_items=[]).state == "running"
        state = store.snapshot(authority.scope, authority.run_id)
        assert state["integration_checks"][0]["state"] == ("uncertain" if legacy else "cancelled")
        assert not state["writer_acceptances"]
    finally:
        observer.close(timeout=2)
