"""Actual human HTTP and independent mTLS clients share one durable run registry."""
# ruff: noqa: F811 -- shared pytest fixtures are intentionally imported by name.

from sonn_governance.app import create_app
from sonn_governance.hosts import HostGovernance
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from sonn_governance.store import GovernanceStore, bootstrap
from test_host_http import actual_host, certificates, client_for  # noqa: F401
from test_identity import actual_http, identity_fixture  # noqa: F401
from test_monitoring import projection
from test_store import policy, uid


def test_real_transports_record_metadata_and_exact_stop_delivery_without_content(database_config, identity_fixture, certificates):
    fixture = identity_fixture
    owner_token = fixture.token()
    owner = fixture.identity.authenticate("Bearer " + owner_token)
    reader_token = fixture.token(changes={"sub": "metadata-operator"})
    reader = fixture.identity.authenticate("Bearer " + reader_token)
    tenant, project, foreign_project = uid(), uid(), uid()
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=owner, project_ids=[project, foreign_project])
    core = GovernanceStore(database_config["application_dsn"])
    core.command(owner, CommandEnvelope(1, uid(), tenant, None, 0, "set_membership", {
        "actor_id": owner.actor_id, "active": True,
        "tenant_permissions": ["membership_admin", "policy_admin"],
        "project_permissions": {project: ["host_admin", "metadata_read", "control_execute"]},
    }))
    core.command(owner, CommandEnvelope(1, uid(), tenant, None, 1, "set_membership", {
        "actor_id": reader.actor_id, "active": True, "tenant_permissions": [],
        "project_permissions": {project: ["metadata_read"]},
    }))
    core.command(owner, CommandEnvelope(1, uid(), tenant, project, 0, "set_policy", {"policy": policy()}))
    hosts, monitor = HostGovernance(core), RunMonitoring(HostGovernance(core))
    base = f"/v1/tenants/{tenant}/projects/{project}"
    with actual_http(create_app(fixture.identity, core, hosts=hosts, monitoring=monitor)) as human, actual_host(hosts, certificates, monitoring=monitor) as (url, _):
        human.headers["Authorization"] = "Bearer " + owner_token
        pending = []
        for certificate in (certificates.first, certificates.second):
            response = human.post(base + "/hosts/commands", json={"protocol_version": 1, "command_id": uid(), "expected_revision": 0,
                "operation": "enroll_host", "payload": {"host_id": uid(), "certificate_sha256": certificate.fingerprint}})
            assert response.status_code == 200
            pending.append(response.json())
        with client_for(url, certificates, certificates.first) as first, client_for(url, certificates, certificates.second) as second:
            leases = []
            for client, host in zip((first, second), pending):
                assert client.post("/v1/activate", json={"challenge": host["challenge"]}).status_code == 200
                response = client.post("/v1/leases", json={"command_id": uid(), "expected_policy_revision": 1})
                assert response.status_code == 200
                leases.append(response.json()["lease_id"])
            registration = {"command_id": uid(), "lease_id": leases[0], "local_run_id": uid(), "session_id": uid()}
            response = first.post("/v1/runs/register", json=registration)
            assert response.status_code == 200
            binding = response.json()["binding_id"]
            assert first.post("/v1/runs/register", json=registration).json()["binding_id"] == binding
            event = {"command_id": uid(), "binding_id": binding, "sequence": 1, "projection": projection()}
            assert first.post("/v1/runs/ingest", json={**event, "projection": {**projection(), "prompt": "synthetic-private-prompt"}}).status_code == 400
            assert first.post("/v1/runs/ingest", json=event).status_code == 200
            assert second.post("/v1/runs/ingest", json=event).status_code == 403
            human.headers["Authorization"] = "Bearer " + reader_token
            response = human.get(base + "/runs")
            assert response.status_code == 200
            row = response.json()["runs"][0]
            assert row["projection"] == projection() and row["evidence_source"] == "host_report"
            assert "synthetic-private-prompt" not in response.text and registration["session_id"] not in response.text
            assert human.get(base.replace(project, foreign_project) + "/runs").status_code == 403
            assert human.get(base + "/runs?limit=1&limit=2").status_code == 400
            path = base + f"/runs/{binding}/controls"
            command = {"command_id": uid(), "expected_revision": row["revision"], "expected_epoch": 1, "expected_local_revision": 5, "operation": "stop"}
            assert human.post(path, json=command).status_code == 403
            human.headers["Authorization"] = "Bearer " + owner_token
            response = human.post(path, json=command)
            assert response.status_code == 200
            control = response.json()
            assert control["outcome"] is None and control["received_at"] is None
            assert human.post(path, json={**command, "owner_actor": owner.actor_id}).status_code == 400
            assert second.post("/v1/runs/controls/poll", json={"binding_id": binding, "lease_id": leases[1]}).status_code == 403
            polled = first.post("/v1/runs/controls/poll", json={"binding_id": binding, "lease_id": leases[0]})
            assert polled.json()["controls"] == [control]
            ack = {"binding_id": binding, "control_id": control["control_id"], "command_id": uid(), "outcome": "received", "processes_stopped": False}
            assert first.post("/v1/runs/controls/observe", json=ack).json()["dispatch_permitted"]
            assert not first.post("/v1/runs/controls/observe", json=ack).json()["dispatch_permitted"]
            final = first.post("/v1/runs/controls/observe", json={**ack, "command_id": uid(), "outcome": "applied", "processes_stopped": True})
            assert final.status_code == 200 and final.json()["reported_processes_stopped"]
            row = human.get(base + "/runs").json()["runs"][0]
            assert row["controls"][0]["outcome"] == "applied"
            # Report ingestion never fabricates an independently completed run.
            assert row["projection"]["state"] == "running"
