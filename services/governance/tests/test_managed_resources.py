"""Real PostgreSQL races and originating-request admission for managed runners."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading

import pytest

from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict
from sonn_governance.monitoring import RunMonitoring
from test_hosts import enroll, expire
from test_store import Tenant, uid


@pytest.fixture
def managed(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin"])
    tenant.store.command(tenant.admin, tenant.command(max_workers=1))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    return tenant, hosts, ManagedResources(hosts), RunMonitoring(hosts)


def participant(managed):
    tenant, hosts, resources, monitor = managed
    principal, active, _ = enroll(tenant, hosts)
    lease = hosts.lease(principal, uid(), 1, runner_protocol=2)
    binding = monitor.register(principal, command_id=uid(), lease_id=lease["lease_id"], local_run_id=uid(), session_id=uid())
    return principal, active, lease, binding


def worker(managed, participant):
    principal, _, lease, binding = participant
    values = {"lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "worker_id": uid(), "kind": "worker"}
    result = managed[2].reserve_worker(principal, **values)
    assert result["dispatch_permitted"]
    return values


def request(managed, participant, worker, *, purpose="primary"):
    principal, _, lease, _ = participant
    values = {"lease_id": lease["lease_id"], "worker_id": worker["worker_id"], "request_id": uid(),
              "purpose": purpose, "model": {"provider": "ollama", "model": "fixture"}, "input_sha256": "a" * 64}
    managed[1].reserve(principal, lease["lease_id"], values["request_id"])
    managed[2].bind_request(principal, **values)
    return values


def test_protocol_floor_cannot_be_bypassed_with_old_or_unbound_lease(managed):
    _, hosts, _, _ = managed
    principal, _, _, _ = participant(managed)
    with pytest.raises(AccessDenied, match="upgrade"):
        hosts.lease(principal, uid(), 1)
    historical = HostGovernance(hosts.store)
    old = historical.lease(principal, uid(), 1)
    with pytest.raises(AccessDenied):
        hosts.reserve(principal, old["lease_id"], uid())
    current = hosts.lease(principal, uid(), 1, runner_protocol=2)
    assert current["runner_protocol"] == 2 and current["offline_request_allowance"] == 0
    assert 0 < (datetime.fromisoformat(current["expires_at"]) - datetime.fromisoformat(current["issued_at"])).total_seconds() <= 60
    request_id = uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    with pytest.raises(AccessDenied, match="binding"):
        hosts.start(principal, request_id)
    # A caller cannot bypass binding by choosing the legacy facade.
    with pytest.raises(AccessDenied, match="binding"):
        historical.start(principal, request_id)


def test_two_hosts_race_one_shared_slot_and_uncertainty_does_not_release_it(managed):
    _, _, resources, _ = managed
    first, second = participant(managed), participant(managed)
    barrier = threading.Barrier(2)
    def reserve(person):
        principal, _, lease, binding = person
        values = {"lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "worker_id": uid(), "kind": "worker"}
        barrier.wait(5)
        try:
            return principal, values, resources.reserve_worker(principal, **values)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [item for item in executor.map(reserve, [first, second]) if item]
    assert len(winners) == 1
    principal, values, result = winners[0]
    assert result["dispatch_permitted"]
    assert not resources.reserve_worker(principal, **values)["dispatch_permitted"]
    assert resources.observe_worker(principal, worker_id=values["worker_id"], outcome="uncertain")["slot_held"]
    with pytest.raises(Conflict, match="exhausted"):
        worker(managed, first)
    resources.observe_worker(principal, worker_id=values["worker_id"], outcome="stopped")
    assert not resources.reserve_worker(principal, **values)["dispatch_permitted"]
    worker(managed, first)


def test_bound_primary_request_and_action_have_exact_once_authority(managed):
    _, hosts, resources, _ = managed
    person = participant(managed)
    principal, _, lease, _ = person
    slot = worker(managed, person)
    bound = request(managed, person, slot)
    assert resources.bind_request(principal, **bound)["bound"]
    with pytest.raises(Conflict, match="different semantics"):
        resources.bind_request(principal, **{**bound, "input_sha256": "b" * 64})
    assert hosts.start(principal, bound["request_id"])["dispatch_permitted"]
    assert not hosts.start(principal, bound["request_id"])["dispatch_permitted"]
    action = {"lease_id": lease["lease_id"], "worker_id": slot["worker_id"], "request_id": bound["request_id"],
              "action_id": uid(), "tool_name": "file_read", "arguments_sha256": "c" * 64}
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, **action)
    hosts.settle(principal, bound["request_id"], "completed")
    assert resources.authorize_tool(principal, **action)["dispatch_permitted"]
    assert not resources.authorize_tool(principal, **action)["dispatch_permitted"]
    with pytest.raises(Conflict, match="different semantics"):
        resources.authorize_tool(principal, **{**action, "arguments_sha256": "d" * 64})
    resources.observe_tool(principal, action_id=action["action_id"], outcome="uncertain")
    assert resources.observe_tool(principal, action_id=action["action_id"], outcome="completed")["state"] == "completed"
    with pytest.raises(Conflict):
        resources.observe_tool(principal, action_id=action["action_id"], outcome="uncertain")
    with pytest.raises(Conflict, match="bound request"):
        resources.observe_worker(principal, worker_id=slot["worker_id"], outcome="never_started")


@pytest.mark.parametrize("purpose", ["planning", "compression"])
def test_auxiliary_requests_cannot_authorize_tool_effects(managed, purpose):
    _, hosts, resources, _ = managed
    person = participant(managed)
    principal, _, lease, _ = person
    slot = worker(managed, person)
    bound = request(managed, person, slot, purpose=purpose)
    hosts.start(principal, bound["request_id"])
    hosts.settle(principal, bound["request_id"], "completed")
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, lease_id=lease["lease_id"], worker_id=slot["worker_id"], request_id=bound["request_id"],
                                 action_id=uid(), tool_name="file_read", arguments_sha256="a" * 64)


def test_foreign_worker_and_policy_changes_cannot_borrow_request(managed):
    tenant, hosts, resources, _ = managed
    person, other = participant(managed), participant(managed)
    principal, _, lease, _ = person
    slot = worker(managed, person)
    bound = request(managed, person, slot)
    with pytest.raises(AccessDenied):
        resources.bind_request(principal, **{**bound, "model": {"provider": "ollama", "model": "unapproved"}})
    with pytest.raises(AccessDenied):
        resources.bind_request(other[0], **bound)
    hosts.start(principal, bound["request_id"])
    hosts.settle(principal, bound["request_id"], "completed")
    action = {"lease_id": lease["lease_id"], "worker_id": slot["worker_id"], "request_id": bound["request_id"],
              "action_id": uid(), "tool_name": "file_write", "arguments_sha256": "a" * 64}
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, **action)
    tenant.store.command(tenant.admin, tenant.command(expected=1, allowed_models=[{"provider": "ollama", "model": "replacement"}]))
    fresh = hosts.lease(principal, uid(), 2, runner_protocol=2)
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, **{**action, "lease_id": fresh["lease_id"], "tool_name": "file_read"})


def test_revocation_stops_new_admission_but_keeps_cleanup_observations(managed):
    tenant, hosts, resources, _ = managed
    person = participant(managed)
    principal, active, lease, _ = person
    slot = worker(managed, person)
    bound = request(managed, person, slot)
    hosts.start(principal, bound["request_id"])
    hosts.settle(principal, bound["request_id"], "completed")
    action = {"lease_id": lease["lease_id"], "worker_id": slot["worker_id"], "request_id": bound["request_id"],
              "action_id": uid(), "tool_name": "file_read", "arguments_sha256": "a" * 64}
    resources.authorize_tool(principal, **action)
    hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, active["revision"], "revoke_host", {"host_id": active["host_id"]}))
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, **action)
    with pytest.raises(AccessDenied):
        resources.reserve_worker(principal, **slot)
    assert resources.observe_tool(principal, action_id=action["action_id"], outcome="completed")["state"] == "completed"
    assert not resources.observe_worker(principal, worker_id=slot["worker_id"], outcome="stopped")["slot_held"]


def test_audit_failure_rolls_back_worker_admission_and_expired_lease_cannot_retry(managed, monkeypatch):
    tenant, _, resources, _ = managed
    person = participant(managed)
    principal, _, lease, binding = person
    values = {"lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "worker_id": uid(), "kind": "worker"}
    original = tenant.store._audit
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError):
        resources.reserve_worker(principal, **values)
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert resources.reserve_worker(principal, **values)["dispatch_permitted"]
    expire(tenant, "policy_leases", "expires_at", "lease_id", lease["lease_id"])
    with pytest.raises(AccessDenied):
        resources.reserve_worker(principal, **values)


def test_audit_delay_cannot_extend_admission_expiry(managed, monkeypatch):
    tenant, _, resources, _ = managed
    person = participant(managed)
    principal, _, lease, binding = person
    original = tenant.store._audit
    def expire_before_receipt(connection, *args, **kwargs):
        # The operator fixture changes only this exact lease while the runtime
        # transaction is open, exercising the final check after audit capture.
        expire(tenant, "policy_leases", "expires_at", "lease_id", lease["lease_id"])
        return original(connection, *args, **kwargs)
    monkeypatch.setattr(tenant.store, "_audit", expire_before_receipt)
    with pytest.raises(AccessDenied):
        resources.reserve_worker(principal, lease_id=lease["lease_id"], binding_id=binding["binding_id"], worker_id=uid(), kind="worker")
    monkeypatch.setattr(tenant.store, "_audit", original)
    fresh = managed[1].lease(principal, uid(), 1, runner_protocol=2)
    worker(managed, (principal, person[1], fresh, binding))


def test_confirmed_failed_request_consumes_unit_but_never_authorizes_tool(managed):
    _, hosts, resources, _ = managed
    person = participant(managed)
    principal, _, lease, _ = person
    slot = worker(managed, person)
    bound = request(managed, person, slot)
    with pytest.raises(Conflict, match="no admitted"):
        hosts.settle(principal, bound["request_id"], "failed")
    hosts.start(principal, bound["request_id"])
    result = hosts.settle(principal, bound["request_id"], "failed")
    assert result["state"] == "failed" and result["consumed_units"] == 1 and result["held_units"] == 0
    assert hosts.settle(principal, bound["request_id"], "failed") == result
    with pytest.raises(Conflict):
        hosts.settle(principal, bound["request_id"], "completed")
    with pytest.raises(Conflict):
        hosts.settle(principal, bound["request_id"], "never_started")
    with pytest.raises(AccessDenied):
        resources.authorize_tool(principal, lease_id=lease["lease_id"], worker_id=slot["worker_id"], request_id=bound["request_id"],
                                 action_id=uid(), tool_name="file_read", arguments_sha256="a" * 64)
