"""Real SCIM HTTP uses tenant/issuer-bound current PostgreSQL credentials."""

import json
import time

import pytest
import psycopg

from sonn_governance.models import AccessDenied, CommandEnvelope, Principal, Query
from sonn_governance.scim import (
    GROUP_SCHEMA, IDENTITY_SCHEMA, PATCH_SCHEMA, USER_SCHEMA, ScimStore,
    register_provisioner, revoke_provisioner,
)
from sonn_governance.scim_app import ScimTransportConfig, create_scim_app
from test_identity import actual_http
from test_store import Tenant, uid


@pytest.fixture
def provisioning(database_config):
    tenant = Tenant(database_config)
    registration = register_provisioner(database_config["owner_dsn"], tenant_id=tenant.id,
        issuer="https://configured-idp.example", expires_at=time.time() + 300)
    scim = ScimStore(tenant.store)
    app = create_scim_app(scim, config=ScimTransportConfig("test", True))
    with actual_http(app) as client:
        client.headers.update({"Authorization": "Bearer " + registration["token"], "Content-Type": "application/scim+json"})
        yield tenant, registration, scim, client


def user(*, subject=None, name="Unicode Ω User"):
    return {"schemas": [USER_SCHEMA, IDENTITY_SCHEMA], "externalId": uid(), "userName": name,
            "active": True, IDENTITY_SCHEMA: {"subject": subject or uid()}}


