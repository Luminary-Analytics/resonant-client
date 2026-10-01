"""Lumi Cloud from the desktop app (lumi/cloud.py), against a fake Lumi Cloud.

The fake implements the contract Lumi Cloud serves: OAuth for native apps,
/api/v1/me, device enrollment, EdDSA device assertions, check-ins, signed
policy and organization oversight's signed acknowledgments.
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
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from lumi import cloud, policy, usage
from lumi.gui.settings import SettingsManager
from lumi.paths import state_home

URL = "https://cloud.example.test"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


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
        self.subscription_required = False
        # Acme is the person's personal workspace: Lumi Cloud made it at their first sign-in.
        self.personal = False
        self.counter = 0
        # Every policy document published, and the oversight acknowledgments received.
        self.published: list[dict] = []
        self.acknowledgments: list[dict] = []
        # Answers given to acknowledgments before any is accepted (httpx.Response each).
        self.acknowledgment_failures: list[httpx.Response] = []

    def publish(self, document: dict) -> None:
        self.policy_version = (self.policy_version or 0) + 1
        self.policy_document = document
        self.published.append(document)

    def _acknowledgment(self, device_id: str, device: dict, body: dict, account_token: str = "") -> httpx.Response:
        """POST /api/v1/oversight/acknowledgments, as the contract with Lumi Cloud describes it."""
        if self.acknowledgment_failures:
            return self.acknowledgment_failures.pop(0)
        record, signature = body.get("record"), str(body.get("signature") or "")
        if not isinstance(record, dict):
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "Send a record."})
        # The device's registered key over the canonical JSON of the record (sorted keys, no spaces, UTF-8).
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(device["public_key"]))
        try:
            key.verify(_unb64url(signature.rstrip("=")), json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        except Exception:
            return httpx.Response(422, json={"error": "invalid_signature",
                                             "error_description": "The signature doesn't verify."})
        if record.get("device_id") != device_id or record.get("organization") != "org_acme":
            return httpx.Response(403, json={"error": "device_mismatch",
                                             "error_description": "Not this device's organization."})
        # A notice one of the organization's policies produced for this device:
        # The SHA-256 hex digest of {device, organization_id, oversight section as published}.
        produced = {hashlib.sha256(json.dumps(
            {"device": device_id, "organization_id": "org_acme", "oversight": document.get("oversight") or {}},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
            for document in self.published}
        if record.get("notice_fingerprint") not in produced:
            return httpx.Response(409, json={"error": "notice_mismatch",
                                             "error_description": "No policy of this organization shows that notice."})
        # Whom it counts for: the member whose own computer it is, or on a managed computer
        # (enrolled with a token) the member whose sign-in came with it; no account, the computer.
        account = (record.get("person") or {}).get("account")
        if not account:
            attribution = "none"
        elif record.get("surface") == "gateway":
            attribution = "unverified"
        elif device["owner"] is not None:
            attribution = "person" if device["owner"] == account else "unverified"
        else:
            attribution = "signed_in" if account == "usr_1" and account_token in self.access_tokens else "unverified"
        stored = {"id": self._token("ack"), "record": record, "signature": signature, "attribution": attribution}
        self.acknowledgments.append(stored)
        return httpx.Response(201, json={"id": stored["id"]})

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
                                                                "has_seat": self.has_seat,
                                                                "personal": self.personal}]})
        if path == "/api/v1/devices":
            body = json.loads(request.content)
            if body.get("enrollment_token"):
                if body["enrollment_token"] != self.enrollment_token:
                    return httpx.Response(401, json={"error": "invalid_grant", "error_description": "Bad token."})
                owner = None
            elif bearer in self.access_tokens:
                if self.subscription_required:
                    return httpx.Response(402, json={"error": "subscription_required",
                                                     "error_description": "Subscribe first."})
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
            header, payload, signature = json.loads(request.content)["assertion"].split(".")
            claims = json.loads(_unb64url(payload))
            device = self.devices.get(claims.get("iss", ""))
            if device is None or device["revoked"]:
                return httpx.Response(401, json={"error": "device_revoked" if device else "invalid_client"})
            # As Lumi Cloud checks it: an EdDSA signature by the enrolled key, for this audience.
            assert json.loads(_unb64url(header))["alg"] == "EdDSA"
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(device["public_key"]))
            key.verify(_unb64url(signature), f"{header}.{payload}".encode())
            assert claims["aud"] == f"{URL}/api/v1/devices/token" and claims["sub"] == claims["iss"]
            assert claims["exp"] - claims["iat"] <= 300
            device_id = claims["iss"]
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
            answer = {"organization": {"id": "org_acme", "name": "Acme"}, "policy_version": self.policy_version,
                      "next_checkin_seconds": 3600, "trusted_keys": {self.key_id: self.public}}
            if getattr(self, "budget", None):
                answer["budget"] = self.budget
            return httpx.Response(200, json=answer)
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
        if path == "/api/v1/oversight/acknowledgments" and request.method == "POST":
            return self._acknowledgment(device_id, device, json.loads(request.content),
                                        request.headers.get("lumi-account-token", ""))
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "machine_policy_file", lambda: tmp_path / "no-machine-policy.json")
    monkeypatch.setattr(policy, "_registry_policy", lambda: None)
    monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
    monkeypatch.setattr(policy, "machine_keys", lambda: {})
    monkeypatch.delenv(cloud.URL_ENV, raising=False)
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
                                                   "has_seat": True, "personal": False}]
    # A team's organization is the person's to choose for this computer.
    assert client.device() == {}
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
    ("cloud.example.test", "Enter the Lumi Cloud address"),
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
    from lumi import activity

    activity.record_turn([{"event": "tool.result", "name": "check_run", "is_error": False,
                           "output": "secret test output"}, {"event": "text.done", "text": "secret answer"}])
    activity.record_turn([{"event": "error", "message": "secret failure"}])
    time.sleep(0.01)
    client.check_in()
    report = fake.checkins[-1]["usage"]
    assert report["models"][0]["model"] == "anthropic:claude-sonnet-4-5"
    assert report["models"][0]["requests"] == 2 and report["models"][0]["input_tokens"] == 2000
    done = fake.checkins[-1]["activity"]
    assert (done["turns"], done["completed"], done["errors"], done["verified"]) == (2, 1, 1, 1)
    assert "secret" not in json.dumps(fake.checkins[-1])  # no projects, sessions, paths or content
    client.check_in()
    assert fake.checkins[-1]["usage"]["models"] == []  # nothing new since the last check-in
    assert fake.checkins[-1]["activity"]["turns"] == 0


def test_no_seat_means_no_enrollment(fake):
    fake.has_seat = False
    client = _client(fake)
    _sign_in(client, fake)
    with pytest.raises(cloud.CloudError, match="No seat") as refused:
        client.enroll("org_acme")
    assert (refused.value.code, refused.value.status) == ("no_seat", 403)
    assert client.device() == {}


def test_the_device_key_signs_what_lumi_cloud_verifies(fake):
    # Organization oversight signs a confirmation of its notice with it (lumi/oversight.py).
    client = _client(fake)
    with pytest.raises(cloud.CloudError) as refused:
        client.sign_as_device(b"before enrolling")
    assert refused.value.code == "not_enrolled"
    _sign_in(client, fake)
    device = client.enroll("org_acme")
    signature = client.sign_as_device(b"canonical record")
    # base64url with padding, verified with the public key the device enrolled with.
    assert set(signature) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(fake.devices[device["id"]]["public_key"]))
    public.verify(base64.urlsafe_b64decode(signature), b"canonical record")


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


def test_a_downloaded_policy_with_a_section_of_the_wrong_type_isnt_applied(fake):
    # parse() used to raise AttributeError here, not the PolicyError the download handles.
    fake.publish({"permissions": "ask only"})
    client = _client(fake)
    _sign_in(client, fake)
    with pytest.raises(cloud.CloudError, match="permissions must be an object"):
        client.enroll("org_acme")
    assert not policy.cloud_policy_path().exists()


def test_a_stored_policy_lumi_cant_use_is_reported_not_raised(fake):
    fake.publish({"models": {"allowed": ["anthropic:*"]}})
    client = _client(fake)
    _sign_in(client, fake)
    client.enroll("org_acme")
    path = policy.cloud_policy_path()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["policy"]["permissions"] = "ask only"  # signed by the organization, but unusable
    envelope["signature"] = _b64(fake.org_key.sign(policy.canonical(envelope["policy"])))
    path.write_text(json.dumps(envelope), encoding="utf-8")
    state = policy.load(force=True)
    assert not state.cloud and "permissions must be an object" in state.cloud_error


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


def test_the_organizations_shared_credit_stops_model_requests(fake):
    from lumi import budgets

    budgets.reset()
    client = _client(fake)
    _sign_in(client, fake)
    client.enroll("org_acme")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    fake.budget = {"organization": "Acme", "period": month, "credit_usd": 50.0, "spent_usd": 45.0}
    client.check_in()
    rule = next(r for r in budgets.rules() if r.scope == "organization")
    assert (rule.owner, rule.block_usd, rule.base_usd) == ("Acme", 50.0, 45.0)
    assert budgets.evaluate(rows=[]) == []  # $45 of $50
    # This computer's spend since the check-in counts too; earlier spend was in Lumi Cloud's $45.
    rows = [{"ts": "2000-01-01T00:00:00.000Z", "cost_usd": 100.0},
            {"ts": rule.since, "cost_usd": 6.0}]
    rows[0]["ts"] = month + "-01T00:00:00.000Z"
    verdict = budgets.evaluate(rows=rows)[0]
    assert verdict.level == "block"
    assert verdict.message == ("This month's spend across Acme is $51.00, which reaches Acme's $50.00 "
                               "shared model credit.")
    # It survives a restart, and a check-in without it removes it.
    budgets.reset()
    _client(fake)
    assert any(r.scope == "organization" for r in budgets.rules())
    fake.budget = None
    client.check_in()
    assert not any(r.scope == "organization" for r in budgets.rules())
    # Last month's credit doesn't count this month.
    budgets.set_shared_credit({"organization": "Acme", "period": "2000-01", "credit_usd": 1.0, "spent_usd": 5.0})
    assert not any(r.scope == "organization" for r in budgets.rules())
    budgets.reset()


# ── The account's tokens stay with the Lumi Cloud that issued them ─────────
# Two Lumi Clouds on one transport: A (the fake above, which signs the person
# in) and B, which only records what it's sent. Whatever moves this computer to
# B (a machine policy, an enrollment, another address in Settings), nothing of
# A's sign-in may reach B, and A's sign-in isn't lost either.

B_URL = "https://b.example.test"


class OtherCloud:
    """Lumi Cloud B: records every request; knows none of A's tokens."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Unknown token."})
        if path == "/api/v1/devices":
            return httpx.Response(201, json={"device_id": "dev_b", "organization": {"id": "org_b", "name": "B"},
                                             "trusted_keys": {}})
        if path == "/api/v1/devices/token":
            return httpx.Response(200, json={"access_token": "devtok_b", "expires_in": 3600})
        if path == "/api/v1/devices/checkin":
            return httpx.Response(200, json={"next_checkin_seconds": 3600})
        return httpx.Response(200, json={})

    def holds(self, *secrets: str) -> bool:
        """Whether any request B got carried one of ``secrets``, in a header or in its body."""
        for request in self.seen:
            text = request.content.decode("utf-8", "replace") + " ".join(request.headers.values())
            if any(secret and secret in text for secret in secrets):
                return True
        return False


