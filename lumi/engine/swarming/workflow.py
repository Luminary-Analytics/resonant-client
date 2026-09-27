"""Durable owner operations around asynchronous integration effects.

The host constructs this facade from captured authority. Its JSON input is
trusted owner configuration, never a model tool contract. Reopening a facade
does not replay queued or interrupted operations. Close revokes new launches;
an already launched operation remains owned until its observation is recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from typing import Any

from .integration import ApplyApproval, CheckSpec, SwarmIntegration
from .integration_processes import IntegrationProcesses
from .models import Conflict, IdempotencyConflict, RevisionConflict, RunAuthority, ScopeDenied, StaleAuthority, require_id
from .store import canonical_json
from .supervisor import SwarmSupervisor
from .workers import DispatchClosed, DispatchGate

_FIELDS = {
    "prepare_candidate": {"writer_ids", "checks", "criterion_checks"},
    "run_check": {"candidate_id", "check_key"},
    "apply": {"candidate_id", "expected_base", "target_revision", "evidence", "approval_seconds"},
    "reconcile_application": {"approval_id"},
    "reconcile_operation": {"operation_id", "evidence"},
    "reconcile_effect": {"effect_kind", "effect_id", "evidence"},
}
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_OBSERVATIONS = frozenset({"reconcile_application", "reconcile_operation", "reconcile_effect"})


class IntegrationWorkflow:
    """Dispatch exact owner intents without holding a caller's lock for Git.

    ``submit`` commits before starting a background thread and returns the
    admission receipt. Its key is scoped by run and captured supervisor. Exact
    retries return that same operation, including after lost acknowledgements;
    they never create another thread. Stop remains a supervisor command, which
    the integration check runner observes and joins before recording cancellation.
    """

    def __init__(self, supervisor: SwarmSupervisor, authority: RunAuthority, integration: SwarmIntegration,
                 *, observer: Any = None):
        if supervisor.store.path != integration.store.path:
            raise ScopeDenied("Integration workflow requires the same captured store")
        if observer is not None and not callable(observer):
            raise ValueError("An integration observer must be a trusted callable")
        self.supervisor, self.authority, self.integration = supervisor, authority, integration
        # Told each operation's recorded outcome (kind, state, result): the audit
        # log's integration steps (organization.TeamGovernance.integration_observed).
        self._observer = observer
        self.store = supervisor.store
        with self.store._connection() as connection:
            run = self.store._authority(connection, authority)
            if not run["managed"]:
                raise Conflict("Integration workflow requires managed supervisor ownership")
        self._gate = DispatchGate()
        self._closed = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()

    @staticmethod
    def _payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in _FIELDS or type(payload) is not dict:
            raise ValueError("Unsupported integration operation")
        value = json.loads(canonical_json(payload))
        if len(canonical_json(value).encode()) > 128 * 1024:
            raise ValueError("Integration intent exceeds 128 KiB")
        fields = _FIELDS[kind]
        required = fields - ({"approval_seconds"} if kind == "apply" else set())
        if set(value) - fields or not required <= set(value):
            raise ValueError("Invalid integration operation fields")
        if kind == "prepare_candidate":
            writers, checks, mapping = value["writer_ids"], value["checks"], value["criterion_checks"]
            if type(writers) is not list or not 1 <= len(writers) <= 256 or any(type(item) is not str for item in writers) or len(set(writers)) != len(writers):
                raise ValueError("Candidate requires unique explicit writer identities")
            for identity in writers:
                require_id(identity)
            if type(checks) is not list or not 1 <= len(checks) <= 64:
                raise ValueError("Candidate requires one to 64 named checks")
            keys = []
            for check in checks:
                if type(check) is not dict or set(check) != {"key", "argv", "timeout_seconds"} or type(check["argv"]) is not list:
                    raise ValueError("Checks require key, argv and timeout_seconds")
                CheckSpec(check["key"], tuple(check["argv"]), check["timeout_seconds"])
                keys.append(check["key"])
            if len(set(keys)) != len(keys):
                raise ValueError("Candidate check names must be unique")
            if type(mapping) is not dict or not mapping or any(type(item) is not dict or not item for item in mapping.values()):
                raise ValueError("Candidate requires explicit work criterion mappings")
            for work_id, criteria in mapping.items():
                require_id(work_id)
                for criterion, check_key in criteria.items():
                    require_id(criterion)
                    if check_key not in keys:
                        raise ValueError("Each criterion must refer to a declared named check")
        else:
            for key in fields - {"evidence", "approval_seconds"}:
                require_id(value[key])
        if kind in {"apply", "reconcile_operation", "reconcile_effect"}:
            SwarmSupervisor._decision_evidence(value["evidence"])
        if kind == "apply":
            seconds = value.get("approval_seconds", 60)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 300:
                raise ValueError("Application approval lifetime must be one to 300 seconds")
            SwarmIntegration._revision(value["expected_base"])
            SwarmIntegration._revision(value["target_revision"])
        return value

    def _validate_records(self, connection, kind, payload):
        if kind == "reconcile_effect":
            effect = IntegrationProcesses.binding(connection, self.authority.run_id, payload["effect_kind"], payload["effect_id"])
            if effect["repo_key"] != self.integration.repo_key:
                raise ScopeDenied("Effect is unavailable in the captured repository")
            if effect["epoch"] >= self.authority.epoch:
                raise Conflict("Effect reconciliation requires a fenced historical owner")
            return
        if kind == "reconcile_operation":
            self._retained_operation(connection, payload["operation_id"])
            return
        if kind == "prepare_candidate":
            for identity in payload["writer_ids"]:
                row = connection.execute("SELECT epoch,state FROM writer_worktrees WHERE id=? AND run_id=? AND repo_key=?",
                    (identity, self.authority.run_id, self.integration.repo_key)).fetchone()
                if row is None:
                    raise ScopeDenied("Writer is unavailable in this run and repository")
                if row["epoch"] != self.authority.epoch or row["state"] != "ready":
                    raise Conflict("Candidate preparation requires current finalized writers")
            return
        if kind == "reconcile_application":
            row = connection.execute("SELECT a.id FROM integration_applications a JOIN integration_candidates c ON c.id=a.candidate_id "
                "WHERE a.id=? AND c.run_id=? AND c.repo_key=?", (payload["approval_id"], self.authority.run_id, self.integration.repo_key)).fetchone()
            if row is None:
                raise ScopeDenied("Application is unavailable in this run and repository")
            return
        row = connection.execute("SELECT * FROM integration_candidates WHERE id=? AND run_id=? AND repo_key=?",
            (payload["candidate_id"], self.authority.run_id, self.integration.repo_key)).fetchone()
        if row is None:
            raise ScopeDenied("Candidate is unavailable in this run and repository")
        if row["epoch"] != self.authority.epoch:
            raise StaleAuthority("Candidate belongs to a historical execution epoch")
        if kind == "run_check":
            if row["state"] not in {"ready", "failed", "verified"}:
                raise Conflict("Candidate is unavailable for verification")
            if payload["check_key"] not in {check["key"] for check in json.loads(row["manifest_json"])["checks"]}:
                raise ScopeDenied("Check was not declared for this candidate")
        elif (row["state"] != "verified" or (row["base_revision"], row["result_revision"]) != (payload["expected_base"], payload["target_revision"])):
            raise Conflict("Approval must bind an exactly verified candidate and base")

    def submit(self, kind: str, payload: dict[str, Any], *, command_id: str, expected_revision: int) -> dict[str, Any]:
        """Commit one exact intent and launch it once; never retry old effects."""
        require_id(command_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Integration submission requires the observed run revision")
        payload = self._payload(kind, payload)
        encoded = canonical_json(payload)
        identity = "integration_" + hashlib.sha256(canonical_json(
            [self.authority.run_id, self.authority.supervisor_id, command_id]).encode()).hexdigest()
        # Only short SQLite work and thread registration hold this gate. No Git,
        # subprocess wait, or caller-owned UI/service lock enters the worker.
        with self._gate.admit():
            with self.store._connection(write=True) as connection:
                run = self.store._authority(connection, self.authority)
                old = connection.execute("SELECT * FROM integration_operations WHERE id=?", (identity,)).fetchone()
                if old:
                    if (old["kind"], old["payload_json"], old["expected_revision"], old["epoch"]) != (kind, encoded, expected_revision, self.authority.epoch):
                        raise IdempotencyConflict("Integration command key already binds different semantics")
                    return self._view(dict(old))
                if run["revision"] != expected_revision:
                    raise RevisionConflict("Run revision changed before integration admission")
                if kind not in _OBSERVATIONS:
                    self.store._admitting(run)
                self._validate_records(connection, kind, payload)
                expiry = self.store.clock() + payload.get("approval_seconds", 60) if kind == "apply" else None
                effect_id = (payload["approval_id"] if kind == "reconcile_application" else
                             payload["operation_id"] if kind == "reconcile_operation" else
                             payload["effect_id"] if kind == "reconcile_effect" else identity)
                connection.execute("INSERT INTO integration_operations(id,run_id,supervisor_id,epoch,command_id,expected_revision,kind,payload_json,effect_id,approval_expires_at,state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'queued')", (identity, run["id"], self.authority.supervisor_id, self.authority.epoch,
                        command_id, expected_revision, kind, encoded, effect_id, expiry))
                connection.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run["id"],))
                self.store._event(connection, run["id"], "integration_operation_queued", {"operation_id": identity, "kind": kind})
                record = dict(connection.execute("SELECT * FROM integration_operations WHERE id=?", (identity,)).fetchone())
            thread = threading.Thread(target=self._run, args=(record,), daemon=True, name=f"swarm-integration-{identity[-10:]}")
            with self._threads_lock:
                self._threads[identity] = thread
            try:
                thread.start()
            except BaseException:
                self._finish(record, "cancelled", {}, "Host could not launch integration operation")
                raise
            return self._view(record)

    def _execute(self, record):
        payload = json.loads(record["payload_json"])
        kind, identity = record["kind"], record["effect_id"]
        if kind == "prepare_candidate":
            checks = tuple(CheckSpec(check["key"], tuple(check["argv"]), check["timeout_seconds"]) for check in payload["checks"])
            return self.integration.prepare_candidate(self.authority, writer_ids=tuple(payload["writer_ids"]), required_checks=checks,
                candidate_id=identity, criterion_checks=payload["criterion_checks"])
        if kind == "run_check":
            return self.integration.run_check(self.authority, payload["candidate_id"], payload["check_key"], receipt_id=identity)
        if kind == "apply":
            approval = ApplyApproval(identity, self.authority.scope, self.authority.run_id, payload["candidate_id"],
                                     payload["expected_base"], payload["target_revision"], record["approval_expires_at"])
            return self.integration.apply(self.authority, payload["candidate_id"], approval=approval)
        if kind == "reconcile_operation":
            return self._reconcile_operation(payload["operation_id"], payload["evidence"])
        if kind == "reconcile_effect":
            return IntegrationProcesses(self.store).reconcile_effect(self.authority, payload["effect_kind"],
                payload["effect_id"], evidence=payload["evidence"])
        return self.integration.reconcile_application(self.authority, payload["approval_id"])

    @staticmethod
    def _result(record):
        return {key: record[key] for key in ("id", "state", "candidate_id", "check_key", "candidate_revision",
                "base_revision", "result_revision", "expected_base", "target_revision", "observed_revision", "exit_code",
                "operation_id", "effect_id", "effect_kind", "effect_state", "invocation_observed", "processes_observed") if key in record}

    @staticmethod
    def _state(result):
        state = result.get("state")
        if state in {"ready", "verified", "passed", "applied", "completed"}:
            return "completed"
        if state in {"conflict", "failed", "not_applied", "input_changed", "timed_out"}:
            return "failed"
        return "cancelled" if state in {"cancelled", "not_started", "superseded"} else "uncertain"

    def _retained_operation(self, connection, operation_id):
        row = connection.execute("SELECT * FROM integration_operations WHERE id=? AND run_id=?",
                                 (operation_id, self.authority.run_id)).fetchone()
        if row is None:
            raise ScopeDenied("Integration operation is unavailable in this run")
        if row["epoch"] >= self.authority.epoch:
            raise Conflict("Operation reconciliation requires a fenced historical epoch")
        return dict(row)

    def _effect(self, connection, record):
        if record["kind"] in _OBSERVATIONS:
            return None  # Observation commands launch no external effect.
        table = {"prepare_candidate": "integration_candidates", "run_check": "integration_checks",
                 "apply": "integration_applications"}[record["kind"]]
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (record["effect_id"],)).fetchone()
        if row is None:
            return None
        candidate = row if record["kind"] == "prepare_candidate" else connection.execute(
            "SELECT * FROM integration_candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        if candidate is None or (candidate["run_id"], candidate["repo_key"]) != (self.authority.run_id, self.integration.repo_key):
            raise ScopeDenied("Integration effect is unavailable in this run and repository")
        return dict(row)

    def _reconcile_operation(self, operation_id, evidence):
        """Observe only persisted facts after the old effect owner is fenced."""
        # OS observation runs outside a SQLite transaction. The observer
        # rechecks identity and current authority before recording cleanup.
        with self.store._connection() as connection:
            self.store._authority(connection, self.authority)
            retained = self._retained_operation(connection, operation_id)
            effect = self._effect(connection, retained)
        if effect is not None and self._state(effect) not in _TERMINAL and effect.get("process_protocol") == 1:
            effect_kind = {"prepare_candidate": "candidate", "run_check": "check", "apply": "application"}[retained["kind"]]
            IntegrationProcesses(self.store).reconcile_effect(self.authority, effect_kind, retained["effect_id"], evidence=evidence)
        with self.store._connection(write=True) as connection:
            self.store._authority(connection, self.authority)
            target = self._retained_operation(connection, operation_id)
            state = target["state"]
            result = json.loads(target["result_json"])
            if state not in _TERMINAL:
                effect = self._effect(connection, target)
                if effect is None:
                    # Every integration mutation records its unique intent in
                    # the same store before invoking Git/check execution. An
                    # old owner cannot commit a missing intent after takeover.
                    state = "cancelled"
                    result = {"id": target["effect_id"], "state": "cancelled" if target["kind"] in _OBSERVATIONS else "not_started"}
                else:
                    state, result = self._state(effect), self._result(effect)
                    if state not in _TERMINAL:
                        raise Conflict("Uncertain Git/check effects require independent process or application observation")
                connection.execute("UPDATE integration_operations SET state=?,result_json=?,error='' WHERE id=?",
                                   (state, canonical_json(result), operation_id))
            self.store._event(connection, self.authority.run_id, "integration_operation_reconciled",
                {"operation_id": operation_id, "state": state, "result": result, "evidence": evidence})
            return {"operation_id": operation_id, "effect_id": target["effect_id"],
                    "effect_state": result.get("state"), "state": state}

    def _observed_effect(self, record):
        if record["kind"] in {"reconcile_operation", "reconcile_effect"}:
            return None
        table = {"prepare_candidate": "integration_candidates", "run_check": "integration_checks",
                 "apply": "integration_applications", "reconcile_application": "integration_applications"}[record["kind"]]
        with self.store._connection() as connection:
            self.store._run(connection, self.authority.scope, self.authority.run_id)
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (record["effect_id"],)).fetchone()
            return dict(row) if row else None

    def _run(self, record):
        try:
            with self._gate.admit():
                if self._closed.is_set():
                    raise DispatchClosed("Closed before integration invocation")
                with self.store._connection(write=True) as connection:
                    run = self.store._authority(connection, self.authority)
                    if record["kind"] not in _OBSERVATIONS:
                        self.store._admitting(run)
                    connection.execute("UPDATE integration_operations SET state='running' WHERE id=? AND state='queued'", (record["id"],))
                    self.store._event(connection, self.authority.run_id, "integration_operation_started", {"operation_id": record["id"]})
            result = self._execute(record)
            self._finish(record, self._state(result), self._result(result), "")
        except BaseException as exc:
            try:
                result = self._observed_effect(record)
                state = self._state(result) if result else ("cancelled" if isinstance(exc, DispatchClosed) else "failed")
                self._finish(record, state, self._result(result or {}), f"Integration requires inspection ({type(exc).__name__})")
            except BaseException:
                # A missing acknowledgement must not authorize replay. The
                # durable queued/running row remains an unresolved blocker.
                pass

    def _finish(self, record, state, result, error):
        with self.store._connection(write=True) as connection:
            self.store._run(connection, self.authority.scope, self.authority.run_id)
            fresh = True
            try:
                self.store._authority(connection, self.authority)
            except StaleAuthority:
                fresh = False
                state = "uncertain"
            changed = connection.execute("UPDATE integration_operations SET state=?,result_json=?,error=? "
                "WHERE id=? AND supervisor_id=? AND epoch=? AND state IN ('queued','running','uncertain')",
                (state, canonical_json(result), error, record["id"], self.authority.supervisor_id, self.authority.epoch)).rowcount
            if not changed:
                return  # A newer explicit observer already settled this dispatch.
            if fresh and record["kind"] == "reconcile_application" and result.get("state") in {"applied", "not_applied"}:
                # The explicit observer can settle an older apply dispatch, but
                # only by its exact durable application identity and outcome.
                connection.execute("UPDATE integration_operations SET state=?,result_json=?,error='' "
                    "WHERE run_id=? AND kind IN ('apply','reconcile_application') AND effect_id=? AND state IN ('queued','running','uncertain')",
                    (state, canonical_json(result), self.authority.run_id, record["effect_id"]))
            self.store._event(connection, self.authority.run_id, "integration_operation_observed",
                              {"operation_id": record["id"], "state": state, "result": result, "error": error})
            if fresh:
                self.supervisor._control_checkpoint(connection, self.authority.run_id)
        if self._observer is not None:
            try:
                self._observer(record["kind"], state, dict(result))
            except Exception:  # noqa: BLE001 - an observer never changes a recorded outcome
                pass

    def _view(self, record):
        with self._threads_lock:
            thread = self._threads.get(record["id"])
        return self.inspect_row(record, active=bool(thread and thread.is_alive()))

    @staticmethod
    def inspect_row(record, *, active: bool = False) -> dict[str, Any]:
        """Project an already scoped durable row without private command data."""
        return {"id": record["id"], "command_id": record["command_id"], "kind": record["kind"], "epoch": record["epoch"],
                "state": record["state"], "active": active, "effect_id": record["effect_id"],
                "approval_expires_at": record["approval_expires_at"], "result": IntegrationWorkflow._result(json.loads(record["result_json"])),
                "error": record["error"], "requires_reconciliation": record["state"] == "uncertain" or (record["state"] not in _TERMINAL and not active)}

    def inspect(self, operation_id: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        """Read scoped safe status without launching or resolving any effect."""
        with self.store._connection() as connection:
            self.store._run(connection, self.authority.scope, self.authority.run_id)
            records = [dict(row) for row in connection.execute("SELECT * FROM integration_operations WHERE run_id=? ORDER BY rowid",
                                                              (self.authority.run_id,))]
        if operation_id is not None:
            for record in records:
                if record["id"] == operation_id:
                    return self._view(record)
            raise ScopeDenied("Integration operation is unavailable in this run")
        return [self._view(record) for record in records]

    def close(self, *, timeout: float = 1) -> list[dict[str, Any]]:
        """Revoke launches and wait briefly; active or unknown effects stay visible."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Close timeout must be finite and nonnegative")
        self._closed.set()
        self._gate.close()
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            threads = list(self._threads.values())
        for thread in threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        return self.inspect()


__all__ = ["IntegrationWorkflow", "DispatchClosed"]
