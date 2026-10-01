"""Opt-in native Authorization Code + PKCE client; no desktop or token storage.

The registered issuer/endpoints/client and resource audience are operator input.
Browser responses and token claims cannot select a key source or tenant. This
initial envelope requires RS256 JWT access/ID tokens and RFC 9207 response iss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import socketserver
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
import webbrowser

import h11
import httpx
import jwt

from .identity import Identity, IdentityConfig, _unique_object
from .models import Principal


class LoginFailed(Exception):
    """Generic local error; never incorporates code, token, or issuer response."""

    def __init__(self):
        super().__init__("Native authorization did not complete")


@dataclass(frozen=True, slots=True)
class NativeOIDCConfig:
    issuer: str
    client_id: str
    audience: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_url: str
    resource: str | None = None
    scopes: tuple[str, ...] = ("openid",)
    callback_path: str = "/oauth2/callback"
    profile: str = "production"
    allow_test_loopback: bool = False
    login_timeout_seconds: int = 180

    def __post_init__(self):
        self.identity_config()
        for endpoint in (self.authorization_endpoint, self.token_endpoint):
            IdentityConfig(self.issuer, self.audience, endpoint, profile=self.profile,
                           allow_test_loopback=self.allow_test_loopback)
        if self.resource is not None:
            # RFC 8707 resource identifiers are absolute URIs; a JWT audience
            # may instead be an opaque string, so the two are not conflated.
            if type(self.resource) is not str or len(self.resource) > 2048:
                raise ValueError("Invalid configured OAuth resource URI")
            parsed = urlsplit(self.resource)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.fragment or any(ord(char) < 33 for char in self.resource)):
                raise ValueError("Invalid configured OAuth resource URI")
        if type(self.client_id) is not str or not re.fullmatch(r"[\x21-\x7e]{1,255}", self.client_id):
            raise ValueError("A registered public native client ID is required")
        if (type(self.scopes) is not tuple or not 1 <= len(self.scopes) <= 16
                or "openid" not in self.scopes or len(set(self.scopes)) != len(self.scopes)
                or any(type(scope) is not str or not re.fullmatch(r"[A-Za-z0-9:._/-]{1,128}", scope)
                       or scope == "offline_access" for scope in self.scopes)):
            raise ValueError("Explicit bounded OIDC scopes are required; offline access is unsupported")
        if type(self.callback_path) is not str or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", self.callback_path):
            raise ValueError("A registered loopback callback path is required")
        if type(self.login_timeout_seconds) is not int or not 1 <= self.login_timeout_seconds <= 300:
            raise ValueError("Native authorization timeout must be bounded")

    def identity_config(self):
        return IdentityConfig(self.issuer, self.audience, self.jwks_url,
                              profile=self.profile, allow_test_loopback=self.allow_test_loopback)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Verified in-memory resource credential; callers must not serialize it."""

    principal: Principal
    access_token: str = field(repr=False)


class _Callback(socketserver.BaseRequestHandler):
    def handle(self):
        protocol = h11.Connection(h11.SERVER, max_incomplete_event_size=8192)
        self.request.settimeout(1)
        deadline = time.monotonic() + 2
        status = 400
        try:
            request = None
            while time.monotonic() < deadline:
                event = protocol.next_event()
                if event is h11.NEED_DATA:
                    chunk = self.request.recv(2048)
                    if not chunk:
                        return
                    protocol.receive_data(chunk)
                elif isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.EndOfMessage):
                    status = self.server.accept_callback(request)
                    break
                elif isinstance(event, (h11.Data, h11.ConnectionClosed)):
                    break
            body = (b"Authorization received. Return to Lumi to finish."
                    if status == 200 else b"Authorization response unavailable.")
            self.request.sendall(protocol.send(h11.Response(status_code=status, headers=[
                (b"Content-Type", b"text/plain; charset=utf-8"), (b"Content-Length", str(len(body)).encode()),
                (b"Cache-Control", b"no-store"), (b"Referrer-Policy", b"no-referrer"),
                (b"Content-Security-Policy", b"default-src 'none'; frame-ancestors 'none'"),
                (b"X-Content-Type-Options", b"nosniff"), (b"Connection", b"close")])))
            self.request.sendall(protocol.send(h11.Data(data=body)) + protocol.send(h11.EndOfMessage()))
        except Exception:
            # Socket/protocol diagnostics may contain callback query material.
            return


class _Loopback(socketserver.TCPServer):
    allow_reuse_address = False

    def __init__(self, config, state):
        super().__init__(("127.0.0.1", 0), _Callback)
        self.config, self.state = config, state
        self.host = f"127.0.0.1:{self.server_address[1]}"
        self.redirect_uri = "http://" + self.host + config.callback_path
        self.ready = threading.Event()
        self.code = None

    def handle_error(self, request, client_address):
        pass

    def accept_callback(self, request):
        if request is None or request.method != b"GET" or len(request.target) > 8192:
            return 400
        hosts = [value for name, value in request.headers if name == b"host"]
        if hosts != [self.host.encode("ascii")]:
            return 400
        parsed = urlsplit(request.target.decode("ascii"))
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != self.config.callback_path:
            return 400
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        values = dict(pairs)
        if len(values) != len(pairs) or any(len(value) > 4096 for value in values.values()):
            return 400
        if (not secrets.compare_digest(values.get("state", ""), self.state)
                or values.get("iss") != self.config.issuer):
            return 400
        if self.ready.is_set():
            return 409
        if set(values) == {"state", "iss", "code"} and values["code"]:
            self.code = values["code"]
        elif set(values) <= {"state", "iss", "error", "error_description", "error_uri"} and values.get("error"):
            self.code = None
        else:
            return 400
        self.ready.set()
        return 200


