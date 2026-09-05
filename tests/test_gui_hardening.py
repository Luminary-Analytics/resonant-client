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
        (
            BackendSpec(
                backend_type="kimi",
                model="kimi-k3",
                base_url="https://api.moonshot.ai/v1",
                api_key_source="settings",
                api_key_setting="kimi",
            ),
            {"api_keys": {"kimi": "stored-key"}},
            {},
            ("kimi",),
            {
                "model": "kimi-k3",
                "api_key": "stored-key",
                "base_url": "https://api.moonshot.ai/v1",
                "thinking": None,
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
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})

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


def test_app_state_applies_permission_mode_to_cli_backend(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".resonant").mkdir()

    app_module = _load_app_module(monkeypatch, project)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})
    state = app_module.AppState()
    backend = _DummyBackend(name="codex", model="gpt-5.5")
    observed = []
    backend.configure_permission_mode = observed.append
    state.session = state.build_session(backend=backend, project_path=str(project))

    state.apply_permission_mode("plan")

    assert observed == ["plan"]
    assert state.permission_mode == "plan"


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
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})

    state = app_module.AppState()
    first_namespace = state.engram._namespace

    state.apply_project_context(str(project_two), refresh_index=True)
    session = state.build_session(backend=_DummyBackend(name="codex", model="gpt-5"), project_path=str(project_two))

    assert os.path.normpath(os.getcwd()) == os.path.normpath(str(project_two))
    assert os.path.normpath(state.project.project_path) == os.path.normpath(str(project_two))
    assert state.codebase_index.project_path == Path(project_two)
    assert state.engram._namespace != first_namespace
    assert "project two instructions" in (session.project_instructions or "")


def test_saved_http_session_reuses_compatible_provider_client(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".resonant").mkdir()

    app_module = _load_app_module(monkeypatch, project)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})
    state = app_module.AppState()
    backend = _DummyBackend(name="kimi", model="kimi-k3")
    state.backend = backend
    state.backend_spec = BackendSpec(backend_type="kimi", model="kimi-k3")
    monkeypatch.setattr(
        state,
        "create_backend",
        lambda *_args, **_kwargs: pytest.fail("compatible provider should be reused"),
    )

    restored = state.restore_session_runtime("kimi", "kimi-k3")

    assert restored is backend
    assert state.session.backend is backend
    assert state.session.project_path == os.path.normpath(str(project))


def test_worker_errors_are_not_promoted_to_duplicate_turn_failures():
    repo_root = Path(__file__).parent.parent
    source = (repo_root / "resonant_client/gui/static/app.js").read_text(encoding="utf-8")

    assert "if (event._subagent) this.handleSubagentError(event);" in source
    assert "if (!this._activeTask)" in source
    assert "if (this.isReplaying) return;" in source
    assert "this._activeTask?.activityEl" in source


def test_websocket_disconnect_detaches_viewer_without_cancelling_run():
    import inspect

    from resonant_client.gui import app as gui_app

    endpoint_source = inspect.getsource(gui_app.websocket_endpoint)
    stream_source = inspect.getsource(gui_app._run_session_streaming)

    assert 'getattr(state, "_chat_run_loop", None)' in endpoint_source
    assert "runs.attach(ws, _process_chat_message)" in endpoint_source
    assert "runs.detach(ws)" in endpoint_source
    finally_body = endpoint_source[endpoint_source.rindex("    finally:") :]
    assert "state.session.cancel()" not in finally_body
    assert "runs.task.cancel()" not in finally_body
    assert "session.cancel()" not in stream_source


def test_streaming_persists_to_the_record_that_started_the_run():
    """Changing the selected sidebar session must not redirect a live ledger."""
    import inspect

    from resonant_client.gui import app as gui_app

    source = inspect.getsource(gui_app._run_session_streaming)

    assert source.count(
        'active_record = getattr(state.project, "current_session", None)'
    ) == 1
    assert "lambda record=active_record" in source


def test_project_switch_does_not_reprobe_global_providers():
    repo_root = Path(__file__).parent.parent
    from resonant_client.gui import ws_commands
    import inspect

    source = inspect.getsource(ws_commands.HANDLERS["set_project"])

    assert "state.detect_backends" not in source
    assert "state.backend = None" not in source


