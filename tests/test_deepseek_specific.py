"""
Tests for cluster 3 — deepseek-v4-flash specific features.

Covers:
- Thinking-mode plumbing through OllamaBackend.__init__
- Thinking-mode persistence on SessionRecord
- BackendSpec round-trip preserves thinking_mode
- Big-context preset overrides env vars when toggled in settings
- get_runtime_telemetry returns a structured dict (best-effort; tolerates Ollama unreachable)
"""

from __future__ import annotations

import json
import os

import pytest

from resonant_client.backends import OllamaBackend
from resonant_client.gui.runtime import BackendSpec
from resonant_client.gui.sessions import SessionRecord


# ── Thinking-mode option plumbing ───────────────────────────────────────


class TestOllamaThinking:
    def test_no_thinking_default(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud")
        assert b.thinking_mode is None
        assert "think" not in b._ollama_options

    def test_low_thinking(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud", thinking="low")
        assert b.thinking_mode == "low"
        assert b._ollama_options["think"] == "low"

    def test_med_normalizes_medium(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud", thinking="medium")
        assert b.thinking_mode == "med"
        assert b._ollama_options["think"] == "med"

    def test_high_thinking(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud", thinking="high")
        assert b._ollama_options["think"] == "high"

    def test_unknown_value_drops_silently(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud", thinking="bananas")
        assert b.thinking_mode is None
        assert "think" not in b._ollama_options

    def test_off_drops(self):
        b = OllamaBackend("http://example", "deepseek-v4-flash:cloud", thinking="off")
        assert b.thinking_mode is None
        assert "think" not in b._ollama_options


# ── BackendSpec round-trip ─────────────────────────────────────────────


class TestBackendSpecThinking:
    def test_default_empty(self):
        spec = BackendSpec(backend_type="ollama", model="x")
        assert spec.thinking_mode == ""

    def test_to_dict_includes_thinking(self):
        spec = BackendSpec(backend_type="ollama", model="x", thinking_mode="med")
        data = spec.to_dict()
        assert data["thinking_mode"] == "med"

    def test_from_dict_round_trip(self):
        original = BackendSpec(backend_type="ollama", model="deepseek-v4-flash:cloud", thinking_mode="high")
        round_tripped = BackendSpec.from_dict(original.to_dict())
        assert round_tripped.thinking_mode == "high"


# ── SessionRecord round-trip ───────────────────────────────────────────


class TestSessionRecordThinking:
    def test_default_empty(self):
        s = SessionRecord(session_id="abc")
        assert s.thinking_mode == ""

    def test_persists_round_trip(self):
        s = SessionRecord(session_id="abc", thinking_mode="med")
        assert SessionRecord.from_dict(s.to_dict()).thinking_mode == "med"

    def test_to_summary_includes_thinking(self):
        s = SessionRecord(session_id="abc", thinking_mode="high")
        assert s.to_summary()["thinking_mode"] == "high"


# ── Big-context preset ─────────────────────────────────────────────────


class _SettingsStub:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        sec = self.data.get(section, {})
        if key is None:
            return sec
        return sec.get(key, default)


class TestBigContextPreset:
    def test_disabled_does_not_set_env(self, monkeypatch):
        monkeypatch.delenv("RESONANT_OLLAMA_NUM_CTX", raising=False)
        monkeypatch.delenv("RESONANT_OLLAMA_NUM_BATCH", raising=False)

        # Use the AppState method directly without instantiating a full AppState
        from resonant_client.gui import app as app_module
        AppState = app_module.AppState
        instance = AppState.__new__(AppState)
        instance.settings = _SettingsStub({"general": {"big_context_profile": False}})
        instance._apply_big_context_preset()
        assert "RESONANT_OLLAMA_NUM_CTX" not in os.environ
        assert "RESONANT_OLLAMA_NUM_BATCH" not in os.environ

    def test_enabled_sets_defaults(self, monkeypatch):
        monkeypatch.delenv("RESONANT_OLLAMA_NUM_CTX", raising=False)
        monkeypatch.delenv("RESONANT_OLLAMA_NUM_BATCH", raising=False)

        from resonant_client.gui import app as app_module
        AppState = app_module.AppState
        instance = AppState.__new__(AppState)
        instance.settings = _SettingsStub({"general": {"big_context_profile": True}})
        instance._apply_big_context_preset()
        assert os.environ["RESONANT_OLLAMA_NUM_CTX"] == "131072"
        assert os.environ["RESONANT_OLLAMA_NUM_BATCH"] == "2048"

    def test_env_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("RESONANT_OLLAMA_NUM_CTX", "64000")
        monkeypatch.delenv("RESONANT_OLLAMA_NUM_BATCH", raising=False)

        from resonant_client.gui import app as app_module
        AppState = app_module.AppState
        instance = AppState.__new__(AppState)
        instance.settings = _SettingsStub({"general": {"big_context_profile": True}})
        instance._apply_big_context_preset()
        # Env value preserved
        assert os.environ["RESONANT_OLLAMA_NUM_CTX"] == "64000"
        # Batch was unset → preset applies
        assert os.environ["RESONANT_OLLAMA_NUM_BATCH"] == "2048"


# ── Telemetry (offline tolerance) ──────────────────────────────────────


class TestTelemetry:
    def test_unreachable_returns_error_dict(self, monkeypatch):
        # Point at a definitely-not-listening port
        b = OllamaBackend("http://127.0.0.1:1", "deepseek-v4-flash:cloud")
        data = b.get_runtime_telemetry(timeout=0.5)
        assert isinstance(data, dict)
        assert "error" in data
        # All structured fields should still be present (defaults)
        assert "loaded_model" in data
        assert "context_length" in data
        assert "active_thinking" in data