def patch(path, value):
    return {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": path, "value": value}]}


def test_users_groups_paging_etags_and_deactivation(provisioning):
    tenant, registration, scim, client = provisioning
    document = user()
    response = client.post("/scim/v2/Users", json=document)
    assert response.status_code == 201, response.text
    value = response.json()
    route = response.headers["Location"]
    assert route == "/scim/v2/Users/" + value["id"]
    assert response.headers["etag"] == value["meta"]["version"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/scim+json")
    assert client.get(route).json() == value
    page = client.get("/scim/v2/Users", params={"filter": 'externalId eq "' + document["externalId"] + '"', "count": 1}).json()
    assert page["totalResults"] == 1 and page["Resources"] == [value]
    assert client.get("/scim/v2/Users?count=0").json()["Resources"] == []
    assert client.patch(route, json=patch("active", False)).status_code == 428
    changed = client.patch(route, json=patch("active", False), headers={"If-Match": response.headers["etag"]})
    assert changed.status_code == 200 and changed.json()["active"] is False
    assert client.patch(route, json=patch("active", True), headers={"If-Match": response.headers["etag"]}).status_code == 412
    actor = Principal("https://configured-idp.example", document[IDENTITY_SCHEMA]["subject"], time.time() + 60)
    with pytest.raises(AccessDenied):
        tenant.store.query(actor, Query(tenant.id, tenant.project, "metadata"))
    group = client.post("/scim/v2/Groups", json={"schemas": [GROUP_SCHEMA], "externalId": uid(), "displayName": "readers",
        "members": [{"value": value["id"], "type": "User"}]} )
    assert group.status_code == 201
    group_path = group.headers["Location"]
    assert client.get(group_path).json()["members"][0]["value"] == value["id"]
    assert client.delete(group_path, headers={"If-Match": group.headers["etag"]}).status_code == 204
    assert client.get(group_path).status_code == 404
    assert client.delete(route, headers={"If-Match": changed.headers["etag"]}).status_code == 204
    assert client.get(route).status_code == 404


def test_foreign_credential_and_current_revocation_never_return_other_tenant_resource(provisioning, database_config, caplog):
    tenant, registration, scim, client = provisioning
    created = client.post("/scim/v2/Users", json=user())
    assert created.status_code == 201
    target = created.headers["Location"]
    foreign = Tenant(database_config)
    other = register_provisioner(database_config["owner_dsn"], tenant_id=foreign.id,
        issuer="https://configured-idp.example", expires_at=time.time() + 300)
    response = client.get(target, headers={"Authorization": "Bearer " + other["token"]})
    assert response.status_code == 404
    forged = client.post("/scim/v2/Users", json={**user(), "tenant_id": foreign.id, "roles": ["admin"]})
    assert forged.status_code == 400
    revoke_provisioner(database_config["owner_dsn"], credential_id=registration["credential_id"])
    response = client.get(target)
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    assert client.post("/scim/v2/Users", json=user()).status_code == 401
    assert registration["token"] not in response.text + caplog.text
    assert other["token"] not in response.text + caplog.text


@pytest.mark.parametrize("fault", ["unknown", "duplicates", "compressed", "oversize", "filter", "duplicate_page", "subject_patch", "roles"])
def test_strict_documents_queries_and_unsupported_attributes_fail_closed(provisioning, fault):
    tenant, registration, scim, client = provisioning
    if fault == "filter":
        response = client.get("/scim/v2/Users", params={"filter": 'password pr or userName co "private"'})
    elif fault == "duplicate_page":
        response = client.get("/scim/v2/Users?count=1&count=2")
    elif fault == "unknown":
        response = client.post("/scim/v2/Users?tenant_id=" + tenant.id, json=user())
    elif fault == "duplicates":
        response = client.post("/scim/v2/Users", content=json.dumps(user())[:-1] + ',"active":false}')
    elif fault == "compressed":
        response = client.post("/scim/v2/Users", json=user(), headers={"Content-Encoding": "gzip"})
    elif fault == "oversize":
        response = client.post("/scim/v2/Users", content=b"x" * 262145)
    elif fault == "subject_patch":
        created = client.post("/scim/v2/Users", json=user())
        response = client.patch(created.headers["Location"], json=patch(IDENTITY_SCHEMA + ":subject", "different"),
            headers={"If-Match": created.headers["etag"]})
    else:
        response = client.post("/scim/v2/Users", json={**user(), "roles": ["membership_admin"]})
    assert response.status_code == (413 if fault == "oversize" else 400)
    assert response.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_adapter_never_logs_secret_exception_or_trusts_forwarded_transport(provisioning, monkeypatch, caplog):
    tenant, registration, scim, client = provisioning
    secret = "synthetic-private-provisioner-credential"
    def broken(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(scim, "list", broken)
    response = client.get("/scim/v2/Users")
    assert response.status_code == 503 and secret not in response.text + caplog.text
    with actual_http(create_scim_app(scim)) as production:
        response = production.get("/scim/v2/Users", headers={"Authorization": "Bearer " + registration["token"],
            "X-Forwarded-Proto": "https", "X-Tenant-Id": tenant.id})
        assert response.status_code == 401


def test_deprovision_and_group_removal_revoke_effective_scoped_access(provisioning):
    tenant, registration, scim, client = provisioning
    document = user()
    actor = Principal("https://configured-idp.example", document[IDENTITY_SCHEMA]["subject"], time.time() + 300)
    person = client.post("/scim/v2/Users", json=document)
    assert person.status_code == 201
    group = client.post("/scim/v2/Groups", json={"schemas": [GROUP_SCHEMA], "externalId": uid(),
        "displayName": "explicitly mapped readers", "members": [{"value": person.json()["id"]}]})
    assert group.status_code == 201
    with psycopg.connect(tenant.config["owner_dsn"]) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
        revision = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s",
                                      (tenant.id,)).fetchone()[0]
    scim.map_group(tenant.admin, CommandEnvelope(1, uid(), tenant.id, None, revision, "map_scim_group", {
        "group_id": group.json()["id"], "tenant_permissions": [],
        "project_permissions": {tenant.project: ["metadata_read"]}}))
    assert tenant.store.query(actor, Query(tenant.id, tenant.project, "metadata"))["project_id"] == tenant.project
    disabled = client.patch(person.headers["Location"], json=patch("active", False), headers={"If-Match": person.headers["etag"]})
    assert disabled.status_code == 200
    with pytest.raises(AccessDenied):
        tenant.store.query(actor, Query(tenant.id, tenant.project, "metadata"))
    enabled = client.patch(person.headers["Location"], json=patch("active", True), headers={"If-Match": disabled.headers["etag"]})
    assert enabled.status_code == 200
    assert tenant.store.query(actor, Query(tenant.id, tenant.project, "metadata"))["project_id"] == tenant.project
    removed = client.patch(group.headers["Location"], json=patch("members", []), headers={"If-Match": group.headers["etag"]})
    assert removed.status_code == 200
    with pytest.raises(AccessDenied):
        tenant.store.query(actor, Query(tenant.id, tenant.project, "metadata"))
