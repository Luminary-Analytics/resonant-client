"""Independent certified clients exercise enrollment and accounting over real TLS."""
# ruff: noqa: F811 -- imported pytest fixtures are intentionally used by name.

import uuid

from sonn_governance.app import create_app
from sonn_governance.hosts import HostGovernance
from sonn_governance.models import CommandEnvelope, Principal, Query
from sonn_governance.store import GovernanceStore, bootstrap
from test_host_http import actual_host, certificates, client_for  # noqa: F401
from test_identity import actual_http, identity_fixture  # noqa: F401


def test_human_enrollment_two_certified_hosts_lease_replay_revocation_and_uncertainty(
        database_config, identity_fixture, certificates):
    identity = identity_fixture
    owner = identity.identity.authenticate("Bearer " + identity.token())
    tenant, project, other_tenant, other_project = [str(uuid.uuid4()) for _ in range(4)]
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=owner, project_ids=[project])
    bootstrap(database_config["owner_dsn"], tenant_id=other_tenant,
        administrator=Principal(owner.issuer, "different-owner", owner.expires_at), project_ids=[other_project])
    core = GovernanceStore(database_config["application_dsn"])
    hosts = HostGovernance(core)
    core.command(owner, CommandEnvelope(1, str(uuid.uuid4()), tenant, None, 0, "set_membership", {
        "actor_id": owner.actor_id, "active": True, "tenant_permissions": ["membership_admin", "policy_admin"],
        "project_permissions": {project: ["host_admin", "metadata_read"]}}))
    core.command(owner, CommandEnvelope(1, str(uuid.uuid4()), tenant, project, 0, "set_policy", {"policy": {
        "policy_version": 1, "allowed_models": [{"provider": "fixture", "model": "selected"}],
        "allowed_tools": ["file_read"], "max_workers": 2, "request_limit": 2, "content_mode": "none"}}))
    base = f"/v1/tenants/{tenant}/projects/{project}/hosts/commands"
    def envelope(operation, revision, payload):
        return {"protocol_version": 1, "command_id": str(uuid.uuid4()), "expected_revision": revision,
                "operation": operation, "payload": payload}
    with actual_http(create_app(identity.identity, core, hosts=hosts)) as human, actual_host(hosts, certificates) as (url, _):
        human.headers["Authorization"] = "Bearer " + identity.token()
        pending = []
        for certificate in (certificates.first, certificates.second):
            request = envelope("enroll_host", 0, {"host_id": str(uuid.uuid4()), "certificate_sha256": certificate.fingerprint})
            response = human.post(base, json=request)
            assert response.status_code == 200 and response.json()["state"] == "pending"
            assert human.post(base, json=request).json() == response.json()
            pending.append(response.json())
        foreign = human.post(base.replace(tenant, other_tenant).replace(project, other_project),
            json=envelope("enroll_host", 0, {"host_id": str(uuid.uuid4()), "certificate_sha256": certificates.unknown.fingerprint}))
        assert foreign.status_code == 403
        with client_for(url, certificates, certificates.first) as first, client_for(url, certificates, certificates.second) as second:
            lease_command = {"command_id": str(uuid.uuid4()), "expected_policy_revision": 1}
            assert first.post("/v1/leases", json=lease_command).status_code == 403
            assert second.post("/v1/activate", json={"challenge": pending[0]["challenge"]}).status_code == 403
            activation = first.post("/v1/activate", json={"challenge": pending[0]["challenge"]})
            assert activation.status_code == 200 and activation.json()["host_id"] == pending[0]["host_id"]
            assert first.post("/v1/activate", json={"challenge": pending[0]["challenge"]}).json() == activation.json()
            assert second.post("/v1/activate", json={"challenge": pending[1]["challenge"]}).status_code == 200
            first_lease = first.post("/v1/leases", json=lease_command)
            second_lease = second.post("/v1/leases", json={"command_id": str(uuid.uuid4()), "expected_policy_revision": 1})
            assert first_lease.status_code == second_lease.status_code == 200
            assert first_lease.json()["offline_request_allowance"] == 0
            assert first_lease.json()["enforcement_mode"] == "managed_local_reporting"
            assert first.post("/v1/leases", json=lease_command).json() == first_lease.json()
            first_request, second_request = str(uuid.uuid4()), str(uuid.uuid4())
            for client, lease, request_id in ((first, first_lease, first_request), (second, second_lease, second_request)):
                request = {"lease_id": lease.json()["lease_id"], "request_id": request_id}
                response = client.post("/v1/requests/reserve", json=request)
                assert response.status_code == 200 and client.post("/v1/requests/reserve", json=request).json() == response.json()
            assert second.post("/v1/requests/start", json={"request_id": first_request}).status_code == 403
            assert first.post("/v1/requests/start", json={"request_id": first_request}).json()["dispatch_permitted"] is True
            assert first.post("/v1/requests/start", json={"request_id": first_request}).json()["dispatch_permitted"] is False
            assert first.post("/v1/requests/reserve", json={"lease_id": second_lease.json()["lease_id"], "request_id": str(uuid.uuid4())}).status_code == 403
            assert second.post("/v1/requests/reserve", json={"lease_id": second_lease.json()["lease_id"], "request_id": str(uuid.uuid4())}).status_code == 409
            revoked = human.post(base, json=envelope("revoke_host", activation.json()["revision"], {"host_id": pending[0]["host_id"]}))
            assert revoked.status_code == 200
            assert first.post("/v1/leases", json=lease_command).status_code == 403
            assert first.post("/v1/requests/start", json={"request_id": first_request}).status_code == 403
            uncertain = first.post("/v1/requests/settle", json={"request_id": first_request, "outcome": "uncertain"})
            assert uncertain.status_code == 200 and uncertain.json()["held_units"] == 1
            assert first.post("/v1/requests/settle", json={"request_id": first_request, "outcome": "never_started"}).status_code == 409
            assert second.post("/v1/requests/settle", json={"request_id": second_request, "outcome": "never_started"}).status_code == 200
            assert second.post("/v1/requests/reserve", json={"lease_id": second_lease.json()["lease_id"], "request_id": str(uuid.uuid4())}).status_code == 200
        projection = hosts.inspect(owner, Query(tenant, project, "metadata"))
        assert projection["usage"] == {"consumed_units": 0, "held_units": 2}
        assert all("challenge" not in row and "certificate_sha256" not in row for row in projection["hosts"])
