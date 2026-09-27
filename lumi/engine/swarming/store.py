"""Local SQLite protocol foundation for swarming.

This module dispatches no workers and admits no tool effects. Callers must keep
the database in project runtime state on the same host. Connections are private
to each operation, writes are short BEGIN IMMEDIATE transactions, and callbacks,
provider requests and process operations never run inside a transaction.

DELETE journaling is deliberate until the packaged SQLite build is qualified
for WAL. FULL synchronization is required before acknowledging a command.
Recovery fences old database authority, but cannot terminate external workers.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import (
    PROTOCOL_VERSION,
    AdmissionClosed,
    AllowanceExceeded,
    AttemptContext,
    Claim,
    Conflict,
    Event,
    IdempotencyConflict,
    LeaseExpired,
    Message,
    Receipt,
    RunAuthority,
    SchemaVersionError,
    Scope,
    ScopeDenied,
    StaleAuthority,
    WorkerPaused,
    require_id,
)

SCHEMA_VERSION = 3
_APPLICATION_ID = 0x53574152  # SWAR; reject unrelated SQLite files.
_SCHEMA = (
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        project_id TEXT NOT NULL, session_id TEXT NOT NULL,
        supervisor_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch > 0),
        protocol_version INTEGER NOT NULL, objective TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','pausing','paused','stopping','cancelled','failed','completed','recovery_required')),
        request_limit INTEGER NOT NULL CHECK(request_limit >= 0),
        event_sequence INTEGER NOT NULL DEFAULT 0,
        managed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, lease_seconds REAL NOT NULL DEFAULT 30,
        policy_json TEXT NOT NULL DEFAULT '{}', stop_requested INTEGER NOT NULL DEFAULT 0,
        worker_limit INTEGER NOT NULL DEFAULT 2 CHECK(worker_limit BETWEEN 1 AND 4))""",
    """CREATE TABLE work_items (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        objective TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('pending','ready','leased','running','submitted','accepted','failed','cancelled','uncertain')),
        specification TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(run_id,id))""",
    """CREATE TABLE attempts (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        work_item_id TEXT, worker_id TEXT NOT NULL, epoch INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('leased','running','submitted','completed','failed','cancelled','uncertain')),
        grant_json TEXT NOT NULL DEFAULT '{}', process_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(process_state IN ('pending','running','stopped','unknown')),
        kind TEXT NOT NULL DEFAULT 'worker' CHECK(kind IN ('worker','coordinator')),
        pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0,1)),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        CHECK((kind='worker' AND work_item_id IS NOT NULL) OR (kind='coordinator' AND work_item_id IS NULL)),
        CHECK(state!='completed' OR kind='coordinator'),
        FOREIGN KEY(run_id,work_item_id) REFERENCES work_items(run_id,id),
        UNIQUE(run_id,id))""",
    """CREATE UNIQUE INDEX active_work_claim ON attempts(work_item_id)
        WHERE state IN ('leased','running','uncertain')""",
    """CREATE UNIQUE INDEX active_worker_claim ON attempts(run_id,worker_id)
        WHERE state IN ('leased','running','uncertain')""",
    """CREATE UNIQUE INDEX active_coordinator_claim ON attempts(run_id)
        WHERE kind='coordinator' AND state IN ('leased','running','uncertain')""",
    """CREATE TABLE reservations (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
        amount INTEGER NOT NULL CHECK(amount > 0), used INTEGER,
        state TEXT NOT NULL CHECK(state IN ('reserved','settled','uncertain')),
        CHECK((state='settled' AND used >= 0 AND used <= amount) OR
              (state IN ('reserved','uncertain') AND used IS NULL)))""",
    """CREATE TABLE dispatches (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
        state TEXT NOT NULL CHECK(state IN ('pending','started','finished','uncertain')))""",
    """CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
        epoch INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, occurred_at REAL, run_state TEXT,
        PRIMARY KEY(run_id,sequence))""",
    """CREATE TABLE commands (
        run_id TEXT NOT NULL REFERENCES runs(id), actor TEXT NOT NULL, key TEXT NOT NULL,
        payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id,actor,key))""",
    """CREATE TABLE messages (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
        sender_attempt_id TEXT NOT NULL, recipient_attempt_id TEXT NOT NULL,
        epoch INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, reply_to TEXT,
        FOREIGN KEY(run_id,sender_attempt_id) REFERENCES attempts(run_id,id),
        FOREIGN KEY(run_id,recipient_attempt_id) REFERENCES attempts(run_id,id),
        FOREIGN KEY(reply_to) REFERENCES messages(id), UNIQUE(run_id,sequence))""",
    """CREATE INDEX recipient_messages ON messages(recipient_attempt_id,sequence)""",
    """CREATE TABLE receipts (
        message_id TEXT NOT NULL REFERENCES messages(id),
        recipient_attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('runtime','context')),
        model_request_id TEXT, input_revision INTEGER,
        PRIMARY KEY(message_id,stage),
        CHECK((stage='runtime' AND model_request_id IS NULL AND input_revision IS NULL) OR
              (stage='context' AND model_request_id IS NOT NULL AND input_revision > 0)))""",
    """CREATE TABLE work_dependencies (
        run_id TEXT NOT NULL, work_item_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
        PRIMARY KEY(run_id,work_item_id,dependency_id),
        FOREIGN KEY(run_id,work_item_id) REFERENCES work_items(run_id,id),
        FOREIGN KEY(run_id,dependency_id) REFERENCES work_items(run_id,id))""",
    """CREATE TABLE model_requests (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
        epoch INTEGER NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('main','auxiliary')),
        state TEXT NOT NULL CHECK(state IN ('reserved','started','completed','failed','not_started','uncertain')),
        used INTEGER CHECK(used IN (0,1)),
        CHECK((state IN ('completed','failed','not_started') AND used IS NOT NULL)
            OR (state IN ('reserved','started','uncertain') AND used IS NULL)))""",
    """CREATE TABLE submissions (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), candidate_revision TEXT NOT NULL,
        handoff TEXT NOT NULL, work_revision INTEGER NOT NULL)""",
    """CREATE TABLE check_receipts (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
        criterion_id TEXT NOT NULL, candidate_revision TEXT NOT NULL,
        executor_id TEXT NOT NULL, check_name TEXT NOT NULL, exit_code INTEGER NOT NULL,
        evidence TEXT NOT NULL)""",
    """CREATE TABLE artifact_refs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        origin TEXT NOT NULL, model_request_id TEXT, tool_call_id TEXT,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0), kind TEXT NOT NULL,
        media_type TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(run_id,id))""",
    """CREATE TABLE artifact_grants (
        artifact_id TEXT NOT NULL REFERENCES artifact_refs(id),
        recipient_attempt_id TEXT NOT NULL REFERENCES attempts(id),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
        PRIMARY KEY(artifact_id,recipient_attempt_id))""",
    """CREATE TABLE request_inputs (
        request_id TEXT PRIMARY KEY REFERENCES model_requests(id), input_sha256 TEXT NOT NULL,
        purpose TEXT NOT NULL, input_artifact_id TEXT REFERENCES artifact_refs(id),
        usage_json TEXT, observation_error TEXT NOT NULL DEFAULT '',
        observation_outcome TEXT CHECK(observation_outcome IN ('completed','uncertain')))""",
    """CREATE TABLE owner_directives (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        owner_id TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL)""",
    """CREATE TABLE owner_directive_receipts (
        directive_id TEXT PRIMARY KEY REFERENCES owner_directives(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL REFERENCES request_inputs(request_id),
        input_sha256 TEXT NOT NULL, input_revision INTEGER NOT NULL CHECK(input_revision>0))""",
    """CREATE TABLE action_receipts (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL REFERENCES model_requests(id), call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('admitted','completed','uncertain')),
        output_artifact_id TEXT REFERENCES artifact_refs(id), is_error INTEGER,
        metadata_json TEXT, UNIQUE(request_id,call_id))""",
    """CREATE TABLE writer_worktrees (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), epoch INTEGER NOT NULL,
        repo_key TEXT NOT NULL, path TEXT NOT NULL, base_revision TEXT NOT NULL,
        result_revision TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)))""",
    """CREATE TABLE integration_candidates (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL,
        repo_key TEXT NOT NULL, path TEXT NOT NULL, base_revision TEXT NOT NULL,
        result_revision TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)))""",
    """CREATE TABLE integration_checks (
        id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        check_key TEXT NOT NULL, candidate_revision TEXT NOT NULL, argv_json TEXT NOT NULL,
        state TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL DEFAULT '',
        job_id TEXT NOT NULL DEFAULT '',
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)))""",
    """CREATE TABLE integration_applications (
        id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        expected_base TEXT NOT NULL, target_revision TEXT NOT NULL,
        state TEXT NOT NULL, observed_revision TEXT NOT NULL DEFAULT '',
        approval_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)))""",
    """CREATE TABLE integration_processes (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL,
        effect_kind TEXT NOT NULL CHECK(effect_kind IN ('writer','candidate','check','application')),
        effect_id TEXT NOT NULL, argv_sha256 TEXT NOT NULL, cwd TEXT NOT NULL,
        host_id TEXT NOT NULL, pid INTEGER CHECK(pid>0), created_at REAL, launch_token TEXT,
        state TEXT NOT NULL CHECK(state IN ('intent','owned','invoked','stopped','unknown','not_started')),
        invoked INTEGER NOT NULL DEFAULT 0 CHECK(invoked IN (0,1)), exit_code INTEGER,
        CHECK((pid IS NULL AND created_at IS NULL AND launch_token IS NULL) OR
              (pid IS NOT NULL AND created_at IS NOT NULL AND launch_token IS NOT NULL)))""",
    """CREATE TABLE integration_operations (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        supervisor_id TEXT NOT NULL, epoch INTEGER NOT NULL, command_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        effect_id TEXT NOT NULL, approval_expires_at REAL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled','uncertain')),
        result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
        UNIQUE(run_id,supervisor_id,command_id))""",
    """CREATE TABLE writer_acceptances (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
        writer_id TEXT NOT NULL REFERENCES writer_worktrees(id),
        candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        application_id TEXT NOT NULL REFERENCES integration_applications(id),
        work_revision INTEGER NOT NULL, writer_revision TEXT NOT NULL,
        candidate_revision TEXT NOT NULL, checks_json TEXT NOT NULL,
        evidence TEXT NOT NULL, owner_id TEXT NOT NULL, epoch INTEGER NOT NULL)""",
    """CREATE TABLE process_observations (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), run_id TEXT NOT NULL REFERENCES runs(id),
        epoch INTEGER NOT NULL, host_id TEXT NOT NULL, pid INTEGER NOT NULL CHECK(pid>0),
        created_at REAL NOT NULL, launch_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('started','stopped','unknown')), exit_code INTEGER)""",
    """CREATE TABLE coordinator_proposals (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), request_id TEXT NOT NULL REFERENCES model_requests(id),
        epoch INTEGER NOT NULL, input_revision INTEGER NOT NULL, sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
        decision_evidence TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE coordinator_inputs (
        request_id TEXT PRIMARY KEY REFERENCES model_requests(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), input_sha256 TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL, graph_sha256 TEXT NOT NULL,
        policy_digest TEXT NOT NULL, criteria_json TEXT NOT NULL)""",
    """CREATE TABLE run_hosts (
        run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL CHECK(epoch>0),
        host_id TEXT NOT NULL, pid INTEGER NOT NULL CHECK(pid>0), created_at REAL NOT NULL,
        PRIMARY KEY(run_id,epoch))""",
)

