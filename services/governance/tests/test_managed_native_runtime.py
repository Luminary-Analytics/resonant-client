"""Actual encrypted host admissions around scripted native Session execution."""
# ruff: noqa: F811 -- shared certificate fixture.

from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace
import threading
import time

import psycopg
import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.managed_client import HostChannelClient, HostChannelError
from lumi.engine.swarming.managed_journal import ManagedBinding, ManagedJournal
from lumi.engine.swarming.managed_runtime import ManagedRuntime
from lumi.engine.swarming.managed_runtime import ManagedAdmissionError
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates as certificate_fixture
from test_managed_host_client import configuration
from test_monitoring import projection
from test_store import Tenant, uid
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


@pytest.fixture
def certificates(tmp_path_factory):
    # Each tenant needs an independently enrolled certificate: fingerprints are
    # globally unique and revocation never frees them for another identity.
    return certificate_fixture.__wrapped__(tmp_path_factory)


def local_command(env, kind, payload=None):
    revision = env.store.snapshot(env.authority.scope, env.authority.run_id)["run"]["revision"]
    return env.supervisor.handle(Command(uid(), env.authority.run_id, revision, env.authority.epoch,
                                        kind, payload or {}), env.authority)


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    assert predicate(), "Managed fixture did not reach its expected observation"


class Backend(StreamingBackend):
    def __init__(self, scripts=None):
        super().__init__(model="chosen", scripts=scripts or [[tool_call("file_read", {"path": "fact.txt"}), done()],
                                                           [text_delta("Observed the isolated fact."), done()]])
        self.calls = 0
        self.closed = False

    def stream(self, **kwargs):
        self.calls += 1
        yield from super().stream(**kwargs)

    def close(self):
        self.closed = True


