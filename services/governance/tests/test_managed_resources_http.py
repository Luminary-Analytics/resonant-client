"""Actual independently certified clients exercise the protocol-2 admission API."""
# ruff: noqa: F811 -- shared pytest fixtures are intentionally imported by name.

from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates, client_for  # noqa: F401
from test_store import Tenant, uid


def test_mtls_protocol2_binds_before_start_and_retains_revoked_cleanup(database_config, certificates):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin"])
    tenant.store.command(tenant.admin, tenant.command(max_workers=1))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    resources, monitor = ManagedResources(hosts), RunMonitoring(hosts)
    pending = []
    for certificate in (certificates.first, certificates.second):
        pending.append(hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
            {"host_id": uid(), "certificate_sha256": certificate.fingerprint})))
    with actual_host(hosts, certificates, monitoring=monitor, resources=resources) as (url, _):
        with client_for(url, certificates, certificates.first) as first, client_for(url, certificates, certificates.second) as second:
            leases, bindings = [], []
            for client, host in zip((first, second), pending):
                assert client.post("/v1/activate", json={"challenge": host["challenge"]}).status_code == 200
                command = {"command_id": uid(), "expected_policy_revision": 1}
                assert client.post("/v1/leases", json=command).status_code == 403
                response = client.post("/v1/leases", json={**command, "runner_protocol": 2})
                assert response.status_code == 200
                leases.append(response.json()["lease_id"])
                response = client.post("/v1/runs/register", json={"command_id": uid(), "lease_id": leases[-1], "local_run_id": uid(), "session_id": uid()})
                assert response.status_code == 200
                bindings.append(response.json()["binding_id"])
            slot = {"lease_id": leases[0], "binding_id": bindings[0], "worker_id": uid(), "kind": "worker"}
            admitted = first.post("/v1/resources/workers/reserve", json=slot)
            assert admitted.status_code == 200 and admitted.json()["dispatch_permitted"]
            assert not first.post("/v1/resources/workers/reserve", json=slot).json()["dispatch_permitted"]
            assert second.post("/v1/resources/workers/reserve", json={**slot, "lease_id": leases[1]}).status_code == 403
            assert second.post("/v1/resources/workers/reserve", json={**slot, "lease_id": leases[1], "binding_id": bindings[1], "worker_id": uid()}).status_code == 409
            request_id = uid()
            assert first.post("/v1/requests/reserve", json={"lease_id": leases[0], "request_id": request_id}).status_code == 200
            assert first.post("/v1/requests/start", json={"request_id": request_id}).status_code == 403
            bound = {"lease_id": leases[0], "worker_id": slot["worker_id"], "request_id": request_id, "purpose": "primary",
                     "model": {"provider": "ollama", "model": "fixture"}, "input_sha256": "a" * 64}
            assert first.post("/v1/resources/requests/bind", json=bound).status_code == 200
            assert first.post("/v1/requests/start", json={"request_id": request_id}).json()["dispatch_permitted"]
            assert first.post("/v1/requests/settle", json={"request_id": request_id, "outcome": "completed"}).status_code == 200
            action = {"lease_id": leases[0], "worker_id": slot["worker_id"], "request_id": request_id,
                      "action_id": uid(), "tool_name": "file_read", "arguments_sha256": "b" * 64}
            assert first.post("/v1/resources/tools/authorize", json=action).json()["dispatch_permitted"]
            assert not first.post("/v1/resources/tools/authorize", json=action).json()["dispatch_permitted"]
            assert second.post("/v1/resources/tools/observe", json={"action_id": action["action_id"], "outcome": "completed"}).status_code == 403
            hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 2, "revoke_host", {"host_id": pending[0]["host_id"]}))
            assert first.post("/v1/resources/tools/authorize", json=action).status_code == 403
            assert first.post("/v1/resources/tools/observe", json={"action_id": action["action_id"], "outcome": "completed"}).status_code == 200
            stopped = first.post("/v1/resources/workers/observe", json={"worker_id": slot["worker_id"], "outcome": "stopped"})
            assert stopped.status_code == 200 and not stopped.json()["slot_held"]
            assert second.post("/v1/resources/workers/reserve", json={**slot, "lease_id": leases[1], "binding_id": bindings[1], "worker_id": uid()}).status_code == 200
