"""Durable native task identity and conservative recovery for SONN execution."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import httpx


class SonnTaskError(RuntimeError):
    """A task needs owner attention or reconciliation before more execution."""


def _configuration(backend, ceiling_microusd, descriptor):
    if type(ceiling_microusd) is not int or not 1 <= ceiling_microusd <= 20_000_000:
        raise ValueError('An explicit integer task allowance is required')
    descriptor = {} if descriptor is None else descriptor
    if not isinstance(descriptor, dict) or set(descriptor) - {'task_breadth', 'dependency_breadth', 'contract_ambiguity', 'unresolved_prerequisite'}:
        raise ValueError('A bounded task descriptor is required')
    for key, value in descriptor.items():
        if key in ('task_breadth', 'dependency_breadth'):
            valid = type(value) is int and 0 <= value <= 8
        elif key == 'unresolved_prerequisite':
            valid = type(value) is bool
        else:
            valid = type(value) in (int, float) and 0 <= value <= 1
        if not valid:
            raise ValueError('Invalid task descriptor value')
    normalized = dict(sorted(descriptor.items()))
    identity = [backend.base_url, backend.conversation_id,
                hashlib.sha256(backend.api_key.encode()).hexdigest(), ceiling_microusd]
    if normalized:
        identity.append(normalized)  # Preserve existing empty-descriptor journals.
    return normalized, hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class SonnTaskController:
    def __init__(self, backend, journal_path: str | Path, *, ceiling_microusd: int, descriptor=None):
        match = re.fullmatch(r'(.*/v1/workspace/projects/[A-Za-z0-9_-]{8,80})/openai/v1', backend.base_url)
        if not match or not backend.conversation_id:
            raise ValueError('Bind a saved SONN project conversation before enabling task execution')
        self.backend, self.url, self.path = backend, match[1] + '/tasks', Path(journal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.descriptor, self.binding = _configuration(backend, ceiling_microusd, descriptor)
        self.ceiling = ceiling_microusd
        self._update(lambda state: {**(state or {'binding': self.binding, 'operation_id': 'native_task_' + uuid.uuid4().hex,
                                            'root_id': None, 'pending': None, 'status': 'new'}),
            'configuration': {'ceiling_microusd': self.ceiling, 'descriptor': self.descriptor}})

    def _update(self, mutate):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS task (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT value FROM task WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else None
            if state and state['binding'] != self.binding:
                raise SonnTaskError('Saved task belongs to a different connection, conversation, or allowance')
            value = mutate(state)
            connection.execute('INSERT OR REPLACE INTO task VALUES (1, ?)', (json.dumps(value),))
            connection.commit()
            return value
        finally:
            connection.close()

    def state(self):
        return self._update(lambda state: state)

    def revise_unstarted(self, *, ceiling_microusd, descriptor=None):
        """Revise a setup that cannot yet have dispatched a model call.

        Preserve old root-creation identities. A late response using the old
        binding cannot attach or proceed to generation after this transaction.
        """
        normalized, binding = _configuration(self.backend, ceiling_microusd, descriptor)
        if binding == self.binding:
            return self
        def revise(state):
            if state['root_id'] is not None or state.get('pending') or state['status'] != 'new':
                raise SonnTaskError('This conversation already has a saved task; recover its original state')
            history = state.get('previous_setups', [])
            if len(history) >= 16:
                raise SonnTaskError('Task setup revision allowance is exhausted')
            return {'binding': binding, 'operation_id': 'native_task_' + uuid.uuid4().hex,
                'root_id': None, 'pending': None, 'status': 'new',
                'configuration': {'ceiling_microusd': ceiling_microusd, 'descriptor': normalized},
                'previous_setups': [*history, {key: state[key] for key in ('binding', 'operation_id', 'configuration')}]}
        self._update(revise)
        revised = copy.copy(self)
        revised.binding, revised.ceiling, revised.descriptor = binding, ceiling_microusd, normalized
        return revised

    def _request(self, method, suffix='', body=None, *, url=None, headers=None, timeout=10):
        try:
            with httpx.Client(timeout=timeout, transport=self.backend._transport, follow_redirects=False) as client:
                with client.stream(method, url or self.url + suffix, headers={**(headers or {}),
                    'Authorization': 'Bearer ' + self.backend.api_key}, json=body) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 256_000:
                            raise SonnTaskError('SONN task response exceeded its bounded contract')
                        chunks.append(chunk)
                    if response.status_code != 200:
                        raise SonnTaskError('SONN task control requires attention (HTTP %d); no model call was retried' % response.status_code)
                    result = json.loads(b''.join(chunks))
                    if not isinstance(result, dict):
                        raise ValueError('task response')
                    return result
        except (httpx.HTTPError, ValueError) as exc:
            raise SonnTaskError('SONN task control is unavailable; retain the task and reconcile before continuing') from exc

    def start(self):
        state = self.state()
        if state['root_id']:
            return state['root_id']
        if state['status'] == 'cancelled':
            raise SonnTaskError('Task is cancelled')
        result = self._request('POST', body={'operation_id': state['operation_id'],
            'conversation_id': self.backend.conversation_id, 'ceiling_microusd': self.ceiling, 'seconds': 900,
            **({'descriptor': self.descriptor} if self.descriptor else {})})
        root_id = result.get('id')
        if not isinstance(root_id, str) or not re.fullmatch('task_[a-f0-9]{40}', root_id):
            raise SonnTaskError('SONN did not return a valid root task identity')
        def save(current):
            if current['root_id'] not in (None, root_id):
                raise SonnTaskError('Conflicting root task identity')
            return {**current, 'root_id': root_id, 'status': 'active'}
        self._update(save)
        return root_id

    def begin_request(self, *, purpose='execution'):
        if purpose not in ('execution', 'compression', 'advice', 'coordination'):
            raise ValueError('Unknown native task purpose')
        root_id = self.start()
        request_id = 'native_call_' + uuid.uuid4().hex
        def claim(state):
            if state['pending'] or state['status'] != 'active':
                raise SonnTaskError('The previous task request is unresolved or cancelled; recover it before continuing')
            advice = state.get('advice')
            if purpose in ('advice', 'coordination') and advice and advice['status'] != 'followed':
                raise SonnTaskError('Use or reconcile the previous advice before another consultation')
            if purpose == 'execution' and advice and advice['status'] == 'awaiting':
                state = {**state, 'advice': {**advice, 'following_request': request_id, 'status': 'following'}}
            return {**state, 'pending': request_id, 'pending_purpose': purpose}
        self._update(claim)
        return root_id, request_id

    def _save_advice(self, request_id, text):
        if (not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', request_id)
                or not isinstance(text, str) or not text.strip()):
            raise SonnTaskError('The advisor did not return a usable recorded answer')
        value = {'request_id': request_id, 'text': text.encode('utf-8')[:8192].decode('utf-8', errors='ignore'),
                 'status': 'awaiting', 'following_request': None}
        def save(state):
            previous = state.get('advice')
            if previous and previous['status'] != 'followed' and previous != value:
                raise SonnTaskError('Another consultation already awaits execution')
            return {**state, 'advice': value}
        self._update(save)

    def ask_advice(self, question, *, source_teaching_ids=None, mode='advise'):
        """One owner-consented consultation; return to ordinary execution next."""
        if mode not in ('advise', 'coordinate'):
            raise ValueError('Select execution advice or coordination advice')
        if not isinstance(question, str) or not question.strip() or len(question.encode('utf-8')) > 8192:
            raise ValueError('Advice question must contain 1 to 8192 UTF-8 bytes')
        sources = [] if source_teaching_ids is None else source_teaching_ids
        if (not isinstance(sources, list) or len(sources) > 8
                or any(not isinstance(x, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', x) for x in sources)):
            raise ValueError('Select at most eight bounded teaching inputs')
        root_id, request_id = self.begin_request(purpose='coordination' if mode == 'coordinate' else 'advice')
        body = {'model': 'sonn-auto', 'user': self.backend.conversation_id, 'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': question}], 'metadata': {
                'sonn_task_id': root_id, 'sonn_task_mode': mode, 'sonn_max_internal_calls': 1,
                'sonn_knowledge_consultation': {'source_teaching_ids': sources},
                'sonn_input_origin': 'generated', 'sonn_learning_control': 'none',
                'sonn_capture_inputs': False, 'sonn_observation_learning': False, 'sonn_human_learning': False}}
        response = self._request('POST', body=body, url=self.backend.base_url + '/chat/completions',
                                 headers={'Idempotency-Key': request_id}, timeout=300)
        self._save_advice(response['id'], response['choices'][0]['message']['content'])
        self.recover()
        return self.state()['advice']

    def recover(self):
        state = self.state()
        pending = state['pending']
        if not pending:
            return self.view()
        path = '/' + state['root_id']
        result = self._request('POST', path + '/request-status', {'idempotency_key': pending})
        if result['status'] in ('allocated', 'reserved', 'refusing', 'dispatching', 'completed'):
            self._request('POST', path + '/recover', {'attempt_id': result['attempt_id']})
            result = self._request('POST', path + '/request-status', {'idempotency_key': pending})
        if result['status'] not in ('settled', 'refused'):
            raise SonnTaskError('Original provider outcome remains uncertain; no model call was retried')
        if (result['status'] == 'settled' and state.get('pending_purpose') in ('advice', 'coordination')
                and not (state.get('advice') or {}).get('status') == 'awaiting'):
            # Recover a response lost after server settlement using its immutable
            # knowledge receipt, never another consultation.
            task = self.view()
            attempt = next((row for row in task['attempts'] if row['id'] == result['attempt_id']), None)
            employee = task.get('employee_id')
            if (not attempt or len(attempt['calls']) != 1 or not isinstance(employee, str)
                    or not re.fullmatch('[A-Za-z0-9_-]{8,80}', employee)):
                raise SonnTaskError('Original advisor receipt needs reconciliation')
            call_id = attempt['calls'][0]['id']
            if not isinstance(call_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', call_id):
                raise SonnTaskError('Invalid original advisor receipt')
            workspace_url = self.url.rsplit('/projects/', 1)[0]
            record = self._request('GET', url=workspace_url + '/employees/' + employee + '/knowledge-candidates/' + call_id)
            self._save_advice(record['request_id'], record['text'])
        def clear(current):
            if current['pending'] != pending:
                raise SonnTaskError('Task request changed during reconciliation')
            advice = current.get('advice')
            if advice and advice.get('following_request') == pending:
                current = {**current, 'advice': {**advice, 'status': 'followed'}}
            return {**current, 'pending': None, 'pending_purpose': None}
        self._update(clear)
        return self.view()

    def view(self):
        return self._request('GET', '/' + self.start())

    def assign_nodes(self, nodes):
        """Freeze the coordinator's bounded graph under its existing root allowance."""
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 8:
            raise ValueError('A root graph requires one to eight assigned nodes')
        return self._request('POST', '/' + self.start() + '/graph', {'nodes': nodes})

    def graph(self):
        return self._request('GET', '/' + self.start() + '/graph')

    def claim_node(self, node_id):
        """Persist claim identity before requesting one isolated worker capability.

        The result contains a private capability for the worker host, never UI
        state or model context. A replay with dispatch=False cannot start work.
        """
        if not isinstance(node_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,128}', node_id):
            raise ValueError('A bounded assigned node identity is required')
        root = self.start()
        def claim(state):
            claims = state.setdefault('node_claims', {})
            if node_id not in claims:
                if len(claims) >= 8:
                    raise SonnTaskError('Native task node claim journal is full')
                claims[node_id] = 'native_claim_' + uuid.uuid4().hex
            return state
        state = self._update(claim)
        return self._request('POST', '/' + root + '/graph/' + node_id + '/claim',
                             {'operation_id': state['node_claims'][node_id], 'seconds': 300})

    def reclaim_node(self, node_id, *, stopped_epoch):
        """Claim a fresh epoch only after the server records trusted host recovery.

        This method cannot attest a stopped process or settle a model request.
        It observes an existing server reconciliation and preserves prior claim
        identities so concurrent/restarted hosts still request the same new work.
        """
        if (not isinstance(node_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,128}', node_id)
                or type(stopped_epoch) is not int or not 1 <= stopped_epoch < 16):
            raise ValueError('Exact stopped node epoch required for recovery')
        state = self.state()
        if state.get('node_claim_epochs', {}).get(node_id) == stopped_epoch:
            return self.claim_node(node_id)
        graph = self.graph().get('graph') or {}
        node = graph.get('nodes', {}).get(node_id) or {}
        history = node.get('history') or []
        if (node.get('status') != 'waiting' or node.get('epoch') != stopped_epoch or not history
                or history[-1].get('epoch') != stopped_epoch
                or not re.fullmatch('[a-f0-9]{64}', str(history[-1].get('stopped_receipt_digest', '')))):
            raise SonnTaskError('The server must reconcile the original stopped worker and accounting before reassignment')
        def renew(current):
            epochs = current.setdefault('node_claim_epochs', {})
            if epochs.get(node_id) == stopped_epoch:
                return current
            claims = current.setdefault('node_claims', {})
            if node_id not in claims or epochs.get(node_id, 0) >= stopped_epoch:
                raise SonnTaskError('The original native node claim is missing or has changed')
            prior = current.setdefault('prior_node_claims', [])
            if len(prior) >= 120:
                raise SonnTaskError('Native node recovery allowance is exhausted')
            prior.append({'node_id': node_id, 'epoch': epochs.get(node_id, 0), 'operation_id': claims[node_id]})
            claims[node_id] = 'native_claim_' + uuid.uuid4().hex
            epochs[node_id] = stopped_epoch
            return current
        self._update(renew)
        return self.claim_node(node_id)

    def mark_cancelled(self):
        self._update(lambda state: {**state, 'status': 'cancelled'})

    def cancel(self):
        self.mark_cancelled()
        root_id = self.state()['root_id']
        if root_id:
            return self._request('POST', '/' + root_id + '/cancel', {})
        return {'status': 'cancelled'}
