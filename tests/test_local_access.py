"""The GUI server serves its socket and private endpoints only to this launch's pages.

Loopback is not a trust boundary: other local accounts, sandboxed processes
and any web page in the user's browser can reach 127.0.0.1. The socket runs
shell commands and edits settings, so each request must name this server in
Host, come from its Origin and carry the launch token.
See resonant_client/gui/local_access.py.
"""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from resonant_client.gui import app as gui
from resonant_client.gui.local_access import (
    ACCESS_HEADER,
    LAUNCH_FRAGMENT,
    WS_ACCESS_PREFIX,
    WS_PROTOCOL,
    WS_REFUSED,
    LocalAccess,
    access,
    allowed_hosts,
    same_origin,
)
from resonant_client.gui.server import _client_host
from tests.gui_access import BASE_URL, LocalClient, app_protocols, launch_token

WS_URL = "ws" + BASE_URL.removeprefix("http") + "/ws"
FOREIGN = "http://evil.example:48123"


@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    """A fresh app state with a project and no provider discovery."""
    from resonant_client.gui import sessions

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sessions, "_is_pytest_temp_path", lambda _: False)
    project = tmp_path / "project"
    project.mkdir()
    state = gui.AppState()
    state.project.set_project(str(project))
    state.available_backends = {"test": {}}
    state.codebase_index = object()
    monkeypatch.setattr(gui, "state", state)
    return state


def _handshake(headers, protocols):
    """Open /ws with exactly these headers: the accepted subprotocol, or the refusal code."""
    with TestClient(gui.app, base_url=BASE_URL) as client:
        try:
            with client.websocket_connect(WS_URL, subprotocols=protocols, headers=headers) as ws:
                return ws.accepted_subprotocol
        except WebSocketDisconnect as refused:
            return refused.code


# ── WebSocket handshake ─────────────────────────────────────────────────


def test_websocket_accepts_this_launchs_page_without_echoing_the_token(gui_state):
    token = launch_token()

    assert _handshake({"Origin": BASE_URL}, app_protocols(token)) == WS_PROTOCOL
    assert _handshake({"Origin": "http://localhost:48123", "Host": "localhost:48123"},
                      app_protocols(token)) == WS_PROTOCOL


@pytest.mark.parametrize("case", [
    "no token",
    "wrong token",
    "token without app protocol",
    "no origin",
    "null origin",
    "foreign origin",
    "origin on another local port",
    "foreign host",
    "host with another port",
    "dns rebinding",
])
def test_websocket_refuses_before_accept(gui_state, case):
    token = launch_token()
    headers = {"Origin": BASE_URL}
    protocols = app_protocols(token)
    if case == "no token":
        protocols = [WS_PROTOCOL]
    elif case == "wrong token":
        protocols = app_protocols("x" * len(token))
    elif case == "token without app protocol":
        protocols = [WS_ACCESS_PREFIX + token]
    elif case == "no origin":
        headers = {}
    elif case == "null origin":
        headers = {"Origin": "null"}
    elif case == "foreign origin":
        headers = {"Origin": "https://evil.example"}
    elif case == "origin on another local port":
        # A project dev server page opened from "Open preview".
        headers = {"Origin": "http://127.0.0.1:3000"}
    elif case == "foreign host":
        headers = {"Origin": BASE_URL, "Host": "evil.example:48123"}
    elif case == "host with another port":
        headers = {"Origin": BASE_URL, "Host": "127.0.0.1:9999"}
    elif case == "dns rebinding":
        # The browser believes it is talking to the attacker's name.
        headers = {"Origin": FOREIGN, "Host": "evil.example:48123"}

    assert _handshake(headers, protocols) == WS_REFUSED
    # Refused before accept(): the connection never reached the run loop.
    assert getattr(gui_state, "_chat_run_loop", None) is None


