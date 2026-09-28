"""A new tester's first run: the default mode, refusals, connections and notices.

Found running the packaged app the way an alpha tester would, on a new
Windows computer: a refused message left the composer "running", unattended
work ran in Full-auto whatever mode the conversation was in, Ollama could only
be set up from the welcome screen, ChatGPT sign-in said nothing new without
the Codex CLI, and the diagnostics file pointed at GitHub issues.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lumi import policy as lumi_policy
from lumi.gui import swarming, ws_commands
from lumi.gui.app import AppState
from lumi.gui.settings import DEFAULT_PERMISSION_MODE, SettingsManager
from tests.test_ws_command_registry import _run, _StubWS


def _without_full_auto() -> None:
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme",
         "permissions": {"allowed_modes": ["ask", "auto-edit"]}}, source="test"))


def _ctx(state, msg):
    return ws_commands.CommandContext(ws=_StubWS(), state=state, msg=msg, runs=None)


# ── New installs start in Auto-edit ────────────────────────────────────────


def test_a_new_install_starts_in_auto_edit_and_saves_it(tmp_path):
    path = tmp_path / "settings.json"
    settings = SettingsManager(path)
    assert DEFAULT_PERMISSION_MODE == "auto-edit"
    assert settings.get("general", "default_permission_mode") == "auto-edit"
    # Saved like every default, so a later default can't change this install.
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["default_permission_mode"] == "auto-edit"
    assert SettingsManager(path).get("general", "default_permission_mode") == "auto-edit"


@pytest.mark.parametrize("saved", ["bypass", "ask", "auto-edit", "plan"])
def test_an_existing_settings_file_keeps_its_mode(tmp_path, saved):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"general": {"default_permission_mode": saved, "theme": "light"}}), encoding="utf-8")
    assert SettingsManager(path).get("general", "default_permission_mode") == saved


@pytest.mark.parametrize("general", [{"theme": "light"}, {"default_permission_mode": ""}, None],
                         ids=["no key", "empty", "no general section"])
def test_an_existing_file_without_a_mode_keeps_the_earlier_full_auto(tmp_path, general):
    # Every earlier first launch wrote Full-auto into the file; one without a
    # mode was written by hand or a tool, and ran in Full-auto until now.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({} if general is None else {"general": general}), encoding="utf-8")
    assert SettingsManager(path).get("general", "default_permission_mode") == "bypass"
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["default_permission_mode"] == "bypass"


def test_an_unreadable_settings_file_gets_the_new_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert SettingsManager(path).get("general", "default_permission_mode") == "auto-edit"


def test_an_empty_mode_means_a_new_installs_default():
    assert AppState.normalize_permission_mode("") == "auto-edit"
    assert AppState.normalize_permission_mode("  ") == "auto-edit"
    assert AppState.normalize_permission_mode("sideways") == "ask"  # unknown: fails closed


# ── A refused message ends the running state ───────────────────────────────


def test_a_refused_turn_says_so_and_names_a_queued_follow_up():
    assert ws_commands.refused_turn({"text": "hi"}, "No model is running.") == {
        "event": "error", "message": "No model is running.", "refused": True}
    assert ws_commands.refused_turn({"message_id": "m-2"}, "Busy", code="oversight_notice") == {
        "event": "error", "message": "Busy", "refused": True, "code": "oversight_notice", "message_id": "m-2"}


def test_a_message_without_a_running_model_is_refused_as_such():
    state = SimpleNamespace(session=None, runtime_unavailable_reason=lambda: "No model is running: choose one.")
    sent = _run(ws_commands.HANDLERS["message"], _ctx(state, {"command": "message", "text": "Fix the bug"}))
    assert sent == [{"event": "error", "message": "No model is running: choose one.", "refused": True}]


# ── Unattended work needs Full-auto ────────────────────────────────────────


def _app_state(mode: str) -> AppState:
    state = AppState.__new__(AppState)  # only the mode matters here
    state.permission_mode = mode
    return state


@pytest.mark.parametrize("work", ["plan", "roadmap", "autonomous", "autonomous_resume", "team", "team_continue"])
def test_unattended_work_outside_full_auto_is_refused_with_the_switch(work):
    needed = _app_state("auto-edit").full_auto_needed(work)
    assert needed["code"] == "needs_full_auto" and needed["can_switch"] is True
    assert "Full-auto" in needed["message"] and "This conversation is in Auto-edit." in needed["message"]
    assert _app_state("ask").full_auto_needed(work)["message"].count("in Ask.") == 1
    assert _app_state("bypass").full_auto_needed(work) is None


def test_where_the_organization_doesnt_allow_full_auto_its_own_refusal_applies():
    _without_full_auto()
    # policy.full_auto_refusal (and the team's mode_refusal) say it in the organization's words.
    assert _app_state("auto-edit").full_auto_needed("plan") is None


class _Intents:
    def __init__(self):
        self.started = []

    def start_intent(self, text, **kwargs):
        self.started.append(text)
        return "intent-1"

    def route_to(self, *args):
        pass


def _refusing_state(**extra):
    needed = _app_state("auto-edit")
    return SimpleNamespace(full_auto_needed=needed.full_auto_needed, **extra)


def test_a_plan_outside_full_auto_is_refused_before_it_starts():
    intents = _Intents()
    state = _refusing_state(backend=object(), get_intent_service=lambda on_event=None: intents)
    [event] = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, {"command": "intent_start", "text": "Add a counter"}))
    # The /plan card matches the prefix (app.js _failStartingPlan); detail is the explanation alone.
    assert event["event"] == "error" and event["message"].startswith("intent_start failed: A plan runs its steps")
    assert event["detail"].startswith("A plan runs its steps") and event["code"] == "needs_full_auto"
    assert event["can_switch"] is True and intents.started == []


def test_a_plan_in_full_auto_starts():
    intents = _Intents()
    state = SimpleNamespace(full_auto_needed=_app_state("bypass").full_auto_needed, backend=object(),
                            get_intent_service=lambda on_event=None: intents)
    sent = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, {"command": "intent_start", "text": "Add a counter"}))
    assert sent == [{"event": "intent.accepted", "intent_id": "intent-1", "text": "Add a counter"}]


def test_build_this_roadmap_outside_full_auto_leaves_the_mission_in_drafting():
    advanced = []
    mission = SimpleNamespace(id="s1", mission_state={"phase": "drafting"},
                              advance_mission_phase=lambda *args, **kwargs: advanced.append(args), save=lambda: None)
    intents = _Intents()
    state = _refusing_state(project=SimpleNamespace(project_path="/p", current_session=mission),
                            get_intent_service=lambda on_event=None: intents)
    [event] = _run(ws_commands.HANDLERS["mission_dispatch_roadmap"], _ctx(state, {"spec_markdown": "## Final spec"}))
    # source puts the Build button back; the page's notice offers the switch.
    assert event["source"] == "mission_dispatch" and event["code"] == "needs_full_auto"
    assert event["message"].startswith("A roadmap is built in Full-auto")
    assert advanced == [] and intents.started == []


def test_an_autonomous_session_outside_full_auto_neither_starts_nor_resumes(monkeypatch):
    from lumi.gui import app as gui_app
    from tests.gui_access import LocalClient

    started = []
    monkeypatch.setattr(gui_app, "_start_autonomous_mission", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(gui_app, "_resume_autonomous_mission", lambda **kwargs: started.append(kwargs))
    mission = SimpleNamespace(id="s1", title="Counter", mission_state={"phase": "drafting"})
    monkeypatch.setattr(gui_app.state, "permission_mode", "auto-edit")
    monkeypatch.setattr(gui_app.state.project, "current_session", mission)
    with LocalClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "mission_dispatch_autonomous", "spec_markdown": "## Final spec",
                                 "time_budget": "4h"})
            dispatch = websocket.receive_json()
            # Refused before the switch to the interrupted session's conversation.
            websocket.send_json({"command": "autonomous_mission_resume", "intent_id": "auto-1", "session_id": "s-other"})
            resume = websocket.receive_json()
    assert dispatch["source"] == "mission_dispatch" and dispatch["code"] == "needs_full_auto"
    assert dispatch["message"].startswith("An autonomous session runs in Full-auto")
    assert (resume["source"], resume["intent_id"], resume["session_id"]) == ("autonomous_resume", "auto-1", "s-other")
    assert resume["code"] == "needs_full_auto" and resume["can_switch"] is True
    assert started == [] and gui_app.state.project.current_session is mission


class _Manager:
    def __init__(self, orchestrated):
        self._orchestrated = orchestrated

    def orchestrated(self, capture, run_id):
        return self._orchestrated


@pytest.mark.parametrize(("message", "orchestrated", "work"), [
    ({"action": "start", "autonomy": {"rounds": 2}}, False, "team"),
    ({"action": "start", "autonomy": {"rounds": 2, "apply": True}}, False, "team"),
    ({"action": "continue_recovered", "run_id": "run-1"}, True, "team_continue"),
    ({"action": "start"}, False, None),
    ({"action": "continue_recovered", "run_id": "run-1"}, False, None),
    ({"action": "resume", "run_id": "run-1"}, True, None),
], ids=["orchestrated", "applies changes", "continue orchestrated", "reviewed by the owner",
        "continue reviewed", "resume a running team"])
def test_a_team_the_orchestrator_runs_needs_full_auto(message, orchestrated, work):
    asked = []

    def full_auto_needed(kind):
        asked.append(kind)
        return {"message": "needs it", "code": "needs_full_auto", "can_switch": True}

    state = SimpleNamespace(full_auto_needed=full_auto_needed)
    needed = swarming._full_auto_needed(state, _Manager(orchestrated), object(), message)
    assert asked == ([work] if work else [])
    assert (needed is not None) == bool(work)


def test_the_runtime_knows_which_retained_teams_the_orchestrator_runs(tmp_path):
    from lumi.engine.swarming import Scope
    from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta
    from tests.test_gui_swarming import enable, start_request
    from tests.test_swarm_autopilot import start as start_orchestrated

    def factory(spec):
        backend = StreamingBackend(events=[text_delta("Inspected."), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        return backend

    def runtime(name):
        # One team per project: each kind gets its own.
        workspace = tmp_path / name
        workspace.mkdir()
        service = SwarmRuntime(SettingsManager(tmp_path / f"{name}.json"), backend_factory=factory,
                               state_root=lambda _: tmp_path / f"{name}-state")
        capture = CapturedSession(Scope.personal("fixture-owner", name, "session"),
                                  str(workspace), BackendSpec("ollama", "chosen"), "")
        enable(service, capture)
        return service, capture

    reviewed_service, reviewed_capture = runtime("reviewed")
    orchestrated_service, orchestrated_capture = runtime("orchestrated")
    try:
        reviewed = reviewed_service.operate(reviewed_capture, start_request())["run"]["run"]["id"]
        orchestrated = start_orchestrated(orchestrated_service, orchestrated_capture, rounds=1)
        assert reviewed_service.orchestrated(reviewed_capture, reviewed) is False
        assert orchestrated_service.orchestrated(orchestrated_capture, orchestrated) is True
        assert orchestrated_service.orchestrated(orchestrated_capture, "no-such-run") is False
    finally:
        reviewed_service.close()
        orchestrated_service.close()


# ── Settings > Connections: Ollama ─────────────────────────────────────────


class _Ollama(BaseHTTPRequestHandler):
    models: list[str] = []
    status = 200

    def do_GET(self):  # noqa: N802 (http.server's name)
        body = json.dumps({"models": [{"name": name} for name in self.models]}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_ollama_address_is_checked():
    assert ws_commands.ollama_address("") == ""
    assert ws_commands.ollama_address(" 10.0.0.5:11434/ ") == "http://10.0.0.5:11434"
    assert ws_commands.ollama_address("https://ollama.example.com") == "https://ollama.example.com"
    for bad in ["ftp://host", "http://", "http://user:pw@host:11434", "http://host:99999"]:
        with pytest.raises(ValueError):
            ws_commands.ollama_address(bad)
    with pytest.raises(ValueError, match="Enter Ollama"):
        ws_commands._socket_setting_value("network", "ollama_url", "ftp://host")


def test_probing_ollama_reports_its_chat_models_or_why_not(ollama, monkeypatch):
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b", "nomic-embed-text"])
    assert ws_commands.probe_ollama(url) == {"status": "ready", "url": url, "models": ["qwen3-coder:30b"], "model_count": 1}
    monkeypatch.setattr(_Ollama, "models", ["nomic-embed-text"])
    empty = ws_commands.probe_ollama(url)
    assert empty["status"] == "empty" and "ollama pull" in empty["error"]
    monkeypatch.setattr(_Ollama, "status", 404)
    assert "HTTP 404" in ws_commands.probe_ollama(url)["error"]
    closed = ws_commands.probe_ollama("http://127.0.0.1:9")
    assert closed["status"] == "unreachable" and "Nothing answered as Ollama" in closed["error"]


class _OllamaState:
    """The AppState parts Settings > Connections' Ollama card uses."""

    def __init__(self, tmp_path, url):
        self.settings = SettingsManager(tmp_path / "settings.json")
        self.ollama_url = url
        self.backend = None
        self.detected = 0
        self.started = 0

    def update_setting_value(self, section, key, value):
        self.settings.set(section, key, value)
        self.ollama_url = value or self.ollama_url
        return {"network": {"ollama_url": value}}

    def detect_backends(self, force=False):
        self.detected += 1

    def ensure_default_runtime_session(self):
        self.started += 1
        self.backend = object()

    def get_init_data(self, refresh_only=False):
        return {"event": "init", "refresh_only": refresh_only}


