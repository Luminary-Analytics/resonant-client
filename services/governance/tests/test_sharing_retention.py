"""Real PostgreSQL bilateral retention, causal accounting and disclosure races."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from sonn_governance.models import AccessDenied, Conflict, InvalidRequest, Principal
from sonn_governance.sharing_retention import SharingRetention
from test_content import sql
from test_managed_collaboration import Fixture, policy
from test_store import uid


@pytest.fixture
def retained(database_config):
    f = Fixture(database_config)
    f.retention = SharingRetention(f.tenant.store)
    f.custodian = Principal("https://issuer.example", uid(), time.time() + 3600)
    f.tenant.member(f.custodian, projects={f.tenant.project: ["retention_admin"]})
    return f


def retain(f, kind, resource_id, *, revision=1, operation="delete_sharing_content", **extra):
    return f.retention.retain(f.custodian, f.tenant.id, kind, resource_id, command_id=uid(),
                              expected_revision=revision, operation=operation, **extra)


@pytest.mark.parametrize("kind", ["terms", "message"])
def test_hold_release_delete_exact_audit_replay_and_no_content_access(retained, kind):
    f = retained
    grant = f.grant()
    message = f.send(grant)
    target = grant if kind == "terms" else message
    before = f.retention.inspect(f.custodian, f.tenant.id, kind, target)
    assert before["revision"] == 1 and not before["held"]
    assert "Selected private" not in str(before) and "sha256" not in str(before)
    args = dict(command_id=uid(), expected_revision=1, operation="hold_sharing_content", reason="legal_review")
    held = f.retention.retain(f.custodian, f.tenant.id, kind, target, **args)
    assert held["held"] and held["revision"] == 2
    assert f.retention.retain(f.custodian, f.tenant.id, kind, target, **args) == held
    with pytest.raises(Conflict):
        f.retention.retain(f.custodian, f.tenant.id, kind, target, **{**args, "reason": "owner_request"})
    with pytest.raises(Conflict, match="hold"):
        retain(f, kind, target, revision=2)
    assert retain(f, kind, target, revision=2, operation="release_sharing_content_hold")["revision"] == 3
    deleted = retain(f, kind, target, revision=3)
    assert deleted["deleted_at"] and deleted["revision"] == 4
    assert f.retention.inspect(f.custodian, f.tenant.id, kind, target) == deleted
    # A replay is an old immutable observation, never a new hold or resurrection.
    assert f.retention.retain(f.custodian, f.tenant.id, kind, target, **args) == held
    table, key, sha = ("sharing_grants", "grant_id", "terms_sha256") if kind == "terms" else ("sharing_messages", "message_id", "body_sha256")
    row = sql(f.tenant, f"SELECT * FROM sonn_governance.{table} WHERE {key}=%s", (target,))[0]
    assert all(row[field] is None for field in ("ciphertext", "nonce", "key_id", sha))
    audits = sql(f.tenant, "SELECT operation,decision_id,resource_revision,project_id FROM sonn_governance.audit "
        "WHERE tenant_id=%s AND decision_id=%s ORDER BY audit_id", (f.tenant.id, target))
    chain = [row for row in audits if not row["operation"].startswith("inspect_")]
    assert [row["resource_revision"] for row in chain] == [1, 2, 3, 4]
    assert all(str(row["project_id"]) == f.tenant.project for row in chain)
    assert chain[-1]["operation"] == f"delete_sharing_{kind}"
    # Removal of current authority closes even an exact old command replay.
    f.tenant.member(f.custodian, active=False)
    with pytest.raises(AccessDenied):
        f.retention.retain(f.custodian, f.tenant.id, kind, target, **args)


def test_bilateral_project_permission_required_before_inspection_and_replay(retained):
    f = retained
    second = f.tenant.other_project
    f.tenant.store.command(f.tenant.admin, f.tenant.command(project=second, content_mode="explicit"))
    f.sharing.set_policy(f.tenant.admin, tenant_id=f.tenant.id, project_id=second, command_id=uid(), expected_revision=0,
                         policy=policy(allowed_peer_projects=[f.tenant.project]))
    f.sharing.set_policy(f.tenant.admin, tenant_id=f.tenant.id, project_id=f.tenant.project, command_id=uid(), expected_revision=1,
                         policy=policy(allowed_peer_projects=[second]))
    peer = f.person(project=second)
    grant = f.grant(b=peer)
    for action in (lambda: f.retention.inspect(f.custodian, f.tenant.id, "terms", grant),
                   lambda: retain(f, "terms", grant)):
        with pytest.raises(AccessDenied):
            action()
    f.tenant.member(f.custodian, projects={f.tenant.project: ["retention_admin"], second: ["retention_admin"]})
    assert f.retention.inspect(f.custodian, f.tenant.id, "terms", grant)["project_ids"] == [f.tenant.project, second]
    with pytest.raises(AccessDenied):
        f.retention.inspect(f.custodian, uid(), "terms", grant)
    # Metadata or ownership alone never implies retention permission.
    with pytest.raises(AccessDenied):
        f.retention.inspect(f.a["owner"], f.tenant.id, "terms", grant)


@pytest.mark.parametrize("kind", ["terms", "message"])
def test_delete_fences_deliver_accept_replay_relay_but_retains_receiver_budget(retained, kind):
    f = retained
    grant = f.grant(max_requests=1)
    send_args = dict(lease_id=f.a["lease_id"], command_id=uid(), grant_id=grant,
                     kind="work_request", data_class="summary", body="Selected private task")
    message = f.sharing.send(f.a["principal"], **send_args)["message_id"]
    delivery = dict(lease_id=f.b["lease_id"], command_id=uid(), message_id=message)
    f.sharing.deliver(f.b["principal"], **delivery)
    worker = f.slot()
    acceptance = dict(lease_id=f.b["lease_id"], command_id=uid(), message_id=message,
                      worker_id=worker, request_limit=1, contract_sha256="a" * 64)
    assert f.sharing.accept_work(f.b["principal"], **acceptance)["dispatch_permitted"]
    third = f.person()
    onward = f.grant(f.b, third)
    retain(f, kind, grant if kind == "terms" else message)
    for action in (lambda: f.sharing.deliver(f.b["principal"], **delivery),
                   lambda: f.sharing.accept_work(f.b["principal"], **acceptance),
                   lambda: f.sharing.send(f.a["principal"], **send_args),
                   lambda: f.sharing.send(f.b["principal"], lease_id=f.b["lease_id"], command_id=uid(), grant_id=onward,
                       kind="finding", data_class="summary", body="selected relay", parent_id=message)):
        with pytest.raises(AccessDenied):
            action()
    request_id = uid()
    f.hosts.reserve(f.b["principal"], f.b["lease_id"], request_id)
    f.resources.bind_request(f.b["principal"], lease_id=f.b["lease_id"], worker_id=worker, request_id=request_id,
        purpose="primary", model={"provider": "ollama", "model": "fixture"}, input_sha256="b" * 64)
    assert f.hosts.start(f.b["principal"], request_id)["dispatch_permitted"]
    rows = sql(f.tenant, "SELECT byte_size,lineage FROM sonn_governance.sharing_messages WHERE message_id=%s", (message,))
    assert rows[0]["byte_size"] == len("Selected private task") and rows[0]["lineage"] == [grant]
    assert len(sql(f.tenant, "SELECT * FROM sonn_governance.sharing_acceptances WHERE tenant_id=%s", (f.tenant.id,))) == 1


def test_delete_serializes_before_waiting_disclosure_and_audit_failure_rolls_back(retained, monkeypatch):
    f = retained
    grant = f.grant()
    message = f.send(grant)
    entered, release = threading.Event(), threading.Event()
    original = f.tenant.store._audit

    def hold(connection, principal, tenant_id, **kwargs):
        value = original(connection, principal, tenant_id, **kwargs)
        if kwargs["operation"] == "delete_sharing_message":
            entered.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(f.tenant.store, "_audit", hold)
    with ThreadPoolExecutor(2) as pool:
        deletion = pool.submit(retain, f, "message", message)
        assert entered.wait(5)
        delivery = pool.submit(f.deliver, message)
        release.set()
        assert deletion.result()["deleted_at"]
        with pytest.raises(AccessDenied):
            delivery.result()
    other = f.send(grant)

    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit unavailable")

    monkeypatch.setattr(f.tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError):
        retain(f, "message", other)
    row = sql(f.tenant, "SELECT deleted_at,ciphertext,retention_revision FROM sonn_governance.sharing_messages WHERE message_id=%s", (other,))[0]
    assert row["deleted_at"] is None and row["ciphertext"] and row["retention_revision"] == 1


@pytest.mark.parametrize("changes", [{"expected_revision": True}, {"reason": []}, {"operation": "delete_all"}, {"reason": "arbitrary private text"}])
def test_invalid_retention_never_mutates(retained, changes):
    f = retained
    grant = f.grant()
    with pytest.raises(InvalidRequest):
        f.retention.retain(f.custodian, f.tenant.id, "terms", grant,
            **{ "command_id": uid(), "expected_revision": 1, "operation": "hold_sharing_content", "reason": "owner_request", **changes})
    assert f.retention.inspect(f.custodian, f.tenant.id, "terms", grant)["revision"] == 1


def test_expired_revoked_sharing_can_be_retained_without_disclosure_permission(retained):
    f = retained
    grant = f.grant()
    message = f.send(grant)
    f.sharing.revoke(f.a["principal"], lease_id=f.a["lease_id"], command_id=uid(), grant_id=grant)
    sql(f.tenant, "UPDATE sonn_governance.policy_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s", (f.tenant.id,))
    assert retain(f, "message", message)["deleted_at"]
    assert retain(f, "terms", grant)["deleted_at"]