def test_project_rail_puts_add_first_and_has_no_permanent_brand():
    repo_root = Path(__file__).parent.parent
    template = (repo_root / "resonant_client/gui/templates/index.html").read_text(
        encoding="utf-8",
    )

    add_position = template.index('id="rail-open-project"')
    projects_position = template.index('id="rail-projects"')

    assert add_position < projects_position
    assert 'aria-label="Add project"' in template
    assert 'class="rail-avatar"' not in template


def test_set_project_echoes_client_switch_id(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from resonant_client.gui import app as gui_app

    target = tmp_path / "target"
    target.mkdir()
    original_project = str(tmp_path / "original")

    monkeypatch.setattr(gui_app.state.project, "project_path", original_project)
    monkeypatch.setattr(gui_app.state.project, "current_session", None)
    monkeypatch.setattr(gui_app.state, "available_backends", {"dummy": {}})
    monkeypatch.setattr(gui_app.state, "backend", object())
    monkeypatch.setattr(gui_app.state, "backend_spec", object())
    monkeypatch.setattr(gui_app.state, "session", None)
    monkeypatch.setattr(gui_app.state, "codebase_index", object())
    monkeypatch.setattr(gui_app.state, "ensure_project_path", lambda path: str(target))
    monkeypatch.setattr(
        gui_app.state,
        "apply_project_context",
        lambda path, refresh_index=True: setattr(gui_app.state.project, "project_path", path),
    )
    monkeypatch.setattr(gui_app.state, "detect_backends", lambda force=False: None)
    monkeypatch.setattr(gui_app.state, "ensure_default_runtime_session", lambda: None)
    monkeypatch.setattr(
        gui_app.state,
        "get_init_data",
        lambda refresh_only=False: {
            "event": "init",
            "refresh_only": refresh_only,
            "cwd": gui_app.state.project.project_path.replace("\\", "/"),
        },
    )

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({
                "command": "set_project",
                "path": str(target),
                "project_switch_id": "switch-latest",
            })
            init = websocket.receive_json()
            notice = websocket.receive_json()

    assert init["event"] == "init"
    assert init["project_switch_id"] == "switch-latest"
    assert init["cwd"] == str(target).replace("\\", "/")
    assert notice["event"] == "ui_notice"
    assert notice["project_switch_id"] == "switch-latest"


def test_duplicate_new_session_request_is_idempotent(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from resonant_client.gui import app as gui_app

    class ProjectStub:
        project_path = str(tmp_path)
        current_session = None

        def __init__(self):
            self.created = 0

        def create_session(self, **kwargs):
            self.created += 1
            self.current_session = SimpleNamespace(id=f"session-{self.created}")
            return self.current_session

        def list_sessions(self):
            if not self.current_session:
                return []
            return [{"id": self.current_session.id}]

    project = ProjectStub()
    monkeypatch.setattr(gui_app.state, "project", project)
    monkeypatch.setattr(gui_app.state, "available_backends", {"exo": {"models": ["glm"]}})
    monkeypatch.setattr(gui_app.state, "backend", _DummyBackend(name="exo", model="glm"))
    monkeypatch.setattr(gui_app.state, "backend_spec", SimpleNamespace())
    monkeypatch.setattr(gui_app.state, "session", SimpleNamespace())
    monkeypatch.setattr(gui_app.state, "codebase_index", object())
    monkeypatch.setattr(gui_app.state, "build_session", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(gui_app.state.costs, "reset_session", lambda: None)

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            request = {
                "command": "clear",
                "session_role": "generator",
                "request_id": "new-session-intent-1",
            }
            websocket.send_json(request)
            first = websocket.receive_json()
            websocket.send_json(request)
            duplicate = websocket.receive_json()

    assert project.created == 1
    assert first == duplicate
    assert first["event"] == "session_cleared"
    assert first["request_id"] == "new-session-intent-1"
    assert first["current_session_id"] == "session-1"


def test_backend_selection_does_not_persist_an_empty_session():
    # Reads the registered handler rather than a line range in app.py, so the
    # assertion survives the command changing files.
    import inspect

    from resonant_client.gui import ws_commands

    body = inspect.getsource(ws_commands.HANDLERS["select_backend"])

    assert "create_session(" not in body
    assert "ctx.state.project.current_session = None" in body
    assert "ctx.state.project.current_session = previous_record" in body
