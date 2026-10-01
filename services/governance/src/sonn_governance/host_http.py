"""Explicit certificate-authenticated host listener with bounded HTTP framing.

TLS peer certificates are read from the actual socket. No ASGI extension,
forwarded header, human bearer token, or body field can construct host identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import socketserver
import ssl
import threading
import time

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
import h11

from .identity import _unique_object
from .models import AccessDenied, Conflict, HostPrincipal, InvalidRequest, UnsupportedVersion


@dataclass(frozen=True, slots=True)
class HostTransportConfig:
    host: str
    port: int
    certificate_file: str
    private_key_file: str
    client_ca_file: str
    profile: str = "production"
    maximum_connections: int = 32

    def __post_init__(self):
        address = ipaddress.ip_address(self.host)
        if self.profile not in {"production", "test"} or (self.profile == "test" and not address.is_loopback):
            raise ValueError("Test host transport requires an explicit loopback listener")
        if type(self.port) is not int or not (1 <= self.port <= 65535 or self.port == 0 and self.profile == "test"):
            raise ValueError("An explicit host listener port is required")
        if type(self.maximum_connections) is not int or not 1 <= self.maximum_connections <= 64:
            raise ValueError("Host connection limit must be bounded")
        for path in (self.certificate_file, self.private_key_file, self.client_ca_file):
            if type(path) is not str or not Path(path).is_absolute() or not Path(path).is_file():
                raise ValueError("Host TLS material requires existing absolute files")


def certificate_principal(connection: ssl.SSLSocket) -> HostPrincipal:
    """Use only the peer verified by this listener's required client-CA context."""
    if not isinstance(connection, ssl.SSLSocket) or connection.context.verify_mode != ssl.CERT_REQUIRED:
        raise AccessDenied("host authentication required")
    encoded = connection.getpeercert(binary_form=True)
    if not encoded or len(encoded) > 16384:
        raise AccessDenied("host authentication required")
    certificate = x509.load_der_x509_certificate(encoded)
    now = time.time()
    if not certificate.not_valid_before_utc.timestamp() <= now < certificate.not_valid_after_utc.timestamp():
        raise AccessDenied("host authentication required")
    try:
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.CLIENT_AUTH not in usage:
            raise AccessDenied("host authentication required")
        if certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise AccessDenied("host authentication required")
    except x509.ExtensionNotFound:
        raise AccessDenied("host authentication required") from None
    return HostPrincipal(certificate_sha256=hashlib.sha256(encoded).hexdigest(),
                         expires_at=certificate.not_valid_after_utc.timestamp())


