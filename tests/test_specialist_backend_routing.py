"""Tests for v0.5.8a1 — per-specialist Ollama model routing.

Covers two layers:
  1. `LocalSpecialistRunner._resolve_backend_for(spec)` — the runner-level
     dispatch that picks per-call backends.
  2. `AppState._resolve_specialist_model_override` and
     `AppState._build_specialist_backend` — the production wiring that
     reads settings + env vars and constructs an OllamaBackend with the
     override model.

The motivation: linux-bridge run hit `verdict=stuck` on a path-mismatch
that arguably needs stronger reasoning. Pinning `deepseek-v4-pro:cloud`
to REFLECT/PLAN_DEEP and leaving `flash` as the IMPLEMENT/EXPLORE
default closes some of the model-capability gap without dropping
local-first positioning.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from resonant_client.backends import OllamaBackend
from resonant_client.gui.app import AppState
from resonant_client.orchestration import (
    LocalSpecialistRunner,
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)


# ── LocalSpecialistRunner resolver dispatch ─────────────────────────────


class TestRunnerResolverDispatch:
    """The runner's `_resolve_backend_for` must:
      - Return default backend when no resolver configured
      - Return default backend when resolver returns None
      - Return resolver's backend when it returns one
      - Fall back to default when resolver raises (no crash)
    """

    def _node(self, spec):
        g = PlanGraph.new("test intent")
        n = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="goal", specialization=spec,
        )
        g.add_node(n)
        return n, g

    def test_no_resolver_uses_default_backend(self):
        default = MagicMock(name="default-backend")
        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[],
        )
        result = runner._resolve_backend_for(NodeSpecialization.REFLECT)
        assert result is default

    def test_resolver_returning_none_uses_default(self):
        default = MagicMock(name="default-backend")
        resolver = MagicMock(return_value=None)
        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[],
            specialist_backend_resolver=resolver,
        )
        result = runner._resolve_backend_for(NodeSpecialization.IMPLEMENT)
        assert result is default
        resolver.assert_called_once_with(NodeSpecialization.IMPLEMENT)

    def test_resolver_returning_backend_overrides_default(self):
        default = MagicMock(name="default-backend")
        override = MagicMock(name="pro-backend")
        resolver = MagicMock(return_value=override)
        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[],
            specialist_backend_resolver=resolver,
        )
        result = runner._resolve_backend_for(NodeSpecialization.REFLECT)
        assert result is override
        # And the resolver received the canonical specialization string
        # (not an enum, not uppercased) so it can use the same key as
        # settings/env var lookups.
        resolver.assert_called_once_with(NodeSpecialization.REFLECT)

    def test_resolver_raising_falls_back_to_default(self):
        default = MagicMock(name="default-backend")

        def boom(spec):
            raise RuntimeError("simulated resolver failure")

        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[],
            specialist_backend_resolver=boom,
        )
        # Must NOT raise — a routing failure should never block a
        # specialist from running. The default backend is always a
        # working option.
        result = runner._resolve_backend_for(NodeSpecialization.PLAN_DEEP)
        assert result is default


class TestRunnerSessionUsesResolvedBackend:
    """Integration: when _run_node constructs a Session, it must pass
    the RESOLVED backend (not the default) so the per-specialist
    override actually reaches the model."""

    def test_session_constructed_with_resolved_backend(self):
        default = MagicMock(name="flash-backend")
        override = MagicMock(name="pro-backend")
        resolver = MagicMock(return_value=override)

        # Capture the backend passed to Session.__init__.
        captured = {}

        def capture_session_init(self, backend, **kwargs):
            captured["backend"] = backend
            self.backend = backend
            self.project_path = kwargs.get("project_path") or "/tmp/proj"
            self.history = []
            self.todos = MagicMock()
            self._settings_ref = None

        def fake_run(self, user_msg, on_permission=None, on_choice=None, images=None):
            yield {"event": "text.done", "text": "done"}
            yield {"event": "session.end"}

        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[{"function": {"name": "file_read"}}],
            specialist_backend_resolver=resolver,
        )

        g = PlanGraph.new("intent")
        node = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="reflect on stuff", specialization=NodeSpecialization.REFLECT,
        )
        g.add_node(node)

        with patch(
            "resonant_client.orchestration.runner.Session.__init__",
            capture_session_init,
        ), patch(
            "resonant_client.orchestration.runner.Session.run", fake_run,
        ):
            runner(node, g)

        assert captured["backend"] is override, (
            f"expected Session to use override backend, got {captured['backend']!r}"
        )

    def test_session_uses_default_when_resolver_returns_none(self):
        default = MagicMock(name="flash-backend")
        resolver = MagicMock(return_value=None)

        captured = {}

        def capture_session_init(self, backend, **kwargs):
            captured["backend"] = backend
            self.backend = backend
            self.project_path = kwargs.get("project_path") or "/tmp/proj"
            self.history = []
            self.todos = MagicMock()
            self._settings_ref = None

        def fake_run(self, user_msg, on_permission=None, on_choice=None, images=None):
            yield {"event": "text.done", "text": "done"}
            yield {"event": "session.end"}

        runner = LocalSpecialistRunner(
            backend=default,
            project_path="/tmp/proj",
            all_tools=[{"function": {"name": "file_read"}}],
            specialist_backend_resolver=resolver,
        )

        g = PlanGraph.new("intent")
        node = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="implement feature", specialization=NodeSpecialization.IMPLEMENT,
        )
        g.add_node(node)

        with patch(
            "resonant_client.orchestration.runner.Session.__init__",
            capture_session_init,
        ), patch(
            "resonant_client.orchestration.runner.Session.run", fake_run,
        ):
            runner(node, g)

        assert captured["backend"] is default


# ── AppState._resolve_specialist_model_override ─────────────────────────


def _make_state_with_settings(get_handler):
    """Bypass __init__ and stub `.settings.get`. The handler is called
    with `(section, key, default)` and must return the value; this lets
    us simulate settings hits, misses, malformed values."""
    state = AppState.__new__(AppState)
    state.settings = MagicMock()
    state.settings.get = MagicMock(side_effect=get_handler)
    return state


class TestSpecialistModelOverrideResolution:
    def test_no_settings_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)
        state = _make_state_with_settings(lambda *a, **kw: {})
        assert state._resolve_specialist_model_override("reflect") == ""

    def test_settings_hit_returns_value(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        assert state._resolve_specialist_model_override("reflect") == \
            "deepseek-v4-pro:cloud"

    def test_settings_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(
            "RESONANT_SPECIALIST_REFLECT_MODEL", "env-model:cloud",
        )

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "settings-model:cloud"}
            return default

        state = _make_state_with_settings(get)
        # Settings wins so persistent UI configuration is authoritative.
        assert state._resolve_specialist_model_override("reflect") == \
            "settings-model:cloud"

    def test_env_var_fallback_when_settings_empty(self, monkeypatch):
        monkeypatch.setenv(
            "RESONANT_SPECIALIST_REFLECT_MODEL", "env-fallback:cloud",
        )
        state = _make_state_with_settings(lambda *a, **kw: {})
        assert state._resolve_specialist_model_override("reflect") == \
            "env-fallback:cloud"

    def test_env_var_uppercase_normalization(self, monkeypatch):
        # `plan_deep` (lowercase) → RESONANT_SPECIALIST_PLAN_DEEP_MODEL
        monkeypatch.setenv(
            "RESONANT_SPECIALIST_PLAN_DEEP_MODEL", "deep-pro:cloud",
        )
        state = _make_state_with_settings(lambda *a, **kw: {})
        assert state._resolve_specialist_model_override("plan_deep") == \
            "deep-pro:cloud"

    def test_settings_malformed_dict_falls_through(self, monkeypatch):
        # If settings returns a string instead of a dict (corrupted /
        # manually-edited settings.json), we shouldn't crash — fall
        # through to env/empty.
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return "not-a-dict"
            return default

        state = _make_state_with_settings(get)
        assert state._resolve_specialist_model_override("reflect") == ""

    def test_settings_non_string_value_ignored(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": 42}  # non-string value
            return default

        state = _make_state_with_settings(get)
        assert state._resolve_specialist_model_override("reflect") == ""

    def test_settings_whitespace_stripped(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "  deepseek-v4-pro:cloud  "}
            return default

        state = _make_state_with_settings(get)
        assert state._resolve_specialist_model_override("reflect") == \
            "deepseek-v4-pro:cloud"

    def test_empty_specialization_returns_empty(self):
        state = _make_state_with_settings(lambda *a, **kw: {})
        assert state._resolve_specialist_model_override("") == ""
        assert state._resolve_specialist_model_override("   ") == ""

    def test_settings_get_raising_falls_through(self, monkeypatch):
        # If settings.get crashes (e.g. settings.json deleted mid-run),
        # the override resolution should not crash either.
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(*a, **kw):
            raise RuntimeError("settings unreachable")

        state = _make_state_with_settings(get)
        assert state._resolve_specialist_model_override("reflect") == ""


# ── AppState._build_specialist_backend ──────────────────────────────────


class TestBuildSpecialistBackend:
    def test_no_override_returns_none(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)
        state = _make_state_with_settings(lambda *a, **kw: {})
        state.backend = OllamaBackend(
            base_url="http://10.0.0.133:11434",
            model="deepseek-v4-flash:cloud",
        )
        # No override configured → caller uses default → return None.
        assert state._build_specialist_backend("reflect") is None

    def test_override_builds_fresh_ollama_backend(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        state.backend = OllamaBackend(
            base_url="http://10.0.0.133:11434",
            model="deepseek-v4-flash:cloud",
        )
        result = state._build_specialist_backend("reflect")
        assert result is not None
        assert isinstance(result, OllamaBackend)
        assert result.model == "deepseek-v4-pro:cloud"
        # Inherits base_url from default.
        assert result.base_url == "http://10.0.0.133:11434"
        # Different instance — not the default.
        assert result is not state.backend

    def test_override_inherits_thinking_mode(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        # Default has thinking=high; override should inherit.
        state.backend = OllamaBackend(
            base_url="http://10.0.0.133:11434",
            model="deepseek-v4-flash:cloud",
            thinking="high",
        )
        result = state._build_specialist_backend("reflect")
        assert result is not None
        assert result.thinking_mode == "high"

    def test_override_with_no_thinking_passes_none(self, monkeypatch):
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        state.backend = OllamaBackend(
            base_url="http://10.0.0.133:11434",
            model="deepseek-v4-flash:cloud",
            # No thinking → thinking_mode is None.
        )
        result = state._build_specialist_backend("reflect")
        assert result is not None
        assert result.thinking_mode is None

    def test_non_ollama_default_backend_returns_none(self, monkeypatch):
        # Defensive — if the default backend is somehow non-Ollama
        # (shouldn't happen post-v0.4.0 but who knows), don't crash.
        # Fall through to default.
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        state.backend = MagicMock(name="some-other-backend")
        # MagicMock isn't an OllamaBackend instance → resolver bows out.
        assert state._build_specialist_backend("reflect") is None

    def test_constructor_failure_falls_through_to_none(self, monkeypatch):
        # If OllamaBackend's constructor somehow raises (network probe
        # blip during init, malformed url, etc.), log + return None so
        # the caller uses the default backend. We patch the class's
        # __init__ rather than the whole class so isinstance() in the
        # production code still works against the real type.
        monkeypatch.delenv("RESONANT_SPECIALIST_REFLECT_MODEL", raising=False)

        def get(section, key, default):
            if section == "general" and key == "specialist_model_overrides":
                return {"reflect": "deepseek-v4-pro:cloud"}
            return default

        state = _make_state_with_settings(get)
        state.backend = OllamaBackend(
            base_url="http://10.0.0.133:11434",
            model="deepseek-v4-flash:cloud",
        )
        original_init = OllamaBackend.__init__

        def boom_init(self, *args, **kwargs):
            # Only blow up when called for the SECOND time (the override
            # construction). The first call already happened above when
            # we built `state.backend`; we want isinstance to keep
            # working for the existing instance.
            raise RuntimeError("simulated init failure")

        monkeypatch.setattr(OllamaBackend, "__init__", boom_init)
        result = state._build_specialist_backend("reflect")
        # Restore so subsequent tests don't see the broken __init__.
        monkeypatch.setattr(OllamaBackend, "__init__", original_init)
        assert result is None


# ── End-to-end through IntentService construction ───────────────────────


class TestIntentServiceWiresResolver:
    def test_resolver_threaded_through_to_runner(self):
        from resonant_client.orchestration.intent_service import IntentService

        resolver = MagicMock(return_value=None)
        svc = IntentService(
            project_path="/tmp/proj",
            backend=MagicMock(),
            all_tools=[],
            specialist_backend_resolver=resolver,
        )
        assert svc.specialist_backend_resolver is resolver

    def test_resolver_default_is_none(self):
        from resonant_client.orchestration.intent_service import IntentService

        svc = IntentService(
            project_path="/tmp/proj",
            backend=MagicMock(),
            all_tools=[],
        )
        # Backwards-compat — services constructed without the resolver
        # behave exactly as before.
        assert svc.specialist_backend_resolver is None
