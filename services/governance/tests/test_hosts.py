"""Real PostgreSQL host authority and project-wide request accounting races."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import secrets
import threading
import time

import psycopg
import pytest

from sonn_governance.hosts import HostGovernance
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict, HostPrincipal, Query
from test_store import Tenant, uid


@pytest.fixture
def setup(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "audit_read"])
    tenant.store.command(tenant.admin, tenant.command(request_limit=2))
    return tenant, HostGovernance(tenant.store)


def enroll(tenant, hosts, *, activate=True):
    principal = HostPrincipal(secrets.token_hex(32), time.time() + 3600)
    command = CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host", {
        "host_id": uid(), "certificate_sha256": principal.certificate_sha256,
    })
    result = hosts.owner_command(tenant.admin, command)
    if activate:
        result = hosts.activate(principal, result["challenge"])
    return principal, result, command


def lease(hosts, principal, revision=1):
    return hosts.lease(principal, uid(), revision)


def expire(tenant, table, field, identity_field, identity):
    with psycopg.connect(tenant.config["owner_dsn"]) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
        connection.execute(f"UPDATE sonn_governance.{table} SET {field}=clock_timestamp()-interval '1 second' "
                           f"WHERE tenant_id=%s AND {identity_field}=%s", (tenant.id, identity))


def test_enrollment_requires_exact_certificate_proof_and_current_human_grant(setup):
    tenant, hosts = setup
    principal, pending, command = enroll(tenant, hosts, activate=False)
    assert pending == hosts.owner_command(tenant.admin, command)
    with pytest.raises(AccessDenied):
        lease(hosts, principal)
    with pytest.raises(AccessDenied):
        hosts.activate(HostPrincipal(secrets.token_hex(32), time.time() + 100), pending["challenge"])
    with pytest.raises(AccessDenied):
        hosts.activate(principal, "wrong")
    active = hosts.activate(principal, pending["challenge"])
    assert active["revision"] == 2
    assert active["host_generation"] == 1
    assert active == hosts.activate(principal, pending["challenge"])
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin"])
    with pytest.raises(AccessDenied):
        hosts.owner_command(tenant.admin, command)


def test_expired_challenge_cannot_activate_and_cert_cannot_be_reenrolled(setup):
    tenant, hosts = setup
    principal, pending, command = enroll(tenant, hosts, activate=False)
    expire(tenant, "hosts", "challenge_expires_at", "host_id", pending["host_id"])
    with pytest.raises(AccessDenied):
        hosts.activate(principal, pending["challenge"])
    other = replace(command, command_id=uid(), payload={**command.payload, "host_id": uid()})
    with pytest.raises(Conflict, match="already reserved"):
        hosts.owner_command(tenant.admin, other)


def test_online_lease_is_exact_policy_has_no_offline_allowance_or_caller_scope(setup):
    tenant, hosts = setup
    principal, active, _ = enroll(tenant, hosts)
    command_id = uid()
    result = hosts.lease(principal, command_id, 1)
    assert hosts.lease(principal, command_id, 1) == result
    assert result["tenant_id"] == tenant.id
    assert result["project_id"] == tenant.project
    assert result["host_id"] == active["host_id"]
    assert result["host_generation"] == active["host_generation"]
    assert result["offline_request_allowance"] == 0
    assert result["enforcement_mode"] == "managed_local_reporting"
    assert result["policy"]["allowed_tools"] == ["file_read"]
    with pytest.raises(Conflict):
        hosts.lease(principal, command_id, 2)
    with pytest.raises(AccessDenied):
        lease(hosts, replace(principal, expires_at=time.time() - 1))


def test_project_quota_atomic_across_two_hosts_and_uncertainty_is_retained(setup):
    tenant, hosts = setup
    tenant.store.command(tenant.admin, tenant.command(expected=1, request_limit=1))
    first, _, _ = enroll(tenant, hosts)
    second, _, _ = enroll(tenant, hosts)
    first_lease, second_lease = lease(hosts, first, 2), lease(hosts, second, 2)
    barrier = threading.Barrier(2)

    def reserve(args):
        principal, current = args
        barrier.wait(5)
        request_id = uid()
        try:
            hosts.reserve(principal, current["lease_id"], request_id)
            return principal, request_id
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [(first, first_lease), (second, second_lease)]))
    winners = [result for result in results if result]
    assert len(winners) == 1
    principal, request_id = winners[0]
    hosts.start(principal, request_id)
    result = hosts.settle(principal, request_id, "uncertain")
    assert result["held_units"] == 1 and result["consumed_units"] is None
    with pytest.raises(Conflict, match="cannot be refunded"):
        hosts.settle(principal, request_id, "never_started")
    with pytest.raises(Conflict, match="exhausted"):
        hosts.reserve(second, second_lease["lease_id"], uid())
    usage = hosts.inspect(tenant.admin, Query(tenant.id, tenant.project, "metadata"))["usage"]
    assert usage == {"consumed_units": 0, "held_units": 1}


def test_start_is_once_even_for_parallel_lost_reply_retries(setup):
    tenant, hosts = setup
    principal, _, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    original = hosts.reserve(principal, current["lease_id"], request_id)
    assert hosts.reserve(principal, current["lease_id"], request_id) == original
    barrier = threading.Barrier(4)

    def start(_):
        barrier.wait(5)
        return hosts.start(principal, request_id)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(start, range(4)))
    assert sum(row["dispatch_permitted"] for row in results) == 1
    hosts.settle(principal, request_id, "completed")
    assert hosts.start(principal, request_id)["dispatch_permitted"] is False


def test_foreign_host_request_lease_and_changed_reservation_denied(setup):
    tenant, hosts = setup
    first, _, _ = enroll(tenant, hosts)
    second, _, _ = enroll(tenant, hosts)
    original, another, request_id = lease(hosts, first), lease(hosts, first), uid()
    hosts.reserve(first, original["lease_id"], request_id)
    with pytest.raises(Conflict, match="different semantics"):
        hosts.reserve(first, another["lease_id"], request_id)
    with pytest.raises(AccessDenied):
        hosts.reserve(second, original["lease_id"], uid())
    with pytest.raises(AccessDenied):
        hosts.start(second, request_id)
    with pytest.raises(AccessDenied):
        hosts.settle(second, request_id, "never_started")
    with pytest.raises(AccessDenied):
        hosts.reserve(second, lease(hosts, second)["lease_id"], request_id)


def test_revocation_fences_admission_and_replay_but_keeps_exact_observation(setup):
    tenant, hosts = setup
    principal, active, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    hosts.start(principal, request_id)
    command = CommandEnvelope(1, uid(), tenant.id, tenant.project, 2, "revoke_host", {"host_id": active["host_id"]})
    result = hosts.owner_command(tenant.admin, command)
    assert result == hosts.owner_command(tenant.admin, command)
    for operation in (lambda: hosts.reserve(principal, current["lease_id"], request_id),
                      lambda: hosts.start(principal, request_id), lambda: lease(hosts, principal)):
        with pytest.raises(AccessDenied):
            operation()
    assert hosts.settle(principal, request_id, "uncertain")["held_units"] == 1
    completed = hosts.settle(principal, request_id, "completed")
    assert completed["consumed_units"] == 1
    assert completed == hosts.settle(principal, request_id, "completed")
    with pytest.raises(Conflict):
        hosts.settle(principal, request_id, "never_started")


def test_expired_lease_retains_reserved_unit_until_explicit_never_started(setup):
    tenant, hosts = setup
    principal, _, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    expire(tenant, "policy_leases", "expires_at", "lease_id", current["lease_id"])
    with pytest.raises(AccessDenied):
        hosts.start(principal, request_id)
    with pytest.raises(AccessDenied):
        hosts.reserve(principal, current["lease_id"], request_id)
    assert hosts.inspect(tenant.admin, Query(tenant.id, tenant.project, "metadata"))["usage"]["held_units"] == 1
    assert hosts.settle(principal, request_id, "never_started")["consumed_units"] == 0
    with pytest.raises(Conflict):
        hosts.settle(principal, request_id, "completed")


def test_policy_revisions_do_not_reset_global_consumed_allowance(setup):
    tenant, hosts = setup
    principal, _, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    hosts.start(principal, request_id)
    hosts.settle(principal, request_id, "completed")
    tenant.store.command(tenant.admin, tenant.command(expected=1, request_limit=1))
    with pytest.raises(Conflict, match="renewal"):
        hosts.reserve(principal, current["lease_id"], uid())
    fresh = lease(hosts, principal, 2)
    with pytest.raises(Conflict, match="exhausted"):
        hosts.reserve(principal, fresh["lease_id"], uid())
    tenant.store.command(tenant.admin, tenant.command(expected=2, request_limit=2))
    hosts.reserve(principal, lease(hosts, principal, 3)["lease_id"], uid())
    assert hosts.inspect(tenant.admin, Query(tenant.id, tenant.project, "metadata"))["usage"] == {
        "consumed_units": 1, "held_units": 1,
    }


def test_quota_audit_failure_rolls_back_reservation(setup, monkeypatch):
    tenant, hosts = setup
    principal, _, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    original = tenant.store._audit

    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(tenant.store, "_audit", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        hosts.reserve(principal, current["lease_id"], request_id)
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert hosts.inspect(tenant.admin, Query(tenant.id, tenant.project, "metadata"))["usage"]["held_units"] == 0
    assert hosts.reserve(principal, current["lease_id"], request_id)["state"] == "reserved"


def test_monitoring_does_not_disclose_challenge_cert_or_foreign_project(setup, database_config):
    tenant, hosts = setup
    enroll(tenant, hosts)
    result = hosts.inspect(tenant.admin, Query(tenant.id, tenant.project, "metadata"))
    assert set(result["hosts"][0]) == {"host_id", "state", "revision", "generation"}
    other = Tenant(database_config)
    with pytest.raises(AccessDenied):
        hosts.inspect(other.admin, Query(tenant.id, tenant.project, "metadata"))
    with pytest.raises(AccessDenied):
        hosts.inspect(tenant.reader, Query(tenant.id, tenant.project, "metadata"))


def test_deactivated_enrolling_member_fences_host_but_preserves_observations(setup):
    tenant, hosts = setup
    tenant.member(tenant.reader, projects={tenant.project: ["host_admin"]})
    principal = HostPrincipal(secrets.token_hex(32), time.time() + 3600)
    command = CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host", {
        "host_id": uid(), "certificate_sha256": principal.certificate_sha256,
    })
    pending = hosts.owner_command(tenant.reader, command)
    hosts.activate(principal, pending["challenge"])
    current, request_id = lease(hosts, principal), uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    hosts.start(principal, request_id)
    tenant.member(tenant.reader, active=False)
    with pytest.raises(AccessDenied):
        lease(hosts, principal)
    with pytest.raises(AccessDenied):
        hosts.start(principal, request_id)
    assert hosts.settle(principal, request_id, "uncertain")["held_units"] == 1


def test_expiry_crossed_during_audit_rolls_back_start(setup, monkeypatch):
    tenant, hosts = setup
    principal, _, _ = enroll(tenant, hosts)
    current, request_id = lease(hosts, principal), uid()
    hosts.reserve(principal, current["lease_id"], request_id)
    original = tenant.store._audit

    # Granting UPDATE solely inside the fixture would weaken the runtime role.
    # Instead observe expiry after a bounded real deadline during audit work.
    with psycopg.connect(tenant.config["owner_dsn"]) as connection:
        connection.execute("UPDATE sonn_governance.policy_leases SET expires_at=clock_timestamp()+interval '0.5 second' "
                           "WHERE tenant_id=%s AND lease_id=%s", (tenant.id, current["lease_id"]))

    def wait_past_deadline(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.6)
        return result

    monkeypatch.setattr(tenant.store, "_audit", wait_past_deadline)
    with pytest.raises(AccessDenied):
        hosts.start(principal, request_id)
    monkeypatch.setattr(tenant.store, "_audit", original)
    # The failed start transaction must not leave a start marker or receipt.
    assert hosts.settle(principal, request_id, "never_started")["consumed_units"] == 0