def _signed_in_at_a(fake: FakeCloud) -> tuple[cloud.CloudClient, OtherCloud, str, str]:
    other = OtherCloud()
    client = _client(fake)
    client.seen = []  # every request, to either Lumi Cloud

    def route(request: httpx.Request) -> httpx.Response:
        client.seen.append(request)
        return fake(request) if request.url.host == "cloud.example.test" else other(request)

    client._transport = httpx.MockTransport(route)
    _sign_in(client, fake)
    return client, other, client._access[0], client.settings.get("api_keys", cloud.REFRESH_SECRET)


def _machine_policy(tmp_path, monkeypatch, cloud_section: dict) -> None:
    machine = tmp_path / "machine-policy.json"
    machine.write_text(json.dumps({"schema": "lumi.policy/v1", "organization": "B Corp", "cloud": cloud_section}),
                       encoding="utf-8")
    monkeypatch.setattr(policy, "machine_policy_file", lambda: machine)
    policy.load(force=True)


def test_the_sign_in_records_the_lumi_cloud_that_issued_it(fake):
    opened: list[str] = []
    client = _client(fake, opened)
    client.begin_sign_in(URL)
    # A sign-in that doesn't complete changes nothing: not the address, not the account's.
    assert client.status()["url"] == cloud.DEFAULT_URL and client.account_url == ""
    client.cancel_sign_in()
    _wait(lambda: not client.status()["signing_in"])
    _sign_in(client, fake)
    assert (client.url, client.account_url) == (URL, URL)
    assert client.status()["signed_in"] and client.status()["signed_in_elsewhere"] == ""