_COLLABORATION_SCHEMA = ('CREATE TABLE collaboration_grants (\n'
 ' id TEXT PRIMARY KEY,\n'
 ' origin_run_id TEXT NOT NULL REFERENCES runs(id), receiver_run_id TEXT NOT NULL REFERENCES runs(id),\n'
 ' origin_epoch INTEGER NOT NULL, receiver_epoch INTEGER NOT NULL,\n'
 ' origin_policy_sha256 TEXT NOT NULL, receiver_policy_sha256 TEXT NOT NULL,\n'
 ' terms_json TEXT NOT NULL, terms_sha256 TEXT NOT NULL,\n'
 " state TEXT NOT NULL CHECK(state IN ('offered','active','revoked')),\n"
 ' created_at REAL NOT NULL, accepted_at REAL, revoked_at REAL,\n'
 ' revoked_by_run_id TEXT REFERENCES runs(id), revocation_evidence TEXT,\n'
 ' CHECK(origin_run_id != receiver_run_id)\n'
 ')',
 'CREATE TABLE collaboration_messages (\n'
 ' id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES collaboration_grants(id),\n'
 ' sender_run_id TEXT NOT NULL REFERENCES runs(id), recipient_run_id TEXT NOT NULL REFERENCES runs(id),\n'
 " kind TEXT NOT NULL CHECK(kind IN ('question','finding','artifact_offer','work_request','work_result')),\n"
 " data_class TEXT NOT NULL CHECK(data_class IN ('summary','code','artifact_reference')),\n"
 ' body TEXT NOT NULL, body_sha256 TEXT NOT NULL,\n'
 ' artifacts_json TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count>=0),\n'
 ' origin_message_id TEXT NOT NULL REFERENCES collaboration_messages(id),\n'
 ' parent_id TEXT REFERENCES collaboration_messages(id), hop INTEGER NOT NULL CHECK(hop BETWEEN 1 AND 8),\n'
 ' created_at REAL NOT NULL, delivered_at REAL, delivered_epoch INTEGER,\n'
 ' CHECK(sender_run_id != recipient_run_id), UNIQUE(origin_message_id,recipient_run_id,kind),\n'
 ' UNIQUE(recipient_run_id,id),\n'
 ' CHECK((delivered_at IS NULL AND delivered_epoch IS NULL) OR (delivered_at IS NOT NULL AND '
 'delivered_epoch>0))\n'
 ')',
 'CREATE TABLE collaboration_acceptances (\n'
 ' message_id TEXT PRIMARY KEY REFERENCES collaboration_messages(id),\n'
 ' receiver_run_id TEXT NOT NULL REFERENCES runs(id), work_item_id TEXT NOT NULL UNIQUE REFERENCES '
 'work_items(id),\n'
 ' attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), request_allowance INTEGER NOT NULL '
 'CHECK(request_allowance>0),\n'
 ' evidence TEXT NOT NULL, accepted_at REAL NOT NULL,\n'
 ' FOREIGN KEY(receiver_run_id,message_id) REFERENCES collaboration_messages(recipient_run_id,id),\n'
 ' FOREIGN KEY(receiver_run_id,work_item_id) REFERENCES work_items(run_id,id),\n'
 ' FOREIGN KEY(receiver_run_id,attempt_id) REFERENCES attempts(run_id,id)\n'
 ')',
 'CREATE INDEX collaboration_message_grant ON collaboration_messages(grant_id)',
 'CREATE INDEX collaboration_message_parent ON collaboration_messages(parent_id)')
