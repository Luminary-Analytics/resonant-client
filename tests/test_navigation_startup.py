"""Saved work remains accessible while provider discovery is blocked."""
import threading
import time
from pathlib import Path

from tests.gui_access import LocalClient


def test_project_navigation_does_not_wait_for_model_discovery(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(alpha)
    from lumi.gui import app as gui
    from lumi.gui import sessions
    monkeypatch.setattr(sessions, "_is_pytest_temp_path", lambda _: False)
    state = gui.AppState()
    state.project.register_project(str(beta))
    monkeypatch.setattr(gui, "state", state)
    started, release = threading.Event(), threading.Event()

    def delayed_discovery():
        started.set()
        assert release.wait(10), "Test must release discovery"
        state.available_backends = {}

    monkeypatch.setattr(state, "detect_backends", delayed_discovery)
    with LocalClient(gui.app) as client:
        try:
            with client.websocket_connect("/ws") as ws:
                navigation = ws.receive_json()
                assert navigation["event"] == "navigation_ready"
                assert any(p["path"] == str(beta) for p in navigation["recent_projects"])
                ws.send_json({"command": "init"})
                initial = ws.receive_json()
                assert initial["event"] == "init" and initial["runtime_loading"]
                assert started.wait(1)
                ws.send_json({"command": "set_project", "path": str(beta), "project_switch_id": "while-discovering"})
                switched = ws.receive_json()
                assert switched["event"] == "init"
                assert switched["project_switch_id"] == "while-discovering"
                assert Path(switched["cwd"]) == beta
                assert not release.is_set(), "Navigation completed before provider discovery"
                release.set()
                while True:
                    message = ws.receive_json()
                    if message["event"] == "backends_discovered":
                        break
                    assert message["event"] == "ui_notice"
        finally:
            release.set()


def test_empty_provider_result_is_cached(monkeypatch):
    from lumi.gui.app import AppState
    state = AppState.__new__(AppState)
    state.available_backends = {}
    state._last_backend_probe = time.time()
    monkeypatch.setattr(state, "refresh_network_defaults", lambda: (_ for _ in ()).throw(AssertionError("unexpected reprobe")))
    assert state.detect_backends() == {}


def test_project_selection_is_visible_before_runtime_setup(tmp_path, monkeypatch):
    from lumi.gui import app as gui
    from lumi.gui import sessions
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sessions, '_is_pytest_temp_path', lambda _: False)
    state = gui.AppState()
    monkeypatch.setattr(gui, 'state', state)
    state.available_backends = {'test': {}}
    monkeypatch.setattr(state, 'get_init_data', lambda: {
        'event': 'init', 'cwd': state.project.project_path})
    release = threading.Event()
    started = threading.Event()

    def prepare():
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(state, 'ensure_default_runtime_session', prepare)
    target = tmp_path / 'target'
    target.mkdir()
    with LocalClient(gui.app) as client:
        try:
            with client.websocket_connect('/ws') as ws:
                ws.send_json({'command': 'set_project', 'path': str(target), 'project_switch_id': 'next'})
                first = ws.receive_json()
                assert first['runtime_preparing'] is True
                assert Path(first['cwd']) == target
                assert started.wait(1)
                assert not release.is_set()
                # Draft writes use HTTP and must work while this socket waits.
                assert client.post('/api/ui-state', json={
                    'project': str(target), 'text': 'keep typing'}).status_code == 200
                release.set()
                assert ws.receive_json()['project_switch_id'] == 'next'
        finally:
            release.set()


def test_navigation_catalog_tracks_new_renamed_and_deleted_sessions(tmp_path, monkeypatch):
    from lumi.gui import sessions
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sessions, "_is_pytest_temp_path", lambda _: False)
    project = tmp_path / "project"
    project.mkdir()
    manager = sessions.ProjectManager(str(project))
    record = manager.create_session()
    assert manager.navigation_snapshot()["sessions"][0]["id"] == record.id
    record.title = "Updated title"
    record.save()
    assert manager.navigation_snapshot()["sessions"][0]["title"] == "Updated title"
    manager.delete_session(record.id)
    assert manager.navigation_snapshot()["sessions"] == []