def test_a_managed_enrollment_elsewhere_never_gets_the_account(fake, tmp_path, monkeypatch):
    client, other, access, refresh = _signed_in_at_a(fake)
    _machine_policy(tmp_path, monkeypatch, {"url": B_URL, "enrollment_token": "lce_b"})
    client.background_step()  # the machine policy enrolls the computer at B
    assert client.device()["organization_id"] == "org_b"
    assert (client.url, client.account_url) == (B_URL, URL)
    status = client.status()
    assert not status["signed_in"] and status["signed_in_elsewhere"] == URL
    client._access = None  # an hour later: whatever asks for the account now
    for call in (lambda: client.account_call("GET", "/api/v1/library"), lambda: client.account_token(client.url),
                 client.refresh_account):
        with pytest.raises(cloud.CloudError, match="Sign in again here") as refused:
            call()
        assert refused.value.code == "signed_out"
    from lumi import team_library

    with pytest.raises(team_library.LibraryError, match="Sign in again here"):
        team_library.sync(client)
    assert not other.holds(access, refresh)
    # A's sign-in isn't lost: nothing refreshed it at B, and B couldn't end it.
    assert fake.refresh_tokens[refresh] is False
    assert client.settings.get("api_keys", cloud.REFRESH_SECRET) == refresh
    # Signing out revokes it at A, where it was issued, never at B.
    client.sign_out()
    assert refresh not in fake.refresh_tokens
    assert not other.holds(refresh) and not any(request.url.path == "/oauth/revoke" for request in other.seen)
    assert client.status()["signed_in_elsewhere"] == "" and client.account_url == ""


