"""Signed real HTTP exercises retention without a content-decryption capability."""
# ruff: noqa: F811 -- shared identity fixture intentionally imported.

from dataclasses import asdict, replace
import json
import time

import pytest

from sonn_governance.admin_client import AdminClient, AdminClientError
from sonn_governance.admin_main import main
from sonn_governance.app import create_app
from sonn_governance.models import Principal
from sonn_governance.oidc import NativeOIDC
from sonn_governance.sharing_retention import SharingRetention
from test_identity import actual_http, identity_fixture  # noqa: F401
from test_managed_collaboration import Fixture
from test_oidc import issuer  # noqa: F401
from test_store import uid


@pytest.fixture
def retention_http(database_config, identity_fixture):
    f = Fixture(database_config)
    identity = identity_fixture
    actor = identity.identity.authenticate("Bearer " + identity.token())
    f.tenant.member(actor, projects={f.tenant.project: ["retention_admin"]})
    grant = f.grant()
    message = f.send(grant)
    with actual_http(create_app(identity.identity, f.tenant.store, sharing_retention=SharingRetention(f.tenant.store))) as client:
        client.headers["Authorization"] = "Bearer " + identity.token()
        yield f, actor, client, {"terms": grant, "message": message}


def path(f, kind, resource_id):
    return f"/v1/tenants/{f.tenant.id}/sharing/content/{kind}/{resource_id}"


@pytest.mark.parametrize("kind", ["terms", "message"])
def test_http_hold_delete_and_operator_cli_route_keep_content_private(retention_http, kind, caplog):
    f, actor, client, ids = retention_http
    target = path(f, kind, ids[kind])
    inspected = client.get(target)
    assert inspected.status_code == 200 and inspected.json()["revision"] == 1
    assert inspected.headers["cache-control"] == "no-store"
    hold = {"command_id": uid(), "expected_revision": 1, "operation": "hold_sharing_content", "reason": "security_review"}
    held = client.post(target + "/retention", json=hold)
    assert held.status_code == 200 and held.json()["held"]
    assert client.post(target + "/retention", json=hold).json() == held.json()
    assert client.post(target + "/retention", json={"command_id": uid(), "expected_revision": 2,
        "operation": "delete_sharing_content"}).status_code == 409
    assert client.post(target + "/retention", json={"command_id": uid(), "expected_revision": 2,
        "operation": "release_sharing_content_hold"}).json()["revision"] == 3
    deleted = client.post(target + "/retention", json={"command_id": uid(), "expected_revision": 3,
        "operation": "delete_sharing_content"})
    assert deleted.status_code == 200 and deleted.json()["deleted_at"]
    assert client.get(target + "/bytes").status_code == 404
    for value in ("Selected private", "Explicit private", "ciphertext", "key_id", "sha256", client.headers["Authorization"]):
        assert value not in inspected.text + held.text + deleted.text + caplog.text
    assert AdminClient.validate({"method": "GET", "path": target})[:2] == ("GET", target)
    assert AdminClient.validate({"method": "POST", "path": target + "/retention", "body": hold})[:2] == ("POST", target + "/retention")
    with pytest.raises(AdminClientError):
        AdminClient.validate({"method": "GET", "path": target + "/bytes"})
    f.tenant.member(actor, active=False)
    assert client.get(target).status_code == 403
    assert client.post(target + "/retention", json=hold).status_code == 403


@pytest.mark.parametrize("fault", ["extra", "duplicate", "bool_revision", "private_reason", "compressed", "oversize"])
def test_strict_http_retention_shape_has_no_mutation(retention_http, fault):
    f, _, client, ids = retention_http
    target = path(f, "message", ids["message"])
    body = {"command_id": uid(), "expected_revision": 1, "operation": "hold_sharing_content", "reason": "owner_request"}
    if fault == "extra":
        body["project_id"] = uid()
    elif fault == "bool_revision":
        body["expected_revision"] = True
    elif fault == "private_reason":
        body["reason"] = "private incident details"
    elif fault == "oversize":
        body["reason"] = "x" * 33000
    if fault == "duplicate":
        response = client.post(target + "/retention", content=json.dumps(body)[:-1] + ',"operation":"delete_sharing_content"}',
                               headers={"Content-Type": "application/json"})
    else:
        response = client.post(target + "/retention", json=body, headers={"Content-Encoding": "gzip"} if fault == "compressed" else {})
    assert response.status_code == 400
    assert client.get(target).json()["revision"] == 1


def test_foreign_scope_unauthenticated_and_unsupported_kind_do_not_disclose(retention_http):
    f, _, client, ids = retention_http
    target = path(f, "terms", ids["terms"])
    assert client.get(target.replace(f.tenant.id, uid())).status_code == 403
    assert client.get(target.replace(ids["terms"], uid())).status_code == 403
    assert client.get(target.replace("/terms/", "/arbitrary/")).status_code == 400
    assert client.get(target, params={"include_content": "true"}).status_code == 400
    del client.headers["Authorization"]
    assert client.get(target).status_code == 401


def test_operator_retention_cli_real_oidc_and_http(database_config, issuer, tmp_path, monkeypatch, capsys):
    f = Fixture(database_config)
    actor = Principal(issuer.url, "fixture-user", time.time() + 300)
    f.tenant.member(actor, projects={f.tenant.project: ["retention_admin"]})
    grant = f.grant()
    config = replace(issuer.config, login_timeout_seconds=5)
    native = NativeOIDC(config)
    monkeypatch.setattr("sonn_governance.oidc.webbrowser.open", issuer.browser)
    with actual_http(create_app(native.identity, f.tenant.store, sharing_retention=SharingRetention(f.tenant.store))) as service:
        config_path, request_path = tmp_path / "operator.json", tmp_path / "request.json"
        config_path.write_text(json.dumps({"server_url": str(service.base_url).rstrip("/"), "oidc": asdict(config)}))
        request_path.write_text(json.dumps({"method": "POST", "path": path(f, "terms", grant) + "/retention",
            "body": {"command_id": uid(), "expected_revision": 1, "operation": "delete_sharing_content"}}))
        assert main(["--config-file", str(config_path), "--request-file", str(request_path)]) == 0
        output = capsys.readouterr()
        result = json.loads(output.out)
        assert result["deleted_at"] and result["revision"] == 2
        assert all(token not in output.out + output.err for token in issuer.tokens)
        assert len(issuer.exchanges) == 1
        assert {item.name for item in tmp_path.iterdir()} == {"operator.json", "request.json"}
