import json

import pytest

from lumi.codex_events import CodexEvents, check_command
from lumi.engine.session import Session
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
    monkeypatch.setattr('lumi.engine.session.execute_tool', forbidden)
    events = list(session.run('yes lets implement'))
    end = next(e for e in events if e['event'] == 'session.end')
    assert end['outcome'] == outcome
    assert end['evidence']['tool_calls'] == 2
    assert end['evidence']['promise_continuations'] == 0
    assert end['evidence']['checks'][0]['source'] == 'codex'
    assert len([e for e in events if e['event'] == 'tool.result']) == 2
    assert 'SONN credit limit' not in json.dumps(end)


def test_earlier_messages_end_as_progress_and_only_the_last_is_the_reply(tmp_path, monkeypatch):
    """Codex's messages before its last are progress, as they happen (live and saved)."""
    session = Session(backend=CliFixture(tmp_path), max_steps=2)
    session.project_path = str(tmp_path)
    monkeypatch.setattr('lumi.engine.session.execute_tool', lambda *a, **k: pytest.fail('observed only'))
    events = list(session.run('Explain this project, then make one small improvement'))
    kinds = [e['event'] for e in events]
    done = [e for e in events if e['event'] == 'text.done']
    assert [(e['text'], bool(e.get('interim'))) for e in done] == [
        ("I'll implement it.", True), ('Implemented and checked.', False)]
    # The progress message ends before the first tool starts, live.
    assert kinds.index('text.done') < kinds.index('tool.call')
    # Deltas still arrive as they stream: live progress stays visible.
    assert [e['delta'] for e in events if e['event'] == 'text.delta'] == [
        "I'll implement it.", '\n\nImplemented and checked.']
    end = next(e for e in events if e['event'] == 'session.end')
    assert end['outcome'] == 'changed_verified'
    assert end['evidence']['cli_backend'] == 'codex'
    assert end['evidence']['unverified_reason'] == ''
    # The model's history keeps the whole step, as before.
    assistant = [m for m in session.conversation_history if m.get('role') == 'assistant']
    assert "I'll implement it." in assistant[-1]['content'] and 'Implemented and checked.' in assistant[-1]['content']


def test_stale_codex_check_is_reported_as_stale(tmp_path, monkeypatch):
    session = Session(backend=CliFixture(tmp_path, stale=True), max_steps=2)
    session.project_path = str(tmp_path)
    monkeypatch.setattr('lumi.engine.session.execute_tool', lambda *a, **k: pytest.fail('observed only'))
    end = [e for e in session.run('implement it') if e['event'] == 'session.end'][-1]
    assert end['outcome'] == 'changed_unverified'
    assert end['evidence']['unverified_reason'] == 'check_stale'


class FailingCli(CliFixture):
    def stream(self, **kwargs):
        parser = CodexEvents(self.model)
        yield from parser.translate(item('completed', 'agent_message', 'intro', text='Reading the project.'))
        yield from parser.translate(item('completed', 'command_execution', 'ls', command='ls', status='completed',
                                         exit_code=0, aggregated_output='app.py'))
        yield from parser.translate(item('completed', 'agent_message', 'partial', text='It is a small CLI'))
        yield 'error', {'message': 'Connection lost'}


def test_failure_after_partial_text_is_still_the_outcome(tmp_path, monkeypatch):
    session = Session(backend=FailingCli(tmp_path), max_steps=2)
    session.project_path = str(tmp_path)
    monkeypatch.setattr('lumi.engine.session.execute_tool', lambda *a, **k: pytest.fail('observed only'))
    events = list(session.run('explain this project'))
    errors = [e['message'] for e in events if e['event'] == 'error']
    assert errors == ['Connection lost']
    # The partial last message is never presented as a finished reply.
    assert [e['text'] for e in events if e['event'] == 'text.done'] == ['Reading the project.']
    assert all(e.get('interim') for e in events if e['event'] == 'text.done')
    assert events[-1]['event'] == 'session.end' and events[-1]['outcome'] == 'failed'
