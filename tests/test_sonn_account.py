"""SONN account contract from sonn_server.workspace and sonn_billing.service."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.sonn_account import read_account, workspace_url

BASE = 'https://sonn.example/v1/workspace/projects/project-fixture/openai/v1'
KEY = 'fixture-private-invitation'


def payload():
    return {'identity': {'user': 'user-fixture', 'learning_operator': True},
            'billing': {'enabled': True, 'available_credit_microusd': 12345678,
                        'reserved_credit_microusd': 2000000, 'total_charged_microusd': 5000000},
            'checkout': {'mode': 'test'}, 'projects': [{'name': 'Private project'}]}


def test_account_read_uses_workspace_auth_and_excludes_private_extra_fields():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload())
    result = read_account(KEY, base_url=BASE, transport=httpx.MockTransport(handle))
    assert len(requests) == 1
    assert str(requests[0].url) == 'https://sonn.example/v1/workspace'
    assert requests[0].headers['authorization'] == 'Bearer ' + KEY
    assert result['user'] == 'user-fixture'
    assert result['billing']['available_credit_microusd'] == 12345678
    assert result['checkout_mode'] == 'test'
    assert 'projects' not in result and 'learning_operator' not in repr(result)
    assert KEY not in repr(result)


@pytest.mark.parametrize('status', [301, 302, 401, 403, 404, 500])
def test_errors_do_not_follow_redirects_or_echo_server_secrets(status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://other.example'}, text=KEY)
    with pytest.raises(ValueError) as caught:
        read_account(KEY, base_url=BASE, transport=httpx.MockTransport(handle))
    assert KEY not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize('url', ['https://sonn.example/openai/v1', 'https://user:pass@sonn.example/v1',
                                 BASE+'?key=secret', 'http://sonn.example/v1/workspace/projects/project-fixture/openai/v1'])
def test_account_route_requires_documented_project_path(url):
    with pytest.raises(ValueError):
        workspace_url(url)


def test_loopback_fixture_and_reverse_proxy_prefix_preserve_origin():
    assert workspace_url('http://127.0.0.1:8765/proxy/v1/workspace/projects/project-fixture/openai/v1') == 'http://127.0.0.1:8765/proxy/v1/workspace'


@pytest.mark.parametrize('value', [None, True, 1.5, '2', 2**60])
def test_unavailable_or_unsafe_money_is_not_fabricated_as_zero(value):
    body = payload()
    body['billing']['available_credit_microusd'] = value
    result = read_account(KEY, base_url=BASE, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    assert result['billing']['available_credit_microusd'] is None


@pytest.mark.parametrize('body', [[], {}, {'identity': {}}, {'identity': {'user': KEY}}])
def test_malformed_or_secret_identity_is_rejected(body):
    with pytest.raises(ValueError):
        read_account(KEY, base_url=BASE, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))


def test_stale_account_result_is_discarded_after_credentials_change(monkeypatch):
    from resonant_client.gui.ws_commands import CommandContext, _cmd_sonn_account
    import resonant_client.sonn_account as account_module
    state = SimpleNamespace(sonn_account_revision=0,
        _api_key_details=lambda *_: (KEY, None, None, None),
        settings=SimpleNamespace(get_all=lambda: {'network': {'sonn_url': BASE}}))
    messages = []
    async def send(data):
        messages.append(data)
    def read(*args, **kwargs):
        state.sonn_account_revision += 1
        return {'user': 'obsolete-account'}
    monkeypatch.setattr(account_module, 'read_account', read)
    context = CommandContext(ws=SimpleNamespace(send_json=send), state=state,
                             runs=SimpleNamespace(busy=False), msg={})
    asyncio.run(_cmd_sonn_account(context))
    assert messages == []
