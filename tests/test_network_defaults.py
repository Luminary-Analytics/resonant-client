"""
Regression tests for network defaults — particularly the v0.4.0
Mac Studio Ollama URL story.

The user's infra (per `~/.claude/projects/.../memory/user_infra.md`)
keeps Ollama on a Mac Studio at `10.0.0.133:11434`. Pre-v0.4.0 the
client's first probe was `localhost:11434`, and only fell back to the
Mac Studio if `OLLAMA_HOST` was set. The v0.4.0 cut inverted that:
the Mac Studio URL is now the canonical default (`localhost` is
never a silent fallback — the welcome-screen wizard takes over when
the configured URL fails).

This file pins that behavior so a well-meaning future refactor can't
silently regress to `localhost`.
"""

from __future__ import annotations

import pytest

from resonant_client.network_defaults import resolve_ollama_url


# ── resolve_ollama_url ──────────────────────────────────────────────────


class TestResolveOllamaUrlDefaults:
    def test_returns_mac_studio_default_on_fresh_install(self, monkeypatch):
        # No env, no settings — must return the Mac Studio URL, NOT localhost.
        # The whole point of v0.4.0's URL chain was making the canonical
        # Resonant deployment default Just Work.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={})
        assert url == "http://10.0.0.133:11434"

    def test_does_not_silently_fall_back_to_localhost(self, monkeypatch):
        # Defensive — if a future "fallback to localhost" patch sneaks
        # in, this test catches it. Localhost as a silent default
        # masks "Ollama isn't where you think it is" misconfigurations.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={})
        assert "localhost" not in url
        assert "127.0.0.1" not in url

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
        assert url == "http://10.0.0.133:11434"


class TestResolveOllamaUrlMalformedSettings:
    def test_missing_network_section_uses_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={"general": {"theme": "dark"}})
        assert url == "http://10.0.0.133:11434"

    def test_network_not_a_dict_uses_default(self, monkeypatch):
        # Real-world setting files corrupt sometimes — assert we don't
        # crash on a `network: "garbage"` shape.
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        url = resolve_ollama_url(settings_data={"network": "garbage"})
        assert url == "http://10.0.0.133:11434"

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