def _connection(state, action, url=""):
    msg = {"command": "provider_connection", "provider": "ollama", "action": action, "url": url}
    return _run(ws_commands.HANDLERS["provider_connection"], _ctx(state, msg))


def test_testing_an_ollama_address_saves_nothing(tmp_path, ollama, monkeypatch):
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    [event] = _connection(state, "test", url)
    assert event["data"] == {"status": "ready", "url": url, "models": ["qwen3-coder:30b"], "model_count": 1, "saved": False}
    assert state.settings.get("network", "ollama_url") == "" and state.detected == 0 and state.started == 0


def test_saving_an_ollama_address_stores_it_and_starts_a_model(tmp_path, ollama, monkeypatch):
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    settings_event, connection, init = _connection(state, "save", url + "/")
    assert settings_event == {"event": "settings", "data": {"network": {"ollama_url": url}}}
    assert connection["data"]["status"] == "ready" and connection["data"]["saved"] is True
    assert init == {"event": "init", "refresh_only": True}
    assert state.settings.get("network", "ollama_url") == url
    assert state.detected == 1 and state.started == 1


def test_a_bad_ollama_address_is_refused_and_not_saved(tmp_path):
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    [event] = _connection(state, "save", "ftp://nowhere")
    assert "Enter Ollama" in event["data"]["error"]
    assert state.settings.get("network", "ollama_url") == ""


