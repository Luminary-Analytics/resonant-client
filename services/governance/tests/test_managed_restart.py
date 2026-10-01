"""Killed native host, durable process proof and fresh enforced epoch over mTLS."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from lumi.engine.swarming import Scope, SwarmStore
from lumi.engine.swarming.managed_client import HostChannelClient, HostChannelError
from lumi.engine.swarming.managed_desktop import ManagedDesktop, ManagedDesktopConfig
from lumi.engine.swarming.managed_recovery import ManagedRecovery
from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.recovery import SwarmRecovery
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.processes import background_process_kwargs
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates as certificate_fixture
from test_managed_host_client import configuration
from test_managed_native_runtime import Backend, until
from test_store import Tenant, uid

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Real killed-parent named-job proof requires Windows")


@pytest.fixture
def certificates(tmp_path_factory):
    return certificate_fixture.__wrapped__(tmp_path_factory)


_HOST = '''import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from lumi.engine.swarming import Scope, SwarmStore, Command, AttemptContext
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.recovery import record_run_host
from lumi.engine.swarming.managed_client import HostChannelConfig
from lumi.engine.swarming.managed_desktop import ManagedDesktopConfig, ManagedDesktop
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
config=json.loads(Path(sys.argv[1]).read_text())
transport=HostChannelConfig(**config.pop('transport'))
mode=config.pop('mode'); root=Path(config.pop('state_root')); child=config.pop('child'); info=config.pop('info')
desktop=ManagedDesktop(ManagedDesktopConfig(transport=transport, **config))
workspace=Path(config['workspace'])
import hashlib
project=hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
scope=desktop.scope(Scope.personal(config['local_owner_id'],project,'saved-session'),str(workspace))
store=SwarmStore(root/'native.sqlite'); supervisor=SwarmSupervisor(store)
authority=supervisor.create(scope,supervisor_id='original-host',objective='Read fixture',request_limit=10,
 policy=PolicyProfile(1,frozenset({'file_read'}),frozenset({'ollama'})),lease_seconds=1)
record_run_host(store,authority)
def command(kind,payload):
 revision=store.snapshot(scope,authority.run_id)['run']['revision']
 return supervisor.handle(Command(str(revision),authority.run_id,revision,1,kind,payload),authority)
command('plan',{'work_items':[{'id':'first','objective':'Read fact','read_roots':['fact.txt'],
 'write_roots':[],'tools':['file_read'],'criteria':['fact']}]})
assigned=command('assign',{'work_item_id':'first','worker_id':'first','requests':3,
 'model':{'provider':'ollama','model':'chosen'}}).result
context=AttemptContext(scope,authority.run_id,assigned['attempt_id'],'first',1)
attachment=desktop.attach(authority,str(workspace),root)
if mode in ('worker_no_commit','worker_committed','request_no_commit'):
 method='reserve' if mode=='request_no_commit' else 'reserve_worker'
 original=getattr(attachment.runtime.client,method)
 def lost(*args,**kwargs):
  if mode=='worker_committed': original(*args,**kwargs)
  os._exit(93)
 setattr(attachment.runtime.client,method,lost)
if mode=='request_observed': attachment.runtime.observe_request=lambda *args,**kwargs: os._exit(93)
if mode=='action_observed': attachment.runtime.observe_action=lambda *args,**kwargs: os._exit(93)
runner=SwarmWorkerRunner(supervisor,authority,str(workspace),backend_factory=lambda _:None,
 managed_readers=True,managed_runtime=attachment.runtime,
 writer_process_factory=lambda:ManagedWorkerProcess(command=[sys.executable,'-I',child],cancel_grace=.1))
Path(info).write_text(json.dumps({'run_id':authority.run_id,'attempt_id':context.attempt_id,'scope':list(scope.values())}))
runner.start(context,BackendSpec('ollama','chosen'))
while True: time.sleep(.1)
'''


@pytest.fixture
def setup(database_config, certificates, tmp_path):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read"])
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}], request_limit=10))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Retained private recovery fact")
    root = tmp_path / "runtime"
    host_path, child_path = tmp_path / "host.py", tmp_path / "child.py"
    host_path.write_text(_HOST, encoding="utf-8")
    active_processes, recoveries, desktops, runners = [], [], [], []
    with actual_host(hosts, certificates, monitoring=RunMonitoring(hosts), resources=ManagedResources(hosts)) as (url, _):
        transport = configuration(url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        config = ManagedDesktopConfig(transport, tenant.id, tenant.project, active["host_id"], 1,
            tenant.admin.actor_id, "local-owner", str(workspace), 1)
        env = SimpleNamespace(tenant=tenant, hosts=hosts, config=config, root=root, workspace=workspace,
            database_config=database_config, runners=runners, desktops=desktops)
        def crash(mode="blocked"):
            ready, info = tmp_path / "entered", tmp_path / "info.json"
            child_path.write_text("import os,sys,time\nfrom pathlib import Path\nsys.path.insert(0,os.getcwd())\n"
                "from lumi.engine.swarming.worker_child import main\n"
                "from tests.streaming_stub import StreamingBackend,tool_call,text_delta,done\n"
                "class Backend(StreamingBackend):\n def stream(self,**kwargs):\n"
                f"  Path({str(ready)!r}).write_text('entered')\n"
                + ("  while True: time.sleep(.1)\n" if mode == "blocked" else "  yield from super().stream(**kwargs)\n")
                + "raise SystemExit(main(backend_factory=lambda spec:Backend(name=spec.backend_type,model=spec.model,"
                "scripts=[[tool_call('file_read',{'path':'fact.txt'}),done()],[text_delta('Read fact.'),done()]])))\n", encoding="utf-8")
            private = tmp_path / "launch.json"
            private.write_text(json.dumps({**asdict(config), "mode": mode, "state_root": str(root),
                "child": str(child_path), "info": str(info)}), encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-I", str(host_path), str(private)], cwd=Path(__file__).resolve().parents[3],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_process_kwargs())
            active_processes.append(process)
            if mode in {"worker_no_commit", "worker_committed", "request_no_commit"}:
                until(lambda: info.exists() and process.poll() is not None, timeout=12)
            else:
                until(ready.exists, timeout=12)
            if mode == "blocked":
                process.kill()
            assert process.wait(timeout=8) is not None
            identity = json.loads(info.read_text())
            scope = Scope(*identity["scope"])
            store = SwarmStore(root / "native.sqlite")
            until(lambda: store.snapshot(scope, identity["run_id"])["run"]["lease_until"] < time.time())
            recovery = SwarmRecovery(SwarmSupervisor(store), scope, identity["run_id"])
            recoveries.append(recovery)
            recovery.acquire(expected_epoch=1, lease_seconds=60)
            desktop = ManagedDesktop(config)
            desktops.append(desktop)
            managed = ManagedRecovery(desktop, recovery, workspace, root)
            managed.fence()
            env.store, env.recovery, env.desktop, env.managed = store, recovery, desktop, managed
            env.run_id, env.attempt_id, env.scope = identity["run_id"], identity["attempt_id"], scope
            return env
        env.crash = crash
        yield env
        for runner in runners:
            runner.close(timeout=3)
        for recovery in recoveries:
            recovery.close()
        for desktop in desktops:
            desktop.close()
        for process in active_processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def rows(env, table):
    assert table in {"host_requests", "worker_slots", "tool_admissions", "managed_runs"}
    with psycopg.connect(env.database_config["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
        return connection.execute(f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s", (env.tenant.id,)).fetchall()


def native_decision(env, kind, payload):
    revision = env.store.snapshot(env.scope, env.run_id)["run"]["revision"]
    return env.recovery.command(kind, payload, command_id=uid(), expected_revision=revision)


def desktop_service(env):
    settings = SettingsManager(env.root / "desktop-settings.json")
    settings.set("swarming", None, {"version": 1, "enabled": True})
    service = SwarmRuntime(settings, backend_factory=lambda _: Backend(), state_root=lambda _: env.root,
                           managed_desktop=env.desktop)
    capture = CapturedSession(env.scope, str(env.workspace), BackendSpec("ollama", "chosen"))
    service._stores[os.path.normcase(str(env.workspace.resolve()))] = env.store
    service._recoveries[env.run_id] = (capture, env.recovery)
    service._managed_recoveries[env.run_id] = env.managed
    # The killed fixture uses the public runner directly. Capture the same
    # desktop setup contract before testing the service's continuation seam.
    with env.store._connection(write=True) as connection:
        env.store._remember(connection, env.run_id, "desktop-setup", uid(),
            {"model": {"provider": "ollama", "model": "chosen"}, "worker_requests": 3, "write_roots": []},
            {"run_id": env.run_id})
    def operate(action, **fields):
        return service.operate(capture, {"action": action, "request_id": uid(), "run_id": env.run_id,
            "expected_revision": env.store.snapshot(env.scope, env.run_id)["run"]["revision"], **fields})
    return service, capture, operate


def test_desktop_recovery_derives_proofs_then_resumes_with_new_managed_runtime(setup):
    env = setup.crash()
    service, capture, operate = desktop_service(env)
    try:
        with pytest.raises(ValueError, match="identities only"):
            operate("managed_reconcile_worker", managed_epoch=1, attempt_id=env.attempt_id, outcome="stopped")
        first = operate("managed_recovery_inspect", managed_epoch=1, kind="workers", limit=1)
        assert first["run"]["managed_recovery"]["kind"] == "workers"
        operate("managed_reconcile_worker", managed_epoch=1, attempt_id=env.attempt_id)
        until(lambda: operate("view")["run"]["managed_recovery"]["operation"]["state"] == "finished")
        request = env.store.snapshot(env.scope, env.run_id)["model_requests"][0]
        operate("reconcile_request", model_request_id=request["id"], outcome="failed", used=1,
                evidence="Observed killed host with exact outstanding request; usage remains consumed.")
        operate("managed_reconcile_request", managed_epoch=1, model_request_id=request["id"])
        until(lambda: operate("view")["run"]["managed_recovery"]["operation"]["state"] == "finished")
        assert operate("view")["run"]["managed_recovery"]["resume_ready"]
        result = operate("continue_recovered", retry_work_items=["first"], worker_requests=3)
        runner = service._runners[env.run_id][1]
        assert result["execution_mode"] == "managed" and not result["run"]["recovery_needed"]
        assert service._managed_attachments[env.run_id].runtime.binding.epoch == 2
        until(lambda: len(env.store.snapshot(env.scope, env.run_id)["submissions"]) == 1)
        until(lambda: runner.inspect_all() and all(not row["alive"] for row in runner.inspect_all()))
        service._managed_attachments[env.run_id].runtime.flush()
        assert len(rows(env, "managed_runs")) == 2
        assert len(rows(env, "host_requests")) == 3
        assert all(row["state"] == "stopped" for row in rows(env, "worker_slots"))
    finally:
        service.close()


def test_desktop_stop_remains_live_while_recovery_http_is_blocked(setup, monkeypatch):
    env = setup.crash()
    service, capture, operate = desktop_service(env)
    entered, release = threading.Event(), threading.Event()
    original = env.desktop._client.observe_worker
    def blocked(**payload):
        entered.set()
        assert release.wait(8)
        return original(**payload)
    monkeypatch.setattr(env.desktop._client, "observe_worker", blocked)
    try:
        started = time.monotonic()
        result = operate("managed_reconcile_worker", managed_epoch=1, attempt_id=env.attempt_id)
        assert time.monotonic() - started < 1
        assert result["run"]["managed_recovery"]["operation"]["state"] == "running"
        assert entered.wait(5)
        assert not operate("view")["run"]["managed_recovery"]["resume_ready"]
        started = time.monotonic()
        stopped = operate("stop")
        assert time.monotonic() - started < 1
        assert stopped["run"]["run"]["stop_requested"]
        assert len(rows(env, "managed_runs")) == 1
    finally:
        release.set()
        until(lambda: service._managed_recovery_operations[env.run_id]["state"] != "running")
        service.close()


@pytest.mark.parametrize("mode", ["worker_no_commit", "request_no_commit"])
def test_explicit_server_fence_resolves_lost_uncommitted_admission_and_denies_late_send(setup, mode):
    env = setup.crash(mode)
    epoch_journal = env.managed._journals[1]
    kind = "workers" if mode == "worker_no_commit" else "requests"
    local_id = env.attempt_id if kind == "workers" else env.store.snapshot(env.scope, env.run_id)["model_requests"][0]["id"]
    key = epoch_journal._worker_key(local_id, 1) if kind == "workers" else local_id
    intent = epoch_journal.absence_intent("worker" if kind == "workers" else "request", key)
    result = env.managed.fence_absent(1, kind, local_id)
    assert result["state"] == "fenced_absent"
    env.managed.fence_absent(1, kind, local_id)  # Immutable reply replay conveys no permit.
    if kind == "workers":
        assert env.managed.inspect()["worker_cleanup_pending"] == 0
        with pytest.raises(HostChannelError):
            env.desktop._client.reserve_worker(lease_id=intent["lease_id"], binding_id=epoch_journal.inspect()["remote_binding_id"],
                                               worker_id=intent["resource_id"], kind="worker")
    else:
        assert env.managed.inspect()["unknown_request_units"] == 0
        with pytest.raises(HostChannelError):
            env.desktop._client.reserve(lease_id=intent["lease_id"], request_id=intent["resource_id"])
        env.managed.reconcile_worker(1, env.attempt_id)
    native = env.recovery.reconcile_process(env.attempt_id)
    assert native["termination_recorded"]
    for request in env.store.snapshot(env.scope, env.run_id)["model_requests"]:
        if request["state"] == "uncertain":
            native_decision(env, "reconcile_request", {"request_id": request["id"], "outcome": "not_started", "used": 0,
                "evidence": "Original request reserve was atomically fenced absent; no remote start was attempted or claimed."})
    assert env.managed.prepare_resume().runtime.binding.epoch == 2
    assert rows(env, "host_requests") == []


def test_present_admission_is_never_refunded_by_absence_check(setup):
    env = setup.crash("worker_committed")
    with pytest.raises(Exception, match="did not prove"):
        env.managed.fence_absent(1, "workers", env.attempt_id)
    assert env.managed.inspect()["worker_cleanup_pending"] == 1
    assert rows(env, "worker_slots")[0]["state"] == "held"
    env.managed.reconcile_worker(1, env.attempt_id)
    assert rows(env, "worker_slots")[0]["state"] == "never_started"


@pytest.mark.parametrize("mode", ["blocked", "request_observed", "action_observed"])
def test_killed_host_reconciles_exact_native_evidence_then_dispatches_new_enforced_epoch(setup, mode):
    env = setup.crash(mode)
    assert env.managed.inspect()["worker_cleanup_pending"] == 1
    assert rows(env, "worker_slots")[0]["state"] == "held"
    with pytest.raises(Conflict):
        env.managed.prepare_resume()
    stopped = env.managed.reconcile_worker(1, env.attempt_id)
    assert stopped["outcome"] == "stopped" and rows(env, "worker_slots")[0]["state"] == "stopped"
    snapshot = env.store.snapshot(env.scope, env.run_id)
    request = snapshot["model_requests"][0]
    if mode == "blocked":
        with pytest.raises(Conflict, match="native request outcome"):
            env.managed.reconcile_request(1, request["id"])
        native_decision(env, "reconcile_request", {"request_id": request["id"], "outcome": "failed", "used": 1,
            "evidence": "Operator records consumed failed invocation after exact killed-host observation; no output success inferred"})
    env.managed.reconcile_request(1, request["id"])
    if mode == "action_observed":
        env.managed.reconcile_action(1, snapshot["action_receipts"][0]["id"])
        assert rows(env, "tool_admissions")[0]["state"] == "completed"
    assert rows(env, "host_requests")[0]["state"] == ("failed" if mode == "blocked" else "completed")
    before = env.managed.inspect()
    assert before["resume_ready"]
    attachment = env.managed.prepare_resume()
    assert attachment.runtime.binding.epoch == 2
    assert attachment.runtime.journal.inspect()["remote_binding_id"] is None
    assert len(rows(env, "managed_runs")) == 1  # Preparation itself never contacts governance.
    native_decision(env, "recover", {"retry_work_items": ["first"]})
    authority = env.recovery.authority
    from lumi.engine.swarming import AttemptContext, Command
    revision = env.store.snapshot(env.scope, env.run_id)["run"]["revision"]
    assigned = env.recovery.supervisor.handle(Command(uid(), env.run_id, revision, 2, "assign", {"work_item_id": "first",
        "worker_id": "replacement", "requests": 3, "model": {"provider": "ollama", "model": "chosen"}}), authority).result
    context = AttemptContext(env.scope, env.run_id, assigned["attempt_id"], "replacement", 2)
    runner = SwarmWorkerRunner(env.recovery.supervisor, authority, env.workspace, backend_factory=lambda _: Backend(),
                               managed_runtime=attachment.runtime)
    env.runners.append(runner)
    runner.start(context, BackendSpec("ollama", "chosen"))
    until(lambda: not runner.inspect(context.attempt_id)["alive"])
    assert runner.inspect(context.attempt_id)["state"] == "submitted"
    assert len(rows(env, "managed_runs")) == 2 and len(rows(env, "host_requests")) == 3
    assert {row["state"] for row in rows(env, "worker_slots")} == {"stopped"}


def test_unacknowledged_slot_cleanup_blocks_new_epoch_without_refund_or_lease(setup, monkeypatch):
    env = setup.crash()
    def offline(**kwargs):
        raise HostChannelError(delivery_unknown=True)
    monkeypatch.setattr(env.desktop._client, "observe_worker", offline)
    env.managed.reconcile_worker(1, env.attempt_id)
    request = env.store.snapshot(env.scope, env.run_id)["model_requests"][0]
    native_decision(env, "reconcile_request", {"request_id": request["id"], "outcome": "failed", "used": 1,
                                              "evidence": "Explicit consumed failure after host termination"})
    with pytest.raises(Conflict, match="acknowledged cleanup"):
        env.managed.prepare_resume()
    assert len(rows(env, "managed_runs")) == 1
    assert rows(env, "host_requests")[0]["state"] == "uncertain"


def test_foreign_epoch_and_altered_input_cannot_supply_recovery_evidence(setup):
    env = setup.crash("request_observed")
    request = env.store.snapshot(env.scope, env.run_id)["model_requests"][0]
    with pytest.raises(ScopeDenied):
        env.managed.reconcile_request(2, request["id"])
    with env.store._connection(write=True) as connection:
        connection.execute("UPDATE request_inputs SET input_sha256=? WHERE request_id=?", ("0" * 64, request["id"]))
    with pytest.raises(ScopeDenied, match="identity differs"):
        env.managed.reconcile_request(1, request["id"])
    assert rows(env, "host_requests")[0]["state"] == "started"


def test_local_resolution_can_continue_with_remote_unknown_unit_explicitly_held(setup):
    env = setup.crash()
    env.managed.reconcile_worker(1, env.attempt_id)
    request = env.store.snapshot(env.scope, env.run_id)["model_requests"][0]
    native_decision(env, "reconcile_request", {"request_id": request["id"], "outcome": "failed", "used": 1,
                                              "evidence": "Explicitly consume failed invocation; remote observation remains unknown"})
    view = env.managed.inspect()
    assert view["resume_ready"] and view["unknown_request_units"] == 1
    assert view["records"][0]["native_state"] == "failed" and view["records"][0]["identity_matches"]
    attachment = env.managed.prepare_resume()
    assert attachment.runtime.binding.epoch == 2
    remote = rows(env, "host_requests")[0]
    assert remote["state"] == "uncertain" and remote["consumed"] is None
    assert attachment.runtime.policy_view()["authenticated"] is False


def test_resume_checks_all_slots_beyond_bounded_history_page(setup):
    env = setup.crash("request_observed")
    env.managed.reconcile_worker(1, env.attempt_id)
    request = env.store.snapshot(env.scope, env.run_id)["model_requests"][0]
    env.managed.reconcile_request(1, request["id"])
    journal = env.managed._journals[1]
    lease = journal.recovery_records("workers")["records"][0]["lease"]
    # Represent retained partial remote slot sends. Missing local process proof
    # must not be hidden merely because the browser displays 100 rows at a time.
    for index in range(101):
        journal.worker(f"retained-{index:03}", 1)
        journal.begin_worker(f"retained-{index:03}", 1, lease)
    page = env.managed.inspect(kind="workers")
    assert len(page["records"]) == 100 and page["next_cursor"] is not None
    assert page["worker_cleanup_pending"] == 101
    with pytest.raises(Conflict, match="acknowledged cleanup"):
        env.managed.prepare_resume()