@pytest.fixture
def native(database_config, certificates, tmp_path):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}], request_limit=10))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    resources, monitoring = ManagedResources(hosts), RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0,
        "enroll_host", {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Private exact fixture fact", encoding="utf-8")
    store = SwarmStore(tmp_path / "swarm.sqlite")
    supervisor = SwarmSupervisor(store)
    authority = supervisor.create(Scope(tenant.id, tenant.admin.actor_id, "local-project", "local-session"),
        supervisor_id="owner", objective="Read one scoped fact", request_limit=10,
        policy=PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama"})), lease_seconds=300)
    env = SimpleNamespace(tenant=tenant, hosts=hosts, resources=resources, monitoring=monitoring,
        store=store, supervisor=supervisor, authority=authority, workspace=workspace, runners=[], database_config=database_config)
    local_command(env, "plan", {"work_items": [{"id": "first", "objective": "Read fact", "read_roots": ["fact.txt"],
        "write_roots": [], "tools": ["file_read"], "criteria": ["fact"]}]})
    result = local_command(env, "assign", {"work_item_id": "first", "worker_id": "first", "requests": 3,
        "model": {"provider": "ollama", "model": "chosen"}}).result
    env.context = AttemptContext(authority.scope, authority.run_id, result["attempt_id"], "first", authority.epoch)
    with actual_host(hosts, certificates, monitoring=monitoring, resources=resources) as (url, server):
        env.client = HostChannelClient(configuration(url, certificates, certificates.first))
        env.active = env.client.activate(pending["challenge"])
        binding = ManagedBinding(url, certificates.first.fingerprint, tenant.id, tenant.project, env.active["host_id"],
            env.active["host_generation"], authority.scope.owner_id, authority.scope.project_id, authority.scope.session_id,
            authority.run_id, authority.epoch)
        env.journal = ManagedJournal(tmp_path / "managed.sqlite", binding)
        env.managed = ManagedRuntime(env.journal, env.client, policy_revision=1)
        env.server = server
        yield env
        for runner in env.runners:
            runner.close(timeout=3)


def launch(env, backend=None):
    backend = backend or Backend()
    runner = SwarmWorkerRunner(env.supervisor, env.authority, env.workspace,
        backend_factory=lambda _: backend, managed_runtime=env.managed)
    env.runners.append(runner)
    runner.start(env.context, BackendSpec("ollama", "chosen"))
    return runner, backend


def finish(env, runner):
    until(lambda: not runner.inspect(env.context.attempt_id)["alive"])
    return env.store.snapshot(env.authority.scope, env.authority.run_id)


def remote_rows(env, table):
    assert table in {"host_requests", "worker_slots", "tool_admissions", "request_bindings"}
    with psycopg.connect(env.database_config["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
        return connection.execute(f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s", (env.tenant.id,)).fetchall()


def test_native_read_is_bound_to_real_remote_request_and_single_action(native):
    runner, backend = launch(native)
    state = finish(native, runner)
    assert runner.inspect(native.context.attempt_id)["state"] == "submitted"
    assert backend.calls == 2 and backend.closed
    assert len(state["action_receipts"]) == 1 and state["action_receipts"][0]["state"] == "completed"
    assert {row["state"] for row in remote_rows(native, "host_requests")} == {"completed"}
    assert [row["state"] for row in remote_rows(native, "worker_slots")] == ["stopped"]
    actions, bindings = remote_rows(native, "tool_admissions"), remote_rows(native, "request_bindings")
    assert len(actions) == 1 and actions[0]["state"] == "completed" and len(bindings) == 2
    assert actions[0]["request_id"] in {row["request_id"] for row in bindings}
    assert native.journal.inspect()["pending_observations"] == 0
    assert b"Private exact fixture fact" not in native.journal.path.read_bytes()
    assert b"certificate_file" not in native.journal.path.read_bytes()


def test_tool_settles_its_exact_request_while_background_outbox_is_busy(native):
    """A held pump must not reorder a completed request behind its tool."""
    assert native.managed._outbox_lock.acquire(blocking=False)
    try:
        runner, backend = launch(native)
        state = finish(native, runner)
        assert backend.calls == 2 and state["submissions"], runner.inspect_all()
        actions = remote_rows(native, "tool_admissions")
        assert len(actions) == 1
        request = next(row for row in remote_rows(native, "host_requests")
                       if row["request_id"] == actions[0]["request_id"])
        assert request["state"] == "completed"
        local = next(row for row in native.journal.inspect()["requests"]
                     if row["remote_request_id"] == str(request["request_id"]))
        observation = native.journal.completed_request_observation(local["local_id"])
        assert observation["receipt"]["state"] == "completed"
        assert len(state["action_receipts"]) == 1 and state["action_receipts"][0]["state"] == "completed"
    finally:
        native.managed._outbox_lock.release()
    native.managed.flush()
    assert native.journal.inspect()["pending_observations"] == 0


def test_unknown_dependency_ack_cannot_admit_a_tool_or_repeat_generation(native, monkeypatch):
    original = native.client.settle
    calls = []

    def lose_reply(**payload):
        calls.append(payload)
        original(**payload)
        raise HostChannelError(delivery_unknown=True)

    monkeypatch.setattr(native.client, "settle", lose_reply)
    assert native.managed._outbox_lock.acquire(blocking=False)
    try:
        runner, backend = launch(native)
        state = finish(native, runner)
        assert backend.calls == 1 and len(calls) == 1
        assert not state["submissions"] and not state["action_receipts"]
        assert remote_rows(native, "tool_admissions") == []
        assert native.journal.inspect()["requests"][0]["outcome"] == "completed"
        assert native.journal.inspect()["pending_observations"] > 0
    finally:
        native.managed._outbox_lock.release()
    monkeypatch.setattr(native.client, "settle", original)
    native.managed.flush()
    assert native.journal.inspect()["pending_observations"] == 0
    assert backend.calls == 1 and remote_rows(native, "tool_admissions") == []


def test_lost_remote_start_reply_never_invokes_provider_or_replays_after_restart(native, monkeypatch):
    original, calls = native.hosts.start, []
    def lost(principal, request_id):
        calls.append(request_id)
        original(principal, request_id)
        raise RuntimeError("synthetic secret must never reach local journal")
    monkeypatch.setattr(native.hosts, "start", lost)
    runner, backend = launch(native)
    finish(native, runner)
    assert len(calls) == 1 and backend.calls == 0 and backend.closed
    row = native.journal.inspect()["requests"][0]
    assert row["outcome"] == "uncertain" and remote_rows(native, "host_requests")[0]["state"] == "uncertain"
    reopened = ManagedJournal(native.journal.path, native.journal.binding)
    reopened.fence_restart()
    assert not reopened.claim_dispatch(row["local_id"])
    assert b"synthetic secret" not in native.journal.path.read_bytes()


def test_offline_reserve_does_not_start_native_call(native, monkeypatch):
    def offline(*args, **kwargs):
        raise HostChannelError(delivery_unknown=True)
    monkeypatch.setattr(native.client, "reserve", offline)
    runner, backend = launch(native)
    finish(native, runner)
    assert backend.calls == 0
    assert native.journal.inspect()["requests"][0]["outcome"] == "uncertain"
    assert remote_rows(native, "host_requests") == []
    assert remote_rows(native, "worker_slots")[0]["state"] == "stopped"


def test_revocation_after_response_denies_next_tool_but_observes_completed_request(native):
    class Revoking(Backend):
        def stream(self, **kwargs):
            yield from super().stream(**kwargs)
            native.hosts.owner_command(native.tenant.admin, CommandEnvelope(1, uid(), native.tenant.id,
                native.tenant.project, native.active["revision"], "revoke_host", {"host_id": native.active["host_id"]}))
    runner, backend = launch(native, Revoking())
    state = finish(native, runner)
    assert backend.calls == 1 and state["action_receipts"] == []
    assert remote_rows(native, "host_requests")[0]["state"] == "completed"
    assert remote_rows(native, "tool_admissions") == []
    assert remote_rows(native, "worker_slots")[0]["state"] == "stopped"


@pytest.mark.parametrize("control", ["stop", "pause", "expire"])
def test_remote_tool_permit_cannot_bypass_local_stop_pause_or_elapsed_ttl(native, monkeypatch, control):
    admitted, release, calls = threading.Event(), threading.Event(), []
    original = native.resources.authorize_tool
    now = [time.monotonic()]
    native.managed.clock = lambda: now[0]
    def gate(principal, **kwargs):
        result = original(principal, **kwargs)
        calls.append(kwargs["action_id"])
        local_command(native, "stop" if control == "stop" else "pause")
        admitted.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(native.resources, "authorize_tool", gate)
    runner, backend = launch(native)
    try:
        assert admitted.wait(5)
        if control == "expire":
            now[0] += 61
        release.set()
        if control != "stop":
            until(lambda: runner.inspect(native.context.attempt_id)["state"] == "paused")
            runner.resume()
        state = finish(native, runner)
        assert len(calls) == 1
        if control == "pause":
            assert backend.calls == 2 and state["action_receipts"][0]["state"] == "completed"
        else:
            assert backend.calls == 1
            assert not any(row["state"] == "completed" for row in state["action_receipts"])
            assert remote_rows(native, "tool_admissions")[0]["state"] == "uncertain"
    finally:
        release.set()


def test_captured_scope_and_changed_model_cannot_use_managed_runtime(native):
    with pytest.raises(Exception, match="Managed admission"):
        native.managed._context(replace(native.context, scope=Scope.personal("other", "project", "session")))
    native.tenant.store.command(native.tenant.admin, native.tenant.command(expected=1,
        allowed_models=[{"provider": "ollama", "model": "replacement"}]))
    constructed = []
    runner = SwarmWorkerRunner(native.supervisor, native.authority, native.workspace,
        backend_factory=lambda _: constructed.append(True), managed_runtime=native.managed)
    native.runners.append(runner)
    runner.start(native.context, BackendSpec("ollama", "chosen"))
    finish(native, runner)
    assert constructed == [] and remote_rows(native, "worker_slots") == []


def test_configuration_owner_must_match_authenticated_host_enroller(native, tmp_path):
    assert native.active["owner_id"] == native.journal.binding.owner_id
    other = replace(native.journal.binding, owner_id="0" * 64)
    journal = ManagedJournal(tmp_path / "foreign-owner.sqlite", other)
    managed = ManagedRuntime(journal, native.client, policy_revision=1)
    with pytest.raises(ManagedAdmissionError):
        managed.register()
    assert managed.policy_view()["authenticated"] is False
    assert journal.inspect()["remote_binding_id"] is None
    assert remote_rows(native, "worker_slots") == []


def test_policy_view_does_not_wait_for_network_lease_refresh(native, monkeypatch):
    now = [time.monotonic()]
    native.managed.clock = lambda: now[0]
    native.managed._lease()
    now[0] += 61
    entered, release = threading.Event(), threading.Event()
    original, errors = native.client.lease, []
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(native.client, "lease", blocked)
    def refresh():
        try:
            native.managed._lease()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=refresh)
    thread.start()
    try:
        assert entered.wait(3)
        before = time.monotonic()
        view = native.managed.policy_view()
        assert time.monotonic() - before < .2
        assert view["authenticated"] and not view["valid"]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and errors == []


@pytest.mark.parametrize("stale", [False, True])
def test_remote_controls_apply_once_at_exact_local_revision(native, stale):
    runner = SwarmWorkerRunner(native.supervisor, native.authority, native.workspace,
        backend_factory=lambda _: pytest.fail("Control polling must not invoke a provider"), managed_runtime=native.managed)
    native.runners.append(runner)
    binding_id = native.managed.register()
    revision = native.store.snapshot(native.authority.scope, native.authority.run_id)["run"]["revision"]
    native.managed.report("before-control", projection(local_revision=revision))
    view = native.monitoring.inspect(native.tenant.admin, native.tenant.id, native.tenant.project)["runs"][0]
    control = native.monitoring.request_control(native.tenant.admin, native.tenant.id, native.tenant.project,
        binding_id=binding_id, command_id=uid(), expected_revision=view["revision"], expected_epoch=1,
        expected_local_revision=revision, operation="stop")
    if stale:
        local_command(native, "renew")
    result = native.managed.poll_controls(runner)
    assert result["applied"] == (0 if stale else 1)
    assert native.managed.poll_controls(runner)["applied"] == 0
    observed = native.monitoring.inspect(native.tenant.admin, native.tenant.id, native.tenant.project)["runs"][0]["controls"][0]
    assert observed["control_id"] == control["control_id"]
    assert observed["outcome"] == ("denied" if stale else "applied")
    assert observed["reported_processes_stopped"] is False
    if stale:
        assert native.store.snapshot(native.authority.scope, native.authority.run_id)["run"]["state"] == "running"


def test_lost_control_acknowledgement_cannot_dispatch_local_stop(native, monkeypatch):
    runner = SwarmWorkerRunner(native.supervisor, native.authority, native.workspace,
        backend_factory=lambda _: pytest.fail("No provider admission"), managed_runtime=native.managed)
    native.runners.append(runner)
    binding_id = native.managed.register()
    revision = native.store.snapshot(native.authority.scope, native.authority.run_id)["run"]["revision"]
    native.managed.report("before-control", projection(local_revision=revision))
    view = native.monitoring.inspect(native.tenant.admin, native.tenant.id, native.tenant.project)["runs"][0]
    native.monitoring.request_control(native.tenant.admin, native.tenant.id, native.tenant.project,
        binding_id=binding_id, command_id=uid(), expected_revision=view["revision"], expected_epoch=1,
        expected_local_revision=revision, operation="stop")
    original, received = native.monitoring.observe_control, []
    def lose(principal, **kwargs):
        result = original(principal, **kwargs)
        if kwargs["outcome"] == "received":
            received.append(kwargs["command_id"])
            raise RuntimeError("lost acknowledgement")
        return result
    monkeypatch.setattr(native.monitoring, "observe_control", lose)
    assert native.managed.poll_controls(runner)["applied"] == 0
    assert native.managed.poll_controls(runner)["applied"] == 0
    assert len(received) == 1
    assert native.store.snapshot(native.authority.scope, native.authority.run_id)["run"]["revision"] == revision
    assert native.journal.inspect()["controls"][0]["phase"] == "uncertain"


def test_shared_runtime_rejects_foreign_attempt_receipts(native):
    runner, _ = launch(native)
    state = finish(native, runner)
    foreign = replace(native.context, attempt_id="foreign-attempt", worker_id="foreign-worker")
    request_id = state["model_requests"][0]["id"]
    action_id = state["action_receipts"][0]["id"]
    for operation in (lambda: native.managed.claim_request(foreign, request_id),
                      lambda: native.managed.observe_request(foreign, request_id, outcome="uncertain"),
                      lambda: native.managed.claim_action(foreign, action_id),
                      lambda: native.managed.observe_action(foreign, action_id, outcome="uncertain")):
        with pytest.raises(ManagedAdmissionError):
            operation()
    assert native.journal.inspect()["pending_observations"] == 0


def test_expired_cached_lease_cannot_refresh_from_replayed_old_identity(native, monkeypatch):
    now = [time.monotonic()]
    native.managed.clock = lambda: now[0]
    assert native.managed.policy_view()["authenticated"] is False
    old, _ = native.managed._lease()
    assert native.managed.policy_view()["valid"]
    now[0] += 61
    assert not native.managed.policy_view()["valid"]
    monkeypatch.setattr(native.client, "lease", lambda *args, **kwargs: old)
    with pytest.raises(ManagedAdmissionError):
        native.managed._lease()
    assert not native.managed.policy_view()["valid"]


def test_local_permit_persistence_failure_prevents_provider(native, monkeypatch):
    def fail(*args):
        raise OSError("fixture receipt disk unavailable")
    monkeypatch.setattr(native.journal, "claim_dispatch", fail)
    runner, backend = launch(native)
    finish(native, runner)
    assert backend.calls == 0 and remote_rows(native, "host_requests")[0]["state"] == "uncertain"
    assert native.journal.inspect()["requests"][0]["outcome"] == "uncertain"


def test_pause_at_worker_claim_waits_without_repeating_remote_slot(native, monkeypatch):
    started, original = threading.Event(), native.supervisor.handle
    def pause(command, authority):
        receipt = original(command, authority)
        if command.kind == "worker_started":
            local_command(native, "pause")
            started.set()
        return receipt
    monkeypatch.setattr(native.supervisor, "handle", pause)
    runner, backend = launch(native)
    assert started.wait(5)
    until(lambda: runner.inspect(native.context.attempt_id)["state"] == "paused")
    assert backend.calls == 0 and len(remote_rows(native, "worker_slots")) == 1
    runner.resume()
    finish(native, runner)
    assert backend.calls == 2 and remote_rows(native, "worker_slots")[0]["state"] == "stopped"


@pytest.mark.skipif(os.name != "nt", reason="Named-job process cleanup qualification runs on Windows")
@pytest.mark.parametrize("cancel", [False, True])
def test_owned_child_uses_parent_admission_without_receiving_host_credentials(native, tmp_path, cancel):
    ready = tmp_path / "provider-entered"
    script = tmp_path / "managed-child.py"
    script.write_text("import sys, os, time\nfrom pathlib import Path\nsys.path.insert(0, os.getcwd())\n"
        "from lumi.engine.swarming.worker_child import main\n"
        "from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call\n"
        "class Backend(StreamingBackend):\n"
        " def stream(self, **kwargs):\n"
        f"  Path({str(ready)!r}).write_text('entered')\n"
        + ("  while True: time.sleep(.1)\n" if cancel else "  yield from super().stream(**kwargs)\n")
        + "def factory(spec):\n"
        " return Backend(name=spec.backend_type,model=spec.model,scripts=[[tool_call('file_read', {'path':'fact.txt'}),done()],"
        "[text_delta('Observed child fact.'),done()]])\nraise SystemExit(main(backend_factory=factory))\n", encoding="utf-8")
    class Process(ManagedWorkerProcess):
        def run(self, initial, **kwargs):
            serialized = json.dumps(initial)
            assert native.client.config.private_key_file not in serialized
            assert native.client.config.certificate_sha256 not in serialized
            assert native.journal.binding.host_id not in serialized
            yield from super().run(initial, **kwargs)
    runner = SwarmWorkerRunner(native.supervisor, native.authority, native.workspace,
        backend_factory=lambda _: pytest.fail("Owned child must create its own backend"), managed_runtime=native.managed,
        managed_readers=True, writer_process_factory=lambda: Process(command=[sys.executable, str(script)], cancel_grace=.1))
    native.runners.append(runner)
    runner.start(native.context, BackendSpec("ollama", "chosen"))
    until(ready.exists)
    if cancel:
        runner.stop()
    state = finish(native, runner)
    assert state["process_observations"][0]["state"] == "stopped"
    assert remote_rows(native, "worker_slots")[0]["state"] == "stopped"
    assert {row["state"] for row in remote_rows(native, "host_requests")} == {"uncertain" if cancel else "completed"}
    if cancel:
        assert not state["submissions"] and state["reservations"][0]["state"] == "uncertain"
    else:
        assert state["action_receipts"][0]["state"] == "completed" and state["submissions"]