class NativeOIDC:
    """One concurrent login per client, external browser, no refresh/persistence."""

    def __init__(self, config: NativeOIDCConfig):
        self.config = config
        self.identity = Identity(config.identity_config())
        self._login = threading.Lock()

    def _id_token(self, token, nonce, access_token):
        if type(token) is not str or not 1 <= len(token) <= 16384 or len(token.split(".")) != 3:
            raise LoginFailed()
        for piece in token.split(".")[:2]:
            if type(json.loads(jwt.utils.base64url_decode(piece), object_pairs_hook=_unique_object)) is not dict:
                raise LoginFailed()
        header = jwt.get_unverified_header(token)
        if (set(header) not in ({"alg", "kid"}, {"alg", "kid", "typ"}) or header.get("alg") != "RS256"
                or header.get("typ", "JWT") != "JWT" or type(header.get("kid")) is not str
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", header["kid"])):
            raise LoginFailed()
        for rotated in (False, True):
            try:
                # Share the same fixed-source bounded cache, never a token URL.
                claims = jwt.decode(token, self.identity._key(header["kid"], rotated=rotated), algorithms=["RS256"],
                    audience=self.config.client_id, issuer=self.config.issuer,
                    options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"], "strict_aud": True})
                break
            except jwt.InvalidSignatureError:
                if rotated:
                    raise LoginFailed() from None
        if (type(claims["nonce"]) is not str or not secrets.compare_digest(claims["nonce"], nonce)
                or claims.get("azp", self.config.client_id) != self.config.client_id
                or any(type(claims[key]) is not int for key in ("exp", "iat"))
                or ("nbf" in claims and type(claims["nbf"]) is not int)
                or not 0 < claims["exp"] - claims["iat"] <= 3600):
            raise LoginFailed()
        if "at_hash" in claims:
            expected = jwt.utils.base64url_encode(hashlib.sha256(access_token.encode("ascii")).digest()[:16]).decode()
            if type(claims["at_hash"]) is not str or not secrets.compare_digest(expected, claims["at_hash"]):
                raise LoginFailed()
        return claims

    def _redeem(self, code, verifier, redirect_uri, nonce):
        data = {"grant_type": "authorization_code", "client_id": self.config.client_id,
                "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}
        if self.config.resource is not None:
            data["resource"] = self.config.resource
        # Code redemption is deliberately never retried: a lost reply is a new
        # user login, not permission to replay a one-use authorization code.
        encoded = bytearray()
        deadline = time.monotonic() + 5
        with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", self.config.token_endpoint, data=data,
                               headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                if (response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity"
                        or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
                    raise LoginFailed()
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline or len(encoded) + len(chunk) > 65536:
                        raise LoginFailed()
                    encoded.extend(chunk)
        value = json.loads(encoded, object_pairs_hook=_unique_object)
        if (type(value) is not dict or value.get("token_type", "").casefold() != "bearer"
                or type(value.get("access_token")) is not str or type(value.get("expires_in")) is not int
                or not 1 <= value["expires_in"] <= 3600):
            raise LoginFailed()
        access_token = value["access_token"]
        principal = self.identity.authenticate("Bearer " + access_token)
        claims = self._id_token(value.get("id_token"), nonce, access_token)
        if claims["sub"] != principal.subject or min(principal.expires_at, claims["exp"]) <= time.time():
            raise LoginFailed()
        return LoginResult(principal, access_token)

    def login(self, *, browser=None, cancel_event: threading.Event | None = None) -> LoginResult:
        """Open an external browser and wait for one verified login or cancellation.

        Call on a background thread. `browser` is a trusted host launch adapter;
        production normally uses the operating system's default web browser.
        """
        if not self._login.acquire(blocking=False):
            raise LoginFailed()
        server, thread = None, None
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise LoginFailed()
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            server = _Loopback(self.config, state)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.1), daemon=True)
            thread.start()
            challenge = jwt.utils.base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode()
            values = {"response_type": "code", "client_id": self.config.client_id,
                      "redirect_uri": server.redirect_uri, "scope": " ".join(self.config.scopes),
                      "state": state, "nonce": nonce, "code_challenge": challenge,
                      "code_challenge_method": "S256"}
            if self.config.resource is not None:
                values["resource"] = self.config.resource
            deadline = time.monotonic() + self.config.login_timeout_seconds
            opener = browser if browser is not None else webbrowser.open
            if not opener(self.config.authorization_endpoint + "?" + urlencode(values)):
                raise LoginFailed()
            while not server.ready.wait(.05):
                if time.monotonic() >= deadline or cancel_event is not None and cancel_event.is_set():
                    raise LoginFailed()
            if (not server.code or time.monotonic() >= deadline
                    or cancel_event is not None and cancel_event.is_set()):
                raise LoginFailed()
            result = self._redeem(server.code, verifier, server.redirect_uri, nonce)
            if time.monotonic() >= deadline or cancel_event is not None and cancel_event.is_set():
                raise LoginFailed()
            return result
        except Exception:
            raise LoginFailed() from None
        finally:
            if server is not None:
                if thread is not None and thread.ident is not None:
                    server.shutdown()
                server.server_close()
            if thread is not None and thread.ident is not None:
                thread.join(timeout=3)
            self._login.release()