def test_another_address_in_settings_never_gets_the_account(fake):
    client, other, access, refresh = _signed_in_at_a(fake)
    client.settings.update_section("cloud", {"url": B_URL})  # a hand edit, or an older version's Settings
    assert client.status()["signed_in_elsewhere"] == URL and not client.status()["signed_in"]
    client._access = None
    with pytest.raises(cloud.CloudError, match="Sign in again here"):
        client.account_call("GET", "/api/v1/me")
    assert other.seen == [] and not other.holds(access, refresh)
    # Back at A, the sign-in works as before.
    client.settings.update_section("cloud", {"url": URL})
    assert client.status()["signed_in"]
    assert client.refresh_account()["email"] == "ada@example.com"


def test_signing_in_somewhere_else_revokes_the_old_sign_in_where_it_was_issued(fake):
    client, other, _access, refresh = _signed_in_at_a(fake)
    # The first sign-in was issued at A/old (recorded so); the next one completes at A.
    client.settings.update_section("cloud", {"account_url": "https://cloud.example.test/old"})
    _sign_in(client, fake)
    revokes = [request for request in client.seen if request.url.path.endswith("/oauth/revoke")]
    assert [str(request.url) for request in revokes] == ["https://cloud.example.test/old/oauth/revoke"]
    assert not other.holds(refresh) and client.account_url == URL


