"""Task naming must not delay coding or overwrite a user's chosen name."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from resonant_client.engine.session_titles import fallback_session_title, generate_session_title
from resonant_client.gui.session_titles import cancel_title_refinement, schedule_title_refinement
from resonant_client.gui.sessions import ProjectManager, SessionRecord
from resonant_client.gui.ws_commands import CommandContext, _cmd_rename_session


@pytest.mark.parametrize(('prompt', 'expected'), [
    ('Hi! Could you please build me an inventory dashboard?', 'Build an inventory dashboard'),
    ('Thanks for the previous changes. Can we fix the login validation?', 'Fix the login validation'),
    ('Please review the authentication module for bugs.', 'Review the authentication module for bugs'),
    ('## My request:\nCan you improve session navigation?', 'Improve session navigation'),
    ('Explain how worker cancellation works.', 'Explain how worker cancellation works'),
    ('', 'New task'),
])
def test_immediate_title_removes_conversation_filler(prompt, expected):
    assert fallback_session_title(prompt) == expected


def test_fallback_never_cuts_a_word_or_adds_an_ellipsis():
    title = fallback_session_title('Please build a dashboard for inventory management with reporting and supplier integration')
    assert len(title) <= 60
    assert not title.endswith(('...', 'with', 'and'))


def test_generated_title_uses_only_first_prompt_without_tools_or_history():
    calls = []
    class Backend:
        handles_tools = False
        def stream(self, **kwargs):
            calls.append(kwargs)
            yield 'text.delta', {'delta': 'Improve session '}
            yield 'text.delta', {'delta': 'navigation'}
            yield 'done', {}
    assert generate_session_title(Backend(), 'A long original request', threading.Event()) == 'Improve session navigation'
    assert calls[0]['tools'] == [] and calls[0]['conversation_history'] == []
    assert calls[0]['max_tokens'] == 32
    assert calls[0]['user_msg'] == 'A long original request'


@pytest.mark.parametrize('events', [
    [('error', {'message': 'unavailable'})],
    [('text.delta', {'delta': 'Here is the title:\nMore text'})],
    [('text.delta', {'delta': 'x' * 300})],
    [('tool_call', {'name': 'bash'})],
    [],
])
def test_invalid_generation_preserves_fallback(events):
    backend = SimpleNamespace(handles_tools=False, stream=lambda **kw: iter(events))
    assert generate_session_title(backend, 'prompt', threading.Event()) == ''


def test_cli_loop_and_cancelled_requests_are_never_invoked():
    def forbidden(**kw):
        pytest.fail('Must not start a coding CLI for metadata')
    cancel = threading.Event()
    assert generate_session_title(SimpleNamespace(handles_tools=True, stream=forbidden), 'prompt', cancel) == ''
    cancel.set()
    assert generate_session_title(SimpleNamespace(handles_tools=False, stream=forbidden), 'prompt', cancel) == ''


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from resonant_client.gui import sessions
    monkeypatch.setattr(sessions.Path, 'home', lambda: tmp_path)
    return ProjectManager(str(tmp_path / 'project'))


def test_title_is_persisted_immediately_and_manual_names_are_preserved(manager):
    record = manager.create_session()
    manager.update_session_title('Can you fix login validation?')
    assert record.title == 'Fix login validation'
    assert manager.list_sessions()[0]['title'] == record.title
    assert SessionRecord.from_dict(record.to_dict()).title_source == 'auto'
    record.title = 'New session'
    record.title_source = 'manual'
    manager.update_session_title('Build something else')
    assert record.title == 'New session'


@pytest.mark.asyncio
@pytest.mark.parametrize('interference', ['none', 'rename', 'navigate', 'new_turn'])
async def test_background_refinement_is_scoped_and_yields_to_user_work(manager, monkeypatch, interference):
    from resonant_client.gui import session_titles
    started, release = threading.Event(), threading.Event()
    def generate(backend, prompt, cancel):
        started.set()
        assert release.wait(5)
        return 'Improve session navigation'
    monkeypatch.setattr(session_titles, 'generate_session_title', generate)
    record = manager.create_session()
    manager.update_session_title('Can you improve the sessions?')
    original = record.title
    state = SimpleNamespace(project=manager, backend=SimpleNamespace(handles_tools=False))
    sent = []
    async def send(payload):
        sent.append(payload)
    try:
        schedule_title_refinement(state, SimpleNamespace(send_json=send), record, 'prompt')
        assert await asyncio.to_thread(started.wait, 2)
        assert record.title == original  # The UI remains usable while inference waits.
        if interference == 'rename':
            record.title_source = 'manual'
            record.title = 'My chosen title'
        elif interference == 'navigate':
            manager.current_session = None
        elif interference == 'new_turn':
            cancel_title_refinement(state)
        release.set()
        await state._session_title_task
        assert bool(sent) == (interference == 'none')
        assert record.title == ('Improve session navigation' if interference == 'none'
                                else 'My chosen title' if interference == 'rename' else original)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_renaming_another_session_does_not_switch_active_conversation(manager):
    other = manager.create_session()
    current = manager.create_session()
    async def send(payload):
        pass
    ctx = CommandContext(ws=SimpleNamespace(send_json=send), state=SimpleNamespace(project=manager),
                         msg={'session_id': other.id, 'title': 'My task'}, runs=None)
    await _cmd_rename_session(ctx)
    assert manager.current_session is current
    saved = manager.load_session(other.id, activate=False)
    assert saved.title == 'My task' and saved.title_source == 'manual'


def test_provider_failure_before_streaming_keeps_local_title():
    def fail(**kwargs):
        raise RuntimeError('provider unavailable')
    assert generate_session_title(SimpleNamespace(handles_tools=False, stream=fail), 'prompt', threading.Event()) == ''


@pytest.mark.asyncio
async def test_slow_title_request_is_cancelled_without_changing_title(manager, monkeypatch):
    from resonant_client.gui import session_titles
    finished = threading.Event()
    def slow(backend, prompt, cancel):
        assert cancel.wait(2)
        finished.set()
        return 'Late title'
    monkeypatch.setattr(session_titles, 'generate_session_title', slow)
    monkeypatch.setattr(session_titles, 'TITLE_TIMEOUT_SECONDS', 0.02)
    record = manager.create_session()
    manager.update_session_title('Build a dashboard')
    state = SimpleNamespace(project=manager, backend=SimpleNamespace(handles_tools=False))
    schedule_title_refinement(state, None, record, 'prompt')
    await state._session_title_task
    assert state._session_title_cancel.is_set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert record.title == 'Build a dashboard'
