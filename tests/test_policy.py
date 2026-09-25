"""Organization policy: parsing, signatures, expiry, sources and enforcement."""

from __future__ import annotations

import base64
import json
import plistlib
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lumi import policy as lumi_policy
from lumi.policy import Policy, PolicyError, canonical, parse

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "packaging" / "policy" / "example-policy.json"


@pytest.fixture(autouse=True)
def no_machine_policy(monkeypatch):
    """Each test starts without a policy; nothing reads the real machine."""
    monkeypatch.setattr(lumi_policy, "_registry_policy", lambda: None)
    monkeypatch.setattr(lumi_policy, "_macos_managed_policy", lambda: None)
    monkeypatch.setattr(lumi_policy, "machine_keys", lambda: {})
    monkeypatch.delenv("LUMI_POLICY_FILE", raising=False)
    lumi_policy.set_for_tests(None)
    yield
    lumi_policy.set_for_tests(None)


def _keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(public).decode()


def _signed(document, private, key_id="acme-1"):
    signature = base64.b64encode(private.sign(canonical(document))).decode()
    return {"policy": document, "signature": signature, "key_id": key_id}


BASE = {"schema": "lumi.policy/v1", "organization": "Acme"}


class TestParse:
    def test_example_policy_is_valid(self):
        policy = parse(json.loads(EXAMPLE.read_text(encoding="utf-8")), source="example")
        assert policy.organization == "Example Corp"
        assert policy.locked("security", "cli_adapters") and policy.value("security", "cli_adapters") is False
        assert policy.mode_allowed("ask") and not policy.mode_allowed("bypass")
        assert policy.model_allowed("anthropic", "claude-sonnet-4-6")
        assert not policy.model_allowed("openrouter", "anthropic/claude")
        assert not policy.model_allowed("ollama", "llama3")  # not in the allowlist
        assert policy.mcp_server_allowed("docs-internal", stdio=False)
        assert not policy.mcp_server_allowed("github", stdio=True)
        assert not policy.mcp_server_allowed("random", stdio=False)
        assert policy.pack_allowed("example-tools") and not policy.pack_allowed("other")
        assert ".env" in policy.exclude and len(policy.shell_rules) == 1

    @pytest.mark.parametrize("document, message", [
        ([], "JSON object"),
        ({"schema": "other/v1"}, "Unknown policy schema"),
        ({**BASE, "settings": {"no_dot": 1}}, "section.key"),
        ({**BASE, "permissions": {"allowed_modes": ["yolo"]}}, "allowed_modes"),
        ({**BASE, "models": {"allowed": "anthropic:*"}}, "list of strings"),
        ({**BASE, "expires_at": "next week"}, "ISO 8601"),
        ({**BASE, "shell": {"rules": ["deny all"]}}, "rule objects"),
    ])
    def test_invalid_documents(self, document, message):
        with pytest.raises(PolicyError, match=message):
            parse(document, source="test")

    def test_empty_policy_changes_nothing(self):
        policy = parse(dict(BASE), source="test")
        assert policy.mode_allowed("bypass") and policy.model_allowed("any", "model")
        assert policy.mcp_server_allowed("x", stdio=True) and policy.pack_allowed("x")


class TestSignatures:
    def test_valid_signature(self):
        private, public = _keypair()
        policy = parse(_signed(dict(BASE), private), source="cloud", trusted_keys={"acme-1": public})
        assert policy.signed and policy.organization == "Acme"

    def test_tampered_policy_is_rejected(self):
        private, public = _keypair()
        signed = _signed(dict(BASE), private)
        signed["policy"]["settings"] = {"security.cli_adapters": True}
        with pytest.raises(PolicyError, match="signature doesn't match"):
            parse(signed, source="cloud", trusted_keys={"acme-1": public})

    def test_unknown_key_and_missing_signature(self):
        private, _ = _keypair()
        with pytest.raises(PolicyError, match="doesn't trust"):
            parse(_signed(dict(BASE), private), source="cloud", trusted_keys={})
        with pytest.raises(PolicyError, match="must be signed"):
            parse(dict(BASE), source="cloud", require_signature=True)


