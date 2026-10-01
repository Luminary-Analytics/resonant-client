"""Actual HTTP exercises member changes and separate approval authority."""
# ruff: noqa: F811 -- shared pytest identity fixture.

from sonn_governance.app import create_app
from sonn_governance.hosts import HostGovernance
from sonn_governance.models import CommandEnvelope
from sonn_governance.store import GovernanceStore, bootstrap
from test_identity import actual_http, identity_fixture  # noqa: F401
from test_store import uid


def test_http_member_inspection_self_elevation_independent_approval_and_revocation(database_config, identity_fixture):
    fixture = identity_fixture
    tokens = [fixture.token(changes={"sub": name}) for name in ("administrator", "independent-approver", "ordinary-member")]
    people = [fixture.identity.authenticate("Bearer " + token) for token in tokens]
    tenant, project = uid(), uid()
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=people[0], project_ids=[project])
    store = GovernanceStore(database_config["application_dsn"])
    base = f"/v1/tenants/{tenant}"
    def command(operation, revision, payload):
        return {"protocol_version": 1, "command_id": uid(), "expected_revision": revision, "operation": operation, "payload": payload}
    def membership(actor, permissions):
        return {"actor_id": actor.actor_id, "active": True, "tenant_permissions": permissions, "project_permissions": {}}
    with actual_http(create_app(fixture.identity, store, hosts=HostGovernance(store))) as client:
        client.headers["Authorization"] = "Bearer " + tokens[0]
        assert client.get("/v1/identity").json()["actor_id"] == people[0].actor_id
        response = client.post(base + "/commands", json=command("set_membership", 0, membership(people[1], ["membership_admin", "membership_approve"])))
        assert response.status_code == 200 and response.json()["revision"] == 1
        response = client.post(base + "/commands", json=command("set_membership", 1, membership(people[2], ["metadata_read"])))
        assert response.status_code == 200
        view = client.get(base + "/members/" + people[0].actor_id).json()
        assert view["authorization_revision"] == 2 and "content_read" not in view["tenant_permissions"]["manual"]
        change = command("set_membership", 2, membership(people[0], ["membership_admin", "membership_approve", "policy_admin", "content_read"]))
        response = client.post(base + "/commands", json=change)
        assert response.status_code == 200 and response.json()["state"] == "pending"
        pending = response.json()
        grant_path = base + "/grants/" + pending["grant_request_id"]
        assert client.get(grant_path).json()["state"] == "pending"
        decision = command("decide_grant", 2, {"request_id": pending["grant_request_id"], "sha256": pending["sha256"], "approve": True})
        assert client.post(base + "/commands", json=decision).status_code == 403
        client.headers["Authorization"] = "Bearer " + tokens[1]
        response = client.post(base + "/commands", json=decision)
        assert response.status_code == 200 and response.json()["state"] == "approved"
        assert client.post(base + "/commands", json=decision).json() == response.json()
        # Being a member or possessing a valid token grants no administration.
        client.headers["Authorization"] = "Bearer " + tokens[2]
        for path in (grant_path, base + "/members/" + people[0].actor_id, base + "/audit"):
            assert client.get(path).status_code == 403
        assert client.post(base + "/commands", json=decision).status_code == 403
        client.headers["Authorization"] = "Bearer " + tokens[0]
        assert client.get(base + "/members/" + people[0].actor_id + "?limit=1&limit=2").status_code == 400
        assert client.get(base + "/members/" + people[0].actor_id + "?after=bad").status_code == 400
        assert client.get(base.replace(tenant, uid()) + "/members/" + people[0].actor_id).status_code == 403
        # Revoke the approver's current grant. A previous success is no authority.
        response = client.post(base + "/commands", json=command("set_membership", 3, membership(people[1], [])))
        assert response.status_code == 200
        client.headers["Authorization"] = "Bearer " + tokens[1]
        assert client.post(base + "/commands", json=decision).status_code == 403


def test_http_audit_and_host_metadata_use_their_own_scoped_permissions(database_config, identity_fixture):
    fixture = identity_fixture
    token = fixture.token()
    actor = fixture.identity.authenticate("Bearer " + token)
    tenant, project, other = uid(), uid(), uid()
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=actor, project_ids=[project, other])
    store = GovernanceStore(database_config["application_dsn"])
    store.command(actor, CommandEnvelope(1, uid(), tenant, None, 0, "set_membership", {
        "actor_id": actor.actor_id, "active": True, "tenant_permissions": ["membership_admin", "policy_admin"],
        "project_permissions": {project: ["metadata_read", "audit_read"]},
    }))
    base = f"/v1/tenants/{tenant}/projects/{project}"
    with actual_http(create_app(fixture.identity, store, hosts=HostGovernance(store))) as client:
        client.headers["Authorization"] = "Bearer " + token
        assert client.get(base + "/hosts").json()["hosts"] == []
        response = client.get(base + "/audit?limit=1")
        assert response.status_code == 200 and len(response.json()["events"]) == 1
        assert client.get(base.replace(project, other) + "/audit").status_code == 403
        assert client.get(f"/v1/tenants/{tenant}/audit").status_code == 403
        assert client.get(base + "/audit?limit=0").status_code == 400
        assert client.get(base + "/audit?after=1&after=2").status_code == 400