def test_a_policy_cant_lock_the_lumi_cloud_address(fake):
    for name in ("cloud.url", "cloud.account_url", "cloud.account", "cloud.device"):
        with pytest.raises(policy.PolicyError, match="can't lock"):
            policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "settings": {name: B_URL}},
                         source="test")
    assert policy.parse({"schema": "lumi.policy/v1", "organization": "Acme",
                         "settings": {"cloud.remote_tasks": False}}, source="test")
    with pytest.raises(policy.PolicyError, match="true or false"):
        policy.parse({"schema": "lumi.policy/v1", "settings": {"cloud.remote_tasks": "off"}}, source="test")
    with pytest.raises(policy.PolicyError, match="user name or password"):
        policy.parse({"schema": "lumi.policy/v1", "cloud": {"url": "https://ada:pw@cloud.example.test"}},
                     source="test")


def test_a_downloaded_policy_cant_move_this_computer_to_another_lumi_cloud(fake):
    client, other, _access, _refresh = _signed_in_at_a(fake)
    client.enroll("org_acme")  # joined in the app; the organization's keys are pinned
    fake.publish({"settings": {"cloud.url": B_URL}})  # signed by the organization's key
    with pytest.raises(cloud.CloudError, match="can't lock 'cloud.url'"):
        client.check_in()
    assert client.url == URL and not policy.load().cloud
    assert other.seen == [] and client.status()["signed_in"]


def test_an_address_with_a_user_name_is_never_used_or_shown(fake):
    with pytest.raises(cloud.CloudError, match="user name or password"):
        cloud.normalize_url("https://ada:secret@cloud.example.test")
    client = _client(fake)
    client.settings.update_section("cloud", {"url": "https://ada:secret@cloud.example.test"})
    assert client.url == "" and "secret" not in json.dumps(client.status())


# ── Tokens with their issuer, and device requests with their enrollment ───────
# The second review of #101. A sign-in at B, completing while something works
# with A's sign-in, is _adopt: what _finish_sign_in does once B has answered.


def _sign_in_completes_at_b(client: cloud.CloudClient) -> None:
    client._adopt(B_URL, "access-issued-by-B", "refresh-issued-by-B", time.monotonic() + 3000,
                  {"user_id": "usr_b", "email": "ben@b.example", "name": "Ben", "organizations": []})


def _device_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if request.url.path.startswith("/api/v1/devices")]


def test_account_tokens_are_only_for_the_lumi_cloud_that_issued_them(fake):
    client, other, access, _refresh = _signed_in_at_a(fake)
    assert client.account_token(URL) == access
    assert client.account_token("HTTPS://Cloud.Example.test:443/") == access  # the same one, written otherwise
    for destination in (B_URL, f"{URL}/elsewhere", ""):
        with pytest.raises(cloud.CloudError) as refused:
            client.account_token(destination)
        assert refused.value.code == "signed_out"
    with pytest.raises(cloud.CloudError, match="Someone else"):
        client.account_token(URL, user_id="usr_2")
    assert client.account_token(URL, user_id="usr_1") == access
    # The token Lumi Cloud refused is refreshed once, where it was issued; one another request replaced isn't.
    fresh = client.account_token(URL, refused=access)
    assert fresh != access and fresh in fake.access_tokens
    assert client.account_token(URL, refused=access) == fresh
    assert other.seen == []


def test_a_refresh_overtaken_by_a_sign_in_elsewhere_keeps_nothing(fake):
    """The reviewer's n1b: A's refreshed token went to B, and A's rotated refresh token was kept as B's."""
    client, other, _access, _refresh = _signed_in_at_a(fake)
    racing = {"on": True}

    def route(request: httpx.Request) -> httpx.Response:
        client.seen.append(request)
        if request.url.host != "cloud.example.test":
            return other(request)
        answer = fake(request)
        if request.url.path == "/oauth/token" and racing["on"]:
            racing["on"] = False
            _sign_in_completes_at_b(client)  # while A answers the refresh
        return answer

    client._transport = httpx.MockTransport(route)
    client.seen = []  # from here on
    client._access = None  # A's access token expired: the next account call refreshes it at A
    with pytest.raises(cloud.CloudError) as changed:
        client.account_call("GET", "/api/v1/me")
    assert changed.value.code == "signed_out"
    assert other.seen == []  # nothing of A's reached B
    assert not any(request.url.path == "/api/v1/me" for request in client.seen)  # nor A: the call wasn't made
    # B's sign-in is the one kept; the refresh token A just made was revoked at A, where it was made.
    assert client.settings.get("api_keys", cloud.REFRESH_SECRET) == "refresh-issued-by-B"
    assert client.account_url == B_URL and all(used for used in fake.refresh_tokens.values())
    # From now on, B's token goes to B.
    client.account_call("GET", "/api/v1/me")
    assert [request.headers["authorization"] for request in other.seen] == ["Bearer access-issued-by-B"]