class TestExpiry:
    def test_current_grace_expired(self):
        expires = "2026-09-01T00:00:00Z"
        policy = parse({**BASE, "expires_at": expires, "grace_days": 7}, source="cloud")
        at = lumi_policy._parse_time(expires)
        assert policy.expiry_state(at - 60) == "current"
        assert policy.expiry_state(at + 3 * 86400) == "grace"
        assert policy.expiry_state(at + 8 * 86400) == "expired"

    def test_expired_policy_blocks_requests(self):
        lumi_policy.set_for_tests(parse({**BASE, "expires_at": "2020-01-01T00:00:00Z", "grace_days": 1}, source="t"))
        assert "expired" in lumi_policy.blocked_reason()
        lumi_policy.set_for_tests(parse(dict(BASE), source="t"))
        assert lumi_policy.blocked_reason() == ""


class TestSources:
    def test_machine_file_and_env_override_precedence(self, tmp_path, monkeypatch):
        machine = tmp_path / "machine" / "policy.json"
        machine.parent.mkdir()
        machine.write_text(json.dumps({**BASE, "organization": "Machine"}), encoding="utf-8")
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps({**BASE, "organization": "Mine"}), encoding="utf-8")
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))

        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: tmp_path / "nothing.json")
        assert lumi_policy.load(force=True).policy.organization == "Mine"
        # A machine policy always wins over the environment variable.
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: machine)
        assert lumi_policy.load(force=True).policy.organization == "Machine"

    def test_invalid_machine_policy_blocks_instead_of_vanishing(self, tmp_path, monkeypatch):
        broken = tmp_path / "policy.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: broken)
        state = lumi_policy.load(force=True)
        assert state.policy is None and "invalid" in state.error
        assert "administrator" in lumi_policy.blocked_reason()

    def test_signed_machine_policy_uses_machine_keys(self, tmp_path, monkeypatch):
        private, public = _keypair()
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(_signed(dict(BASE), private)), encoding="utf-8")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: path)
        assert "doesn't trust" in lumi_policy.load(force=True).error
        monkeypatch.setattr(lumi_policy, "machine_keys", lambda: {"acme-1": public})
        assert lumi_policy.load(force=True).policy.signed


class TestTemplates:
    def test_admx_and_adml_agree(self):
        ns = {"p": "http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions"}
        admx = ET.parse(ROOT / "packaging" / "policy" / "lumi.admx").getroot()
        adml = ET.parse(ROOT / "packaging" / "policy" / "en-US" / "lumi.adml").getroot()
        strings = {s.get("id") for s in adml.iterfind(".//p:string", ns)}
        presentations = {p.get("id") for p in adml.iterfind(".//p:presentation", ns)}
        for element in admx.iter():
            for attribute in ("displayName", "explainText", "presentation"):
                value = element.get(attribute) or ""
                if value.startswith("$(string."):
                    assert value[9:-1] in strings, value
                if value.startswith("$(presentation."):
                    assert value[15:-1] in presentations, value
        keys = {p.get("key") for p in admx.iterfind(".//p:policy", ns)}
        assert keys == {lumi_policy.REGISTRY_KEY}
        values = {e.get("valueName") for e in admx.iter() if e.get("valueName")}
        assert values == {"Policy", "PolicyFile", "PolicyKeys"}

    def test_mobileconfig_carries_a_valid_policy(self):
        profile = plistlib.loads((ROOT / "packaging" / "policy" / "lumi-policy.mobileconfig").read_bytes())
        [payload] = profile["PayloadContent"]
        assert payload["PayloadType"] == lumi_policy.MAC_DOMAIN
        assert parse(json.loads(payload["Policy"]), source="profile").organization == "Example Corp"


def test_policy_summary_is_serializable():
    policy = parse(json.loads(EXAMPLE.read_text(encoding="utf-8")), source="example")
    assert json.loads(json.dumps(policy.summary()))["organization"] == "Example Corp"
    assert isinstance(Policy(), Policy) and time.time() > 0