def test_a_policy_that_locks_ollamas_address_keeps_it(tmp_path):
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme", "settings": {"network.ollama_url": "http://gpu:11434"}},
        source="test"))
    state = _OllamaState(tmp_path, "http://gpu:11434")
    [event] = _connection(state, "save", "http://127.0.0.1:11434")
    assert "managed by Acme" in event["data"]["error"]


# ── ChatGPT sign-in without the Codex CLI ──────────────────────────────────


def test_chatgpt_sign_in_without_the_codex_cli_says_what_to_install(monkeypatch):
    from lumi import codex_account

    monkeypatch.setattr(codex_account, "resolve_codex_cli_path", lambda: "")
    monkeypatch.setattr(codex_account.shutil, "which", lambda name: None)
    account = codex_account.CodexAccount()
    with pytest.raises(codex_account.CodexCliMissing, match="needs Node.js") as missing:
        account.login()
    assert "npm install -g @openai/codex" in str(missing.value)
    monkeypatch.setattr(codex_account.shutil, "which", lambda name: f"C:/tools/{name}.exe")
    assert "needs Node.js" not in codex_account.missing_cli_message()

    monkeypatch.setattr(codex_account, "codex_account", account)
    state = SimpleNamespace(codex_connection=None, available_backends={})
    msg = {"command": "provider_connection", "provider": "codex", "action": "login"}
    [event] = _run(ws_commands.HANDLERS["provider_connection"],
                   ws_commands.CommandContext(ws=_StubWS(), state=state, msg=msg, runs=SimpleNamespace(busy=False)))
    assert event["data"]["missing_cli"] is True and "Codex CLI" in event["data"]["error"]


# ── Diagnostics ────────────────────────────────────────────────────────────


def test_show_in_folder_reveals_only_the_saved_diagnostics(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(ws_commands.subprocess, "Popen", lambda args, **kwargs: opened.append(args))
    state = SimpleNamespace()
    [event] = _run(ws_commands.HANDLERS["reveal_diagnostics"], _ctx(state, {"path": str(tmp_path / "other.zip")}))
    assert "isn't there anymore" in event["message"] and opened == []

    saved = tmp_path / "lumi-diagnostics-1.zip"
    saved.write_bytes(b"PK")
    state._last_diagnostics_zip = str(saved)
    # The page names no path; one it sends anyway is ignored.
    [event] = _run(ws_commands.HANDLERS["reveal_diagnostics"], _ctx(state, {"path": "C:/Windows/System32"}))
    assert event["message"] == "Showing lumi-diagnostics-1.zip in its folder"
    expected = {"win32": f'explorer /select,"{saved}"', "darwin": ["open", "-R", str(saved)]}
    assert opened == [expected.get(sys.platform, ["xdg-open", str(tmp_path)])]
