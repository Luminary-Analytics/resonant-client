"""Real signed HTTP requests reach encrypted PostgreSQL content transactions."""
# ruff: noqa: F811 -- shared fixture intentionally imported.

import base64
import json

import pytest

from sonn_governance.app import create_app
from sonn_governance.content import ContentKeys, ContentStore
from test_identity import actual_http, identity_fixture  # noqa: F401
from test_store import Tenant, uid


@pytest.fixture
def content_http(database_config, identity_fixture):
    identity = identity_fixture
    tenant = Tenant(database_config)
    actor = identity.identity.authenticate("Bearer " + identity.token())
    tenant.member(actor, projects={tenant.project: ["content_write", "content_read", "metadata_read", "retention_admin"]})
    tenant.store.command(tenant.admin, tenant.command(content_mode="explicit"))
    content = ContentStore(tenant.store, ContentKeys({"fixture": b"k" * 32}, "fixture"))
    with actual_http(create_app(identity.identity, tenant.store, content=content)) as client:
        client.headers["Authorization"] = "Bearer " + identity.token()
        yield tenant, actor, content, client


def upload(data=b"private fixture evidence"):
    return {"command_id": uid(), "data_base64": base64.b64encode(data).decode("ascii"),
            "media_type": "text/plain", "retention_seconds": 3600}


def path(tenant):
    return f"/v1/tenants/{tenant.id}/projects/{tenant.project}/content/{uid()}"


def test_upload_pages_metadata_retention_and_replay(content_http):
    tenant, actor, content, client = content_http
    target, body = path(tenant), upload(("Ω🙂 private evidence\n" * 80).encode())
    first = client.post(target, json=body)
    assert first.status_code == 200
    assert client.post(target, json=body).json() == first.json()
    assert client.get(target).json() == first.json()
    assert "private" not in first.text and "sha256" not in first.text
    offset, data = 0, bytearray()
    while True:
        page = client.get(target + "/bytes", params={"offset": offset, "limit": 113})
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        value = page.json()
        assert "data" not in value and "url" not in value
        data.extend(base64.b64decode(value["data_base64"]))
        if value["next_offset"] is None:
            break
        offset = value["next_offset"]
    assert bytes(data) == base64.b64decode(body["data_base64"])
    hold = {"command_id": uid(), "expected_revision": 1, "operation": "hold_content", "reason": "owner_request"}
    assert client.post(target + "/retention", json=hold).json()["held"] is True
    delete = {"command_id": uid(), "expected_revision": 2, "operation": "delete_content"}
    assert client.post(target + "/retention", json=delete).status_code == 409
    release = {"command_id": uid(), "expected_revision": 2, "operation": "release_content_hold"}
    assert client.post(target + "/retention", json=release).json()["revision"] == 3
    assert client.post(target + "/retention", json={**delete, "expected_revision": 3}).status_code == 200
    assert client.get(target + "/bytes").status_code == 403
    assert client.post(target, json=body).status_code == 403


def test_current_policy_foreign_scope_and_revocation_close_all_access(content_http):
    tenant, actor, content, client = content_http
    target, body = path(tenant), upload()
    assert client.post(target, json=body).status_code == 200
    for foreign in (target.replace(tenant.project, tenant.other_project), target.replace(tenant.id, uid())):
        assert client.get(foreign).status_code == 403
        assert client.get(foreign + "/bytes").status_code == 403
        assert client.post(foreign, json=body).status_code == 403
    tenant.store.command(tenant.admin, tenant.command(expected=1, content_mode="none"))
    assert client.get(target + "/bytes").status_code == 403
    assert client.post(target, json=body).status_code == 403
    assert client.get(target).status_code == 200
    tenant.member(actor, active=False)
    assert client.get(target).status_code == 403
    assert client.post(target + "/retention", json={"command_id": uid(), "expected_revision": 1,
        "operation": "hold_content", "reason": "owner_request"}).status_code == 403


def test_content_key_unavailable_never_discloses_key_or_plaintext(content_http, caplog):
    tenant, actor, content, client = content_http
    target, body = path(tenant), upload()
    assert client.post(target, json=body).status_code == 200
    content.keys = ContentKeys({"replacement": b"s" * 32}, "replacement")
    response = client.get(target + "/bytes")
    assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
    assert "private fixture" not in caplog.text + response.text
    assert client.headers["Authorization"] not in caplog.text


@pytest.mark.parametrize("fault", ["too_big", "bad_base64", "extra", "duplicate", "compressed", "credential"])
def test_invalid_uploads_do_not_publish(content_http, fault):
    tenant, actor, content, client = content_http
    target, body = path(tenant), upload()
    if fault == "too_big":
        body = upload(b"x" * 1048577)
    elif fault == "bad_base64":
        body["data_base64"] = "YQ==\n"
    elif fault == "extra":
        body["tenant_id"] = tenant.id
    elif fault == "credential":
        body = upload(b"Authorization: Bearer synthetic-private-resource-token")
    if fault == "duplicate":
        response = client.post(target, content=json.dumps(body)[:-1] + ',"command_id":"duplicate"}',
                               headers={"Content-Type": "application/json"})
    else:
        response = client.post(target, json=body, headers={"Content-Encoding": "gzip"} if fault == "compressed" else {})
    assert response.status_code == 400
    assert client.get(target).status_code == 403


def test_exact_upload_limit_and_page_parameter_bounds(content_http):
    tenant, actor, content, client = content_http
    target = path(tenant)
    assert client.post(target, json=upload(b"x" * 1048576)).status_code == 200
    for query in ("limit=65537", "limit=-1", "offset=-1", "limit=1&limit=2", "url=https://outside.invalid"):
        assert client.get(target + "/bytes?" + query).status_code == 400
    page = client.get(target + "/bytes?limit=65536").json()
    assert len(base64.b64decode(page["data_base64"])) == 65536
