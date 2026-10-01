"""Parent-only managed collaboration admission before a receiver worker exists.

This journal binds a peer message to an independently planned local assignment.
It stores hashes/receipts, not peer text or credentials. A lost acceptance reply
never authorizes dispatch, and reopening never restores a dispatch permit. The
receiving supervisor alone owns task verification and final result acceptance.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .managed_runtime import ManagedAdmissionError
from .models import AttemptContext, Conflict, RevisionConflict, require_id


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ManagedAdmissionError() from None
    return value


class ManagedCollaborationAdmission:
    """One captured run/epoch, with immutable local intent before remote effects.

    Construct with the existing ManagedJournal, and supply this object
    as ManagedRuntime(worker_admission=...). bind_attempt is an explicit owner
    action after that owner's normal plan/assign budget reservation, before
    runner.start. No work or authority is inferred from the sender's text.
    """

    def __init__(self, path, *, supervisor, authority, journal, client):
        binding = journal.binding
        if not journal.sharing_eligible:
            raise ManagedAdmissionError()
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.supervisor, self.authority, self.binding, self.client = supervisor, authority, binding, client
        if (authority.scope.values() != (binding.tenant_id, binding.owner_id, binding.local_project_id, binding.session_id)
                or authority.run_id != binding.run_id or authority.epoch != binding.epoch):
            raise ManagedAdmissionError()
        self.instance_id = str(uuid4())
        if binding.tenant_id.startswith("personal:"):
            raise ManagedAdmissionError()
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ManagedAdmissionError()
                connection.execute("CREATE TABLE binding(document TEXT NOT NULL)")
                connection.execute("CREATE TABLE acceptances(attempt_id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, command_id TEXT NOT NULL UNIQUE, semantics TEXT NOT NULL, instance_id TEXT NOT NULL, phase TEXT NOT NULL, receipt TEXT)")
                connection.execute("INSERT INTO binding VALUES(?)", (_json(asdict(binding)),))
                connection.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ManagedAdmissionError()
            if connection.execute("SELECT document FROM binding").fetchone()[0] != _json(asdict(binding)):
                raise ManagedAdmissionError()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _snapshot(self, context):
        if (context.scope.values() != (self.binding.tenant_id, self.binding.owner_id, self.binding.local_project_id, self.binding.session_id)
                or context.run_id != self.binding.run_id or context.epoch != self.binding.epoch):
            raise ManagedAdmissionError()
        return self.supervisor.store.snapshot(context.scope, context.run_id)

    def _contract(self, context, connection=None):
        if connection is None:
            snapshot = self._snapshot(context)
            run = snapshot["run"]
            attempt = next((row for row in snapshot["attempts"] if row["id"] == context.attempt_id), None)
            reservation = next((row for row in snapshot["reservations"] if row["attempt_id"] == context.attempt_id), None)
            work = next((row for row in snapshot["work_items"] if attempt and row["id"] == attempt["work_item_id"]), None)
        else:
            run = self.supervisor.store._authority(connection, self.authority)
            attempt = self.supervisor.store._attempt(connection, context)
            reservation = connection.execute("SELECT * FROM reservations WHERE attempt_id=?", (context.attempt_id,)).fetchone()
            work = connection.execute("SELECT * FROM work_items WHERE run_id=? AND id=?", (context.run_id, attempt["work_item_id"])).fetchone()
        if (run["epoch"] != context.epoch or run["state"] != "running"
                or not attempt or attempt["epoch"] != context.epoch or attempt["kind"] != "worker"
                or attempt["state"] != "leased" or attempt["process_state"] != "pending"
                or attempt["pause_requested"] or attempt["cancel_requested"]
                or not reservation or reservation["state"] != "reserved" or not work):
            raise ManagedAdmissionError()
        contract = {"objective": work["objective"], "specification": json.loads(work["specification"]),
                    "grant": json.loads(attempt["grant_json"]), "work_item_id": work["id"],
                    "attempt_id": context.attempt_id, "epoch": context.epoch, "request_limit": reservation["amount"]}
        return {"work_item_id": work["id"], "request_limit": reservation["amount"],
                "contract_sha256": hashlib.sha256(_json(contract).encode()).hexdigest()}

    def bind_attempt(self, context, message_id):
        """Freeze this owner's exact task/grant/reservation before runner.start."""
        _uuid(message_id)
        semantics = {"message_id": message_id, **self._contract(context)}
        # This authoritative receipt survives a new epoch or a different local
        # admission journal. A crash between these two commits prevents launch;
        # it cannot restore the bound item to ordinary unmetered retry.
        with self.supervisor.store._connection(write=True) as connection:
            current = self._contract(context, connection)
            if current != {key: semantics[key] for key in current}:
                raise ManagedAdmissionError()
            self._remember_binding(connection, context, semantics)
        return self._bind_private(context, semantics)

    def _remember_binding(self, connection, context, semantics):
        payload = {**semantics, "attempt_id": context.attempt_id, "epoch": context.epoch}
        prior = self.supervisor.store._duplicate(connection, context.run_id, "managed-sharing-work", semantics["work_item_id"], payload)
        if prior is None:
            self.supervisor.store._remember(connection, context.run_id, "managed-sharing-work", semantics["work_item_id"], payload,
                                            {"work_item_id": semantics["work_item_id"], "attempt_id": context.attempt_id})
            connection.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (context.run_id,))
            self.supervisor.store._event(connection, context.run_id, "managed_sharing_bound",
                                         {"work_item_id": semantics["work_item_id"], "attempt_id": context.attempt_id})

    def _bind_private(self, context, semantics):
        message_id = semantics["message_id"]
        with self._connection() as connection:
            existing = connection.execute("SELECT * FROM acceptances WHERE attempt_id=? OR message_id=?", (context.attempt_id, message_id)).fetchone()
            if existing:
                if existing["attempt_id"] != context.attempt_id or existing["semantics"] != _json(semantics):
                    raise ManagedAdmissionError()
                return {"attempt_id": context.attempt_id, "state": existing["phase"]}
            rows = connection.execute("SELECT semantics FROM acceptances").fetchall()
            if len(rows) >= 256 or any(json.loads(row["semantics"]).get("work_item_id") in {None, semantics["work_item_id"]} for row in rows):
                raise ManagedAdmissionError()
            connection.execute("INSERT INTO acceptances VALUES(?,?,?,?,?,'prepared',NULL)",
                               (context.attempt_id, message_id, str(uuid4()), _json(semantics), self.instance_id))
        return {"attempt_id": context.attempt_id, "state": "prepared"}

    def prepare_work(self, *, work, message_id, model, requests, worker_id, command_id, expected_revision):
        """Atomically plan, allocate and fence a fresh owner task; never dispatch.

        Replays return no launch permission. If private journal initialization
        fails after the main commit, the exact assignment remains fenced and
        requires explicit recovery; it cannot appear as ordinary ready work.
        """
        _uuid(message_id)
        require_id(command_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("A captured revision is required")
        work, model = json.loads(_json(work)), json.loads(_json(model))
        intent = {"work": work, "message_id": message_id, "model": model, "requests": requests,
                  "worker_id": worker_id, "expected_revision": expected_revision}
        authority, store = self.authority, self.supervisor.store
        with store._connection(write=True) as connection:
            run = store._authority(connection, authority)
            store._admitting(run)
            prior = store._duplicate(connection, authority.run_id, "managed-sharing-prepare", command_id, intent)
            if prior is not None:
                context = AttemptContext(authority.scope, authority.run_id, prior["attempt_id"], prior["worker_id"], prior["epoch"])
                return {"context": context, "dispatch_permitted": False}
            if run["revision"] != expected_revision:
                raise RevisionConflict("Refresh the team before accepting shared work")
            if type(work) is not dict or "id" not in work or connection.execute("SELECT 1 FROM work_items WHERE id=?", (work["id"],)).fetchone():
                raise Conflict("Shared work requires a fresh owner work identity")
            if connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-work' AND json_extract(payload,'$.message_id')=?",
                                  (authority.run_id, message_id)).fetchone():
                raise Conflict("This peer request already owns a local assignment")
            retained = [json.loads(row["specification"]) for row in connection.execute("SELECT specification FROM work_items WHERE run_id=?", (authority.run_id,))]
            self.supervisor._plan(connection, run, work_items=retained + [work])
            assigned = self.supervisor._assign(connection, run, work_item_id=work["id"], worker_id=worker_id, requests=requests, model=model)
            context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], assigned["worker_id"], authority.epoch)
            semantics = {"message_id": message_id, **self._contract(context, connection)}
            self._remember_binding(connection, context, semantics)
            store._remember(connection, authority.run_id, "managed-sharing-prepare", command_id, intent,
                            {"attempt_id": context.attempt_id, "worker_id": context.worker_id, "epoch": context.epoch})
        self._bind_private(context, semantics)
        return {"context": context, "dispatch_permitted": True}

    def __call__(self, context, *, worker_id, lease_id, binding_id):
        """Called once after the shared slot, before local/remote worker claim."""
        for identity in (worker_id, lease_id, binding_id):
            _uuid(identity)
        snapshot = self._snapshot(context)
        attempt = next((row for row in snapshot["attempts"] if row["id"] == context.attempt_id), None)
        if attempt and attempt["kind"] == "coordinator":
            return  # Sharing binds receiver workers, never ordinary coordinators.
        contract = self._contract(context)
        with self.supervisor.store._connection() as connection:
            self.supervisor.store._authority(connection, self.authority)
            main = connection.execute("SELECT payload FROM commands WHERE run_id=? AND actor='managed-sharing-work' AND key=?",
                                      (context.run_id, contract["work_item_id"])).fetchone()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM acceptances WHERE attempt_id=?", (context.attempt_id,)).fetchone()
            if row is None:
                if main is not None:
                    raise ManagedAdmissionError()
                recorded = connection.execute("SELECT semantics FROM acceptances").fetchall()
                if any(json.loads(item["semantics"]).get("work_item_id") in {None, contract["work_item_id"]} for item in recorded):
                    raise ManagedAdmissionError()
                return  # Unrelated owner-created tasks retain the ordinary path.
        current = {"message_id": row["message_id"], **contract}
        if main is None or json.loads(main["payload"]) != {**current, "attempt_id": context.attempt_id, "epoch": context.epoch}:
            raise ManagedAdmissionError()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM acceptances WHERE attempt_id=?", (context.attempt_id,)).fetchone()
            if row["phase"] != "prepared" or row["instance_id"] != self.instance_id or row["semantics"] != _json(current):
                raise ManagedAdmissionError()
            connection.execute("UPDATE acceptances SET phase='pending' WHERE attempt_id=?", (context.attempt_id,))
        # No SQLite transaction or runner lock spans this network operation.
        result = self.client.sharing_accept(lease_id=lease_id, command_id=row["command_id"], message_id=row["message_id"],
            worker_id=worker_id, request_limit=current["request_limit"], contract_sha256=current["contract_sha256"])
        expected = {"message_id": row["message_id"], "worker_id": worker_id, "request_limit": current["request_limit"],
                    "state": "work_reserved", "dispatch_permitted": True}
        if result != expected:
            raise ManagedAdmissionError()
        with self._connection() as connection:
            connection.execute("UPDATE acceptances SET phase='admitted',receipt=? WHERE attempt_id=? AND phase='pending'", (_json(result), context.attempt_id))

    def inspect(self):
        """Safe owner metadata; admitted is a permit receipt, never a task result."""
        with self._connection() as connection:
            return [{"attempt_id": row["attempt_id"], "message_id": row["message_id"], "state": row["phase"],
                     "request_limit": json.loads(row["semantics"])["request_limit"]}
                    for row in connection.execute("SELECT * FROM acceptances ORDER BY attempt_id LIMIT 256")]

    def accepted_work_item_ids(self):
        """Owner UI/preassignment fence; even uncertain acceptance binds the item."""
        with self.supervisor.store._connection() as connection:
            self.supervisor.store._run(connection, self.authority.scope, self.authority.run_id)
            return [row["key"] for row in connection.execute("SELECT key FROM commands WHERE run_id=? AND actor='managed-sharing-work' ORDER BY key", (self.authority.run_id,))]

    @staticmethod
    def require_unbound_assignment(connection, run_id, work_item_id):
        """Retry/recovery may not spend outside an exact accepted peer contract."""
        if connection.execute("SELECT 1 FROM commands WHERE run_id=? AND actor='managed-sharing-work' AND key=?", (run_id, work_item_id)).fetchone():
            raise Conflict("Managed shared work requires a new explicit peer request and work item")
