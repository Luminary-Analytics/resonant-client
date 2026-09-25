import json

import httpx
import pytest

from lumi.sonn import SonnBackend
from lumi.sonn_tasks import SonnTaskController, SonnTaskError
BASE = 'https://sonn.example/v1/workspace/projects/project-test/openai/v1'


def sse(chunk):
    return 'data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n'


@pytest.fixture
def task(tmp_path):
    requests = []
    state = {'id': 'task_' + 'a' * 40, 'status': 'active', 'request_status': 'unknown'}
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/chat/completions'):
            state['request_status'] = 'settled'
            mode = json.loads(request.content).get('metadata', {}).get('sonn_task_mode')
            if mode in ('advise', 'coordinate'):
                state.update(employee_id='employee_fixture', attempts=[{'id': 'attempt_' + 'b' * 40,
                    'calls': [{'id': 'chatcmpl-advicefixture', 'mode': mode}]}])
                return httpx.Response(200, json={'id': 'chatcmpl-advicefixture',
                    'choices': [{'message': {'content': 'Check the recovery invariant before changing the code.'}}]})
            return httpx.Response(200, text=sse({'id': 'response', 'choices': [{'index': 0,
                'delta': {'content': 'Done.'}, 'finish_reason': 'stop'}]}), headers={'content-type': 'text/event-stream'})
        if request.url.path.endswith('/request-status'):
            return httpx.Response(200, json={'status': state['request_status'], 'attempt_id': 'attempt_' + 'b' * 40})
        if '/knowledge-candidates/' in request.url.path:
            return httpx.Response(200, json={'request_id': 'chatcmpl-advicefixture',
                'text': 'Check the recovery invariant before changing the code.'})
        if request.url.path.endswith('/recover'):
            if state['request_status'] == 'dispatching':
                return httpx.Response(409, json={})
            state['request_status'] = 'settled'
        if request.url.path.endswith('/cancel'):
            state['status'] = 'cancelled'
        return httpx.Response(200, json={k: v for k, v in state.items() if k != 'request_status'})
    backend = SonnBackend('fixture-key', base_url=BASE, transport=httpx.MockTransport(handle))
    backend.conversation_id = 'saved_native_conversation'
    path = tmp_path / 'task.sqlite'
    backend.enable_employee_task(path, ceiling_microusd=100_000)
    return backend, path, requests, state


def test_persisted_request_identity_blocks_restart_until_known_recovery(task):
    backend, path, requests, state = task
    root, request = backend.task_controller.begin_request()
    restored = SonnTaskController(backend, path, ceiling_microusd=100_000)
    assert restored.start() == root and restored.state()['pending'] == request
    with pytest.raises(SonnTaskError, match='unresolved'):
        restored.begin_request()
    with pytest.raises(SonnTaskError, match='uncertain'):
        restored.recover()
    state['request_status'] = 'dispatching'
    with pytest.raises(SonnTaskError, match='409'):
        restored.recover()
    assert restored.state()['pending'] == request
    state['request_status'] = 'completed'
    restored.recover()
    assert restored.state()['pending'] is None
    assert all(not r.url.path.endswith('/chat/completions') for r in requests)
    assert 'fixture-key' not in path.read_bytes().decode(errors='ignore')


def test_native_stream_and_compression_share_root_without_generated_learning(task):
    backend, _, requests, _ = task
    kwargs = dict(user_msg='Continue', conversation_history=[], instructions='fixture', tools=[], max_tokens=64)
    assert list(backend.stream(**kwargs))[-1][0] == 'done'
    assert list(backend.stream_auxiliary(purpose='compression', **kwargs))[-1][0] == 'done'
    model_requests = [r for r in requests if r.url.path.endswith('/chat/completions')]
    assert len(model_requests) == 2
    first, auxiliary = [json.loads(r.content) for r in model_requests]
    assert first['user'] == auxiliary['user'] == backend.conversation_id
    assert first['metadata']['sonn_task_id'] == auxiliary['metadata']['sonn_task_id']
    assert auxiliary['metadata']['sonn_learning_control'] == 'none'
    assert auxiliary['metadata']['sonn_task_purpose'] == 'compression'
    assert first['metadata']['sonn_max_internal_calls'] == 1
    assert model_requests[0].headers['idempotency-key'] != model_requests[1].headers['idempotency-key']
    assert backend.task_controller.state()['pending'] is None
    list(backend.stream_auxiliary(purpose='title', **kwargs))
    assert len([r for r in requests if r.url.path.endswith('/chat/completions')]) == 2


