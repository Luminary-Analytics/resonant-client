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
        ({**BASE, "shell": "deny curl"}, "shell must be an object"),
        # Rules that used to load although they can't be applied as written.
        ({**BASE, "shell": {"rules": [{"tool_pattern": "bash", "action": "deny", "arg_patterns": "curl"}]}},
         "shell.rules rule 1: arg_patterns"),
        ({**BASE, "shell": {"rules": [{"tool_pattern": "bash", "action": "deny"},
                                      {"tool_pattern": 5, "action": "deny"}]}}, "rule 2: tool_pattern"),
        ({**BASE, "shell": {"rules": [{"tool_pattern": "bash", "action": "block"}]}}, "action must be"),
        ({**BASE, "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                                       "arg_patterns": {"command": "(curl"}}]}}, "regular expression"),
        ({**BASE, "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                                       "arg_globs": {"command": ["curl*", 5]}}]}}, "arg_globs"),
        ({**BASE, "settings": {"security.shell_sandbox": "strict"}}, "shell_sandbox"),
        # Sections of the wrong type. These raised something other than
        # PolicyError, which load() didn't catch.
        ({**BASE, "permissions": "ask only"}, "permissions must be an object"),
        ({**BASE, "models": ["anthropic:*"]}, "models must be an object"),
        ({**BASE, "mcp": 5}, "mcp must be an object"),
        ({**BASE, "extensions": ["team-*"]}, "extensions must be an object"),
        ({**BASE, "files": ".env"}, "files must be an object"),
        ({**BASE, "pricing": "cheap"}, "pricing must be an object"),
        ({**BASE, "settings": ["privacy.secret_scan"]}, "settings must be an object"),
        ({**BASE, "cloud": "https://cloud.example.com"}, "cloud must be an object"),
        ({**BASE, "approvals": []}, "approvals must be an object"),
        ({**BASE, "trusted_keys": ["acme-1"]}, "trusted_keys must map"),
        ({**BASE, "trusted_keys": {"acme-1": 5}}, "trusted_keys must map"),
        ({**BASE, "grace_days": [7]}, "grace_days"),
        ({**BASE, "grace_days": "a week"}, "grace_days"),
        ({**BASE, "grace_days": True}, "grace_days"),
        # Values that loosened what they control when written as text.
        ({**BASE, "mcp": {"allow_stdio": "no"}}, "mcp.allow_stdio must be true or false"),
        ({**BASE, "extensions": {"require_signed": "true"}}, "extensions.require_signed must be true or false"),
    ])
    def test_invalid_documents(self, document, message):
        with pytest.raises(PolicyError, match=message):
            parse(document, source="test")

    def test_empty_policy_changes_nothing(self):
        policy = parse(dict(BASE), source="test")
        assert policy.mode_allowed("bypass") and policy.model_allowed("any", "model")
        assert policy.mcp_server_allowed("x", stdio=True) and policy.pack_allowed("x")

    def test_null_sections_are_empty_and_grace_days_may_be_text(self):
        sections = ("settings", "permissions", "models", "mcp", "extensions", "files", "pricing",
                    "shell", "approvals", "trusted_keys", "cloud")
        policy = parse({**BASE, **dict.fromkeys(sections), "grace_days": "3"}, source="test")
        assert policy.mode_allowed("bypass") and policy.mcp_server_allowed("x", stdio=True)
        assert not policy.require_signed and policy.grace_days == 3
        assert parse({**BASE, "mcp": {"allow_stdio": None}}, source="test").mcp_allow_stdio
        assert parse({**BASE, "grace_days": None}, source="test").grace_days == 0


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

    @pytest.mark.parametrize("raw", [
        json.dumps({**BASE, "permissions": "ask only"}).encode(),
        json.dumps({**BASE, "mcp": 5}).encode(),
        json.dumps({**BASE, "trusted_keys": ["acme-1"]}).encode(),
        json.dumps({**BASE, "grace_days": [7]}).encode(),
        json.dumps(BASE).encode("utf-16"),  # Windows PowerShell 5.1's Out-File default
        b'{"schema": "lumi.policy/v1", "x": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    ], ids=["permissions is text", "mcp is a number", "trusted_keys is a list", "grace_days is a list",
            "UTF-16", "nested too deeply"])
    def test_a_machine_policy_lumi_cant_use_blocks_on_every_load(self, tmp_path, monkeypatch, raw):
        path = tmp_path / "policy.json"
        path.write_bytes(raw)
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: path)
        # These used to raise from the first load; later loads then saw no policy at all.
        first = lumi_policy.load(force=True)
        assert first.policy is None and first.error
        assert lumi_policy.load().error == first.error and lumi_policy.current() is None
        assert "administrator" in lumi_policy.blocked_reason()

    def test_a_policy_file_with_a_utf8_byte_order_mark_applies(self, tmp_path, monkeypatch):
        # Windows PowerShell 5.1 writes one for -Encoding utf8; it used to make the policy invalid.
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({**BASE, "permissions": {"allowed_modes": ["ask"]}}), encoding="utf-8-sig")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: path)
        state = lumi_policy.load(force=True)
        assert state.error == "" and not state.policy.mode_allowed("bypass")

    def test_anything_unforeseen_while_loading_fails_closed(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(BASE), encoding="utf-8")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: path)

        def broken(*args, **kwargs):
            raise RuntimeError("an unforeseen bug")

        monkeypatch.setattr(lumi_policy, "parse", broken)
        assert "is invalid: an unforeseen bug" in lumi_policy.load(force=True).error
        monkeypatch.setattr(lumi_policy, "_load_text", broken)
        state = lumi_policy.load(force=True)
        assert state.policy is None and "couldn't be loaded: an unforeseen bug" in state.error
        assert lumi_policy.load() is state and "administrator" in lumi_policy.blocked_reason()

    def test_a_cloud_policy_lumi_cant_use_leaves_the_machine_policy(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({**BASE, "models": {"allowed": ["ollama:*"]},
                                    "cloud": {"url": "https://cloud.example.com"}}), encoding="utf-8")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: path)
        lumi_policy.cloud_policy_path().parent.mkdir(parents=True, exist_ok=True)
        lumi_policy.cloud_policy_path().write_text(json.dumps({"policy": {}, "signature": "x"}), encoding="utf-8")
        parse_machine_policy = lumi_policy.parse

        def parse(data, **kwargs):
            if kwargs.get("require_signature"):
                raise RuntimeError("an unforeseen bug")
            return parse_machine_policy(data, **kwargs)

        monkeypatch.setattr(lumi_policy, "parse", parse)
        state = lumi_policy.load(force=True)
        assert not state.cloud and state.policy.model_allowed("ollama", "qwen3")
        assert "an unforeseen bug" in state.cloud_error and "machine policy applies instead" in state.cloud_error

    def test_a_shell_rule_that_cant_be_applied_blocks_instead_of_failing_tool_calls(self, tmp_path, monkeypatch):
        broken = tmp_path / "policy.json"
        broken.write_text(json.dumps({**BASE, "shell": {"rules": [
            {"tool_pattern": "bash", "action": "deny", "arg_patterns": "curl"},
        ]}}), encoding="utf-8")
        monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: broken)
        state = lumi_policy.load(force=True)
        assert state.policy is None and "shell.rules rule 1: arg_patterns" in state.error
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