def test_an_ended_refresh_never_forgets_a_sign_in_that_completed_meanwhile(fake):
    client, other, _access, refresh = _signed_in_at_a(fake)
    fake.refresh_tokens[refresh] = True  # used elsewhere: A answers invalid_grant

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host != "cloud.example.test":
            return other(request)
        answer = fake(request)
        if request.url.path == "/oauth/token":
            _sign_in_completes_at_b(client)
        return answer

    client._transport = httpx.MockTransport(route)
    client._access = None
    with pytest.raises(cloud.CloudError, match="Sign in again"):
        client.account_token(URL)
    assert client.account_url == B_URL and client.settings.get("api_keys", cloud.REFRESH_SECRET) == \
        "refresh-issued-by-B"


def test_device_requests_go_only_to_the_lumi_cloud_the_computer_enrolled_with(fake):
    """The reviewer's n2a: after a sign-in at B, A's device token went to B with the check-in."""
    client, other, _access, _refresh = _signed_in_at_a(fake)
    device = client.enroll("org_acme")
    device_token = client._device_access[0]
    client.sign_out()  # still enrolled in Acme
    _sign_in_completes_at_b(client)
    assert client.url == B_URL and client.device_url == URL
    checkins = len(fake.checkins)
    client.check_in()  # to A, where the computer enrolled
    assert len(fake.checkins) == checkins + 1
    with pytest.raises(cloud.CloudError) as moved:  # a request prepared for another Lumi Cloud is refused
        client.device_call("POST", "/api/v1/devices/checkin", json={}, expect=B_URL)
    assert moved.value.code == "changed"
    assert _device_requests(other.seen) == [] and not other.holds(device_token)
    status = client.status()
    assert status["device_elsewhere"].startswith("This computer is enrolled in Acme at cloud.example.test, and "
                                                 "Lumi now uses b.example.test.")
    assert status["device"]["url"] == URL
    # Leaving tells A, with A's device token; B hears nothing.
    client.unenroll()
    assert fake.devices[device["id"]]["revoked"] and client.device() == {}
    assert _device_requests(other.seen) == []


@pytest.mark.parametrize("token", ["lce_b", ""])
def test_a_machine_policy_naming_another_lumi_cloud_ends_the_enrollment_and_enrolls_there(fake, tmp_path, monkeypatch,
                                                                                          token):
    """The reviewer's n2b: the machine policy's address is authoritative for enrollment (docs/lumi-cloud.md)."""
    client, other, _access, _refresh = _signed_in_at_a(fake)
    old = client.enroll("org_acme")
    device_token = client._device_access[0]
    _machine_policy(tmp_path, monkeypatch, {"url": B_URL, **({"enrollment_token": token} if token else {})})
    client.background_step()
    assert fake.devices[old["id"]]["revoked"]  # A was told, with its own device token
    assert not other.holds(device_token)
    if token:
        assert (client.device()["organization_id"], client.device()["how"], client.device_url) == ("org_b", "managed",
                                                                                                      B_URL)
        enrolling = other.seen[0]
        assert enrolling.url.path == "/api/v1/devices" and json.loads(enrolling.content)["enrollment_token"] == "lce_b"
        assert "authorization" not in enrolling.headers
        assert [request.url.path for request in other.seen[1:3]] == ["/api/v1/devices/token", "/api/v1/devices/checkin"]
    else:
        assert client.device() == {} and other.seen == []