def test_reconnect_with_the_same_token_reattaches_the_run_loop(gui_state):
    with LocalClient(gui.app) as client:
        with client.websocket_connect("/ws") as first:
            first.send_json({"command": "get_costs"})
            assert first.receive_json()["event"] == "costs"
        runs = gui_state._chat_run_loop
        assert runs.ws is None  # the viewer detached; the run loop lives on

        # A page reload reconnects with the token it stored when it first loaded.
        with client.websocket_connect("/ws") as second:
            second.send_json({"command": "get_costs"})
            assert second.receive_json()["event"] == "costs"
            assert gui_state._chat_run_loop is runs
            assert runs.ws is not None


# ── /api/ui-state ────────────────────────────────────────────────────────


def _ui_state(method, headers):
    with TestClient(gui.app, base_url=BASE_URL) as client:
        if method == "GET":
            return client.get("/api/ui-state", headers=headers).status_code
        return client.post("/api/ui-state", json={"sidebar": {"desktop": True}}, headers=headers).status_code


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_ui_state_serves_this_launchs_page(gui_state, method):
    token = launch_token()
    assert _ui_state(method, {"Origin": BASE_URL, ACCESS_HEADER: token}) == 200


def test_ui_state_read_accepts_same_origin_fetch_without_origin(gui_state):
    # Browsers omit Origin on same-origin GET requests.
    assert _ui_state("GET", {ACCESS_HEADER: launch_token()}) == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("case", ["no token", "wrong token", "foreign origin", "foreign host"])
def test_ui_state_refuses_other_callers(gui_state, method, case):
    token = launch_token()
    headers = {"Origin": BASE_URL, ACCESS_HEADER: token}
    if case == "no token":
        del headers[ACCESS_HEADER]
    elif case == "wrong token":
        headers[ACCESS_HEADER] = "x" * len(token)
    elif case == "foreign origin":
        headers["Origin"] = "https://evil.example"
    elif case == "foreign host":
        headers["Host"] = "evil.example:48123"

    assert _ui_state(method, headers) == 403


def test_ui_state_write_requires_origin(gui_state):
    assert _ui_state("POST", {ACCESS_HEADER: launch_token()}) == 403


def test_rebinding_read_is_refused_by_host(gui_state):
    # A rebound page's own fetches are same-origin to the browser, so they
    # carry no Origin; only Host shows they are not for this server.
    assert _ui_state("GET", {ACCESS_HEADER: launch_token(), "Host": "evil.example:48123"}) == 403


def test_endpoint_checks_host_without_the_middleware():
    from starlette.requests import Request

    def request(host, origin=None):
        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        return Request({"type": "http", "method": "GET", "path": "/", "scheme": "http",
                        "server": ("127.0.0.1", 48123), "headers": headers})

    assert same_origin(request("127.0.0.1:48123"), require_origin=False)
    assert same_origin(request("LOCALHOST:48123", "http://localhost:48123"), require_origin=True)
    assert not same_origin(request("evil.example:48123"), require_origin=False)
    assert not same_origin(request("evil.example:48123", BASE_URL), require_origin=True)
    assert not same_origin(request("127.0.0.1:9999", BASE_URL), require_origin=True)
    assert not same_origin(request("127.0.0.1:48123"), require_origin=True)


# ── Launch code exchange ─────────────────────────────────────────────────


def test_launch_code_is_redeemed_once_for_a_working_token(gui_state):
    code = access.issue_launch_code()
    with TestClient(gui.app, base_url=BASE_URL) as client:
        redeemed = client.post("/api/access", json={"code": code}, headers={"Origin": BASE_URL})
        assert redeemed.status_code == 200
        assert redeemed.headers["cache-control"] == "no-store"
        token = redeemed.json()["token"]

        again = client.post("/api/access", json={"code": code}, headers={"Origin": BASE_URL})
        assert again.status_code == 403
        assert "token" not in again.json()

        assert client.get("/api/access", headers={ACCESS_HEADER: token}).status_code == 204
        assert client.get("/api/access", headers={ACCESS_HEADER: "x"}).status_code == 403
        assert client.get("/api/access").status_code == 403
        assert client.get("/api/ui-state", headers={ACCESS_HEADER: token}).status_code == 200


