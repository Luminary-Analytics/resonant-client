"""Lumi Cloud from the desktop app (lumi/cloud.py), against a fake Lumi Cloud.

The fake implements the contract Lumi Cloud serves: OAuth for native apps,
/api/v1/me, device enrollment, EdDSA device assertions, check-ins and signed
policy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from lumi import cloud, policy, usage
from lumi.gui.settings import SettingsManager
from lumi.paths import state_home

URL = "https://cloud.example.test"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class FakeCloud:
    """Just enough of Lumi Cloud for the client, with switches for failure cases."""

    def __init__(self) -> None:
        self.org_key = Ed25519PrivateKey.generate()
        self.key_id = "acme-20260925-abc123"
        self.public = _b64(self.org_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                                   serialization.PublicFormat.Raw))
        self.challenge = ""
        self.redirect_uri = ""
        self.refresh_tokens: dict[str, bool] = {}  # token -> used
        self.access_tokens: set[str] = set()
        self.devices: dict[str, dict] = {}
        self.device_tokens: dict[str, str] = {}
        self.enrollment_token = "lce_managed"
        self.checkins: list[dict] = []
        self.policy_version: int | None = None
        self.policy_document: dict = {}
        self.has_seat = True
        self.counter = 0

    def publish(self, document: dict) -> None:
        self.policy_version = (self.policy_version or 0) + 1
        self.policy_document = document

    def _token(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def _tokens(self) -> httpx.Response:
        access, refresh = self._token("access"), self._token("refresh")
        self.access_tokens.add(access)
        self.refresh_tokens[refresh] = False
        return httpx.Response(200, json={"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                                         "expires_in": 3600})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        bearer = request.headers.get("authorization", "")[7:]
        if path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if form["code"] != "good-code" or challenge != self.challenge \
                        or form["redirect_uri"] != self.redirect_uri or form["client_id"] != "lumi-desktop":
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code."})
                return self._tokens()
            used = self.refresh_tokens.get(form.get("refresh_token", ""))
            if used is None or used:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Ended."})
            self.refresh_tokens[form["refresh_token"]] = True
            return self._tokens()
        if path == "/oauth/revoke":
            self.refresh_tokens.pop(parse_qs(request.content.decode()).get("token", [""])[0], None)
            return httpx.Response(200, json={})
        if path == "/api/v1/me":
            if bearer not in self.access_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"user": {"id": "usr_1", "email": "ada@example.com", "name": "Ada"},
                                             "organizations": [{"id": "org_acme", "name": "Acme", "role": "owner",
                                                                "has_seat": self.has_seat}]})
        if path == "/api/v1/devices":
            body = json.loads(request.content)
            if body.get("enrollment_token"):
                if body["enrollment_token"] != self.enrollment_token:
                    return httpx.Response(401, json={"error": "invalid_grant", "error_description": "Bad token."})
                owner = None
            elif bearer in self.access_tokens:
                if not self.has_seat:
                    return httpx.Response(403, json={"error": "no_seat", "error_description": "No seat."})
                owner = "usr_1"
            else:
                return httpx.Response(401, json={"error": "invalid_token"})
            device_id = self._token("dev")
            self.devices[device_id] = {"public_key": body["public_key"], "owner": owner, "revoked": False,
                                       "name": body["name"]}
            return httpx.Response(201, json={"device_id": device_id,
                                             "organization": {"id": "org_acme", "name": "Acme"},
                                             "trusted_keys": {self.key_id: self.public}})
        if path == "/api/v1/devices/token":
            assertion = json.loads(request.content)["assertion"]
            device_id = jwt.decode(assertion, options={"verify_signature": False})["iss"]
            device = self.devices.get(device_id)
            if device is None or device["revoked"]:
                return httpx.Response(401, json={"error": "device_revoked" if device else "invalid_client"})
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(device["public_key"]))
            jwt.decode(assertion, key=key, algorithms=["EdDSA"], audience=f"{URL}/api/v1/devices/token",
                       issuer=device_id)
            token = self._token("devtok")
            self.device_tokens[token] = device_id
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 3600})
        device_id = self.device_tokens.get(bearer)
        device = self.devices.get(device_id or "")
        if device is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        if device["revoked"]:
            return httpx.Response(401, json={"error": "device_revoked"})
        if path == "/api/v1/devices/checkin":
            self.checkins.append(json.loads(request.content))
            return httpx.Response(200, json={"organization": {"id": "org_acme", "name": "Acme"},
                                             "policy_version": self.policy_version, "next_checkin_seconds": 3600,
                                             "trusted_keys": {self.key_id: self.public}})
        if path == "/api/v1/devices/policy":
            if self.policy_version is None:
                return httpx.Response(404, json={"error": "no_policy"})
            now = datetime.now(timezone.utc)
            document = {**self.policy_document, "schema": "lumi.policy/v1", "organization": "Acme",
                        "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=14)).isoformat(),
                        "grace_days": 7, "policy_version": self.policy_version}
            signature = self.org_key.sign(policy.canonical(document))
            return httpx.Response(200, json={"policy": document, "signature": _b64(signature),
                                             "key_id": self.key_id})
        if path == "/api/v1/devices/unenroll":
            device["revoked"] = True
            return httpx.Response(200, json={"revoked": True})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "machine_policy_file", lambda: tmp_path / "no-machine-policy.json")
    monkeypatch.setattr(policy, "_registry_policy", lambda: None)
    monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
    monkeypatch.setattr(policy, "machine_keys", lambda: {})
    usage.set_for_tests(usage.UsageLedger(tmp_path / "usage"))
    policy.load(force=True)
    return FakeCloud()


def _client(fake: FakeCloud, opened: list[str] | None = None) -> cloud.CloudClient:
    settings = SettingsManager(path=state_home() / "settings.json")
    return cloud.CloudClient(settings, transport=httpx.MockTransport(fake),
                             open_browser=(opened.append if opened is not None else lambda url: None))


def _sign_in(client: cloud.CloudClient, fake: FakeCloud) -> None:
    opened: list[str] = []
    client._open_browser = opened.append
    client.begin_sign_in(URL)
    params = {k: v[0] for k, v in parse_qs(urlsplit(opened[0]).query).items()}
    fake.challenge, fake.redirect_uri = params["code_challenge"], params["redirect_uri"]
    # The browser comes back to Lumi's one-time listener on this computer.
    with urllib.request.urlopen(f"{params['redirect_uri']}?code=good-code&state={params['state']}") as page:
        assert b"signed in" in page.read()
    _wait(lambda: client.status()["signed_in"] and not client.status()["signing_in"])


def _wait(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


# ── Signing in ──────────────────────────────────────────────────────────────


def test_signing_in_through_the_browser(fake):
    client = _client(fake)
    _sign_in(client, fake)
    status = client.status()
    assert status["account"]["email"] == "ada@example.com"
    assert status["account"]["organizations"] == [{"id": "org_acme", "name": "Acme", "role": "owner",
                                                   "has_seat": True}]
    assert status["url"] == URL
    # The refresh token is a secret: it sits in api_keys, which Settings never shows.
    assert client.settings.get("api_keys", cloud.REFRESH_SECRET).startswith("refresh-")
    assert client.settings.get_masked()["api_keys"][cloud.REFRESH_SECRET] == ""


def test_the_listener_refuses_a_different_sign_in(fake):
    opened: list[str] = []
    client = _client(fake, opened)
    client.begin_sign_in(URL)
    redirect = parse_qs(urlsplit(opened[0]).query)["redirect_uri"][0]
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(f"{redirect}?code=good-code&state=someone-elses")
    assert refused.value.code == 400
    assert client.status()["signing_in"] and not client.status()["signed_in"]
    client.cancel_sign_in()
    _wait(lambda: not client.status()["signing_in"])


def test_cancelling_in_the_browser(fake):
    opened: list[str] = []
    client = _client(fake, opened)
    client.begin_sign_in(URL)
    params = {k: v[0] for k, v in parse_qs(urlsplit(opened[0]).query).items()}
    urllib.request.urlopen(f"{params['redirect_uri']}?error=access_denied&state={params['state']}").read()
    _wait(lambda: not client.status()["signing_in"])
    assert "cancelled" in client.status()["error"] and not client.status()["signed_in"]


@pytest.mark.parametrize(("address", "problem"), [
    ("http://cloud.example.test", "https"),
    ("", "Enter the Lumi Cloud address"),
    ("https://cloud.example.test/?x=1", "without"),
])
def test_addresses_must_be_https(fake, address, problem):
    with pytest.raises(cloud.CloudError, match=problem):
        _client(fake).begin_sign_in(address)
    assert cloud.normalize_url("http://127.0.0.1:8710/") == "http://127.0.0.1:8710"


def test_refresh_rotates_and_an_ended_sign_in_signs_out(fake):
    client = _client(fake)
    _sign_in(client, fake)
    first = client.settings.get("api_keys", cloud.REFRESH_SECRET)
    client._access = None
    client.refresh_account()
    second = client.settings.get("api_keys", cloud.REFRESH_SECRET)
    assert second != first and fake.refresh_tokens[first] is True
    fake.refresh_tokens[second] = True  # someone else used it
    client._access = None
    with pytest.raises(cloud.CloudError, match="Sign in again"):
        client.refresh_account()
    assert client.status()["signed_in"] is False and client.status()["account"] == {}


def test_signing_out(fake):
    client = _client(fake)
    _sign_in(client, fake)
    refresh = client.settings.get("api_keys", cloud.REFRESH_SECRET)
    client.sign_out()
    assert refresh not in fake.refresh_tokens
    assert not client.status()["signed_in"]


# ── Enrolling and check-ins ────────────────────────────────────────────────


def test_joining_an_organization_applies_its_signed_policy(fake):
    fake.publish({"models": {"allowed": ["anthropic:*"]}, "permissions": {"allowed_modes": ["ask", "plan"]}})
    client = _client(fake)
    _sign_in(client, fake)
    device = client.enroll("org_acme")
    assert device["organization_name"] == "Acme" and device["how"] == "joined"
    assert client.settings.get("api_keys", cloud.DEVICE_SECRET)  # the private key stays on this computer
    state = policy.load()
    assert state.cloud and state.policy.signed
    assert state.policy.source == "Lumi Cloud: Acme (joined in this app)"
    assert state.policy.model_allowed("anthropic", "claude-sonnet-4-5")
    assert not state.policy.model_allowed("openai", "gpt-5")
    assert client.status()["policy_version"] == 1
    assert fake.checkins[-1]["policy_version"] == 1  # confirmed at once, for the fleet page
    fake.publish({"models": {"allowed": ["openai:*"]}})
    client.check_in()
    assert policy.load().policy.model_allowed("openai", "gpt-5")
    assert client.status()["policy_version"] == 2


def test_check_ins_report_usage_totals_not_content(fake):
    client = _client(fake)
    _sign_in(client, fake)
    client.enroll("org_acme")
    ledger = usage.ledger()
    for _ in range(2):
        ledger.record(provider="anthropic", model="claude-sonnet-4-5", stats={"prompt_tokens": 1000,
                                                                              "completion_tokens": 200},
                      purpose="turn", session="s1", project="/secret/project")
    # Windows are half-open (since <= ts < until): a record from the same
    # millisecond as a check-in goes into the next one, never lost or doubled.
    time.sleep(0.01)
    client.check_in()
    report = fake.checkins[-1]["usage"]
    assert report["models"][0]["model"] == "anthropic:claude-sonnet-4-5"
    assert report["models"][0]["requests"] == 2 and report["models"][0]["input_tokens"] == 2000
    assert "secret" not in json.dumps(fake.checkins[-1])  # no projects, sessions or paths
    client.check_in()
    assert fake.checkins[-1]["usage"]["models"] == []  # nothing new since the last check-in


def test_no_seat_means_no_enrollment(fake):
    fake.has_seat = False
    client = _client(fake)
    _sign_in(client, fake)
    with pytest.raises(cloud.CloudError, match="No seat"):
        client.enroll("org_acme")
    assert client.device() == {}


def test_a_tampered_download_is_not_applied(fake):
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    client = _client(fake)
    _sign_in(client, fake)
    client.enroll("org_acme")
    path = policy.cloud_policy_path()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["policy"]["models"]["allowed"] = ["*:*"]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    state = policy.load(force=True)
    assert state.policy is None and "signature" in state.cloud_error


def test_a_revoked_computer_forgets_its_enrollment(fake):
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    client = _client(fake)
    _sign_in(client, fake)
    device = client.enroll("org_acme")
    fake.devices[device["id"]]["revoked"] = True
    client._device_access = None
    assert client.check_in() == {"revoked": True}
    assert client.device() == {} and not policy.cloud_policy_path().exists()
    assert policy.load().policy is None
    assert "removed this computer" in client.status()["error"]


def test_leaving_on_this_computer(fake):
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    client = _client(fake)
    _sign_in(client, fake)
    device = client.enroll("org_acme")
    client.unenroll()
    assert fake.devices[device["id"]]["revoked"] and client.device() == {}
    assert policy.load().policy is None


def test_a_machine_policy_enrolls_a_managed_computer(fake, tmp_path, monkeypatch):
    bootstrap = tmp_path / "machine-policy.json"
    bootstrap.write_text(json.dumps({
        "schema": "lumi.policy/v1", "organization": "Acme",
        "models": {"allowed": ["ollama:*"]},
        "cloud": {"url": URL, "organization_id": "org_acme", "enrollment_token": fake.enrollment_token},
        "trusted_keys": {fake.key_id: fake.public},
    }), encoding="utf-8")
    monkeypatch.setattr(policy, "machine_policy_file", lambda: bootstrap)
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    assert policy.load(force=True).policy.model_allowed("ollama", "qwen3")  # the bootstrap rules, until enrolled

    client = _client(fake)
    assert client.status()["url"] == URL and client.status()["url_locked"]
    assert client.background_step() == 3600.0
    device = client.device()
    assert device["how"] == "managed" and fake.devices[device["id"]]["owner"] is None
    state = policy.load()
    assert state.cloud and state.machine.organization == "Acme"
    assert state.policy.source.startswith("Lumi Cloud: Acme (set up by")
    assert state.policy.model_allowed("anthropic", "x") and not state.policy.model_allowed("ollama", "qwen3")
    with pytest.raises(cloud.CloudError, match="manages"):
        client.unenroll()
    with pytest.raises(cloud.CloudError, match="policy"):
        client.begin_sign_in("https://somewhere-else.example")


def test_a_managed_download_signed_by_an_untrusted_key_leaves_the_machine_policy(fake, tmp_path, monkeypatch):
    bootstrap = tmp_path / "machine-policy.json"
    bootstrap.write_text(json.dumps({
        "schema": "lumi.policy/v1", "organization": "Acme", "models": {"allowed": ["ollama:*"]},
        "cloud": {"url": URL, "organization_id": "org_acme", "enrollment_token": fake.enrollment_token},
        "trusted_keys": {"someone-else": fake.public[::-1]},
    }), encoding="utf-8")
    monkeypatch.setattr(policy, "machine_policy_file", lambda: bootstrap)
    policy.load(force=True)
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    client = _client(fake)
    assert client.background_step() == float(cloud.RETRY_SECONDS)  # enrolled, but the policy didn't verify
    state = policy.load(force=True)
    assert not state.cloud and state.policy.model_allowed("ollama", "qwen3")


# ── Settings > Lumi account (socket commands) ─────────────────────────────


def _run(client: cloud.CloudClient, command: str, **msg) -> list[dict]:
    import asyncio
    from types import SimpleNamespace

    from lumi.gui import ws_commands
    from tests.test_connections import _StubWS

    ctx = ws_commands.CommandContext(ws=_StubWS(), state=SimpleNamespace(cloud=client),
                                     msg={"command": command, **msg}, runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_the_settings_page_gets_status_and_errors_without_secrets(fake):
    client = _client(fake)
    [reply] = _run(client, "cloud_sign_in", url="http://cloud.example.test")
    assert reply["event"] == "cloud_status" and "https" in reply["data"]["error"]
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    _sign_in(client, fake)
    [reply] = _run(client, "cloud_enroll", organization_id="org_acme")
    status = reply["data"]
    assert status["device"]["organization_name"] == "Acme" and status["policy_version"] == 1
    assert status["error"] == "" and status["policy_source"] == "Lumi Cloud: Acme (joined in this app)"
    text = json.dumps(status)
    for secret in (client.settings.get("api_keys", cloud.REFRESH_SECRET),
                   client.settings.get("api_keys", cloud.DEVICE_SECRET)):
        assert secret and secret not in text
    assert "trusted_keys" not in status["device"]
    [reply] = _run(client, "cloud_unenroll")
    assert reply["data"]["device"] == {}
