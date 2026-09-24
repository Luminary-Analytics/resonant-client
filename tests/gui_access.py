"""Test clients that pass the GUI server's launch access checks.

The server refuses its WebSocket and private HTTP endpoints unless a request
names the server in Host, comes from the server's own Origin, and carries the
launch's access token (resonant_client/gui/local_access.py). These helpers get
a token the way the page does: by redeeming a one-time launch code.
"""

from __future__ import annotations

from urllib.parse import urljoin

from starlette.testclient import TestClient

from resonant_client.gui.local_access import (
    ACCESS_HEADER,
    WS_ACCESS_PREFIX,
    WS_PROTOCOL,
    access,
)

BASE_URL = "http://127.0.0.1:48123"


def launch_token() -> str:
    token = access.redeem(access.issue_launch_code())
    assert token
    return token


def app_protocols(token: str) -> list[str]:
    """The WebSocket subprotocols the app page offers."""
    return [WS_PROTOCOL, WS_ACCESS_PREFIX + token]


class LocalClient(TestClient):
    """A TestClient that presents the launch token the way the app page does.

    Requests go to a loopback base URL with a matching Origin. Starlette's
    ``websocket_connect`` resolves relative URLs against ws://testserver, so
    they are made absolute here to keep Host and Origin consistent.
    """

    def __init__(self, app, base_url: str = BASE_URL, **kwargs):
        self.token = launch_token()
        self.origin = base_url
        headers = {"Origin": base_url, ACCESS_HEADER: self.token, **kwargs.pop("headers", {})}
        super().__init__(app, base_url=base_url, headers=headers, **kwargs)

    def websocket_url(self, path: str = "/ws") -> str:
        return urljoin("ws" + self.origin.removeprefix("http"), path)

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        if subprotocols is None:
            subprotocols = app_protocols(self.token)
        return super().websocket_connect(self.websocket_url(url), subprotocols=subprotocols, **kwargs)
