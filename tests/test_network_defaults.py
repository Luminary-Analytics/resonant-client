"""Regression tests for portable network and model defaults."""

from __future__ import annotations

import pytest

from resonant_client.network_defaults import (
    default_thinking_for_model,
    get_default_model,
    resolve_ollama_url,
)


# ── resolve_ollama_url ──────────────────────────────────────────────────


class TestResolveOllamaUrlDefaults:
    def test_returns_local_default_on_fresh_install(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={})
        assert url == "http://127.0.0.1:11434"

    def test_default_is_loopback_not_private_infrastructure(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={})
        assert url.startswith("http://127.0.0.1")

    def test_strips_trailing_slash(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(
            explicit="http://10.0.0.133:11434/",
            settings_data={},
        )
        assert url == "http://10.0.0.133:11434"


class TestResolveOllamaUrlOverrideOrder:
    def test_explicit_argument_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
        url = resolve_ollama_url(
            explicit="http://explicit-host:9999",
            settings_data={"network": {"ollama_url": "http://settings-host:11434"}},
        )
        assert url == "http://explicit-host:9999"

    def test_env_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
        url = resolve_ollama_url(
            settings_data={"network": {"ollama_url": "http://settings-host:11434"}},
        )
        assert url == "http://env-host:11434"

    def test_settings_wins_over_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(
            settings_data={"network": {"ollama_url": "http://settings-host:11434"}},
        )
        assert url == "http://settings-host:11434"

    def test_empty_explicit_falls_through_to_env(self, monkeypatch):
        # Defensive — `explicit=""` (empty string from a CLI default)
        # should NOT override env / settings; it should fall through.
        monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
        url = resolve_ollama_url(explicit="", settings_data={})
        assert url == "http://env-host:11434"

    def test_whitespace_only_explicit_falls_through(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://env-host:11434")
        url = resolve_ollama_url(explicit="   ", settings_data={})
        assert url == "http://env-host:11434"

    def test_empty_settings_value_falls_through_to_default(self, monkeypatch):
        # An empty string in settings.json (the v0.4.0 default value)
        # must not be treated as "the user picked empty" — it should
        # fall through to the Mac Studio default.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(
            settings_data={"network": {"ollama_url": ""}},
        )
        assert url == "http://127.0.0.1:11434"


class TestResolveOllamaUrlMalformedSettings:
    def test_missing_network_section_uses_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={"general": {"theme": "dark"}})
        assert url == "http://127.0.0.1:11434"

    def test_network_not_a_dict_uses_default(self, monkeypatch):
        # Real-world setting files corrupt sometimes — assert we don't
        # crash on a `network: "garbage"` shape.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={"network": "garbage"})
        assert url == "http://127.0.0.1:11434"

    def test_settings_data_none_uses_default(self, monkeypatch):
        # `settings_data=None` triggers the disk-load path in
        # `_get_setting`. We're not asserting on that path's behavior
        # here (it depends on whether the caller has a real
        # settings.json on disk); just that the function doesn't blow
        # up when called with None.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        # No assertion on value — just making sure no exception.
        result = resolve_ollama_url(settings_data=None)
        assert isinstance(result, str)
        assert result.startswith("http")


# v0.4.4 (T1.4) — `TestOtherResolvers` deleted. Pre-v0.4.4 it smoke-
# tested `resolve_resonant_api_url` and `resolve_remote_engine_ws_url`
# (defaults `http://localhost:8000` and `ws://localhost:8765`). Both
# resolvers were removed when ResonantBackend left the codebase.


# ── get_default_model ────────────────────────────────────────────────────


class TestGetDefaultModel:
    def test_fresh_install_uses_backend_discovery(self, monkeypatch):
        monkeypatch.delenv("RESONANT_DEFAULT_MODEL", raising=False)
        assert get_default_model(settings_data={}) == ""

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("RESONANT_DEFAULT_MODEL", "deepseek-v4-pro:cloud")
        assert get_default_model(settings_data={}) == "deepseek-v4-pro:cloud"

    def test_settings_value_overrides_default(self, monkeypatch):
        monkeypatch.delenv("RESONANT_DEFAULT_MODEL", raising=False)
        assert get_default_model(
            settings_data={"general": {"default_model": "deepseek-v4-flash:cloud"}}
        ) == "deepseek-v4-flash:cloud"

    def test_env_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("RESONANT_DEFAULT_MODEL", "kimi-k2.5:cloud")
        assert get_default_model(
            settings_data={"general": {"default_model": "deepseek-v4-pro:cloud"}}
        ) == "kimi-k2.5:cloud"

    def test_empty_settings_value_uses_backend_discovery(self, monkeypatch):
        monkeypatch.delenv("RESONANT_DEFAULT_MODEL", raising=False)
        assert get_default_model(
            settings_data={"general": {"default_model": ""}}
        ) == ""


# ── default_thinking_for_model ───────────────────────────────────────────


class TestDefaultThinkingForModel:
    @pytest.mark.parametrize("model", [
        "glm-5.2:cloud",
        "glm-4.7:cloud",            # older GLM line — opt-in, not defaulted
        "deepseek-v4-pro:cloud",
        "deepseek-v4-flash:cloud",
        "llama3.1:8b",
        "qwen3.5:cloud",
        "",
        None,
    ])
    def test_others_default_to_off(self, model):
        assert default_thinking_for_model(model) == ""