@pytest.mark.parametrize("case", ["no origin", "foreign origin", "foreign host", "not json"])
def test_launch_code_exchange_refuses_other_callers(gui_state, case):
    code = access.issue_launch_code()
    headers = {"Origin": BASE_URL}
    kwargs = {"json": {"code": code}}
    if case == "no origin":
        headers = {}
    elif case == "foreign origin":
        headers = {"Origin": "https://evil.example"}
    elif case == "foreign host":
        headers = {"Origin": BASE_URL, "Host": "evil.example:48123"}
    elif case == "not json":
        kwargs = {"content": f'{{"code": "{code}"}}', "headers": {"Content-Type": "text/plain"}}
        headers.update(kwargs.pop("headers"))

    with TestClient(gui.app, base_url=BASE_URL) as client:
        response = client.post("/api/access", headers=headers, **kwargs)
        assert response.status_code == (415 if case == "not json" else 403)
        # A refused exchange leaves the code unspent for the real page.
        assert client.post("/api/access", json={"code": code},
                           headers={"Origin": BASE_URL}).status_code == 200


# ── Page and static assets ───────────────────────────────────────────────


def test_rebinding_hosts_cannot_load_the_page_or_assets(gui_state):
    with TestClient(gui.app, base_url=BASE_URL) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        assert client.get("/", headers={"Host": "evil.example:48123"}).status_code == 403
        assert client.get("/static/app.js", headers={"Host": "evil.example:48123"}).status_code == 403


# ── Access primitives ────────────────────────────────────────────────────


def test_launch_link_carries_the_code_only_in_the_fragment():
    local = LocalAccess()
    parts = urlsplit(local.launch_url("http://127.0.0.1:5000/"))

    assert (parts.netloc, parts.path, parts.query) == ("127.0.0.1:5000", "/", "")
    name, _, code = parts.fragment.partition("=")
    assert name == LAUNCH_FRAGMENT
    assert local.redeem(code)
    assert local.redeem(code) is None


def test_codes_are_single_use_and_bounded():
    local = LocalAccess()
    first = local.issue_launch_code()
    later = [local.issue_launch_code() for _ in range(16)]

    assert local.redeem(first) is None  # oldest unredeemed code dropped
    assert local.redeem(later[0]) == local.redeem(later[-1])  # one token per launch
    for bad in (None, "", 7, ["code"], later[0]):
        assert local.redeem(bad) is None


def test_token_comparison_tolerates_any_input():
    local = LocalAccess()
    token = local.redeem(local.issue_launch_code())

    assert local.token_valid(token)
    for bad in (None, "", token[:-1], token + "x", "\udcff", "é" * 43, b"bytes", 12):
        assert not local.token_valid(bad)
    assert not LocalAccess().token_valid(token)  # tokens are per launch


def test_allowed_hosts_name_the_listening_socket():
    assert allowed_hosts({"server": ("127.0.0.1", 5000)}) == {"127.0.0.1:5000", "localhost:5000"}
    assert "10.0.0.5:5000" in allowed_hosts({"server": ("10.0.0.5", 5000)})
    assert "[::1]:5000" in allowed_hosts({"server": ("::1", 5000)})
    assert allowed_hosts({"server": None}) == frozenset()
    assert allowed_hosts({"server": ("/tmp/gui.sock", None)}) == frozenset()


def test_wildcard_binds_print_a_reachable_loopback_url():
    assert _client_host("0.0.0.0") == "127.0.0.1"
    assert _client_host("") == "127.0.0.1"
    assert _client_host("::") == "::1"
    assert _client_host("127.0.0.1") == "127.0.0.1"
    assert _client_host("10.0.0.5") == "10.0.0.5"
