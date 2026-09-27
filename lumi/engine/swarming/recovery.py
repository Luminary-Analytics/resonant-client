"""Captured local recovery ownership without replaying uncertain execution.

The authenticated host constructs this facade and retains it independently of
viewer sockets. It accepts neither caller-selected process identities nor worker
termination claims. Closing it stops lease renewal, not outstanding processes.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any
import uuid

import psutil

from ...processes import reap_orphaned_launches
from ..execution_guard import FILE_TOOL_NAMES, SWARM_TOOL_NAMES
from .models import Command, CommandReceipt, Conflict, RevisionConflict, RunAuthority, Scope, ScopeDenied, require_id
from .policy import AssignmentGrant
from .processes import ProcessObservations
from .store import SwarmStore
from .supervisor import SwarmSupervisor

_TERMINAL = frozenset({"completed", "cancelled", "failed"})
_FIELDS = {
    "stop": frozenset(),
    "recover": frozenset({"retry_work_items"}),
    "reconcile_request": frozenset({"request_id", "outcome", "used", "evidence"}),
    "reconcile_action": frozenset({"action_id", "outcome", "evidence"}),
}


def record_run_host(
    store: SwarmStore, authority: RunAuthority, *, process_observations: ProcessObservations | None = None,
) -> dict[str, Any]:
    """Persist this actual app process before launching any epoch participant.

    There is no PID parameter. The trusted host calls this after creating the
    run; a missing historical receipt cannot be backfilled after dispatch.
    This identifies in-process thread ownership, never child-tree ownership.
    """
    if process_observations is not None and process_observations.store.path != store.path:
        raise ScopeDenied("Run host observations must use the captured store")
    observations = process_observations or ProcessObservations(store)
    pid = os.getpid()
    created_at = psutil.Process(pid).create_time()
    record = {"run_id": authority.run_id, "epoch": authority.epoch, "host_id": observations.host_id,
              "pid": pid, "created_at": created_at}
    with store._connection(write=True) as connection:
        run = store._authority(connection, authority)
        if not run["managed"]:
            raise Conflict("Run host identity requires the managed protocol")
        previous = connection.execute("SELECT * FROM run_hosts WHERE run_id=? AND epoch=?", (authority.run_id, authority.epoch)).fetchone()
        if previous is not None:
            if dict(previous) != record:
                raise Conflict("Run epoch already belongs to a different captured app process")
            return dict(previous)
        # A replacement owner may identify this app before explicitly resuming.
        # This records observation only; it grants no participant admission.
        if run["state"] != "recovery_required":
            store._admitting(run)
        if connection.execute("SELECT 1 FROM attempts WHERE run_id=? AND epoch=? AND process_state!='pending' LIMIT 1",
                              (authority.run_id, authority.epoch)).fetchone():
            raise Conflict("Run host identity must be recorded before participant dispatch")
        if connection.execute("SELECT 1 FROM process_observations WHERE run_id=? AND epoch=? LIMIT 1",
                              (authority.run_id, authority.epoch)).fetchone():
            raise Conflict("Run host identity must be recorded before participant process launch")
        connection.execute("INSERT INTO run_hosts(run_id,epoch,host_id,pid,created_at) VALUES(?,?,?,?,?)", tuple(record.values()))
        store._event(connection, authority.run_id, "run_host_recorded", record)
    return record


def _host_process_observation(record: dict[str, Any]) -> str:
    """Prove only that an exact app process ended, never its children."""
    try:
        process = psutil.Process(record["pid"])
        if abs(process.create_time() - record["created_at"]) >= .001:
            return "stopped"  # The captured process ended; this PID was reused.
        return "stopped" if process.status() == psutil.STATUS_ZOMBIE else "unknown"
    except psutil.NoSuchProcess:
        return "stopped"
    except (psutil.AccessDenied, OSError):
        return "unknown"


class SwarmRecovery:
    """Own one explicit takeover and renew its lease without worker activity.

    ``scope`` comes from the owner's authenticated local session. The optional
    observer is a trusted host dependency, never a socket-deserialized object.
    Request/action reconciliation requires an explicit outcome and evidence;
    observing a stopped process never resolves those unknown effects by itself.
    """

    def __init__(
        self, supervisor: SwarmSupervisor, scope: Scope, run_id: str,
        *, process_observations: ProcessObservations | None = None,
    ) -> None:
        require_id(run_id)
        if not isinstance(scope, Scope):
            raise TypeError("Recovery requires a trusted captured scope")
        self.supervisor, self.store = supervisor, supervisor.store
        self.scope, self.run_id = scope, run_id
        if process_observations is not None and process_observations.store.path != self.store.path:
            raise ScopeDenied("Recovery observations must use the captured store")
        with self.store._connection() as connection:
            run = self.store._run(connection, scope, run_id)
            if not run["managed"]:
                raise Conflict("Recovery ownership requires the managed swarm protocol")
        self._observations = process_observations or ProcessObservations(self.store)
        self._supervisor_id = "recovery-" + uuid.uuid4().hex
        self._acquire_id = "acquire-" + uuid.uuid4().hex
        self._takeover: tuple[int, float] | None = None
        self._authority: RunAuthority | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._renewal_error = ""
        self._stop_maintenance = threading.Event()
        self._maintenance: threading.Thread | None = None

    @property
    def authority(self) -> RunAuthority:
        """Return captured authority to trusted runtime code, never adopt input."""
        with self._lock:
            if self._closed:
                raise Conflict("Recovery owner is closed")
            if self._renewal_error:
                raise Conflict("Recovery lease maintenance failed; inspect durable ownership")
            if self._authority is None:
                raise Conflict("Recovery ownership has not been acquired")
            return self._authority

    def acquire(self, *, expected_epoch: int, lease_seconds: float = 30) -> RunAuthority:
        """Explicitly take an expired lease, retaining the same retry identity.

        An ambiguous storage failure is propagated; calling this again uses the
        original takeover key. Neither a live owner nor an expired replacement
        authority is silently refreshed by retrying the takeover.
        """
        if type(expected_epoch) is not int or expected_epoch < 1:
            raise ValueError("Recovery requires the observed positive epoch")
        self.supervisor._lease_duration(lease_seconds)
        # The host being recovered may have died while starting a child, which
        # then waits suspended outside any job: end such leftovers first.
        reap_orphaned_launches(force=True)
        with self._lock:
            if self._closed:
                raise Conflict("Recovery owner is closed")
            semantics = (expected_epoch, lease_seconds)
            if self._takeover is not None and self._takeover != semantics:
                raise Conflict("This recovery owner already captured different takeover semantics")
            self._takeover = semantics
            authority = self.supervisor.acquire(
                self.scope, self.run_id, expected_epoch=expected_epoch,
                supervisor_id=self._supervisor_id, command_id=self._acquire_id,
                lease_seconds=lease_seconds,
            )
            self._authority = authority
            if self._maintenance is None:
                self._maintenance = threading.Thread(target=self._maintain, args=(min(5.0, lease_seconds / 3),),
                                                     name=f"swarm-recovery-{self.run_id[:12]}", daemon=True)
                self._maintenance.start()
            return authority

    def _owned(self) -> RunAuthority:
        authority = self.authority
        with self.store._connection() as connection:
            self.store._authority(connection, authority)
        return authority

    def _internal_command(self, kind: str, payload: dict[str, Any] | None = None) -> CommandReceipt:
        identity = uuid.uuid4().hex
        for _ in range(16):
            authority = self._owned()
            revision = self.store.snapshot(self.scope, self.run_id)["run"]["revision"]
            try:
                return self.supervisor.handle(Command(identity, self.run_id, revision, authority.epoch, kind, payload or {}), authority)
            except RevisionConflict:
                continue  # Only a definite pre-mutation rejection is retried.
        raise RevisionConflict("Recovery could not claim a current command revision")

    def _maintain(self, interval: float) -> None:
        while not self._stop_maintenance.wait(interval):
            try:
                if self.store.snapshot(self.scope, self.run_id)["run"]["state"] in _TERMINAL:
                    return
                self._internal_command("renew")
            except Exception as exc:
                with self._lock:
                    if not self._closed:
                        self._renewal_error = f"{type(exc).__name__}: {exc}"
                return  # Lost or uncertain ownership is never reacquired here.

    def inspect(self) -> dict[str, Any]:
        """Read scoped history and lease status; no OS observation is inferred."""
        snapshot = self.store.snapshot(self.scope, self.run_id)
        for process in snapshot["process_observations"]:
            process.pop("launch_token", None)
        for process in snapshot["integration_processes"]:
            process.pop("launch_token", None)
            process.pop("cwd", None)
        with self._lock:
            authority = self._authority
            run = snapshot["run"]
            owned = (not self._closed and not self._renewal_error and authority is not None
                     and run["epoch"] == authority.epoch and run["supervisor_id"] == authority.supervisor_id
                     and run["lease_until"] > self.store.clock())
            snapshot["recovery"] = {"owns_lease": bool(owned), "closed": self._closed,
                                    "requested_epoch": self._takeover[0] if self._takeover is not None else None,
                                    "acquired_epoch": authority.epoch if authority is not None else None,
                                    "renewal_error": self._renewal_error,
                                    "lease_maintenance_alive": bool(self._maintenance and self._maintenance.is_alive())}
        return snapshot

    def _attempt(self, attempt_id: str) -> dict[str, Any]:
        require_id(attempt_id)
        with self.store._connection() as connection:
            self.store._run(connection, self.scope, self.run_id)
            row = connection.execute("SELECT * FROM attempts WHERE run_id=? AND id=?", (self.run_id, attempt_id)).fetchone()
            if row is None:
                raise ScopeDenied("Attempt is unavailable in this recovery scope")
            return dict(row)

    def inspect_process(self, attempt_id: str) -> dict[str, Any]:
        """Inspect a managed child or a specifically captured reader-thread host."""
        attempt = self._attempt(attempt_id)
        observation = self._observations.inspect(self.scope, self.run_id, attempt_id)
        if "pid" in observation:
            return {**observation, "source": "managed_process"}
        observation = self._inspect_thread_host(attempt)
        if "pid" not in observation:
            observation.setdefault("next_step", (
                "No durable process identity was captured. A trusted original runtime must observe its thread's termination; "
                "a missing thread cannot be declared stopped after restart. Future readers need durable owned-process identities."
            ))
        return observation

    def _inspect_thread_host(self, attempt: dict[str, Any]) -> dict[str, Any]:
        unknown = {"attempt_id": attempt["id"], "observation": "unknown", "source": "run_host"}
        grant = AssignmentGrant.from_dict(json.loads(attempt["grant_json"]))
        if grant.write_roots or not grant.tools <= FILE_TOOL_NAMES | SWARM_TOOL_NAMES:
            return {**unknown, "reason": "App termination cannot prove a writer or external-effect process ended",
                    "next_step": "Reconcile this participant's own durable managed-process identity."}
        with self.store._connection() as connection:
            self.store._run(connection, self.scope, self.run_id)
            if connection.execute("SELECT 1 FROM writer_worktrees WHERE attempt_id=?", (attempt["id"],)).fetchone():
                return {**unknown, "reason": "Writer worktree requires its own managed-process observation"}
            # A stopped Python host cannot prove an orphaned search subprocess
            # ended. Completed tool receipts retain the ordinary cleanup proof.
            if connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND tool_name='grep' AND state!='completed'",
                                  (attempt["id"],)).fetchone():
                return {**unknown, "reason": "A search subprocess lacks a completed action observation",
                        "next_step": "Observe the search child independently; app exit alone does not prove child cleanup."}
            row = connection.execute("SELECT * FROM run_hosts WHERE run_id=? AND epoch=?", (self.run_id, attempt["epoch"])).fetchone()
        if row is None:
            return {**unknown, "reason": "No durable original app process identity"}
        record = dict(row)
        if record["host_id"] != self._observations.host_id:
            return {**unknown, "reason": "Original app process belongs to another host"}
        observed = _host_process_observation(record)
        return {**record, **unknown, "observation": observed,
                "reason": "Original app process is gone" if observed == "stopped" else "Original app process is live or cannot be inspected"}

    def reconcile_process(self, attempt_id: str) -> dict[str, Any]:
        """Record host observation, then a failure/cancellation only after join.

        This grants no process-kill authority and launches nothing. Call Stop to
        close admission; an actually live owned process still requires its host
        runtime to terminate it before this observation can settle termination.
        """
        authority = self._owned()
        self._attempt(attempt_id)
        observation = self.inspect_process(attempt_id)
        if "pid" not in observation:
            return {**observation, "termination_recorded": False}
        if observation["source"] == "managed_process":
            observation = {**self._observations.reconcile(authority, attempt_id), "source": "managed_process"}
        else:
            # Re-observe outside the transaction, then fence and compare the
            # immutable receipt before retaining this app-process observation.
            observation = self._inspect_thread_host(self._attempt(attempt_id))
            with self.store._connection(write=True) as connection:
                self.store._authority(connection, authority)
                recorded = connection.execute("SELECT * FROM run_hosts WHERE run_id=? AND epoch=?", (self.run_id, observation.get("epoch"))).fetchone()
                if (recorded is None or any(observation.get(key) != recorded[key] for key in ("host_id", "pid", "created_at"))
                        or connection.execute("SELECT 1 FROM process_observations WHERE attempt_id=?", (attempt_id,)).fetchone()):
                    raise Conflict("Captured thread host identity changed during observation")
                self.store._event(connection, self.run_id, "thread_host_reconciled", {
                    "attempt_id": attempt_id, "epoch": recorded["epoch"], "host_id": recorded["host_id"],
                    "pid": recorded["pid"], "created_at": recorded["created_at"], "observation": observation["observation"],
                })
        if observation["observation"] != "stopped":
            return {**observation, "termination_recorded": False}
        attempt = self._attempt(attempt_id)
        if attempt["process_state"] != "stopped":
            run = self.store.snapshot(self.scope, self.run_id)["run"]
            outcome = "cancelled" if run["stop_requested"] else "failed"
            receipt = self._internal_command("worker_stopped", {
                "attempt_id": attempt_id, "attempt_epoch": attempt["epoch"], "outcome": outcome,
                "evidence": (f"Recovery observed {observation['source']} PID {observation['pid']} created at {observation['created_at']} ended; "
                             "request, action and integration outcomes remain independent"),
            })
            return {**observation, "termination_recorded": True, "outcome": receipt.result["outcome"]}
        return {**observation, "termination_recorded": True, "outcome": attempt["state"]}

    def command(self, kind: str, payload: dict[str, Any], *, command_id: str, expected_revision: int) -> CommandReceipt:
        """Apply an explicit, revision-bound recovery decision to captured scope.

        Supported commands are stop, reconcile_request, reconcile_action and
        recover. The host decides whether supplied evidence is sufficient; this
        facade never turns model prose or a stopped PID into accounting proof.
        """
        require_id(command_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Recovery decisions require the observed run revision")
        if kind not in _FIELDS or type(payload) is not dict or set(payload) != _FIELDS[kind]:
            raise ValueError("Unsupported recovery command or fields")
        values = copy.deepcopy(payload)
        authority = self._owned()
        if kind in {"reconcile_request", "reconcile_action"}:
            evidence = values["evidence"]
            if not isinstance(evidence, str) or not evidence.strip() or len(evidence.encode("utf-8")) > 65536:
                raise ValueError("Reconciliation requires 1 to 65536 bytes of explicit trusted evidence")
            table, key = ("model_requests", "request_id") if kind == "reconcile_request" else ("action_receipts", "action_id")
            require_id(values[key])
            with self.store._connection() as connection:
                self.store._authority(connection, authority)
                target = connection.execute(f"SELECT r.epoch FROM {table} r JOIN attempts a ON a.id=r.attempt_id "
                                            "WHERE r.id=? AND a.run_id=?", (values[key], self.run_id)).fetchone()
                if target is None:
                    raise ScopeDenied("Reconciliation record is unavailable in this recovery scope")
                values["attempt_epoch"] = target["epoch"]
        return self.supervisor.handle(Command(command_id, self.run_id, expected_revision, authority.epoch, kind, values), authority)

    def close(self, *, timeout: float = 1.0) -> None:
        """Stop this runtime lease owner without asserting process termination."""
        with self._lock:
            self._closed = True
            self._stop_maintenance.set()
            maintenance = self._maintenance
        if maintenance is not None and maintenance is not threading.current_thread():
            maintenance.join(timeout=max(0.0, timeout))

    def __enter__(self) -> SwarmRecovery:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
