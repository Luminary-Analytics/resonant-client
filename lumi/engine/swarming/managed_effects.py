"""One-use central permits for trusted owner Git/check process invocations.

This host journal is separate from the model-request journal. It retains hashes
and public identities only. It never authenticates a browser, renews old leases,
or interprets process disappearance as a successful check. Network calls are
outside native/journal transactions; the final claim is network-free and fenced
by the native supervisor's write transaction before argv reaches the child.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from .argv_process import ArgvResult
from .integration_processes import IntegrationProcesses
from .managed_client import HostChannelError
from .managed_journal import absence_receipt
from .managed_runtime import ManagedAdmissionError
from .models import Conflict, ScopeDenied
from .store import canonical_json


KINDS = {"writer": "writer_git", "candidate": "candidate_git",
         "check": "candidate_check", "application": "checkout_apply"}
_TERMINAL = {"completed", "failed", "never_started"}
_SCHEMA = """
CREATE TABLE binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1), document TEXT NOT NULL,
 coverage_version INTEGER NOT NULL CHECK(coverage_version=1));
CREATE TABLE effects(process_id TEXT PRIMARY KEY, remote_id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, semantics TEXT NOT NULL, instance_id TEXT NOT NULL,
 phase TEXT NOT NULL, lease TEXT, binding_id TEXT, receipt TEXT,
 claimed INTEGER NOT NULL DEFAULT 0, cleanup_observed INTEGER NOT NULL DEFAULT 0,
 outcome TEXT, evidence_sha256 TEXT);
CREATE TABLE observations(id TEXT PRIMARY KEY, process_id TEXT NOT NULL REFERENCES effects(process_id),
 outcome TEXT NOT NULL, receipt TEXT, superseded INTEGER NOT NULL DEFAULT 0,
 last_attempt INTEGER NOT NULL DEFAULT 0, UNIQUE(process_id,outcome));
