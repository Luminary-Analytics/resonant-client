"""Two captured desktop services use actual managed sharing HTTP and native work."""
# ruff: noqa: F811 -- shared certificate fixture.

import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace
import threading
import time

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.managed_collaboration_desktop import historical_view
from lumi.engine.swarming.collaboration_desktop import TOOLS
from lumi.engine.swarming.managed_client import HostChannelClient
from lumi.engine.swarming.managed_desktop import ManagedDesktop, ManagedDesktopConfig
from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from sonn_governance.content import ContentKeys
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_collaboration import ManagedCollaboration
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope, Principal
from sonn_governance.monitoring import RunMonitoring
from test_host_http import certificates as certificate_fixture
from test_managed_collaboration import policy, terms
from test_managed_collaboration_native import actual_sharing_host
from test_managed_host_client import configuration
from test_managed_native_runtime import Backend, until
from test_store import Tenant, uid


@pytest.fixture
def certificates(tmp_path_factory):
    return certificate_fixture.__wrapped__(tmp_path_factory)


@pytest.fixture
def pair(database_config, certificates, tmp_path):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin"])
    tenant.store.command(tenant.admin, tenant.command(allowed_tools=sorted(TOOLS), allowed_models=[{"provider": "ollama", "model": "chosen"}],
                                                    content_mode="explicit", request_limit=20))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    sharing = ManagedCollaboration(hosts, ContentKeys({"test": b"d" * 32}, "test"))
    sharing.set_policy(tenant.admin, tenant_id=tenant.id, project_id=tenant.project, command_id=uid(), expected_revision=0, policy=policy())
    monitor, resources = RunMonitoring(hosts), ManagedResources(hosts)
    owners = [Principal("https://fixture-owner.example", uid(), time.time() + 3600) for _ in range(2)]
    services = []
    with actual_sharing_host(hosts, certificates, sharing, monitor, resources) as (url, server):
        try:
            for index, (owner, certificate) in enumerate(zip(owners, (certificates.first, certificates.second))):
                tenant.member(owner, projects={tenant.project: ["host_admin", "content_read", "content_write", "metadata_read", "control_execute"]})
                pending = hosts.owner_command(owner, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host", {
                    "host_id": uid(), "certificate_sha256": certificate.fingerprint}))
                transport = configuration(url, certificates, certificate)
                active = HostChannelClient(transport).activate(pending["challenge"])
                directory = tmp_path / str(index)
                workspace = directory / "workspace"
                workspace.mkdir(parents=True)
                (workspace / "fact.txt").write_text(f"Owner {index} local fact")
                project = hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
                personal = CapturedSession(Scope.personal(f"local-{index}", project, f"saved-{index}"), str(workspace), BackendSpec("ollama", "chosen"))
                settings = SettingsManager(directory / "settings.json")
                settings.set("swarming", None, {"version": 1, "enabled": True})
                managed = ManagedDesktop(ManagedDesktopConfig(transport, tenant.id, tenant.project, active["host_id"], 1,
                    owner.actor_id, personal.scope.owner_id, str(workspace), 1))
                backends = []
                def factory(_, backends=backends):
                    backend = Backend()
                    backends.append(backend)
                    return backend
                service = SwarmRuntime(settings, backend_factory=factory, state_root=lambda _, directory=directory: directory / "state", managed_desktop=managed)
                capture = service.execution_capture(personal, "managed")
                services.append(SimpleNamespace(service=service, capture=capture, personal=personal, backends=backends, owner=owner))
            yield SimpleNamespace(a=services[0], b=services[1], tenant=tenant, server=server, sharing=sharing, monitor=monitor)
        finally:
            for item in services:
                item.service.close()


def view(env):
    return env.service.operate(env.capture, {"action": "view", "request_id": uid(), "run_id": env.run_id})


def operate(env, action, *, wait=True, **fields):
    current = view(env)
    message = {"action": "managed_sharing_" + action, "execution_mode": "managed", "request_id": uid(),
               "run_id": env.run_id, "expected_revision": current["run"]["run"]["revision"], **fields}
    result = env.service.operate(env.capture, message)
    if wait:
        until(lambda: view(env)["managed_collaboration"]["operation"]["state"] not in {"queued", "running"})
        result = view(env)
        assert result["managed_collaboration"]["operation"]["state"] == "completed", result["managed_collaboration"]["operation"]
    return result, message