_SCHEMA += _COLLABORATION_SCHEMA

_V1_TABLES = ("runs", "work_items", "attempts", "reservations", "dispatches", "events",
              "commands", "messages", "receipts")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_json(value: Any) -> str:
    """Encode shared proposal/input fingerprints without ASCII escaping.

    Existing command storage keeps its original encoding for idempotent replay.
    New cross-module fingerprints must use this exact UTF-8 JSON contract.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _id() -> str:
    return uuid.uuid4().hex


def _count(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Expected an integer >= {minimum}")


class SwarmStore:
    """A scoped, durable store for trusted local runtime adapters.

    Opening does not reconcile, replay, settle, or resume anything. Unknown
    schemas fail closed; inspect_database and backup_database do not assume a
    schema and remain available to a downgraded client. There are no released
    earlier released schemas. Local v1/v2 stores upgrade transactionally after
    a coherent backup; uncertainty is retained without replaying any effect.
    """

    def __init__(
        self, path: str | Path, *, read_only: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only
        self.clock = clock
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=not read_only, initialize=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not read_only and version == 0 and application == 0 and not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif not read_only and version == 1 and application == _APPLICATION_ID:
                # RESERVED locking in DELETE mode excludes other writers while
                # the separate backup reader copies the last committed state.
                backup = self.path.with_name(f"{self.path.name}.v1-{_id()}.backup")
                self.backup_database(self.path, backup)
                self._migrate_v1(connection)
            elif not read_only and version == 2 and application == _APPLICATION_ID:
                backup = self.path.with_name(f"{self.path.name}.v2-{_id()}.backup")
                self.backup_database(self.path, backup)
                self._migrate_v2(connection)
            elif version != SCHEMA_VERSION or application != _APPLICATION_ID:
                raise SchemaVersionError(f"Unsupported swarm schema version {version}")

    @contextmanager
    def _connection(
        self, *, write: bool = False, initialize: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if write and self.read_only:
            raise PermissionError("Swarm store is read-only")
        uri = f"{self.path.as_uri()}?mode={'rwc' if write else 'ro'}"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                # Do not change the journal of an unrecognized/downgraded store.
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                application = connection.execute("PRAGMA application_id").fetchone()[0]
                supported = (0, 1, 2, SCHEMA_VERSION) if initialize else (SCHEMA_VERSION,)
                if version not in supported or application not in (0, _APPLICATION_ID):
                    raise SchemaVersionError(f"Unsupported swarm schema version {version}")
                journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if journal != "delete":
                    raise SchemaVersionError("Swarm store requires DELETE journal mode")
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            if not initialize:
                if (connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                        or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID):
                    raise SchemaVersionError("Swarm schema changed; reopen with a compatible client")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add explicit run collaboration without altering historical work."""
        for statement in _COLLABORATION_SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        """Rebuild constrained tables atomically, retaining every historical row."""
        columns: dict[str, list[str]] = {}
        for table in _V1_TABLES:
            columns[table] = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            connection.execute(f"CREATE TEMP TABLE migration_{table} AS SELECT * FROM {table}")
        for table in reversed(_V1_TABLES):
            connection.execute(f"DROP TABLE {table}")
        for statement in _SCHEMA:
            connection.execute(statement)
        for table in _V1_TABLES:
            names = ",".join('"' + column.replace('"', '""') + '"' for column in columns[table])
            connection.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM migration_{table}")
            connection.execute(f"DROP TABLE migration_{table}")
        connection.execute("UPDATE attempts SET process_state=CASE WHEN state='leased' THEN 'pending' "
                           "WHEN state='running' THEN 'running' WHEN state='uncertain' THEN 'unknown' ELSE 'stopped' END")
        connection.execute("UPDATE runs SET worker_limit=COALESCE(json_extract(policy_json,'$.max_workers'),2) WHERE managed=1")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def inspect_database(path: str | Path) -> dict[str, Any]:
        """Inspect schema metadata read-only, even for an unsupported version.

        This administrative file operation discloses no run/message content.
        File access must be authorized by the runtime before exposing it in UI.
        """
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            return {
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "integrity": [row[0] for row in connection.execute("PRAGMA quick_check")],
            }

    @staticmethod
    def backup_database(source: str | Path, destination: str | Path) -> Path:
        """Copy a coherent SQLite snapshot without interpreting or migrating it.

        The destination must be new. This is a privileged local file operation,
        not a scope-filtered export endpoint or an artifact access permission.
        """
        source_path = Path(source).expanduser().resolve()
        target = Path(destination).expanduser().resolve()
        # Exclusive creation prevents accidentally overwriting a retained backup.
        with target.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as origin:
                with closing(sqlite3.connect(target)) as copy:
                    origin.backup(copy)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def _run(self, connection: sqlite3.Connection, scope: Scope, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE id=? AND tenant_id=? AND owner_id=? "
            "AND project_id=? AND session_id=?", (run_id, *scope.values()),
        ).fetchone()
        if row is None:
            raise ScopeDenied("Run is unavailable in this scope")
        if row["protocol_version"] != PROTOCOL_VERSION:
            raise SchemaVersionError("Unsupported swarm protocol")
        return row

    def _authority(self, connection: sqlite3.Connection, authority: RunAuthority) -> sqlite3.Row:
        row = self._run(connection, authority.scope, authority.run_id)
        if row["epoch"] != authority.epoch or row["supervisor_id"] != authority.supervisor_id:
            raise StaleAuthority("Supervisor authority is no longer current")
        if row["managed"] and row["lease_until"] <= self.clock():
            raise LeaseExpired("Supervisor lease expired; explicit reconciliation is required")
        return row

    def _legacy_authority(self, connection: sqlite3.Connection, authority: RunAuthority) -> sqlite3.Row:
        row = self._authority(connection, authority)
        if row["managed"]:
            raise Conflict("Managed runs require versioned supervisor commands")
        return row

    def _attempt(self, connection: sqlite3.Connection, context: AttemptContext) -> sqlite3.Row:
        run = self._run(connection, context.scope, context.run_id)
        if run["epoch"] != context.epoch:
            raise StaleAuthority("Worker epoch is no longer current")
        if run["managed"] and run["lease_until"] <= self.clock():
            raise LeaseExpired("Worker admission lease expired")
        row = connection.execute(
            "SELECT * FROM attempts WHERE run_id=? AND id=? AND worker_id=? AND epoch=?",
            (context.run_id, context.attempt_id, context.worker_id, context.epoch),
        ).fetchone()
        if row is None:
            raise ScopeDenied("Attempt is unavailable in this scope")
        return row

    @staticmethod
    def _admitting(run: sqlite3.Row) -> None:
        if run["state"] != "running":
            raise AdmissionClosed(f"Run admission is closed: {run['state']}")

    @staticmethod
    def _attempt_admitting(attempt: sqlite3.Row) -> None:
        """Check controls only before new effects, never for observations."""
        if attempt["cancel_requested"]:
            raise AdmissionClosed("Participant cancellation closes new admission")
        if attempt["pause_requested"]:
            raise WorkerPaused("Participant is paused at its next execution boundary")

    def _event(self, connection: sqlite3.Connection, run_id: str, kind: str, payload: Any) -> int:
        row = connection.execute(
            "UPDATE runs SET event_sequence=event_sequence+1 WHERE id=? "
            "RETURNING event_sequence,epoch,state", (run_id,),
        ).fetchone()
        sequence = row["event_sequence"]
        connection.execute("INSERT INTO events(run_id,sequence,epoch,kind,payload,occurred_at,run_state) VALUES (?,?,?,?,?,?,?)",
                           (run_id, sequence, row["epoch"], kind, _json(payload), self.clock(), row["state"]))
        return sequence

    @staticmethod
    def _duplicate(
        connection: sqlite3.Connection, run_id: str, actor: str, key: str, payload: Any,
    ) -> dict[str, Any] | None:
        require_id(key)
        row = connection.execute(
            "SELECT payload,result FROM commands WHERE run_id=? AND actor=? AND key=?",
            (run_id, actor, key),
        ).fetchone()
        if row is None:
            return None
        if row["payload"] != _json(payload):
            raise IdempotencyConflict("Command key already has different arguments")
        return json.loads(row["result"])

    @staticmethod
    def _remember(
        connection: sqlite3.Connection, run_id: str, actor: str, key: str,
        payload: Any, result: Any,
    ) -> None:
        connection.execute("INSERT INTO commands VALUES (?,?,?,?,?)",
                           (run_id, actor, key, _json(payload), _json(result)))

    def create_run(
        self, scope: Scope, *, supervisor_id: str, objective: str, request_limit: int,
        run_id: str | None = None,
    ) -> RunAuthority:
        """Create a run, with run_id serving as its durable idempotency key."""
        run_id = run_id or _id()
        require_id(run_id)
        require_id(supervisor_id)
        _count(request_limit)
        with self._connection(write=True) as connection:
            existing = connection.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                row = self._run(connection, scope, run_id)
                if (row["supervisor_id"], row["objective"], row["request_limit"]) != (
                    supervisor_id, objective, request_limit,
                ):
                    raise IdempotencyConflict("Run identity already has different arguments")
                if row["managed"] or row["epoch"] != 1:
                    raise StaleAuthority("Run creation cannot renew recovered supervisor authority")
                return RunAuthority(scope, run_id, supervisor_id, row["epoch"])
            connection.execute(
                "INSERT INTO runs(id,tenant_id,owner_id,project_id,session_id,supervisor_id,"
                "epoch,protocol_version,objective,state,request_limit) VALUES(?,?,?,?,?,?,1,?,?,'running',?)",
                (run_id, *scope.values(), supervisor_id, PROTOCOL_VERSION, objective, request_limit),
            )
            self._event(connection, run_id, "run_created", {"request_limit": request_limit})
        return RunAuthority(scope, run_id, supervisor_id, 1)

    def add_work_item(
        self, authority: RunAuthority, *, work_item_id: str, objective: str,
    ) -> None:
        """Add one ready item; dependency planning belongs to the supervisor."""
        require_id(work_item_id)
        with self._connection(write=True) as connection:
            run = self._legacy_authority(connection, authority)
            row = connection.execute("SELECT * FROM work_items WHERE id=?", (work_item_id,)).fetchone()
            if row:
                if row["run_id"] != authority.run_id:
                    raise ScopeDenied("Work item is unavailable in this scope")
                if row["objective"] != objective:
                    raise IdempotencyConflict("Work item identity already has a different objective")
                return
            self._admitting(run)
            connection.execute("INSERT INTO work_items(id,run_id,objective,state) VALUES(?,?,?,'ready')",
                               (work_item_id, authority.run_id, objective))
            self._event(connection, authority.run_id, "work_item_added", {"work_item_id": work_item_id})

    def claim(
        self, authority: RunAuthority, *, work_item_id: str, worker_id: str,
        requests: int, command_id: str,
    ) -> Claim:
        """Commit assignment, allowance, event and dispatch intent atomically.

        A repeated command returns the same intent; it never authorizes another
        process launch. Executors must reconcile pending/uncertain dispatches.
        """
        require_id(worker_id)
        _count(requests, minimum=1)
        payload = {"operation": "claim", "work_item_id": work_item_id,
                   "worker_id": worker_id, "requests": requests, "epoch": authority.epoch}
        actor = f"supervisor:{authority.supervisor_id}"
        with self._connection(write=True) as connection:
            run = self._legacy_authority(connection, authority)
            previous = self._duplicate(connection, authority.run_id, actor, command_id, payload)
            if previous is not None:
                return self._claim_from_dict(authority.scope, previous)
            self._admitting(run)
            work = connection.execute("SELECT state FROM work_items WHERE run_id=? AND id=?",
                                      (authority.run_id, work_item_id)).fetchone()
            if work is None:
                raise ScopeDenied("Work item is unavailable in this scope")
            if work["state"] != "ready":
                raise Conflict("Work item already has a claim or result")
            active = connection.execute(
                "SELECT id FROM attempts WHERE run_id=? AND worker_id=? "
                "AND state IN ('leased','running','uncertain')", (authority.run_id, worker_id),
            ).fetchone()
            if active:
                raise Conflict("Worker already owns an unresolved attempt")
            if requests + self._allocated(connection, authority.run_id) > run["request_limit"]:
                raise AllowanceExceeded("Request allowance is already allocated or exhausted")
            attempt_id, reservation_id, dispatch_id = _id(), _id(), _id()
            connection.execute("INSERT INTO attempts(id,run_id,work_item_id,worker_id,epoch,state) VALUES(?,?,?,?,?,'leased')",
                               (attempt_id, authority.run_id, work_item_id, worker_id, authority.epoch))
            connection.execute("UPDATE work_items SET state='leased' WHERE id=?", (work_item_id,))
            connection.execute("INSERT INTO reservations VALUES(?,?,?,NULL,'reserved')",
                               (reservation_id, attempt_id, requests))
            connection.execute("INSERT INTO dispatches VALUES(?,?,'pending')", (dispatch_id, attempt_id))
            claim = Claim(AttemptContext(authority.scope, authority.run_id, attempt_id,
                                         worker_id, authority.epoch),
                          work_item_id, reservation_id, dispatch_id, requests)
            self._event(connection, authority.run_id, "assignment_claimed", asdict(claim))
            self._remember(connection, authority.run_id, actor, command_id, payload, asdict(claim))
            return claim

    @staticmethod
    def _claim_from_dict(scope: Scope, value: dict[str, Any]) -> Claim:
        context = {**value["context"], "scope": scope}
        return Claim(**{**value, "context": AttemptContext(**context)})

    @staticmethod
    def _allocated(connection: sqlite3.Connection, run_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN r.state='settled' THEN r.used ELSE r.amount END),0) "
            "FROM reservations r JOIN attempts a ON a.id=r.attempt_id WHERE a.run_id=?", (run_id,),
        ).fetchone()[0]

    def mark_dispatched(self, authority: RunAuthority, context: AttemptContext) -> None:
        """Record an observed worker start; this is not provider-request evidence."""
        with self._connection(write=True) as connection:
            run = self._legacy_authority(connection, authority)
            self._same_run(authority, context)
            attempt = self._attempt(connection, context)
            if attempt["state"] == "running":
                return
            self._admitting(run)
            if attempt["state"] != "leased":
                raise Conflict("Attempt cannot be dispatched")
            connection.execute("UPDATE attempts SET state='running' WHERE id=?", (context.attempt_id,))
            connection.execute("UPDATE work_items SET state='running' WHERE id=?", (attempt["work_item_id"],))
            connection.execute("UPDATE dispatches SET state='started' WHERE attempt_id=?", (context.attempt_id,))
            self._event(connection, context.run_id, "worker_started", {"attempt_id": context.attempt_id})

    @staticmethod
    def _same_run(authority: RunAuthority, context: AttemptContext) -> None:
        if (authority.scope, authority.run_id, authority.epoch) != (
            context.scope, context.run_id, context.epoch,
        ):
            raise ScopeDenied("Attempt does not belong to this captured run")

    def finish_attempt(
        self, authority: RunAuthority, context: AttemptContext, *, outcome: str,
        used_requests: int, command_id: str,
    ) -> None:
        """Record trusted termination and known accounting, not work acceptance.

        Only an executor observation of stopped execution and complete accounting
        permits this call. A model's handoff or successful process exit is not
        sufficient evidence of request usage or accepted artifact quality.
        """
        if outcome not in ("submitted", "failed", "cancelled"):
            raise ValueError("Expected submitted, failed or cancelled outcome")
        _count(used_requests)
        actor = f"supervisor:{authority.supervisor_id}"
        payload = {"operation": "finish", "attempt_id": context.attempt_id,
                   "outcome": outcome, "used_requests": used_requests, "epoch": authority.epoch}
        with self._connection(write=True) as connection:
            self._legacy_authority(connection, authority)
            self._same_run(authority, context)
            attempt = self._attempt(connection, context)
            if self._duplicate(connection, context.run_id, actor, command_id, payload) is not None:
                return
            if attempt["state"] not in ("leased", "running"):
                raise Conflict("Attempt is already terminal or requires reconciliation")
            reservation = connection.execute("SELECT amount FROM reservations WHERE attempt_id=?",
                                             (context.attempt_id,)).fetchone()
            if used_requests > reservation["amount"]:
                raise Conflict("Observed usage exceeds this reservation")
            connection.execute("UPDATE attempts SET state=? WHERE id=?", (outcome, context.attempt_id))
            connection.execute("UPDATE work_items SET state=? WHERE id=?", (outcome, attempt["work_item_id"]))
            connection.execute("UPDATE reservations SET state='settled',used=? WHERE attempt_id=?",
                               (used_requests, context.attempt_id))
            connection.execute("UPDATE dispatches SET state='finished' WHERE attempt_id=?", (context.attempt_id,))
            self._event(connection, context.run_id, "attempt_finished", payload)
            self._remember(connection, context.run_id, actor, command_id, payload, {})
            self._close_stopped(connection, context.run_id)

    def stop(self, authority: RunAuthority, *, command_id: str) -> None:
        """Close admission durably; outstanding execution remains stopping."""
        actor = f"supervisor:{authority.supervisor_id}"
        payload = {"operation": "stop", "epoch": authority.epoch}
        with self._connection(write=True) as connection:
            run = self._legacy_authority(connection, authority)
            if self._duplicate(connection, authority.run_id, actor, command_id, payload) is not None:
                return
            if run["state"] == "running":
                connection.execute("UPDATE runs SET state='stopping' WHERE id=?", (authority.run_id,))
                self._event(connection, authority.run_id, "stop_requested", {})
            connection.execute("UPDATE work_items SET state='cancelled' WHERE run_id=? AND state='ready'",
                               (authority.run_id,))
            self._close_stopped(connection, authority.run_id)
            self._remember(connection, authority.run_id, actor, command_id, payload, {})

    def _close_stopped(self, connection: sqlite3.Connection, run_id: str) -> None:
        unresolved = connection.execute(
            "SELECT 1 FROM attempts WHERE run_id=? AND state IN ('leased','running','uncertain') LIMIT 1",
            (run_id,),
        ).fetchone()
        if not unresolved:
            changed = connection.execute("UPDATE runs SET state='cancelled' WHERE id=? AND state='stopping'",
                                         (run_id,)).rowcount
            if changed:
                self._event(connection, run_id, "stop_confirmed", {})

    def reconcile(self, authority: RunAuthority, *, supervisor_id: str) -> RunAuthority:
        """Fence a lost supervisor and retain every unresolved allocation.

        Requires trusted recovery ownership outside this store. No lease expiry,
        database reopen, or missing process is proof an effect did not occur.
        This leaves admission closed and performs no replay, refund or retry.
        """
        require_id(supervisor_id)
        with self._connection(write=True) as connection:
            run = self._legacy_authority(connection, authority)
            if run["state"] == "cancelled":
                raise Conflict("A confirmed stopped run does not require recovery")
            connection.execute("UPDATE runs SET epoch=epoch+1,supervisor_id=?,state='recovery_required' WHERE id=?",
                               (supervisor_id, authority.run_id))
            connection.execute("UPDATE attempts SET state='uncertain' WHERE run_id=? AND state IN ('leased','running')",
                               (authority.run_id,))
            connection.execute("UPDATE work_items SET state='uncertain' WHERE run_id=? AND state IN ('leased','running')",
                               (authority.run_id,))
            connection.execute("UPDATE reservations SET state='uncertain' WHERE state='reserved' AND attempt_id IN "
                               "(SELECT id FROM attempts WHERE run_id=?)", (authority.run_id,))
            connection.execute("UPDATE dispatches SET state='uncertain' WHERE state IN ('pending','started') "
                               "AND attempt_id IN (SELECT id FROM attempts WHERE run_id=?)", (authority.run_id,))
            self._event(connection, authority.run_id, "recovery_required", {"previous_epoch": authority.epoch})
        return RunAuthority(authority.scope, authority.run_id, supervisor_id, authority.epoch + 1)

    def send(
        self, context: AttemptContext, *, recipient_attempt_id: str, kind: str,
        body: str, command_id: str, reply_to: str | None = None,
    ) -> Message:
        """Persist addressed task data; no message can change runtime authority."""
        with self._connection(write=True) as connection:
            return self._send(connection, context, recipient_attempt_id=recipient_attempt_id,
                              kind=kind, body=body, command_id=command_id, reply_to=reply_to)

    def _send(
        self, connection: sqlite3.Connection, context: AttemptContext, *,
        recipient_attempt_id: str, kind: str, body: str, command_id: str,
        reply_to: str | None = None,
    ) -> Message:
        """Compose message acceptance with related changes in one transaction."""
        if kind not in ("finding", "question", "answer", "blocker", "change_proposal", "handoff_reference"):
            raise ValueError("Unsupported agent message kind")
        if not isinstance(body, str) or len(body.encode("utf-8")) > 8192:
            raise ValueError("Message body must be at most 8 KiB")
        actor = f"attempt:{context.attempt_id}"
        payload = {"operation": "send", "recipient": recipient_attempt_id, "kind": kind,
                   "body": body, "reply_to": reply_to, "epoch": context.epoch}
        sender = self._attempt(connection, context)
        previous = self._duplicate(connection, context.run_id, actor, command_id, payload)
        if previous is not None:
            return Message(**previous)
        self._admitting(self._run(connection, context.scope, context.run_id))
        if sender["state"] not in ("leased", "running"):
            raise Conflict("Sender attempt is no longer active")
        recipient = connection.execute(
            "SELECT id FROM attempts WHERE run_id=? AND id=? AND epoch=? AND state IN ('leased','running')",
            (context.run_id, recipient_attempt_id, context.epoch),
        ).fetchone()
        if recipient is None:
            raise ScopeDenied("Recipient is unavailable in this run and epoch")
        if reply_to is not None:
            reply = connection.execute(
                "SELECT id FROM messages WHERE id=? AND run_id=? AND epoch=? "
                "AND sender_attempt_id=? AND recipient_attempt_id=?",
                (reply_to, context.run_id, context.epoch, recipient_attempt_id, context.attempt_id),
            ).fetchone()
            if reply is None:
                raise ScopeDenied("Reply target is unavailable to these participants")
        pending = connection.execute(
            "SELECT COUNT(*) FROM messages m WHERE recipient_attempt_id=? AND NOT EXISTS "
            "(SELECT 1 FROM receipts r WHERE r.message_id=m.id AND r.stage='context')",
            (recipient_attempt_id,),
        ).fetchone()[0]
        if pending >= 100:
            raise Conflict("Recipient has 100 messages pending context delivery")
        message_id = _id()
        sequence = self._event(connection, context.run_id, "message_accepted", {"message_id": message_id})
        message = Message(message_id, sequence, context.attempt_id, recipient_attempt_id,
                          context.epoch, kind, body, reply_to)
        connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                           (message.id, context.run_id, message.sequence, message.sender_attempt_id,
                            message.recipient_attempt_id, message.epoch, message.kind, message.body,
                            message.reply_to))
        self._remember(connection, context.run_id, actor, command_id, payload, asdict(message))
        return message

    def receive(self, context: AttemptContext, *, after: int = 0, limit: int = 100) -> list[Message]:
        """Read addressed messages; reads neither acknowledge nor spend requests."""
        _count(after)
        _count(limit, minimum=1)
        if limit > 100:
            raise ValueError("Maximum page size is 100")
        with self._connection() as connection:
            attempt = self._attempt(connection, context)
            self._admitting(self._run(connection, context.scope, context.run_id))
            if attempt["state"] not in ("leased", "running"):
                raise Conflict("Recipient attempt is no longer active")
            rows = connection.execute(
                "SELECT id,sequence,sender_attempt_id,recipient_attempt_id,epoch,kind,body,reply_to "
                "FROM messages WHERE run_id=? AND recipient_attempt_id=? AND epoch=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (context.run_id, context.attempt_id, context.epoch, after, limit),
            ).fetchall()
            return [Message(**dict(row)) for row in rows]

    def acknowledge(
        self, context: AttemptContext, message_id: str, *, stage: str,
        model_request_id: str | None = None, input_revision: int | None = None,
    ) -> Receipt:
        """Record runtime delivery or durable inclusion in a specific input.

        Runtime adapters call this, never the model. Context receipts require a
        previous runtime receipt and a captured model-request/input identity.
        """
        if stage == "context":
            require_id(model_request_id)
            _count(input_revision, minimum=1)
        elif stage != "runtime" or model_request_id is not None or input_revision is not None:
            raise ValueError("Invalid receipt stage or input identity")
        receipt = Receipt(message_id, context.attempt_id, context.epoch, stage,
                          model_request_id, input_revision)
        with self._connection(write=True) as connection:
            attempt = self._attempt(connection, context)
            run = self._run(connection, context.scope, context.run_id)
            if stage == "context" and run["managed"]:
                raise Conflict("Managed context receipts require atomic mailbox input assignment")
            message = connection.execute(
                "SELECT id FROM messages WHERE id=? AND run_id=? AND recipient_attempt_id=? AND epoch=?",
                (message_id, context.run_id, context.attempt_id, context.epoch),
            ).fetchone()
            if message is None:
                raise ScopeDenied("Message is unavailable to this recipient")
            previous = connection.execute("SELECT * FROM receipts WHERE message_id=? AND stage=?",
                                          (message_id, stage)).fetchone()
            if previous:
                if dict(previous) != asdict(receipt):
                    raise IdempotencyConflict("Receipt already binds a different model input")
                return receipt
            self._admitting(self._run(connection, context.scope, context.run_id))
            if attempt["state"] not in ("leased", "running"):
                raise Conflict("Recipient attempt is no longer active")
            if stage == "context" and not connection.execute(
                "SELECT 1 FROM receipts WHERE message_id=? AND stage='runtime'", (message_id,),
            ).fetchone():
                raise Conflict("Runtime delivery must be recorded before context inclusion")
            connection.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?)",
                               (message_id, context.attempt_id, context.epoch, stage,
                                model_request_id, input_revision))
            self._event(connection, context.run_id, f"message_{stage}", asdict(receipt))
            return receipt

    def events(self, scope: Scope, run_id: str, *, after: int = 0, limit: int = 100) -> list[Event]:
        """Replay a bounded page in monotonic per-run order."""
        _count(after)
        _count(limit, minimum=1)
        if limit > 1000:
            raise ValueError("Maximum event page size is 1000")
        with self._connection() as connection:
            self._run(connection, scope, run_id)
            rows = connection.execute("SELECT sequence,kind,epoch,payload,occurred_at,run_state FROM events WHERE run_id=? "
                                      "AND sequence>? ORDER BY sequence LIMIT ?", (run_id, after, limit))
            return [Event(row["sequence"], row["kind"], row["epoch"], json.loads(row["payload"]), row["occurred_at"], row["run_state"])
                    for row in rows]

    def snapshot(self, scope: Scope, run_id: str) -> dict[str, Any]:
        """Read one coherent, owner-scoped runtime snapshot and replay cursor."""
        with self._connection() as connection:
            run = dict(self._run(connection, scope, run_id))
            result = {"run": run, "remaining_requests": run["request_limit"] - self._allocated(connection, run_id)}
            for table in ("work_items", "attempts", "messages"):
                result[table] = [dict(row) for row in connection.execute(
                    f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,),
                )]
            for table in ("reservations", "dispatches"):
                result[table] = [dict(row) for row in connection.execute(
                    f"SELECT r.* FROM {table} r JOIN attempts a ON a.id=r.attempt_id WHERE a.run_id=? ORDER BY r.rowid",
                    (run_id,),
                )]
            result["receipts"] = [dict(row) for row in connection.execute(
                "SELECT r.* FROM receipts r JOIN messages m ON m.id=r.message_id WHERE m.run_id=? ORDER BY r.rowid",
                (run_id,),
            )]
            result["dependencies"] = [dict(row) for row in connection.execute(
                "SELECT * FROM work_dependencies WHERE run_id=? ORDER BY work_item_id,dependency_id", (run_id,),
            )]
            for table in ("model_requests", "submissions", "check_receipts", "artifact_refs", "action_receipts", "writer_acceptances", "process_observations", "coordinator_inputs", "owner_directives", "owner_directive_receipts"):
                result[table] = [dict(row) for row in connection.execute(
                    f"SELECT t.* FROM {table} t JOIN attempts a ON a.id=t.attempt_id WHERE a.run_id=? ORDER BY t.rowid",
                    (run_id,),
                )]
            result["request_inputs"] = [dict(row) for row in connection.execute(
                "SELECT i.* FROM request_inputs i JOIN model_requests r ON r.id=i.request_id "
                "JOIN attempts a ON a.id=r.attempt_id WHERE a.run_id=? ORDER BY i.rowid", (run_id,),
            )]
            for table in ("writer_worktrees", "integration_candidates", "integration_operations", "integration_processes", "coordinator_proposals", "run_hosts"):
                result[table] = [dict(row) for row in connection.execute(
                    f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,),
                )]
            for table in ("integration_checks", "integration_applications"):
                result[table] = [dict(row) for row in connection.execute(
                    f"SELECT t.* FROM {table} t JOIN integration_candidates c ON c.id=t.candidate_id "
                    "WHERE c.run_id=? ORDER BY t.rowid", (run_id,),
                )]
            return result
