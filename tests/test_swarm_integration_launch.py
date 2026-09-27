"""Integration gates reject unsupported or unobserved effects before ref claims."""
# ruff: noqa: F811 -- imported pytest fixture is intentionally used by name.

import json
from concurrent.futures import ThreadPoolExecutor
import sys
import time

import pytest

from lumi.engine.swarming import argv_process
from lumi.engine.execution_guard import ExecutionGuardError
from lumi.engine.swarming.integration import SwarmIntegration
from lumi.engine.swarming.models import AdmissionClosed, Conflict, ScopeDenied
from tests.test_swarm_integration import command, setup, writers  # noqa: F401


def application_intent(setup, *, protocol=1):
    store, _, authority, integration, _, base = setup
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,result_revision,state,manifest_json,process_protocol) "
            "VALUES('candidate',?,?,?,?,?,?,'verified','{}',1)",
            (authority.run_id, authority.epoch, integration.repo_key, str(integration.root), base, '1' * 40))
        connection.execute("INSERT INTO integration_applications(id,candidate_id,expected_base,target_revision,state,approval_json,process_protocol) "
            "VALUES('application','candidate',?,?,'uncertain','{}',?)", (base, '1' * 40, protocol))
    return authority, integration


def test_unsupported_host_rejects_writer_and_check_before_scope_or_intent(monkeypatch):
    monkeypatch.setattr(argv_process, "_NAMED_JOB_SUPPORT", False)
    integration = object.__new__(SwarmIntegration)
    assert not integration.writer_support()["supported"]
    with pytest.raises(ScopeDenied, match="Windows named-job"):
        integration.create_writer(None, None, base_revision="unused")
    with pytest.raises(ScopeDenied, match="Windows named-job"):
        integration.run_check(None, "unused", "unused")


def test_application_cannot_inspect_ref_before_owned_process_cleanup(setup, monkeypatch):
    authority, integration = application_intent(setup)
    with integration.store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_processes(id,run_id,epoch,effect_kind,effect_id,argv_sha256,cwd,host_id,state) "
            "VALUES('process',?,?,'application','application','hash',?,'host','invoked')",
            (authority.run_id, authority.epoch, str(integration.project)))
    monkeypatch.setattr(integration, "_head", lambda _: pytest.fail("Unsettled application inspected its ref"))
    with pytest.raises(Conflict, match="process cleanup"):
        integration.reconcile_application(authority, "application")


def test_legacy_application_cannot_infer_absence_from_current_ref(setup, monkeypatch):
    authority, integration = application_intent(setup, protocol=0)
    monkeypatch.setattr(integration, "_head", lambda _: pytest.fail("Legacy application inspected its ref"))
    with pytest.raises(Conflict, match="containment proof"):
        integration.reconcile_application(authority, "application")


def test_cleaned_startup_failure_without_creation_time_records_never_invoked(setup, monkeypatch):
    authority, integration = application_intent(setup)
    with integration.store._connection(write=True) as connection:
        connection.execute("UPDATE integration_applications SET state='applying'")
    class FailedGate:
        pid, created_at, cleanup_confirmed = 12345, None, True
        def execute(self, *args, **kwargs):
            raise OSError("Fixture job admission failure")
    monkeypatch.setattr("lumi.engine.swarming.integration.ManagedArgvProcess", FailedGate)
    with pytest.raises(OSError, match="job admission"):
        integration._execute(authority, "application", "application", ["fixture"], integration.project)
    records = integration.store.snapshot(authority.scope, authority.run_id)["integration_processes"]
    assert len(records) == 1 and records[0]["state"] == "not_started" and records[0]["invoked"] == 0


def test_late_old_writer_failure_preserves_reconciled_terminal_disposition(setup):
    store, _, authority, integration, _, base = setup
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,result_revision,state,manifest_json) "
            "VALUES('candidate',?,?,?,?,?,?,'failed',?)", (authority.run_id, authority.epoch, integration.repo_key,
            str(integration.root), base, base, json.dumps({"retained": True})))
        connection.execute("UPDATE runs SET epoch=epoch+1 WHERE id=?", (authority.run_id,))
    integration._outcome(authority, "integration_candidates", "candidate", "uncertain", historical=True)
    with store._connection() as connection:
        row = connection.execute("SELECT * FROM integration_candidates WHERE id='candidate'").fetchone()
    assert row["state"] == "failed" and row["result_revision"] == base and json.loads(row["manifest_json"]) == {"retained": True}


