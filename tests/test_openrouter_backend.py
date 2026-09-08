"""OpenRouter wire contracts and cross-provider context preservation."""
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.backends import EVENT_DONE, EVENT_ERROR, EVENT_TOOL_CALL, _build_codex_prompt, create_backend
from resonant_client.gui.app import AppState
from resonant_client.gui.costs import CostTracker
from resonant_client.gui.settings import SettingsManager
from resonant_client.openrouter import OpenRouterBackend


def sse(*events):
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(OpenRouterBackend, '_catalog', [])
    monkeypatch.setattr(OpenRouterBackend, '_catalog_at', 0)


def test_catalog_uses_reported_capabilities_and_caches(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [
            {'id': 'vendor/coder', 'supported_parameters': ['tools'], 'context_length': 65536,
             'architecture': {'input_modalities': ['text']}, 'top_provider': {'max_completion_tokens': 2048}},
            {'id': 'vendor/embed', 'supported_parameters': []},
        ]})
    transport = httpx.MockTransport(handler)
    assert [r['id'] for r in OpenRouterBackend.catalog(transport=transport)] == ['vendor/coder']
    OpenRouterBackend.catalog(transport=transport)
    assert len(calls) == 1
    backend = OpenRouterBackend('secret', 'vendor/coder')
    assert backend.effective_context_tokens == 65536
    assert not backend.capability_profile.supports('vision')
    payload = backend._payload('hi', [], 'system', [], 4096)
    assert payload['max_tokens'] == 2048
    assert 'reasoning_effort' not in payload