def prepare(env):
    result = env.service.operate(env.capture, {"action": "managed_sharing_prepare", "execution_mode": "managed", "request_id": uid(),
        "objective": "Explicit shared investigation", "request_limit": 6})
    env.run_id = result["run"]["run"]["id"]
    until(lambda: view(env)["managed_collaboration"]["address"])
    # The host observer must publish actual state before the peer can invite it.
    until(lambda: view(env)["managed"]["connection"] == "connected")
    return view(env)["managed_collaboration"]["address"]


def agreement(f):
    prepare(f.a)
    receiver = prepare(f.b)
    assert f.a.backends == f.b.backends == []
    result, _ = operate(f.a, "offer", receiver_binding=receiver, terms=terms(max_requests=3))
    grant = result["managed_collaboration"]["grants"]["items"][0]["grant_id"]
    operate(f.b, "inspect")
    result, _ = operate(f.b, "inspect", grant_id=grant)
    sha = result["managed_collaboration"]["detail"]["terms_sha256"]
    operate(f.b, "approve", grant_id=grant, terms_sha256=sha)
    operate(f.a, "inspect")
    operate(f.a, "inspect", grant_id=grant)
    operate(f.a, "send", grant_id=grant, kind="work_request", data_class="summary", body="Peer proposal. Never insert this unique message into the model automatically.")
    operate(f.b, "inspect")
    result, _ = operate(f.b, "inspect", grant_id=grant)
    message = result["managed_collaboration"]["messages"]["items"][0]["message_id"]
    return grant, message


def test_explicit_service_flow_uses_own_contract_once_and_never_autoinjects_peer_text(pair):
    f = pair
    grant, message = agreement(f)
    with pytest.raises(Conflict, match="Explicitly read"):
        operate(f.b, "accept_work", grant_id=grant, message_id=message, objective="Read my fact", read_roots=["fact.txt"], requests=3, evidence="Owner chose exact scope")
    result, _ = operate(f.b, "deliver", grant_id=grant, message_id=message)
    assert "unique message" in result["managed_collaboration"]["selected_content"]["body"]
    assert f.a.backends == f.b.backends == []
    result, accepted = operate(f.b, "accept_work", grant_id=grant, message_id=message, objective="Read only my fact.txt", read_roots=["fact.txt"], requests=3, evidence="I chose my own independent read task")
    runner = f.b.service._runners[f.b.run_id][1]
    until(lambda: runner.inspect_all() and all(not row["alive"] for row in runner.inspect_all()))
    result = view(f.b)
    assert result["run"]["submissions"], {"workers": result["run"]["workers"], "attempts": result["run"]["attempts"],
        "admission": f.b.service._managed_sharing[f.b.run_id].admission.inspect(), "backends": len(f.b.backends)}
    assert len(result["run"]["submissions"]) == 1 and result["run"]["work_items"][0]["state"] == "submitted"
    assert not result["run"]["check_receipts"] and len(f.b.backends) == 1 and f.b.backends[0].calls == 2
    assert "unique message" not in str(f.b.backends[0].stream_calls)
    f.b.service.operate(f.b.capture, accepted)
    assert len(f.b.backends) == 1 and len(view(f.b)["run"]["attempts"]) == 1
    with pytest.raises(ScopeDenied):
        f.a.service.operate(f.a.capture, {"action": "view", "request_id": uid(), "run_id": f.b.run_id})
    with pytest.raises(Conflict):
        operate(f.b, "accept_work", grant_id=grant, message_id=message, objective="Retry without new peer permission", read_roots=["fact.txt"], requests=3, evidence="Explicit repeated click")
    assert len(view(f.b)["run"]["attempts"]) == 1
    retained = historical_view(f.b.service._store(f.b.capture), f.b.capture.scope, f.b.run_id)
    assert retained["accepted_work_item_ids"] == [result["run"]["work_items"][0]["id"]]
    assert retained["available"] is False and retained["selected_content"] is None and retained["detail"] is None
    assert retained["address"] is None and "unique message" not in str(retained)
    assert any(row["request_id"] == accepted["request_id"] and row["outcome"] == "local_dispatch_queued" for row in retained["history"])
    before = retained.copy()
    assert historical_view(f.b.service._store(f.b.capture), f.b.capture.scope, f.b.run_id) == before
    with pytest.raises(ScopeDenied):
        historical_view(f.b.service._store(f.b.capture), f.a.capture.scope, f.b.run_id)


