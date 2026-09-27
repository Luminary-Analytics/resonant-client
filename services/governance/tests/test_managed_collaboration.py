"""Real PostgreSQL bilateral sharing, independent payer and disclosure checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import secrets
import threading
import time

import psycopg
import pytest

from sonn_governance.content import ContentKeys, ContentUnavailable
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_collaboration import ManagedCollaboration, sharing_policy, sharing_terms
from sonn_governance.managed_collaboration_http import dispatch_collaboration
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict, HostPrincipal, InvalidRequest, Principal
from sonn_governance.monitoring import RunMonitoring
from test_content import sql
from test_monitoring import projection
from test_store import Tenant, uid


def policy(**overrides):
    return {"version": 1, "enabled": True, "allowed_peer_projects": [], "kinds": ["question", "finding", "work_request", "work_result", "artifact_offer"],
            "data_classes": ["summary", "code", "artifact_reference"], "max_messages": 20, "max_total_bytes": 20000,
            "max_message_bytes": 8192, "max_requests": 10, "max_hops": 8, "max_fanout": 8, "max_ttl_seconds": 3600, **overrides}


def terms(**overrides):
    values = policy()
    for key in ("version", "enabled", "allowed_peer_projects", "max_ttl_seconds"):
        values.pop(key)
    return {**values, "purpose": "Explicit private investigation", "expires_at": int(time.time()) + 600,
            "payer": "receiver", "revokers": "either_owner", **overrides}


class Fixture:
    def __init__(self, database_config):
        self.tenant = tenant = Tenant(database_config)
        tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
        tenant.store.command(tenant.admin, tenant.command(content_mode="explicit", request_limit=30, max_workers=4))
        self.hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
        self.monitor = RunMonitoring(self.hosts)
        self.resources = ManagedResources(self.hosts)
        self.sharing = ManagedCollaboration(self.hosts, ContentKeys({"test": b"k" * 32}, "test"))
        self.sharing.set_policy(tenant.admin, tenant_id=tenant.id, project_id=tenant.project, command_id=uid(), expected_revision=0, policy=policy())
        self.a = self.person()
        self.b = self.person(tenant.reader)

    def person(self, owner=None, project=None):
        tenant = self.tenant
        owner = owner or Principal("https://issuer.example", uid(), time.time() + 3600)
        project = project or tenant.project
        if owner != tenant.admin:
            tenant.member(owner, projects={project: ["host_admin", "content_read", "content_write", "metadata_read", "control_execute"]})
        principal = HostPrincipal(secrets.token_hex(32), time.time() + 3600)
        pending = self.hosts.owner_command(owner, CommandEnvelope(1, uid(), tenant.id, project, 0, "enroll_host", {"host_id": uid(), "certificate_sha256": principal.certificate_sha256}))
        self.hosts.activate(principal, pending["challenge"])
        lease = self.hosts.lease(principal, uid(), 1, runner_protocol=2)
        binding = self.monitor.register(principal, command_id=uid(), lease_id=lease["lease_id"], local_run_id=uid(), session_id=uid())
        self.monitor.ingest(principal, command_id=uid(), binding_id=binding["binding_id"], sequence=1, projection=projection())
        return {"principal": principal, "lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "owner": owner}

    def grant(self, a=None, b=None, **changes):
        a, b = a or self.a, b or self.b
        offered = self.sharing.offer(a["principal"], lease_id=a["lease_id"], command_id=uid(), origin_binding=a["binding_id"], receiver_binding=b["binding_id"], terms=terms(**changes))
        self.sharing.approve(b["principal"], lease_id=b["lease_id"], command_id=uid(), grant_id=offered["grant_id"], terms_sha256=offered["terms_sha256"])
        return offered["grant_id"]

    def send(self, grant, a=None, **changes):
        a = a or self.a
        return self.sharing.send(a["principal"], lease_id=a["lease_id"], command_id=uid(), grant_id=grant, kind="work_request", data_class="summary", body="Selected private task", **changes)["message_id"]

    def deliver(self, message, b=None):
        b = b or self.b
        return self.sharing.deliver(b["principal"], lease_id=b["lease_id"], command_id=uid(), message_id=message)

    def slot(self, person=None):
        person = person or self.b
        worker_id = uid()
        self.resources.reserve_worker(person["principal"], lease_id=person["lease_id"], binding_id=person["binding_id"], worker_id=worker_id, kind="worker")
        return worker_id


@pytest.fixture
def sharing(database_config):
    return Fixture(database_config)


def test_exact_bilateral_content_separate_from_metadata_and_no_identity_fields(sharing):
    f = sharing
    offer = f.sharing.offer(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), origin_binding=f.a["binding_id"], receiver_binding=f.b["binding_id"], terms=terms())
    with pytest.raises(AccessDenied):
        f.send(offer["grant_id"])
    with pytest.raises(AccessDenied):
        f.sharing.approve(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=offer["grant_id"], terms_sha256=offer["terms_sha256"])
    with pytest.raises(Conflict):
        f.sharing.approve(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=offer["grant_id"], terms_sha256="0" * 64)
    selected = f.sharing.inspect_terms(f.b["principal"], lease_id=f.b["lease_id"], grant_id=offer["grant_id"])
    assert selected["terms"]["purpose"] == "Explicit private investigation"
    f.sharing.approve(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=offer["grant_id"], terms_sha256=offer["terms_sha256"])
    message = f.send(offer["grant_id"])
    assert f.deliver(message)["body"] == "Selected private task"
    metadata = f.sharing.inspect(f.b["principal"], lease_id=f.b["lease_id"], binding_id=f.b["binding_id"])
    assert set(metadata["items"][0]) == {"grant_id", "direction", "state"}
    for table in ("sharing_receipts", "audit", "run_events"):
        rows = sql(f.tenant, f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s", (f.tenant.id,))
        assert "Selected private task" not in str(rows) and "Explicit private investigation" not in str(rows)
    rows = sql(f.tenant, "SELECT ciphertext FROM sonn_governance.sharing_messages WHERE tenant_id=%s", (f.tenant.id,))
    assert b"Selected private task" not in bytes(rows[0]["ciphertext"])
    with pytest.raises(InvalidRequest):
        dispatch_collaboration(f.sharing, f.a["principal"], "/v1/sharing/inspect", {"lease_id": f.a["lease_id"], "binding_id": f.a["binding_id"], "owner_id": f.b["owner"].actor_id})


@pytest.mark.parametrize("change", ["membership", "policy", "sharing_policy", "epoch", "stopped", "expiry", "peer_lease", "ciphertext"])
def test_current_both_endpoints_gate_disclosure_and_replay(sharing, change):
    f = sharing
    grant = f.grant()
    message = f.send(grant)
    args = {"lease_id": f.b["lease_id"], "command_id": uid(), "message_id": message}
    f.sharing.deliver(f.b["principal"], **args)
    if change == "membership":
        f.tenant.member(f.b["owner"], projects={f.tenant.project: ["host_admin"]})
    elif change == "policy":
        f.tenant.store.command(f.tenant.admin, f.tenant.command(expected=1, content_mode="explicit"))
    elif change == "sharing_policy":
        f.sharing.set_policy(f.tenant.admin, tenant_id=f.tenant.id, project_id=f.tenant.project, command_id=uid(), expected_revision=1, policy=policy(enabled=False))
    elif change in {"epoch", "stopped"}:
        f.monitor.ingest(f.a["principal"], command_id=uid(), binding_id=f.a["binding_id"], sequence=2,
                         projection=projection(epoch=2) if change == "epoch" else projection(state="stopping"))
    elif change == "expiry":
        sql(f.tenant, "UPDATE sonn_governance.sharing_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE grant_id=%s", (grant,))
    elif change == "peer_lease":
        sql(f.tenant, "UPDATE sonn_governance.policy_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE lease_id=%s", (f.a["lease_id"],))
    else:
        sql(f.tenant, "UPDATE sonn_governance.sharing_messages SET ciphertext=decode('00','hex') WHERE message_id=%s", (message,))
    with pytest.raises((AccessDenied, Conflict, ContentUnavailable)):
        f.sharing.deliver(f.b["principal"], **args)


def test_receiver_owns_fresh_worker_budget_and_revoke_preserves_accepted_work(sharing):
    f = sharing
    grant = f.grant(max_requests=1)
    message = f.send(grant)
    f.deliver(message)
    own_worker, foreign_worker = f.slot(), f.slot(f.a)
    args = {"lease_id": f.b["lease_id"], "command_id": uid(), "message_id": message,
            "worker_id": own_worker, "request_limit": 1, "contract_sha256": "a" * 64}
    with pytest.raises(AccessDenied):
        f.sharing.accept_work(f.b["principal"], **{**args, "worker_id": foreign_worker})
    result = f.sharing.accept_work(f.b["principal"], **args)
    assert result["state"] == "work_reserved" and result["dispatch_permitted"]
    assert not f.sharing.accept_work(f.b["principal"], **args)["dispatch_permitted"]
    f.sharing.revoke(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=grant)
    with pytest.raises(AccessDenied):
        f.deliver(message)
    request_id = uid()
    f.hosts.reserve(f.b["principal"], f.b["lease_id"], request_id)
    f.resources.bind_request(f.b["principal"], lease_id=f.b["lease_id"], worker_id=own_worker, request_id=request_id,
                             purpose="primary", model={"provider": "ollama", "model": "fixture"}, input_sha256="b" * 64)
    assert f.hosts.start(f.b["principal"], request_id)["dispatch_permitted"]
    another = uid()
    f.hosts.reserve(f.b["principal"], f.b["lease_id"], another)
    with pytest.raises(Conflict, match="allowance"):
        f.resources.bind_request(f.b["principal"], lease_id=f.b["lease_id"], worker_id=own_worker, request_id=another,
                                 purpose="compression", model={"provider": "ollama", "model": "fixture"}, input_sha256="c" * 64)


def test_forwarding_cumulative_origin_disclosure_and_cycle(sharing):
    f = sharing
    third = f.person()
    root = f.grant(max_total_bytes=100, max_message_bytes=100)
    message = f.sharing.send(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=root,
                             kind="finding", data_class="summary", body="a" * 52)["message_id"]
    f.deliver(message)
    onward = f.grant(f.b, third)
    with pytest.raises(Conflict, match="disclosure"):
        f.sharing.send(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=onward,
                       kind="finding", data_class="summary", body="b" * 52, parent_id=message)
    back = f.grant(f.b, f.a)
    with pytest.raises(Conflict, match="cycle"):
        f.sharing.send(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=back,
                       kind="finding", data_class="summary", body="return", parent_id=message)
    f.sharing.revoke(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=root)
    with pytest.raises(AccessDenied):
        f.sharing.send(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=onward,
                       kind="finding", data_class="summary", body="short", parent_id=message)


def test_expiry_during_required_audit_rolls_back_delivery(sharing, monkeypatch):
    f = sharing
    grant, message = f.grant(), None
    message = f.send(grant)
    real_audit, real_now = f.tenant.store._audit, f.hosts._now
    advanced = False
    def audit(*args, **kwargs):
        nonlocal advanced
        result = real_audit(*args, **kwargs)
        advanced = True
        return result
    monkeypatch.setattr(f.tenant.store, "_audit", audit)
    monkeypatch.setattr(f.hosts, "_now", lambda connection: real_now(connection) + timedelta(hours=2) if advanced else real_now(connection))
    with pytest.raises(AccessDenied):
        f.deliver(message)
    row = sql(f.tenant, "SELECT delivered_at FROM sonn_governance.sharing_messages WHERE message_id=%s", (message,))[0]
    assert row["delivered_at"] is None


def test_policy_offer_ingest_concurrency_has_no_lock_inversion(sharing):
    f = sharing
    barrier = threading.Barrier(3)
    def invoke(kind):
        barrier.wait(5)
        try:
            if kind == "policy":
                return f.sharing.set_policy(f.tenant.admin, tenant_id=f.tenant.id, project_id=f.tenant.project, command_id=uid(), expected_revision=1, policy=policy())
            if kind == "ingest":
                return f.monitor.ingest(f.b["principal"], command_id=uid(), binding_id=f.b["binding_id"], sequence=2, projection=projection(local_revision=6))
            return f.grant()
        except AccessDenied:
            return "revision invalidated"
    with ThreadPoolExecutor(max_workers=3) as executor:
        assert len(list(executor.map(invoke, ["policy", "ingest", "offer"]))) == 3


@pytest.mark.parametrize("factory,change", [(sharing_policy, {"kinds": [[]]}), (sharing_terms, {"payer": "sender"}),
    (sharing_terms, {"max_requests": True}), (sharing_terms, {"purpose": "a", "owner_id": "forged"})])
def test_strict_untrusted_shapes(factory, change):
    with pytest.raises(InvalidRequest):
        factory({**(policy() if factory is sharing_policy else terms()), **change})


def test_hash_matches_complete_selected_utf8_body(sharing):
    f = sharing
    grant = f.grant()
    body = "Selected Ω🙂 evidence"
    message = f.sharing.send(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=grant,
                             kind="finding", data_class="summary", body=body)["message_id"]
    assert f.deliver(message)["sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_cross_project_requires_both_explicit_policies_and_default_is_disabled(sharing):
    f = sharing
    t = f.tenant
    assert f.sharing.inspect_policy(t.admin, tenant_id=t.id, project_id=t.other_project) == {
        "project_id": t.other_project, "revision": 0, "policy": None, "enabled": False}
    t.store.command(t.admin, t.command(project=t.other_project, content_mode="explicit"))
    other = f.person(project=t.other_project)
    with pytest.raises(AccessDenied):
        f.grant(f.a, other)
    f.sharing.set_policy(t.admin, tenant_id=t.id, project_id=t.project, command_id=uid(), expected_revision=1,
                         policy=policy(allowed_peer_projects=[t.other_project]))
    f.sharing.set_policy(t.admin, tenant_id=t.id, project_id=t.other_project, command_id=uid(), expected_revision=0, policy=policy())
    with pytest.raises(AccessDenied):
        f.grant(f.a, other)
    f.sharing.set_policy(t.admin, tenant_id=t.id, project_id=t.other_project, command_id=uid(), expected_revision=1,
                         policy=policy(allowed_peer_projects=[t.project]))
    assert f.grant(f.a, other)


def test_rebinding_same_member_session_does_not_evade_causal_cycle(sharing):
    f = sharing
    root = f.grant()
    message = f.send(root)
    f.deliver(message)
    old = sql(f.tenant, "SELECT session_id FROM sonn_governance.managed_runs WHERE binding_id=%s", (f.a["binding_id"],))[0]
    rebound = f.monitor.register(f.a["principal"], command_id=uid(), lease_id=f.a["lease_id"], local_run_id=uid(), session_id=str(old["session_id"]))
    f.monitor.ingest(f.a["principal"], command_id=uid(), binding_id=rebound["binding_id"], sequence=1, projection=projection())
    target = {**f.a, "binding_id": rebound["binding_id"]}
    grant = f.grant(f.b, target)
    with pytest.raises(Conflict, match="cycle"):
        f.send(grant, f.b, parent_id=message)


def test_independent_work_request_cycle_is_rejected_before_second_acceptance(sharing):
    f = sharing
    first, second = f.grant(), f.grant(f.b, f.a)
    message, reverse = f.send(first), f.send(second, f.b)
    f.deliver(message)
    f.deliver(reverse, f.a)
    f.sharing.accept_work(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), message_id=message,
                          worker_id=f.slot(), request_limit=1, contract_sha256="a" * 64)
    with pytest.raises(Conflict, match="cycle"):
        f.sharing.accept_work(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), message_id=reverse,
                              worker_id=f.slot(f.a), request_limit=1, contract_sha256="b" * 64)


def test_message_pages_are_bounded_and_never_implicitly_decrypt(sharing):
    f = sharing
    grant = f.grant()
    ids = {f.send(grant) for _ in range(3)}
    page = f.sharing.inspect_messages(f.b["principal"], lease_id=f.b["lease_id"], grant_id=grant, limit=2)
    assert len(page["items"]) == 2 and page["next_cursor"]
    last = f.sharing.inspect_messages(f.b["principal"], lease_id=f.b["lease_id"], grant_id=grant, limit=2, before_message_id=page["next_cursor"])
    assert len(last["items"]) == 1 and last["next_cursor"] is None
    assert {row["message_id"] for row in page["items"] + last["items"]} == ids
    assert all(set(row) == {"message_id", "kind", "data_class", "state"} and row["state"] == "queued" for row in page["items"] + last["items"])
    third = f.person()
    with pytest.raises(AccessDenied):
        f.sharing.inspect_messages(third["principal"], lease_id=third["lease_id"], grant_id=grant)


def test_required_archive_blocks_new_and_replayed_content_but_revoke_remains_live(sharing):
    f = sharing
    grant = f.grant()
    message = f.send(grant)
    args = {"lease_id": f.b["lease_id"], "command_id": uid(), "message_id": message}
    f.sharing.deliver(f.b["principal"], **args)
    sql(f.tenant, "INSERT INTO sonn_governance.archive_requirements(tenant_id,max_delay_seconds) VALUES(%s,30)", (f.tenant.id,))
    for call in (lambda: f.sharing.deliver(f.b["principal"], **args), lambda: f.send(grant),
                 lambda: f.sharing.inspect_terms(f.b["principal"], lease_id=f.b["lease_id"], grant_id=grant)):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            call()
    assert len(sql(f.tenant, "SELECT message_id FROM sonn_governance.sharing_messages WHERE grant_id=%s", (grant,))) == 1
    assert f.sharing.inspect(f.b["principal"], lease_id=f.b["lease_id"], binding_id=f.b["binding_id"])["items"]
    assert f.sharing.revoke(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=grant)["state"] == "revoked"


def test_pending_origin_remote_stop_closes_disclosure_before_host_projection(sharing):
    f = sharing
    grant, message = f.grant(), None
    message = f.send(grant)
    rows = f.monitor.inspect(f.a["owner"], f.tenant.id, f.tenant.project)["runs"]
    origin = next(row for row in rows if row["binding_id"] == f.a["binding_id"])
    f.monitor.request_control(f.a["owner"], f.tenant.id, f.tenant.project, binding_id=f.a["binding_id"], command_id=uid(),
        expected_revision=origin["revision"], expected_epoch=1, expected_local_revision=5, operation="stop")
    with pytest.raises(AccessDenied):
        f.deliver(message)