class _HostHandler(socketserver.BaseRequestHandler):
    def _discard_rejected_input(self):
        """Bound the rejection close without resetting a still-arriving body.

        An immediate close with unread TCP input can discard the already-sent
        error response (RFC 9112 section 9.6). This is transport cleanup only:
        bytes are discarded, never parsed, persisted, or admitted to a route.
        """
        remaining_bytes = 65536
        deadline = time.monotonic() + .25
        while remaining_bytes:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                return
            try:
                self.request.settimeout(remaining_time)
                chunk = self.request.recv(min(4096, remaining_bytes))
            except OSError:
                return
            if not chunk:
                return
            remaining_bytes -= len(chunk)

    def _read(self, protocol):
        deadline = time.monotonic() + 5
        request, body = None, bytearray()
        while True:
            event = protocol.next_event()
            if event is h11.NEED_DATA:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self.request.settimeout(remaining)
                chunk = self.request.recv(4096)
                protocol.receive_data(chunk)
            elif isinstance(event, h11.Request):
                if request is not None or event.method != b"POST" or event.http_version != b"1.1":
                    raise InvalidRequest("unsupported request")
                headers = {}
                for name, value in event.headers:
                    if name in headers:
                        raise InvalidRequest("duplicate header")
                    headers[name] = value
                if (headers.get(b"content-type", b"").split(b";", 1)[0].strip() != b"application/json"
                        or headers.get(b"content-encoding", b"identity") != b"identity"
                        or b"transfer-encoding" in headers or b"expect" in headers
                        or b"content-length" not in headers or int(headers[b"content-length"]) > 32768):
                    raise InvalidRequest("bounded JSON request required")
                request = event
            elif isinstance(event, h11.Data):
                if request is None or len(body) + len(event.data) > 32768:
                    raise InvalidRequest("request exceeds byte limit")
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                if request is None:
                    raise InvalidRequest("missing request")
                value = json.loads(body, object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                if type(value) is not dict:
                    raise InvalidRequest("JSON object required")
                return request.target.decode("ascii"), value
            else:
                raise InvalidRequest("incomplete request")

    def _dispatch(self, principal, path, value):
        if path.startswith("/v1/sharing/") and self.server.collaboration is not None:
            from .managed_collaboration_http import dispatch_collaboration
            return dispatch_collaboration(self.server.collaboration, principal, path, value)
        routes = {
            "/v1/activate": ("activate", {"challenge"}),
            "/v1/leases": ("lease", {"command_id", "expected_policy_revision"}),
            "/v1/requests/reserve": ("reserve", {"lease_id", "request_id"}),
            "/v1/requests/start": ("start", {"request_id"}),
            "/v1/requests/settle": ("settle", {"request_id", "outcome"}),
        }
        target = self.server.governance
        if path == "/v1/leases" and "runner_protocol" in value:
            routes[path] = ("lease", {"command_id", "expected_policy_revision", "runner_protocol"})
        if path.startswith("/v1/runs/") and self.server.monitoring is not None:
            target = self.server.monitoring
            routes = {
                "/v1/runs/register": ("register", {"command_id", "lease_id", "local_run_id", "session_id"}),
                "/v1/runs/ingest": ("ingest", {"command_id", "binding_id", "sequence", "projection"}),
                "/v1/runs/controls/poll": ("poll_controls", {"binding_id", "lease_id"}),
                "/v1/runs/controls/observe": ("observe_control", {"binding_id", "control_id", "command_id", "outcome", "processes_stopped"}),
            }
        if path.startswith("/v1/resources/") and self.server.resources is not None:
            target = self.server.resources
            routes = {
                "/v1/resources/workers/reserve": ("reserve_worker", {"lease_id", "binding_id", "worker_id", "kind"}),
                "/v1/resources/workers/observe": ("observe_worker", {"worker_id", "outcome"}),
                "/v1/resources/requests/bind": ("bind_request", {"lease_id", "worker_id", "request_id", "purpose", "model", "input_sha256"}),
                "/v1/resources/tools/authorize": ("authorize_tool", {"lease_id", "worker_id", "request_id", "action_id", "tool_name", "arguments_sha256"}),
                "/v1/resources/tools/observe": ("observe_tool", {"action_id", "outcome"}),
                "/v1/resources/effects/authorize": ("authorize_effect", {"lease_id", "binding_id", "effect_id", "kind", "semantics_sha256"}),
                "/v1/resources/effects/observe": ("observe_effect", {"effect_id", "outcome"}),
                "/v1/resources/fence-absent": ("fence_absent", {"resource_kind", "resource_id", "lease_id"}),
            }
        if path not in routes:
            raise InvalidRequest("unsupported host operation")
        method, fields = routes[path]
        if set(value) != fields:
            raise InvalidRequest("invalid host operation fields")
        return getattr(target, method)(principal, **value)

    def handle(self):
        protocol = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
        status, result = 503, {"error": "service_unavailable"}
        request_complete = False
        try:
            principal = certificate_principal(self.request)
            path, value = self._read(protocol)
            request_complete = True
            result = self._dispatch(principal, path, value)
            status = 200
        except AccessDenied:
            status, result = 403, {"error": "host_unavailable"}
        except Conflict:
            status, result = 409, {"error": "command_conflict"}
        except (InvalidRequest, UnsupportedVersion, ValueError, TypeError, TimeoutError, h11.RemoteProtocolError):
            status, result = 400, {"error": "invalid_request"}
        except Exception:
            pass  # Never print credential-bearing transport/database exceptions.
        try:
            body = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > 65536:
                raise ValueError("Response exceeds byte limit")
            self.request.settimeout(2)
            headers = [(b"Content-Type", b"application/json"), (b"Content-Length", str(len(body)).encode()),
                       (b"Connection", b"close"), (b"Cache-Control", b"no-store"), (b"X-Content-Type-Options", b"nosniff")]
            response = protocol.send(h11.Response(status_code=status, headers=headers))
            response += protocol.send(h11.Data(data=body))
            response += protocol.send(h11.EndOfMessage())
            self.request.sendall(response)
            if not request_complete:
                self._discard_rejected_input()
        except (OSError, ValueError, TypeError, h11.LocalProtocolError):
            pass


class HostHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A separately started bounded mTLS listener; it trusts only configured CA."""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, config: HostTransportConfig, governance, *, monitoring=None, resources=None, collaboration=None):
        self.config, self.governance, self.monitoring = config, governance, monitoring
        self.resources = resources
        self.collaboration = collaboration
        self._slots = threading.BoundedSemaphore(config.maximum_connections)
        self._active = set()
        self._active_lock = threading.Lock()
        self._closing = False
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=config.client_ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.keylog_filename = None
        context.load_cert_chain(config.certificate_file, config.private_key_file)
        context.set_alpn_protocols(["http/1.1"])
        self._tls = context
        if ipaddress.ip_address(config.host).version == 6:
            self.address_family = socket.AF_INET6
        super().__init__((config.host, config.port), _HostHandler)

    def process_request(self, request, client_address):
        if self._closing or not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            request.close()
            raise

    def process_request_thread(self, request, client_address):
        secured = None
        try:
            request.settimeout(2)
            secured = self._tls.wrap_socket(request, server_side=True)
            with self._active_lock:
                if self._closing:
                    return
                self._active.add(secured)
            self.finish_request(secured, client_address)
        except Exception:
            pass  # TLS rejection is intentionally silent; no body was trusted.
        finally:
            with self._active_lock:
                self._active.discard(secured)
            (secured or request).close()
            self._slots.release()

    def server_close(self):
        self._closing = True
        with self._active_lock:
            for connection in tuple(self._active):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        super().server_close()
