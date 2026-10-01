"""OS-backed integration recovery without success inference or PID-only kills."""

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import psutil
import pytest

from lumi.engine.swarming import Command, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.integration_processes import IntegrationProcesses
from lumi.engine.swarming.models import Conflict, IdempotencyConflict, ScopeDenied, StaleAuthority
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.processes import job_name
from lumi.engine.swarming.workflow import IntegrationWorkflow
from lumi.processes import background_process_kwargs, close_windows_job, windows_kill_job


@pytest.fixture
def fixture(tmp_path):
    now = [1000.0]
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset(), allowed_providers=frozenset({"ollama"}),
                           read_roots=(".",), write_roots=())
    authority = supervisor.create(scope, supervisor_id="first", objective="Recovery fixture", request_limit=10, policy=policy)
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,result_revision,state,manifest_json,process_protocol) "
                           "VALUES('candidate',?,1,'repo',?,'base','result','ready','{}',1)", (authority.run_id, str(tmp_path)))
        connection.execute("INSERT INTO integration_checks(id,candidate_id,check_key,candidate_revision,argv_json,state,process_protocol) "
                           "VALUES('check','candidate','proof','result','[]','running',1)")
    observations = IntegrationProcesses(store, host_id="fixture-host")

    def takeover():
        now[0] += 31
        return supervisor.acquire(scope, authority.run_id, expected_epoch=1, supervisor_id="replacement", command_id="takeover")

    return store, supervisor, authority, observations, takeover, tmp_path


@pytest.fixture
def helper():
    token = uuid.uuid4().hex
    process = subprocess.Popen([sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
                               **background_process_kwargs(new_process_group=True))
    job = windows_kill_job(process, name=job_name(token))
    captured = SimpleNamespace(pid=process.pid, created_at=psutil.Process(process.pid).create_time(),
                               launch_token=token, cleanup_confirmed=False, exit_code=None)

    def stop():
        nonlocal job
        if job:
            close_windows_job(job)
            job = None
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        captured.cleanup_confirmed, captured.exit_code = True, process.returncode

    try:
        yield captured, stop
    finally:
        stop()


def intent(fixture, *, kind="check", identity="check"):
    store, supervisor, authority, observations, takeover, path = fixture
    return observations.intent(authority, kind, identity, [sys.executable, "-c", "pass"], path)


def test_known_never_invoked_intent_is_fenced_and_not_replayed(fixture):
    store, supervisor, authority, observations, takeover, path = fixture
    identity = intent(fixture)
    replacement = takeover()
    result = observations.reconcile_effect(replacement, "check", "check", evidence="Inspect the gated intent")
    assert result["effect_state"] == "not_started"
    assert result["invocation_observed"] is False
    with store._connection() as connection:
        assert connection.execute("SELECT state,invoked FROM integration_processes WHERE id=?", (identity,)).fetchone()[:] == ("not_started", 0)
    with pytest.raises(StaleAuthority):
        observations.invoke(authority, identity)
    assert store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]["exit_code"] is None


