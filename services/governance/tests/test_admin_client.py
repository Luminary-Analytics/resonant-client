"""Real native OIDC and resource HTTP; operator tokens never enter saved output."""
# ruff: noqa: F811 -- shared issuer fixture.

from dataclasses import asdict, replace
import json
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Route

from sonn_governance.admin_client import AdminClient, AdminClientError, AdminConfig, load_config
from sonn_governance.admin_main import main
from sonn_governance.app import create_app
from sonn_governance.models import CommandEnvelope, Principal
from sonn_governance.oidc import LoginFailed, NativeOIDC
from sonn_governance.store import GovernanceStore, bootstrap
from test_identity import actual_http
from test_oidc import issuer  # noqa: F401
from test_store import policy, uid


def test_operator_cli_uses_real_code_exchange_and_current_server_grants(database_config, issuer, tmp_path, monkeypatch, capsys):
    actor = Principal(issuer.url, "fixture-user", time.time() + 300)
    tenant, project = uid(), uid()
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=actor, project_ids=[project])
    store = GovernanceStore(database_config["application_dsn"])
    # This success/revocation test is not a one-second deadline test. The shared
    # issuer keeps its short timeout for the separate negative exchange cases.
    success_config = replace(issuer.config, login_timeout_seconds=5)
    native = NativeOIDC(success_config)
    config_path, request_path = tmp_path / "operator.json", tmp_path / "request.json"
    monkeypatch.setattr("sonn_governance.oidc.webbrowser.open", issuer.browser)
    with actual_http(create_app(native.identity, store)) as service:
        config_path.write_text(json.dumps({"server_url": str(service.base_url).rstrip("/"), "oidc": asdict(success_config)}))
        assert main(["--config-file", str(config_path), "--identity"]) == 0
        identity_output = capsys.readouterr()
        assert json.loads(identity_output.out)["actor_id"] == actor.actor_id
        request = {"method": "POST", "path": f"/v1/tenants/{tenant}/projects/{project}/commands", "body": {
            "protocol_version": 1, "command_id": uid(), "expected_revision": 0, "operation": "set_policy", "payload": {"policy": policy()}}}
        request_path.write_text(json.dumps(request))
        args = ["--config-file", str(config_path), "--request-file", str(request_path)]
        assert main(args) == 0
        success = capsys.readouterr()
        assert json.loads(success.out)["revision"] == 1
        store.command(actor, CommandEnvelope(1, uid(), tenant, None, 0, "set_membership", {
            "actor_id": actor.actor_id, "active": True, "tenant_permissions": ["membership_admin"], "project_permissions": {}}))
        assert main(args) == 2
        denied = capsys.readouterr()
        assert denied.err.strip() == json.dumps({"error": "request_denied", "status": 403}), denied.err
        combined = identity_output.out + identity_output.err + success.out + success.err + denied.out + denied.err
        assert all(token not in combined for token in issuer.tokens)
        assert len(issuer.exchanges) == 3
        assert set(item.name for item in tmp_path.iterdir()) == {"operator.json", "request.json"}


def test_success_fixture_allows_browser_delay_without_weakening_deadline_rejection(issuer):
    def delayed_browser(url):
        result = issuer.browser(url)
        time.sleep(1.1)
        return result

    with pytest.raises(LoginFailed):
        NativeOIDC(issuer.config).login(browser=delayed_browser)
    assert len(issuer.exchanges) == 0
    credential = NativeOIDC(replace(issuer.config, login_timeout_seconds=5)).login(browser=delayed_browser)
    assert credential.principal.subject == "fixture-user"
    assert len(issuer.exchanges) == 1


@pytest.mark.parametrize("document", [
    {"method": "GET", "path": "https://attacker.invalid/v1/identity"},
    {"method": "GET", "path": "//attacker.invalid/v1/identity"},
    {"method": "GET", "path": "/v1/identity?token=bad"},
    {"method": "GET", "path": "/v1/../identity"},
    {"method": "GET", "path": "/v1/identity", "body": {}},
    {"method": [], "path": "/v1/identity"},
    {"method": "GET", "path": "/v1/identity", "query": {"token": "synthetic-secret"}},
])
def test_invalid_or_origin_changing_request_is_refused_before_login(document):
    with pytest.raises(AdminClientError) as failure:
        AdminClient.validate(document)
    assert not failure.value.delivery_unknown


def test_authenticated_redirect_is_never_followed(issuer):
    credential = NativeOIDC(issuer.config).login(browser=issuer.browser)
    calls = []
    async def redirect(request):
        calls.append(request.url.path)
        return RedirectResponse("http://127.0.0.1:1/stolen")
    with actual_http(Starlette(routes=[Route("/v1/identity", redirect)])) as service:
        client = AdminClient(AdminConfig(str(service.base_url).rstrip("/"), issuer.config), credential)
        with pytest.raises(AdminClientError) as failure:
            client.request({"method": "GET", "path": "/v1/identity"})
        assert failure.value.status == 307 and calls == ["/v1/identity"]


def test_operator_configuration_rejects_duplicate_fields_and_unbounded_input(issuer, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"server_url":"first","server_url":"second","oidc":{}}')
    with pytest.raises(ValueError):
        load_config(path)
    path.write_bytes(b"x" * 65537)
    with pytest.raises(ValueError):
        load_config(path)
    with pytest.raises(ValueError):
        AdminConfig("https://user:synthetic-secret@example.test", issuer.config)
