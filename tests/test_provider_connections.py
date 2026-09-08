"""Account protocol, explicit project defaults, and safe provider changes."""
import asyncio
import io
import json
import os
from types import SimpleNamespace

from resonant_client.codex_account import CodexAccount
from resonant_client.gui.settings import SettingsManager
from resonant_client.gui.ws_commands import CommandContext, _cmd_provider_connection, _cmd_switch_model


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def test_account_stdio_handshake_pagination_usage_and_cleanup(monkeypatch):
    import resonant_client.codex_account as module
    replies = [
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'account': {'type': 'chatgpt', 'planType': 'plus'}}},
        {'id': 3, 'result': {'data': [{'id': 'one'}], 'nextCursor': 'next'}},
        {'id': 4, 'result': {'data': [{'id': 'two'}], 'nextCursor': None}},
        {'id': 5, 'result': {'rateLimits': {'primary': {'usedPercent': 20}}}},
    ]
    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO('\n'.join(json.dumps(row) for row in replies))
            self.stopped = False
        def poll(self):
            return 0 if self.stopped else None
        def terminate(self):
            self.stopped = True
        def wait(self, **kwargs):
            return 0
    proc = Process()
    monkeypatch.setattr(module, 'resolve_codex_cli_path', lambda: 'codex-fixture')
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: proc)
    client = CodexAccount()
    result = client.status()
    assert [model['id'] for model in result['models']] == ['one', 'two']
    assert result['rate_limits']['rateLimits']['primary']['usedPercent'] == 20
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [row['method'] for row in sent][:3] == ['initialize', 'initialized', 'account/read']
    assert sent[4]['params']['cursor'] == 'next'
    client.close()
    assert proc.stopped and proc.stdin.closed and proc.stdout.closed


def test_account_login_can_be_cancelled_without_logging_out(monkeypatch):
    client = CodexAccount()
    client._process = object()
    calls = []
    monkeypatch.setattr(client, '_start', lambda: None)
    def rpc(method, params=None):
        calls.append((method, params))
        return {'loginId': 'one', 'authUrl': 'https://auth.openai.com/sign-in'}
    monkeypatch.setattr(client, '_rpc', rpc)
    assert client.login() == {'auth_url': 'https://auth.openai.com/sign-in'}
    client.cancel_login()
    assert calls[-1] == ('account/login/cancel', {'loginId': 'one'})
    assert not any(method == 'account/logout' for method, _ in calls)


def test_account_response_is_allowlisted_before_sending_to_browser(monkeypatch):
    from resonant_client.codex_account import codex_account
    monkeypatch.setattr(codex_account, 'status', lambda: {
        'account': {'type': 'chatgpt', 'email': 'a@example.com', 'planType': 'plus', 'unexpected_secret': 'secret'},
        'models': [{'id': 'model', 'displayName': 'Model'}], 'rate_limits': None,
    })
    state = SimpleNamespace(available_backends={}, get_init_data=lambda **kw: {'event': 'init'})
    ctx = CommandContext(ws=Socket(), state=state, msg={'provider': 'codex'}, runs=SimpleNamespace(busy=False))
    asyncio.run(_cmd_provider_connection(ctx))
    assert 'secret' not in json.dumps(ctx.ws.sent)
    assert state.available_backends['codex']['models'] == ['model']


def test_session_override_does_not_replace_project_default(tmp_path):
    settings = SettingsManager(tmp_path / 'settings.json')
    changes = []
    state = SimpleNamespace(
        settings=settings, backend=None, CLI_WRAPPED_BACKENDS={'codex'},
        project=SimpleNamespace(project_path=str(tmp_path), current_session=None),
        swap_backend=lambda backend, model, **kw: changes.append((backend, model)),
        get_init_data=lambda **kw: {'event': 'init'},
    )
    ctx = CommandContext(ws=Socket(), state=state, msg={
        'backend': 'openrouter', 'model': 'vendor/coder', 'remember_project': True,
    }, runs=SimpleNamespace(busy=False))
    asyncio.run(_cmd_switch_model(ctx))
    ctx.msg = {'backend': 'codex', 'model': 'other'}
    asyncio.run(_cmd_switch_model(ctx))
    key = os.path.normcase(os.path.abspath(tmp_path))
    assert settings.get('project_models', key) == {'backend': 'openrouter', 'model': 'vendor/coder'}
    assert changes == [('openrouter', 'vendor/coder'), ('codex', 'other')]
    assert not any(item.get('event') == 'backend_swap_warning' for item in ctx.ws.sent)
    ctx.runs.busy = True
    asyncio.run(_cmd_switch_model(ctx))
    assert len(changes) == 2
    assert ctx.ws.sent[-2]['event'] == 'model_switch_blocked'
    assert 'Finish or stop' in ctx.ws.sent[-2]['message']