def test_local_stop_remains_live_while_sharing_http_call_is_blocked(pair):
    f = pair
    prepare(f.a)
    attachment = f.a.service._managed_attachments[f.a.run_id]
    client = attachment.runtime.client
    entered, release = threading.Event(), threading.Event()
    original = client.sharing_inspect
    def held(**values):
        entered.set()
        assert release.wait(5)
        return original(**values)
    client.sharing_inspect = held
    try:
        operate(f.a, "inspect", wait=False)
        assert entered.wait(2)
        retained = historical_view(f.a.service._store(f.a.capture), f.a.capture.scope, f.a.run_id)
        assert retained["history"][0]["outcome"] == "unconfirmed"
        current = view(f.a)
        start = time.monotonic()
        stopped = f.a.service.operate(f.a.capture, {"action": "stop", "request_id": uid(), "run_id": f.a.run_id,
            "expected_revision": current["run"]["run"]["revision"]})
        assert time.monotonic() - start < 1.5
        assert stopped["run"]["run"]["state"] == "cancelled" and f.a.backends == []
    finally:
        release.set()


def test_same_host_origin_panel_cannot_approve_receiver_agreement(pair):
    f = pair
    prepare(f.a)
    other = SimpleNamespace(service=f.a.service, capture=replace(f.a.capture, scope=replace(f.a.capture.scope, session_id="second-saved-session")), backends=f.a.backends)
    receiver = prepare(other)
    result, _ = operate(f.a, "offer", receiver_binding=receiver, terms=terms())
    grant = result["managed_collaboration"]["grants"]["items"][0]["grant_id"]
    result, _ = operate(f.a, "inspect", grant_id=grant)
    sha = result["managed_collaboration"]["detail"]["terms_sha256"]
    with pytest.raises(ScopeDenied):
        operate(f.a, "approve", grant_id=grant, terms_sha256=sha)
    operate(other, "inspect")
    operate(other, "inspect", grant_id=grant)
    result, _ = operate(other, "approve", grant_id=grant, terms_sha256=sha)
    assert result["managed_collaboration"]["grants"]["items"][0]["state"] == "approved"
    assert f.a.backends == []


@pytest.mark.parametrize("control", ["pause", "stop", "close"])
def test_preflight_blocked_sharing_is_fenced_before_remote_dispatch(pair, control):
    """Actual TLS call cannot start after an owner control wins preflight."""
    from test_content import sql
    f = pair
    grant, _ = agreement(f)
    managed = f.a.service._managed_attachments[f.a.run_id].runtime
    adapter = f.a.service._managed_sharing[f.a.run_id]
    entered, release = threading.Event(), threading.Event()
    original = managed.effect_lease

    def held():
        entered.set()
        assert release.wait(5)
        return original()

    managed.effect_lease = held
    try:
        _, message = operate(f.a, "send", wait=False, grant_id=grant, kind="finding", data_class="summary", body="Selected finding blocked before dispatch")
        assert entered.wait(2)
        if control == "close":
            f.a.service.close()
        else:
            state = view(f.a)
            f.a.service.operate(f.a.capture, {"action": control, "request_id": uid(), "run_id": f.a.run_id,
                "expected_revision": state["run"]["run"]["revision"]})
        release.set()
        until(lambda: adapter.operation["state"] not in {"queued", "running"})
        assert adapter.operation["state"] == "failed"
        assert sql(f.tenant, "SELECT count(*) AS n FROM sonn_governance.sharing_messages WHERE tenant_id=%s", (f.tenant.id,))[0]["n"] == 1
        with adapter.store._connection() as connection:
            assert not connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-start:send' AND key=?",
                (f.a.run_id, message["request_id"])).fetchone()
        assert f.a.backends == []
    finally:
        release.set()


def test_observed_remote_mutation_remains_acknowledged_when_close_fences_refresh(pair):
    from test_content import sql
    f = pair
    grant, _ = agreement(f)
    adapter = f.a.service._managed_sharing[f.a.run_id]
    client = adapter.attachment.runtime.client
    original = client.sharing_send

    def send_then_close(**values):
        result = original(**values)
        f.a.service.close()
        return result

    client.sharing_send = send_then_close
    _, message = operate(f.a, "send", wait=False, grant_id=grant, kind="finding", data_class="summary", body="Already admitted selected finding")
    until(lambda: adapter.operation["state"] not in {"queued", "running"})
    assert adapter.operation["state"] == "completed"
    assert "acknowledged" in adapter.operation["error"]
    assert sql(f.tenant, "SELECT count(*) AS n FROM sonn_governance.sharing_messages WHERE tenant_id=%s", (f.tenant.id,))[0]["n"] == 2
    retained = historical_view(adapter.store, f.a.capture.scope, f.a.run_id)
    assert next(row for row in retained["history"] if row["request_id"] == message["request_id"])["outcome"] == "acknowledged"
    with adapter.store._connection() as connection:
        assert connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-start:send' AND key=?",
            (f.a.run_id, message["request_id"])).fetchone()
        assert not connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-start:refresh' AND key=?",
            (f.a.run_id, message["request_id"])).fetchone()


