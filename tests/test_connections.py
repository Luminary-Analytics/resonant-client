"""Custom model connections: validation, backends, specs and the Settings commands."""

from __future__ import annotations

import json

import httpx
import pytest

from lumi.anthropic_api import AnthropicBackend
from lumi.connections import (
    OpenAICompatibleBackend,
    create_connection_backend,
    discover_models,
    normalize_connection,
)
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from lumi.openai_api import OpenAIResponsesBackend

GATEWAY = {"name": "Company gateway", "type": "openai-compatible", "base_url": "https://llm.acme.example/v1",
           "headers": {"X-Team": "platform"}, "models": "qwen-coder, gpt-oss-120b"}


def test_normalize_fills_defaults_and_slugs_the_id():
    connection = normalize_connection(GATEWAY)
    assert connection["id"] == "company-gateway"
    assert connection["models"] == ["qwen-coder", "gpt-oss-120b"]
    assert connection["auth"] == "bearer" and connection["max_tokens_param"] == "max_tokens"
    local = normalize_connection({"name": "LiteLLM", "type": "openai-compatible", "base_url": "http://localhost:4000/v1"})
    assert local["base_url"] == "http://localhost:4000/v1"


@pytest.mark.parametrize(("change", "message"), [
    ({"base_url": "http://llm.acme.example/v1"}, "https"),
    ({"base_url": "https://user:pw@llm.acme.example"}, "credentials"),
    ({"headers": {"Host": "evil"}}, "can't be set"),
    ({"type": "bogus"}, "connection type"),
    ({"name": ""}, "name"),
    ({"auth": "aws"}, "Bedrock"),
    ({"context_window": 10}, "between"),
])
def test_normalize_rejects_unsafe_or_malformed_entries(change, message):
    with pytest.raises(ValueError, match=message):
        normalize_connection({**GATEWAY, **change})


def test_cloud_platforms_need_their_location_and_models():
    with pytest.raises(ValueError, match="region"):
        normalize_connection({"name": "Bedrock", "type": "anthropic-bedrock", "models": ["m"]})
    with pytest.raises(ValueError, match="model"):
        normalize_connection({"name": "Bedrock", "type": "anthropic-bedrock", "region": "us-east-1"})
    with pytest.raises(ValueError, match="project"):
        normalize_connection({"name": "Vertex", "type": "anthropic-vertex", "region": "us-east5", "models": ["m"]})
    bedrock = normalize_connection({"name": "Bedrock", "type": "anthropic-bedrock", "region": "us-east-1",
                                    "models": ["us.anthropic.claude-v1:0"]})
    assert bedrock["auth"] == "aws"
    with pytest.raises(ValueError, match="already exists"):
        normalize_connection(GATEWAY, existing_ids={"company-gateway"})


def test_openai_compatible_backend_sends_the_connection_headers_and_limits():
    connection = normalize_connection({**GATEWAY, "max_tokens_param": "max_completion_tokens",
                                       "context_window": 65536, "vision": True})
    backend = OpenAICompatibleBackend(connection, "qwen-coder", "sk-gw")
    headers = backend._request_headers()
    assert headers["Authorization"] == "Bearer sk-gw" and headers["X-Team"] == "platform"
    payload = backend._payload("Hi", [{"role": "tool_catalog", "tools": [{"type": "function", "function": {
        "name": "extra", "parameters": {"type": "object"}}}]}], "sys", [], 512)
    assert payload["max_completion_tokens"] == 512 and "reasoning_effort" not in payload
    assert not any(m.get("tools") for m in payload["messages"])
    assert backend.effective_context_tokens == 65536 and backend.capability_profile.supports("vision")

    keyed = OpenAICompatibleBackend(normalize_connection(
        {**GATEWAY, "auth": "header", "auth_header": "api-key", "reasoning_effort": "high"}), "m", "k")
    assert keyed._request_headers()["api-key"] == "k"
    assert keyed._payload("Hi", [], "", [], None)["reasoning_effort"] == "high"
    with pytest.raises(ValueError, match="Add the key"):
        OpenAICompatibleBackend(normalize_connection(GATEWAY), "m", "")


def test_each_connection_type_builds_its_backend():
    azure = create_connection_backend(normalize_connection({
        "name": "Azure", "type": "azure-openai", "base_url": "https://acme.openai.azure.com/openai/v1",
        "models": ["gpt5-prod"]}), "gpt5-prod", "key")
    assert isinstance(azure, OpenAIResponsesBackend) and azure.azure and azure._request_headers()["api-key"] == "key"
    bedrock = create_connection_backend(normalize_connection({
        "name": "Bedrock", "type": "anthropic-bedrock", "region": "eu-west-1", "models": ["m"]}), "m")
    assert isinstance(bedrock, AnthropicBackend) and bedrock.platform == "bedrock"
    assert bedrock.name == "conn-bedrock" and bedrock.PROVIDER_LABEL == "Bedrock"
    proxy = create_connection_backend(normalize_connection({
        "name": "Claude proxy", "type": "anthropic", "base_url": "https://claude.acme.example",
        "auth": "bearer"}), "claude-a", "tok")
    assert proxy._request_headers()["authorization"] == "Bearer tok"