def test_cancellation_and_connection_change_cannot_replace_root(task):
    backend, path, requests, state = task
    backend.task_controller.begin_request()
    backend.task_controller.cancel()
    assert state['status'] == 'cancelled'
    with pytest.raises(SonnTaskError):
        backend.task_controller.begin_request()
    backend.conversation_id = 'different_conversation'
    with pytest.raises(SonnTaskError, match='different connection'):
        SonnTaskController(backend, path, ceiling_microusd=100_000)
    assert len([r for r in requests if r.url.path.endswith('/tasks')]) == 1


def test_task_controls_never_follow_credential_redirects(tmp_path):
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={'location': 'https://foreign.example/tasks'})
    backend = SonnBackend('fixture-key', base_url=BASE, transport=httpx.MockTransport(handle))
    backend.conversation_id = 'saved_conversation'
    with pytest.raises(SonnTaskError, match='307'):
        backend.enable_employee_task(tmp_path / 'task.sqlite', ceiling_microusd=100_000)
    assert len(seen) == 1 and 'foreign' not in seen[0]


def test_declared_task_shape_is_frozen_across_restart(task, tmp_path):
    backend, _, requests, _ = task
    path = tmp_path / 'declared.sqlite'
    descriptor = {'dependency_breadth': 4, 'contract_ambiguity': .8}
    backend.enable_employee_task(path, ceiling_microusd=100_000, descriptor=descriptor)
    assert json.loads(requests[-1].content)['descriptor'] == descriptor
    restored = SonnTaskController(backend, path, ceiling_microusd=100_000,
                                  descriptor=dict(reversed(list(descriptor.items()))))
    assert restored.start() == backend.task_controller.start()
    descriptor['dependency_breadth'] = 1
    assert backend.task_controller.descriptor['dependency_breadth'] == 4
    with pytest.raises(SonnTaskError, match='different connection'):
        SonnTaskController(backend, path, ceiling_microusd=100_000, descriptor=descriptor)
    for invalid in ({'dependency_breadth': True}, {'contract_ambiguity': float('nan')}, {'verified_failure': True}):
        with pytest.raises(ValueError):
            SonnTaskController(backend, path, ceiling_microusd=100_000, descriptor=invalid)


