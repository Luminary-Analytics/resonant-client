import json

import pytest

from resonant_client.codex_events import CodexEvents, check_command
from resonant_client.engine.session import Session
from tests.streaming_stub import StreamingBackend, error


def item(kind, item_type, item_id='one', **fields):
    return {'type': f'item.{kind}', 'item': {'id': item_id, 'type': item_type, **fields}}


def test_messages_stream_once_with_separate_paragraphs():
    parser = CodexEvents('gpt-6-astra')
    result = []
    for event in [item('updated', 'agent_message', text='Working'),
                  item('completed', 'agent_message', text='Working now.'),
                  item('completed', 'agent_message', text='Working now.'),
                  item('completed', 'agent_message', 'two', text='Done.')]:
        result.extend(parser.translate(event))
    assert ''.join(data['delta'] for _, data in result) == 'Working now.\n\nDone.'


@pytest.mark.parametrize('command,expected', [
    ('npm.cmd test', True), ('python -m pytest -q', True),
    ('"C:\\Windows\\powershell.exe" -Command \'npm.cmd test\'', True),
    ('pwsh -Command "$env:PYTHONDONTWRITEBYTECODE = \'1\'; python -m pytest -q"', True),
    ('node --check app.js', True), ('ruff check .', True),
    ('echo "pytest"', False), ('pytest; echo ok', False),
    ('npm test || true', False), ('pytest --collect-only', False),
    ('pytest --version', False), ('ruff format .', False), ('git status', False),
])
def test_check_evidence_is_conservative(command, expected):
    assert check_command(command) is expected


def test_failed_changes_are_not_successful_edits():
    events = CodexEvents('astra').translate(item('completed', 'file_change',
        changes=[{'path': 'app.py', 'kind': 'update'}], status='failed'))
    assert events[0][1]['stage'] == 'started'
    assert events[-1][1]['is_error']
    assert events[-1][1]['changed_files'] == []


class CliFixture:
    name = 'codex'
    model = 'gpt-6-astra'
    handles_tools = True

    def __init__(self, root, *, fail_edit=False, stale=False):
        self.cwd = str(root)
        self.root = root
        self.fail_edit = fail_edit
        self.stale = stale

    def stream(self, **kwargs):
        parser = CodexEvents(self.model)
        yield from parser.translate(item('completed', 'agent_message', 'intro', text="I'll implement it."))
        (self.root / 'app.py').write_text('x = 1\n')
        yield from parser.translate(item('completed', 'file_change', 'edit',
            changes=[{'path': str(self.root / 'app.py'), 'kind': 'add'}],
            status='failed' if self.fail_edit else 'completed'))
        yield from parser.translate(item('started', 'command_execution', 'check',
            command='python -m pytest -q', status='in_progress'))
        if self.stale:
            (self.root / 'app.py').write_text('x = 2\n')
        yield from parser.translate(item('completed', 'command_execution', 'check',
            command='python -m pytest -q', status='completed', exit_code=0, aggregated_output='1 passed'))
        yield from parser.translate(item('completed', 'agent_message', 'final', text='Implemented and checked.'))
        yield 'done', {'model': self.model}


@pytest.mark.parametrize('fail_edit,stale,outcome', [
    (False, False, 'changed_verified'), (True, False, 'incomplete'),
    (False, True, 'changed_unverified'),
])
def test_sonn_failure_then_cli_handoff_records_real_results(tmp_path, monkeypatch, fail_edit, stale, outcome):
    session = Session(backend=StreamingBackend(name='sonn', events=[error('SONN credit limit')]), max_steps=2)
    session.project_path = str(tmp_path)
    failed = list(session.run('implement it'))
    assert failed[-1]['outcome'] == 'failed'
    session.backend = CliFixture(tmp_path, fail_edit=fail_edit, stale=stale)
    def forbidden(*args, **kwargs):
        pytest.fail('CLI tools must not be executed again by the native engine')
    monkeypatch.setattr('resonant_client.engine.session.execute_tool', forbidden)
    events = list(session.run('yes lets implement'))
    end = next(e for e in events if e['event'] == 'session.end')
    assert end['outcome'] == outcome
    assert end['evidence']['tool_calls'] == 2
    assert end['evidence']['promise_continuations'] == 0
    assert end['evidence']['checks'][0]['source'] == 'codex'
    assert len([e for e in events if e['event'] == 'tool.result']) == 2
    assert 'SONN credit limit' not in json.dumps(end)
