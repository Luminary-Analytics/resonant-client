"""Native UI task controls with private durable configuration and captured identity."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from ..sonn import SonnBackend
from ..sonn_tasks import SonnTaskController, SonnTaskError
from .runtime import bind_sonn_conversation
from .sessions import _sessions_dir, is_valid_session_id


def journal_path(project_path: str, session_id: str) -> Path:
    if not project_path or not is_valid_session_id(session_id):
        raise SonnTaskError('Open a saved SONN conversation to manage a bounded task')
    return _sessions_dir(project_path) / (session_id + '.sonn-task.sqlite')


def restore_task(backend, project_path: str, session_id: str):
    """Restore before generation. Invalid saved setup must never fall back to unbounded calls."""
    if not isinstance(backend, SonnBackend):
        return None
    path = journal_path(project_path, session_id)
    if not path.exists():
        if backend.task_controller is not None:
            raise SonnTaskError('This backend still belongs to another task; reopen the intended conversation')
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            row = connection.execute('SELECT value FROM task WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else {}
        if state.get('detached'):
            backend.task_controller = None
            return None
        config = state['configuration']
        bind_sonn_conversation(backend, project_path, session_id)
        controller = SonnTaskController(backend, path, ceiling_microusd=config['ceiling_microusd'],
                                        descriptor=config['descriptor'])
        backend.task_controller = controller
        return controller
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        raise SonnTaskError('Saved employee task needs recovery before this conversation can generate again') from exc


def configure(backend, project_path: str, session_id: str, *, ceiling_microusd: int, descriptor: dict):
    if not isinstance(backend, SonnBackend):
        raise SonnTaskError('Choose the SONN backend for this conversation')
    path = journal_path(project_path, session_id)
    bind_sonn_conversation(backend, project_path, session_id)
    if path.exists():
        controller = restore_task(backend, project_path, session_id)
        if controller is None:
            raise SonnTaskError('This conversation already has a closed task; start a fresh conversation for another')
        controller = controller.revise_unstarted(ceiling_microusd=ceiling_microusd, descriptor=descriptor)
    else:
        controller = SonnTaskController(backend, path, ceiling_microusd=ceiling_microusd, descriptor=descriptor)
    backend.task_controller = controller  # Attach before networking; interruptions stay bounded.
    controller.start()
    return controller


def public_state(backend, project_path: str, session_id: str):
    if not isinstance(backend, SonnBackend):
        return {'available': False, 'message': 'Choose a saved SONN conversation to manage a bounded employee task.'}
    controller = restore_task(backend, project_path, session_id)
    if controller is None:
        return {'available': True, 'task': None, 'used': journal_path(project_path, session_id).exists()}
    local = controller.state()
    task = checked_view(controller)
    # Explicit projection; never send credentials, local advice text, grants,
    # private journal paths, or arbitrary server records to the browser.
    return {'available': True, 'task': {key: task.get(key) for key in (
        'id', 'status', 'deadline', 'deadline_passed', 'ceiling_microusd', 'call_limit', 'advisory_limit',
        'employee_id', 'unresolved_attempts', 'unresolved_preflights')} | {'quota': {
            key: (task.get('quota') or {}).get(key, 0) for key in
            ('charged_microusd', 'allocated_microusd', 'claimed_calls', 'claimed_advisory')}},
        'pending': bool(local.get('pending')), 'advice_status': (local.get('advice') or {}).get('status'),
        'configuration': local['configuration']}


def detach(controller):
    """Explicitly return to normal per-request work after closing the old root."""
    controller.recover()
    task = checked_view(controller)
    if (task['status'] not in ('cancelled', 'completed') and not task.get('deadline_passed')
            or task.get('unresolved_attempts') or task.get('unresolved_preflights')
            or task.get('quota', {}).get('allocated_microusd')):
        raise SonnTaskError('Close the task and reconcile every original request before returning to ordinary requests')
    controller._update(lambda state: {**state, 'detached': True})
    controller.backend.task_controller = None


def checked_view(controller):
    task = controller.view()
    quota = task.get('quota')
    fields = ('allocated_microusd', 'charged_microusd', 'claimed_calls', 'claimed_advisory')
    if (task.get('id') != controller.state()['root_id'] or not isinstance(quota, dict)
            or any(type(quota.get(key)) is not int or quota[key] < 0 for key in fields)
            or any(type(task.get(key)) is not int or task[key] < 0 for key in ('unresolved_attempts', 'unresolved_preflights'))):
        raise SonnTaskError('Task accounting is incomplete; retain the original task and reconcile before continuing')
    return task


def recover_all(controller):
    """Reconcile saved root attempts, including specialists, without model retries."""
    task = checked_view(controller)
    failures = []
    attempts = task.get('attempts')
    if not isinstance(attempts, list) or len(attempts) > 128:
        raise SonnTaskError('Root recovery requires its bounded original attempt list')
    for attempt in attempts:
        if attempt.get('status') in ('allocated', 'reserved', 'refusing', 'dispatching', 'completed'):
            try:
                controller._request('POST', '/' + task['id'] + '/recover', {'attempt_id': attempt['id']})
            except SonnTaskError as exc:
                failures.append(exc)
    try:
        controller.recover()
    except SonnTaskError as exc:
        failures.append(exc)
    if failures:
        raise SonnTaskError('Some original requests remain unresolved. Known receipts were reconciled; refresh task status before continuing.')


def public_graph(controller):
    """Expose assignment contracts and recovery state, never worker capabilities."""
    graph = controller.graph().get('graph')
    if graph is None:
        return None
    nodes = graph.get('nodes') if isinstance(graph, dict) else None
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 8:
        raise SonnTaskError('Assignment graph is incomplete; refresh before continuing')
    result = []
    for key, node in nodes.items():
        if (not isinstance(node, dict) or node.get('id') != key
                or node.get('status') not in ('waiting', 'running', 'reported', 'failed', 'succeeded')
                or not isinstance(node.get('dependencies'), list) or len(node['dependencies']) > 7
                or any(dependency not in nodes for dependency in node['dependencies'])
                or not isinstance(node.get('outputs'), list) or len(node['outputs']) > 8):
            raise SonnTaskError('Assignment graph is incomplete; refresh before continuing')
        expired = node['status'] == 'running' and time.time() >= node.get('lease_until', 0)
        pending_tools = sum(action.get('status') != 'reported' for action in node.get('tool_actions', {}).values()
                            if action.get('identity', {}).get('epoch') == node.get('epoch'))
        result.append({field: node.get(field) for field in
            ('id', 'employee_id', 'input_contract', 'outputs', 'dependencies', 'integration', 'status', 'epoch')} | {
                'lease_expired': expired, 'unresolved_file_actions': pending_tools,
                'prior_executions': len(node.get('history', [])),
                'blocked_by': [dependency for dependency in node['dependencies'] if nodes[dependency]['status'] != 'succeeded']})
    return {'status': graph.get('status'), 'root_status': graph.get('root_status'),
            'deadline_passed': graph.get('deadline_passed'), 'nodes': result}


async def command(state, send, message):
    """Run through the chat queue so setup/advice cannot race ordinary execution."""
    import asyncio
    import os
    project = state.project.project_path
    record = state.project.current_session
    session_id = record.id if record else ''
    reply = {'event': 'employee_task_state', 'project': project, 'session_id': session_id,
             'request_id': str(message.get('request_id') or '')}
    try:
        if (not session_id or message.get('session_id') != session_id
                or os.path.normcase(os.path.abspath(str(message.get('project') or ''))) != os.path.normcase(os.path.abspath(project))):
            raise SonnTaskError('The selected conversation changed; reopen its employee task panel')
        if state.session is None:
            raise SonnTaskError('Open a saved SONN conversation before changing its task')
        backend = state.session.backend
        action = message.get('action', 'view')
        def operate():
            assignments = {}
            if action == 'start':
                configure(backend, project, session_id, ceiling_microusd=message.get('ceiling_microusd'),
                          descriptor=message.get('descriptor', {}))
            elif action != 'view':
                controller = restore_task(backend, project, session_id)
                if controller is None:
                    raise SonnTaskError('This conversation has no active bounded task')
                if action == 'recover':
                    recover_all(controller)
                elif action == 'graph':
                    assignments['graph'] = public_graph(controller)
                elif action == 'cancel':
                    controller.cancel()
                elif action == 'advice':
                    controller.ask_advice(message.get('question'), mode=message.get('consultation_mode', 'advise'))
                elif action == 'detach':
                    detach(controller)
                else:
                    raise SonnTaskError('Unknown employee task action')
            return public_state(backend, project, session_id) | assignments
        reply.update(await asyncio.to_thread(operate))
    except (SonnTaskError, ValueError, OSError, sqlite3.Error) as exc:
        reply.update(error=str(exc), available=False)
    await send(reply)
