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
