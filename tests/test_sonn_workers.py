import json
from pathlib import Path

import httpx
import pytest

from resonant_client.sonn_workers import SonnWorkerBackend, create_worker_session, fingerprint
from resonant_client.sonn_tasks import SonnTaskError
from resonant_client.engine.sandbox import SandboxViolation


@pytest.fixture
def worker(tmp_path):
    requests = []
    state = {'dispatch': True, 'status': 'settled', 'attempt_id': 'attempt_' + 'c' * 40}
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=state)
    grant = {'task_id': 'task_' + 'a' * 40, 'workspace_id': 'isolated_' + 'b' * 32, 'epoch': 1, 'inputs_digest': fingerprint([])}
    backend = SonnWorkerBackend('worker-capability', base_url='https://sonn.example/v1/employee-worker/v1',
        conversation_id='worker_conversation', grant=grant, journal_path=tmp_path / 'private.sqlite',
        transport=httpx.MockTransport(handle))
    return backend, requests, state


def test_worker_payload_fixes_generated_origin_and_cannot_create_root(worker):
    backend, requests, _ = worker
    root, identity = backend.task_controller.begin_request()
    payload = backend._payload('Assigned work', [], 'Instructions', [], 1024)
    assert payload['metadata']['sonn_input_origin'] == 'generated'
    assert 'sonn_task_id' not in payload['metadata']
    assert payload['metadata']['sonn_max_internal_calls'] == 1
    with pytest.raises(SonnTaskError):
        backend.enable_employee_task('other.sqlite', ceiling_microusd=100)
    with pytest.raises(SonnTaskError):
        backend.consult_employee('Help')
    assert not requests
    backend.task_controller.recover()
    assert backend.task_controller.state()['pending'] is None
    assert requests[0].url.path == '/v1/employee-worker/v1/request-status'


def test_file_claim_is_not_replayed_and_unknown_request_remains_held(worker):
    backend, requests, state = worker
    controller = backend.task_controller
    controller.claim_tool('file_call', 'file_write', {'path': '/isolated/file.txt'})
    assert json.loads(requests[-1].content)['arguments_digest'] == fingerprint({'path': '/isolated/file.txt'})
    state['dispatch'] = False
    with pytest.raises(SonnTaskError, match='already exists'):
        controller.claim_tool('file_call', 'file_write', {'path': '/isolated/file.txt'})
    with pytest.raises(SonnTaskError, match='file-only'):
        controller.claim_tool('shell_call', 'bash', {'command': 'echo forbidden'})
    controller.begin_request()
    state['status'] = 'unknown'
    with pytest.raises(SonnTaskError, match='uncertain'):
        controller.recover()
    assert controller.state()['pending']


def test_native_worker_has_file_only_tools_and_epoch_sandbox(worker, tmp_path):
    backend, _, _ = worker
    session = create_worker_session(backend, tmp_path / 'workspaces')
    assert {tool['function']['name'] for tool in session._allowed_tools} == {'file_read', 'file_write', 'file_edit', 'glob', 'grep'}
    assert Path(session.project_path).name == 'epoch-1'
    assert session.sandbox.enabled and not session.sandbox.allowed_dirs
    with pytest.raises(SandboxViolation):
        session._prepare_workspace_tool_args('file_read', {'path': str(tmp_path / 'private.sqlite')})
    with pytest.raises(SandboxViolation):
        session._prepare_workspace_tool_args('file_write', {'path': '../other-epoch.txt', 'content': 'bad'})
    controller = backend.task_controller
    controller.mark_cancelled()
    with pytest.raises(SonnTaskError):
        controller.claim_tool('cancelled_call', 'file_read', {'path': 'file.txt'})


def test_special_worker_tools_cannot_bypass_early_guard(worker, tmp_path):
    backend, _, _ = worker
    session = create_worker_session(backend, tmp_path / 'workspaces')
    def stream(**kwargs):
        yield 'tool_call', {'id': 'invented_subtask', 'name': 'task', 'arguments': json.dumps({'description': 'escape'})}
        yield 'done', {}
    backend.stream = stream
    events = list(session.run('Do the assigned task'))
    assert any(event.get('code') == 'worker_action_blocked' for event in events), events


def test_verified_inputs_preserve_conflicting_versions_and_reject_tampering(worker, tmp_path):
    import hashlib
    from resonant_client.sonn_workers import install_worker_inputs, worker_input_path
    backend, _, _ = worker
    session = create_worker_session(backend, tmp_path / 'workspaces')
    a, b = b'first', b'second'
    first, second = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    inputs = [{'node_id': 'specialist_a', 'receipt_digest': 'a' * 64, 'artifacts': {'schema.json': first}},
              {'node_id': 'specialist_b', 'receipt_digest': 'b' * 64, 'artifacts': {'schema.json': second}}]
    backend.task_controller.grant['inputs_digest'] = fingerprint(inputs)
    with pytest.raises(SonnTaskError, match='signed worker assignment'):
        install_worker_inputs(session, inputs[:1], {first: a}.__getitem__)
    with pytest.raises(SonnTaskError, match='bytes do not match'):
        install_worker_inputs(session, inputs, lambda _: b)
    assert not list(Path(session.project_path).iterdir())
    expected = {worker_input_path('specialist_a', 'schema.json'): first,
                worker_input_path('specialist_b', 'schema.json'): second}
    assert install_worker_inputs(session, inputs, {first: a, second: b}.__getitem__) == expected
    assert not (Path(session.project_path) / 'schema.json').exists()
    instructions = session.project_instructions
    assert all(path in instructions for path in expected)
    for path, identity in expected.items():
        assert (Path(session.project_path) / path).read_bytes() == {first: a, second: b}[identity]
    assert install_worker_inputs(session, inputs, {first: a, second: b}.__getitem__) == expected
    assert session.project_instructions == instructions
    (Path(session.project_path) / next(iter(expected))).write_bytes(b'changed')
    with pytest.raises(SonnTaskError, match='Existing worker input differs'):
        install_worker_inputs(session, inputs, {first: a, second: b}.__getitem__)


@pytest.mark.parametrize('paths', [
    ['Schema.json', 'schema.json'], ['../escape'], ['CON.json'], ['trailing.'], ['nested/NUL'], ['C:/escape'],
    ['folder', 'folder/file'], ['Folder', 'folder/file'], ['file?']])
def test_invalid_dependency_paths_fail_before_install(worker, tmp_path, paths):
    import hashlib
    from resonant_client.sonn_workers import install_worker_inputs
    backend, _, _ = worker
    session = create_worker_session(backend, tmp_path / 'workspaces')
    raw = b'file'
    inputs = [{'node_id': 'specialist_a', 'receipt_digest': 'a' * 64,
               'artifacts': {path: hashlib.sha256(raw).hexdigest() for path in paths}}]
    backend.task_controller.grant['inputs_digest'] = fingerprint(inputs)
    with pytest.raises(SonnTaskError):
        install_worker_inputs(session, inputs, lambda _: raw)
    assert not list(Path(session.project_path).iterdir())
