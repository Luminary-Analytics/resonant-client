import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from resonant_client.engine.mcp import MCPManager
from resonant_client.gui.runtime import BackendSpec


class _SettingsStub:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        value = self.data.get(section, {})
        if key is None:
            return value
        if isinstance(value, dict):
            return value.get(key, default)
        return default


class _DummyBackend:
    def __init__(self, name="dummy", model="dummy-model", handles_tools=False):
        self.name = name
        self.model = model
        self.handles_tools = handles_tools


def _load_app_module(monkeypatch, cwd: Path):
    monkeypatch.setattr(Path, "home", lambda: cwd)
    monkeypatch.chdir(cwd)
    import resonant_client.gui.app as app_module

    return importlib.reload(app_module)


# v0.4.0 — collapsed multi-backend parametrization to Ollama-only.
# Pre-v0.4.0 this exercised resonant / claude / openai / lmstudio /
# claude-code / codex paths; those are gone. The remaining test
# verifies the Ollama path still wires through correctly.
@pytest.mark.parametrize(
    ("spec", "settings_data", "env", "expected_args", "expected_kwargs"),
    [
        (
            BackendSpec(
                backend_type="ollama",
                url="http://10.0.0.133:11434",
                model="deepseek-v4-flash:cloud",
            ),
            {},
            {},
            ("ollama",),
            {
                "url": "http://10.0.0.133:11434",
                "model": "deepseek-v4-flash:cloud",
                "thinking": None,
            },
        ),
        (
            BackendSpec(
                backend_type="codex",
                model="gpt-5.5",
                cwd="D:/Repos/example",
            ),
            {},
            {},
            ("codex",),
            {
                "model": "gpt-5.5",
                "cwd": "D:/Repos/example",
                "permission_mode": None,
            },
        ),
    ],
)
def test_backend_spec_recreates_expected_backend(monkeypatch, spec, settings_data, env, expected_args, expected_kwargs):
    calls = []

    def fake_create_backend(*args, **kwargs):
        calls.append((args, kwargs))
        return {"args": args, "kwargs": kwargs}

    monkeypatch.setattr("resonant_client.gui.runtime.create_backend", fake_create_backend)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    backend = spec.create_backend(_SettingsStub(settings_data))

    assert backend["args"] == expected_args
    assert backend["kwargs"] == expected_kwargs
    assert calls == [(expected_args, expected_kwargs)]


def test_mcp_manager_routes_longest_matching_server_name():
    manager = MCPManager()
    long_conn = SimpleNamespace(
        connected=True,
        call_tool=lambda name, arguments: {"name": name, "arguments": arguments},
    )
    short_conn = SimpleNamespace(
        connected=True,
        call_tool=lambda name, arguments: {"short": name, "arguments": arguments},
    )
    manager._connections = {
        "my_server": long_conn,
        "my": short_conn,
    }

    result = manager.call_tool("mcp_my_server_do_thing", {"x": 1})

    assert result == {"name": "do_thing", "arguments": {"x": 1}}


def test_app_state_preserves_blank_secret_and_applies_permission_mode(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".resonant").mkdir()

    app_module = _load_app_module(monkeypatch, project)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self: {})

    state = app_module.AppState()
    state.settings.set("api_keys", "openai", "existing-secret")

    masked = state.update_setting_value("api_keys", "openai", "")
    assert state.settings.get("api_keys", "openai") == "existing-secret"
    assert masked["_meta"]["api_keys_present"]["openai"] is True

    masked = state.update_setting_value("api_keys", "openai", "", clear_secret=True)
    assert state.settings.get("api_keys", "openai") == ""
    assert masked["_meta"]["api_keys_present"]["openai"] is False

    state.session = state.build_session(backend=_DummyBackend(), project_path=str(project))
    state.update_setting_value("general", "default_permission_mode", "ask")
    assert state.permission_mode == "ask"
    assert state.session.auto_approve is False


def test_app_state_applies_project_context_and_builds_project_scoped_session(monkeypatch, tmp_path):
    project_one = tmp_path / "one"
    project_two = tmp_path / "two"
    project_one.mkdir()
    project_two.mkdir()
    (project_one / ".resonant").mkdir()
    (project_two / ".resonant").mkdir()
    (project_one / "RESONANT.md").write_text("project one instructions", encoding="utf-8")
    (project_two / "RESONANT.md").write_text("project two instructions", encoding="utf-8")

    app_module = _load_app_module(monkeypatch, project_one)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self: {})

    state = app_module.AppState()
    first_namespace = state.engram._namespace

    state.apply_project_context(str(project_two), refresh_index=True)
    session = state.build_session(backend=_DummyBackend(name="codex", model="gpt-5"), project_path=str(project_two))

    assert os.path.normpath(os.getcwd()) == os.path.normpath(str(project_two))
    assert os.path.normpath(state.project.project_path) == os.path.normpath(str(project_two))
    assert state.codebase_index.project_path == Path(project_two)
    assert state.engram._namespace != first_namespace
    assert "project two instructions" in (session.project_instructions or "")
