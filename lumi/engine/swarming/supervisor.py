"""Deterministic, local orchestration over one authoritative SQLite graph.

This module performs no model, filesystem, process or network effects. Trusted
runtime adapters authenticate callers and report observations. Commands cannot
turn model prose into authority or verification. Epoch fencing does not stop a
process; explicit termination and accounting reconciliation remain necessary.
"""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from dataclasses import asdict
from typing import Any

from ..execution_guard import FILE_TOOL_NAMES, SWARM_TOOL_NAMES
from .models import (
    PROTOCOL_VERSION, AdmissionClosed, Command, CommandReceipt, Conflict, IdempotencyConflict,
    RevisionConflict, RunAuthority, SchemaVersionError, Scope, ScopeDenied,
    StaleAuthority, require_id,
)
from .policy import AssignmentGrant, ModelSelection, PolicyProfile, SwarmPolicy, scopes_overlap
from .store import SwarmStore, _count, _id, _json, canonical_json

_REQUEST_TERMINAL = ("completed", "failed", "not_started")


class SwarmSupervisor:
    """Trusted versioned command boundary, never a model-facing dispatcher.

    The policy snapshot, DAG, leases, requests, submissions and check receipts
    all live in SwarmStore. No in-memory graph or worker registry owns status.
    Native provider/model selection is mandatory at assignment; credentials are
    deliberately absent from all persisted contracts.
    """

    def __init__(self, store: SwarmStore) -> None:
        self.store = store

    def create(
        self, scope: Scope, *, supervisor_id: str, objective: str,
        request_limit: int, policy: PolicyProfile, run_id: str | None = None,
        lease_seconds: float = 30,
    ) -> RunAuthority:
        """Commit initial ownership; retry never extends or renews its lease."""
        run_id = run_id or _id()
        require_id(run_id)
        require_id(supervisor_id)
        _count(request_limit)
        self._lease_duration(lease_seconds)
        policy = SwarmPolicy.intersect(policy)
        fingerprint = {"objective": objective, "request_limit": request_limit,
                       "policy": policy.to_dict(), "lease_seconds": lease_seconds}
        authority = RunAuthority(scope, run_id, supervisor_id, 1)
        actor = f"supervisor:{supervisor_id}"
        with self.store._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                self.store._authority(connection, authority)
                original = self.store._duplicate(connection, run_id, actor, "$create", fingerprint)
                if original is None:
                    raise IdempotencyConflict("Run was created by another protocol")
                return authority
            connection.execute(
                "INSERT INTO runs(id,tenant_id,owner_id,project_id,session_id,supervisor_id,epoch,"
                "protocol_version,objective,state,request_limit,managed,lease_until,lease_seconds,policy_json,worker_limit) "
                "VALUES(?,?,?,?,?,?,1,?,?,'running',?,1,?,?,?,?)",
                (run_id, *scope.values(), supervisor_id, PROTOCOL_VERSION, objective, request_limit,
                 self.store.clock() + lease_seconds, lease_seconds, _json(policy.to_dict()), policy.max_workers),
            )
            self.store._event(connection, run_id, "run_created", fingerprint)
            self.store._remember(connection, run_id, actor, "$create", fingerprint, {"epoch": 1})
        return authority

    @staticmethod
    def _lease_duration(seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 0 < seconds <= 300:
            raise ValueError("Lease duration must be more than zero and at most 300 seconds")

    def acquire(
        self, scope: Scope, run_id: str, *, expected_epoch: int, supervisor_id: str,
        command_id: str, lease_seconds: float = 30,
    ) -> RunAuthority:
        """Take an expired lease and fence old authority, without resuming work.

        A live lease cannot be stolen. Every unresolved process and request is
        retained as uncertain, including dispatches that might not have started.
        """
        require_id(supervisor_id)
        self._lease_duration(lease_seconds)
        actor = f"supervisor:{supervisor_id}"
        payload = {"operation": "acquire", "expected_epoch": expected_epoch, "lease_seconds": lease_seconds}
        with self.store._connection(write=True) as connection:
            run = self.store._run(connection, scope, run_id)
            previous = self.store._duplicate(connection, run_id, actor, command_id, payload)
            if previous is not None:
                authority = RunAuthority(scope, run_id, supervisor_id, previous["epoch"])
                self.store._authority(connection, authority)
                return authority
            if not run["managed"] or run["epoch"] != expected_epoch:
                raise StaleAuthority("Recovery epoch is no longer current")
            if run["lease_until"] > self.store.clock():
                raise Conflict("An active supervisor still owns this run")
            if run["state"] in ("cancelled", "completed", "failed"):
                raise Conflict("Terminal runs cannot resume")
            epoch = expected_epoch + 1
            connection.execute("UPDATE runs SET epoch=?,supervisor_id=?,lease_until=?,lease_seconds=?,"
                               "state='recovery_required',revision=revision+1 WHERE id=?",
                               (epoch, supervisor_id, self.store.clock() + lease_seconds, lease_seconds, run_id))
            connection.execute("UPDATE attempts SET state='uncertain',process_state='unknown' "
                               "WHERE run_id=? AND process_state IN ('pending','running')", (run_id,))
            connection.execute("UPDATE process_observations SET state='unknown' WHERE run_id=? AND state='started'", (run_id,))
            connection.execute("UPDATE work_items SET state='uncertain' WHERE run_id=? AND state IN ('leased','running')", (run_id,))
            connection.execute("UPDATE model_requests SET state='uncertain' WHERE state IN ('reserved','started') "
                               "AND attempt_id IN (SELECT id FROM attempts WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE action_receipts SET state='uncertain' WHERE state='admitted' "
                               "AND attempt_id IN (SELECT id FROM attempts WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE reservations SET state='uncertain' WHERE state='reserved' "
                               "AND attempt_id IN (SELECT id FROM attempts WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE dispatches SET state='uncertain' WHERE state IN ('pending','started') "
                               "AND attempt_id IN (SELECT id FROM attempts WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE writer_worktrees SET state='uncertain' WHERE run_id=? AND state IN ('creating','finalizing')", (run_id,))
            connection.execute("UPDATE integration_candidates SET state='uncertain' WHERE run_id=? AND state='preparing'", (run_id,))
            connection.execute("UPDATE integration_checks SET state='uncertain' WHERE state='running' AND candidate_id IN "
                               "(SELECT id FROM integration_candidates WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE integration_applications SET state='uncertain' WHERE state='applying' AND candidate_id IN "
                               "(SELECT id FROM integration_candidates WHERE run_id=?)", (run_id,))
            connection.execute("UPDATE integration_operations SET state='uncertain' WHERE run_id=? AND state IN ('queued','running')", (run_id,))
            connection.execute("UPDATE integration_processes SET state='unknown' WHERE run_id=? AND state IN ('intent','owned','invoked')", (run_id,))
            self.store._event(connection, run_id, "recovery_required", {"previous_epoch": expected_epoch})
            self.store._remember(connection, run_id, actor, command_id, payload, {"epoch": epoch})
        return RunAuthority(scope, run_id, supervisor_id, epoch)

    def handle(self, command: Command, authority: RunAuthority) -> CommandReceipt:
        """Apply one command atomically after scope, lease and revision checks."""
        return self.handle_once(command, authority)[0]

    def handle_once(self, command: Command, authority: RunAuthority) -> tuple[CommandReceipt, bool]:
        """Return whether this call made the first commit, for trusted dispatch.

        The boolean is transient, never stored as a reusable launch permit. A
        replay returns False even if the caller lost the original acknowledgement.
        Unknown commit outcomes raise and must not cause blind external dispatch.
        """
        if command.version != PROTOCOL_VERSION:
            raise SchemaVersionError("Unsupported command protocol version")
        if command.run_id != authority.run_id or command.epoch != authority.epoch:
            raise StaleAuthority("Command is not bound to this captured run and epoch")
        _count(command.expected_revision)
        # Copy nested user data once; a caller cannot mutate semantics mid-commit.
        semantics = json.loads(_json(asdict(command)))
        actor = f"supervisor:{authority.supervisor_id}"
        handlers = {
            "renew": self._renew, "plan": self._plan, "assign": self._assign,
            "set_concurrency": self._set_concurrency,
            "pause_worker": self._pause_worker, "resume_worker": self._resume_worker,
            "cancel_worker": self._cancel_worker, "steer_worker": self._steer_worker,
            "start_coordinator": self._start_coordinator,
            "decide_proposal": self._decide_proposal,
            "worker_started": self._worker_started, "worker_stopped": self._worker_stopped,
            "reserve_request": self._reserve_request, "start_request": self._start_request,
            "settle_request": self._settle_request, "reconcile_request": self._reconcile_request,
            "submit": self._submit, "record_check": self._record_check,
            "accept": self._accept, "complete": self._complete,
            "review_read_result": self._review_read_result,
            "accept_under_grant": self._accept_under_grant,
            "accept_writer": self._accept_writer,
            "pause": self._pause, "resume": self._resume, "stop": self._stop,
            "recover": self._recover,
            "reject": self._reject,
            "retry": self._retry,
            "reconcile_action": self._reconcile_action,
            "checkpoint": self._checkpoint,
        }
        if command.kind not in handlers:
            raise ValueError("Unsupported supervisor command")
        with self.store._connection(write=True) as connection:
            run = self.store._authority(connection, authority)
            if not run["managed"]:
                raise Conflict("This run uses the legacy storage spike contract")
            previous = self.store._duplicate(connection, authority.run_id, actor, command.command_id, semantics)
            if previous is not None:
                return CommandReceipt(**previous), False
            if run["revision"] != command.expected_revision:
                raise RevisionConflict("Run revision changed; refresh the snapshot")
            result = handlers[command.kind](connection, run, **semantics["payload"])
            self._control_checkpoint(connection, run["id"])
            connection.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run["id"],))
            self.store._event(connection, run["id"], f"command_{command.kind}",
                              {"command_id": command.command_id, "result": result})
            updated = connection.execute("SELECT revision,event_sequence,state FROM runs WHERE id=?", (run["id"],)).fetchone()
            receipt = CommandReceipt(updated["revision"], updated["event_sequence"], updated["state"], result)
            self.store._remember(connection, run["id"], actor, command.command_id, semantics, asdict(receipt))
            return receipt, True

    def _renew(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        until = self.store.clock() + run["lease_seconds"]
        connection.execute("UPDATE runs SET lease_until=? WHERE id=?", (until, run["id"]))
        return {"lease_until": until}

    def _set_concurrency(self, connection: sqlite3.Connection, run: sqlite3.Row, *, max_workers: int) -> dict[str, Any]:
        """Change admission capacity while retaining every existing grant."""
        if run["state"] not in ("running", "pausing", "paused"):
            raise AdmissionClosed("Worker concurrency can change only for an active or paused run")
        cap = PolicyProfile.from_dict(json.loads(run["policy_json"])).max_workers
        if type(max_workers) is not int or not 1 <= max_workers <= cap:
            raise ValueError("Worker concurrency must be a positive integer within the original policy limit")
        connection.execute("UPDATE runs SET worker_limit=? WHERE id=?", (max_workers, run["id"]))
        return {"max_workers": max_workers, "policy_max_workers": cap}

    def _controlled_attempt(self, connection: sqlite3.Connection, run: sqlite3.Row, attempt_id: str, attempt_epoch: int) -> sqlite3.Row:
        if run["state"] not in ("running", "pausing", "paused"):
            raise AdmissionClosed("Participant controls require an active or paused run")
        if type(attempt_epoch) is not int or attempt_epoch != run["epoch"]:
            raise StaleAuthority("Participant controls cannot revive a historical attempt")
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["state"] not in ("leased", "running") or attempt["process_state"] not in ("pending", "running"):
            raise Conflict("Only an active participant can receive owner controls")
        return attempt

    @staticmethod
    def _control_result(connection: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
        return dict(connection.execute("SELECT id AS attempt_id,epoch AS attempt_epoch,pause_requested,cancel_requested "
                                       "FROM attempts WHERE id=?", (attempt_id,)).fetchone())

    def _pause_worker(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int) -> dict[str, Any]:
        attempt = self._controlled_attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["cancel_requested"]:
            raise AdmissionClosed("A cancelled participant cannot be paused or resumed")
        connection.execute("UPDATE attempts SET pause_requested=1 WHERE id=?", (attempt_id,))
        return self._control_result(connection, attempt_id)

    def _resume_worker(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int) -> dict[str, Any]:
        attempt = self._controlled_attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["cancel_requested"]:
            raise AdmissionClosed("A cancelled participant cannot be paused or resumed")
        # This clears only the participant flag. Run admission is independently
        # checked at every request/tool boundary; run Pause and Stop still win.
        connection.execute("UPDATE attempts SET pause_requested=0 WHERE id=?", (attempt_id,))
        return self._control_result(connection, attempt_id)

    def _cancel_worker(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int) -> dict[str, Any]:
        self._controlled_attempt(connection, run, attempt_id, attempt_epoch)
        connection.execute("UPDATE attempts SET cancel_requested=1 WHERE id=?", (attempt_id,))
        # Intent is not termination, accounting settlement, or released capacity.
        return self._control_result(connection, attempt_id)

    def _steer_worker(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int, text: str) -> dict[str, Any]:
        attempt = self._controlled_attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["cancel_requested"]:
            raise AdmissionClosed("A cancelled participant cannot receive new guidance")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192:
            raise ValueError("Owner guidance must contain 1 to 8192 UTF-8 bytes")
        total, pending = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(CASE WHEN r.directive_id IS NULL THEN 1 ELSE 0 END),0) "
            "FROM owner_directives d LEFT JOIN owner_directive_receipts r ON r.directive_id=d.id WHERE d.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if total >= 1024 or pending >= 64:
            raise Conflict("Participant owner guidance limit reached")
        directive_id, digest = _id(), hashlib.sha256(text.encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO owner_directives VALUES(?,?,?,?,?,?,?)",
                           (directive_id, run["id"], attempt_id, attempt_epoch, run["owner_id"], text, digest))
        return {"attempt_id": attempt_id, "attempt_epoch": attempt_epoch, "directive_id": directive_id, "sha256": digest}

    def _plan(self, connection: sqlite3.Connection, run: sqlite3.Row, *, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        self.store._admitting(run)
        if not isinstance(work_items, list) or not work_items or len(work_items) > 256:
            raise ValueError("Plan must contain 1 to 256 work items")
        specs: dict[str, dict[str, Any]] = {}
        policy = PolicyProfile.from_dict(json.loads(run["policy_json"]))
        for item in work_items:
            if not isinstance(item, dict) or set(item) - {"id", "objective", "dependencies", "role", "tools", "read_roots", "write_roots", "criteria"}:
                raise ValueError("Invalid work item fields")
            require_id(item.get("id"))
            if item["id"] in specs:
                raise Conflict("Duplicate work item ID")
            if not isinstance(item.get("objective"), str) or not item["objective"].strip():
                raise ValueError("Work items require an objective")
            spec = {"id": item["id"], "objective": item["objective"],
                    "dependencies": item.get("dependencies", []), "role": item.get("role", "explore"),
                    "tools": item.get("tools", []), "read_roots": item.get("read_roots", ["."]),
                    "write_roots": item.get("write_roots", []), "criteria": item.get("criteria", [])}
            if spec["role"] not in ("explore", "implement", "verify"):
                raise ValueError("Unsupported work role")
            for field in ("dependencies", "tools", "read_roots", "write_roots", "criteria"):
                if not isinstance(spec[field], list) or len(spec[field]) != len(set(spec[field])):
                    raise ValueError("Work item list fields must contain unique strings")
                for value in spec[field]:
                    require_id(value)
            if spec["role"] != "implement" and spec["write_roots"]:
                raise Conflict("Only implement assignments may request write scope")
            # Admission performs the complete selected-provider policy check.
            if not set(spec["tools"]) <= policy.allowed_tools:
                raise Conflict("Plan requests tools outside its effective policy")
            specs[spec["id"]] = spec
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise Conflict("Work dependencies contain a cycle")
            if item_id in visited:
                return
            if item_id not in specs:
                raise Conflict("Dependency does not exist in this plan")
            visiting.add(item_id)
            for dependency in specs[item_id]["dependencies"]:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in specs:
            visit(item_id)
        existing = {row["id"]: row for row in connection.execute("SELECT * FROM work_items WHERE run_id=?", (run["id"],))}
        if set(existing) - set(specs):
            raise Conflict("Plan replacement must retain historical work item identities")
        for item_id, spec in specs.items():
            old = existing.get(item_id)
            if old and old["specification"] != _json(spec):
                if connection.execute("SELECT 1 FROM attempts WHERE work_item_id=?", (item_id,)).fetchone():
                    raise Conflict("Attempted assignment contracts are immutable; create a new work item")
            if old:
                if old["specification"] != _json(spec):
                    connection.execute("UPDATE work_items SET objective=?,specification=?,revision=revision+1 WHERE id=?",
                                       (spec["objective"], _json(spec), item_id))
            else:
                if connection.execute("SELECT 1 FROM work_items WHERE id=?", (item_id,)).fetchone():
                    raise ScopeDenied("Work item is unavailable in this run")
                connection.execute("INSERT INTO work_items(id,run_id,objective,state,specification) VALUES(?,?,?,'pending',?)",
                                   (item_id, run["id"], spec["objective"], _json(spec)))
        connection.execute("DELETE FROM work_dependencies WHERE run_id=?", (run["id"],))
        for item_id, spec in specs.items():
            connection.executemany("INSERT INTO work_dependencies VALUES(?,?,?)",
                                   [(run["id"], item_id, dependency) for dependency in spec["dependencies"]])
        self._readiness(connection, run["id"])
        return {"work_items": list(specs)}

    @staticmethod
    def _readiness(connection: sqlite3.Connection, run_id: str) -> None:
        connection.execute(
            "UPDATE work_items SET state=CASE WHEN EXISTS (SELECT 1 FROM work_dependencies d "
            "JOIN work_items parent ON parent.id=d.dependency_id WHERE d.work_item_id=work_items.id "
            "AND parent.state!='accepted') THEN 'pending' ELSE 'ready' END "
            "WHERE run_id=? AND state IN ('pending','ready')", (run_id,),
        )

    def _assign(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, work_item_id: str,
        worker_id: str, requests: int, model: dict[str, str],
    ) -> dict[str, Any]:
        self.store._admitting(run)
        require_id(worker_id)
        _count(requests, minimum=1)
        work = connection.execute("SELECT * FROM work_items WHERE run_id=? AND id=?", (run["id"], work_item_id)).fetchone()
        if work is None:
            raise ScopeDenied("Work item is unavailable in this run")
        if work["state"] != "ready":
            raise Conflict("Work item is not ready for assignment")
        from .collaboration import SwarmCollaboration
        SwarmCollaboration.require_unbound_assignment(connection, work_item_id)
        from .managed_collaboration import ManagedCollaborationAdmission
        ManagedCollaborationAdmission.require_unbound_assignment(connection, run["id"], work_item_id)
        policy = PolicyProfile.from_dict(json.loads(run["policy_json"]))
        active = connection.execute("SELECT * FROM attempts WHERE run_id=? AND "
                                    "(state IN ('leased','running','uncertain') OR process_state!='stopped')", (run["id"],)).fetchall()
        if sum(item["kind"] == "worker" for item in active) >= run["worker_limit"]:
            raise Conflict("Configured worker capacity is already allocated")
        if any(item["worker_id"] == worker_id for item in active):
            raise Conflict("Worker already owns an unresolved attempt")
        if set(model) != {"provider", "model"}:
            raise ValueError("Select an explicit provider and model")
        spec = json.loads(work["specification"])
        grant = SwarmPolicy.admit(policy, model=ModelSelection(**model), tools=tuple(spec["tools"]),
                                  read_roots=tuple(spec["read_roots"]), write_roots=tuple(spec["write_roots"]))
        for item in active:
            other = AssignmentGrant.from_dict(json.loads(item["grant_json"]))
            if (scopes_overlap(grant.write_roots, other.read_roots + other.write_roots, case_sensitive=False)
                    or scopes_overlap(other.write_roots, grant.read_roots + grant.write_roots, case_sensitive=False)):
                raise Conflict("Assignment scope conflicts with unresolved work")
        if requests + self.store._allocated(connection, run["id"]) > run["request_limit"]:
            from .models import AllowanceExceeded
            raise AllowanceExceeded("Request allowance is already allocated or exhausted")
        attempt, reservation, dispatch = _id(), _id(), _id()
        connection.execute("INSERT INTO attempts(id,run_id,work_item_id,worker_id,epoch,state,grant_json) VALUES(?,?,?,?,?,'leased',?)",
                           (attempt, run["id"], work_item_id, worker_id, run["epoch"], _json(grant.to_dict())))
        connection.execute("UPDATE work_items SET state='leased' WHERE id=?", (work_item_id,))
        connection.execute("INSERT INTO reservations VALUES(?,?,?,NULL,'reserved')", (reservation, attempt, requests))
        connection.execute("INSERT INTO dispatches VALUES(?,?,'pending')", (dispatch, attempt))
        return {"attempt_id": attempt, "epoch": run["epoch"], "worker_id": worker_id,
                "reservation_id": reservation, "dispatch_id": dispatch, "grant": grant.to_dict()}

    def _start_coordinator(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, worker_id: str,
        requests: int, model: dict[str, str], tools: list[str], read_roots: list[str],
    ) -> dict[str, Any]:
        """Reserve one native planning participant without inventing DAG work.

        The coordinator may read and use addressed collaboration tools. Its
        proposals remain data; only trusted owner/runtime commands alter work.
        """
        self.store._admitting(run)
        require_id(worker_id)
        _count(requests, minimum=1)
        if (not isinstance(tools, list) or not isinstance(read_roots, list)
                or any(not isinstance(value, str) for value in tools + read_roots)
                or len(tools) != len(set(tools)) or len(read_roots) != len(set(read_roots))):
            raise ValueError("Coordinator tools and read scopes must be explicit unique string lists")
        if not set(tools) <= FILE_TOOL_NAMES | (SWARM_TOOL_NAMES - {"swarm_submit"}):
            raise ScopeDenied("Coordinators receive only native read and addressed collaboration tools")
        active = connection.execute("SELECT * FROM attempts WHERE run_id=? AND "
                                    "(state IN ('leased','running','uncertain') OR process_state!='stopped')", (run["id"],)).fetchall()
        if any(attempt["kind"] == "coordinator" for attempt in active):
            raise Conflict("Run already has an unresolved coordinator")
        if any(attempt["worker_id"] == worker_id for attempt in active):
            raise Conflict("Participant identity already owns an unresolved attempt")
        if not isinstance(model, dict) or set(model) != {"provider", "model"}:
            raise ValueError("Select an explicit native coordinator provider and model")
        policy = PolicyProfile.from_dict(json.loads(run["policy_json"]))
        grant = SwarmPolicy.admit(policy, model=ModelSelection(**model), tools=tuple(tools), read_roots=tuple(read_roots), write_roots=())
        for attempt in active:
            other = AssignmentGrant.from_dict(json.loads(attempt["grant_json"]))
            if scopes_overlap(other.write_roots, grant.read_roots, case_sensitive=False):
                raise Conflict("Coordinator read scope conflicts with unresolved writer work")
        if requests + self.store._allocated(connection, run["id"]) > run["request_limit"]:
            from .models import AllowanceExceeded
            raise AllowanceExceeded("Coordinator request allowance is already allocated or exhausted")
        attempt, reservation, dispatch = _id(), _id(), _id()
        connection.execute("INSERT INTO attempts(id,run_id,worker_id,epoch,state,grant_json,kind) VALUES(?,?,?,?,'leased',?,'coordinator')",
                           (attempt, run["id"], worker_id, run["epoch"], _json(grant.to_dict())))
        connection.execute("INSERT INTO reservations VALUES(?,?,?,NULL,'reserved')", (reservation, attempt, requests))
        connection.execute("INSERT INTO dispatches VALUES(?,?,'pending')", (dispatch, attempt))
        return {"attempt_id": attempt, "epoch": run["epoch"], "worker_id": worker_id, "kind": "coordinator",
                "reservation_id": reservation, "dispatch_id": dispatch, "grant": grant.to_dict()}

    def _decide_proposal(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, proposal_id: str,
        sha256: str, accept: bool, evidence: str,
    ) -> dict[str, Any]:
        """Adopt or reject retained planning data through explicit authority."""
        self.store._admitting(run)
        self._decision_evidence(evidence)
        if type(accept) is not bool:
            raise ValueError("Proposal acceptance must be an explicit boolean decision")
        proposal = connection.execute("SELECT * FROM coordinator_proposals WHERE id=? AND run_id=?", (proposal_id, run["id"])).fetchone()
        if proposal is None:
            raise ScopeDenied("Proposal is unavailable in this run")
        if proposal["state"] != "pending":
            raise Conflict("Only a pending proposal may be decided")
        envelope = json.loads(proposal["payload_json"])
        digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
        if digest != proposal["sha256"] or sha256 != digest:
            raise Conflict("Proposal decision must bind its exact immutable envelope")
        # A replacement owner may decide retained evidence after explicit
        # recovery. This reads the original participant; it grants no new
        # execution or proposal-recording authority to that historical epoch.
        attempt = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=? AND epoch=?",
                                     (run["id"], proposal["attempt_id"], proposal["epoch"])).fetchone()
        if attempt is None:
            raise ScopeDenied("Proposal participant is unavailable in this run and epoch")
        if attempt["kind"] != "coordinator" or attempt["state"] != "completed":
            raise Conflict("Proposal review requires an observed completed coordinator")
        self._resolved_attempt(connection, attempt["id"])
        request = connection.execute("SELECT r.state,r.purpose,i.purpose AS input_purpose,i.observation_outcome,i.rowid AS input_row,i.input_sha256 "
                                     "FROM model_requests r JOIN request_inputs i ON i.request_id=r.id "
                                     "WHERE r.id=? AND r.attempt_id=? AND r.epoch=?",
                                     (proposal["request_id"], attempt["id"], attempt["epoch"])).fetchone()
        if (request is None or request["state"] != "completed" or request["purpose"] != "main"
                or request["input_purpose"] != "primary" or request["observation_outcome"] != "completed"):
            raise Conflict("Proposal must bind an observed completed coordinator request")
        input_revision = connection.execute("SELECT COUNT(*) FROM request_inputs i JOIN model_requests r ON r.id=i.request_id "
                                            "WHERE r.attempt_id=? AND i.purpose='primary' AND i.rowid<=?",
                                            (attempt["id"], request["input_row"])).fetchone()[0]
        if proposal["input_revision"] != input_revision:
            raise Conflict("Proposal does not bind its exact primary input assignment")
        if (not isinstance(envelope, dict) or set(envelope) != {"plan", "source_sha256", "allowed_criteria", "policy_digest", "graph_sha256", "input_sha256"}
                or envelope["policy_digest"] != PolicyProfile.from_dict(json.loads(run["policy_json"])).digest):
            raise Conflict("Proposal policy contract does not match the current run")
        captured = connection.execute("SELECT * FROM coordinator_inputs WHERE request_id=? AND attempt_id=?",
                                      (proposal["request_id"], attempt["id"])).fetchone()
        if (captured is None or captured["input_sha256"] != request["input_sha256"]
                or captured["input_sha256"] != envelope["input_sha256"] or captured["graph_sha256"] != envelope["graph_sha256"]
                or captured["policy_digest"] != envelope["policy_digest"] or captured["criteria_json"] != canonical_json(envelope["allowed_criteria"])):
            raise Conflict("Proposal is not bound to its actually admitted coordinator input")
        plan = envelope["plan"]
        criteria = envelope["allowed_criteria"]
        if (not isinstance(plan, dict) or set(plan) != {"summary", "use_team", "work_items"}
                or not isinstance(plan["summary"], str) or not plan["summary"].strip() or type(plan["use_team"]) is not bool
                or not isinstance(plan["work_items"], list) or not isinstance(criteria, list)
                or any(not isinstance(value, str) for value in criteria) or len(criteria) != len(set(criteria))):
            raise ValueError("Malformed retained proposal contract")
        if any(not isinstance(item, dict) or not isinstance(item.get("criteria"), list)
               or any(criterion not in criteria for criterion in item["criteria"]) for item in plan["work_items"]):
            raise Conflict("Proposal criteria exceed the captured trusted criterion set")
        if accept:
            graph = [dict(row) for row in connection.execute(
                "SELECT id,specification,revision,state FROM work_items WHERE run_id=? ORDER BY id", (run["id"],),
            )]
            if hashlib.sha256(canonical_json(graph).encode("utf-8")).hexdigest() != envelope["graph_sha256"]:
                raise Conflict("Work graph changed after the proposal input; request a fresh plan")
            # Proposals supplement the retained graph. Deletion is never
            # inferred from a model omitting a historical work identity.
            merged = {item["id"]: json.loads(item["specification"]) for item in graph}
            proposed_ids = [item.get("id") for item in plan["work_items"]]
            if len(proposed_ids) != len(set(proposed_ids)):
                raise Conflict("Proposal work identities must be unique")
            if set(proposed_ids) & set(merged):
                raise Conflict("Additional proposals cannot replace historical work identities")
            merged.update({item["id"]: item for item in plan["work_items"]})
            if merged:  # An orchestrator that answered directly adds no work at all.
                self._plan(connection, run, work_items=list(merged.values()))
        state = "accepted" if accept else "rejected"
        connection.execute("UPDATE coordinator_proposals SET state=?,decision_evidence=? WHERE id=?", (state, evidence, proposal_id))
        return {"proposal_id": proposal_id, "sha256": digest, "state": state}

    @staticmethod
    def _attempt(connection: sqlite3.Connection, run: sqlite3.Row, attempt_id: str, attempt_epoch: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=? AND epoch=?",
                                 (run["id"], attempt_id, attempt_epoch)).fetchone()
        if row is None:
            raise ScopeDenied("Attempt is unavailable in this run and epoch")
        if attempt_epoch != run["epoch"] and run["state"] != "recovery_required":
            raise StaleAuthority("Historical attempts can only be reconciled during explicit recovery")
        return row

    def _worker_started(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int) -> dict[str, Any]:
        self.store._admitting(run)
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        self.store._attempt_admitting(attempt)
        if attempt["state"] != "leased" or attempt["process_state"] != "pending":
            raise Conflict("Dispatch is not pending")
        connection.execute("UPDATE attempts SET state='running',process_state='running' WHERE id=?", (attempt_id,))
        if attempt["kind"] == "worker":
            connection.execute("UPDATE work_items SET state='running' WHERE id=?", (attempt["work_item_id"],))
        connection.execute("UPDATE dispatches SET state='started' WHERE attempt_id=?", (attempt_id,))
        return {"attempt_id": attempt_id}

    def _reserve_request(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, request_id: str, purpose: str,
    ) -> dict[str, Any]:
        self.store._admitting(run)
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        self.store._attempt_admitting(attempt)
        require_id(request_id)
        if purpose not in ("main", "auxiliary"):
            raise ValueError("Request purpose must be main or auxiliary")
        if attempt["state"] != "running" or attempt["process_state"] != "running":
            raise Conflict("Requests require an observed active worker")
        if connection.execute("SELECT 1 FROM model_requests WHERE id=?", (request_id,)).fetchone():
            raise IdempotencyConflict("Request identity is already reserved")
        reservation = connection.execute("SELECT amount,state FROM reservations WHERE attempt_id=?", (attempt_id,)).fetchone()
        if reservation["state"] != "reserved":
            raise Conflict("Unknown accounting closes this attempt's request admission")
        allocation = reservation["amount"]
        held = connection.execute("SELECT COALESCE(SUM(CASE WHEN used IS NULL THEN 1 ELSE used END),0) "
                                  "FROM model_requests WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
        if held >= allocation:
            from .models import AllowanceExceeded
            raise AllowanceExceeded("Attempt request allowance is exhausted or uncertain")
        connection.execute("INSERT INTO model_requests VALUES(?,?,?,?,'reserved',NULL)", (request_id, attempt_id, attempt_epoch, purpose))
        return {"request_id": request_id}

    def _request(self, connection: sqlite3.Connection, run: sqlite3.Row, request_id: str, attempt_epoch: int) -> sqlite3.Row:
        row = connection.execute("SELECT r.* FROM model_requests r JOIN attempts a ON a.id=r.attempt_id "
                                 "WHERE r.id=? AND a.run_id=? AND r.epoch=?", (request_id, run["id"], attempt_epoch)).fetchone()
        if row is None:
            raise ScopeDenied("Request is unavailable in this run and epoch")
        self._attempt(connection, run, row["attempt_id"], attempt_epoch)
        return row

    def _start_request(self, connection: sqlite3.Connection, run: sqlite3.Row, *, request_id: str, attempt_epoch: int) -> dict[str, Any]:
        self.store._admitting(run)
        request = self._request(connection, run, request_id, attempt_epoch)
        if request["state"] != "reserved":
            raise Conflict("Only a reserved request may start")
        attempt = self._attempt(connection, run, request["attempt_id"], attempt_epoch)
        self.store._attempt_admitting(attempt)
        if attempt["process_state"] != "running" or attempt["state"] != "running":
            raise Conflict("Request worker is not active")
        reservation = connection.execute("SELECT state FROM reservations WHERE attempt_id=?", (request["attempt_id"],)).fetchone()
        if reservation["state"] != "reserved":
            raise Conflict("Unknown accounting closes this attempt's request admission")
        connection.execute("UPDATE model_requests SET state='started' WHERE id=?", (request_id,))
        return {"request_id": request_id}

    def _settle_request(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, request_id: str,
        attempt_epoch: int, outcome: str, used: int | None,
    ) -> dict[str, Any]:
        request = self._request(connection, run, request_id, attempt_epoch)
        if request["state"] == "uncertain":
            raise Conflict("Unknown execution requires explicit request reconciliation")
        return self._settle(connection, run, request, outcome, used)

    def _reconcile_request(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, request_id: str,
        attempt_epoch: int, outcome: str, used: int | None, evidence: str,
    ) -> dict[str, Any]:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Reconciliation requires an attributable observation")
        request = self._request(connection, run, request_id, attempt_epoch)
        if request["state"] != "uncertain":
            raise Conflict("Only uncertain requests require reconciliation")
        result = self._settle(connection, run, request, outcome, used)
        result["evidence"] = evidence
        return result

    def _settle(self, connection: sqlite3.Connection, run: sqlite3.Row, request: sqlite3.Row, outcome: str, used: int | None) -> dict[str, Any]:
        if outcome not in (*_REQUEST_TERMINAL, "uncertain"):
            raise ValueError("Unsupported request observation")
        if request["state"] in _REQUEST_TERMINAL:
            raise Conflict("Request accounting is already settled")
        if outcome == "uncertain":
            if used is not None:
                raise ValueError("Unknown execution cannot claim known usage")
        elif type(used) is not int or used not in (0, 1):
            raise ValueError("Known request usage must be zero or one")
        # Recovery rewrites mutable request state to uncertain. Its immutable
        # start receipt remains proof that invocation crossed the admission
        # boundary; generic reconciliation prose cannot erase that possibility.
        # Keep these receipts when retaining/archiving request history.
        started = request["state"] == "started" or connection.execute(
            "SELECT 1 FROM events WHERE run_id=? AND kind='command_start_request' "
            "AND json_extract(payload,'$.result.request_id')=? LIMIT 1", (run["id"], request["id"]),
        ).fetchone() is not None
        if outcome == "not_started" and (used != 0 or started):
            raise Conflict("A started request cannot be refunded as not started")
        if outcome == "failed" and used != 1:
            raise Conflict("A failed invocation consumes one request unit; only a never-started reservation uses zero")
        if outcome == "completed" and (used != 1 or not started):
            raise Conflict("Completion requires an observed start and one request")
        if outcome == "failed" and request["state"] == "reserved":
            raise Conflict("An unstarted request is cancelled as not_started")
        connection.execute("UPDATE model_requests SET state=?,used=? WHERE id=?", (outcome, used, request["id"]))
        self._refresh_reservation(connection, request["attempt_id"])
        self._control_checkpoint(connection, run["id"])
        return {"request_id": request["id"], "outcome": outcome, "used": used}

    def _worker_stopped(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, outcome: str, evidence: str,
    ) -> dict[str, Any]:
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        allowed = ("completed", "failed", "cancelled") if attempt["kind"] == "coordinator" else ("submitted", "failed", "cancelled")
        if outcome not in allowed or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Termination requires an outcome and trusted observation")
        if attempt["process_state"] == "stopped":
            raise Conflict("Termination is already recorded")
        if outcome == "submitted" and not connection.execute("SELECT 1 FROM submissions WHERE attempt_id=?", (attempt_id,)).fetchone():
            raise Conflict("Submitted outcome requires an immutable handoff")
        # Normal reserved-but-not-started requests are known unused only once
        # termination is observed. Recovery-marked uncertainty is never cleared.
        connection.execute("UPDATE model_requests SET state='not_started',used=0 WHERE attempt_id=? AND state='reserved'", (attempt_id,))
        connection.execute("UPDATE model_requests SET state='uncertain' WHERE attempt_id=? AND state='started'", (attempt_id,))
        connection.execute("UPDATE action_receipts SET state='uncertain' WHERE attempt_id=? AND state='admitted'", (attempt_id,))
        unknown = connection.execute("SELECT 1 FROM model_requests WHERE attempt_id=? AND state='uncertain'", (attempt_id,)).fetchone()
        unknown = unknown or connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND state='uncertain'", (attempt_id,)).fetchone()
        unknown = unknown or connection.execute("SELECT 1 FROM writer_worktrees WHERE attempt_id=? AND state IN ('creating','finalizing','uncertain')", (attempt_id,)).fetchone()
        unknown = unknown or connection.execute(
            "SELECT 1 FROM integration_processes p JOIN writer_worktrees w ON w.id=p.effect_id "
            "WHERE p.effect_kind='writer' AND w.attempt_id=? AND p.state NOT IN ('stopped','not_started')", (attempt_id,),
        ).fetchone()
        unknown = unknown or connection.execute("SELECT 1 FROM process_observations WHERE attempt_id=? AND state!='stopped'", (attempt_id,)).fetchone()
        actual = "uncertain" if unknown else outcome
        connection.execute("UPDATE attempts SET state=?,process_state='stopped' WHERE id=?", (actual, attempt_id))
        if attempt["kind"] == "worker":
            connection.execute("UPDATE work_items SET state=? WHERE id=?", (actual, attempt["work_item_id"]))
        connection.execute("UPDATE dispatches SET state='finished' WHERE attempt_id=?", (attempt_id,))
        self._refresh_reservation(connection, attempt_id)
        self._control_checkpoint(connection, run["id"])
        return {"attempt_id": attempt_id, "outcome": actual, "termination_evidence": evidence}

    @staticmethod
    def _refresh_reservation(connection: sqlite3.Connection, attempt_id: str) -> None:
        process = connection.execute("SELECT process_state,state,work_item_id,kind FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        unknown = connection.execute("SELECT 1 FROM model_requests WHERE attempt_id=? AND state IN ('reserved','started','uncertain')", (attempt_id,)).fetchone()
        unresolved_action = connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND state!='completed'", (attempt_id,)).fetchone()
        unresolved_writer = connection.execute("SELECT 1 FROM writer_worktrees WHERE attempt_id=? AND state IN ('creating','finalizing','uncertain')", (attempt_id,)).fetchone()
        unresolved_writer = unresolved_writer or connection.execute(
            "SELECT 1 FROM integration_processes p JOIN writer_worktrees w ON w.id=p.effect_id "
            "WHERE p.effect_kind='writer' AND w.attempt_id=? AND p.state NOT IN ('stopped','not_started')", (attempt_id,),
        ).fetchone()
        unresolved_process = connection.execute("SELECT 1 FROM process_observations WHERE attempt_id=? AND state!='stopped'", (attempt_id,)).fetchone()
        if unresolved_process and process["process_state"] in ("stopped", "unknown"):
            connection.execute("UPDATE reservations SET state='uncertain',used=NULL WHERE attempt_id=?", (attempt_id,))
            return
        if process["process_state"] == "stopped" and not unknown:
            used = connection.execute("SELECT COALESCE(SUM(used),0) FROM model_requests WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
            connection.execute("UPDATE reservations SET state='settled',used=? WHERE attempt_id=?", (used, attempt_id))
            if process["state"] == "uncertain" and not unresolved_action and not unresolved_writer and not unresolved_process:
                # Recoverable failure is honest; submission is never inferred.
                connection.execute("UPDATE attempts SET state='failed' WHERE id=?", (attempt_id,))
                if process["kind"] == "worker":
                    connection.execute("UPDATE work_items SET state='failed' WHERE id=?", (process["work_item_id"],))
        elif unknown:
            uncertain = connection.execute("SELECT 1 FROM model_requests WHERE attempt_id=? AND state='uncertain'", (attempt_id,)).fetchone()
            if uncertain:
                connection.execute("UPDATE reservations SET state='uncertain' WHERE attempt_id=?", (attempt_id,))

    def _submit(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, candidate_revision: str, handoff: str,
    ) -> dict[str, Any]:
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["kind"] != "worker":
            raise Conflict("Coordinator proposals cannot become work submissions")
        if attempt_epoch != run["epoch"] or attempt["state"] not in ("leased", "running"):
            raise Conflict("Only a current active attempt may submit")
        if not isinstance(handoff, str) or not handoff.strip() or len(handoff.encode()) > 65536:
            raise ValueError("Handoff must contain 1 to 65536 bytes")
        require_id(candidate_revision)
        if connection.execute("SELECT 1 FROM submissions WHERE attempt_id=?", (attempt_id,)).fetchone():
            raise Conflict("A submitted handoff is immutable")
        revision = connection.execute("SELECT revision FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()[0]
        connection.execute("INSERT INTO submissions VALUES(?,?,?,?)", (attempt_id, candidate_revision, handoff, revision))
        return {"attempt_id": attempt_id, "candidate_revision": candidate_revision}

    def _record_check(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, check_id: str, criterion_id: str, candidate_revision: str,
        executor_id: str, check_name: str, exit_code: int, evidence: str,
    ) -> dict[str, Any]:
        if self._attempt(connection, run, attempt_id, attempt_epoch)["kind"] != "worker":
            raise Conflict("Coordinator proposals cannot acquire work acceptance checks")
        for identifier in (check_id, criterion_id, candidate_revision, executor_id, check_name):
            require_id(identifier)
        if type(exit_code) is not int or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Check observation requires an exit status and retained evidence")
        if connection.execute("SELECT 1 FROM check_receipts WHERE id=?", (check_id,)).fetchone():
            raise IdempotencyConflict("Check identity already exists")
        connection.execute("INSERT INTO check_receipts VALUES(?,?,?,?,?,?,?,?)",
                           (check_id, attempt_id, criterion_id, candidate_revision, executor_id, check_name, exit_code, evidence))
        return {"check_id": check_id}

    def _accept(self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str, attempt_epoch: int) -> dict[str, Any]:
        self.store._admitting(run)
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        return self._accept_read_submission(connection, run, attempt)

    def _accept_read_submission(self, connection: sqlite3.Connection, run: sqlite3.Row, attempt: sqlite3.Row) -> dict[str, Any]:
        """Validate read evidence; callers separately bind their authority."""
        attempt_id = attempt["id"]
        if attempt["kind"] != "worker":
            raise Conflict("Coordinators do not represent accepted work")
        if attempt["state"] != "submitted" or attempt["process_state"] != "stopped":
            raise Conflict("Acceptance requires a submitted result and confirmed termination")
        self._resolved_attempt(connection, attempt_id)
        work = connection.execute("SELECT * FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()
        submission = connection.execute("SELECT * FROM submissions WHERE attempt_id=?", (attempt_id,)).fetchone()
        if work["state"] != "submitted" or submission is None or submission["work_revision"] != work["revision"]:
            raise Conflict("Submission does not match current requirements")
        spec = json.loads(work["specification"])
        if spec["write_roots"]:
            raise Conflict("Writer acceptance requires the separate verified integration protocol")
        if not spec["criteria"]:
            raise Conflict("Acceptance requires explicit criteria and trusted check evidence")
        for criterion in spec["criteria"]:
            receipt = connection.execute("SELECT exit_code FROM check_receipts WHERE attempt_id=? AND criterion_id=? "
                                         "AND candidate_revision=? ORDER BY rowid DESC LIMIT 1",
                                         (attempt_id, criterion, submission["candidate_revision"])).fetchone()
            if receipt is None or receipt[0] != 0:
                raise Conflict("Required criterion lacks current passing evidence")
        connection.execute("UPDATE work_items SET state='accepted' WHERE id=?", (work["id"],))
        self._readiness(connection, run["id"])
        return {"work_item_id": work["id"], "attempt_id": attempt_id}

    @staticmethod
    def _decision_evidence(evidence: str) -> None:
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence.encode()) > 65536:
            raise ValueError("Review evidence must contain 1 to 65536 bytes")

    @staticmethod
    def _resolved_attempt(connection: sqlite3.Connection, attempt_id: str) -> None:
        unresolved = connection.execute(
            "SELECT 1 FROM attempts a JOIN reservations r ON r.attempt_id=a.id WHERE a.id=? "
            "AND (a.process_state!='stopped' OR r.state!='settled')", (attempt_id,),
        ).fetchone()
        unresolved = unresolved or connection.execute(
            "SELECT 1 FROM model_requests WHERE attempt_id=? AND state IN ('reserved','started','uncertain')", (attempt_id,),
        ).fetchone()
        unresolved = unresolved or connection.execute(
            "SELECT 1 FROM action_receipts WHERE attempt_id=? AND state!='completed'", (attempt_id,),
        ).fetchone()
        unresolved = unresolved or connection.execute(
            "SELECT 1 FROM process_observations WHERE attempt_id=? AND state!='stopped'", (attempt_id,),
        ).fetchone()
        unresolved = unresolved or connection.execute(
            "SELECT 1 FROM integration_processes p JOIN writer_worktrees w ON w.id=p.effect_id "
            "WHERE p.effect_kind='writer' AND w.attempt_id=? AND p.state NOT IN ('stopped','not_started')", (attempt_id,),
        ).fetchone()
        if unresolved:
            raise Conflict("Acceptance requires resolved process, request and action observations")

    @staticmethod
    def _autonomy_grant(connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        """The owner's start-time autonomy grant, from the team's captured setup."""
        record = connection.execute("SELECT payload FROM commands WHERE run_id=? AND actor='desktop-setup' "
                                    "ORDER BY rowid LIMIT 1", (run["id"],)).fetchone()
        grant = json.loads(record["payload"]).get("autonomy") if record is not None else None
        if not isinstance(grant, dict) or type(grant.get("rounds")) is not int:
            raise ScopeDenied("The owner did not let this team's orchestrator run it")
        return grant

    def _accept_under_grant(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, candidate_revision: str, evidence: str,
    ) -> dict[str, Any]:
        """Accept a read result under the owner's autonomy grant, so later rounds can use it.

        The owner chose, when starting the team, to let its orchestrator run
        without reviewing each result. The receipt records exactly that, with
        executor ``autonomy:<owner>``: never an owner review or a named check.
        """
        self.store._admitting(run)
        self._decision_evidence(evidence)
        self._autonomy_grant(connection, run)
        attempt = self._attempt(connection, run, attempt_id, attempt_epoch)
        if attempt["kind"] != "worker":
            raise Conflict("Coordinators do not represent accepted work")
        work = connection.execute("SELECT * FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()
        specification = json.loads(work["specification"])
        if work["state"] != "submitted" or specification["write_roots"] or specification["criteria"] != ["owner_review"]:
            raise Conflict("Only a submitted read result awaiting review can be accepted under the grant")
        submission = connection.execute("SELECT * FROM submissions WHERE attempt_id=?", (attempt_id,)).fetchone()
        if submission is None or submission["candidate_revision"] != candidate_revision:
            raise Conflict("Acceptance must identify the exact submitted result")
        self._resolved_attempt(connection, attempt_id)
        check_id = _id()
        connection.execute("INSERT INTO check_receipts VALUES(?,?,?,?,?,?,?,?)",
                           (check_id, attempt_id, "owner_review", candidate_revision, f"autonomy:{run['owner_id']}",
                            "Accepted under the owner's autonomy grant, not reviewed", 0, evidence))
        result = self._accept_read_submission(connection, run, attempt)
        return {**result, "check_id": check_id, "review_source": "autonomy"}

    def _review_read_result(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, candidate_revision: str, evidence: str,
    ) -> dict[str, Any]:
        """Atomically record an explicit human review and accept its read result.

        The owner adapter authenticates the human decision. Model tool arguments
        cannot invoke this command or provide executor identities/check statuses.
        """
        self.store._admitting(run)
        self._decision_evidence(evidence)
        # A current owner may freshly review a retained result after takeover.
        # This path reads an immutable historical submission; it never grants
        # the old worker permission to submit, check, or execute again.
        attempt = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=? AND epoch=?",
                                     (run["id"], attempt_id, attempt_epoch)).fetchone()
        if attempt is None:
            raise ScopeDenied("Owner review result is unavailable in this run and original epoch")
        if attempt["kind"] != "worker":
            raise Conflict("Coordinators do not represent owner-reviewable work")
        work = connection.execute("SELECT * FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()
        specification = json.loads(work["specification"])
        if work["state"] != "submitted" or specification["write_roots"] or specification["criteria"] != ["owner_review"]:
            raise Conflict("Owner review requires a submitted read result with only owner_review declared")
        submission = connection.execute("SELECT * FROM submissions WHERE attempt_id=?", (attempt_id,)).fetchone()
        if submission is None or submission["candidate_revision"] != candidate_revision:
            raise Conflict("Owner review must identify the exact submitted result")
        self._resolved_attempt(connection, attempt_id)
        check_id = _id()
        # Do not broaden _record_check's epoch fence. Only this explicit human
        # decision can add owner evidence to an earlier epoch's read result.
        connection.execute("INSERT INTO check_receipts VALUES(?,?,?,?,?,?,?,?)",
                           (check_id, attempt_id, "owner_review", candidate_revision, f"owner:{run['owner_id']}",
                            "Explicit owner review", 0, evidence))
        result = self._accept_read_submission(connection, run, attempt)
        return {**result, "check_id": check_id, "review_source": "owner"}

    def _writer_acceptance_binding(
        self, connection: sqlite3.Connection, run: sqlite3.Row,
        attempt: sqlite3.Row, candidate_id: str,
    ) -> dict[str, Any]:
        """Revalidate persisted effects; this performs no Git or process work.

        An observed application is historical evidence. Later owner edits do
        not erase it or authorize any additional checkout mutation.
        """
        work = connection.execute("SELECT * FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()
        spec = json.loads(work["specification"])
        if not spec["write_roots"] or not spec["criteria"] or attempt["state"] != "submitted" or work["state"] not in ("submitted", "accepted"):
            raise Conflict("Writer acceptance requires submitted work with explicit criteria")
        self._resolved_attempt(connection, attempt["id"])
        writer = connection.execute("SELECT * FROM writer_worktrees WHERE attempt_id=?", (attempt["id"],)).fetchone()
        submission = connection.execute("SELECT * FROM submissions WHERE attempt_id=?", (attempt["id"],)).fetchone()
        if (writer is None or writer["state"] != "ready" or writer["epoch"] != attempt["epoch"]
                or not writer["result_revision"] or submission is None
                or submission["candidate_revision"] != writer["result_revision"] or submission["work_revision"] != work["revision"]):
            raise Conflict("Submission must bind the exact finalized writer and current work revision")
        candidate = connection.execute("SELECT * FROM integration_candidates WHERE run_id=? AND id=?", (run["id"], candidate_id)).fetchone()
        if candidate is None or candidate["repo_key"] != writer["repo_key"]:
            raise ScopeDenied("Integration candidate is unavailable for this writer")
        if candidate["epoch"] != attempt["epoch"] or candidate["state"] != "applied":
            raise Conflict("Writer acceptance requires its exact applied integration candidate")
        manifest = json.loads(candidate["manifest_json"])
        member = {"id": writer["id"], "attempt_id": attempt["id"], "result_revision": writer["result_revision"]}
        if member not in manifest["writers"] or candidate["base_revision"] != writer["base_revision"]:
            raise Conflict("Applied candidate does not contain this exact writer result")
        mapping = manifest.get("criterion_checks", {}).get(work["id"], {})
        if set(mapping) != set(spec["criteria"]):
            raise Conflict("Acceptance requires the criteria/check contract declared before candidate verification")
        # Every contributor to a combined result remains part of its evidence.
        # Unrelated readers may continue while this result is reviewed.
        for source in manifest["writers"]:
            source_attempt = connection.execute("SELECT a.*,s.candidate_revision,s.work_revision,w.revision,w.state AS work_state "
                                                "FROM attempts a JOIN submissions s ON s.attempt_id=a.id "
                                                "JOIN work_items w ON w.id=a.work_item_id WHERE a.run_id=? AND a.id=?",
                                                (run["id"], source["attempt_id"])).fetchone()
            source_writer = connection.execute("SELECT * FROM writer_worktrees WHERE id=? AND attempt_id=?",
                                               (source["id"], source["attempt_id"])).fetchone()
            if (source_attempt is None or source_writer is None or source_attempt["state"] != "submitted"
                    or source_attempt["work_state"] not in ("submitted", "accepted") or source_writer["state"] != "ready"
                    or source_writer["epoch"] != candidate["epoch"] or source_writer["repo_key"] != candidate["repo_key"]
                    or source_writer["base_revision"] != candidate["base_revision"]
                    or source_writer["result_revision"] != source["result_revision"]
                    or source_attempt["candidate_revision"] != source["result_revision"]
                    or source_attempt["work_revision"] != source_attempt["revision"]):
                raise Conflict("Combined candidate contributor no longer binds a submitted finalized result")
            self._resolved_attempt(connection, source["attempt_id"])
            if self._related_workflow_unresolved(connection, run["id"], writer_id=source["id"]):
                raise Conflict("Acceptance requires resolved related integration operations")
            linked = connection.execute("SELECT DISTINCT c.id,c.state FROM integration_candidates c,json_each(c.manifest_json,'$.writers') w "
                                        "WHERE c.run_id=? AND json_extract(w.value,'$.attempt_id')=?", (run["id"], source["attempt_id"])).fetchall()
            for related in linked:
                if self._candidate_process_unresolved(connection, related["id"]) or self._related_workflow_unresolved(connection, run["id"], candidate_id=related["id"]) or related["state"] in ("preparing", "uncertain") or connection.execute(
                    "SELECT 1 FROM integration_checks WHERE candidate_id=? AND state IN ('running','uncertain') "
                    "UNION ALL SELECT 1 FROM integration_applications WHERE candidate_id=? AND state IN ('applying','uncertain')",
                    (related["id"], related["id"]),
                ).fetchone():
                    raise Conflict("Acceptance requires resolved related integration effects")
        check_receipts = {}
        for check in manifest["checks"]:
            receipt = connection.execute("SELECT * FROM integration_checks WHERE candidate_id=? AND check_key=? "
                                         "ORDER BY rowid DESC LIMIT 1", (candidate_id, check["key"])).fetchone()
            if (receipt is None or receipt["candidate_revision"] != candidate["result_revision"]
                    or receipt["state"] != "passed" or receipt["exit_code"] != 0 or not receipt["job_id"]
                    or receipt["argv_json"] != _json(check["argv"])):
                raise Conflict("Every declared check requires passing execution evidence at the exact applied candidate")
            check_receipts[check["key"]] = receipt["id"]
        if not check_receipts or any(key not in check_receipts for key in mapping.values()):
            raise Conflict("Declared work criteria do not bind verified candidate checks")
        application = connection.execute("SELECT * FROM integration_applications WHERE candidate_id=? AND state='applied' "
                                         "AND expected_base=? AND target_revision=? AND observed_revision=? ORDER BY rowid DESC LIMIT 1",
                                         (candidate_id, candidate["base_revision"], candidate["result_revision"], candidate["result_revision"])).fetchone()
        if application is None:
            raise Conflict("Acceptance requires an exact observed application receipt")
        return {"attempt_id": attempt["id"], "writer_id": writer["id"], "candidate_id": candidate_id,
                "application_id": application["id"], "work_revision": work["revision"],
                "writer_revision": writer["result_revision"], "candidate_revision": candidate["result_revision"],
                "checks_json": _json({"criteria": mapping, "receipts": check_receipts})}

    def _accept_writer(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, candidate_id: str, evidence: str,
    ) -> dict[str, Any]:
        """Commit a trusted acceptance decision separately from application."""
        self.store._admitting(run)
        self._decision_evidence(evidence)
        # Fresh owner review may accept exact already-applied historical
        # evidence. It never restores that writer's execution authority.
        attempt = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=? AND epoch=?",
                                     (run["id"], attempt_id, attempt_epoch)).fetchone()
        if attempt is None:
            raise ScopeDenied("Writer result is unavailable in this run and epoch")
        if attempt["kind"] != "worker":
            raise Conflict("Coordinators cannot submit writer integration results")
        if connection.execute("SELECT 1 FROM writer_acceptances WHERE attempt_id=?", (attempt_id,)).fetchone():
            raise Conflict("Writer acceptance is immutable; replay the original decision command")
        binding = self._writer_acceptance_binding(connection, run, attempt, candidate_id)
        connection.execute("INSERT INTO writer_acceptances(attempt_id,writer_id,candidate_id,application_id,work_revision,"
                           "writer_revision,candidate_revision,checks_json,evidence,owner_id,epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (*binding.values(), evidence, run["owner_id"], run["epoch"]))
        connection.execute("UPDATE work_items SET state='accepted' WHERE id=?", (attempt["work_item_id"],))
        self._readiness(connection, run["id"])
        return {"work_item_id": attempt["work_item_id"], "attempt_id": attempt_id,
                "candidate_id": candidate_id, "application_id": binding["application_id"]}

    def _complete(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        self.store._admitting(run)
        states = [row[0] for row in connection.execute("SELECT state FROM work_items WHERE run_id=?", (run["id"],))]
        # A team without work completes only on an accepted orchestrator answer.
        answered = connection.execute("SELECT 1 FROM coordinator_proposals WHERE run_id=? AND state='accepted'",
                                      (run["id"],)).fetchone() is not None
        if (not states and not answered) or any(state != "accepted" for state in states) or self._unresolved(connection, run["id"]):
            raise Conflict("Completion requires all work accepted and all effects/accounting resolved")
        for work in connection.execute("SELECT * FROM work_items WHERE run_id=?", (run["id"],)):
            if not json.loads(work["specification"])["write_roots"]:
                continue
            receipt = connection.execute("SELECT r.* FROM writer_acceptances r JOIN attempts a ON a.id=r.attempt_id "
                                         "WHERE a.work_item_id=?", (work["id"],)).fetchone()
            if receipt is None:
                raise Conflict("Accepted writer lacks its immutable acceptance decision")
            attempt = connection.execute("SELECT * FROM attempts WHERE id=?", (receipt["attempt_id"],)).fetchone()
            binding = self._writer_acceptance_binding(connection, run, attempt, receipt["candidate_id"])
            if any(receipt[key] != value for key, value in binding.items()):
                raise Conflict("Writer acceptance evidence changed after its decision")
        connection.execute("UPDATE runs SET state='completed' WHERE id=?", (run["id"],))
        return {}

    def _repairable(self, connection: sqlite3.Connection, run: sqlite3.Row, work_item_id: str) -> list[str]:
        attempts = connection.execute("SELECT a.id,a.process_state,r.state AS accounting FROM attempts a "
                                      "JOIN reservations r ON r.attempt_id=a.id WHERE a.run_id=? AND a.work_item_id=?",
                                      (run["id"], work_item_id)).fetchall()
        candidates: set[str] = set()
        for attempt in attempts:
            self._resolved_attempt(connection, attempt["id"])
            if attempt["process_state"] != "stopped" or attempt["accounting"] != "settled":
                raise Conflict("Repair requires every prior attempt to be stopped and accounted for")
            if connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND state!='completed'", (attempt["id"],)).fetchone():
                raise Conflict("Repair requires resolved action observations")
            if connection.execute("SELECT 1 FROM writer_worktrees WHERE attempt_id=? AND state IN ('creating','finalizing','uncertain')", (attempt["id"],)).fetchone():
                raise Conflict("Repair requires resolved writer integration effects")
            writer = connection.execute("SELECT id FROM writer_worktrees WHERE attempt_id=?", (attempt["id"],)).fetchone()
            if writer and self._related_workflow_unresolved(connection, run["id"], writer_id=writer["id"]):
                raise Conflict("Repair requires resolved related integration operations")
            linked = connection.execute("SELECT DISTINCT c.id,c.state FROM integration_candidates c, json_each(c.manifest_json,'$.writers') w "
                                        "WHERE c.run_id=? AND json_extract(w.value,'$.attempt_id')=?", (run["id"], attempt["id"])).fetchall()
            for candidate in linked:
                if self._candidate_process_unresolved(connection, candidate["id"]):
                    raise Conflict("Repair requires observed candidate subprocess cleanup")
                if self._related_workflow_unresolved(connection, run["id"], candidate_id=candidate["id"]):
                    raise Conflict("Repair requires resolved related integration operations")
                if candidate["state"] in ("preparing", "uncertain", "applied"):
                    raise Conflict("Repair cannot supersede applied or unresolved integration effects")
                if connection.execute("SELECT 1 FROM integration_checks WHERE candidate_id=? AND state IN ('running','uncertain')", (candidate["id"],)).fetchone():
                    raise Conflict("Repair requires observed integration check termination")
                if connection.execute("SELECT 1 FROM integration_applications WHERE candidate_id=? AND state IN ('applying','uncertain','applied')", (candidate["id"],)).fetchone():
                    raise Conflict("Repair cannot supersede an unresolved or applied candidate")
                candidates.add(candidate["id"])
        return sorted(candidates)

    def _reject(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, attempt_id: str,
        attempt_epoch: int, evidence: str,
    ) -> dict[str, Any]:
        # A new recovery owner may reject an already retained submission after
        # checking its resolved effects. This grants no stale worker authority
        # and permits an explicit fresh attempt instead of stranding history.
        if run["state"] != "recovery_required":
            self.store._admitting(run)
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Rejection requires trusted evidence")
        # Rejection is an explicit current-owner review of retained work,
        # including after Continue. Generic historical worker commands remain
        # fenced by _attempt, and repair still requires resolved old effects.
        attempt = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=? AND epoch=?",
                                     (run["id"], attempt_id, attempt_epoch)).fetchone()
        if attempt is None:
            raise ScopeDenied("Result is unavailable in this run and epoch")
        if attempt["kind"] != "worker":
            raise Conflict("Coordinator decisions do not reject DAG work")
        work = connection.execute("SELECT state FROM work_items WHERE id=?", (attempt["work_item_id"],)).fetchone()
        if attempt["state"] != "submitted" or work["state"] != "submitted":
            raise Conflict("Only submitted work can be rejected for repair")
        candidates = self._repairable(connection, run, attempt["work_item_id"])
        connection.execute("UPDATE attempts SET state='failed' WHERE id=?", (attempt_id,))
        connection.execute("UPDATE work_items SET state='failed' WHERE id=?", (attempt["work_item_id"],))
        connection.executemany("UPDATE integration_candidates SET state='superseded' WHERE id=?", [(identity,) for identity in candidates])
        return {"attempt_id": attempt_id, "evidence": evidence, "superseded_candidates": candidates}

    def _retry(self, connection: sqlite3.Connection, run: sqlite3.Row, *, work_item_id: str, evidence: str) -> dict[str, Any]:
        self.store._admitting(run)
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Retry requires an explicit trusted repair decision")
        work = connection.execute("SELECT state FROM work_items WHERE run_id=? AND id=?", (run["id"], work_item_id)).fetchone()
        if work is None:
            raise ScopeDenied("Retry work is unavailable in this run")
        from .collaboration import SwarmCollaboration
        SwarmCollaboration.require_unbound_assignment(connection, work_item_id)
        from .managed_collaboration import ManagedCollaborationAdmission
        ManagedCollaborationAdmission.require_unbound_assignment(connection, run["id"], work_item_id)
        if work["state"] not in ("failed", "cancelled"):
            raise Conflict("Only conclusively failed or cancelled work can be retried")
        candidates = self._repairable(connection, run, work_item_id)
        connection.execute("UPDATE work_items SET state='pending' WHERE id=?", (work_item_id,))
        connection.executemany("UPDATE integration_candidates SET state='superseded' WHERE id=?", [(identity,) for identity in candidates])
        self._readiness(connection, run["id"])
        return {"work_item_id": work_item_id, "evidence": evidence, "superseded_candidates": candidates}

    @staticmethod
    def _related_workflow_unresolved(connection: sqlite3.Connection, run_id: str, *, writer_id: str = "", candidate_id: str = "") -> bool:
        return connection.execute("SELECT 1 FROM integration_operations o WHERE o.run_id=? "
            "AND o.state IN ('queued','running','uncertain') AND ((?!='' AND EXISTS "
            "(SELECT 1 FROM json_each(o.payload_json,'$.writer_ids') WHERE value=?)) "
            "OR (?!='' AND json_extract(o.payload_json,'$.candidate_id')=?)) LIMIT 1",
            (run_id, writer_id, writer_id, candidate_id, candidate_id)).fetchone() is not None

    @staticmethod
    def _unresolved(connection: sqlite3.Connection, run_id: str) -> bool:
        return bool(connection.execute("SELECT 1 FROM attempts a JOIN reservations r ON r.attempt_id=a.id "
                                       "WHERE a.run_id=? AND (a.process_state!='stopped' OR r.state!='settled') LIMIT 1",
                                       (run_id,)).fetchone() or connection.execute(
            "SELECT 1 FROM action_receipts r JOIN attempts a ON a.id=r.attempt_id "
            "WHERE a.run_id=? AND r.state!='completed' LIMIT 1", (run_id,),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM process_observations WHERE run_id=? AND state!='stopped' LIMIT 1", (run_id,),
        ).fetchone() or SwarmSupervisor._integration_unresolved(connection, run_id))

    @staticmethod
    def _candidate_process_unresolved(connection: sqlite3.Connection, candidate_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM integration_processes p WHERE p.state NOT IN ('stopped','not_started') AND "
            "((p.effect_kind='candidate' AND p.effect_id=?) OR "
            "(p.effect_kind='check' AND p.effect_id IN (SELECT id FROM integration_checks WHERE candidate_id=?)) OR "
            "(p.effect_kind='application' AND p.effect_id IN (SELECT id FROM integration_applications WHERE candidate_id=?))) LIMIT 1",
            (candidate_id, candidate_id, candidate_id),
        ).fetchone() is not None

    @staticmethod
    def _integration_unresolved(connection: sqlite3.Connection, run_id: str) -> bool:
        queries = (
            "SELECT 1 FROM integration_processes WHERE run_id=? AND state NOT IN ('stopped','not_started') LIMIT 1",
            "SELECT 1 FROM integration_operations WHERE run_id=? AND state IN ('queued','running','uncertain') LIMIT 1",
            "SELECT 1 FROM writer_worktrees WHERE run_id=? AND state IN ('creating','finalizing','uncertain') LIMIT 1",
            "SELECT 1 FROM integration_candidates WHERE run_id=? AND state IN ('preparing','uncertain') LIMIT 1",
            "SELECT 1 FROM integration_checks r JOIN integration_candidates c ON c.id=r.candidate_id "
            "WHERE c.run_id=? AND r.state IN ('running','uncertain') LIMIT 1",
            "SELECT 1 FROM integration_applications r JOIN integration_candidates c ON c.id=r.candidate_id "
            "WHERE c.run_id=? AND r.state IN ('applying','uncertain') LIMIT 1",
        )
        return any(connection.execute(query, (run_id,)).fetchone() for query in queries)

    def _reconcile_action(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, action_id: str,
        attempt_epoch: int, outcome: str, evidence: str,
    ) -> dict[str, Any]:
        action = connection.execute("SELECT r.* FROM action_receipts r JOIN attempts a ON a.id=r.attempt_id "
                                    "WHERE r.id=? AND a.run_id=? AND r.epoch=?", (action_id, run["id"], attempt_epoch)).fetchone()
        if action is None:
            raise ScopeDenied("Action is unavailable in this run and epoch")
        attempt = self._attempt(connection, run, action["attempt_id"], attempt_epoch)
        if action["state"] != "uncertain" or attempt["process_state"] != "stopped":
            raise Conflict("Action reconciliation requires unknown outcome and confirmed termination")
        if outcome not in ("completed", "failed", "not_started") or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Action reconciliation requires a known outcome and trusted observation")
        metadata = json.loads(action["metadata_json"]) if action["metadata_json"] else {}
        metadata["reconciliation"] = {"outcome": outcome, "evidence": evidence}
        connection.execute("UPDATE action_receipts SET state='completed',is_error=?,metadata_json=? WHERE id=?",
                           (int(outcome != "completed"), _json(metadata), action_id))
        self._refresh_reservation(connection, action["attempt_id"])
        return {"action_id": action_id, "outcome": outcome, "evidence": evidence}

    def _checkpoint(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        self._control_checkpoint(connection, run["id"])
        return {}

    def _pause(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        self.store._admitting(run)
        connection.execute("UPDATE runs SET state='pausing' WHERE id=?", (run["id"],))
        self._control_checkpoint(connection, run["id"])
        return {}

    def _resume(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        if run["state"] != "paused":
            raise Conflict("Only a live paused run can resume")
        if connection.execute("SELECT 1 FROM reservations r JOIN attempts a ON a.id=r.attempt_id "
                              "WHERE a.run_id=? AND r.state='uncertain'", (run["id"],)).fetchone():
            raise Conflict("Unknown accounting must be reconciled before resume")
        connection.execute("UPDATE runs SET state='running' WHERE id=?", (run["id"],))
        return {}

    def _stop(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        if run["state"] in ("completed", "failed", "cancelled"):
            raise Conflict("Run is already terminal")
        if run["state"] != "recovery_required":
            connection.execute("UPDATE runs SET state='stopping' WHERE id=?", (run["id"],))
        connection.execute("UPDATE runs SET stop_requested=1 WHERE id=?", (run["id"],))
        connection.execute("UPDATE work_items SET state='cancelled' WHERE run_id=? AND state IN ('ready','pending')", (run["id"],))
        self._control_checkpoint(connection, run["id"])
        return {}

    def _recover(self, connection: sqlite3.Connection, run: sqlite3.Row, *, retry_work_items: list[str]) -> dict[str, Any]:
        if run["state"] != "recovery_required":
            raise Conflict("Explicit recovery requires a reconciled replacement epoch")
        if self._unresolved(connection, run["id"]):
            raise Conflict("Recovery is blocked by unresolved process effects or request accounting")
        if not isinstance(retry_work_items, list) or len(retry_work_items) != len(set(retry_work_items)):
            raise ValueError("Retry items must be an explicit unique list")
        if run["stop_requested"]:
            if retry_work_items:
                raise Conflict("A stopped run cannot restart assignments during recovery")
            connection.execute("UPDATE runs SET state='cancelled' WHERE id=?", (run["id"],))
            return {"retry_work_items": []}
        for item in retry_work_items:
            work = connection.execute("SELECT state FROM work_items WHERE run_id=? AND id=?", (run["id"], item)).fetchone()
            if work is None:
                raise ScopeDenied("Retry item is unavailable in this run")
            if work["state"] not in ("failed", "cancelled"):
                raise Conflict("Only conclusively stopped failed/cancelled work may be retried")
            from .managed_collaboration import ManagedCollaborationAdmission
            ManagedCollaborationAdmission.require_unbound_assignment(connection, run["id"], item)
            connection.execute("UPDATE work_items SET state='pending' WHERE id=?", (item,))
        self._readiness(connection, run["id"])
        connection.execute("UPDATE runs SET state='running' WHERE id=?", (run["id"],))
        return {"retry_work_items": retry_work_items}

    def _control_checkpoint(self, connection: sqlite3.Connection, run_id: str) -> None:
        state = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        if state == "pausing":
            active = connection.execute("SELECT 1 FROM model_requests r JOIN attempts a ON a.id=r.attempt_id "
                                        "WHERE a.run_id=? AND r.state IN ('started','uncertain')", (run_id,)).fetchone()
            active = active or connection.execute("SELECT 1 FROM action_receipts r JOIN attempts a ON a.id=r.attempt_id "
                                                   "WHERE a.run_id=? AND r.state IN ('admitted','uncertain')", (run_id,)).fetchone()
            active = active or self._integration_unresolved(connection, run_id)
            if not active:
                connection.execute("UPDATE runs SET state='paused' WHERE id=?", (run_id,))
        elif state == "stopping" and not self._unresolved(connection, run_id):
            connection.execute("UPDATE runs SET state='cancelled' WHERE id=?", (run_id,))
            # Some effect observers checkpoint after writing their own event.
            # Capture the actual terminal transition now, so a later Stop or
            # lease renewal cannot become its first timestamped terminal event.
            self.store._event(connection, run_id, "run_terminal", {"state": "cancelled"})
