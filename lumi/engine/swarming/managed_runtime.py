"""Trusted parent-runtime adapter for managed authority and observation delivery.

Network calls are outside local SQLite transactions and runner control locks.
The journal carries public binding/receipts only; the TLS client and private key
stay in this parent object. Constructing this adapter performs no enrollment,
registration, dispatch, polling, or personal-run conversion.
"""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import re
import threading
import time
from uuid import UUID, uuid4

from ..execution_guard import ExecutionGuardError
from .managed_client import HostChannelClient, HostChannelError
from .managed_journal import ManagedJournal


class ManagedAdmissionError(ExecutionGuardError):
    def __init__(self):
        super().__init__("Managed admission is unavailable; inspect retained receipts before continuing")


def _stamp(value):
    if type(value) is not str:
        raise ManagedAdmissionError()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()):
        raise ManagedAdmissionError()
    return parsed.timestamp()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class ManagedRuntime:
    """One captured managed run/epoch; never grants authority through a prompt."""

    def __init__(self, journal: ManagedJournal, client: HostChannelClient, *, policy_revision: int, clock=time.monotonic, worker_admission=None):
        if (type(policy_revision) is not int or policy_revision < 1
                or journal.binding.server_url.rstrip("/") != client.config.endpoint.rstrip("/")
                or journal.binding.certificate_sha256 != client.config.certificate_sha256):
            raise ManagedAdmissionError()
        self.journal, self.client = journal, client
        self.binding = journal.binding
        self.policy_revision, self.clock = policy_revision, clock
        self._lease_lock = threading.Lock()
        self._policy_lock = threading.Lock()
        self._outbox_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._lease_value = None
        self._lease_deadline = 0.0
        self._seen_leases = set()
        self._issued_leases = {}
        self._request_deadlines = {}
        self._action_deadlines = {}
        self._worker_ids = {}
        self._request_ids = {}
        self._request_contexts = {}
        self._action_contexts = {}
        self._observed_requests = set()
        self._observed_actions = set()
        self._claimed_workers = set()
        self._registration_lock = threading.Lock()
        self._worker_admission = worker_admission

    def register(self):
        """Register this explicit managed run; never convert a personal run."""
        with self._registration_lock:
            current = self.journal.inspect()["remote_binding_id"]
            if current is not None:
                return current
            lease, deadline = self._lease()
            intent = self.journal.registration(lease)
            result = self.client.register(**intent)
            if (self.clock() >= deadline or result.get("revision") != 1
                    or result.get("ownership_generation") != 1):
                raise ManagedAdmissionError()
            self.journal.bind_remote(result["binding_id"])
            return result["binding_id"]

    def prepare_worker(self, context, *, kind, grant):
        """Obtain one shared slot before backend construction or child init."""
        self._context(context)
        remote_binding = self.register()
        lease, deadline = self._lease()
        self._model_allowed(lease, grant.model.to_dict())
        if not grant.tools <= frozenset(lease["policy"].get("allowed_tools", [])):
            raise ManagedAdmissionError()
        row = self.journal.worker(context.attempt_id, context.epoch, kind=kind)
        self._worker_ids[context.attempt_id] = row["remote_worker_id"]
        if not self.journal.begin_worker(context.attempt_id, context.epoch, lease):
            raise ManagedAdmissionError()
        receipt = self.client.reserve_worker(lease_id=lease["lease_id"], binding_id=remote_binding,
            worker_id=row["remote_worker_id"], kind=kind)
        self.journal.worker_receipt(context.attempt_id, context.epoch, lease, receipt)
        if self._worker_admission is not None:
            if receipt.get("dispatch_permitted") is not True or self.clock() >= deadline:
                raise ManagedAdmissionError()
            # Trusted parent-only extension; no network inside the local ledger
            # or runner lock, and no worker/backend exists at this boundary.
            self._worker_admission(context, worker_id=row["remote_worker_id"], lease_id=lease["lease_id"],
                                   binding_id=remote_binding)
        return deadline

    def claim_worker(self, context, deadline):
        self._context(context)
        if (self.clock() >= deadline
                or not self.journal.claim_worker(context.attempt_id, context.epoch)):
            raise ManagedAdmissionError()
        self._claimed_workers.add(context.attempt_id)

    def finish_worker(self, context, *, stopped):
        """Only the owning runner can report actual closed resources."""
        self._context(context)
        if context.attempt_id not in self._worker_ids:
            return
        # Abrupt child termination may prevent its end_request/end_tool RPC.
        # Cleanup proves no surviving worker, never a successful model/action.
        for identity, owner in tuple(self._request_contexts.items()):
            if owner == context and identity in self._request_ids and identity not in self._observed_requests:
                self.journal.observe(identity, "uncertain")
                self._observed_requests.add(identity)
        for identity, owner in tuple(self._action_contexts.items()):
            if owner == context and identity not in self._observed_actions:
                self.journal.observe_action(identity, "uncertain")
                self._observed_actions.add(identity)
        outcome = ("stopped" if context.attempt_id in self._claimed_workers else "never_started") if stopped else "uncertain"
        self.journal.observe_worker(context.attempt_id, context.epoch, outcome)
        self.flush()

    def prepare_request(self, context, request_id, *, purpose, model, input_sha256):
        self._context(context)
        worker_id = self._worker_ids.get(context.attempt_id)
        if context.attempt_id not in self._claimed_workers or worker_id is None:
            raise ManagedAdmissionError()
        lease, deadline = self._lease()
        self._model_allowed(lease, model)
        row = self.journal.request(request_id, {"attempt_id": context.attempt_id, "attempt_epoch": context.epoch,
            "purpose": purpose, "model": model, "input_sha256": input_sha256, "policy_revision": self.policy_revision})
        self._request_ids[request_id] = row["remote_request_id"]
        self._request_contexts[request_id] = context
        self._request_deadlines[request_id] = deadline
        if not self.journal.mark_reserve_pending(request_id, lease):
            raise ManagedAdmissionError()
        try:
            receipt = self.client.reserve(lease["lease_id"], row["remote_request_id"])
        except HostChannelError as exc:
            if not exc.delivery_unknown:
                self.journal.reserve_rejected(request_id)
                self._request_ids.pop(request_id, None)
            raise ManagedAdmissionError() from None
        self.journal.reserve_receipt(request_id, lease, receipt)
        bound = self.client.bind_request(lease_id=lease["lease_id"], worker_id=worker_id,
            request_id=row["remote_request_id"], purpose=purpose, model=model, input_sha256=input_sha256)
        self.journal.bind_request_receipt(request_id, worker_id, bound)
        if not self.journal.begin_start(request_id):
            raise ManagedAdmissionError()
        receipt = self.client.start(row["remote_request_id"])
        self.journal.start_receipt(request_id, receipt)

    def claim_request(self, context, request_id):
        self._context(context)
        self._owner(self._request_contexts, request_id, context)
        if (self.clock() >= self._request_deadlines.get(request_id, 0)
                or not self.journal.claim_dispatch(request_id)):
            raise ManagedAdmissionError()

    def abandon_request(self, context, request_id):
        self._context(context)
        if request_id in self._request_ids:
            self._owner(self._request_contexts, request_id, context)
            if request_id not in self._observed_requests:
                self.journal.observe(request_id, "uncertain")
                self._observed_requests.add(request_id)
            self.flush()

    def observe_request(self, context, request_id, *, outcome):
        self._context(context)
        self._owner(self._request_contexts, request_id, context)
        self.journal.observe(request_id, outcome)
        self._observed_requests.add(request_id)
        self.flush()

    def prepare_action(self, context, receipt_id, request_id, *, name, arguments_sha256):
        self._context(context)
        self._owner(self._request_contexts, request_id, context)
        lease, deadline = self._lease()
        if name not in lease["policy"].get("allowed_tools", []):
            raise ManagedAdmissionError()
        worker_id = self._worker_ids.get(context.attempt_id)
        remote_request = self._request_ids.get(request_id)
        if context.attempt_id not in self._claimed_workers or worker_id is None or remote_request is None:
            raise ManagedAdmissionError()
        self._require_request_settlement(request_id)
        row = self.journal.action(receipt_id, {"worker_id": worker_id, "request_id": remote_request,
            "tool_name": name, "arguments_sha256": arguments_sha256})
        self._action_contexts[receipt_id] = context
        self._action_deadlines[receipt_id] = deadline
        if not self.journal.begin_action(receipt_id, lease):
            raise ManagedAdmissionError()
        receipt = self.client.authorize_tool(lease_id=lease["lease_id"], worker_id=worker_id,
            request_id=remote_request, action_id=row["remote_action_id"], tool_name=name, arguments_sha256=arguments_sha256)
        self.journal.action_receipt(receipt_id, lease, receipt)

    def _require_request_settlement(self, request_id):
        """A tool waits for its own request's acknowledgement, not a whole flush.

        The background outbox pump may be busy with unrelated reports. Deliver
        this one immutable observation directly when needed; settlement is
        idempotent even if the pump sends the same observation concurrently.
        No native/control lock spans HTTP, and an unavailable acknowledgement
        keeps tool admission closed. This never retries generation or a tool.
        """
        observation = self.journal.completed_request_observation(request_id)
        if observation["receipt"] is not None:
            return
        try:
            self.journal.mark_delivery_attempt(observation["id"])
            receipt = self.client.settle(**observation["payload"])
            self.journal.acknowledge(observation["id"], receipt)
        except Exception:
            raise ManagedAdmissionError() from None

    def claim_action(self, context, receipt_id):
        self._context(context)
        self._owner(self._action_contexts, receipt_id, context)
        if (self.clock() >= self._action_deadlines.get(receipt_id, 0)
                or not self.journal.claim_action(receipt_id)):
            raise ManagedAdmissionError()

    def abandon_action(self, context, receipt_id):
        self._context(context)
        if receipt_id in self._action_deadlines:
            self._owner(self._action_contexts, receipt_id, context)
            if receipt_id not in self._observed_actions:
                self.journal.observe_action(receipt_id, "uncertain")
                self._observed_actions.add(receipt_id)
            self.flush()

    def observe_action(self, context, receipt_id, *, outcome):
        self._context(context)
        self._owner(self._action_contexts, receipt_id, context)
        self.journal.observe_action(receipt_id, outcome)
        self._observed_actions.add(receipt_id)
        self.flush()

    @staticmethod
    def _owner(mapping, identity, context):
        if mapping.get(identity) != context:
            raise ManagedAdmissionError()

    def _context(self, context):
        expected = (self.binding.tenant_id, self.binding.owner_id, self.binding.local_project_id, self.binding.session_id)
        if (context.scope.values() != expected or context.run_id != self.binding.run_id
                or context.epoch != self.binding.epoch):
            raise ManagedAdmissionError()

    def _lease(self):
        with self._lease_lock:
            if self._lease_value is not None and self.clock() + 5 < self._lease_deadline:
                return copy.deepcopy(self._lease_value), self._lease_deadline
            sent = self.clock()
            lease = self.client.lease(str(uuid4()), self.policy_revision)
            identity = lease.get("lease_id")
            if type(identity) is not str or str(UUID(identity)) != identity or identity in self._seen_leases:
                raise ManagedAdmissionError()
            self._seen_leases.add(identity)
            for key in ("tenant_id", "project_id", "host_id", "host_generation", "owner_id"):
                if lease.get(key) != getattr(self.binding, key):
                    raise ManagedAdmissionError()
            if (type(lease.get("runner_protocol")) is not int or lease["runner_protocol"] != 2
                    or type(lease.get("protocol_version")) is not int or lease["protocol_version"] != 1
                    or type(lease.get("offline_request_allowance")) is not int or lease["offline_request_allowance"] != 0
                    or type(lease.get("host_generation")) is not int
                    or type(lease.get("owner_id")) is not str or not re.fullmatch(r"[0-9a-f]{64}", lease["owner_id"])
                    or type(lease.get("policy_revision")) is not int or lease["policy_revision"] != self.policy_revision
                    or type(lease.get("policy")) is not dict
                    or lease.get("policy_sha256") != hashlib.sha256(_json(lease["policy"]).encode()).hexdigest()):
                raise ManagedAdmissionError()
            duration = _stamp(lease.get("expires_at")) - _stamp(lease.get("issued_at"))
            if not 0 < duration <= 60:
                raise ManagedAdmissionError()
            # Request-start is no later than server issuance. Thus this local
            # monotonic deadline is conservative even with skewed wall clocks.
            deadline = sent + duration
            if self.clock() >= deadline:
                raise ManagedAdmissionError()
            with self._policy_lock:
                self._lease_value, self._lease_deadline = copy.deepcopy(lease), deadline
                self._issued_leases[identity] = (_json(lease), deadline)
            return lease, deadline

    def effect_lease(self):
        """Trusted owner-effect helper admission; network stays outside locks."""
        return self._lease()

    def claim_effect_lease(self, lease, deadline):
        """Check this runtime's original lease receipt without I/O or renewal.

        The separate effect journal consumes the permit and the caller holds
        current native authority. This check alone grants no process launch.
        """
        with self._policy_lock:
            original = self._issued_leases.get(lease.get("lease_id"))
            if (original != (_json(lease), deadline) or self.clock() >= deadline):
                raise ManagedAdmissionError()

    def policy_view(self):
        """Display only an actually received policy; this performs no network."""
        with self._policy_lock:
            if self._lease_value is None:
                return {"authenticated": False, "valid": False, "policy": None, "policy_revision": None}
            return {"authenticated": True, "valid": self.clock() < self._lease_deadline,
                    "policy": copy.deepcopy(self._lease_value["policy"]),
                    "policy_revision": self._lease_value["policy_revision"]}

    @staticmethod
    def _model_allowed(lease, model):
        if type(model) is not dict or set(model) != {"provider", "model"} or model not in lease["policy"].get("allowed_models", []):
            raise ManagedAdmissionError()

    def flush(self, *, maximum=8):
        """Retry immutable observations only; never replay allocation or dispatch.

        Independent cleanup observations can be delivered even when an earlier
        ambiguous request needs explicit reconciliation. No server exception or
        payload is included in the returned bounded status.
        """
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError("Invalid observation batch limit")
        if not self._outbox_lock.acquire(blocking=False):
            return {"delivered": 0, "unavailable": True}
        delivered, unavailable = 0, False
        try:
            deadline = self.clock() + 5
            for item in self.journal.pending(limit=maximum):
                if self.clock() >= deadline:
                    unavailable = True
                    break
                if item["kind"] not in {"settle", "ingest", "observe_control", "observe_worker", "observe_tool"}:
                    raise ManagedAdmissionError()
                try:
                    self.journal.mark_delivery_attempt(item["id"])
                    result = getattr(self.client, item["kind"])(**item["payload"])
                    self.journal.acknowledge(item["id"], result)
                    delivered += 1
                except Exception:
                    unavailable = True
            return {"delivered": delivered, "unavailable": unavailable}
        finally:
            self._outbox_lock.release()

    def report(self, key, projection):
        """Queue strict metadata; content disclosure uses its separate gateway."""
        result = self.journal.enqueue_report(key, projection)
        self.flush()
        return result

    def poll_controls(self, runner, *, maximum=2):
        """Apply only first-delivered exact-revision pause/stop permits."""
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise ValueError("Invalid control batch limit")
        if (runner.authority.run_id != self.binding.run_id or runner.authority.epoch != self.binding.epoch
                or runner.authority.scope.values() != (self.binding.tenant_id, self.binding.owner_id,
                    self.binding.local_project_id, self.binding.session_id)):
            raise ManagedAdmissionError()
        if not self._control_lock.acquire(blocking=False):
            return {"applied": 0, "unavailable": False}
        applied, unavailable = 0, False
        try:
            budget_deadline = self.clock() + 5
            lease, _ = self._lease()
            remote = self.journal.inspect()["remote_binding_id"]
            if remote is None:
                raise ManagedAdmissionError()
            result = self.client.poll_controls(binding_id=remote, lease_id=lease["lease_id"])
            if (result.get("binding_id") != remote or type(result.get("controls")) is not list
                    or len(result["controls"]) > 20):
                raise ManagedAdmissionError()
            for control in result["controls"][:maximum]:
                if self.clock() >= budget_deadline:
                    unavailable = True
                    break
                row = self.journal.prepare_control(control)
                control_id = control["control_id"]
                if control["policy_revision"] != self.policy_revision or not self.journal.begin_control_receive(control_id):
                    continue
                try:
                    sent = self.clock()
                    receipt = self.client.observe_control(binding_id=remote, control_id=control_id,
                        command_id=row["receive_command_id"], outcome="received", processes_stopped=False)
                    self.journal.control_receipt(control_id, receipt)
                    ttl = _stamp(receipt.get("expires_at")) - _stamp(receipt.get("received_at"))
                    if not 0 < ttl <= 60 or self.clock() >= sent + ttl:
                        raise ManagedAdmissionError()
                    permit = self.journal.claim_control(control_id)
                    if permit is None:
                        raise ManagedAdmissionError()
                except Exception:
                    self.journal.observe_control(control_id, "uncertain")
                    unavailable = True
                    continue
                try:
                    getattr(runner, permit["operation"])(command_id=permit["command_id"],
                                                        expected_revision=permit["expected_local_revision"])
                except Exception as exc:
                    from .models import RevisionConflict
                    outcome = "denied" if isinstance(exc, RevisionConflict) else "uncertain"
                    unavailable = True
                else:
                    outcome = "applied"
                # Keep this persistence outside the dispatch exception handler:
                # an ambiguous write must not overwrite a committed observation.
                # A local Stop command alone never proves process cleanup.
                self.journal.observe_control(control_id, outcome, processes_stopped=False)
                applied += int(outcome == "applied")
            self.flush()
            return {"applied": applied, "unavailable": unavailable}
        finally:
            self._control_lock.release()
