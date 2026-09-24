import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.sonn import SonnBackend
from resonant_client.sonn_tasks import SonnTaskError
from resonant_client.gui import employee_tasks, ws_commands
from tests.test_ws_command_registry import _ctx, _run


@pytest.fixture
def gui_task(tmp_path, monkeypatch):
    requests = []
    server = {'id': 'task_' + 'a' * 40, 'status': 'active', 'ceiling_microusd': 100000,
        'quota': {'allocated_microusd': 0, 'charged_microusd': 0, 'claimed_calls': 0, 'claimed_advisory': 0,
                  'private_cost_breakdown': 'never expose'},
        'unresolved_attempts': 0, 'unresolved_preflights': 0, 'attempts': []}
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/graph'):
            return httpx.Response(200, json={'graph': server.get('graph')})
        if request.url.path.endswith('/cancel'):
            server['status'] = 'cancelled'
        return httpx.Response(200, json=server)
    transport = httpx.MockTransport(handle)
    backend = SonnBackend('private-owner-key', base_url='https://sonn.example/v1/workspace/projects/project_test/openai/v1', transport=transport)
    monkeypatch.setattr(employee_tasks, '_sessions_dir', lambda _: tmp_path / 'private')
    state = SimpleNamespace(project=SimpleNamespace(project_path=str(tmp_path / 'work'), current_session=SimpleNamespace(id='saved_session')),
                            session=SimpleNamespace(backend=backend))
    return state, server, requests, transport


def test_ui_configuration_and_restart_preserve_task_binding(gui_task):
    state, server, requests, transport = gui_task
    backend = state.session.backend
    employee_tasks.configure(backend, state.project.project_path, 'saved_session', ceiling_microusd=100000, descriptor={})
    assert json.loads(requests[0].content)['ceiling_microusd'] == 100000
    restored = SonnBackend(backend.api_key, base_url=backend.base_url, transport=transport)
    controller = employee_tasks.restore_task(restored, state.project.project_path, 'saved_session')
    assert restored.conversation_id == backend.conversation_id
    assert controller.start() == server['id'] and len(requests) == 1
    view = employee_tasks.public_state(restored, state.project.project_path, 'saved_session')
    assert 'private-owner-key' not in json.dumps(view) and 'private_cost_breakdown' not in json.dumps(view)
    with pytest.raises(SonnTaskError, match='already has'):
        employee_tasks.configure(restored, state.project.project_path, 'saved_session', ceiling_microusd=200000, descriptor={})


def test_uncertain_or_malformed_task_cannot_detach_into_unbounded_generation(gui_task):
    state, server, _, _ = gui_task
    backend = state.session.backend
    controller = employee_tasks.configure(backend, state.project.project_path, 'saved_session', ceiling_microusd=100000, descriptor={})
    controller.cancel()
    server['quota']['allocated_microusd'] = 100
    with pytest.raises(SonnTaskError, match='reconcile'):
        employee_tasks.detach(controller)
    assert backend.task_controller is not None
    server['quota']['allocated_microusd'] = 0
    employee_tasks.detach(controller)
    assert employee_tasks.restore_task(backend, state.project.project_path, 'saved_session') is None
    assert employee_tasks.public_state(backend, state.project.project_path, 'saved_session')['used'] is True
    employee_tasks.journal_path(state.project.project_path, 'saved_session').write_bytes(b'corrupted local state')
    with pytest.raises(SonnTaskError, match='recovery'):
        employee_tasks.restore_task(backend, state.project.project_path, 'saved_session')


def test_ui_command_checks_captured_identity_and_routes_writes_through_queue(gui_task):
    state, _, requests, _ = gui_task
    message = {'command': 'employee_task', 'action': 'start', 'project': state.project.project_path,
               'session_id': 'saved_session', 'request_id': 'request_one', 'ceiling_microusd': 100000, 'descriptor': {}}
    queued = []
    async def enqueue(msg): queued.append(msg)
    ctx = _ctx(state=state, msg=message, runs=SimpleNamespace(busy=False, enqueue=enqueue))
    _run(ws_commands.HANDLERS['employee_task'], ctx)
    assert queued == [message] and requests == []
    asyncio.run(employee_tasks.command(state, ctx.send, queued[0]))
    assert ctx.ws.sent[-1]['task']['status'] == 'active'
    stale = dict(message, action='cancel', session_id='other_session')
    asyncio.run(employee_tasks.command(state, ctx.send, stale))
    assert 'changed' in ctx.ws.sent[-1]['error']
    assert len(requests) == 2  # create + owned status; stale cancel did not reach the server.
    ctx = _ctx(state=state, msg=message, runs=SimpleNamespace(busy=True))
    assert 'active operation' in _run(ws_commands.HANDLERS['employee_task'], ctx)[0]['error']


