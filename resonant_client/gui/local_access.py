"""
Per-launch access control for the local GUI server.

Binding to 127.0.0.1 keeps the server off the network, but loopback is not a
trust boundary. Other accounts on the machine and sandboxed local processes
can connect to it, and any web page in the user's browser can open a WebSocket
to it (WebSockets are exempt from CORS) or reach it through DNS rebinding. The
socket runs shell commands, edits settings and answers permission prompts, so
a privileged request must show three things:

* ``Host`` names this server exactly: ``127.0.0.1:<port>``, ``localhost:<port>``
  or the literal address of a non-loopback bind. DNS rebinding cannot satisfy
  this, because the browser still sends the attacker's host name.
* ``Origin`` is this server's own origin. Browsers always send it on WebSocket
  handshakes and POSTs, where it is required; they omit it on same-origin GETs,
  where only a foreign value is refused.
* The launch's access token, attached explicitly: an ``X-SONN-Access`` header
  on HTTP requests or a ``sonn.access.<token>`` WebSocket subprotocol.

The token is created per process. The server never prints, logs or stores it.
A page obtains it by redeeming a one-time launch code carried in the URL
fragment (``#sonn-launch=<code>``). Browsers do not send fragments to servers,
so the code cannot reach access logs or ``Referer`` headers, and a printed or
remembered link is useless after its first use.

The page keeps the token in its origin's storage rather than in a cookie.
Cookies are not isolated by port (RFC 6265, section 8.5): a cookie for
127.0.0.1 would also be sent to every other local server, including the project
dev servers that "Open preview" links to. Origin storage is per port, and a
token that must be attached explicitly also rules out cross-site request
forgery.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import OrderedDict
from typing import Optional

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

ACCESS_HEADER = "x-sonn-access"
LAUNCH_FRAGMENT = "sonn-launch"
WS_PROTOCOL = "sonn.v1"
WS_ACCESS_PREFIX = "sonn.access."
# WebSocket close code for a refused handshake: RFC 6455 "policy violation".
# Closing before accept() makes the ASGI server answer the upgrade with 403.
WS_REFUSED = 1008

# Unredeemed codes are few: one for the desktop window, one printed in browser
# mode, and one per "Open in Browser". Bounding them keeps a page that mints
# codes repeatedly from growing this set; the oldest code is dropped first.
_MAX_PENDING_CODES = 16

_LOOPBACK_NAMES = ("127.0.0.1", "localhost")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _host_label(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def allowed_hosts(scope: Scope) -> frozenset[str]:
    """Host header values that name the server this connection reached.

    The port and address come from the listening socket (``scope["server"]``),
    not from anything the client sent. A connection without a known TCP
    address is refused.
    """
    server = scope.get("server")
    if not server or len(server) < 2 or not isinstance(server[1], int):
        return frozenset()
    host, port = str(server[0] or "").lower(), server[1]
    names = {f"{name}:{port}" for name in _LOOPBACK_NAMES}
    if host and host not in _LOOPBACK_NAMES:
        names.add(f"{_host_label(host)}:{port}")
    return frozenset(names)


def _allowed_origins(scope: Scope) -> frozenset[str]:
    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    return frozenset(f"{scheme}://{host}" for host in allowed_hosts(scope))


def same_origin(conn: HTTPConnection, *, require_origin: bool) -> bool:
    """True when ``Host`` names this server and ``Origin`` is its own.

    ``require_origin`` is for WebSocket handshakes and state-changing requests,
    where browsers always send ``Origin``.
    """
    host = conn.headers.get("host", "").strip().lower()
    if host not in allowed_hosts(conn.scope):
        return False
    origin = conn.headers.get("origin")
    if origin is None:
        return not require_origin
    return origin.strip().lower().rstrip("/") in _allowed_origins(conn.scope)


class LocalAccess:
    """The per-process access token and its one-time launch codes."""

    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(32)
        self._codes: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def issue_launch_code(self) -> str:
        """Create a code that one page can redeem once for the access token."""
        code = secrets.token_urlsafe(24)
        with self._lock:
            # Only a digest is held, so a memory dump of this set cannot be
            # replayed; the plaintext code exists only in the launch link.
            self._codes[_digest(code)] = None
            while len(self._codes) > _MAX_PENDING_CODES:
                self._codes.popitem(last=False)
        return code

    def launch_url(self, base_url: str) -> str:
        """A one-time link to the app page for ``base_url`` (scheme://host:port)."""
        return f"{base_url.rstrip('/')}/#{LAUNCH_FRAGMENT}={self.issue_launch_code()}"

    def redeem(self, code: object) -> Optional[str]:
        """Consume a launch code and return the access token, or None."""
        if not isinstance(code, str) or not code:
            return None
        key = _digest(code)
        with self._lock:
            if key not in self._codes:
                return None
            del self._codes[key]
        return self._token

    def token_valid(self, candidate: object) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(
            candidate.encode("utf-8", "surrogatepass"),
            self._token.encode("utf-8"),
        )

    def authorized(self, conn: HTTPConnection, token: object, *, require_origin: bool) -> bool:
        """Same-origin request (see :func:`same_origin`) that carries the token."""
        return same_origin(conn, require_origin=require_origin) and self.token_valid(token)

    def http_authorized(self, conn: HTTPConnection, *, require_origin: bool) -> bool:
        return self.authorized(conn, conn.headers.get(ACCESS_HEADER), require_origin=require_origin)

    def websocket_subprotocol(self, conn: HTTPConnection) -> Optional[str]:
        """The subprotocol to accept a WebSocket handshake with, or None to refuse.

        The page offers ``sonn.v1`` plus ``sonn.access.<token>``. The server
        selects ``sonn.v1``, so the token is never echoed back.
        """
        offered = [str(p) for p in conn.scope.get("subprotocols") or ()]
        if WS_PROTOCOL not in offered:
            return None
        token = next(
            (p[len(WS_ACCESS_PREFIX):] for p in offered if p.startswith(WS_ACCESS_PREFIX)),
            None,
        )
        if not self.authorized(conn, token, require_origin=True):
            return None
        return WS_PROTOCOL


access = LocalAccess()


class LocalHostGuard:
    """ASGI middleware that refuses requests whose Host is not this server.

    Covers every route, including the page and static assets, so a rebinding
    page cannot load the app under a foreign origin. Token and Origin checks
    stay in the privileged endpoints.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = Headers(scope=scope).get("host", "").strip().lower()
            if host not in allowed_hosts(scope):
                if scope["type"] == "websocket":
                    await WebSocketClose(code=WS_REFUSED)(scope, receive, send)
                else:
                    await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
