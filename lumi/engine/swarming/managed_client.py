"""Private parent-runtime transport for the managed host protocol.

This module does not import the governance server, discover endpoints, enroll a
host, retry effects, or expose certificates to a model or browser worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit

import httpx


class HostChannelError(RuntimeError):
    """Safe transport failure; ambiguity must never become a dispatch permit."""

    def __init__(self, *, delivery_unknown=True, status=None):
        self.delivery_unknown = delivery_unknown
        self.status = status
        super().__init__("Managed host operation unavailable")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate protocol field")
        result[name] = value
    return result


@dataclass(frozen=True, slots=True)
class HostChannelConfig:
    endpoint: str
    ca_file: str = field(repr=False)
    certificate_file: str = field(repr=False)
    private_key_file: str = field(repr=False)
    certificate_sha256: str
    profile: str = "production"
    allow_test_loopback: bool = False
    timeout_seconds: float = 2.0

    def __post_init__(self):
        if type(self.endpoint) is not str or len(self.endpoint) > 2048:
            raise ValueError("An explicit managed HTTPS origin is required")
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(ord(char) < 33 for char in self.endpoint)):
            raise ValueError("An explicit managed HTTPS origin is required")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.casefold() == "localhost"
        if (self.profile not in {"production", "test"} or type(self.allow_test_loopback) is not bool
                or self.profile == "test" and (not self.allow_test_loopback or not loopback)
                or self.profile == "production" and (self.allow_test_loopback or loopback)):
            raise ValueError("Managed test transport requires explicit loopback configuration")
        if type(self.certificate_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", self.certificate_sha256):
            raise ValueError("An explicit enrolled certificate fingerprint is required")
        if type(self.timeout_seconds) not in {int, float} or not .1 <= self.timeout_seconds <= 5:
            raise ValueError("Managed network timeout must be bounded")
        for name in (self.ca_file, self.certificate_file, self.private_key_file):
            if type(name) is not str or not Path(name).is_absolute() or not Path(name).is_file():
                raise ValueError("Managed TLS requires protected absolute files")


class HostChannelClient:
    """Fixed HTTPS methods; one network attempt per call, no credential payloads."""

    _routes = {
        "activate": ("/v1/activate", {"challenge"}),
        "lease": ("/v1/leases", {"command_id", "expected_policy_revision", "runner_protocol"}),
        "reserve": ("/v1/requests/reserve", {"lease_id", "request_id"}),
        "start": ("/v1/requests/start", {"request_id"}),
        "settle": ("/v1/requests/settle", {"request_id", "outcome"}),
        "register": ("/v1/runs/register", {"command_id", "lease_id", "local_run_id", "session_id"}),
        "ingest": ("/v1/runs/ingest", {"command_id", "binding_id", "sequence", "projection"}),
        "poll_controls": ("/v1/runs/controls/poll", {"binding_id", "lease_id"}),
        "observe_control": ("/v1/runs/controls/observe", {
            "binding_id", "control_id", "command_id", "outcome", "processes_stopped"}),
        "reserve_worker": ("/v1/resources/workers/reserve", {"lease_id", "binding_id", "worker_id", "kind"}),
        "observe_worker": ("/v1/resources/workers/observe", {"worker_id", "outcome"}),
        "bind_request": ("/v1/resources/requests/bind", {
            "lease_id", "worker_id", "request_id", "purpose", "model", "input_sha256"}),
        "authorize_tool": ("/v1/resources/tools/authorize", {
            "lease_id", "worker_id", "request_id", "action_id", "tool_name", "arguments_sha256"}),
        "observe_tool": ("/v1/resources/tools/observe", {"action_id", "outcome"}),
        "authorize_effect": ("/v1/resources/effects/authorize", {"lease_id", "binding_id", "effect_id", "kind", "semantics_sha256"}),
        "fence_absent": ("/v1/resources/fence-absent", {"resource_kind", "resource_id", "lease_id"}),
        "observe_effect": ("/v1/resources/effects/observe", {"effect_id", "outcome"}),
        "sharing_offer": ("/v1/sharing/offer", {"lease_id", "command_id", "origin_binding", "receiver_binding", "terms"}),
        "sharing_approve": ("/v1/sharing/approve", {"lease_id", "command_id", "grant_id", "terms_sha256"}),
        "sharing_revoke": ("/v1/sharing/revoke", {"lease_id", "command_id", "grant_id"}),
        "sharing_send": ("/v1/sharing/send", {"lease_id", "command_id", "grant_id", "kind", "data_class", "body", "parent_id"}),
        "sharing_deliver": ("/v1/sharing/deliver", {"lease_id", "command_id", "message_id"}),
        "sharing_accept": ("/v1/sharing/accept", {"lease_id", "command_id", "message_id", "worker_id", "request_limit", "contract_sha256"}),
        "sharing_inspect": ("/v1/sharing/inspect", {"lease_id", "binding_id", "before_grant_id", "limit"}),
        "sharing_terms": ("/v1/sharing/terms", {"lease_id", "grant_id"}),
        "sharing_messages": ("/v1/sharing/messages", {"lease_id", "grant_id", "before_message_id", "limit"}),
    }

    def __init__(self, config: HostChannelConfig):
        self.config = config
        try:
            with Path(config.certificate_file).open("rb") as source:
                encoded = source.read(131073)
            match = re.search(br"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", encoded, re.DOTALL)
            if len(encoded) > 131072 or match is None:
                raise ValueError("Invalid certificate file")
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(match.group().decode("ascii"))).hexdigest()
            if fingerprint != config.certificate_sha256:
                raise ValueError("Certificate does not match enrollment")
            context = ssl.create_default_context(cafile=config.ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.keylog_filename = None
            context.load_cert_chain(config.certificate_file, config.private_key_file)
            self._tls = context
        except Exception:
            raise HostChannelError(delivery_unknown=False) from None

    def _call(self, operation, values):
        path, fields = self._routes[operation]
        if type(values) is not dict or set(values) != fields:
            raise HostChannelError(delivery_unknown=False)
        try:
            body = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > 32768:
                raise HostChannelError(delivery_unknown=False)
        except (ValueError, TypeError):
            raise HostChannelError(delivery_unknown=False) from None
        try:
            deadline = time.monotonic() + self.config.timeout_seconds * 2
            encoded = bytearray()
            with httpx.Client(verify=self._tls, trust_env=False, follow_redirects=False,
                              timeout=self.config.timeout_seconds) as client:
                with client.stream("POST", self.config.endpoint.rstrip("/") + path, content=body,
                                   headers={"Content-Type": "application/json", "Accept": "application/json",
                                            "Accept-Encoding": "identity"}) as response:
                    if response.status_code != 200:
                        raise HostChannelError(status=response.status_code,
                                               delivery_unknown=response.status_code not in {400, 403, 409})
                    if (response.headers.get("Content-Encoding", "identity") != "identity"
                            or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
                        raise HostChannelError()
                    for chunk in response.iter_raw():
                        if time.monotonic() > deadline or len(encoded) + len(chunk) > 65536:
                            raise HostChannelError()
                        encoded.extend(chunk)
            if time.monotonic() > deadline:
                raise HostChannelError()
            value = json.loads(encoded, object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            if type(value) is not dict:
                raise HostChannelError()
            return value
        except HostChannelError:
            raise
        except Exception:
            # Do not propagate TLS paths, credential-bearing backend errors, or
            # request bodies. A transport failure may follow a committed effect.
            raise HostChannelError() from None

    def activate(self, challenge):
        return self._call("activate", {"challenge": challenge})

    def lease(self, command_id, expected_policy_revision):
        return self._call("lease", {"command_id": command_id, "expected_policy_revision": expected_policy_revision,
                                    "runner_protocol": 2})

    def reserve(self, lease_id, request_id):
        return self._call("reserve", {"lease_id": lease_id, "request_id": request_id})

    def start(self, request_id):
        return self._call("start", {"request_id": request_id})

    def settle(self, request_id, outcome):
        return self._call("settle", {"request_id": request_id, "outcome": outcome})

    def register(self, *, command_id, lease_id, local_run_id, session_id):
        return self._call("register", dict(command_id=command_id, lease_id=lease_id,
                                         local_run_id=local_run_id, session_id=session_id))

    def ingest(self, *, command_id, binding_id, sequence, projection):
        return self._call("ingest", dict(command_id=command_id, binding_id=binding_id, sequence=sequence, projection=projection))

    def poll_controls(self, *, binding_id, lease_id):
        return self._call("poll_controls", dict(binding_id=binding_id, lease_id=lease_id))

    def observe_control(self, *, binding_id, control_id, command_id, outcome, processes_stopped=False):
        return self._call("observe_control", dict(binding_id=binding_id, control_id=control_id,
            command_id=command_id, outcome=outcome, processes_stopped=processes_stopped))

    def reserve_worker(self, *, lease_id, binding_id, worker_id, kind):
        return self._call("reserve_worker", dict(lease_id=lease_id, binding_id=binding_id, worker_id=worker_id, kind=kind))

    def observe_worker(self, *, worker_id, outcome):
        return self._call("observe_worker", dict(worker_id=worker_id, outcome=outcome))

    def bind_request(self, *, lease_id, worker_id, request_id, purpose, model, input_sha256):
        return self._call("bind_request", dict(lease_id=lease_id, worker_id=worker_id, request_id=request_id,
            purpose=purpose, model=model, input_sha256=input_sha256))

    def authorize_tool(self, *, lease_id, worker_id, request_id, action_id, tool_name, arguments_sha256):
        return self._call("authorize_tool", dict(lease_id=lease_id, worker_id=worker_id, request_id=request_id,
            action_id=action_id, tool_name=tool_name, arguments_sha256=arguments_sha256))

    def observe_tool(self, *, action_id, outcome):
        return self._call("observe_tool", dict(action_id=action_id, outcome=outcome))

    def authorize_effect(self, *, lease_id, binding_id, effect_id, kind, semantics_sha256):
        return self._call("authorize_effect", dict(lease_id=lease_id, binding_id=binding_id, effect_id=effect_id,
            kind=kind, semantics_sha256=semantics_sha256))

    def observe_effect(self, *, effect_id, outcome):
        return self._call("observe_effect", dict(effect_id=effect_id, outcome=outcome))

    def fence_absent(self, *, resource_kind, resource_id, lease_id):
        return self._call("fence_absent", dict(resource_kind=resource_kind, resource_id=resource_id, lease_id=lease_id))

    def sharing_offer(self, *, lease_id, command_id, origin_binding, receiver_binding, terms):
        return self._call("sharing_offer", dict(lease_id=lease_id, command_id=command_id,
            origin_binding=origin_binding, receiver_binding=receiver_binding, terms=terms))

    def sharing_approve(self, *, lease_id, command_id, grant_id, terms_sha256):
        return self._call("sharing_approve", dict(lease_id=lease_id, command_id=command_id, grant_id=grant_id, terms_sha256=terms_sha256))

    def sharing_revoke(self, *, lease_id, command_id, grant_id):
        return self._call("sharing_revoke", dict(lease_id=lease_id, command_id=command_id, grant_id=grant_id))

    def sharing_send(self, *, lease_id, command_id, grant_id, kind, data_class, body, parent_id=None):
        return self._call("sharing_send", dict(lease_id=lease_id, command_id=command_id,
            grant_id=grant_id, kind=kind, data_class=data_class, body=body, parent_id=parent_id))

    def sharing_deliver(self, *, lease_id, command_id, message_id):
        return self._call("sharing_deliver", dict(lease_id=lease_id, command_id=command_id, message_id=message_id))

    def sharing_accept(self, *, lease_id, command_id, message_id, worker_id, request_limit, contract_sha256):
        return self._call("sharing_accept", dict(lease_id=lease_id, command_id=command_id, message_id=message_id,
            worker_id=worker_id, request_limit=request_limit, contract_sha256=contract_sha256))

    def sharing_inspect(self, *, lease_id, binding_id, before_grant_id=None, limit=20):
        return self._call("sharing_inspect", dict(lease_id=lease_id, binding_id=binding_id, before_grant_id=before_grant_id, limit=limit))

    def sharing_terms(self, *, lease_id, grant_id):
        return self._call("sharing_terms", dict(lease_id=lease_id, grant_id=grant_id))

    def sharing_messages(self, *, lease_id, grant_id, before_message_id=None, limit=20):
        return self._call("sharing_messages", dict(lease_id=lease_id, grant_id=grant_id, before_message_id=before_message_id, limit=limit))
