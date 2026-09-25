"""Zero data retention: connections that keep no data, and policies that require them."""

from __future__ import annotations

import pytest

from lumi import connections, policy
from lumi.policy import PolicyError, parse


class _Settings:
    def __init__(self, items):
        self.items = items

    def get(self, section, key=None, default=None):
        return self.items if section == "connections" and key is None else default


@pytest.fixture(autouse=True)
def _resolver():
    yield
    policy.set_zero_retention_resolver(None)


def _policy(**models) -> policy.Policy:
    return parse({"schema": "lumi.policy/v1", "organization": "Acme", "models": models}, source="test")


def test_a_connection_says_whether_it_keeps_data():
    base = {"name": "Gateway", "type": "openai-compatible", "base_url": "https://llm.example.com/v1"}
    assert connections.normalize_connection(base)["zero_retention"] is False
    assert connections.normalize_connection({**base, "zero_retention": True})["zero_retention"] is True
    assert connections.normalize_connection({**base, "zero_retention": "yes"})["zero_retention"] is False


def test_requiring_zero_retention_allows_only_what_keeps_no_data():
    settings = _Settings([
        {"id": "zdr-gateway", "name": "ZDR gateway", "type": "openai-compatible",
         "base_url": "https://zdr.example.com/v1", "zero_retention": True},
        {"id": "other", "name": "Other", "type": "openai-compatible", "base_url": "https://other.example.com/v1"},
    ])
    policy.set_zero_retention_resolver(connections.zero_retention_resolver(settings))
    required = _policy(require_zero_retention=True, zero_retention_providers=["anthropic"])
    assert required.model_allowed("ollama", "qwen3")  # on this computer or the local network
    assert not required.model_allowed("ollama", "gpt-oss:120b-cloud")  # runs on ollama.com
    assert required.model_allowed("anthropic", "claude-sonnet-5")  # the organization's agreement
    assert not required.model_allowed("openai", "gpt-5")
    assert not required.model_allowed("codex", "gpt-5.5")
    assert required.model_allowed("conn-zdr-gateway", "llama-4")
    assert not required.model_allowed("conn-other", "llama-4")
    # Other rules still apply on top.
    blocked = _policy(require_zero_retention=True, zero_retention_providers=["anthropic"], blocked=["anthropic:*"])
    assert not blocked.model_allowed("anthropic", "claude-sonnet-5")
    # Without the requirement, nothing changes.
    assert _policy().model_allowed("openai", "gpt-5")
    summary = required.summary()
    assert summary["require_zero_retention"] is True and summary["zero_retention_providers"] == ["anthropic"]


def test_the_policy_fields_are_checked():
    with pytest.raises(PolicyError, match="require_zero_retention must be true or false"):
        _policy(require_zero_retention="yes")
    with pytest.raises(PolicyError, match="zero_retention_providers"):
        _policy(zero_retention_providers="anthropic")


def test_an_unreadable_connection_doesnt_qualify():
    def broken(backend):
        raise RuntimeError("settings unavailable")

    policy.set_zero_retention_resolver(broken)
    assert not _policy(require_zero_retention=True).model_allowed("conn-x", "m")
