"""Real database independent review; model or requester prose grants nothing."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from sonn_governance.grants import GrantApprovals
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict
from test_store import Tenant, uid


@pytest.fixture
def setup(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.reader, tenant=["membership_admin", "membership_approve"])
    return tenant, GrantApprovals(tenant.store)


def request(tenant, *, project=False, permission="content_read"):
    payload = {"actor_id": tenant.admin.actor_id, "active": True,
               "tenant_permissions": ["membership_admin", "membership_approve", "policy_admin"],
               "project_permissions": {}}
    if project:
        payload["project_permissions"] = {tenant.project: [permission]}
    else:
        payload["tenant_permissions"].append(permission)
    command = CommandEnvelope(1, uid(), tenant.id, None, tenant.revision, "set_membership", payload)
    result = tenant.store.command(tenant.admin, command)
    return command, result


def decide(tenant, result, *, approve=True, command_id=None, expected=None):
    return CommandEnvelope(1, command_id or uid(), tenant.id, None,
                           tenant.revision if expected is None else expected, "decide_grant", {
                               "request_id": result["grant_request_id"], "sha256": result["sha256"], "approve": approve,
                           })


def content_authority(tenant, principal=None, permission="content_read"):
    with tenant.store._connection() as connection:
        tenant.store._authorize(connection, principal or tenant.admin, tenant.id, tenant.project, permission)


@pytest.mark.parametrize("permission", ["content_read", "content_write", "retention_admin"])
def test_self_sensitive_request_grants_nothing_until_distinct_current_approval(setup, permission):
    tenant, approvals = setup
    command, pending = request(tenant, permission=permission)
    assert pending["state"] == "pending" and pending["revision"] == tenant.revision
    assert pending == tenant.store.command(tenant.admin, command)
    with pytest.raises(AccessDenied):
        content_authority(tenant, permission=permission)
    with pytest.raises(AccessDenied, match="independent"):
        approvals.command(tenant.admin, decide(tenant, pending))
    decision = decide(tenant, pending)
    accepted = approvals.command(tenant.reader, decision)
    assert accepted["state"] == "approved" and accepted["revision"] == tenant.revision + 1
    assert approvals.command(tenant.reader, decision) == accepted
    content_authority(tenant, permission=permission)
    view = approvals.inspect(tenant.admin, tenant.id, pending["grant_request_id"])
    assert view["decided_by"] == tenant.reader.actor_id and view["payload"] == command.payload


def test_independent_admin_grant_remains_direct_and_project_broadening_needs_review(setup):
    tenant, _ = setup
    # A separately authenticated membership administrator may explicitly grant
    # another member access without making that recipient their own approver.
    tenant.member(tenant.reader, projects={tenant.project: ["content_read"]})
    content_authority(tenant, tenant.reader)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin"],
                  projects={tenant.project: ["content_read"]})
    # This was a self-addition, so it is pending and grants nothing.
    with pytest.raises(AccessDenied):
        content_authority(tenant)


def test_rejection_and_pending_revocation_are_audited_and_never_apply(setup):
    tenant, approvals = setup
    _, pending = request(tenant)
    rejected = approvals.command(tenant.reader, decide(tenant, pending, approve=False))
    assert rejected["state"] == "rejected"
    with pytest.raises(AccessDenied):
        content_authority(tenant)
    with pytest.raises(Conflict):
        approvals.command(tenant.reader, decide(tenant, pending))
    _, second = request(tenant)
    revoke = CommandEnvelope(1, uid(), tenant.id, None, tenant.revision, "revoke_grant", {
        "request_id": second["grant_request_id"], "sha256": second["sha256"],
    })
    result = approvals.command(tenant.admin, revoke)
    assert result["state"] == "revoked" and approvals.command(tenant.admin, revoke) == result
    with pytest.raises(Conflict):
        approvals.command(tenant.reader, decide(tenant, second))


def test_stale_scope_digest_and_removed_approver_cannot_apply_or_replay(setup):
    tenant, approvals = setup
    _, pending = request(tenant)
    decision = decide(tenant, pending)
    with pytest.raises(Conflict):
        approvals.command(tenant.reader, replace(decision, payload={**decision.payload, "sha256": "0" * 64}))
    tenant.member(tenant.reader, tenant=["membership_admin", "membership_approve", "metadata_read"])
    with pytest.raises(Conflict, match="stale"):
        approvals.command(tenant.reader, decide(tenant, pending))
    # Rejecting the exact stale request is safe and records that disposition.
    rejected_command = decide(tenant, pending, approve=False)
    approvals.command(tenant.reader, rejected_command)
    tenant.member(tenant.reader, tenant=["membership_admin"])
    with pytest.raises(AccessDenied):
        approvals.command(tenant.reader, rejected_command)


def test_new_sensitive_project_or_tenant_scope_requires_new_approval(setup):
    tenant, approvals = setup
    _, pending = request(tenant, project=True)
    first = approvals.command(tenant.reader, decide(tenant, pending))
    tenant.revision = first["revision"]
    content_authority(tenant)
    _, broadened = request(tenant, project=False)
    assert broadened["state"] == "pending"
    with tenant.store._connection() as connection:
        with pytest.raises(AccessDenied):
            tenant.store._authorize(connection, tenant.admin, tenant.id, tenant.other_project, "content_read")
    approvals.command(tenant.reader, decide(tenant, broadened))
    with tenant.store._connection() as connection:
        tenant.store._authorize(connection, tenant.admin, tenant.id, tenant.other_project, "content_read")


def test_approved_access_revocation_is_immediate_and_old_approval_does_not_restore_it(setup):
    tenant, approvals = setup
    _, pending = request(tenant)
    command = decide(tenant, pending)
    accepted = approvals.command(tenant.reader, command)
    tenant.revision = accepted["revision"]
    tenant.member(tenant.admin, tenant=["membership_admin", "membership_approve", "policy_admin"])
    with pytest.raises(AccessDenied):
        content_authority(tenant)
    assert approvals.command(tenant.reader, command) == accepted
    with pytest.raises(AccessDenied):
        content_authority(tenant)


def test_parallel_approval_and_rejection_commit_one_disposition(setup):
    tenant, approvals = setup
    _, pending = request(tenant)
    barrier = threading.Barrier(2)

    def apply(approve):
        barrier.wait(5)
        try:
            return approvals.command(tenant.reader, decide(tenant, pending, approve=approve))
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, (True, False)))
    assert sum(result is not None for result in results) == 1
    view = approvals.inspect(tenant.admin, tenant.id, pending["grant_request_id"])
    assert view["state"] in {"approved", "rejected"}


def test_foreign_request_denied_and_audit_failure_rolls_back_decision(setup, database_config, monkeypatch):
    tenant, approvals = setup
    _, pending = request(tenant)
    other = Tenant(database_config)
    with pytest.raises(AccessDenied):
        approvals.inspect(other.admin, tenant.id, pending["grant_request_id"])
    original = tenant.store._audit

    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(tenant.store, "_audit", unavailable)
    with pytest.raises(RuntimeError):
        approvals.command(tenant.reader, decide(tenant, pending))
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert approvals.inspect(tenant.admin, tenant.id, pending["grant_request_id"])["state"] == "pending"
    with pytest.raises(AccessDenied):
        content_authority(tenant)
