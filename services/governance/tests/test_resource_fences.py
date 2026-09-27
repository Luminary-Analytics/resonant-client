"""A retained absence proof prevents every delayed dispatch of that identity."""
# ruff: noqa: F811 -- imported real database/transport fixtures.

from concurrent.futures import ThreadPoolExecutor
from functools import partial
import threading

import pytest

from sonn_governance.models import AccessDenied, CommandEnvelope
from test_hosts import expire
from test_managed_resources import participant, request, worker
from test_owner_effects import effects  # noqa: F401
from test_store import uid


def admission(effects, person, kind):
    principal, _, lease, binding = person
    resource, hosts = effects[2], effects[1]
    identity = uid()
    if kind == "worker":
        operation = partial(resource.reserve_worker, principal, lease_id=lease["lease_id"], binding_id=binding["binding_id"], worker_id=identity, kind="worker")
    elif kind == "request":
        operation = partial(hosts.reserve, principal, lease["lease_id"], identity)
    elif kind == "effect":
        operation = partial(resource.authorize_effect, principal, lease_id=lease["lease_id"], binding_id=binding["binding_id"], effect_id=identity,
            kind="candidate_check", semantics_sha256="a" * 64)
    else:
        slot = worker(effects, person)
        bound = request(effects, person, slot)
        hosts.start(principal, bound["request_id"])
        hosts.settle(principal, bound["request_id"], "completed")
        operation = partial(resource.authorize_tool, principal, lease_id=lease["lease_id"], worker_id=slot["worker_id"], request_id=bound["request_id"],
            action_id=identity, tool_name="file_read", arguments_sha256="a" * 64)
    fence = partial(resource.fence_absent, principal, resource_kind=kind, resource_id=identity, lease_id=lease["lease_id"])
    return identity, operation, fence


@pytest.mark.parametrize("kind", ["request", "worker", "tool", "effect"])
def test_absence_receipt_is_immutable_and_delayed_original_admission_is_denied(effects, kind):
    person = participant(effects)
    identity, admit, fence = admission(effects, person, kind)
    result = fence()
    assert result["state"] == "fenced_absent" and result["resource_id"] == identity
    assert result["source"] == "server_non_admission_fence" and result["dispatch_permitted"] is False
    assert fence() == result
    with pytest.raises(AccessDenied, match="fenced"):
        admit()
    if kind == "request":
        with pytest.raises(AccessDenied):
            effects[1].start(person[0], identity)


@pytest.mark.parametrize("kind", ["request", "worker", "tool", "effect"])
def test_existing_admission_is_never_refunded_by_fencing(effects, kind):
    person = participant(effects)
    _, admit, fence = admission(effects, person, kind)
    admit()
    result = fence()
    assert result["state"] == "present" and "fence_id" not in result and "outcome" not in result
    assert not result["dispatch_permitted"]


@pytest.mark.parametrize("kind", ["request", "worker", "tool", "effect"])
def test_racing_late_admission_and_fence_cannot_both_succeed(effects, kind):
    person = participant(effects)
    _, admit, fence = admission(effects, person, kind)
    barrier = threading.Barrier(2)
    def dispatch():
        barrier.wait(5)
        try:
            admit()
            return True
        except AccessDenied:
            return False
    def tombstone():
        barrier.wait(5)
        return fence()
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch_result, fence_result = pool.submit(dispatch), pool.submit(tombstone)
        assert dispatch_result.result() == (fence_result.result()["state"] == "present")


def test_original_expired_revoked_host_lease_can_fence_but_foreign_lease_cannot(effects):
    tenant, hosts, resources, _ = effects
    first, second = participant(effects), participant(effects)
    resource_id = uid()
    with pytest.raises(AccessDenied):
        resources.fence_absent(first[0], resource_kind="worker", resource_id=resource_id, lease_id=second[2]["lease_id"])
    expire(tenant, "policy_leases", "expires_at", "lease_id", first[2]["lease_id"])
    hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, first[1]["revision"], "revoke_host", {"host_id": first[1]["host_id"]}))
    result = resources.fence_absent(first[0], resource_kind="worker", resource_id=resource_id, lease_id=first[2]["lease_id"])
    assert result["state"] == "fenced_absent"
    with pytest.raises(AccessDenied):
        resources.fence_absent(second[0], resource_kind="worker", resource_id=resource_id, lease_id=second[2]["lease_id"])


def test_audit_failure_cannot_claim_durable_absence(effects, monkeypatch):
    person = participant(effects)
    _, admit, fence = admission(effects, person, "worker")
    original = effects[0].store._audit
    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit unavailable")
    monkeypatch.setattr(effects[0].store, "_audit", fail)
    with pytest.raises(RuntimeError):
        fence()
    monkeypatch.setattr(effects[0].store, "_audit", original)
    assert admit()["dispatch_permitted"]