@pytest.mark.parametrize('model', ['vendor/coder', 'openai/gpt-6-astra'])
def test_stream_preserves_reasoning_tool_results_and_actual_cost(model):
    requests = []
    def handler(request):
        requests.append(json.loads(request.read()))
        assert request.headers['authorization'] == 'Bearer test-secret'
        assert request.url == 'https://openrouter.ai/api/v1/chat/completions'
        return httpx.Response(200, text=sse(
            {'id': 'r1', 'choices': [{'delta': {'reasoning_details': [{'type': 'reasoning.encrypted', 'index': 0, 'data': 'abc'}]}}]},
            {'id': 'r1', 'choices': [{'delta': {'reasoning_details': [{'type': 'reasoning.encrypted', 'index': 0, 'data': 'def'}],
                 'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'read', 'arguments': '{"path":"README.md"}'}}]}}]},
            {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0123}},
        ))
    backend = OpenRouterBackend('test-secret', model, transport=httpx.MockTransport(handler))
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    events = list(backend.stream('read file', [], 'keep instructions', tools))
    assert not [e for e in events if e[0] == EVENT_ERROR]
    call = next(data for event, data in events if event == EVENT_TOOL_CALL)
    assert call['reasoning_details'][0]['data'] == 'abcdef'
    stats = next(data['stats'] for event, data in events if event == EVENT_DONE)
    assert stats['cost_usd'] == 0.0123
    history = [{'role': 'user', 'content': 'read file'}, {**call, 'role': 'tool_call'},
               {'role': 'tool_result', 'call_id': 'c1', 'name': 'read', 'content': 'hello'}]
    payload = backend._payload('continue', history, 'keep instructions', tools, None)
    assistant = next(m for m in payload['messages'] if m.get('tool_calls'))
    assert assistant['reasoning_details'] == call['reasoning_details']
    assert 'reasoning_content' not in assistant
    assert payload['messages'][-2]['role'] == 'tool'
    assert payload['messages'][-2]['content'] == 'hello'
    assert 'reasoning_effort' not in requests[0]
    assert requests[0]['provider'] == {'require_parameters': True}
    assert requests[0]['model'] == model


def test_plain_reasoning_is_preserved_only_for_the_model_that_produced_it():
    backend = OpenRouterBackend('key', 'vendor/model')
    history = [{'role': 'tool_call', 'name': 'read', 'call_id': 'r', 'arguments': '{}',
                'reasoning_content': 'Read the file first.', 'provider_model': 'vendor/model'},
               {'role': 'tool_result', 'call_id': 'r', 'name': 'read', 'content': 'result'}]
    messages = backend._messages(history, 'system', 'continue')
    assistant = next(m for m in messages if m.get('tool_calls'))
    assert assistant['reasoning'] == 'Read the file first.'
    backend.model = 'another/model'
    messages = backend._messages(history, 'system', 'continue')
    assistant = next(m for m in messages if m.get('tool_calls'))
    assert 'reasoning' not in assistant and 'reasoning_details' not in assistant


@pytest.mark.parametrize('code, phrase', [(401, 'API key'), (402, 'credits are exhausted')])
def test_auth_and_credit_errors_are_actionable(code, phrase):
    backend = OpenRouterBackend('secret', 'vendor/coder', transport=httpx.MockTransport(
        lambda _: httpx.Response(code, json={'error': {'message': 'no'}})))
    events = list(backend.stream('hi', [], '', []))
    assert phrase in next(d['message'] for e, d in events if e == EVENT_ERROR)


def test_settings_spec_hides_secret_and_project_preference_survives_reload(tmp_path):
    settings = SettingsManager(tmp_path / 'settings.json')
    settings.set('api_keys', 'openrouter', 'secret')
    state = AppState.__new__(AppState)
    state.settings = settings
    state.permission_mode = 'bypass'
    state.backend_spec = None
    state.available_backends = {'openrouter': {'models': ['vendor/coder']}}
    state.project = SimpleNamespace(project_path=str(tmp_path), current_session=None, list_sessions=lambda: [])
    spec = state.build_backend_spec('openrouter')
    assert spec.api_key == ''
    assert spec.create_backend(settings).api_key == 'secret'
    assert settings.get_masked()['api_keys']['openrouter'] == ''
    assert 'secret' not in json.dumps(spec.to_dict())
    project_key = os.path.normcase(os.path.abspath(tmp_path))
    settings.set('project_models', project_key, {'backend': 'openrouter', 'model': 'vendor/coder'})
    state.settings = SettingsManager(tmp_path / 'settings.json')
    assert state.project_chat_backend_choice() == ('openrouter', 'vendor/coder')
    state.project.current_session = SimpleNamespace(backend_type='codex', model='selected-for-session')
    assert state.project_chat_backend_choice() == ('codex', 'selected-for-session')
    assert create_backend('openrouter', api_key='secret', model='vendor/coder').name == 'openrouter'


def test_provider_cost_overrides_generic_model_price(tmp_path):
    costs = CostTracker(tmp_path / 'costs.json')
    assert costs.record_usage('unknown/model', 100, 20, actual_cost=0.0123) == 0.0123
    assert costs.get_session_cost()['cost_usd'] == 0.0123
    assert costs.record_usage('kimi-k3', 100, 20, actual_cost=0) == 0


def test_codex_handoff_keeps_project_notes_and_compaction_summary():
    history = [{'role': 'system', 'content': 'Progress: login implemented; tests still needed.'}]
    history += [{'role': 'assistant', 'content': f'update {i}'} for i in range(30)]
    instructions = '--- PROJECT MEMORY ---\nUse pnpm test\n--- END PROJECT MEMORY ---'
    prompt = _build_codex_prompt(user_msg='continue', conversation_history=history, instructions=instructions, cwd='project')
    assert 'Use pnpm test' in prompt
    assert 'login implemented; tests still needed' in prompt
    assert 'CURRENT USER REQUEST' in prompt


def test_engine_builds_a_file_and_continues_with_tool_result(tmp_path):
    from resonant_client.engine.session import Session
    target = tmp_path / 'hello.py'
    requests = []
    def handler(request):
        payload = json.loads(request.read())
        requests.append(payload)
        if len(requests) == 1:
            response = {'id': 'build-1', 'choices': [{'delta': {
                'reasoning_details': [{'type': 'reasoning.encrypted', 'index': 0, 'data': 'opaque'}],
                'tool_calls': [{'index': 0, 'id': 'write-1', 'function': {
                    'name': 'file_write', 'arguments': json.dumps({'path': str(target), 'content': 'print("hello")\n'})}}],
            }}]}
        else:
            response = {'choices': [{'delta': {'content': 'Created hello.py.'}}]}
        return httpx.Response(200, text=sse(response))
    backend = OpenRouterBackend('fixture-key', 'vendor/coder', transport=httpx.MockTransport(handler))
    session = Session(backend=backend, max_steps=2, auto_approve=True)
    session.project_path = str(tmp_path)
    list(session.run('Create the requested hello.py file.'))
    assert target.read_text() == 'print("hello")\n'
    assert len(requests) == 2
    assistant = next(m for m in requests[1]['messages'] if m.get('tool_calls'))
    assert assistant['reasoning_details'][0]['data'] == 'opaque'
    assert any(m['role'] == 'tool' and m['tool_call_id'] == 'write-1' for m in requests[1]['messages'])
    history = session.conversation_history
    session.set_backend(SimpleNamespace(name='codex', model='test-model', handles_tools=True))
    assert session.conversation_history is history
    assert any('Created hello.py' in str(turn.get('content')) for turn in history)