def test_missing_accounting_does_not_mean_zero_and_root_recovery_includes_workers(gui_task):
    state, server, requests, _ = gui_task
    controller = employee_tasks.configure(state.session.backend, state.project.project_path, 'saved_session',
                                         ceiling_microusd=100000, descriptor={})
    server['attempts'] = [{'id': 'attempt_' + 'a' * 40, 'status': 'dispatching'},
                          {'id': 'attempt_' + 'b' * 40, 'status': 'reserved'}]
    employee_tasks.recover_all(controller)
    recovered = [json.loads(request.content)['attempt_id'] for request in requests if request.url.path.endswith('/recover')]
    assert recovered == [attempt['id'] for attempt in server['attempts']]
    controller.cancel()
    server['quota'].pop('allocated_microusd')
    with pytest.raises(SonnTaskError, match='accounting is incomplete'):
        employee_tasks.detach(controller)
    assert state.session.backend.task_controller is not None


def test_actual_app_websocket_dispatches_task_controls_through_chat_queue(gui_task, monkeypatch):
    from tests.gui_access import LocalClient
    from resonant_client.gui import app as gui_app
    state, _, requests, _ = gui_task
    state.available_backends = {'sonn': {}}
    state.codebase_index = object()
    monkeypatch.setattr(gui_app, 'state', state)
    with LocalClient(gui_app.app) as client:
        with client.websocket_connect('/ws') as socket:
            socket.send_json({'command': 'employee_task', 'action': 'start', 'project': state.project.project_path,
                'session_id': 'saved_session', 'request_id': 'socket_start', 'ceiling_microusd': 100000})
            result = socket.receive_json()
            assert result['event'] == 'employee_task_state' and result['task']['status'] == 'active', result
            socket.send_json({'command': 'employee_task', 'action': 'cancel', 'project': state.project.project_path,
                             'session_id': 'saved_session', 'request_id': 'socket_cancel'})
            result = socket.receive_json()
            assert result['task']['status'] == 'cancelled', result
    assert not any(request.url.path.endswith('/chat/completions') for request in requests)
    assert 'private-owner-key' not in json.dumps(result)


def test_graph_inspector_preserves_unverified_and_blocked_status_without_secrets(gui_task):
    state, server, _, _ = gui_task
    controller = employee_tasks.configure(state.session.backend, state.project.project_path, 'saved_session',
                                         ceiling_microusd=100000, descriptor={})
    specialist = {'id': 'specialist_node', 'employee_id': 'employee_specialist', 'status': 'reported',
        'dependencies': [], 'outputs': ['schema.json'], 'input_contract': '<script>source</script>',
        'epoch': 1, 'integration': False, 'grant': 'private-worker-capability',
        'receipt': {'private': 'not-browser-data'}, 'history': []}
    integration = {'id': 'integration_node', 'employee_id': 'employee_coordinator', 'status': 'waiting',
        'dependencies': ['specialist_node'], 'outputs': ['integrated.json'], 'input_contract': 'Verify combined result',
        'epoch': 0, 'integration': True}
    server['graph'] = {'status': 'active', 'root_status': 'active', 'deadline_passed': False,
                       'nodes': {'specialist_node': specialist, 'integration_node': integration}}
    graph = employee_tasks.public_graph(controller)
    assert graph['nodes'][0]['status'] == 'reported'
    assert graph['nodes'][1]['blocked_by'] == ['specialist_node']
    assert 'private-worker-capability' not in json.dumps(graph) and 'not-browser-data' not in json.dumps(graph)
    specialist.update(status='running', lease_until=1, tool_actions={
        'old': {'status': 'claimed', 'identity': {'epoch': 0}},
        'current': {'status': 'claimed', 'identity': {'epoch': 1}}})
    graph = employee_tasks.public_graph(controller)
    assert graph['nodes'][0]['lease_expired'] and graph['nodes'][0]['unresolved_file_actions'] == 1
    replies = []
    async def send(value): replies.append(value)
    asyncio.run(employee_tasks.command(state, send, {'action': 'graph', 'project': state.project.project_path,
        'session_id': 'saved_session', 'request_id': 'inspect_graph'}))
    assert replies[0]['graph'] == graph
