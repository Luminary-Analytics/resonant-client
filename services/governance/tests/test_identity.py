"""Signed tokens and actual loopback JWKS HTTP exercise the resource boundary."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
import time
from types import SimpleNamespace
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest
import uvicorn

from sonn_governance.app import create_app
from sonn_governance.identity import AuthenticationFailed, Identity, IdentityConfig
from sonn_governance.models import AccessDenied, Conflict


@pytest.fixture
def identity_fixture():
    keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]
    def jwk(index, kid=None):
        return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(keys[index].public_key())),
                "kid": kid or f"key-{index}", "alg": "RS256", "use": "sig"}
    state = SimpleNamespace(body={"keys": [jwk(0)]}, paths=[], status=200, location=None, headers=[])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.paths.append(self.path)
            state.headers.append(dict(self.headers))
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            if state.location:
                self.send_header("Location", state.location)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(state.body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    config = IdentityConfig(issuer=url, audience="governance-resource", jwks_url=url + "/keys",
                            profile="test", allow_test_loopback=True)
    identity = Identity(config)
    def token(*, index=0, changes=None, headers=None, algorithm="RS256"):
        now = int(time.time())
        claims = {"iss": url, "aud": config.audience, "sub": "fixture-member", "iat": now, "nbf": now, "exp": now + 300}
        claims.update(changes or {})
        actual = {"kid": f"key-{index}", "typ": "at+jwt", **(headers or {})}
        return jwt.encode(claims, keys[index] if algorithm == "RS256" else "synthetic-test-secret", algorithm=algorithm, headers=actual)
    try:
        yield SimpleNamespace(identity=identity, token=token, state=state, jwk=jwk, keys=keys, url=url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextmanager
def actual_http(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, proxy_headers=False,
                                         lifespan="off", timeout_graceful_shutdown=2, ws="none"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def test_valid_token_has_only_verified_identity_and_uses_pinned_cached_keys(identity_fixture):
    fixture = identity_fixture
    token = fixture.token(changes={"tenant_id": "forged", "roles": ["administrator"], "email": "owner@example.invalid"})
    principal = fixture.identity.authenticate("Bearer " + token)
    assert principal.issuer == fixture.url and principal.subject == "fixture-member" and principal.kind == "human"
    assert not hasattr(principal, "roles") and not hasattr(principal, "tenant_id")
    assert fixture.identity.authenticate("Bearer " + token) == principal
    assert fixture.state.paths == ["/keys"]
    assert all("Authorization" not in value for value in fixture.state.headers)


@pytest.mark.parametrize("changes,headers,algorithm", [
    ({"iss": "https://foreign.example"}, {}, "RS256"),
    ({"aud": "different-resource"}, {}, "RS256"),
    ({"aud": ["governance-resource", "another"]}, {}, "RS256"),
    ({"exp": 1}, {}, "RS256"),
    ({"nbf": int(time.time()) + 3600}, {}, "RS256"),
    ({"iat": int(time.time()) + 3600}, {}, "RS256"),
    ({"sub": ""}, {}, "RS256"),
    ({"sub": 123}, {}, "RS256"),
    ({"exp": True}, {}, "RS256"),
    ({"nbf": "1"}, {}, "RS256"),
    ({"exp": int(time.time()) + 7200}, {}, "RS256"),
    ({}, {"typ": "JWT"}, "RS256"),
    ({}, {"jku": "http://127.0.0.1/attacker"}, "RS256"),
    ({}, {"jwk": {"kty": "RSA"}}, "RS256"),
    ({}, {"x5u": "https://attacker.invalid/key"}, "RS256"),
    ({}, {"crit": ["custom"]}, "RS256"),
    ({}, {}, "HS256"),
])
def test_invalid_claims_algorithms_and_token_key_sources_fail_closed(identity_fixture, changes, headers, algorithm):
    fixture = identity_fixture
    with pytest.raises(AuthenticationFailed, match="^Resource authentication failed$"):
        fixture.identity.authenticate("Bearer " + fixture.token(changes=changes, headers=headers, algorithm=algorithm))
    assert all(path == "/keys" for path in fixture.state.paths)


def test_wrong_signature_and_unknown_key_refresh_are_globally_bounded(identity_fixture):
    fixture = identity_fixture
    fixture.identity.authenticate("Bearer " + fixture.token())
    invalid = fixture.token(index=1, headers={"kid": "key-0"})
    for _ in range(10):
        with pytest.raises(AuthenticationFailed):
            fixture.identity.authenticate("Bearer " + invalid)
        with pytest.raises(AuthenticationFailed):
            fixture.identity.authenticate("Bearer " + fixture.token(index=1))
    assert fixture.state.paths == ["/keys"]
    fixture.identity._last_refresh -= 31
    with pytest.raises(AuthenticationFailed):
        fixture.identity.authenticate("Bearer " + invalid)
    assert fixture.state.paths == ["/keys", "/keys"]


@pytest.mark.parametrize("same_kid", [False, True])
def test_rotation_refreshes_only_configured_source_and_expired_keys_do_not_survive(identity_fixture, same_kid):
    fixture = identity_fixture
    fixture.identity.authenticate("Bearer " + fixture.token())
    kid = "key-0" if same_kid else "key-1"
    fixture.state.body = {"keys": [fixture.jwk(1, kid)]}
    fixture.identity._last_refresh -= 31
    assert fixture.identity.authenticate("Bearer " + fixture.token(index=1, headers={"kid": kid})).subject == "fixture-member"
    with pytest.raises(AuthenticationFailed):
        fixture.identity.authenticate("Bearer " + fixture.token())
    fixture.identity._expires_at = 0
    fixture.identity._last_refresh -= 31
    fixture.state.status = 503
    with pytest.raises(AuthenticationFailed):
        fixture.identity.authenticate("Bearer " + fixture.token(index=1, headers={"kid": kid}))


@pytest.mark.parametrize("fault", ["redirect", "oversized", "duplicate", "private"])
def test_key_response_constraints_reject_redirects_unbounded_or_private_keys(identity_fixture, fault):
    fixture = identity_fixture
    if fault == "redirect":
        fixture.state.status, fixture.state.location = 302, fixture.url + "/attacker"
    elif fault == "oversized":
        fixture.state.body = {"keys": [fixture.jwk(0)], "padding": "x" * 150000}
    elif fault == "duplicate":
        fixture.state.body = {"keys": [fixture.jwk(0), fixture.jwk(0)]}
    else:
        fixture.state.body = {"keys": [{**fixture.jwk(0), "d": "private"}]}
    with pytest.raises(AuthenticationFailed):
        fixture.identity.authenticate("Bearer " + fixture.token())
    assert fixture.state.paths == ["/keys"]


@pytest.mark.parametrize("config", [
    {"issuer": "http://127.0.0.1", "jwks_url": "http://127.0.0.1/keys"},
    {"profile": "production", "allow_test_loopback": True},
    {"issuer": "https://login.test"},
    {"jwks_url": "https://user:secret@login.example/keys"},
    {"jwks_url": "https://login.example/keys?api_key=secret"},
    {"profile": "test", "jwks_url": "http://public.example/keys", "allow_test_loopback": True},
])
def test_test_transport_cannot_be_enabled_by_production_configuration(config):
    values = {"issuer": "https://login.example", "audience": "resource", "jwks_url": "https://login.example/keys", **config}
    with pytest.raises(ValueError):
        IdentityConfig(**values)


def test_http_authentication_revocation_replay_and_sanitized_errors(identity_fixture, caplog):
    fixture = identity_fixture
    tenant, project = str(uuid.uuid4()), str(uuid.uuid4())
    class Store:
        active = True
        decisions = {}
        def authorize(self, principal, value):
            if not self.active or (value.tenant_id, value.project_id) != (tenant, project):
                raise AccessDenied("synthetic-private-detail")
        def query(self, principal, value):
            self.authorize(principal, value)
            return {"project_id": project, "policy_revision": 0}
        def command(self, principal, value):
            self.authorize(principal, value)
            semantics = value.semantics()
            if value.command_id in self.decisions and self.decisions[value.command_id] != semantics:
                raise Conflict("synthetic-private-detail")
            self.decisions[value.command_id] = semantics
            return {"decision_id": value.command_id, "revision": 1}
    store = Store()
    token = fixture.token()
    base = f"/v1/tenants/{tenant}/projects/{project}"
    envelope = {"protocol_version": 1, "command_id": str(uuid.uuid4()), "expected_revision": 0,
                "operation": "set_policy", "payload": {"policy": {}}}
    with caplog.at_level(logging.WARNING, logger="sonn_governance"), actual_http(create_app(fixture.identity, store)) as client:
        assert client.get(base + "/metadata").status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        assert client.get(base + "/metadata").status_code == 200
        assert client.get(base.replace(project, str(uuid.uuid4())) + "/metadata").json() == {"error": "resource_unavailable"}
        first = client.post(base + "/commands", json=envelope)
        assert first.status_code == 200 and client.post(base + "/commands", json=envelope).json() == first.json()
        assert client.post(base + "/commands", json={**envelope, "expected_revision": 1}).status_code == 409
        store.active = False
        assert client.get(base + "/metadata").status_code == 403
        assert client.post(base + "/commands", json=envelope).status_code == 403
        store.query = lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic-secret " + token))
        failure = client.get(base + "/metadata")
        assert failure.status_code == 503 and failure.json() == {"error": "service_unavailable"}
        assert failure.headers["cache-control"] == "no-store"
    assert token not in caplog.text and "synthetic-secret" not in caplog.text and "synthetic-private-detail" not in caplog.text
