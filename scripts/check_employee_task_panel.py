"""Real browser events against the native task panel and Python controller.

The browser bridge and upstream provider are scripted. This is component evidence,
not a packaged desktop or live-model qualification run. No installed secrets load.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

import httpx
from playwright.sync_api import sync_playwright, expect


def run(output):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from lumi.sonn import SonnBackend
    from lumi.gui import employee_tasks
    import pytest
    patch = pytest.MonkeyPatch()
    requests, errors = [], []
    task = {'id': 'task_' + 'a' * 40, 'status': 'active', 'ceiling_microusd': 2000000,
        'deadline': time.time() + 900, 'deadline_passed': False, 'call_limit': 8, 'advisory_limit': 2,
        'quota': {'allocated_microusd': 0, 'charged_microusd': 0, 'claimed_calls': 0, 'claimed_advisory': 0},
        'unresolved_attempts': 0, 'unresolved_preflights': 0, 'attempts': []}
    def http(request):
        requests.append(request)
        path = request.url.path
        body = json.loads(request.content or '{}')
        if path.endswith('/graph'):
            specialist = {'id': 'specialist_node', 'employee_id': 'employee_specialist', 'status': 'reported',
                'dependencies': [], 'outputs': ['schema.json'], 'input_contract': '<script>throw Error("untrusted")</script>',
                'epoch': 1, 'integration': False, 'worker': {'token': 'private-worker-capability'}}
            integration = {'id': 'integration_node', 'employee_id': 'employee_coordinator', 'status': 'waiting',
                'dependencies': ['specialist_node'], 'outputs': ['integration.json'], 'input_contract': 'Verify combined result',
                'epoch': 0, 'integration': True}
            return httpx.Response(200, json={'graph': {'status': 'active', 'root_status': task['status'], 'deadline_passed': False,
                'nodes': {'specialist_node': specialist, 'integration_node': integration}}})
        if path.endswith('/tasks') and body.get('ceiling_microusd', 0) > 2000000:
            return httpx.Response(422, json={'error': {'code': 'task_quota'}})
        if path.endswith('/request-status'):
            return httpx.Response(200, json={'status': 'settled', 'attempt_id': 'attempt_' + 'b' * 40})
        if path.endswith('/cancel'):
            task['status'] = 'cancelled'
        if path.endswith('/chat/completions'):
            task['quota']['claimed_calls'] += 1
            task['quota']['charged_microusd'] += 10000
            if body.get('metadata', {}).get('sonn_task_mode') in ('advise', 'coordinate'):
                task['quota']['claimed_advisory'] += 1
                return httpx.Response(200, json={'id': 'chatcmpl-advice_fixture', 'choices': [{'message': {'content': 'Check the recovery invariant before changing the code.'}}]})
            frame = {'id': 'response_fixture', 'choices': [{'index': 0, 'delta': {'content': 'Following execution fixture.'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=task)
    transport = httpx.MockTransport(http)
    def backend():
        return SonnBackend('local-component-secret', base_url='https://sonn.example/v1/workspace/projects/project_test/openai/v1', transport=transport)
    output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='sonn-task-panel-') as directory, sync_playwright() as play:
            patch.setattr(employee_tasks, '_sessions_dir', lambda _: Path(directory) / 'private')
            state = SimpleNamespace(project=SimpleNamespace(project_path=str(Path(directory) / 'project'), current_session=SimpleNamespace(id='saved_session')),
                                    session=SimpleNamespace(backend=backend()))
            browser = play.chromium.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><body><button id="employee-task-button">Employee task</button></body></html>'))
            def load():
                page.goto('http://127.0.0.1/component')
                page.add_style_tag(path=str(root / 'lumi/gui/static/styles.css'))
                page.add_script_tag(path=str(root / 'lumi/gui/static/employee_tasks.js'))
                page.evaluate('''project => {
                    window.messages = [];
                    window.app = new window.ResonantEmployeeTasks();
                    Object.assign(app, {currentSessionId: 'saved_session', currentBackendName: 'sonn', currentCwd: project,
                        _normalizeProjectPath: value => String(value).replaceAll('\\\\', '/').toLowerCase(),
                        send: value => messages.push(value)});
                    app.bindEmployeeTaskPanel();
                }''', state.project.project_path)
            def pump(*, expect_error=False):
                message = page.evaluate('messages.shift()')
                assert message
                replies = []
                async def send(value): replies.append(value)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(lambda: asyncio.run(employee_tasks.command(state, send, message))).result(timeout=10)
                assert len(replies) == 1 and ('error' in replies[0]) == expect_error, replies
                page.evaluate('value => app.receiveEmployeeTaskState(value)', replies[0])
            load()
            page.get_by_role('button', name='Employee task', exact=True).click()
            pump()
            page.get_by_label('Total task allowance (USD)').fill('3')
            page.get_by_label('Task complexity').select_option('complex')
            page.get_by_role('button', name='Start bounded task').click()
            pump(expect_error=True)
            expect(page.get_by_label('Total task allowance (USD)')).to_have_value('3')
            page.get_by_label('Total task allowance (USD)').fill('2')
            page.get_by_role('button', name='Start bounded task').click()
            pump()
            expect(page.get_by_text('Status: active', exact=True)).to_be_visible()
            assert json.loads(requests[0].content)['descriptor']['dependency_breadth'] == 6
            page.screenshot(path=str(output / 'native-task-desktop.png'))
            page.get_by_label('Question for the frontier advisor').fill('How should I verify the recovery invariant?')
            page.get_by_label('Consultation purpose').select_option('coordinate')
            page.get_by_role('button', name='Consult frontier advisor').click()
            pump()
            assert json.loads([request for request in requests if request.url.path.endswith('/chat/completions')][-1].content)['metadata']['sonn_task_mode'] == 'coordinate'
            expect(page.get_by_text('Recorded advice is ready for the next execution. Compression will not consume it.')).to_be_visible()
            expect(page.get_by_role('button', name='Consult frontier advisor')).to_be_disabled()
            # Recreate native backend and browser; only the private SQLite journal survives.
            state.session.backend = backend()
            load()
            page.get_by_role('button', name='Employee task', exact=True).click()
            pump()
            assert state.session.backend.task_controller.state()['advice']['status'] == 'awaiting'
            list(state.session.backend.stream(user_msg='Continue the assigned work', conversation_history=[], instructions='Fixture', tools=[], max_tokens=64))
            execution = json.loads([r for r in requests if r.url.path.endswith('/chat/completions')][-1].content)
            assert execution['metadata']['sonn_after_advice'] == 'chatcmpl-advice_fixture'
            page.get_by_role('button', name='Refresh task').click()
            pump()
            expect(page.get_by_text('Calls used or reserved: 2/8. Consultations: 1/2.')).to_be_visible()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'native-task-mobile.png'))
            page.get_by_role('button', name='Inspect assignments', exact=True).click()
            pump()
            expect(page.get_by_text('Output reported. Independent verification is still required.')).to_be_visible()
            expect(page.get_by_text('Waiting for: specialist_node.')).to_be_visible()
            page.get_by_text('Assignment contract', exact=True).first.click()
            expect(page.get_by_text('<script>throw Error("untrusted")</script>', exact=True)).to_be_visible()
            assert 'private-worker-capability' not in page.content()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'native-assignments-mobile.png'))
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.screenshot(path=str(output / 'native-assignments-desktop.png'))
            page.get_by_role('button', name='Cancel task', exact=True).click()
            pump()
            page.get_by_role('button', name='Return to ordinary requests').click()
            pump()
            expect(page.get_by_text('This task is closed and the conversation uses ordinary per-request limits. Start a fresh conversation for another bounded task.')).to_be_visible()
            assert state.session.backend.task_controller is None
            assert 'local-component-secret' not in page.content()
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog', name='Employee task')).to_have_count(0)
            assert errors == [], errors
            browser.close()
            report = {'schema': 'sonn.native-task-panel-check.v1', 'passed': True, 'paid_calls': 0,
                'checks': ['start with explicit allowance', 'failed setup preserves form and permits corrected retry', 'declared complexity', 'bounded coordination consultation', 'backend and browser restart',
                           'one following execution link', 'assignment inspector', 'unverified and blocked dependency status', 'untrusted contract rendered as text',
                           'cancel', 'explicit return to ordinary requests', 'mobile width', 'keyboard close', 'no credential in DOM'],
                'limitations': ['Native component and controller with a scripted bridge/provider; not the packaged desktop or full WebSocket app.']}
            (output / 'native-task-panel-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(report))
    finally:
        patch.undo()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