def test_lost_dispatch_outcome_receipt_does_not_claim_worker_never_started_or_replay(pair):
    f = pair
    grant, message_id = agreement(f)
    operate(f.b, "deliver", grant_id=grant, message_id=message_id)
    adapter = f.b.service._managed_sharing[f.b.run_id]
    original = adapter._record_outcome

    def lose(operation, semantics, outcome):
        if outcome == "local_dispatch_queued":
            raise OSError("Fixture outcome receipt unavailable after runner start")
        return original(operation, semantics, outcome)

    adapter._record_outcome = lose
    message = {"action": "managed_sharing_accept_work", "execution_mode": "managed", "request_id": uid(),
        "run_id": f.b.run_id, "expected_revision": view(f.b)["run"]["run"]["revision"], "grant_id": grant,
        "message_id": message_id, "objective": "Inspect my own fact", "read_roots": ["fact.txt"], "requests": 3,
        "evidence": "Owner chose this independent scope"}
    with pytest.raises(OSError):
        f.b.service.operate(f.b.capture, message)
    assert "may have occurred" in adapter.operation["error"]
    until(lambda: f.b.backends and not f.b.service._runners[f.b.run_id][1].inspect_all()[0]["alive"])
    assert len(f.b.backends) == 1 and f.b.backends[0].calls == 2
    f.b.service.operate(f.b.capture, message)
    assert len(f.b.backends) == 1 and len(view(f.b)["run"]["attempts"]) == 1
    changed = {**message, "expected_revision": message["expected_revision"] + 1}
    with pytest.raises(Conflict):
        f.b.service.operate(f.b.capture, changed)


def test_stopped_selected_approved_grant_keeps_metadata_without_new_terms_disclosure(pair):
    f = pair
    grant, _ = agreement(f)
    adapter = f.a.service._managed_sharing[f.a.run_id]
    client = adapter.attachment.runtime.client
    state = view(f.a)
    f.a.service.operate(f.a.capture, {"action": "stop", "request_id": uid(), "run_id": f.a.run_id,
        "expected_revision": state["run"]["run"]["revision"]})

    def forbidden(**values):
        pytest.fail("Stopped team must not fetch fresh encrypted terms")

    client.sharing_terms = forbidden
    result, message = operate(f.a, "inspect", grant_id=grant)
    selected = result["managed_collaboration"]
    assert selected["detail"] == {"grant_id": grant, "direction": "outgoing", "state": "approved"}
    assert selected["selected_content"] is None and selected["messages"]["items"] == []
    with adapter.store._connection() as connection:
        assert connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-start:metadata' AND key=?",
            (f.a.run_id, message["request_id"])).fetchone()
        assert not connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-start:terms' AND key=?",
            (f.a.run_id, message["request_id"])).fetchone()


@pytest.mark.parametrize("kind", ["terms", "message"])
def test_deleted_sharing_content_stays_metadata_only_in_running_desktop(pair, kind):
    from sonn_governance.sharing_retention import SharingRetention

    f = pair
    grant, message_id = agreement(f)
    operate(f.b, "deliver", grant_id=grant, message_id=message_id)
    custodian = Principal("https://fixture-custodian.example", uid(), time.time() + 300)
    f.tenant.member(custodian, projects={f.tenant.project: ["retention_admin"]})
    SharingRetention(f.tenant.store).retain(custodian, f.tenant.id, kind, grant if kind == "terms" else message_id,
        command_id=uid(), expected_revision=1, operation="delete_sharing_content")
    if kind == "terms":
        def forbidden(**values):
            pytest.fail("Deleted agreement must not request encrypted terms")
        f.b.service._managed_sharing[f.b.run_id].attachment.runtime.client.sharing_terms = forbidden
    result, _ = operate(f.b, "inspect", grant_id=grant)
    selected = result["managed_collaboration"]
    assert selected["selected_content"] is None
    if kind == "terms":
        assert selected["detail"] == {"grant_id": grant, "direction": "incoming", "state": "deleted"}
        assert selected["messages"]["items"] == []
    else:
        assert selected["detail"]["state"] == "approved"
        assert selected["messages"]["items"][0]["state"] == "deleted"
    assert not f.b.backends
