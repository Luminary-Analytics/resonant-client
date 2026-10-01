"""Explicit observation-only reconciliation of earlier managed run epochs.

This facade never renews an old lease, sends a dispatch, infers an absent remote
reservation from a denial, or accepts a browser-supplied process/outcome proof.
The local recovery owner must first resolve native accounting. Remote unknown
request units may remain held, but every old shared worker slot must have an
acknowledged cleanup observation before a new epoch can attach.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

from .models import Conflict, ScopeDenied, require_id


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ManagedRecovery:
    """One explicitly acquired native epoch, with no old dispatch authority."""

    def __init__(self, desktop, recovery, workspace, state_root):
        self.desktop, self.recovery = desktop, recovery
        self.store, self.authority = recovery.store, recovery.authority
        self.workspace, self.state_root = workspace, state_root
        if not 2 <= self.authority.epoch <= 256:
            raise Conflict("Managed recovery requires a bounded replacement epoch")
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._journals = {}
        self._effects = {}
        self._missing_effects = set()
        self._flush_cursor = 0
        self._fenced = False
        self._attachment = None
        self._current()

    def _current(self):
        if self.recovery.authority != self.authority:
            raise ScopeDenied("Managed recovery ownership changed")
        with self.store._connection() as connection:
            return dict(self.store._authority(connection, self.authority))

    def fence(self):
        """Explicitly close retained permits after the native epoch takeover."""
        with self._lock:
            self._current()
            if not self._fenced:
                # Existing-file-only lookup verifies scope, certificate, owner,
                # host generation and workspace. Missing history stays blocked.
                journals = {epoch: self.desktop.open_history(self.authority.scope, self.authority.run_id,
                    epoch, self.workspace, self.state_root) for epoch in range(1, self.authority.epoch)}
                for journal in journals.values():
                    journal.fence_restart()
                from .managed_effects import ManagedEffects
                for epoch, journal in journals.items():
                    if journal.path.with_suffix(".effects.sqlite").is_file():
                        effects = ManagedEffects.open_history(self.store, journal, self.desktop._client)
                        effects.fence_restart()
                        self._effects[epoch] = effects
                    else:
                        with self.store._connection() as connection:
                            count = connection.execute("SELECT count(*) FROM integration_processes WHERE run_id=? AND epoch=?",
                                                       (self.authority.run_id, epoch)).fetchone()[0]
                        if count:
                            self._missing_effects.add(epoch)
                self._journals, self._fenced = journals, True
            return self.inspect()

    def _journal(self, epoch):
        if not self._fenced:
            raise Conflict("Fence retained managed permits before reconciliation")
        if type(epoch) is not int or epoch not in self._journals:
            raise ScopeDenied("Managed history is outside this captured recovery")
        return self._journals[epoch]

    def _record(self, epoch, kind, local_id):
        require_id(local_id)
        journal = self._journal(epoch)
        with journal._connection() as connection:
            if kind == "request":
                row = connection.execute("SELECT * FROM requests WHERE local_id=?", (local_id,)).fetchone()
            else:
                key = journal._worker_key(local_id, epoch) if kind == "worker" else local_id
                row = connection.execute("SELECT * FROM effects WHERE kind=? AND local_id=?", (kind, key)).fetchone()
            if row is None:
                raise ScopeDenied("Managed receipt is unavailable in this epoch")
            return journal, journal._view(row)

    @staticmethod
    def _event(connection, run_id, kind, key, identity):
        # Keys and event kinds are constants selected by trusted code below.
        row = connection.execute("SELECT sequence,epoch,kind,payload FROM events WHERE run_id=? AND kind=? "
            "AND json_extract(payload,?)=? ORDER BY sequence DESC LIMIT 1",
            (run_id, kind, "$." + key, identity)).fetchone()
        return dict(row) if row else None

    def _request_proof(self, epoch, local_id, record):
        with self.store._connection() as connection:
            self.store._authority(connection, self.authority)
            row = connection.execute("SELECT r.*,a.run_id,a.grant_json,i.input_sha256,i.purpose AS input_purpose,"
                "i.observation_outcome FROM model_requests r JOIN attempts a ON a.id=r.attempt_id "
                "LEFT JOIN request_inputs i ON i.request_id=r.id WHERE r.id=? AND a.run_id=? AND r.epoch=?",
                (local_id, self.authority.run_id, epoch)).fetchone()
            if row is None:
                raise ScopeDenied("Native request proof is unavailable")
            expected = record["semantics"]
            model = json.loads(row["grant_json"])["model"]
            if (expected["attempt_id"] != row["attempt_id"] or expected["attempt_epoch"] != epoch
                    or expected["model"] != model or expected["input_sha256"] != row["input_sha256"]
                    or expected["purpose"] != row["input_purpose"]):
                raise ScopeDenied("Native request identity differs from its managed receipt")
            state = row["state"]
            if state not in {"completed", "failed", "not_started"}:
                raise Conflict("Resolve the native request outcome before reporting managed accounting")
            event = self._event(connection, self.authority.run_id, "command_reconcile_request", "result.request_id", local_id)
            if state == "completed" and row["observation_outcome"] == "completed":
                event = self._event(connection, self.authority.run_id, "request_observed", "request_id", local_id)
            elif state == "not_started" and not event:
                event = self._event(connection, self.authority.run_id, "command_worker_stopped", "result.attempt_id", row["attempt_id"])
            if not event or row["used"] != (0 if state == "not_started" else 1):
                raise Conflict("Native accounting lacks an attributable settled observation")
            if state == "completed" and not record["dispatch_claimed"]:
                raise Conflict("A native outcome cannot invent a managed dispatch claim")
            if state == "failed" and not record["start_attempted"]:
                raise Conflict("A failed managed request requires its original remote start attempt")
            if state == "not_started" and (record["start_attempted"] or record["dispatch_claimed"]):
                raise Conflict("A possible remote invocation remains held; it cannot be refunded")
            outcome = "never_started" if state == "not_started" else state
            return outcome, _digest({"kind": "request", "run_id": self.authority.run_id, "epoch": epoch,
                "local_id": local_id, "semantics": expected, "state": state, "used": row["used"], "event": event})

    def reconcile_request(self, epoch, request_id):
        """Report exact native accounting; unknown/possible starts never refund."""
        self._current()
        journal, record = self._record(epoch, "request", request_id)
        outcome, evidence = self._request_proof(epoch, request_id, record)
        journal.reconcile_request(request_id, outcome, evidence)
        return {"kind": "request", "epoch": epoch, "local_id": request_id, "outcome": outcome,
                "delivery": self.flush()}

    def _action_proof(self, epoch, local_id, record):
        with self.store._connection() as connection:
            self.store._authority(connection, self.authority)
            row = connection.execute("SELECT r.* FROM action_receipts r JOIN attempts a ON a.id=r.attempt_id "
                "WHERE r.id=? AND a.run_id=? AND r.epoch=?", (local_id, self.authority.run_id, epoch)).fetchone()
            if row is None:
                raise ScopeDenied("Native action proof is unavailable")
            journal = self._journal(epoch)
            with journal._connection() as history:
                request = history.execute("SELECT remote_id FROM requests WHERE local_id=?", (row["request_id"],)).fetchone()
                worker = history.execute("SELECT remote_id FROM effects WHERE kind='worker' AND local_id=?",
                                         (journal._worker_key(row["attempt_id"], epoch),)).fetchone()
            expected = record["semantics"]
            if (not request or not worker or expected != {"request_id": request[0], "worker_id": worker[0],
                    "tool_name": row["tool_name"], "arguments_sha256": row["arguments_sha256"]}):
                raise ScopeDenied("Native action identity differs from its managed receipt")
            if row["state"] != "completed" or not record["claimed"]:
                raise Conflict("Resolve the native action observation before reporting managed completion")
            event = self._event(connection, self.authority.run_id, "command_reconcile_action", "result.action_id", local_id)
            if event is None:
                event = self._event(connection, self.authority.run_id, "action_observed", "receipt_id", local_id)
            if event is None:
                raise Conflict("Native action lacks an attributable settled observation")
            return _digest({"kind": "action", "run_id": self.authority.run_id, "epoch": epoch,
                "local_id": local_id, "request_id": row["request_id"], "call_id": row["call_id"],
                "semantics": expected, "artifact_id": row["output_artifact_id"], "is_error": row["is_error"],
                "metadata_sha256": _digest(row["metadata_json"]), "event": event})

    def reconcile_action(self, epoch, action_id):
        """Retain a known action disposition, never claim artifact quality."""
        self._current()
        journal, record = self._record(epoch, "action", action_id)
        evidence = self._action_proof(epoch, action_id, record)
        journal.reconcile_action(action_id, "completed", evidence)
        return {"kind": "action", "epoch": epoch, "local_id": action_id, "outcome": "completed",
                "delivery": self.flush()}

    def reconcile_worker(self, epoch, attempt_id):
        """Release an old slot only after exact native process identity proof."""
        self._current()
        journal, record = self._record(epoch, "worker", attempt_id)
        if record["semantics"]["attempt_id"] != attempt_id or record["semantics"]["attempt_epoch"] != epoch:
            raise ScopeDenied("Managed worker identity differs")
        # OS observation and its local persistence are outside journal locks.
        observation = self.recovery.reconcile_process(attempt_id)
        if observation.get("observation") != "stopped" or not observation.get("termination_recorded"):
            raise Conflict("The captured worker process has not been proven stopped")
        with self.store._connection() as connection:
            self.store._authority(connection, self.authority)
            attempt = connection.execute("SELECT epoch,process_state FROM attempts WHERE id=? AND run_id=?",
                                         (attempt_id, self.authority.run_id)).fetchone()
            if not attempt or attempt["epoch"] != epoch or attempt["process_state"] != "stopped":
                raise ScopeDenied("Native termination differs from this managed worker")
        outcome = "stopped" if record["claimed"] else "never_started"
        proof = {key: observation[key] for key in ("source", "host_id", "pid", "created_at", "observation")}
        evidence = _digest({"kind": "worker", "run_id": self.authority.run_id, "epoch": epoch,
                            "attempt_id": attempt_id, "process": proof})
        journal.reconcile_worker(attempt_id, epoch, outcome, evidence)
        return {"kind": "worker", "epoch": epoch, "local_id": attempt_id, "outcome": outcome,
                "delivery": self.flush()}

    def flush(self, *, maximum=8):
        """Retry old observations only, without acquiring any lease or permit."""
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError("Invalid managed recovery delivery limit")
        if not self._fenced:
            raise Conflict("Fence retained managed permits before delivery")
        if not self._flush_lock.acquire(blocking=False):
            return {"delivered": 0, "unavailable": True}
        delivered, attempted, unavailable = 0, 0, False
        try:
            deadline = time.monotonic() + 5
            # Rotate both across epochs and across the separate owner-effect
            # lane. Permanently denied history must not starve known cleanup.
            sources = [("journal", item) for item in self._journals.values()]
            sources += [("effects", item) for item in self._effects.values()]
            empty = 0
            while attempted < maximum and time.monotonic() < deadline and empty < len(sources):
                kind, journal = sources[self._flush_cursor % len(sources)]
                self._flush_cursor += 1
                if kind == "effects":
                    if not journal.inspect()["pending_observations"]:
                        empty += 1
                        continue
                    empty = 0
                    attempted += 1
                    result = journal.flush(maximum=1)
                    delivered += result["delivered"]
                    unavailable |= result["unavailable"]
                    continue
                pending = journal.pending(limit=1)
                if not pending:
                    empty += 1
                    continue
                empty = 0
                for item in pending:
                    if item["kind"] not in {"settle", "observe_worker", "observe_tool", "observe_control", "ingest"}:
                        raise Conflict("Unsupported retained managed observation")
                    attempted += 1
                    journal.mark_delivery_attempt(item["id"])
                    try:
                        receipt = getattr(self.desktop._client, item["kind"])(**item["payload"])
                        journal.acknowledge(item["id"], receipt)
                        delivered += 1
                    except Exception:
                        unavailable = True
            return {"delivered": delivered, "unavailable": unavailable}
        finally:
            self._flush_lock.release()

    def reconcile_effect(self, epoch, process_id):
        """Report only already retained native integration-process cleanup."""
        self._current()
        self._journal(epoch)
        if epoch not in self._effects:
            raise Conflict("Managed owner-process history is unavailable")
        require_id(process_id)
        self._effects[epoch].reconcile_process(self.authority, process_id)
        return {"kind": "effect", "epoch": epoch, "local_id": process_id, "delivery": self.flush()}

    def fence_absent(self, epoch, kind, local_id):
        """Explicitly fence a possible lost admission; presence proves no outcome."""
        self._current()
        journal = self._journal(epoch)
        if kind == "effects":
            target = self._effects.get(epoch)
            if target is None:
                raise Conflict("Managed owner-process history is unavailable")
            intent = target.absence_intent(local_id)
        else:
            names = {"requests": "request", "workers": "worker", "actions": "action"}
            if kind not in names:
                raise ValueError("Select a request, worker, tool or owner-process admission")
            target = journal
            key = journal._worker_key(local_id, epoch) if kind == "workers" else local_id
            intent = journal.absence_intent(names[kind], key)
        receipt = self.desktop._client.fence_absent(**intent)
        # The server atomically prevents any delayed admission after its absent
        # result. A present or denied result never changes local accounting.
        self._current()
        if kind == "effects":
            target.record_absence(local_id, receipt)
        else:
            target.record_absence(names[kind], key, receipt)
        return {"kind": kind, "epoch": epoch, "local_id": local_id, "state": "fenced_absent"}

    def _effect_views(self):
        return [{"epoch": epoch, **effects.inspect()} for epoch, effects in self._effects.items()]

    @staticmethod
    def _slot_blockers(journal):
        # Inspect every row, not the capped history shown in a browser.
        with journal._connection() as connection:
            return connection.execute("SELECT count(*) FROM effects e WHERE kind='worker' AND ("
                "outcome IS NULL OR outcome NOT IN ('stopped','never_started') OR (phase<>'not_admitted' AND lease IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM outbox o WHERE o.semantic_key='effect:'||e.remote_id||':'||e.outcome "
                "AND o.kind='observe_worker' AND o.receipt IS NOT NULL)))").fetchone()[0]

    @staticmethod
    def _unconfirmed_request_units(journal):
        with journal._connection() as connection:
            return connection.execute("SELECT count(*) FROM requests r WHERE lease IS NOT NULL AND phase!='not_admitted' "
                "AND (outcome IS NULL OR outcome='uncertain' OR NOT EXISTS (SELECT 1 FROM outbox o "
                "WHERE o.kind='settle' AND o.semantic_key='request:'||r.remote_id||':'||r.outcome "
                "AND o.receipt IS NOT NULL))").fetchone()[0]

    def _native_view(self, connection, epoch, kind, row):
        """Small proof labels only; raw provider/tool evidence stays local."""
        semantics = row.get("semantics", {})
        if kind == "requests":
            native = connection.execute("SELECT r.state,r.epoch,a.run_id,i.input_sha256,i.observation_outcome "
                "FROM model_requests r JOIN attempts a ON a.id=r.attempt_id "
                "LEFT JOIN request_inputs i ON i.request_id=r.id WHERE r.id=? AND a.run_id=? AND r.epoch=?",
                (row["local_id"], self.authority.run_id, epoch)).fetchone()
            return {"native_state": native["state"] if native else None,
                "identity_matches": bool(native and native["input_sha256"] == semantics["input_sha256"]),
                "native_observation": native["observation_outcome"] if native else None}
        if kind == "workers":
            native = connection.execute("SELECT a.process_state,p.state AS identity_state,p.pid FROM attempts a "
                "LEFT JOIN process_observations p ON p.attempt_id=a.id WHERE a.id=? AND a.run_id=? AND a.epoch=?",
                (semantics["attempt_id"], self.authority.run_id, epoch)).fetchone()
            return {"native_process_state": native["process_state"] if native else None,
                    "managed_process_identity": bool(native and native["pid"] is not None),
                    "identity_observation": native["identity_state"] if native else None}
        if kind == "actions":
            native = connection.execute("SELECT r.state,r.arguments_sha256 FROM action_receipts r JOIN attempts a ON a.id=r.attempt_id "
                "WHERE r.id=? AND a.run_id=? AND r.epoch=?", (row["local_id"], self.authority.run_id, epoch)).fetchone()
            return {"native_state": native["state"] if native else None,
                    "identity_matches": bool(native and native["arguments_sha256"] == semantics["arguments_sha256"])}
        return {}

    def inspect(self, *, epoch=None, kind="requests", after=None, limit=100):
        """Show bounded identities and proof status, without raw inputs or output."""
        if not self._fenced:
            return {"fenced": False, "epoch": self.authority.epoch, "records": [], "resume_ready": False}
        epoch = min(self._journals) if epoch is None else epoch
        journal = self._journal(epoch)
        page = journal.recovery_records(kind, after, limit)
        records = []
        with self.store._connection() as connection:
            self.store._run(connection, self.authority.scope, self.authority.run_id)
            for row in page["records"]:
                semantics = row.get("semantics", {})
                records.append({"local_id": semantics.get("attempt_id") if kind == "workers" else row.get("local_id", row.get("id")),
                    "remote_id": row.get("remote_request_id", row.get("remote_worker_id", row.get("remote_action_id", row.get("id")))),
                    "phase": row["phase"], "outcome": row.get("outcome"), "epoch": epoch,
                    "attempt_id": semantics.get("attempt_id"), "has_recovery_evidence": bool(row.get("recovery_evidence")),
                    "can_fence_absent": bool(row.get("lease") and row["phase"] != "not_admitted" and kind != "controls"
                        and not row.get("claimed") and not row.get("start_attempted") and not row.get("dispatch_claimed")),
                    **self._native_view(connection, epoch, kind, row)})
        blocked = sum(self._slot_blockers(item) for item in self._journals.values())
        effects = self._effect_views()
        effects_pending = sum(item["cleanup_pending"] for item in effects) + len(self._missing_effects)
        summaries = [{"epoch": value, **item.recovery_summary(), "request_units_unconfirmed": self._unconfirmed_request_units(item)}
                     for value, item in self._journals.items()]
        with self.store._connection() as connection:
            self.store._run(connection, self.authority.scope, self.authority.run_id)
            unresolved = self.recovery.supervisor._unresolved(connection, self.authority.run_id)
        return {"fenced": True, "epoch": self.authority.epoch, "history_epoch": epoch,
            "kind": kind, "records": records, "next_cursor": page["next_cursor"], "epochs": summaries,
            "worker_cleanup_pending": blocked, "local_unresolved": bool(unresolved),
            "resume_ready": not blocked and not unresolved and not effects_pending,
            "owner_effects": effects, "effect_cleanup_pending": effects_pending,
            "missing_effect_epochs": sorted(self._missing_effects),
            "unknown_request_units": sum(item["request_units_unconfirmed"] for item in summaries),
            "offline_request_allowance": 0}

    def prepare_resume(self):
        """Return a newly enforced epoch attachment; never execute local Recover."""
        with self._lock:
            run = self._current()
            if not self._fenced or run["state"] != "recovery_required":
                raise Conflict("Managed resume requires an explicitly fenced native recovery")
            if any(self._slot_blockers(journal) for journal in self._journals.values()):
                raise Conflict("Every prior managed worker slot needs acknowledged cleanup before resume")
            if self._missing_effects or any(not item["resume_ready"] for item in self._effect_views()):
                raise Conflict("Every prior managed owner process needs acknowledged cleanup before resume")
            with self.store._connection() as connection:
                self.store._authority(connection, self.authority)
                if self.recovery.supervisor._unresolved(connection, self.authority.run_id):
                    raise Conflict("Resolve native process effects and accounting before managed resume")
            if self._attachment is None:
                self._attachment = self.desktop.attach(self.authority, self.workspace, self.state_root)
            return self._attachment
