"""Real desktop ownership, native child writes, TLS admission and verified Git."""
# ruff: noqa: F811 -- independent per-test PKI fixture is shared intentionally.

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import psycopg
import pytest

from lumi.engine.swarming.managed_client import HostChannelClient
from lumi.engine.swarming.managed_desktop import ManagedDesktop, ManagedDesktopConfig
from lumi.engine.swarming.models import Conflict, Scope
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime, _READ_TOOLS, _WRITE_TOOLS
from lumi.engine.swarming.store import canonical_json
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host
from test_managed_desktop_flow import certificates  # noqa: F401
from test_managed_host_client import configuration
from test_managed_native_runtime import until
from test_store import Tenant, uid
from tests.test_swarm_desktop_writers import request
from tests.test_swarm_integration import git


@pytest.fixture
def desktop(database_config, certificates, tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Owned arbitrary check trees require Windows named jobs")
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    kinds = ["writer_git", "candidate_git", "candidate_check", "checkout_apply"]
    tenant.store.command(tenant.admin, tenant.command(policy_version=2, allowed_effects=kinds,
        allowed_models=[{"provider": "ollama", "model": "chosen"}], allowed_tools=sorted(_READ_TOOLS | _WRITE_TOOLS), request_limit=20))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    resources, monitoring = ManagedResources(hosts), RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "value.txt").write_text("original\n")
    (project / "personal.txt").write_text("committed personal\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    source = Path(__file__).resolve().parents[3]
    script = tmp_path / "scripted-native-writer.py"
    script.write_text("import sys\n" + f"sys.path.insert(0,{str(source)!r})\n"
        "from lumi.engine.swarming.worker_child import main\n"
        "from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call\n"
        "def factory(spec):\n"
        " return StreamingBackend(name=spec.backend_type, model=spec.model, scripts=["
        "[tool_call('file_write',{'path':'src/value.txt','content':'verified change\\n'},'write-1'),done()],"
        "[text_delta('Isolated change ready for independent owner verification.'),done()]])\n"
        "raise SystemExit(main(backend_factory=factory))\n", encoding="utf-8")
    process_inputs = []
    class Process(ManagedWorkerProcess):
        def run(self, initial, **kwargs):
            process_inputs.append(json.dumps(initial))
            yield from super().run(initial, **kwargs)
    def runner(*args, **kwargs):
        kwargs["backend_factory"] = lambda _: pytest.fail("The parent must not run the scripted native provider")
        kwargs["writer_process_factory"] = lambda: Process(command=[sys.executable, str(script)], cancel_grace=.1)
        return SwarmWorkerRunner(*args, **kwargs)
    monkeypatch.setattr("lumi.engine.swarming.service.SwarmWorkerRunner", runner)
    project_id = hashlib.sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()
    personal = CapturedSession(Scope.personal("fixture-owner", project_id, "session"), str(project), BackendSpec("ollama", "chosen"))
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("swarming", None, {"version": 1, "enabled": True})
    with actual_host(hosts, certificates, monitoring=monitoring, resources=resources) as (url, server):
        transport = configuration(url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        managed = ManagedDesktop(ManagedDesktopConfig(transport, tenant.id, tenant.project, active["host_id"], active["host_generation"],
            tenant.admin.actor_id, personal.scope.owner_id, str(project), 1))
        service = SwarmRuntime(settings, state_root=lambda _: tmp_path / "state", managed_desktop=managed)
        capture = service.execution_capture(personal, "managed")
        env = SimpleNamespace(service=service, capture=capture, project=project, managed=managed, hosts=hosts,
            resources=resources, tenant=tenant, server=server, active=active, process_inputs=process_inputs,
            database=database_config, certificates=certificates, url=url)
        try:
            yield env
        finally:
            service.close()


def view(env):
    return env.service.operate(env.capture, {"request_id": uid(), "run_id": env.run_id})["run"]


def operate(env, action, **payload):
    return env.service.operate(env.capture, {"action": action, "request_id": uid(), "run_id": env.run_id,
        "expected_revision": view(env)["run"]["revision"], **payload})["run"]


def settled(env):
    until(lambda: all(row["state"] not in {"queued", "running"} for row in view(env)["integration_operations"]), timeout=20)
    return view(env)


def launch(env):
    env.run_id = env.service.operate(env.capture, request(execution_mode="managed"))["run"]["run"]["id"]
    until(lambda: len(view(env)["submissions"]) == 1, timeout=20)
    state = view(env)
    assert state["process_observations"][0]["state"] == "stopped"
    assert state["attempts"][0]["process_state"] == "stopped"
    assert len(env.process_inputs) == 1
    assert env.certificates.first.private_key_file not in env.process_inputs[0]
    assert env.url not in env.process_inputs[0]
    assert env.active["host_id"] not in env.process_inputs[0]
    return state


def prepare(env):
    state = launch(env)
    operate(env, "prepare_candidate", writer_ids=[state["writer_worktrees"][0]["id"]])
    state = settled(env)
    assert state["integration_candidates"][0]["state"] == "ready", state["integration_operations"]
    return state["integration_candidates"][0]


def remote_effects(env):
    with psycopg.connect(env.database["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
        return connection.execute("SELECT * FROM sonn_governance.owner_effects WHERE tenant_id=%s", (env.tenant.id,)).fetchall()


def test_managed_desktop_writer_checks_exact_candidate_preserves_dirty_checkout_and_requires_acceptance(desktop):
    env = desktop
    original = git(env.project, "rev-parse", "HEAD")
    candidate = prepare(env)
    assert (env.project / "src" / "value.txt").read_text() == "original\n"
    operate(env, "run_check", candidate_id=candidate["id"], check_key="value-check")
    state = settled(env)
    assert state["integration_candidates"][0]["state"] == "verified"
    assert state["integration_checks"][0]["candidate_revision"] == candidate["result_revision"]
    apply = {"candidate_id": candidate["id"], "expected_base": candidate["base_revision"],
             "target_revision": candidate["result_revision"], "evidence": "Owner reviewed exact managed candidate and named check"}
    (env.project / "personal.txt").write_text("User unfinished work\n")
    before = len(remote_effects(env))
    operate(env, "apply_candidate", **apply)
    assert settled(env)["integration_operations"][-1]["state"] == "failed"
    assert len(remote_effects(env)) == before and git(env.project, "rev-parse", "HEAD") == original
    assert (env.project / "personal.txt").read_text() == "User unfinished work\n"
    (env.project / "personal.txt").write_text("committed personal\n")
    operate(env, "apply_candidate", **apply)
    state = settled(env)
    assert state["integration_candidates"][0]["state"] == "applied"
    assert state["work_items"][0]["state"] == "submitted" and state["writer_acceptances"] == []
    with pytest.raises(Conflict, match="Completion"):
        operate(env, "complete")
    attempt = state["attempts"][0]
    operate(env, "accept_writer", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"], candidate_id=candidate["id"],
        evidence="Owner separately accepts the applied SHA and exact trusted check receipts")
    assert operate(env, "complete")["run"]["state"] == "completed"
    attachment = env.service._managed_attachments[env.run_id]
    effects = attachment.effects(env.service._store(env.capture))
    until(lambda: effects.flush(maximum=100)["unavailable"] is False and effects.inspect()["resume_ready"], timeout=10)
    remote = remote_effects(env)
    assert {row["kind"] for row in remote} == {"writer_git", "candidate_git", "candidate_check", "checkout_apply"}
    assert all(row["state"] == "completed" for row in remote)
    with effects._connection() as connection:
        local = connection.execute("SELECT remote_id,semantics FROM effects").fetchall()
    assert len(local) == len(remote) == len(state["integration_processes"])
    expected = {row["remote_id"]: hashlib.sha256(canonical_json(json.loads(row["semantics"])).encode()).hexdigest() for row in local}
    assert all(expected[str(row["effect_id"])] == row["semantics_sha256"] for row in remote)
    assert (env.project / "src" / "value.txt").read_text() == "verified change\n"


def test_revoked_owner_cannot_launch_next_check_and_local_stop_stays_available(desktop):
    env = desktop
    candidate = prepare(env)
    env.hosts.owner_command(env.tenant.admin, CommandEnvelope(1, uid(), env.tenant.id, env.tenant.project,
        env.active["revision"], "revoke_host", {"host_id": env.active["host_id"]}))
    before = len(remote_effects(env))
    operate(env, "run_check", candidate_id=candidate["id"], check_key="value-check")
    state = settled(env)
    assert state["integration_checks"][0]["state"] != "passed" and len(remote_effects(env)) == before
    assert state["integration_processes"][-1]["state"] == "not_started"
    started = time.monotonic()
    assert operate(env, "stop")["run"]["state"] == "cancelled"
    assert time.monotonic() - started < 1


def test_stop_during_blocked_central_effect_admission_never_launches_check(desktop, monkeypatch):
    env = desktop
    candidate = prepare(env)
    entered, release = threading.Event(), threading.Event()
    original = env.resources.authorize_effect
    def blocked(principal, **payload):
        if payload["kind"] == "candidate_check":
            entered.set()
            assert release.wait(5)
        return original(principal, **payload)
    monkeypatch.setattr(env.resources, "authorize_effect", blocked)
    operate(env, "run_check", candidate_id=candidate["id"], check_key="value-check")
    try:
        assert entered.wait(3)
        started = time.monotonic()
        assert operate(env, "stop")["run"]["state"] == "stopping"
        assert time.monotonic() - started < 1
    finally:
        release.set()
    state = settled(env)
    assert state["integration_checks"][0]["state"] != "passed"
    assert state["integration_processes"][-1]["state"] == "not_started"


def test_offline_desktop_cannot_spend_cached_policy_on_a_new_check(desktop):
    env = desktop
    candidate = prepare(env)
    before = len(remote_effects(env))
    env.server.shutdown()
    env.server.server_close()
    operate(env, "run_check", candidate_id=candidate["id"], check_key="value-check")
    state = settled(env)
    assert state["integration_checks"][0]["state"] == "cancelled"
    assert state["integration_processes"][-1]["state"] == "not_started"
    assert len(remote_effects(env)) == before
    started = time.monotonic()
    assert operate(env, "stop")["run"]["state"] == "cancelled"
    assert time.monotonic() - started < 1
