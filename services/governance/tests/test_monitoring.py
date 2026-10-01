"""Real transactions distinguish reported metadata, delivery and actual outcomes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

import psycopg
import pytest

from sonn_governance.hosts import HostGovernance
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict, InvalidRequest
from sonn_governance.monitoring import RunMonitoring
from test_hosts import enroll, expire, lease
from test_store import Tenant, uid


def projection(**values):
    return {"version": 1, "epoch": 1, "local_revision": 5, "state": "running", "alert": "none",
            "counts": {"workers_active": 2, "workers_pending": 0, "requests_known": 3, "requests_held": 1,
                       "requests_unknown": 0, "checks_passed": 1, "checks_failed": 0}, **values}


@pytest.fixture
def setup(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.member(tenant.reader, projects={tenant.project: ["metadata_read"]})
    tenant.store.command(tenant.admin, tenant.command())
    hosts = HostGovernance(tenant.store)
    principal, active, _ = enroll(tenant, hosts)
    current = lease(hosts, principal)
    monitor = RunMonitoring(hosts)
    registration = {"command_id": uid(), "lease_id": current["lease_id"], "local_run_id": uid(), "session_id": uid()}
    binding = monitor.register(principal, **registration)
    return tenant, hosts, monitor, principal, active, current, registration, binding


def ingest(setup, sequence=1, **values):
    _, _, monitor, principal, _, _, _, binding = setup
    return monitor.ingest(principal, command_id=uid(), binding_id=binding["binding_id"], sequence=sequence, projection=projection(**values))


def request_stop(setup, **changes):
    tenant, _, monitor, _, _, _, _, binding = setup
    view = monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]
    values = {"binding_id": binding["binding_id"], "command_id": uid(), "expected_revision": view["revision"],
              "expected_epoch": 1, "expected_local_revision": 5, "operation": "stop", **changes}
    return values, monitor.request_control(tenant.admin, tenant.id, tenant.project, **values)


def test_registration_scopes_owner_from_cert_and_requires_current_authority(setup):
    tenant, hosts, monitor, principal, active, _, registration, binding = setup
    assert monitor.register(principal, **registration) == binding
    second, _, _ = enroll(tenant, hosts)
    with pytest.raises(AccessDenied):
        monitor.register(second, **registration)
    with pytest.raises(Conflict):
        monitor.register(principal, **{**registration, "command_id": uid()})
    hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, active["revision"], "revoke_host", {"host_id": active["host_id"]}))
    with pytest.raises(AccessDenied):
        monitor.register(principal, **registration)


def test_metadata_has_allowlisted_values_and_no_implicit_content_access(setup):
    tenant, _, monitor, _, _, _, registration, binding = setup
    ingest(setup)
    result = monitor.inspect(tenant.reader, tenant.id, tenant.project)
    row = result["runs"][0]
    assert row["projection"] == projection()
    assert row["evidence_source"] == "host_report" and row["sequence"] == 1
    assert registration["session_id"] not in str(result)
    assert registration["local_run_id"] not in str(result)
    assert "owner_actor" not in str(result)
    with pytest.raises(AccessDenied):
        monitor.inspect(tenant.reader, tenant.id, tenant.other_project)
    with pytest.raises(AccessDenied):
        monitor.inspect(tenant.reader, uid(), tenant.project)
    with pytest.raises(AccessDenied):
        monitor.request_control(tenant.reader, tenant.id, tenant.project, binding_id=binding["binding_id"], command_id=uid(),
                                expected_revision=row["revision"], expected_epoch=1, expected_local_revision=5, operation="stop")


@pytest.mark.parametrize("change", [
    {"objective": "private user prompt"}, {"state": "private failure message"}, {"version": True},
    {"epoch": 0}, {"alert": "http://credential@example.test"}, {"counts": {"requests_known": True}},
])
def test_unknown_or_content_projection_fields_are_rejected_before_persistence(setup, change):
    tenant, _, monitor, _, _, _, _, _ = setup
    with pytest.raises(InvalidRequest):
        ingest(setup, **change)
    assert monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]["sequence"] == 0


def test_reordered_and_conflicting_events_retain_visible_gaps_without_fabricated_success(setup):
    tenant, _, monitor, principal, _, _, _, binding = setup
    second = ingest(setup, sequence=2, local_revision=6, state="review")
    assert second["status"] == "gap" and second["expected_sequence"] == 1
    row = monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]
    assert row["projection"] is None and row["pending_events"] == 1
    first = ingest(setup)
    assert first["sequence"] == 2
    row = monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]
    assert row["projection"]["state"] == "review" and row["pending_events"] == 0
    with pytest.raises(Conflict):
        monitor.ingest(principal, command_id=uid(), binding_id=binding["binding_id"], sequence=2, projection=projection(state="completed"))
    stale = ingest(setup, sequence=3, local_revision=1)
    assert stale["status"] == "quarantined" and stale["sequence"] == 2


def test_two_hosts_cannot_report_or_ack_each_others_runs(setup):
    tenant, hosts, monitor, _, _, _, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    second, _, _ = enroll(tenant, hosts)
    current = lease(hosts, second)
    operations = [
        lambda: monitor.ingest(second, command_id=uid(), binding_id=binding["binding_id"], sequence=2, projection=projection()),
        lambda: monitor.poll_controls(second, binding_id=binding["binding_id"], lease_id=current["lease_id"]),
        lambda: monitor.observe_control(second, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="received"),
    ]
    for operation in operations:
        with pytest.raises(AccessDenied):
            operation()


def test_stop_request_poll_ack_and_final_observation_are_distinct_and_retry_cannot_dispatch(setup):
    tenant, _, monitor, principal, _, current, _, binding = setup
    ingest(setup)
    kwargs, control = request_stop(setup)
    assert not control["received_at"] and not control["outcome"] and not control["reported_processes_stopped"]
    assert monitor.request_control(tenant.admin, tenant.id, tenant.project, **kwargs) == control
    poll = {"binding_id": binding["binding_id"], "lease_id": current["lease_id"]}
    assert monitor.poll_controls(principal, **poll)["controls"] == [control]
    ack = {"binding_id": binding["binding_id"], "control_id": control["control_id"], "command_id": uid(), "outcome": "received"}
    received = monitor.observe_control(principal, **ack)
    assert received["dispatch_permitted"] and received["received_at"] and received["outcome"] is None
    assert not monitor.observe_control(principal, **ack)["dispatch_permitted"]
    final = {**ack, "command_id": uid(), "outcome": "applied", "processes_stopped": True}
    observed = monitor.observe_control(principal, **final)
    assert observed["reported_processes_stopped"] and observed["evidence_source"] == "host_report"
    assert not observed["dispatch_permitted"]
    assert monitor.observe_control(principal, **final) == observed
    assert monitor.poll_controls(principal, **poll)["controls"] == []
    with pytest.raises(Conflict):
        monitor.observe_control(principal, **{**final, "command_id": uid(), "outcome": "uncertain", "processes_stopped": False})


def test_concurrent_acknowledgements_grant_only_one_dispatch(setup):
    _, _, monitor, principal, _, _, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    kwargs = {"binding_id": binding["binding_id"], "control_id": control["control_id"], "command_id": uid(), "outcome": "received"}
    barrier = threading.Barrier(2)

    def acknowledge(_):
        barrier.wait(5)
        return monitor.observe_control(principal, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acknowledge, range(2)))
    assert sum(result["dispatch_permitted"] for result in results) == 1


def test_revision_or_membership_change_fences_queued_control_and_replay(setup):
    tenant, _, monitor, principal, _, current, _, binding = setup
    ingest(setup)
    kwargs, control = request_stop(setup)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read"])
    assert monitor.poll_controls(principal, binding_id=binding["binding_id"], lease_id=current["lease_id"])["controls"] == []
    with pytest.raises(AccessDenied):
        monitor.request_control(tenant.admin, tenant.id, tenant.project, **kwargs)
    with pytest.raises(AccessDenied):
        monitor.observe_control(principal, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="received")


def test_expired_control_cannot_be_acknowledged_or_applied(setup):
    tenant, _, monitor, principal, _, current, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    expire(tenant, "remote_controls", "expires_at", "control_id", control["control_id"])
    assert monitor.poll_controls(principal, binding_id=binding["binding_id"], lease_id=current["lease_id"])["controls"] == []
    with pytest.raises(AccessDenied):
        monitor.observe_control(principal, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="received")
    with pytest.raises(Conflict):
        monitor.observe_control(principal, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="applied")


def test_revoked_host_reports_old_uncertainty_without_new_admission_or_projected_success(setup):
    tenant, hosts, monitor, principal, active, current, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    args = {"binding_id": binding["binding_id"], "control_id": control["control_id"], "command_id": uid(), "outcome": "received"}
    monitor.observe_control(principal, **args)
    hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, active["revision"], "revoke_host", {"host_id": active["host_id"]}))
    result = monitor.observe_control(principal, **{**args, "command_id": uid(), "outcome": "uncertain"})
    assert result["outcome"] == "uncertain" and not result["dispatch_permitted"]
    reported = ingest(setup, sequence=2, state="completed", local_revision=6)
    assert reported["status"] == "quarantined"
    row = monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]
    assert row["projection"]["state"] == "running" and row["host_state"] == "revoked"
    with pytest.raises(AccessDenied):
        monitor.poll_controls(principal, binding_id=binding["binding_id"], lease_id=current["lease_id"])


def test_audit_failure_rolls_back_metadata_and_control(setup, monkeypatch):
    tenant, _, monitor, _, _, _, _, binding = setup
    ingest(setup)
    before = monitor.inspect(tenant.admin, tenant.id, tenant.project)

    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit unavailable")

    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError):
        ingest(setup, sequence=2, local_revision=6)
    with pytest.raises(RuntimeError):
        monitor.request_control(tenant.admin, tenant.id, tenant.project, binding_id=binding["binding_id"], command_id=uid(),
                                expected_revision=before["runs"][0]["revision"], expected_epoch=1, expected_local_revision=5, operation="stop")
    monkeypatch.undo()
    assert monitor.inspect(tenant.admin, tenant.id, tenant.project) == before


def test_runtime_role_cannot_rewrite_original_host_evidence(setup):
    tenant, _, _, _, _, _, _, _ = setup
    ingest(setup)
    for table in ("run_events", "monitor_receipts"):
        with psycopg.connect(tenant.config["application_dsn"]) as connection:
            connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(f"DELETE FROM sonn_governance.{table}")
            connection.rollback()


def test_expired_certificate_does_not_release_delayed_acknowledgement(setup, monkeypatch):
    tenant, _, monitor, principal, _, _, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    principal = replace(principal, expires_at=time.time() + 1)
    original = tenant.store._audit

    def delay(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(1.1)
        return result

    monkeypatch.setattr(tenant.store, "_audit", delay)
    with pytest.raises(AccessDenied):
        monitor.observe_control(principal, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="received")


def test_new_local_epoch_fences_old_control_before_delivery(setup):
    _, _, monitor, principal, _, current, _, binding = setup
    ingest(setup)
    _, control = request_stop(setup)
    ingest(setup, sequence=2, epoch=2, local_revision=6)
    assert monitor.poll_controls(principal, binding_id=binding["binding_id"], lease_id=current["lease_id"])["controls"] == []
    with pytest.raises(AccessDenied):
        monitor.observe_control(principal, binding_id=binding["binding_id"], control_id=control["control_id"], command_id=uid(), outcome="received")


@pytest.mark.parametrize("state", ["pausing", "stopping"])
def test_pending_cleanup_control_state_is_not_reported_as_finished(setup, state):
    tenant, _, monitor, _, _, _, _, _ = setup
    result = ingest(setup, state=state, alert="unconfirmed_stop")
    assert result["status"] == "recorded"
    row = monitor.inspect(tenant.admin, tenant.id, tenant.project)["runs"][0]
    assert row["projection"]["state"] == state
    assert row["projection"]["counts"]["workers_active"] == 2
    assert row["projection"]["alert"] == "unconfirmed_stop"


@pytest.mark.parametrize("revoke", ["provisioned_active", "host_admin"])
def test_owner_deprovisioning_or_enrollment_grant_removal_quarantines_late_report(setup, revoke):
    tenant, _, monitor, _, _, _, _, _ = setup
    ingest(setup)
    if revoke == "host_admin":
        tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "metadata_read"])
    else:
        # This is the exact separate suspension flag written by the qualified
        # SCIM transaction; manual grants cannot undo it.
        with psycopg.connect(tenant.config["owner_dsn"]) as connection:
            connection.execute("UPDATE sonn_governance.memberships SET provisioned_active=false WHERE tenant_id=%s AND actor_id=%s", (tenant.id, tenant.admin.actor_id))
    result = ingest(setup, sequence=2, local_revision=6, state="completed")
    assert result["status"] == "quarantined" and result["sequence"] == 1
    row = monitor.inspect(tenant.reader, tenant.id, tenant.project)["runs"][0]
    assert row["projection"]["state"] == "running"
