"""Regression cases from real CLI, browser and transaction build evaluations."""
import json
import socket
import sys
import threading
import urllib.request

import httpx
import pytest

from resonant_client.backends import KimiBackend, EVENT_BACKEND_STATUS
from resonant_client.engine.previews import PreviewManager
from resonant_client.engine.project_memory import ProjectMemory
from resonant_client.engine.session import Session
from resonant_client.engine.tools import execute_tool
from resonant_client.engine.turn_outcomes import current_checks
from resonant_client.gui.runtime import BackendSpec
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_preview_lifetime_ownership_logs_and_stop(tmp_path):
    manager = PreviewManager()
    port = free_port()
    url = f'http://127.0.0.1:{port}'
    (tmp_path / 'index.html').write_text('preview still alive')
    try:
        item = manager.start(tmp_path, [sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'], url)
        assert item['state'] == 'ready'
        # Tool returned; independent HTTP requests and another project do not kill it.
        assert urllib.request.urlopen(url).read() == b'preview still alive'
        other = tmp_path / 'other'; other.mkdir()
        assert manager.list(other) == []
        with pytest.raises(ValueError):
            manager.stop(other, item['id'])
        duplicate = manager.start(tmp_path, item['command'], url)
        assert duplicate['id'] == item['id']
        assert manager.stop(tmp_path, item['id'])['state'] == 'stopped'
        assert manager.stop(tmp_path, item['id'])['state'] == 'stopped'
        assert manager.status(tmp_path, item['id'])['logs']
    finally:
        manager.close()


def test_preview_start_failure_and_cancel_clean_up(tmp_path):
    manager = PreviewManager()
    try:
        url = f'http://127.0.0.1:{free_port()}'
        failed = manager.start(tmp_path, [sys.executable, '-c', 'raise SystemExit(7)'], url)
        assert failed['state'] == 'stopped' and failed['exit_code'] == 7
        cancel = threading.Event(); cancel.set()
        stopped = manager.start(tmp_path, [sys.executable, '-c', 'import time; time.sleep(30)'], url, cancel_event=cancel)
        assert stopped['state'] == 'stopped'
        with pytest.raises(ValueError):
            manager.start(tmp_path, [sys.executable], 'https://example.com:80')
    finally:
        manager.close()


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_kimi_effort_reaches_wire_with_private_progress(effort):
    captured = {}
    def handler(request):
        captured.update(json.loads(request.read()))
        deltas = [{'reasoning_content': 'never display this'}, {'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'file_write', 'arguments': '{"path":"x","content":"ok"}'}}]}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps({'choices': [{'delta': d}]}) + '\n\n' for d in deltas) + 'data: [DONE]\n\n')
    backend = KimiBackend('key', thinking=effort, transport=httpx.MockTransport(handler))
    events = list(backend.stream('build', [], 'system', [], None))
    assert captured['reasoning_effort'] == effort
    statuses = [d for k,d in events if k == EVENT_BACKEND_STATUS and d.get('kind') == 'generation_progress']
    assert [s['phase'] for s in statuses] == ['reasoning', 'generating_code']
    assert 'never display' not in json.dumps(statuses)
    assert backend.capability_profile.reasoning_levels == ('low', 'high', 'max')


def test_kimi_effort_persists_in_backend_spec(monkeypatch):
    monkeypatch.setenv('PRIORITY_TEST_KEY', 'test-key')
    spec = BackendSpec(backend_type='kimi', model='kimi-k3', thinking_mode='low', api_key_source='env', api_key_env='PRIORITY_TEST_KEY')
    restored = BackendSpec.from_dict(spec.to_dict())
    assert restored.create_backend().thinking_mode == 'low'
    with pytest.raises(ValueError):
        KimiBackend('key', thinking='off')


def run_check_scenario(tmp_path, commands):
    target = tmp_path / 'app.py'
    scripts = [[tool_call('file_write', {'path': str(target), 'content': 'value=1\n'}), done()]]
    scripts += [[tool_call(name, args, call_id=f'c{i+2}'), done()] for i,(name,args) in enumerate(commands)]
    scripts.append([text_delta('Implemented; see named check results.'), done()])
    session = Session(backend=StreamingBackend(scripts=scripts), max_steps=20, auto_approve=True)
    session.project_path = str(tmp_path)
    events = list(session.run('implement app.py'))
    return next(e for e in reversed(events) if e['event'] == 'session.end')


def test_unrelated_success_does_not_hide_failed_check(tmp_path):
    failed = {'command': 'python -c "raise SystemExit(1)"', 'requirement': 'reject malformed input'}
    end = run_check_scenario(tmp_path, [('check_run', failed), ('bash', {'command': 'python --version'}), ('check_run', {'command': 'python -c "assert 1 == 1"', 'requirement': 'other behavior'})])
    assert end['outcome'] == 'changed_unverified'
    assert [c['status'] for c in end['evidence']['checks']] == ['failed', 'passed']


def test_named_check_and_edit_freshness(tmp_path):
    check = {'command': 'python -m py_compile app.py', 'requirement': 'Python syntax'}
    end = run_check_scenario(tmp_path, [('check_run', check)])
    assert end['outcome'] == 'changed_verified'
    assert end['evidence']['changed_files'] == ['app.py']
    assert end['evidence']['checks'][0]['files']['app.py']
    end = run_check_scenario(tmp_path, [('check_run', check), ('file_write', {'path': str(tmp_path/'app.py'), 'content': 'value=2\n'})])
    assert end['outcome'] == 'changed_unverified'
    assert end['evidence']['checks'][0]['status'] == 'stale'


def test_plain_shell_never_counts_as_acceptance(tmp_path):
    end = run_check_scenario(tmp_path, [('bash', {'command': 'python --version'})])
    assert end['outcome'] == 'changed_unverified'
    assert end['evidence']['checks'] == []


def test_check_rerun_supersedes_failure():
    records = [dict(command='pytest', requirement='parser', files={'a': '1'}, status=status) for status in ['failed', 'passed']]
    assert current_checks(records, {'a': '1'})[0]['status'] == 'passed'
    assert current_checks(records, {'a': '2'})[0]['status'] == 'stale'


def test_project_memory_scope_edit_delete_and_freshness(tmp_path):
    source = tmp_path/'app.py'; source.write_text('version=1')
    memory = ProjectMemory(tmp_path)
    item = memory.save('Parser supports JSON', kind='fact', source='app.py inspection', sources=['app.py'])
    assert item['confidence'] == 'model assertion'
    assert 'Parser supports JSON' in memory.context('JSON parser')
    assert ProjectMemory(tmp_path/'other').context('JSON parser') == ''
    assert memory.context('astronomy') == ''
    source.write_text('version=2')
    assert memory.list()[0]['stale']
    assert memory.context('JSON parser') == ''
    changed = memory.save('Parser supports JSON and CSV', source='review', sources=['app.py'], memory_id=item['id'], author='user')
    assert changed['confidence'] == 'user supplied'
    assert not memory.list()[0]['stale']
    memory.delete(item['id'])
    assert memory.list() == []
    with pytest.raises(ValueError):
        memory.save('escape', source='outside', sources=['../secret'])


def test_memory_tool_and_missing_source(tmp_path):
    result = execute_tool('memory_save', {'text': 'Use SQLite', 'source': 'user requirement'}, project_path=str(tmp_path))
    assert not result.is_error
    assert result.metadata['memory']['confidence'] == 'model assertion'
    result = execute_tool('memory_save', {'text': 'no source'}, project_path=str(tmp_path))
    assert result.is_error


def test_skill_retrieval_budget_dedup_negative_query_and_suppression(tmp_path, monkeypatch):
    from resonant_client.orchestration.skills import Skill, save_skill
    from resonant_client.orchestration.skill_loader import match_skills_for_query, format_skills_for_prompt
    monkeypatch.setenv('RESONANT_STATE_HOME', str(tmp_path/'state'))
    for identifier in ('sqlite-rollback', 'duplicate-rollback'):
        save_skill(Skill(id=identifier, name='SQLite rollback', description='SQLite rollback transactions', tokens=['sqlite', 'rollback', 'transactions'], scope='project'), project_path=tmp_path)
    matches = match_skills_for_query('sqlite rollback transactions', project_path=tmp_path)
    assert len(matches) == 1
    assert match_skills_for_query('astronomy nebula telescope', project_path=tmp_path) == []
    assert len(format_skills_for_prompt(matches, max_tokens=200)) <= 800
    assert format_skills_for_prompt(matches, max_tokens=1) == ''
    (tmp_path/'.resonant').mkdir(exist_ok=True)
    (tmp_path/'.resonant'/'skill-policy.json').write_text(json.dumps({'suppressed_ids': ['sqlite-rollback', 'duplicate-rollback']}))
    assert match_skills_for_query('sqlite rollback transactions', project_path=tmp_path) == []


def test_pack_catalog_is_bounded_and_loads_body_lazily(tmp_path):
    from resonant_client.engine.capability_packs import CapabilityPackManager
    root = tmp_path/'.resonant'/'packs'/'quality'; root.mkdir(parents=True)
    (root/'resonant-pack.json').write_text(json.dumps({'id':'quality','enabled':True,'trust':'local','skills':['sqlite.md']}))
    (root/'sqlite.md').write_text('description: SQLite transaction rollback\n' + 'full procedure body\n'*2000)
    manager = CapabilityPackManager(tmp_path)
    catalog = manager.skill_context('sqlite rollback', max_tokens=150)
    assert len(catalog) <= 600
    assert 'full procedure body' not in catalog
    assert 'pack:quality:sqlite.md' in catalog
    assert 'full procedure body' in manager.read_skill('pack:quality:sqlite.md')
    assert manager.skill_context('the and with') == ''
    assert manager.skill_context('astronomy nebula') == ''
    with pytest.raises(ValueError):
        manager.read_skill('pack:quality:../../secret')


def test_evaluation_mode_excludes_personal_skills_and_engram(tmp_path, monkeypatch):
    from resonant_client.orchestration.skills import Skill, save_skill
    from resonant_client.orchestration.skill_loader import match_skills_for_query
    from resonant_client.engine.memory import EngramIntegration
    monkeypatch.setenv('RESONANT_STATE_HOME', str(tmp_path/'state'))
    save_skill(Skill(id='personal', name='Personal', description='My private procedure', pinned=True))
    monkeypatch.setenv('RESONANT_EVALUATION_MODE', '1')
    assert match_skills_for_query('procedure') == []
    memory = EngramIntegration(); memory._enabled=True; memory._server_url='http://unused'
    assert not memory.enabled


def test_restoring_kimi_session_uses_saved_effort():
    from resonant_client.gui.app import AppState
    state = AppState.__new__(AppState)
    state.backend = object()
    state.backend_spec = BackendSpec(backend_type='kimi', model='kimi-k3', thinking_mode='max')
    calls = []
    state.create_backend = lambda *args, **kwargs: calls.append(kwargs)
    state.restore_session_runtime('kimi', 'kimi-k3', thinking_mode='low')
    assert calls[0]['thinking_mode'] == 'low'


def test_cancelled_empty_response_does_not_retry():
    class CancelBackend(StreamingBackend):
        def stream(self, **kwargs):
            kwargs['cancel_event'].set()
            return
            yield
    session = Session(backend=CancelBackend())
    events = list(session.run('build a project'))
    assert not any(e.get('kind') == 'empty_response_retry' for e in events)