PRAGMA user_version=1;
"""


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ManagedAdmissionError()
    return value


class ManagedEffects:
    """Captured managed run/epoch authority; constructed by the trusted owner."""

    def __init__(self, store, runtime, *, path=None):
        self.store, self.runtime, self.client = store, runtime, runtime.client
        self.binding = runtime.binding
        self.path = Path(path) if path else runtime.journal.path.with_suffix(".effects.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.open("xb").close()
        except FileExistsError:
            raise Conflict("Retained managed effects require explicit observation-only recovery")
        self._initialize(create=True)

    @classmethod
    def open_history(cls, store, journal, client):
        """Open existing observation history without renewing any authority."""
        result = object.__new__(cls)
        result.store, result.runtime, result.client = store, None, client
        result.binding = journal.binding
        result.path = journal.path.with_suffix(".effects.sqlite")
        if not result.path.is_file():
            raise Conflict("Managed effect history is unavailable")
        result._initialize(create=False)
        return result

    def _initialize(self, *, create):
        self.instance_id = str(uuid4())
        self._deadlines = {}
        self._flush_lock = threading.Lock()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True, initialize=create) as connection:
            row = connection.execute("SELECT * FROM binding WHERE singleton=1").fetchone()
            document = canonical_json(asdict(self.binding))
            if row is None:
                if not create:
                    raise Conflict("Managed effect coverage record is unavailable")
                connection.execute("INSERT INTO binding VALUES(1,?,1)", (document,))
            elif row["document"] != document or row["coverage_version"] != 1:
                raise ScopeDenied("Managed effect history belongs to another captured binding")

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True,
                                     timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 1 and not (initialize and version == 0):
                raise Conflict("Unsupported managed effects journal version")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if initialize:
                connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if version == 0:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _authority(self, authority, *, historical=False):
        binding = self.binding
        if (authority.scope.values() != (binding.tenant_id, binding.owner_id, binding.local_project_id, binding.session_id)
                or authority.run_id != binding.run_id
                or (authority.epoch <= binding.epoch if historical else authority.epoch != binding.epoch)):
            raise ScopeDenied("Managed effect belongs to another captured owner")

    def descriptor(self, connection, authority, process_id, *, environment, timeout_seconds, max_output_bytes):
        """Hash the exact invocation and immutable candidate/approval context."""
        self._authority(authority)
        process = IntegrationProcesses(self.store)._current(connection, authority, process_id)
        effect = IntegrationProcesses.binding(connection, authority.run_id, process["effect_kind"], process["effect_id"])
        keys = {"writer": ("id", "attempt_id", "epoch", "repo_key", "base_revision", "manifest_json", "state"),
                "candidate": ("id", "epoch", "repo_key", "base_revision", "manifest_json", "state"),
                "check": ("id", "candidate_id", "candidate_revision", "check_key", "argv_json"),
                "application": ("id", "candidate_id", "expected_base", "target_revision", "approval_json")}
        inputs = {key: effect[key] for key in keys[process["effect_kind"]]}
        if process["effect_kind"] in {"check", "application"}:
            candidate = connection.execute("SELECT base_revision,result_revision,manifest_json FROM integration_candidates WHERE id=?",
                                           (effect["candidate_id"],)).fetchone()
            inputs["candidate"] = dict(candidate)
        return {"version": 1, "process_id": process_id, "effect_id": process["effect_id"],
                "run_id": authority.run_id, "epoch": authority.epoch, "supervisor_id": authority.supervisor_id,
                "kind": KINDS[process["effect_kind"]], "repo_key": effect["repo_key"],
                "argv_sha256": process["argv_sha256"], "cwd_sha256": _hash(process["cwd"]),
                "environment_sha256": _hash(environment), "inputs_sha256": _hash(inputs),
                "timeout_seconds": timeout_seconds, "max_output_bytes": max_output_bytes}

    def prepare_effect(self, authority, process_id, *, environment, timeout_seconds, max_output_bytes):
        """Persist a new identity before one network admission; never retry it."""
        if self.runtime is None:
            raise ManagedAdmissionError()
        with self.store._connection() as connection:
            semantics = self.descriptor(connection, authority, process_id, environment=environment,
                timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
        document = canonical_json(semantics)
        with self._connection(write=True) as connection:
            old = connection.execute("SELECT * FROM effects WHERE process_id=?", (process_id,)).fetchone()
            if old is not None:
                if old["semantics"] != document:
                    raise Conflict("Managed effect identity cannot change its immutable invocation")
                raise ManagedAdmissionError()
            if connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] >= 10000:
                raise Conflict("Managed effect journal reached its per-run limit")
            remote_id = str(uuid4())
            connection.execute("INSERT INTO effects(process_id,remote_id,kind,semantics,instance_id,phase) VALUES(?,?,?,?,?,'prepared')",
                               (process_id, remote_id, semantics["kind"], document, self.instance_id))
        try:
            binding_id = _uuid(self.runtime.register())
            lease, deadline = self.runtime.effect_lease()
            if (lease["policy"].get("policy_version") != 2
                    or semantics["kind"] not in lease["policy"].get("allowed_effects", [])):
                raise ManagedAdmissionError()
            with self._connection(write=True) as connection:
                row = self._row(connection, process_id)
                if row["phase"] != "prepared" or row["instance_id"] != self.instance_id:
                    raise ManagedAdmissionError()
                connection.execute("UPDATE effects SET phase='pending',lease=?,binding_id=? WHERE process_id=?",
                                   (canonical_json(lease), binding_id, process_id))
            try:
                receipt = self.client.authorize_effect(lease_id=lease["lease_id"], binding_id=binding_id,
                    effect_id=remote_id, kind=semantics["kind"], semantics_sha256=_hash(semantics))
            except HostChannelError as exc:
                if not exc.delivery_unknown:
                    with self._connection(write=True) as connection:
                        connection.execute("UPDATE effects SET phase='not_admitted',outcome='never_started',cleanup_observed=1 "
                                           "WHERE process_id=? AND phase='pending'", (process_id,))
                raise ManagedAdmissionError() from None
            self.runtime.claim_effect_lease(lease, deadline)
            if (type(receipt) is not dict or receipt.get("effect_id") != remote_id
                    or receipt.get("state") != "admitted" or receipt.get("dispatch_permitted") is not True):
                raise ManagedAdmissionError()
            with self._connection(write=True) as connection:
                row = self._row(connection, process_id)
                if row["phase"] != "pending" or row["instance_id"] != self.instance_id:
                    raise ManagedAdmissionError()
                connection.execute("UPDATE effects SET phase='permitted',receipt=? WHERE process_id=?",
                                   (canonical_json(receipt), process_id))
            self._deadlines[process_id] = deadline
        except BaseException:
            self._abandon(process_id)
            raise

    @staticmethod
    def _row(connection, process_id):
        row = connection.execute("SELECT * FROM effects WHERE process_id=?", (process_id,)).fetchone()
        if row is None:
            raise ScopeDenied("Managed effect identity is unavailable")
        return row

    def claim_effect(self, authority, process_id, connection):
        """Consume a permit inside the native BEGIN IMMEDIATE invocation gate."""
        self._authority(authority)
        process = IntegrationProcesses(self.store)._current(connection, authority, process_id)
        if process["state"] != "owned" or process["invoked"] or self.runtime is None:
            raise ManagedAdmissionError()
        with self._connection(write=True) as journal:
            row = self._row(journal, process_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "permitted" or row["claimed"]:
                raise ManagedAdmissionError()
            # Re-read native candidate/approval bindings without retaining raw
            # argv, environment values, or user paths in this public journal.
            semantics = json.loads(row["semantics"])
            recomputed = self.descriptor(connection, authority, process_id, environment={},
                timeout_seconds=semantics["timeout_seconds"], max_output_bytes=semantics["max_output_bytes"])
            recomputed["environment_sha256"] = semantics["environment_sha256"]
            if canonical_json(recomputed) != row["semantics"]:
                raise Conflict("Managed effect immutable native inputs changed before invocation")
            self.runtime.claim_effect_lease(json.loads(row["lease"]), self._deadlines.get(process_id, 0))
            journal.execute("UPDATE effects SET phase='claimed',claimed=1 WHERE process_id=?", (process_id,))

    def _queue(self, connection, process_id, outcome, *, cleanup, evidence=None):
        row = self._row(connection, process_id)
        if row["phase"] == "not_admitted":
            if outcome != "never_started":
                raise Conflict("A definitely denied effect has no remote invocation")
            return
        if row["outcome"] in _TERMINAL and row["outcome"] != outcome:
            raise Conflict("Terminal managed effect observations are immutable")
        if outcome == "never_started" and row["claimed"]:
            raise Conflict("A consumed effect permit cannot be reported never started")
        connection.execute("UPDATE effects SET phase='observed',outcome=?,cleanup_observed=MAX(cleanup_observed,?),"
                           "evidence_sha256=COALESCE(?,evidence_sha256) WHERE process_id=?",
                           (outcome, int(cleanup), evidence, process_id))
        if row["lease"] is None:
            return
        if outcome != "uncertain":
            connection.execute("UPDATE observations SET superseded=1 WHERE process_id=? AND outcome='uncertain'", (process_id,))
        connection.execute("INSERT OR IGNORE INTO observations(id,process_id,outcome) VALUES(?,?,?)", (str(uuid4()), process_id, outcome))

    def _abandon(self, process_id):
        with self._connection(write=True) as connection:
            row = self._row(connection, process_id)
            if row["phase"] == "not_admitted":
                return
            self._queue(connection, process_id, "never_started" if row["phase"] == "prepared" else "uncertain",
                        cleanup=not row["claimed"])

    def observe_effect(self, authority, process_id, result, *, cleanup_confirmed):
        """Retain actual command observation after owned cleanup, including Stop."""
        self._authority(authority)
        with self.store._connection() as native:
            self.store._run(native, authority.scope, authority.run_id)
            process = native.execute("SELECT * FROM integration_processes WHERE id=? AND run_id=? AND epoch=?",
                (process_id, authority.run_id, authority.epoch)).fetchone()
            if process is None:
                raise ScopeDenied("Native integration process is unavailable")
            cleanup_confirmed = bool(cleanup_confirmed and process["state"] in {"stopped", "not_started"})
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM effects WHERE process_id=?", (process_id,)).fetchone()
            if row is None:
                # Stop may win before the first journal insert. Since the host
                # cannot send admission before that insert, an actual native
                # never-invoked receipt needs no invented central observation.
                if process["state"] == "not_started" and not process["invoked"]:
                    return
                raise ScopeDenied("Managed effect identity is unavailable")
            if row["instance_id"] != self.instance_id:
                raise ManagedAdmissionError()
            if row["phase"] == "not_admitted":
                return
            if not row["claimed"]:
                outcome = "never_started"
            elif (cleanup_confirmed and isinstance(result, ArgvResult) and result.output_complete
                    and type(result.exit_code) is int and not result.timed_out and not result.cancelled
                    and result.interruption is None):
                outcome = "completed" if result.exit_code == 0 else "failed"
            else:
                outcome = "uncertain"
            evidence = _hash({"process_id": process_id, "cleanup": bool(cleanup_confirmed), "outcome": outcome,
                              "exit_code": result.exit_code if isinstance(result, ArgvResult) else None})
            self._queue(connection, process_id, outcome, cleanup=cleanup_confirmed or not row["claimed"], evidence=evidence)

    def fence_restart(self):
        """Invalidate unconsumed permits; never restore an old instance's lease."""
        with self._connection(write=True) as connection:
            for row in connection.execute("SELECT * FROM effects WHERE instance_id<>?", (self.instance_id,)).fetchall():
                if row["outcome"] in _TERMINAL:
                    continue
                self._queue(connection, row["process_id"], "uncertain" if row["claimed"] else "never_started",
                            cleanup=not row["claimed"])

    def reconcile_process(self, authority, process_id):
        """Use retained native OS cleanup; a lost command result stays unknown."""
        self._authority(authority, historical=True)
        with self.store._connection(write=True) as native:
            self.store._authority(native, authority)
            process = native.execute("SELECT * FROM integration_processes WHERE id=? AND run_id=? AND epoch=?",
                (process_id, self.binding.run_id, self.binding.epoch)).fetchone()
            if process is None or process["state"] not in {"stopped", "not_started"}:
                raise Conflict("Managed effect requires a retained native process cleanup observation")
            with self._connection(write=True) as connection:
                row = self._row(connection, process_id)
                outcome = row["outcome"] if row["outcome"] in _TERMINAL else ("uncertain" if row["claimed"] else "never_started")
                self._queue(connection, process_id, outcome, cleanup=True,
                            evidence=_hash({key: process[key] for key in ("id", "run_id", "epoch", "state", "invoked", "exit_code")}))
        return self.inspect()

    def inspect(self):
        """Bounded public status; no paths, argv, environment, lease, or keys."""
        with self._connection() as connection:
            counts = connection.execute("SELECT COUNT(*) total,SUM(outcome='uncertain') uncertain,"
                "SUM(claimed=0 AND outcome IS NULL) unclaimed,SUM(claimed=1 AND cleanup_observed=0) claimed_without_cleanup FROM effects").fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM observations WHERE receipt IS NULL AND superseded=0").fetchone()[0]
            unresolved = connection.execute("SELECT COUNT(*) FROM effects e WHERE e.cleanup_observed=0 OR e.outcome IS NULL "
                "OR (e.lease IS NOT NULL AND e.phase<>'not_admitted' AND NOT EXISTS "
                "(SELECT 1 FROM observations o WHERE o.process_id=e.process_id AND o.outcome=e.outcome "
                "AND o.receipt IS NOT NULL AND o.superseded=0))").fetchone()[0]
            rows = [dict(row) for row in connection.execute("SELECT process_id,remote_id,kind,phase,claimed,cleanup_observed,outcome, "
                                                            "(claimed=0 AND lease IS NOT NULL AND phase<>'not_admitted') AS can_fence_absent "
                                                            "FROM effects ORDER BY rowid DESC LIMIT 100")]
        return {**{key: value or 0 for key, value in dict(counts).items()}, "pending_observations": pending,
                "resume_ready": unresolved == 0, "cleanup_pending": unresolved, "effects": rows}

    def absence_intent(self, process_id):
        """Capture an old unclaimed effect identity without renewing a lease."""
        with self._connection() as connection:
            row = self._absence_row(connection, process_id)
            return {"resource_kind": "effect", "resource_id": row["remote_id"], "lease_id": json.loads(row["lease"])["lease_id"]}

    def _absence_row(self, connection, process_id):
        row = self._row(connection, process_id)
        if (self.runtime is not None or row["instance_id"] == self.instance_id or row["claimed"]
                or row["lease"] is None or row["outcome"] not in (None, "uncertain", "never_started")):
            raise Conflict("Only an old unclaimed owner effect can be fenced absent")
        return row

    def record_absence(self, process_id, receipt):
        """Keep server tombstone proof distinct from a command observation."""
        with self._connection(write=True) as connection:
            row = self._absence_row(connection, process_id)
            encoded = absence_receipt(receipt, "effect", row["remote_id"], json.loads(row["lease"])["lease_id"])
            old = connection.execute("SELECT receipt FROM observations WHERE process_id=? AND outcome='fenced_absent'", (process_id,)).fetchone()
            if old and old[0] != encoded:
                raise Conflict("Non-admission fence receipt changed")
            if old is None:
                connection.execute("INSERT INTO observations(id,process_id,outcome,receipt) VALUES(?,?,'fenced_absent',?)",
                                   (str(uuid4()), process_id, encoded))
            connection.execute("UPDATE observations SET superseded=1 WHERE process_id=? AND receipt IS NULL", (process_id,))
            connection.execute("UPDATE effects SET phase='not_admitted',outcome='never_started',cleanup_observed=1,evidence_sha256=? WHERE process_id=?",
                               (hashlib.sha256(encoded.encode()).hexdigest(), process_id))

    def flush(self, *, maximum=8):
        """Deliver bounded, fair observation retries without renewing a lease."""
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError("Invalid effect observation batch limit")
        if not self._flush_lock.acquire(blocking=False):
            return {"delivered": 0, "unavailable": True}
        delivered, unavailable = 0, False
        try:
            deadline = time.monotonic() + 5
            with self._connection() as connection:
                rows = [dict(row) for row in connection.execute("SELECT o.*,e.remote_id FROM observations o JOIN effects e "
                    "ON e.process_id=o.process_id WHERE o.receipt IS NULL AND o.superseded=0 ORDER BY o.last_attempt,o.rowid LIMIT ?", (maximum,))]
            for row in rows:
                if time.monotonic() >= deadline:
                    unavailable = True
                    break
                try:
                    with self._connection(write=True) as connection:
                        connection.execute("UPDATE observations SET last_attempt=(SELECT COALESCE(MAX(last_attempt),0)+1 FROM observations) WHERE id=?", (row["id"],))
                    receipt = self.client.observe_effect(effect_id=row["remote_id"], outcome=row["outcome"])
                    if (type(receipt) is not dict or receipt.get("effect_id") != row["remote_id"]
                            or receipt.get("state") != row["outcome"] or receipt.get("dispatch_permitted") is not False
                            or receipt.get("source") != "authenticated_host_observation"):
                        raise ManagedAdmissionError()
                    with self._connection(write=True) as connection:
                        old = connection.execute("SELECT receipt FROM observations WHERE id=?", (row["id"],)).fetchone()[0]
                        encoded = canonical_json(receipt)
                        if old is not None and old != encoded:
                            raise Conflict("Effect delivery receipt changed")
                        connection.execute("UPDATE observations SET receipt=? WHERE id=?", (encoded, row["id"]))
                    delivered += 1
                except Exception:
                    unavailable = True
            return {"delivered": delivered, "unavailable": unavailable}
        finally:
            self._flush_lock.release()