@pytest.mark.skipif(os.name != "nt", reason="Orphan arbitrary-argv containment requires Windows named jobs")
def test_live_helper_blocks_recovery_until_actual_os_termination_never_yields_pass(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    replacement = takeover()
    observed = observations.inspect_effect(authority.scope, authority.run_id, "check", "check")
    assert observed["processes"][0]["observation"] == "running"
    assert "launch_token" not in observed["processes"][0]
    with pytest.raises(Conflict, match="live"):
        observations.reconcile_effect(replacement, "check", "check", evidence="Owner thinks it passed")
    stop()
    # Reopen without the original Popen or job handle: use durable identity and
    # actual OS evidence, not the helper fixture's in-memory cleanup assertion.
    reopened = SwarmStore(store.path, clock=store.clock)
    fresh_observer = IntegrationProcesses(reopened, host_id="fixture-host")
    result = fresh_observer.reconcile_effect(replacement, "check", "check", evidence="Inspect retained host identity")
    assert result["effect_state"] == "cancelled"
    snapshot = reopened.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_processes"][0]["state"] == "stopped"
    assert snapshot["integration_checks"][0]["exit_code"] is None
    assert snapshot["integration_candidates"][0]["state"] == "failed"


def test_actual_cleanup_can_be_recorded_after_stop_and_preserves_observed_check_output(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    with store._connection(write=True) as connection:
        # Simulate a retained full trusted check receipt, separate from process
        # identity. Recovery must retain this evidence rather than synthesize it.
        connection.execute("UPDATE integration_checks SET state='failed',exit_code=7,output='exact failed assertion' WHERE id='check'")
        connection.execute("UPDATE integration_candidates SET state='failed' WHERE id='candidate'")
    replacement = takeover()
    stop()
    observations.stopped(authority.scope, authority.run_id, identity, process)
    result = observations.reconcile_effect(replacement, "check", "check", evidence="Observe recorded cleanup")
    assert result["effect_state"] == "failed"
    check = store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]
    assert (check["exit_code"], check["output"]) == (7, "exact failed assertion")


def test_missing_legacy_protocol_or_foreign_host_remains_unknown(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    replacement = takeover()
    foreign = IntegrationProcesses(store, host_id="another-host")
    assert foreign.inspect_effect(authority.scope, authority.run_id, "check", "check")["processes"][0]["observation"] == "unknown"
    with pytest.raises(Conflict):
        foreign.reconcile_effect(replacement, "check", "check", evidence="Unavailable remote host")
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_checks SET process_protocol=0 WHERE id='check'")
    stop()
    with pytest.raises(Conflict, match="protocol"):
        observations.reconcile_effect(replacement, "check", "check", evidence="Even an absent process is insufficient for legacy intent")
    assert store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]["state"] == "uncertain"


def test_owned_helper_without_invocation_still_requires_cleanup(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    with pytest.raises(Conflict):
        observations.not_started(authority.scope, authority.run_id, identity)
    replacement = takeover()
    with pytest.raises(Conflict):
        observations.reconcile_effect(replacement, "check", "check", evidence="Await helper EOF cleanup")
    stop()
    result = observations.reconcile_effect(replacement, "check", "check", evidence="Observe helper ended")
    assert result["effect_state"] == "not_started"
    assert result["invocation_observed"] is False


def test_cleanup_after_lost_ownership_commit_retains_real_identity_without_invocation(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    replacement = takeover()
    with pytest.raises(StaleAuthority):
        observations.owned(authority, identity, process)
    stop()
    observations.stopped(authority.scope, authority.run_id, identity, process)
    result = observations.reconcile_effect(replacement, "check", "check", evidence="No argv passed the fenced gate")
    assert result["effect_state"] == "not_started"
    observed = store.snapshot(authority.scope, authority.run_id)["integration_processes"][0]
    assert observed["pid"] == process.pid
    assert observed["invoked"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Orphan arbitrary-argv containment requires Windows named jobs")
def test_application_outcome_stays_unknown_after_process_cleanup(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_checks SET state='failed' WHERE id='check'")
        connection.execute("INSERT INTO integration_applications(id,candidate_id,expected_base,target_revision,state,approval_json,process_protocol) "
                           "VALUES('apply','candidate','base','result','applying','{}',1)")
    process, stop = helper
    identity = intent(fixture, kind="application", identity="apply")
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    replacement = takeover()
    stop()
    result = observations.reconcile_effect(replacement, "application", "apply", evidence="Process gone; inspect Git refs separately")
    assert result["effect_state"] == "uncertain"
    with store._connection() as connection:
        assert supervisor._integration_unresolved(connection, authority.run_id)


def test_wrong_scope_identity_and_current_epoch_cannot_reconcile(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    identity = intent(fixture)
    with pytest.raises(Conflict, match="historical"):
        observations.reconcile_effect(authority, "check", "check", evidence="Not a recovery decision")
    with pytest.raises(ScopeDenied):
        observations.inspect_effect(replace(authority.scope, owner_id="foreign"), authority.run_id, "check", "check")
    process, stop = helper
    wrong = SimpleNamespace(**vars(process))
    wrong.created_at += 2
    with pytest.raises(Conflict, match="identity"):
        observations.owned(authority, identity, wrong)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    with pytest.raises(Conflict):
        observations.invoke(authority, identity)
    with pytest.raises(Conflict):
        observations.stopped(authority.scope, authority.run_id, identity, process)
    stop()
    wrong = SimpleNamespace(**vars(process))
    wrong.launch_token = uuid.uuid4().hex
    with pytest.raises(ScopeDenied):
        observations.stopped(authority.scope, authority.run_id, identity, wrong)


def test_unknown_process_blocks_stop_even_when_effect_receipt_is_terminal(fixture, helper):
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_checks SET state='cancelled' WHERE id='check'")
        connection.execute("UPDATE integration_candidates SET state='failed' WHERE id='candidate'")
    result = supervisor.handle(Command("stop", authority.run_id, 0, authority.epoch, "stop", {}), authority)
    assert result.state == "stopping"
    stop()
    observations.stopped(authority.scope, authority.run_id, identity, process)
    assert store.snapshot(authority.scope, authority.run_id)["run"]["state"] == "cancelled"


def test_posix_group_absence_does_not_prove_arbitrary_argv_descendants_ended(fixture, helper, monkeypatch):
    from lumi.engine.swarming import integration_processes
    store, supervisor, authority, observations, takeover, path = fixture
    process, stop = helper
    identity = intent(fixture)
    observations.owned(authority, identity, process)
    observations.invoke(authority, identity)
    replacement = takeover()
    stop()
    monkeypatch.setattr(integration_processes, "os", SimpleNamespace(name="posix"))
    with pytest.raises(Conflict, match="cleanup"):
        observations.reconcile_effect(replacement, "check", "check", evidence="An escaped session cannot be ruled out")
    assert store.snapshot(authority.scope, authority.run_id)["integration_checks"][0]["state"] == "uncertain"


def test_workflow_owner_observation_is_exact_scoped_idempotent_and_accepts_no_outcome(fixture):
    store, supervisor, authority, observations, takeover, path = fixture
    # This protocol-covered check stopped before it even created a helper.
    replacement = takeover()
    workflow = IntegrationWorkflow(supervisor, replacement, SimpleNamespace(
        store=store, repo_key="repo", waiting_for_repository=lambda thread: False))
    revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    payload = {"effect_kind": "check", "effect_id": "check", "evidence": "Inspect never-invoked check"}
    try:
        with pytest.raises(ValueError):
            workflow.submit("reconcile_effect", {**payload, "outcome": "passed"}, command_id="forged", expected_revision=revision)
        receipt = workflow.submit("reconcile_effect", payload, command_id="observe", expected_revision=revision)
        deadline = time.monotonic() + 30
        while workflow.inspect(receipt["id"])["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.01)
        assert workflow.inspect(receipt["id"])["state"] == "completed"
        assert workflow.submit("reconcile_effect", payload, command_id="observe", expected_revision=revision)["id"] == receipt["id"]
        with pytest.raises(IdempotencyConflict):
            workflow.submit("reconcile_effect", {**payload, "evidence": "Different decision"}, command_id="observe", expected_revision=revision)
        snapshot = store.snapshot(authority.scope, authority.run_id)
        assert snapshot["integration_checks"][0]["state"] == "not_started"
        assert len(snapshot["integration_operations"]) == 1
        assert snapshot["integration_processes"] == []
    finally:
        workflow.close()


@pytest.mark.skipif(os.name != "nt", reason="Actual host-crash containment uses a Windows named job")
def test_killed_host_leaves_durable_invocation_and_orphan_tree_is_observed_after_restart(fixture):
    store, supervisor, authority, observations, takeover, path = fixture
    marker = path / "effect-pids.json"
    target = ("import os,subprocess,sys,time,json; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(600)']); "
              f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(600)")
    argv = [sys.executable, "-I", "-S", "-c", target]
    source = Path(__file__).resolve().parents[1]
    # A separate real application host dies without executing its cleanup
    # callback. Only the OS-owned named job can stop its effect descendants.
    host_code = f"""
import sys
sys.path.insert(0, {str(source)!r})
from lumi.engine.swarming import Scope, RunAuthority, SwarmStore
from lumi.engine.swarming.integration_processes import IntegrationProcesses
from lumi.engine.swarming.argv_process import ManagedArgvProcess
store = SwarmStore({str(store.path)!r}, clock=lambda: 1000.0)
authority = RunAuthority(Scope.personal('owner','project','session'), {authority.run_id!r}, 'first', 1)
observations = IntegrationProcesses(store, host_id='fixture-host')
identity = observations.intent(authority, 'check', 'check', {argv!r}, {str(path)!r})
process = ManagedArgvProcess()
def admitted(child):
    observations.owned(authority, identity, child)
    observations.invoke(authority, identity)
process.execute({argv!r}, {str(path)!r}, timeout_seconds=900, on_started=admitted)
observations.stopped(authority.scope, authority.run_id, identity, process)
"""
    host = subprocess.Popen([sys.executable, "-c", host_code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            **background_process_kwargs(new_process_group=True))
    children = []
    try:
        deadline = time.monotonic() + 60  # the host, its gate and the target start first
        while time.monotonic() < deadline:
            if host.poll() is not None:
                pytest.fail(f"Fixture host exited before its target ran: {host.stderr.read().decode(errors='replace')}")
            try:
                pids = json.loads(marker.read_text())
                children = [(psutil.Process(pid), psutil.Process(pid).create_time()) for pid in pids]
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.02)
        assert len(children) == 2
        original = store.snapshot(authority.scope, authority.run_id)["integration_processes"][0]
        assert original["state"] == "invoked" and original["invoked"] == 1
        host.kill()
        host.wait(timeout=5)
        # No original Python finally or check completion receipt executed.
        assert store.snapshot(authority.scope, authority.run_id)["integration_processes"][0]["state"] == "invoked"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            inspected = observations.inspect_effect(authority.scope, authority.run_id, "check", "check")
            if inspected["processes"][0]["observation"] == "stopped":
                break
            time.sleep(.02)
        assert inspected["processes"][0]["observation"] == "stopped"
        for child, created in children:
            assert not child.is_running() or child.create_time() != created or child.status() == psutil.STATUS_ZOMBIE
        replacement = takeover()
        reopened = SwarmStore(store.path, clock=store.clock)
        observed = IntegrationProcesses(reopened, host_id="fixture-host").reconcile_effect(
            replacement, "check", "check", evidence="Fresh host inspected the captured named-job identity")
        assert observed["effect_state"] == "cancelled"
        final = reopened.snapshot(authority.scope, authority.run_id)
        assert final["integration_checks"][0]["exit_code"] is None
        assert final["integration_checks"][0]["output"] == ""
        assert final["integration_processes"][0]["state"] == "stopped"
    finally:
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)
        host.stderr.close()
        # Failure cleanup is limited to the exact fixture children observed
        # before the host was killed, never an arbitrary supplied PID.
        for child, created in children:
            try:
                if child.is_running() and child.create_time() == created:
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