def test_requests_from_chat_wait_while_the_computer_uses_another_lumi_cloud(fake, tmp_path):
    """The reviewer's n2c: B handed out a request, and it ran here with A's device token."""
    from lumi.remote_tasks import IDLE_SECONDS, RemoteTasks

    client, other, _access, _refresh = _signed_in_at_a(fake)
    client.enroll("org_acme")
    project = tmp_path / "project"
    project.mkdir()
    client.settings.update_section("cloud", {"remote_tasks": True, "remote_tasks_project": str(project),
                                             "remote_tasks_mode": "ask"})
    client.sign_out()
    _sign_in_completes_at_b(client)
    ran = []
    tasks = RemoteTasks(client.settings, client, session_factory=lambda *args: ran.append(args))
    assert tasks.blocked() == client.device_elsewhere() and "tasks from chat wait" in tasks.blocked()
    assert tasks.step() == IDLE_SECONDS and ran == []
    assert _device_requests(other.seen) == []


def test_a_request_from_chat_reports_only_to_the_lumi_cloud_that_handed_it_out(fake):
    client, _other, _access, _refresh = _signed_in_at_a(fake)
    client.enroll("org_acme")
    sent = []
    real = client._call

    def recording(method, url, **kwargs):
        sent.append(url)
        return real(method, url, **kwargs)

    client._call = recording
    with pytest.raises(cloud.CloudError) as moved:
        client.device_call("POST", "/api/v1/devices/tasks/t1/result", json={}, expect=B_URL)
    assert moved.value.code == "changed" and sent == []


@pytest.mark.parametrize("written", ["https://Cloud.Example.Test", "https://cloud.example.test:443",
                                     "https://cloud.example.test/", "HTTPS://CLOUD.EXAMPLE.TEST:443//"])
def test_the_same_lumi_cloud_written_differently_is_the_same(fake, written):
    assert cloud.same_address(written, URL) and cloud.normalize_url(written) == URL
    client, other, _access, _refresh = _signed_in_at_a(fake)
    client.settings.update_section("cloud", {"url": written})
    assert client.status()["signed_in"] and client.status()["signed_in_elsewhere"] == ""
    assert client.refresh_account()["email"] == "ada@example.com" and other.seen == []


def test_what_makes_lumi_cloud_addresses_differ():
    assert cloud.same_address("https://b\u00fccher.example", "https://xn--bcher-kva.example")
    assert cloud.same_address("http://127.0.0.1:80", "http://127.0.0.1")
    assert cloud.same_address("https://[::1]:443/", "https://[::1]")
    for other in ("https://cloud.example.test:8443", "https://cloud.example.test/path", "https://other.example.test",
                  "", "not an address", "https://ada:pw@cloud.example.test"):
        assert not cloud.same_address(other, URL), other


def test_signing_in_again_at_the_same_lumi_cloud_revokes_the_earlier_sign_in(fake):
    client, other, _access, first = _signed_in_at_a(fake)
    _sign_in(client, fake)
    second = client.settings.get("api_keys", cloud.REFRESH_SECRET)
    assert second != first and first not in fake.refresh_tokens and second in fake.refresh_tokens
    assert other.seen == []


def test_a_sign_out_its_lumi_cloud_cant_be_told_about_says_so(fake, tmp_path):
    from lumi import audit, offline

    log = audit.AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    client, other, _access, refresh = _signed_in_at_a(fake)
    offline.set_for_tests(enabled=True, allowed_hosts=())
    notice = client.sign_out()
    assert notice == ("Signed out on this computer. cloud.example.test wasn't told (offline mode kept Lumi from "
                      "reaching it), so that sign-in stays valid there until it expires.")
    status = client.status()
    assert status["notice"] == notice and not status["signed_in"] and status["signed_in_elsewhere"] == ""
    assert refresh in fake.refresh_tokens  # still valid at A: nothing reached it
    assert client.settings.get("api_keys", cloud.REFRESH_SECRET) == ""  # and nothing of it is kept here
    [record] = [json.loads(line) for path in sorted(log.root.glob("*.jsonl"))
                for line in path.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["type"] == "cloud.signed_out"]
    assert record["data"]["revoked"] is False
    # A sign-out Lumi Cloud hears about says nothing more.
    offline.set_for_tests(enabled=False)
    _sign_in(client, fake)
    assert client.sign_out() == "" and client.status()["notice"] == ""
    audit.set_for_tests(None)


