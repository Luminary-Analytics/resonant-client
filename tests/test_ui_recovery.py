"""Draft isolation, local endpoint protection, and concise memory recall."""
from pathlib import Path

from starlette.testclient import TestClient

from resonant_client.engine.project_memory import ProjectMemory
from resonant_client.gui.ui_state import ui_state


def test_drafts_survive_reopen_and_are_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    a = {'project': str(tmp_path / 'a'), 'session_id': 'one'}
    b = {**a, 'session_id': 'two'}
    c = {**a, 'project': str(tmp_path / 'b')}
    ui_state({**a, 'text': 'unfinished'}, write=True)
    assert ui_state(a)['text'] == 'unfinished'
    assert ui_state(b)['text'] == ui_state(c)['text'] == ''
    ui_state({**a, 'text': ''}, write=True)
    assert ui_state(a)['text'] == ''
    ui_state({'sidebar': {'desktop': True, 'mobile': False}}, write=True)
    assert ui_state({})['sidebar'] == {'desktop': True, 'mobile': False}


def test_draft_http_survives_different_launch_origins(tmp_path, monkeypatch):
    from resonant_client.gui.app import app
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    data = {'project': str(tmp_path / 'a'), 'session_id': 'one', 'text': 'keep me'}
    with TestClient(app, base_url='http://localhost:9010') as client:
        assert client.post('/api/ui-state', json=data, headers={'Origin': 'http://localhost:9010'}).status_code == 200
        assert client.post('/api/ui-state', json=data, headers={'Origin': 'https://unrelated.example'}).status_code == 403
        assert client.post('/api/ui-state', data=data).status_code == 415
    with TestClient(app, base_url='http://localhost:9011') as client:
        response = client.get('/api/ui-state', params={k: v for k, v in data.items() if k != 'text'})
        assert response.json()['text'] == 'keep me'
        assert response.headers['cache-control'] == 'no-store'


def test_memory_selects_commands_and_limits_recall(tmp_path):
    (tmp_path / 'package.json').write_text('{}')
    memory = ProjectMemory(tmp_path)
    for n in range(10):
        memory.save(f'npm run check{n}', kind='build_command', source='checked locally', sources=['package.json'])
    context = memory.context('test the project')
    assert context.count('\n- ') == 6
    assert len(context) <= 2400
    assert 'model assertion' in context
    assert memory.context('astronomy') == ''
    (tmp_path / 'package.json').write_text('{"changed":true}')
    assert memory.context('test the project') == ''


def test_new_composer_does_not_create_empty_sessions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from resonant_client.gui import app as gui
    from resonant_client.gui import sessions
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sessions, '_is_pytest_temp_path', lambda _: False)
    project = tmp_path / 'project'
    project.mkdir()
    state = gui.AppState()
    state.project.set_project(str(project))
    saved = state.project.create_session()
    saved.title = 'Existing conversation'
    saved.save()
    monkeypatch.setattr(gui, 'state', state)
    state.available_backends = {'test': {}}
    state.backend = SimpleNamespace(name='test', model='test')
    monkeypatch.setattr(state, 'build_session', lambda **kwargs: SimpleNamespace(conversation_history=[]))
    with TestClient(gui.app) as client:
        with client.websocket_connect('/ws') as ws:
            for n in range(3):
                request = {'command': 'clear', 'draft_only': True, 'request_id': str(n)}
                ws.send_json(request)
                response = ws.receive_json()
                assert response['current_session_id'] == ''
                assert len(response['sessions']) == 1
            assert state.session.conversation_history == []
            # First-message persistence still creates the actual session.
            record = state.ensure_persisted_current_session()
            assert record.id != saved.id
            assert len(state.project.list_sessions()) == 2
            state._chat_run_loop.task = SimpleNamespace(done=lambda: False)
            try:
                ws.send_json({'command': 'clear', 'draft_only': True, 'request_id': 'while-running'})
                assert ws.receive_json()['event'] == 'error'
                assert state.project.current_session is record
            finally:
                state._chat_run_loop.task = None


def test_offline_new_composer_detaches_old_session(tmp_path, monkeypatch):
    from resonant_client.gui import app as gui
    from resonant_client.gui import sessions
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sessions, '_is_pytest_temp_path', lambda _: False)
    project = tmp_path / 'project'
    project.mkdir()
    state = gui.AppState()
    state.project.set_project(str(project))
    saved = state.project.create_session()
    state.available_backends = {'test': {}}
    monkeypatch.setattr(gui, 'state', state)
    with TestClient(gui.app) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_json({'command': 'clear', 'draft_only': True})
            response = ws.receive_json()
            assert response['current_session_id'] == ''
            assert response['sessions'][0]['id'] == saved.id