def test_discovery_uses_the_endpoint_when_no_models_are_listed():
    connection = normalize_connection({**GATEWAY, "models": []})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://llm.acme.example/v1/models")
        assert request.headers["authorization"] == "Bearer sk" and request.headers["x-team"] == "platform"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    assert discover_models(connection, "sk", transport=httpx.MockTransport(handler)) == ["a", "b"]
    assert discover_models(normalize_connection(GATEWAY), "sk") == ["qwen-coder", "gpt-oss-120b"]


def test_backend_spec_rebuilds_a_connection_backend_from_settings(tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("connections", None, [normalize_connection(GATEWAY)])
    settings.set("api_keys", "conn_company-gateway", "sk-saved")
    spec = BackendSpec(backend_type="conn-company-gateway", model="qwen-coder",
                       api_key_source="settings", api_key_setting="conn_company-gateway")
    backend = spec.create_backend(settings)
    assert isinstance(backend, OpenAICompatibleBackend) and backend.api_key == "sk-saved"
    assert "sk-saved" not in json.dumps(spec.to_dict())

    settings.set("connections", None, [])
    with pytest.raises(ValueError, match="removed"):
        spec.create_backend(settings)


# ── Settings commands ────────────────────────────────────────────────────


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _command(settings, command, **msg):
    """Run one socket command against real settings and return what it sent."""
    import asyncio
    from types import SimpleNamespace

    from lumi.gui import ws_commands

    def update_setting_value(section, key, value, *, clear_secret=False):
        if key is None:
            settings.update_section(section, value)
        else:
            settings.set(section, key, value)
        return settings.get_masked()

    state = SimpleNamespace(settings=settings, backend_spec=None, update_setting_value=update_setting_value,
                            get_init_data=lambda refresh_only=False: {"event": "init"})
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": command, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_save_list_and_delete_a_connection_without_exposing_its_key(tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    sent = _command(settings, "connection_save", connection=GATEWAY, api_key="sk-secret")
    assert sent[0] == {"event": "connection_saved", "data": {"id": "company-gateway"}}
    listed = sent[1]["data"]["items"]
    assert listed[0]["id"] == "company-gateway" and listed[0]["has_key"] is True
    assert "sk-secret" not in json.dumps(sent)
    assert settings.get("api_keys", "conn_company-gateway") == "sk-secret"

    refused = _command(settings, "connection_save",
                       connection={**GATEWAY, "name": "Public", "base_url": "http://public.example"})
    assert "https" in refused[0]["data"]["error"]
    # Connection keys are written only through connection_save.
    blocked = _command(settings, "update_settings", section="api_keys", key="conn_company-gateway",
                       value="overwrite")
    assert blocked[0]["event"] == "error"
    assert settings.get("api_keys", "conn_company-gateway") == "sk-secret"

    deleted = _command(settings, "connection_delete", id="company-gateway")
    assert deleted[0]["data"] == {"deleted": "company-gateway"}
    assert settings.get("connections") == []
    assert settings.get("api_keys", "conn_company-gateway") == ""


def test_renaming_a_connection_moves_its_stored_key(tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    _command(settings, "connection_save", connection=GATEWAY, api_key="sk-keep")
    sent = _command(settings, "connection_save", original_id="company-gateway",
                    connection={**GATEWAY, "id": "gateway-2", "name": "Gateway 2"})
    assert sent[0]["data"] == {"id": "gateway-2"}
    assert settings.get("api_keys", "conn_gateway-2") == "sk-keep"
    assert settings.get("api_keys", "conn_company-gateway") == ""
    assert [c["id"] for c in settings.get("connections")] == ["gateway-2"]


def test_testing_a_draft_reports_models_or_a_fix(tmp_path, monkeypatch):
    from lumi import connections

    settings = SettingsManager(tmp_path / "settings.json")
    monkeypatch.setattr(connections, "discover_models", lambda c, key, timeout=5.0: ["m1", "m2"])
    ok = _command(settings, "connection_test", connection={**GATEWAY, "models": []}, api_key="sk")
    assert ok[0]["data"] == {"ok": True, "models": ["m1", "m2"], "message": "Connected · 2 models available"}

    monkeypatch.setattr(connections, "discover_models", lambda c, key, timeout=5.0: [])
    empty = _command(settings, "connection_test", connection={**GATEWAY, "models": []}, api_key="sk")
    assert empty[0]["data"]["ok"] is False and "listed no models" in empty[0]["data"]["message"]