# ── First launch ───────────────────────────────────────────────────────────────


def test_a_new_install_uses_luminarys_lumi_cloud(fake, monkeypatch):
    client = _client(fake)
    assert client.url == cloud.DEFAULT_URL == "https://cloud.lumi.luminaryanalytics.com"
    # Development against a local Lumi Cloud: LUMI_CLOUD_URL stands in for the default.
    monkeypatch.setenv(cloud.URL_ENV, "http://127.0.0.1:8700")
    assert client.url == "http://127.0.0.1:8700"
    # One saved in Settings (the last sign-in's) comes first.
    client.settings.update_section("cloud", {"url": URL})
    assert client.url == URL


def test_the_first_launch_offers_signing_in_once(fake):
    client = _client(fake)
    assert client.status()["first_run_prompt"] is True
    # Continuing without an account answers it.
    client.settings.update_section("onboarding", {"cloud_prompted": True})
    assert client.status()["first_run_prompt"] is False
    client.settings.update_section("onboarding", {"cloud_prompted": False})
    # So does signing in, from the offer or from Settings.
    _sign_in(client, fake)
    assert client.settings.get("onboarding", "cloud_prompted") is True
    client.settings.update_section("onboarding", {"cloud_prompted": False})
    assert client.status()["first_run_prompt"] is False  # signed in: nothing to offer


def test_no_offer_in_offline_mode_or_on_a_managed_computer(fake, tmp_path, monkeypatch):
    from lumi import offline

    client = _client(fake)
    offline.set_for_tests(enabled=True)
    try:
        assert client.status()["first_run_prompt"] is False
    finally:
        offline.set_for_tests(enabled=False)
    assert client.status()["first_run_prompt"] is True
    _machine_policy(tmp_path, monkeypatch, {"url": URL, "enrollment_token": "lce_managed"})
    assert client.status()["first_run_prompt"] is False


def test_a_personal_workspace_is_used_on_this_computer_at_once(fake):
    fake.personal = True
    client = _client(fake)
    _sign_in(client, fake)
    status = client.status()
    assert status["account"]["organizations"][0]["personal"] is True
    assert status["device"]["organization_id"] == "org_acme" and status["device"]["how"] == "joined"
    assert len(fake.devices) == 1 and fake.checkins and not status["error"]


def test_a_personal_workspace_without_a_seat_waits_for_settings(fake):
    fake.personal, fake.has_seat = True, False
    client = _client(fake)
    _sign_in(client, fake)
    assert client.device() == {} and not fake.devices and client.status()["signed_in"]


def test_a_failed_enrollment_keeps_the_sign_in_and_says_so(fake):
    fake.personal, fake.subscription_required = True, True
    client = _client(fake)
    _sign_in(client, fake)
    status = client.status()
    assert status["signed_in"] and client.device() == {}
    assert "isn't set up in Acme yet (Subscribe first.)" in status["notice"] and not status["error"]


@pytest.mark.parametrize("provider", ["google", "microsoft", "email"])
def test_the_chosen_way_to_sign_in_goes_to_lumi_cloud(fake, provider):
    opened: list[str] = []
    client = _client(fake, opened)
    client.begin_sign_in(URL, provider)
    assert parse_qs(urlsplit(opened[0]).query)["provider"] == [provider]
    client.cancel_sign_in()


def test_no_chosen_way_leaves_it_to_lumi_cloud_and_an_unknown_one_is_refused(fake):
    opened: list[str] = []
    client = _client(fake, opened)
    client.begin_sign_in(URL)
    assert "provider" not in parse_qs(urlsplit(opened[0]).query)
    client.cancel_sign_in()
    with pytest.raises(cloud.CloudError, match="Choose email"):
        client.begin_sign_in(URL, "evil&next=https://attacker.test")
