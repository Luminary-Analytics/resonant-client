"""Durable gated subprocess ownership for integration effects.

Only a trusted host launcher uses this API. Invocation is committed before its
private child receives executable argv. A stopped process proves cleanup, never
check quality or whether Git updated the checkout. Recovery never kills a PID.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

import psutil

from .models import Conflict, RunAuthority, Scope, ScopeDenied, StaleAuthority, require_id
from .processes import _os_observation, host_identity, job_name
from .store import SwarmStore, _id, canonical_json

_TABLES = {"writer": "writer_worktrees", "candidate": "integration_candidates",
           "check": "integration_checks", "application": "integration_applications"}
_ACTIVE = {"writer": {"creating", "finalizing"}, "candidate": {"preparing"},
           "check": {"running"}, "application": {"applying"}}


class IntegrationProcesses:
    """Bind each argv launch to one scoped, immutable integration intent."""

    def __init__(self, store: SwarmStore, *, host_id: str | None = None):
        self.store = store
        self.host_id = host_id or host_identity()
        require_id(self.host_id)

    @staticmethod
    def binding(connection, run_id: str, kind: str, effect_id: str) -> dict[str, Any]:
        if kind not in _TABLES:
            raise ValueError("Unsupported integration process kind")
        row = connection.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id=?", (effect_id,)).fetchone()
        if row is None:
            raise ScopeDenied("Integration effect is unavailable")
        record = dict(row)
        parent = record if kind in {"writer", "candidate"} else connection.execute(
            "SELECT * FROM integration_candidates WHERE id=?", (row["candidate_id"],),
        ).fetchone()
        if parent is None or parent["run_id"] != run_id:
            raise ScopeDenied("Integration effect is unavailable in this run")
        return {**record, "run_id": run_id, "epoch": parent["epoch"], "repo_key": parent["repo_key"]}

    def intent(self, authority: RunAuthority, effect_kind: str, effect_id: str, argv, cwd) -> str:
        """Commit an identity before spawning a child which has no argv yet."""
        if (not isinstance(argv, (tuple, list)) or not argv
                or any(type(value) is not str or not value or "\0" in value for value in argv)):
            raise ValueError("Integration argv requires nonempty strings")
        path = str(Path(cwd).resolve(strict=True))
        digest = hashlib.sha256(canonical_json(list(argv)).encode()).hexdigest()
        identity = _id()
        with self.store._connection(write=True) as connection:
            self.store._admitting(self.store._authority(connection, authority))
            effect = self.binding(connection, authority.run_id, effect_kind, effect_id)
            if effect["epoch"] != authority.epoch:
                raise StaleAuthority("Process intent belongs to an older integration epoch")
            if effect["process_protocol"] != 1 or effect["state"] not in _ACTIVE[effect_kind]:
                raise Conflict("Integration effect does not admit gated subprocesses")
            connection.execute("INSERT INTO integration_processes(id,run_id,epoch,effect_kind,effect_id,argv_sha256,cwd,host_id,state) "
                               "VALUES(?,?,?,?,?,?,?,?,'intent')",
                               (identity, authority.run_id, authority.epoch, effect_kind, effect_id, digest, path, self.host_id))
            self.store._event(connection, authority.run_id, "integration_process_intent", {
                "process_id": identity, "effect_kind": effect_kind, "effect_id": effect_id, "argv_sha256": digest,
            })
        return identity

    def _current(self, connection, authority: RunAuthority, identity: str):
        self.store._admitting(self.store._authority(connection, authority))
        row = connection.execute("SELECT * FROM integration_processes WHERE id=? AND run_id=? AND epoch=?",
                                 (identity, authority.run_id, authority.epoch)).fetchone()
        if row is None or row["host_id"] != self.host_id:
            raise ScopeDenied("Integration process does not belong to this captured owner and host")
        effect = self.binding(connection, authority.run_id, row["effect_kind"], row["effect_id"])
        if effect["state"] not in _ACTIVE[row["effect_kind"]] or effect["process_protocol"] != 1:
            raise Conflict("Integration process effect is no longer active")
        return row

    def owned(self, authority: RunAuthority, identity: str, process) -> None:
        """Capture a real owned helper before executable input can be sent."""
        if type(process.pid) is not int or process.pid <= 0 or type(process.created_at) not in (int, float) or not math.isfinite(process.created_at):
            raise ValueError("Helper identity requires a real PID and creation time")
        job_name(process.launch_token)
        observed = psutil.Process(process.pid)
        if abs(observed.create_time() - process.created_at) >= .001:
            raise Conflict("Helper process identity changed before ownership commit")
        with self.store._connection(write=True) as connection:
            row = self._current(connection, authority, identity)
            if row["state"] != "intent" or row["pid"] is not None:
                raise Conflict("An integration launch can acquire only one helper identity")
            connection.execute("UPDATE integration_processes SET state='owned',pid=?,created_at=?,launch_token=? WHERE id=?",
                               (process.pid, process.created_at, process.launch_token, identity))
            self.store._event(connection, authority.run_id, "integration_process_owned", {"process_id": identity})

    def invoke(self, authority: RunAuthority, identity: str, *, claim=None) -> None:
        """Commit potential invocation before the launcher sends its first argv."""
        with self.store._connection(write=True) as connection:
            row = self._current(connection, authority, identity)
            if row["state"] != "owned" or row["invoked"]:
                raise Conflict("Integration subprocess invocation is single use")
            if claim is not None:
                # Trusted network-free central permit consumption shares the
                # native write gate with Stop. A later local rollback may burn
                # the remote permit; it must never make it reusable.
                claim(connection)
            connection.execute("UPDATE integration_processes SET state='invoked',invoked=1 WHERE id=?", (identity,))
            self.store._event(connection, authority.run_id, "integration_process_invoked", {"process_id": identity})

    def stopped(self, scope: Scope, run_id: str, identity: str, process) -> None:
        """Retain exact observed tree cleanup even after Stop or epoch expiry."""
        if not process.cleanup_confirmed or type(process.exit_code) is not int:
            raise Conflict("Owned helper cleanup has not been observed")
        with self.store._connection(write=True) as connection:
            self.store._run(connection, scope, run_id)
            row = connection.execute("SELECT * FROM integration_processes WHERE id=? AND run_id=?", (identity, run_id)).fetchone()
            if row is not None and row["host_id"] == self.host_id and row["pid"] is None and not row["invoked"]:
                # Ownership admission may fail after Popen because Stop won.
                # The child received no argv; retain its actually joined identity.
                job_name(process.launch_token)
                if type(process.pid) is not int or type(process.created_at) not in (int, float) or not math.isfinite(process.created_at):
                    raise ValueError("Cleaned helper requires its captured OS identity")
                connection.execute("UPDATE integration_processes SET pid=?,created_at=?,launch_token=? WHERE id=?",
                                   (process.pid, process.created_at, process.launch_token, identity))
                row = connection.execute("SELECT * FROM integration_processes WHERE id=?", (identity,)).fetchone()
            if (row is None or row["host_id"] != self.host_id or row["pid"] != process.pid
                    or row["launch_token"] != process.launch_token or row["created_at"] is None
                    or abs(row["created_at"] - process.created_at) >= .001):
                raise ScopeDenied("Cleanup belongs to a different captured integration process")
            if row["state"] == "stopped" and row["exit_code"] == process.exit_code:
                return
            connection.execute("UPDATE integration_processes SET state='stopped',exit_code=? WHERE id=?", (process.exit_code, identity))
            self.store._event(connection, run_id, "integration_process_stopped", {"process_id": identity, "exit_code": process.exit_code})
            from .supervisor import SwarmSupervisor
            SwarmSupervisor(self.store)._control_checkpoint(connection, run_id)

    def not_started(self, scope: Scope, run_id: str, identity: str) -> None:
        """Close a Popen failure with no durable helper or invocation marker."""
        with self.store._connection(write=True) as connection:
            self.store._run(connection, scope, run_id)
            row = connection.execute("SELECT * FROM integration_processes WHERE id=? AND run_id=?", (identity, run_id)).fetchone()
            if row is None or row["host_id"] != self.host_id or row["pid"] is not None or row["invoked"]:
                raise Conflict("Only a never-owned, never-invoked launch can be closed without a process observation")
            connection.execute("UPDATE integration_processes SET state='not_started' WHERE id=?", (identity,))
            self.store._event(connection, run_id, "integration_process_not_started", {"process_id": identity})

    def _observe(self, record):
        if record["state"] in {"stopped", "not_started"}:
            return record["state"]  # A retained trusted observation is immutable evidence.
        if record["host_id"] != self.host_id:
            return "unknown"
        if record["pid"] is None:
            return "not_started" if not record["invoked"] else "unknown"
        if os.name != "nt" and record["invoked"]:
            # An arbitrary trusted check can create a new POSIX session. Group
            # absence after a crash cannot establish those descendants ended.
            # A stronger containment backend is required for orphan recovery.
            return "unknown"
        return _os_observation(record)

    def inspect_effect(self, scope: Scope, run_id: str, effect_kind: str, effect_id: str) -> dict[str, Any]:
        """Observe scoped local identities without mutating or claiming quality."""
        with self.store._connection() as connection:
            self.store._run(connection, scope, run_id)
            effect = self.binding(connection, run_id, effect_kind, effect_id)
            records = [dict(row) for row in connection.execute(
                "SELECT * FROM integration_processes WHERE run_id=? AND effect_kind=? AND effect_id=? ORDER BY rowid",
                (run_id, effect_kind, effect_id),
            )]
        processes = [{**{key: value for key, value in row.items() if key not in {"launch_token", "cwd"}},
                      "observation": self._observe(row)} for row in records]
        return {"effect_kind": effect_kind, "effect_id": effect_id, "epoch": effect["epoch"],
                "effect_state": effect["state"], "process_protocol": effect["process_protocol"], "processes": processes}

    def reconcile_effect(self, authority: RunAuthority, effect_kind: str, effect_id: str, *, evidence: str) -> dict[str, Any]:
        """Observe fenced process cleanup, conservatively abandoning lost results.

        No caller-selected outcome is accepted. No invocation can be replayed.
        Lost check completion is cancelled, even when the OS says it exited.
        Applied Git state still requires the separate exact ref observer.
        """
        from .supervisor import SwarmSupervisor
        SwarmSupervisor._decision_evidence(evidence)
        observation = self.inspect_effect(authority.scope, authority.run_id, effect_kind, effect_id)
        with self.store._connection(write=True) as connection:
            self.store._authority(connection, authority)
            effect = self.binding(connection, authority.run_id, effect_kind, effect_id)
            if effect["epoch"] >= authority.epoch:
                raise Conflict("Effect reconciliation requires a fenced historical owner")
            if effect["process_protocol"] != 1:
                raise Conflict("Historical effect has no complete gated process ownership protocol")
            records = [dict(row) for row in connection.execute(
                "SELECT * FROM integration_processes WHERE run_id=? AND effect_kind=? AND effect_id=? ORDER BY rowid",
                (authority.run_id, effect_kind, effect_id),
            )]
            if [row["id"] for row in records] != [row["id"] for row in observation["processes"]]:
                raise Conflict("Integration process records changed during OS observation")
            for row, observed in zip(records, observation["processes"], strict=True):
                if any(row[key] != observed[key] for key in ("epoch", "host_id", "pid", "created_at", "invoked")):
                    raise Conflict("Integration process identity changed during observation")
                if observed["observation"] not in {"stopped", "not_started"}:
                    raise Conflict("Integration helper is live or its cleanup cannot be established")
            for row, observed in zip(records, observation["processes"], strict=True):
                connection.execute("UPDATE integration_processes SET state=? WHERE id=?", (observed["observation"], row["id"]))
            invoked = any(row["invoked"] for row in records)
            unresolved = effect["state"] in (_ACTIVE[effect_kind] | {"uncertain"})
            state = effect["state"]
            if unresolved and effect_kind != "application":
                state = ("cancelled" if invoked else "not_started") if effect_kind == "check" else "failed"
                connection.execute(f"UPDATE {_TABLES[effect_kind]} SET state=? WHERE id=?", (state, effect_id))
                if effect_kind == "check":
                    connection.execute("UPDATE integration_candidates SET state='failed' WHERE id=? AND state!='applied'",
                                       (effect["candidate_id"],))
            # No destructive cleanup: partial isolated worktrees remain evidence.
            # Application state is never resolved from a process disappearance.
            supervisor = SwarmSupervisor(self.store)
            if effect_kind == "writer":
                supervisor._refresh_reservation(connection, effect["attempt_id"])
            result = {"effect_kind": effect_kind, "effect_id": effect_id, "effect_state": state,
                      "invocation_observed": invoked, "processes_observed": len(records), "state": "completed"}
            self.store._event(connection, authority.run_id, "integration_processes_reconciled", {**result, "evidence": evidence})
            supervisor._control_checkpoint(connection, authority.run_id)
            return result
