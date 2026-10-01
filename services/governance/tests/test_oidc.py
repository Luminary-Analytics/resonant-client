"""A real local issuer, browser redirects and token exchange exercise native PKCE."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
import hashlib
import json
import secrets
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from sonn_governance.oidc import LoginFailed, NativeOIDC, NativeOIDCConfig
from sonn_governance.app import create_app
from test_identity import actual_http
from test_store import Tenant


@pytest.fixture
def issuer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = SimpleNamespace(codes={}, requests=[], exchanges=[], callback_responses=[], fault=None, tokens=[])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def output(self, status, data):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/keys":
                return self.output(200, {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                    "kid": "fixture", "alg": "RS256", "use": "sig"}]})
            if parsed.path != "/authorize":
                return self.output(404, {})
            query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
            state.requests.append(query)
            assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
            assert query["client_id"] == "registered-public-client" and "client_secret" not in query
            assert query["resource"] == "https://governance.example.com"
            code = secrets.token_urlsafe(24)
            state.codes[code] = query
            callback = {"state": query["state"], "iss": state.url, "code": code}
            if state.fault == "state":
                callback["state"] = "forged"
            elif state.fault == "response_issuer":
                callback["iss"] = "https://foreign.example"
            elif state.fault == "missing_issuer":
                del callback["iss"]
            elif state.fault == "error":
                callback.pop("code")
                callback.update(error="access_denied", error_description="synthetic-private-error")
            redirect = query["redirect_uri"] + "?" + urlencode(callback)
            if state.fault == "duplicate_state":
                redirect += "&state=forged"
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()

        def do_POST(self):
            assert self.path == "/token"
            query = {name: values[0] for name, values in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
            state.exchanges.append(query)
            expected = state.codes.pop(query["code"], None)
            digest = jwt.utils.base64url_encode(hashlib.sha256(query["code_verifier"].encode()).digest()).decode()
            if (expected is None or expected["code_challenge"] != digest or state.fault == "pkce"
                    or query["redirect_uri"] != expected["redirect_uri"]):
                return self.output(400, {"error": "invalid_grant"})
            if state.fault == "lost_reply":
                self.connection.close()
                return
            if state.fault == "token_redirect":
                self.send_response(302)
                self.send_header("Location", state.url + "/attacker")
                self.end_headers()
                return
            if state.fault == "late_token":
                time.sleep(1.1)
            now = int(time.time())
            common = {"iss": state.url, "sub": "fixture-user", "iat": now, "exp": now + 300}
            resource = {**common, "aud": "governance-resource"}
            if state.fault == "access_audience":
                resource["aud"] = "other-resource"
            if state.fault == "access_type":
                access_type = "JWT"
            else:
                access_type = "at+jwt"
            access = jwt.encode(resource, key, algorithm="RS256", headers={"kid": "fixture", "typ": access_type})
            identity = {**common, "aud": query["client_id"], "nonce": expected["nonce"],
                        "at_hash": jwt.utils.base64url_encode(hashlib.sha256(access.encode()).digest()[:16]).decode()}
            if state.fault == "nonce":
                identity["nonce"] = "forged"
            if state.fault == "id_audience":
                identity["aud"] = "other-client"
            if state.fault == "id_issuer":
                identity["iss"] = "https://foreign.example"
            if state.fault == "subject":
                identity["sub"] = "other-user"
            if state.fault == "expired":
                identity["exp"] = 1
            if state.fault == "azp":
                identity["azp"] = "foreign-client"
            if state.fault == "at_hash":
                identity["at_hash"] = "forged"
            header = {"kid": "fixture", "typ": "JWT"}
            if state.fault == "key_source":
                header["jku"] = state.url + "/attacker"
            id_token = jwt.encode(identity, wrong if state.fault == "signature" else key,
                                  algorithm="RS256", headers=header)
            state.tokens.extend([access, id_token])
            self.output(200, {"access_token": access, "id_token": id_token, "token_type": "Bearer", "expires_in": 300})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.config = NativeOIDCConfig(issuer=state.url, client_id="registered-public-client", audience="governance-resource",
        authorization_endpoint=state.url + "/authorize", token_endpoint=state.url + "/token", jwks_url=state.url + "/keys",
        resource="https://governance.example.com",
        profile="test", allow_test_loopback=True, login_timeout_seconds=1)
    def browser(url):
        with httpx.Client(follow_redirects=True, trust_env=False, timeout=3) as client:
            response = client.get(url)
            state.callback_responses.append(response)
        return True
    state.browser = browser
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_native_code_exchange_validates_identity_resource_and_pkce_without_secret_persistence(issuer, caplog):
    client = NativeOIDC(issuer.config)
    result = client.login(browser=issuer.browser)
    assert result.principal.subject == "fixture-user"
    assert client.identity.authenticate("Bearer " + result.access_token) == result.principal
    assert result.access_token not in repr(result) + caplog.text
    assert len(issuer.exchanges) == 1 and "client_secret" not in issuer.exchanges[0]
    query = issuer.requests[0]
    assert len(query["state"]) >= 43 and len(query["nonce"]) >= 43
    assert len(issuer.exchanges[0]["code_verifier"]) == 43
    callback = issuer.callback_responses[0]
    assert callback.status_code == 200 and callback.headers["cache-control"] == "no-store"
    assert query["state"] not in callback.text and issuer.tokens[0] not in callback.text
    # The temporary callback listener is gone after completion.
    with httpx.Client(trust_env=False, timeout=.5) as http:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            http.get(query["redirect_uri"])


@pytest.mark.parametrize("fault", ["state", "response_issuer", "missing_issuer", "duplicate_state", "error",
    "pkce", "lost_reply", "token_redirect", "access_audience", "access_type", "nonce", "id_audience",
    "id_issuer", "subject", "expired", "azp", "at_hash", "key_source", "signature", "late_token"])
def test_forged_callback_or_invalid_exchange_never_returns_credential(issuer, caplog, fault):
    issuer.fault = fault
    with pytest.raises(LoginFailed, match="^Native authorization did not complete$"):
        NativeOIDC(issuer.config).login(browser=issuer.browser)
    assert len(issuer.exchanges) <= 1
    assert all(token not in caplog.text for token in issuer.tokens)
    assert "synthetic-private-error" not in caplog.text
    if fault in {"state", "response_issuer", "missing_issuer", "duplicate_state", "error"}:
        assert issuer.exchanges == []


def test_invalid_then_valid_callback_and_replay_are_single_use(issuer):
    def browser(url):
        with httpx.Client(trust_env=False) as http:
            redirect = http.get(url).headers["Location"]
            forged = redirect.replace("state=", "state=forged")
            assert http.get(forged).status_code == 400
            assert http.get(redirect, headers={"Host": "attacker.invalid"}).status_code == 400
            assert http.get(redirect).status_code == 200
            assert http.get(redirect).status_code == 409
        return True
    # This exercise deliberately makes four separate Windows HTTP connections
    # before redemption; its purpose is replay protection, not the one-second
    # timeout used by the invalid-callback and delayed-token fixtures.
    NativeOIDC(replace(issuer.config, login_timeout_seconds=5)).login(browser=browser)
    assert len(issuer.exchanges) == 1


def test_cancel_and_browser_failure_close_callback_without_redemption(issuer):
    event = threading.Event()
    def cancel(url):
        event.set()
        return True
    with pytest.raises(LoginFailed):
        NativeOIDC(issuer.config).login(browser=cancel, cancel_event=event)
    with pytest.raises(LoginFailed):
        NativeOIDC(issuer.config).login(browser=lambda _: False)
    assert not issuer.exchanges


def test_production_endpoints_cannot_inherit_fixture_transport():
    common = dict(issuer="https://idp.example.com", client_id="public", audience="resource",
        authorization_endpoint="https://idp.example.com/authorize", token_endpoint="https://idp.example.com/token",
        jwks_url="https://idp.example.com/keys")
    assert NativeOIDCConfig(**common).profile == "production"
    for changes in ({"allow_test_loopback": True}, {"token_endpoint": "http://127.0.0.1/token"},
                    {"authorization_endpoint": "https://secret@idp.example.com/authorize"},
                    {"callback_path": "//attacker.invalid"}, {"scopes": ("openid", "offline_access")}):
        with pytest.raises(ValueError):
            NativeOIDCConfig(**{**common, **changes})


def test_native_login_resource_token_reaches_current_postgres_permissions(issuer, database_config):
    native = NativeOIDC(replace(issuer.config, login_timeout_seconds=5))
    result = native.login(browser=issuer.browser)
    tenant = Tenant(database_config)
    tenant.member(result.principal, projects={tenant.project: ["metadata_read"]})
    target = f"/v1/tenants/{tenant.id}/projects/{tenant.project}/metadata"
    with actual_http(create_app(native.identity, tenant.store)) as resource:
        resource.headers["Authorization"] = "Bearer " + result.access_token
        assert resource.get(target).status_code == 200
        tenant.member(result.principal, active=False)
        assert resource.get(target).status_code == 403