@pytest.mark.parametrize('mode', ['advise', 'coordinate'])
def test_advice_survives_restart_and_compression_and_links_once(task, monkeypatch, mode):
    backend, path, requests, _ = task
    # Lose the process after the server settled, before the local answer save.
    def interrupted(*args):
        raise RuntimeError('interrupted local save')
    monkeypatch.setattr(backend.task_controller, '_save_advice', interrupted)
    with pytest.raises(RuntimeError, match='interrupted'):
        backend.consult_employee('How should this bounded recovery be checked?', mode=mode)
    restored = SonnTaskController(backend, path, ceiling_microusd=100_000)
    backend.task_controller = restored
    restored.recover()
    assert restored.state()['advice']['status'] == 'awaiting'
    kwargs = dict(user_msg='Continue', conversation_history=[], instructions='fixture', tools=[], max_tokens=64)
    list(backend.stream_auxiliary(purpose='compression', **kwargs))
    assert restored.state()['advice']['status'] == 'awaiting'
    list(backend.stream(**kwargs))
    assert restored.state()['advice']['status'] == 'followed'
    first = json.loads([r for r in requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert first['metadata']['sonn_after_advice'] == 'chatcmpl-advicefixture'
    # The server supplies its recorded advice bytes; the client sends only the
    # original identity and cannot replace them with its own model statement.
    assert all('Unverified frontier advice' not in row['content'] for row in first['messages'])
    list(backend.stream(**kwargs))
    next_call = json.loads([r for r in requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert 'sonn_after_advice' not in next_call['metadata']
    assert len([r for r in requests if r.url.path.endswith('/chat/completions')
        and json.loads(r.content).get('metadata', {}).get('sonn_task_mode') == mode]) == 1


def test_node_claim_identity_survives_host_restart_without_new_root(task):
    backend, path, requests, _ = task
    backend.task_controller.claim_node('specialist_node')
    restored = SonnTaskController(backend, path, ceiling_microusd=100_000)
    restored.claim_node('specialist_node')
    claims = [request for request in requests if request.url.path.endswith('/graph/specialist_node/claim')]
    assert len(claims) == 2
    assert json.loads(claims[0].content) == json.loads(claims[1].content)
    assert json.loads(claims[0].content)['seconds'] == 300
    assert len([request for request in requests if request.url.path.endswith('/tasks')]) == 1
    assert not any(request.url.path.endswith('/chat/completions') for request in requests)


def test_revised_unstarted_setup_fences_late_root_response(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        return httpx.Response(200, json={'id': 'task_' + ('a' if len(requests) == 1 else 'b') * 40})
    backend = SonnBackend('fixture-key', base_url=BASE, transport=httpx.MockTransport(handle))
    backend.conversation_id = 'saved_conversation'
    original = SonnTaskController(backend, tmp_path / 'task.sqlite', ceiling_microusd=200000)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(original.start)
        assert entered.wait(5)
        revised = original.revise_unstarted(ceiling_microusd=100000)
        release.set()
        with pytest.raises(SonnTaskError, match='different connection'):
            old.result(timeout=5)
    assert revised.state()['root_id'] is None
    assert len(revised.state()['previous_setups']) == 1
    assert revised.start() == 'task_' + 'b' * 40
    assert json.loads(requests[0].content)['operation_id'] != json.loads(requests[1].content)['operation_id']
    assert all(request.url.path.endswith('/tasks') for request in requests)


def test_reclaim_needs_server_reconciliation_and_preserves_new_identity_across_restart(task):
    backend, path, requests, state = task
    controller = backend.task_controller
    controller.claim_node('specialist_node')
    original = json.loads(requests[-1].content)['operation_id']
    state['graph'] = {'nodes': {'specialist_node': {'status': 'running', 'epoch': 1, 'history': []}}}
    with pytest.raises(SonnTaskError, match='reconcile'):
        controller.reclaim_node('specialist_node', stopped_epoch=1)
    assert controller.state()['node_claims']['specialist_node'] == original
    state['graph']['nodes']['specialist_node'].update(status='waiting',
        history=[{'epoch': 1, 'stopped_receipt_digest': 'a' * 64}])
    controller.reclaim_node('specialist_node', stopped_epoch=1)
    renewed = json.loads(requests[-1].content)['operation_id']
    assert renewed != original
    state['graph']['nodes']['specialist_node'].update(status='running', epoch=2)
    restored = SonnTaskController(backend, path, ceiling_microusd=100_000)
    restored.reclaim_node('specialist_node', stopped_epoch=1)
    assert json.loads(requests[-1].content)['operation_id'] == renewed
    assert restored.state()['prior_node_claims'] == [{'node_id': 'specialist_node', 'epoch': 0, 'operation_id': original}]
    assert len([request for request in requests if request.url.path.endswith('/tasks')]) == 1
    assert not any(request.url.path.endswith('/chat/completions') for request in requests)
