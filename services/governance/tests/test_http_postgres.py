"""Actual HTTP resource authorization against a restricted PostgreSQL login."""
# ruff: noqa: F811 -- shared pytest fixture is intentionally imported by name.

import json
import os
from pathlib import Path
import time
import uuid

import pytest

from sonn_governance.app import create_app
from sonn_governance.models import CommandEnvelope, Principal
from sonn_governance.store import GovernanceStore, bootstrap, migrate
from test_identity import actual_http, identity_fixture  # noqa: F401


@pytest.fixture
def postgres_config():
    source = os.environ.get("SONN_GOVERNANCE_TEST_CONFIG")
    if not source:
        pytest.skip("Set SONN_GOVERNANCE_TEST_CONFIG to a protected disposable PostgreSQL fixture config")
    return json.loads(Path(source).read_text("utf-8"))


def test_signed_resource_http_scope_current_revocation_and_replay(identity_fixture, postgres_config):
    fixture = identity_fixture
    config = postgres_config
    migrate(config["owner_dsn"], application_role=config["application_role"])
    principal = fixture.identity.authenticate("Bearer " + fixture.token())
    tenant, project, foreign_tenant, foreign_project = [str(uuid.uuid4()) for _ in range(4)]
    bootstrap(config["owner_dsn"], tenant_id=tenant, administrator=principal, project_ids=[project])
    foreign = Principal(fixture.url, "foreign-administrator", time.time() + 300)
    bootstrap(config["owner_dsn"], tenant_id=foreign_tenant, administrator=foreign, project_ids=[foreign_project])
    store = GovernanceStore(config["application_dsn"])
    def membership(active, revision):
        return store.command(principal, CommandEnvelope(1, str(uuid.uuid4()), tenant, None, revision, "set_membership",
            {"actor_id": principal.actor_id, "active": active,
             "tenant_permissions": ["membership_admin", "policy_admin"],
             "project_permissions": {project: ["metadata_read", "policy_admin"]}}))
    base = f"/v1/tenants/{tenant}/projects/{project}"
    policy = {"policy_version": 1, "allowed_models": [{"provider": "fixture", "model": "chosen"}],
              "allowed_tools": ["file_read"], "max_workers": 2, "request_limit": 10, "content_mode": "none"}
    envelope = {"protocol_version": 1, "command_id": str(uuid.uuid4()), "expected_revision": 0,
                "operation": "set_policy", "payload": {"policy": policy}}
    with actual_http(create_app(fixture.identity, store)) as client:
        client.headers["Authorization"] = "Bearer " + fixture.token(changes={"roles": ["admin"], "tenant_id": foreign_tenant})
        # Bootstrap policy administration never implicitly confers metadata.
        assert client.get(base + "/metadata").status_code == 403
        membership(True, 0)
        assert client.get(base + "/metadata").status_code == 200
        for target in (base.replace(project, foreign_project), base.replace(tenant, foreign_tenant),
                       base.replace(project, str(uuid.uuid4()))):
            response = client.get(target + "/metadata")
            assert response.status_code == 403 and response.json() == {"error": "resource_unavailable"}
        first = client.post(base + "/commands", json=envelope)
        assert first.status_code == 200 and first.json()["revision"] == 1
        assert client.post(base + "/commands", json=envelope).json() == first.json()
        assert client.post(base + "/commands", json={**envelope, "expected_revision": 1}).status_code == 409
        assert client.get(base + "/policy").status_code == 200
        assert client.post(base.replace(project, foreign_project) + "/commands", json=envelope).status_code == 403
        for token in (
            fixture.token(index=1, headers={"kid": "key-0"}),
            fixture.token(changes={"iss": "https://foreign.example"}),
            fixture.token(changes={"aud": "other-resource"}),
            fixture.token(changes={"exp": 1}),
            fixture.token(changes={"nbf": int(time.time()) + 3600}),
            fixture.token(headers={"typ": "JWT"}),
            fixture.token(headers={"jku": fixture.url + "/attacker"}),
            fixture.token(algorithm="HS256"),
        ):
            client.headers["Authorization"] = "Bearer " + token
            response = client.get(base + "/metadata")
            assert response.status_code == 401 and token not in response.text
        client.headers["Authorization"] = "Bearer " + fixture.token()
        membership(False, 1)
        assert client.get(base + "/metadata").status_code == 403
        assert client.get(base + "/policy").status_code == 403
        assert client.post(base + "/commands", json=envelope).status_code == 403
        assert all(path == "/keys" for path in fixture.state.paths)
