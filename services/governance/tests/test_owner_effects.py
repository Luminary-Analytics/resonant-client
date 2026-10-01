"""Owner process admission is separate from model/tool authority."""
# ruff: noqa: F811 -- real transport fixtures are imported intentionally.

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import AccessDenied, CommandEnvelope, Conflict, InvalidRequest, policy_document
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates, client_for  # noqa: F401
from test_hosts import expire
from test_managed_resources import participant
from test_store import Tenant, policy, uid


@pytest.fixture
def effects(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(policy_version=2, allowed_effects=["candidate_check", "writer_git"]))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    return tenant, hosts, ManagedResources(hosts), RunMonitoring(hosts)


def invocation(person, **overrides):
    return {"lease_id": person[2]["lease_id"], "binding_id": person[3]["binding_id"], "effect_id": uid(),
            "kind": "candidate_check", "semantics_sha256": "a" * 64, **overrides}


def test_effect_replay_never_reissues_permit_and_final_outcomes_are_immutable(effects):
    person = participant(effects)
    resource, principal = effects[2], person[0]
    values = invocation(person)
    assert resource.authorize_effect(principal, **values)["dispatch_permitted"]
    assert not resource.authorize_effect(principal, **values)["dispatch_permitted"]
    with pytest.raises(Conflict):
        resource.authorize_effect(principal, **{**values, "semantics_sha256": "b" * 64})
    resource.observe_effect(principal, effect_id=values["effect_id"], outcome="uncertain")
    result = resource.observe_effect(principal, effect_id=values["effect_id"], outcome="failed")
    assert result["source"] == "authenticated_host_observation" and not result["dispatch_permitted"]
    with pytest.raises(Conflict):
        resource.observe_effect(principal, effect_id=values["effect_id"], outcome="completed")


def test_concurrent_duplicate_effect_admits_only_one_process(effects):
    person = participant(effects)
    values, barrier = invocation(person), threading.Barrier(2)
    def authorize():
        barrier.wait(5)
        return effects[2].authorize_effect(person[0], **values)["dispatch_permitted"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(authorize) for _ in range(2)]
        assert sorted(future.result() for future in futures) == [False, True]


def test_policy_v1_missing_effect_and_membership_revocation_block_new_processes(effects):
    tenant, hosts, resource, _ = effects
    person = participant(effects)
    values = invocation(person)
    with pytest.raises(AccessDenied):
        resource.authorize_effect(person[0], **{**values, "kind": "checkout_apply"})
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin"])
    with pytest.raises(AccessDenied):
        resource.authorize_effect(person[0], **values)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "control_execute"])
    resource.authorize_effect(person[0], **values)
    tenant.store.command(tenant.admin, tenant.command(expected=1))
    lease = hosts.lease(person[0], uid(), 2, runner_protocol=2)
    with pytest.raises(AccessDenied):
        resource.authorize_effect(person[0], **{**values, "lease_id": lease["lease_id"]})
    assert resource.observe_effect(person[0], effect_id=values["effect_id"], outcome="completed")["state"] == "completed"


def test_foreign_host_cannot_use_binding_or_observe_other_effect(effects):
    first, second = participant(effects), participant(effects)
    values = invocation(first)
    resource = effects[2]
    with pytest.raises(AccessDenied):
        resource.authorize_effect(second[0], **{**values, "lease_id": second[2]["lease_id"]})
    resource.authorize_effect(first[0], **values)
    with pytest.raises(AccessDenied):
        resource.observe_effect(second[0], effect_id=values["effect_id"], outcome="completed")


def test_failed_audit_and_delayed_expiry_cannot_leave_an_admission(effects, monkeypatch):
    tenant, _, resource, _ = effects
    person, original = participant(effects), tenant.store._audit
    values = invocation(person)
    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit unavailable")
    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError):
        resource.authorize_effect(person[0], **values)
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert resource.authorize_effect(person[0], **values)["dispatch_permitted"]
    def delay(connection, *args, **kwargs):
        expire(tenant, "policy_leases", "expires_at", "lease_id", person[2]["lease_id"])
        return original(connection, *args, **kwargs)
    monkeypatch.setattr(tenant.store, "_audit", delay)
    with pytest.raises(AccessDenied):
        resource.authorize_effect(person[0], **invocation(person))


@pytest.mark.parametrize("extra", [{"allowed_effects": ["candidate_check"]},
    {"policy_version": 2}, {"policy_version": 2, "allowed_effects": ["shell"]},
    {"policy_version": 2, "allowed_effects": ["writer_git", "writer_git"]}])
def test_policy_effects_are_explicit_versioned_and_bounded(extra):
    with pytest.raises(InvalidRequest):
        policy_document(policy(**extra))


def test_mtls_effect_dispatch_and_revoked_cleanup_use_exact_certificate(database_config, certificates):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(policy_version=2, allowed_effects=["candidate_check"]))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    resource, monitor = ManagedResources(hosts), RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    with actual_host(hosts, certificates, monitoring=monitor, resources=resource) as (url, _):
        with client_for(url, certificates, certificates.first) as client:
            assert client.post("/v1/activate", json={"challenge": pending["challenge"]}).status_code == 200
            lease = client.post("/v1/leases", json={"command_id": uid(), "expected_policy_revision": 1, "runner_protocol": 2}).json()
            binding = client.post("/v1/runs/register", json={"command_id": uid(), "lease_id": lease["lease_id"], "local_run_id": uid(), "session_id": uid()}).json()
            values = {"lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "effect_id": uid(),
                "kind": "candidate_check", "semantics_sha256": "c" * 64}
            assert client.post("/v1/resources/effects/authorize", json=values).json()["dispatch_permitted"]
            assert not client.post("/v1/resources/effects/authorize", json=values).json()["dispatch_permitted"]
            hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 2, "revoke_host", {"host_id": pending["host_id"]}))
            assert client.post("/v1/resources/effects/authorize", json=values).status_code == 403
            result = client.post("/v1/resources/effects/observe", json={"effect_id": values["effect_id"], "outcome": "never_started"})
            assert result.status_code == 200 and result.json()["state"] == "never_started"
