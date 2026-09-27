"""Explicit managed sharing operations over a captured desktop run.

Only the selected terms/body enter the owner's view. They never become native
instructions automatically. Remote calls run outside the foreground control lock;
hash-only durable intents prevent a lost UI reply from dispatching work twice.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid

from . import collaboration_desktop
from .managed_collaboration import ManagedCollaborationAdmission
from .models import AdmissionClosed, Conflict, RevisionConflict, ScopeDenied, StaleAuthority, require_id
from .policy import normalize_scopes

ACTIONS = {"managed_sharing_" + action for action in (
    "prepare", "inspect", "offer", "approve", "revoke", "send", "deliver", "accept_work")}
FIELDS = {"receiver_binding"}
_ENVELOPE = {"command", "project", "session_id", "run_id", "request_id", "expected_revision", "execution_mode"}
_ARGUMENTS = {
    "prepare": {"objective", "request_limit"},
    "inspect": {"grant_id", "before_grant_id", "before_message_id"},
    "offer": {"receiver_binding", "terms"},
    "approve": {"grant_id", "terms_sha256"},
    "revoke": {"grant_id", "evidence"},
    "send": {"grant_id", "kind", "data_class", "body", "parent_id"},
    "deliver": {"message_id", "grant_id"},
    "accept_work": {"message_id", "grant_id", "objective", "read_roots", "requests", "evidence"},
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


class ManagedSharing:
    """One fresh run/epoch; reopening requires recovery, never dispatch replay."""

    def __init__(self, runtime, capture, store, runner, attachment):
        self.runtime, self.capture, self.store = runtime, capture, store
        self.runner, self.attachment = runner, attachment
        managed = attachment.runtime
        self.admission = ManagedCollaborationAdmission(managed.journal.path.with_suffix(".sharing.sqlite"),
            supervisor=runner.supervisor, authority=runner.authority, journal=managed.journal, client=managed.client)
        # Construction precedes runner publication and the first worker. A
        # resumed epoch cannot inherit this callback or its dispatch permits.
        if managed._worker_admission is not None:
            raise Conflict("Managed sharing admission is already captured")
        managed._worker_admission = self.admission
        self.operation = None
        self.grants = {"items": [], "next_cursor": None}
        self.detail = None
        self.messages = {"items": [], "next_cursor": None}
        self.selected_content = None

    def view(self):
        """Cached content plus local receipts only; rendering never calls HTTP."""
        admissions = {row["message_id"]: row for row in self.admission.inspect()}
        messages = {**self.messages, "items": [{**row, "acceptance": admissions.get(row["message_id"])}
                                             for row in self.messages["items"]]}
        return copy.deepcopy({"available": True,
            "address": self.attachment.runtime.journal.inspect()["remote_binding_id"],
            "operation": self.operation, "grants": self.grants, "detail": self.detail,
            "messages": messages, "selected_content": self.selected_content,
            "accepted_work_item_ids": self.admission.accepted_work_item_ids()})

    def _selected_grant(self, grant_id):
        if self.detail and self.detail["grant_id"] == grant_id:
            return self.detail
        row = next((row for row in self.grants["items"] if row["grant_id"] == grant_id), None)
        if row is None:
            raise Conflict("Inspect this team's grant list before selecting a grant")
        return row

    def queue(self, message):
        """Caller holds the desktop lock; persist the intent before any effect."""
        action = message["action"].removeprefix("managed_sharing_")
        run_id, authority = self.runner.authority.run_id, self.runner.authority
        semantics = {"action": action, "expected_revision": message.get("expected_revision"),
                     **{key: message[key] for key in _ARGUMENTS[action] if key in message}}
        receipt = {"action": action, "semantics_sha256": _digest(semantics)}
        with self.store._connection() as connection:
            self._current(connection, disclosure=action != "inspect")
            previous = self.store._duplicate(connection, run_id, "managed-sharing-dispatch", message["request_id"], receipt)
        if previous is not None:
            # Do not overwrite a newer displayed operation with a historical
            # response, and never reconstruct a worker from this receipt.
            return
        if self.operation and self.operation["state"] in {"queued", "running"}:
            raise Conflict("Wait for the current sharing operation before another sharing change")
        snapshot = self.store.snapshot(authority.scope, run_id)
        if action != "inspect" and (type(message.get("expected_revision")) is not int
                or message["expected_revision"] != snapshot["run"]["revision"]):
            raise RevisionConflict("Refresh the team before changing shared work")
        if action != "inspect":
            with self.store._connection() as connection:
                run = self.store._authority(connection, authority)
                self.store._admitting(run)
        grant_id = message.get("grant_id")
        selected = self._selected_grant(grant_id) if grant_id else None
        if action in {"approve", "revoke", "send"} and selected is None:
            raise Conflict("Inspect the selected grant before changing it")
        if action == "approve" and (selected["direction"] != "incoming" or not self.detail
                or self.detail.get("grant_id") != grant_id or not self.detail.get("terms_sha256")
                or self.detail["terms_sha256"] != message.get("terms_sha256")):
            raise ScopeDenied("Read this incoming agreement and approve its exact inspected terms")
        if action == "send" and selected["direction"] != "outgoing":
            raise ScopeDenied("Send selected content only from this team's outgoing agreement")
        if action in {"deliver", "accept_work"}:
            if not self.detail or grant_id not in {None, self.detail["grant_id"]}:
                raise Conflict("Inspect the selected grant before reading or accepting work")
            selected = self.detail
            listed = next((row for row in self.messages["items"] if row["message_id"] == message.get("message_id")), None)
            if not listed or listed["direction"] != "incoming":
                raise ScopeDenied("Choose an incoming message from this team's selected grant")
            if action == "accept_work" and (listed["kind"] != "work_request" or not self.selected_content
                    or self.selected_content["message_id"] != listed["message_id"]):
                raise Conflict("Explicitly read this work request before choosing an independent task")
        if action == "accept_work":
            self._validate_work(message)
        if action == "inspect":
            self.selected_content = None
            self.detail = None
            self.messages = {"items": [], "next_cursor": None}
        # The local receipt stores no terms, message text, or peer credentials.
        with self.store._connection(write=True) as connection:
            run = self._current(connection, disclosure=action != "inspect")
            if action != "inspect":
                self.store._admitting(run)
                if run["revision"] != message["expected_revision"]:
                    raise RevisionConflict("Refresh the team before changing shared work")
            self.store._remember(connection, run_id, "managed-sharing-dispatch", message["request_id"], receipt,
                                 {"dispatch_attempted": True})
        operation = self.operation = {"request_id": message["request_id"], "action": message["action"], "state": "queued"}
        if action == "accept_work":
            # Native planning/assignment is local and pins workspace ownership
            # before returning to the GUI. Remote acceptance happens only at
            # the runner's original worker-admission boundary.
            try:
                self._accept_work(message, snapshot)
                operation["state"] = "completed"
                self._record_outcome(operation, receipt, "local_dispatch_queued")
            except Exception:
                operation.update(state="failed", error="Shared work dispatch may have occurred. Inspect retained local records; this command will not dispatch again.")
                raise
            return
        command_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "sonn-managed-sharing:" + run_id + ":" + message["request_id"]))

        def execute():
            with self.runtime._lock:
                operation["state"] = "running"
            acknowledged = False

            def acknowledge():
                nonlocal acknowledged
                if acknowledged:
                    return
                self._record_outcome(operation, receipt, "acknowledged")
                acknowledged = True

            try:
                result = self._remote(action, message, command_id, copy.deepcopy(selected), acknowledge)
                acknowledge()
                with self.runtime._lock:
                    for name, value in result.items():
                        setattr(self, name, value)
                    operation["state"] = "completed"
            except Exception:
                with self.runtime._lock:
                    if acknowledged:
                        self.selected_content, self.detail = None, None
                        self.messages = {"items": [], "next_cursor": None}
                        operation.update(state="completed", error="The operation was acknowledged; refreshed metadata is unavailable.")
                    else:
                        operation.update(state="failed", error="Sharing outcome is unconfirmed. Inspect current receipts before a new decision.")

        threading.Thread(target=execute, daemon=True, name="swarm-managed-sharing").start()

    def _record_outcome(self, operation, semantics, outcome):
        """A durable receipt distinguishes acknowledged operations from intent.

        An absent receipt stays uncertain. No body, terms, private path or
        transport error is retained in this shared native command ledger.
        """
        with self.store._connection(write=True) as connection:
            self.store._run(connection, self.capture.scope, self.runner.authority.run_id)
            self.store._remember(connection, self.runner.authority.run_id, "managed-sharing-outcome",
                operation["request_id"], semantics, {"action": operation["action"], "outcome": outcome})

    def _current(self, connection, *, disclosure):
        """Remote metadata may inspect terminal history, never a fenced owner."""
        authority = self.runner.authority
        if self.runtime._closed or self.attachment._closed:
            raise Conflict("This managed sharing host has closed")
        if disclosure:
            return self.store._authority(connection, authority)
        run = self.store._run(connection, authority.scope, authority.run_id)
        if run["epoch"] != authority.epoch or run["supervisor_id"] != authority.supervisor_id:
            raise StaleAuthority("Managed sharing owner is no longer current")
        return run

    def _admit_remote(self, message, stage, *, disclosure):
        """Mark one call in flight after blocking preflight, before HTTP.

        Stop serializes with this short transaction. An already marked HTTP
        request can still finish; neither Stop nor close claims to retract it.
        Reopening/replaying an intent never recreates this dispatch permission.
        """
        authority = self.runner.authority
        key = message["request_id"]
        semantics = {"stage": stage, "sha256": _digest(message)}
        with self.runtime._lock:
            with self.store._connection(write=True) as connection:
                run = self._current(connection, disclosure=disclosure)
                if disclosure:
                    self.store._admitting(run)
                actor = "managed-sharing-start:" + stage
                if self.store._duplicate(connection, authority.run_id, actor, key, semantics) is not None:
                    raise Conflict("This sharing call was already admitted; inspect its retained outcome")
                self.store._remember(connection, authority.run_id, actor, key, semantics, {"in_flight": True})

    def _remote(self, action, message, command_id, selected, acknowledge):
        managed = self.attachment.runtime
        binding = managed.register()
        lease, _ = managed.effect_lease()
        client = managed.client
        common = {"lease_id": lease["lease_id"], "command_id": command_id}
        if action == "inspect":
            self._admit_remote(message, "metadata", disclosure=False)
            grants = client.sharing_inspect(lease_id=lease["lease_id"], binding_id=binding,
                                           before_grant_id=message.get("before_grant_id"))
            result = {"grants": grants}
            if selected:
                selected = next((row for row in grants["items"] if row["grant_id"] == selected["grant_id"]), selected)
                if selected["state"] in {"revoked", "expired", "deleted"}:
                    result["detail"] = selected
                    return result
                try:
                    self._admit_remote(message, "terms", disclosure=True)
                except AdmissionClosed:
                    # An owner may inspect stopped-run metadata. That does not
                    # admit a fresh disclosure of encrypted agreement terms.
                    result["detail"] = selected
                    return result
                detail = {**selected, **client.sharing_terms(lease_id=lease["lease_id"], grant_id=selected["grant_id"])}
                result["detail"] = detail
                if detail["state"] == "approved":
                    self._admit_remote(message, "messages", disclosure=False)
                    messages = client.sharing_messages(lease_id=lease["lease_id"], grant_id=selected["grant_id"],
                                                       before_message_id=message.get("before_message_id"))
                    result["messages"] = {**messages, "items": [{**row, "direction": detail["direction"]} for row in messages["items"]]}
            return result
        self._admit_remote(message, action, disclosure=True)
        if action == "offer":
            client.sharing_offer(**common, origin_binding=binding, receiver_binding=message.get("receiver_binding"), terms=message.get("terms"))
        elif action == "approve":
            client.sharing_approve(**common, grant_id=message["grant_id"], terms_sha256=message.get("terms_sha256"))
        elif action == "revoke":
            client.sharing_revoke(**common, grant_id=message["grant_id"])
        elif action == "send":
            client.sharing_send(**common, grant_id=message["grant_id"], kind=message.get("kind"), data_class=message.get("data_class"),
                                body=message.get("body"), parent_id=message.get("parent_id"))
        elif action == "deliver":
            body = client.sharing_deliver(**common, message_id=message["message_id"])
            return {"selected_content": body}
        # The primary reply is evidence even if a later metadata refresh is
        # fenced by Stop/close, denied, or disconnected.
        acknowledge()
        self._admit_remote(message, "refresh", disclosure=False)
        return {"selected_content": None, "detail": None, "messages": {"items": [], "next_cursor": None},
                "grants": client.sharing_inspect(lease_id=lease["lease_id"], binding_id=binding)}

    def _validate_work(self, message):
        objective, evidence, roots, requests = (message.get(key) for key in ("objective", "evidence", "read_roots", "requests"))
        if (type(objective) is not str or not objective.strip() or len(objective.encode()) > 8192
                or type(evidence) is not str or not evidence.strip() or len(evidence.encode()) > 8192):
            raise ValueError("Choose an independent objective and record why you accept this work request")
        if type(roots) is not list or not roots or any(type(root) is not str for root in roots):
            raise ValueError("Choose explicit readable project folders")
        normalize_scopes(tuple(roots))
        if type(requests) is not int or not 1 <= requests <= 1000:
            raise ValueError("Choose one to 1000 receiver-funded requests")
        self.runtime._refresh_ownership()
        if (self.runtime._active - self.runtime._collaboration_idle) - {self.runner.authority.run_id}:
            raise Conflict("Another team owns active work; wait before accepting this assignment")

    def _accept_work(self, message, snapshot):
        authority = self.runner.authority
        key = hashlib.sha256((authority.run_id + message["request_id"]).encode()).hexdigest()
        work = {"id": "managed_peer_work_" + key, "objective": message["objective"], "role": "explore",
                "read_roots": list(normalize_scopes(tuple(message["read_roots"]))), "write_roots": [],
                "tools": sorted(collaboration_desktop.TOOLS), "criteria": ["owner_review"]}
        prepared = self.admission.prepare_work(work=work, message_id=message["message_id"],
            worker_id="managed_peer_worker_" + key, requests=message["requests"],
            model={"provider": self.capture.backend_spec.backend_type, "model": self.capture.backend_spec.model},
            command_id="sharing-prepare-" + key, expected_revision=message["expected_revision"])
        if not prepared["dispatch_permitted"]:
            return
        self.runtime._collaboration_idle.discard(authority.run_id)
        self.runner.start(prepared["context"], self.capture.backend_spec)


def operate(runtime, capture, message):
    """Validate explicit sharing commands before entering a captured adapter."""
    action = message["action"].removeprefix("managed_sharing_")
    if set(message) - (_ENVELOPE | {"action"} | _ARGUMENTS[action]):
        raise ValueError("Unsupported managed sharing command fields")
    require_id(message.get("request_id"))
    if runtime._execution_mode(capture) != "managed":
        raise ScopeDenied("Managed sharing requires an explicitly configured organization team")
    with runtime._lock:
        if runtime._closed:
            raise Conflict("This desktop runtime has closed")
        store = runtime._store(capture)
        if action == "prepare":
            run_id = collaboration_desktop.prepare(runtime, capture, store, message, managed=True)
        else:
            run_id = message.get("run_id")
            adapter = runtime._managed_sharing.get(run_id)
            if adapter is None or adapter.capture.scope != capture.scope:
                raise ScopeDenied("Prepare this conversation's managed sharing team before changing shared work")
            adapter.queue(copy.deepcopy(message))
        return runtime._view(capture, store, run_id)


def historical_view(store, scope, run_id):
    """Preserve safe retry fences and uncertain command history after restart."""
    if not run_id or scope.tenant_id == f"personal:{scope.owner_id}":
        return None
    with store._connection() as connection:
        store._run(connection, scope, run_id)
        rows = connection.execute("SELECT key,payload FROM commands WHERE run_id=? AND actor='managed-sharing-dispatch' ORDER BY rowid DESC LIMIT 20",
                                  (run_id,)).fetchall()
        accepted = [row["key"] for row in connection.execute("SELECT key FROM commands WHERE run_id=? AND actor='managed-sharing-work' ORDER BY key", (run_id,))]
        history = []
        for row in rows:
            result = connection.execute("SELECT result FROM commands WHERE run_id=? AND actor='managed-sharing-outcome' AND key=?",
                                        (run_id, row["key"])).fetchone()
            history.append({"request_id": row["key"], "action": json.loads(row["payload"]).get("action", "unknown"),
                            "outcome": json.loads(result["result"])["outcome"] if result else "unconfirmed"})
    if not history and not accepted:
        return None
    return {"available": False, "address": None, "operation": None, "grants": {"items": [], "next_cursor": None},
            "detail": None, "messages": {"items": [], "next_cursor": None}, "selected_content": None,
            "accepted_work_item_ids": accepted, "history": history}
