"""Tests for v0.5.7a1 — `gui/app.py::AppState._resolve_default_model`.

The helper picks the right Ollama model when a session is being
constructed without an explicit model argument. Linux-bridge field-
observation #4: project switches were landing on the first model
Ollama returned (typically `deepseek-v4-flash`) instead of the user's
pinned `default_model` setting.

Coverage:
- Empty model list → "" (degenerate input, no settings access)
- No configured default → first detected model (legacy behavior)
- Configured default present in detected models → exact match wins
- Configured default present case-insensitively → canonical form returned
- Configured default missing from detected models → fall through to first
- Settings access failure → first detected model (no crash)
"""
from __future__ import annotations

from unittest.mock import MagicMock

from resonant_client.gui.app import AppState


def _make_state_with_settings_get(get_returns):
    """Build a stub AppState shell that exposes only `.settings.get`
    so we can call _resolve_default_model in isolation. Avoids the
    full AppState() construction which spins up a SettingsManager,
    ProjectManager, EngramIntegration, etc."""
    state = AppState.__new__(AppState)  # bypass __init__
    state.settings = MagicMock()
    state.settings.get = MagicMock(return_value=get_returns)
    return state


# ── Tests ───────────────────────────────────────────────────────────────


class TestResolveDefaultModelEmptyAndDegenerate:
    def test_empty_models_returns_empty_string(self):
        state = _make_state_with_settings_get("deepseek-v4-pro:cloud")
        assert state._resolve_default_model([]) == ""
        # Settings shouldn't even be read for the degenerate case.
        state.settings.get.assert_not_called()

    def test_settings_failure_falls_back_to_first(self):
        state = AppState.__new__(AppState)
        state.settings = MagicMock()
        state.settings.get = MagicMock(side_effect=RuntimeError("settings unreachable"))
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        # Must not raise; fall through to legacy behavior.
        assert state._resolve_default_model(models) == "deepseek-v4-flash"


class TestResolveDefaultModelLegacyBehavior:
    def test_no_configured_default_returns_first(self):
        state = _make_state_with_settings_get("")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-flash"

    def test_whitespace_only_default_returns_first(self):
        state = _make_state_with_settings_get("   ")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-flash"

    def test_none_configured_default_returns_first(self):
        state = _make_state_with_settings_get(None)
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-flash"


class TestResolveDefaultModelHonorsConfiguration:
    def test_exact_match_wins(self):
        # The bug: previous behavior was `models[0]` always, so
        # `deepseek-v4-flash` won even though the user pinned
        # `deepseek-v4-pro:cloud` as default. After the fix the
        # configured value wins.
        state = _make_state_with_settings_get("deepseek-v4-pro:cloud")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-pro:cloud"

    def test_case_insensitive_lookup(self):
        # Real-world: settings.json may have any casing because the
        # user typed it manually. Ollama's tags endpoint returns one
        # canonical form. We lowercase-compare and return Ollama's
        # canonical form so the spec carries the exact tag.
        state = _make_state_with_settings_get("DeepSeek-V4-Pro:Cloud")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-pro:cloud"

    def test_configured_at_arbitrary_position_in_list(self):
        # Honor the configured value regardless of where it sits in
        # the detected models order.
        state = _make_state_with_settings_get("kimi-k2.5:cloud")
        models = [
            "deepseek-v4-flash:cloud",
            "deepseek-v3.2:cloud",
            "minimax-m2.7:cloud",
            "kimi-k2.5:cloud",
            "glm-5.1:cloud",
        ]
        assert state._resolve_default_model(models) == "kimi-k2.5:cloud"


class TestResolveDefaultModelFallback:
    def test_configured_missing_from_models_falls_back_to_first(self):
        # User pinned a model that isn't currently pulled and isn't
        # in CLOUD_MODELS either (e.g. deprecated tag). Fall through
        # to first available rather than crashing — mirrors the
        # pre-v0.5.7 silent-fallback contract so this fix doesn't
        # introduce a new failure mode.
        state = _make_state_with_settings_get("non-existent-model:cloud")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-flash"

    def test_configured_with_trailing_whitespace_still_matches(self):
        # Whitespace in settings.json values is common (manual edits
        # leave trailing spaces). Strip before compare.
        state = _make_state_with_settings_get("  deepseek-v4-pro:cloud  ")
        models = ["deepseek-v4-flash", "deepseek-v4-pro:cloud"]
        assert state._resolve_default_model(models) == "deepseek-v4-pro:cloud"


class TestDefaultChatBackendChoice:
    def _state(self, *, default_backend="", default_model=""):
        state = AppState.__new__(AppState)
        state.available_backends = {
            "ollama": {"models": ["glm-5.2:cloud"]},
            "kimi": {"models": ["kimi-k3"]},
            "codex": {"models": ["gpt-5.5", "gpt-5.4-mini"]},
        }
        state.settings = MagicMock()

        def _get(section, key=None, default=None):
            if section == "general" and key == "default_backend":
                return default_backend
            if section == "general" and key == "default_model":
                return default_model
            return default

        state.settings.get = MagicMock(side_effect=_get)
        return state

    def test_honors_codex_default_backend(self):
        state = self._state(default_backend="codex", default_model="gpt-5.5")

        assert state.default_chat_backend_choice() == ("codex", "gpt-5.5")

    def test_honors_kimi_default_backend(self):
        state = self._state(default_backend="kimi", default_model="kimi-k3")

        assert state.default_chat_backend_choice() == ("kimi", "kimi-k3")

    def test_auto_uses_codex_when_ollama_unavailable(self):
        state = self._state(default_backend="", default_model="")
        state.available_backends = {"codex": {"models": ["gpt-5.5"]}}

        assert state.default_chat_backend_choice() == ("codex", "gpt-5.5")


class TestProjectChatBackendChoice:
    def test_keeps_latest_project_model_when_provider_catalog_temporarily_loses_it(self):
        state = AppState.__new__(AppState)
        state.project = MagicMock()
        state.project.current_session = None
        state.project.list_sessions.return_value = [{
            "backend_type": "exo",
            "model": "mlx-community/GLM-5.2-mxfp4",
            "updated_at": 20,
        }]
        state.default_chat_backend_choice = MagicMock(
            return_value=("exo", "mlx-community/Llama-3.2-3B-Instruct-4bit")
        )

        assert state.project_chat_backend_choice() == (
            "exo",
            "mlx-community/GLM-5.2-mxfp4",
        )
        state.default_chat_backend_choice.assert_not_called()

    def test_falls_back_to_global_default_for_project_without_history(self):
        state = AppState.__new__(AppState)
        state.project = MagicMock()
        state.project.current_session = None
        state.project.list_sessions.return_value = []
        state.default_chat_backend_choice = MagicMock(return_value=("codex", "gpt-5.5"))

        assert state.project_chat_backend_choice() == ("codex", "gpt-5.5")