def test_pause_during_owned_isolated_effect_retains_failure_then_reaches_checkpoint(setup, monkeypatch):
    store, supervisor, authority, integration, project, _ = setup
    marker = project.parent / "launched"
    original = integration._execute
    def slow_effect(owner, kind, identity, argv, cwd, **kwargs):
        return original(owner, kind, identity, [sys.executable, "-c",
            f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)"], cwd, **kwargs)
    monkeypatch.setattr(integration, "_execute", slow_effect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(writers, setup, ("a",))
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        assert command(supervisor, authority, "pause").state == "pausing"
        with pytest.raises(AdmissionClosed):
            pending.result(timeout=5)
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["writer_worktrees"][0]["state"] == "failed"
    assert all(row["state"] == "stopped" for row in snapshot["integration_processes"])
    attempt = snapshot["attempts"][0]
    command(supervisor, authority, "worker_stopped", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
        outcome="cancelled", evidence="Isolated setup failed after actual owned helper cleanup; model was never launched")
    assert store.snapshot(authority.scope, authority.run_id)["run"]["state"] == "paused"


def test_late_check_exception_cannot_overwrite_new_owner_cancelled_observation(setup, monkeypatch):
    authority, integration = application_intent(setup)
    with integration.store._connection(write=True) as connection:
        connection.execute("UPDATE integration_candidates SET state='ready',manifest_json=?",
            (json.dumps({"checks": [{"key": "check", "argv": ["fixture"], "timeout_seconds": 1}]}),))
    monkeypatch.setattr(integration, "_head", lambda _: '1' * 40)
    monkeypatch.setattr(integration, "_clean", lambda _: True)
    monkeypatch.setattr(integration, "_path", lambda _: integration.root)
    def fenced(*args, **kwargs):
        with integration.store._connection(write=True) as connection:
            connection.execute("UPDATE runs SET epoch=epoch+1 WHERE id=?", (authority.run_id,))
            connection.execute("UPDATE integration_checks SET state='cancelled',output='New owner cleanup proof'")
            connection.execute("UPDATE integration_candidates SET state='failed'")
        raise Conflict("Old host completed after takeover")
    monkeypatch.setattr(integration, "_execute", fenced)
    with pytest.raises(Conflict, match="Old host"):
        integration.run_check(authority, "candidate", "check")
    snapshot = integration.store.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_checks"][0]["state"] == "cancelled"
    assert snapshot["integration_checks"][0]["output"] == "New owner cleanup proof"
    assert snapshot["integration_candidates"][0]["state"] == "failed"


def test_stop_after_actual_check_exit_preserves_output_without_claiming_pass(setup, monkeypatch):
    authority, integration = application_intent(setup)
    _, supervisor, _, _, _, _ = setup
    with integration.store._connection(write=True) as connection:
        connection.execute("UPDATE integration_candidates SET state='ready',manifest_json=?",
            (json.dumps({"checks": [{"key": "check", "argv": [sys.executable, "-c", "print('exact fixture output')"],
                                     "timeout_seconds": 5}]}),))
    monkeypatch.setattr(integration, "_head", lambda _: '1' * 40)
    monkeypatch.setattr(integration, "_clean", lambda _: True)
    monkeypatch.setattr(integration, "_path", lambda _: integration.root)
    original = integration._execute
    def stopped_after_observation(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.exit_code == 0 and result.output_complete
        command(supervisor, authority, "stop")
        return result
    monkeypatch.setattr(integration, "_execute", stopped_after_observation)
    with pytest.raises(AdmissionClosed):
        integration.run_check(authority, "candidate", "check")
    observed = integration.store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]
    assert observed["state"] == "cancelled" and observed["exit_code"] == 0
    assert "exact fixture output" in observed["output"] and "Check interrupted:" in observed["output"]


def test_broken_gate_protocol_retains_actual_partial_output_as_uncertain(setup, monkeypatch):
    authority, integration = application_intent(setup)
    with integration.store._connection(write=True) as connection:
        connection.execute("UPDATE integration_candidates SET state='ready',manifest_json=?",
            (json.dumps({"checks": [{"key": "check", "argv": ["fixture"], "timeout_seconds": 5}]}),))
    monkeypatch.setattr(integration, "_head", lambda _: '1' * 40)
    monkeypatch.setattr(integration, "_clean", lambda _: True)
    monkeypatch.setattr(integration, "_path", lambda _: integration.root)
    code = ("import sys,json,base64,os; "
        "print(json.dumps({'version':1,'kind':'ready'}),flush=True); sys.stdin.readline(); "
        "print(json.dumps({'version':1,'kind':'event','event':{'event':'status','stream':'stdout',"
        "'data':base64.b64encode(b'actual partial check bytes').decode()}}),flush=True); os._exit(9)")
    process = argv_process.ManagedArgvProcess(command=[sys.executable, "-I", "-S", "-c", code])
    monkeypatch.setattr("lumi.engine.swarming.integration.ManagedArgvProcess", lambda: process)
    with pytest.raises(ExecutionGuardError):
        integration.run_check(authority, "candidate", "check")
    observed = integration.store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]
    assert process.cleanup_confirmed and process.result.stdout == b"actual partial check bytes"
    assert not process.result.output_complete and process.result.exit_code is None
    assert observed["state"] == "uncertain" and observed["exit_code"] is None
    assert "actual partial check bytes" in observed["output"] and "Check interrupted:" in observed["output"]
