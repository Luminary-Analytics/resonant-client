"""One-way, operator-only retention recovery into a permanently quarantined DB.

This is not backup creation or authority reconstruction. No key material is
loaded; neither source sealing nor target reconciliation has an unseal operation.
The separate archive credential must be read-only and its identities are pinned
independently of the supplied checkpoint. Database owners remain trusted.
"""

from __future__ import annotations

from contextlib import contextmanager
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .archive import _safe_role, _validate_payload
from .models import Conflict, InvalidRequest, digest, identifier
from .restore_guard import RESTORE_LOCK
from . import restore_sharing

MAX_RECORDS = 100000
_CONTENT = {"publish_content", "hold_content", "release_content_hold", "delete_content"}
_FORMAT = "sonn-sealed-retention-checkpoint"


@contextmanager
def _operator(dsn):
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout='30s'")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL synchronous_commit=on")
        # Offline operators must see every row, including forced RLS tables.
        role = connection.execute("SELECT rolsuper OR rolbypassrls AS allowed FROM pg_roles WHERE rolname=current_user").fetchone()
        if not role["allowed"]:
            raise InvalidRequest("offline recovery requires a protected database operator")
        yield connection


def _identity(connection):
    row = connection.execute("SELECT current_database() AS database,oid::bigint AS database_oid,"
        "(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier "
        "FROM pg_database WHERE datname=current_database()").fetchone()
    return dict(row)


def database_identity(operator_dsn):
    """Read nonsecret identity for independent operator pinning, without sealing."""
    with _operator(operator_dsn) as connection:
        return _identity(connection)


def _expected_identity(connection, expected):
    if (type(expected) is not dict or set(expected) != {"database", "database_oid", "system_identifier"}
            or type(expected["database"]) is not str or type(expected["database_oid"]) is not int
            or type(expected["system_identifier"]) is not str or _identity(connection) != expected):
        raise Conflict("database identity differs from the independent operator pin")


def _offline(connection):
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (RESTORE_LOCK,))
    _no_other_clients(connection)


def _no_other_clients(connection):
    # A final observation catches clients that arrived during validation. This
    # does not replace externally stopped services/network isolation: an old
    # binary can ignore the advisory lock and race any finite observation.
    connection.execute("SELECT pg_stat_clear_snapshot()")
    if connection.execute("SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                          "AND pid<>pg_backend_pid() AND backend_type='client backend' LIMIT 1").fetchone():
        raise Conflict("offline recovery requires all other database clients to be stopped")


def _lock_governance(connection):
    version = connection.execute("SELECT version FROM sonn_governance.schema_version WHERE singleton").fetchone()
    if not version or version["version"] not in {14, 15}:
        raise InvalidRequest("offline retention recovery requires governance schema 14 or 15")
    tables = connection.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='sonn_governance' AND c.relkind IN ('r','p') ORDER BY c.relname").fetchall()
    for table in tables:
        connection.execute(sql.SQL("LOCK TABLE sonn_governance.{} IN SHARE MODE").format(sql.Identifier(table["relname"])))


def _sequence(connection):
    row = connection.execute("SELECT n.nspname,c.relname,s.seqincrement,s.seqcycle,s.seqcache FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_sequence s ON s.seqrelid=c.oid "
        "WHERE c.oid=pg_get_serial_sequence('sonn_governance.audit','audit_id')::regclass").fetchone()
    if not row or row["seqincrement"] != 1 or row["seqcycle"] or row["seqcache"] != 1:
        raise Conflict("audit sequence configuration is unsupported")
    name = sql.Identifier(row["nspname"], row["relname"])
    value = connection.execute(sql.SQL("SELECT last_value,is_called FROM {}").format(name)).fetchone()
    next_value = value["last_value"] + int(value["is_called"])
    maximum = connection.execute("SELECT coalesce(max(audit_id),0) AS maximum FROM sonn_governance.audit").fetchone()["maximum"]
    if not maximum < next_value < 2**63:
        raise Conflict("audit sequence is behind retained evidence or exhausted")
    return name, next_value


def _rows(connection, tenant_id):
    rows = connection.execute("SELECT audit_id,sha256,payload FROM sonn_governance.audit_outbox "
        "WHERE tenant_id=%s ORDER BY audit_id LIMIT %s", (tenant_id, MAX_RECORDS + 1)).fetchall()
    if not rows or len(rows) > MAX_RECORDS:
        raise InvalidRequest("checkpoint audit set is empty or exceeds the offline limit")
    for row in rows:
        _validate_payload(tenant_id, row["audit_id"], bytes(row["payload"]), row["sha256"])
    ids = [row["audit_id"] for row in rows]
    actual = connection.execute("SELECT audit_id FROM sonn_governance.audit WHERE tenant_id=%s ORDER BY audit_id", (tenant_id,)).fetchall()
    if ids != [row["audit_id"] for row in actual]:
        raise Conflict("source audit and durable outbox differ")
    return rows


def _entries(rows):
    return [{"audit_id": row["audit_id"], "sha256": row["sha256"]} for row in rows]


def _archive(archive_dsn, *, archive_id, source_id, tenant_id):
    with psycopg.connect(archive_dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout='30s'")
        _safe_role(connection, schema="sonn_archive", tables=("configuration", "records"))
        for table in ("configuration", "records"):
            if connection.execute("SELECT has_any_column_privilege(current_user,%s,'INSERT') AS unsafe",
                                  ("sonn_archive." + table,)).fetchone()["unsafe"]:
                raise InvalidRequest("restore archive credential must be read-only")
        config = connection.execute("SELECT version,archive_id FROM sonn_archive.configuration WHERE singleton").fetchone()
        if not config or config["version"] != 1 or str(config["archive_id"]) != archive_id:
            raise Conflict("archive identity differs from the independent operator pin")
        rows = connection.execute("SELECT audit_id,sha256,payload,receipt_id FROM sonn_archive.records "
            "WHERE source_id=%s AND tenant_id=%s ORDER BY audit_id LIMIT %s",
            (source_id, tenant_id, MAX_RECORDS + 1)).fetchall()
        if len(rows) > MAX_RECORDS:
            raise InvalidRequest("archive set exceeds the offline limit")
        for row in rows:
            _validate_payload(tenant_id, row["audit_id"], bytes(row["payload"]), row["sha256"])
        return rows


def _create_guard(connection, *, mode, restore_id, document):
    connection.execute("CREATE SCHEMA sonn_restore")
    connection.execute("REVOKE ALL ON SCHEMA sonn_restore FROM PUBLIC")
    connection.execute("CREATE TABLE sonn_restore.guard(singleton boolean PRIMARY KEY CHECK(singleton),"
        "mode text NOT NULL CHECK(mode IN ('source_sealed','restore_target')),restore_id uuid NOT NULL,"
        "document jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
    connection.execute("INSERT INTO sonn_restore.guard(singleton,mode,restore_id,document) VALUES(true,%s,%s,%s)",
                       (mode, restore_id, Jsonb(document)))
    connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA sonn_restore FROM PUBLIC")


def _deny_runtime_connections(connection):
    """Keep old service binaries out too; only the trusted operator retains CONNECT.

    The offline check excludes existing clients. Database ACL changes are part
    of the same transaction as the permanent marker and roll back on failure.
    A schema-only pg_restore cannot replace these database-level ACLs.
    """
    database = connection.execute("SELECT current_database() AS name,current_user AS operator").fetchone()
    grants = connection.execute("SELECT DISTINCT r.rolname FROM pg_database d,"
        "aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a JOIN pg_roles r ON r.oid=a.grantee "
        "WHERE d.datname=current_database() AND a.privilege_type='CONNECT' AND r.rolname<>current_user").fetchall()
    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database["name"]), sql.Identifier(database["operator"])))
    connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database["name"])))
    for row in grants:
        connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(sql.Identifier(database["name"]), sql.Identifier(row["rolname"])))


def _require_private_connections(connection):
    if connection.execute("SELECT 1 FROM pg_database d,aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a "
            "WHERE d.datname=current_database() AND a.privilege_type='CONNECT' "
            "AND a.grantee<>(SELECT oid FROM pg_roles WHERE rolname=current_user) LIMIT 1").fetchone():
        raise Conflict("restore database connection quarantine has changed")


def seal_source(source_dsn, archive_dsn, *, expected_database, tenant_id, archive_id, source_id, seal_id):
    """Permanently seal a stopped source; return a complete archived checkpoint.

    All validation and both archive comparisons are inside the source transaction.
    Failed verification rolls back even the quarantine marker. Archive metadata
    is verified again after the marker is staged, before committing the seal.
    """
    for value in (tenant_id, archive_id, source_id, seal_id):
        identifier(value)
    with _operator(source_dsn) as connection:
        _expected_identity(connection, expected_database)
        _offline(connection)
        _lock_governance(connection)
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone():
            raise Conflict("source is already sealed; retain its original checkpoint")
        requirement = connection.execute("SELECT archive_id,source_id FROM sonn_governance.archive_requirements WHERE tenant_id=%s", (tenant_id,)).fetchone()
        if not requirement or (str(requirement["archive_id"]), str(requirement["source_id"])) != (archive_id, source_id):
            raise Conflict("source archive binding differs")
        rows = _rows(connection, tenant_id)
        archived = _archive(archive_dsn, archive_id=archive_id, source_id=source_id, tenant_id=tenant_id)
        if _entries(rows) != _entries(archived):
            raise Conflict("source and archive sets differ; drain before sealing")
        deliveries = connection.execute("SELECT audit_id,archive_id,source_id,sha256,receipt_id FROM sonn_governance.archive_deliveries "
            "WHERE tenant_id=%s ORDER BY audit_id", (tenant_id,)).fetchall()
        if len(deliveries) != len(archived) or any(
                (d["audit_id"], str(d["archive_id"]), str(d["source_id"]), d["sha256"], str(d["receipt_id"])) !=
                (a["audit_id"], archive_id, source_id, a["sha256"], str(a["receipt_id"])) for d, a in zip(deliveries, archived)):
            raise Conflict("source archive acknowledgements are incomplete or differ")
        _, next_value = _sequence(connection)
        checkpoint = {"format": _FORMAT, "version": 1, "seal_id": seal_id, "source_database": expected_database,
            "tenant_id": tenant_id, "archive_id": archive_id, "source_id": source_id,
            "audit_sequence_next": next_value, "records": _entries(rows)}
        # Validate causal retention revisions before making sealing irreversible.
        _retention(archived)
        restore_sharing.verify_objects(connection, tenant_id, archived)
        _create_guard(connection, mode="source_sealed", restore_id=seal_id, document=checkpoint)
        _deny_runtime_connections(connection)
        after = _archive(archive_dsn, archive_id=archive_id, source_id=source_id, tenant_id=tenant_id)
        if _entries(after) != checkpoint["records"]:
            raise Conflict("archive changed during source sealing")
        _no_other_clients(connection)
        return {"checkpoint": checkpoint, "sha256": digest(checkpoint)}


def inspect_seal(source_dsn, *, expected_database):
    """Recover an acknowledged-or-unknown seal response without creating a new seal."""
    with _operator(source_dsn) as connection:
        _expected_identity(connection, expected_database)
        row = connection.execute("SELECT mode,document FROM sonn_restore.guard WHERE singleton").fetchone()
        if not row or row["mode"] != "source_sealed":
            raise Conflict("database has no source seal")
        return {"checkpoint": row["document"], "sha256": digest(row["document"])}


def prepare_target(target_dsn, *, expected_database, restore_id):
    """Mark an empty offline database BEFORE pg_restore installs stale app grants."""
    identifier(restore_id)
    with _operator(target_dsn) as connection:
        _expected_identity(connection, expected_database)
        _offline(connection)
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('sonn_restore','sonn_governance')").fetchone():
            raise Conflict("restore target must be empty and previously unprepared")
        if connection.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' LIMIT 1").fetchone():
            raise Conflict("restore target contains existing application objects")
        _create_guard(connection, mode="restore_target", restore_id=restore_id, document=expected_database)
        _deny_runtime_connections(connection)
        connection.execute("CREATE TABLE sonn_restore.reconciliations(checkpoint_sha256 text PRIMARY KEY,"
            "checkpoint jsonb NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
        connection.execute("CREATE TABLE sonn_restore.retention(tenant_id uuid NOT NULL,project_id uuid NOT NULL,"
            "content_id uuid NOT NULL,checkpoint_sha256 text NOT NULL,state text NOT NULL,resource_revision bigint NOT NULL,"
            "evidence jsonb NOT NULL,PRIMARY KEY(tenant_id,project_id,content_id))")
        restore_sharing.prepare_tables(connection)
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA sonn_restore FROM PUBLIC")
        _no_other_clients(connection)
    return {"restore_id": restore_id, "quarantined": True}


def _checkpoint(value, fingerprint, *, tenant_id, archive_id, source_id):
    fields = {"format", "version", "seal_id", "source_database", "tenant_id", "archive_id", "source_id", "audit_sequence_next", "records"}
    if (type(value) is not dict or set(value) != fields or value["format"] != _FORMAT
            or type(value["version"]) is not int or value["version"] != 1
            or type(fingerprint) is not str or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or digest(value) != fingerprint
            or (value["tenant_id"], value["archive_id"], value["source_id"]) != (tenant_id, archive_id, source_id)
            or type(value["audit_sequence_next"]) is not int or not 1 <= value["audit_sequence_next"] < 2**63):
        raise InvalidRequest("checkpoint differs from independent pins or supported format")
    identifier(value["seal_id"])
    entries = value["records"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_RECORDS:
        raise InvalidRequest("invalid checkpoint record set")
    previous = 0
    for entry in entries:
        if (type(entry) is not dict or set(entry) != {"audit_id", "sha256"}
                or type(entry["audit_id"]) is not int or not previous < entry["audit_id"] < value["audit_sequence_next"]
                or type(entry["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise InvalidRequest("invalid checkpoint record identity")
        previous = entry["audit_id"]


def _retention(rows):
    import json

    objects = {}
    for row in rows:
        event = json.loads(bytes(row["payload"]))
        operation = event["operation"]
        if operation not in _CONTENT:
            continue
        if event["project_id"] is None or event["decision_id"] is None:
            raise Conflict("historical content audit lacks exact object binding")
        key = (event["project_id"], event["decision_id"])
        old = objects.get(key)
        revision = event["resource_revision"]
        if (revision != (old["revision"] + 1 if old else 1)
                or (old is None) != (operation == "publish_content")
                or old and old["state"] == "deleted"
                or operation == "delete_content" and old["state"] == "held"):
            raise Conflict("retention evidence has missing or contradictory revisions")
        state = {"publish_content": "retained", "hold_content": "held", "release_content_hold": "retained", "delete_content": "deleted"}[operation]
        objects[key] = {"revision": revision, "state": state, "event": event}
    return objects


def reconcile(target_dsn, archive_dsn, *, expected_database, restore_id, checkpoint,
              checkpoint_sha256, tenant_id, archive_id, source_id):
    """Reconcile exact archived retention evidence; keep all service access closed.

    An archived hold has no recoverable reason. Its evidence is retained in the
    restore-only table and all content mutations remain blocked. Existing hold
    provenance is preserved; no invented historical hold or release is recorded.
    """
    for value in (restore_id, tenant_id, archive_id, source_id):
        identifier(value)
    _checkpoint(checkpoint, checkpoint_sha256, tenant_id=tenant_id, archive_id=archive_id, source_id=source_id)
    with _operator(target_dsn) as connection:
        _expected_identity(connection, expected_database)
        if expected_database == checkpoint["source_database"]:
            raise Conflict("restore target must differ from sealed source")
        _offline(connection)
        _require_private_connections(connection)
        guard = connection.execute("SELECT * FROM sonn_restore.guard WHERE singleton FOR UPDATE").fetchone()
        if not guard or guard["mode"] != "restore_target" or str(guard["restore_id"]) != restore_id or guard["document"] != expected_database:
            raise Conflict("restore target quarantine identity differs")
        _lock_governance(connection)
        rows = _archive(archive_dsn, archive_id=archive_id, source_id=source_id, tenant_id=tenant_id)
        if _entries(rows) != checkpoint["records"]:
            raise Conflict("archive is missing, changed, or later than the sealed checkpoint")
        sequence_name, next_value = _sequence(connection)
        if next_value > checkpoint["audit_sequence_next"]:
            raise Conflict("restored audit sequence is newer than the sealed checkpoint")
        existing = connection.execute("SELECT result FROM sonn_restore.reconciliations WHERE checkpoint_sha256=%s", (checkpoint_sha256,)).fetchone()
        if existing:
            if next_value != checkpoint["audit_sequence_next"]:
                raise Conflict("reconciled audit sequence has changed")
            return existing["result"]
        if connection.execute("SELECT 1 FROM sonn_restore.reconciliations LIMIT 1").fetchone():
            raise Conflict("restore target already has a different reconciliation")
        restored = _rows(connection, tenant_id)
        if _entries(restored) != checkpoint["records"][:len(restored)]:
            raise Conflict("restored outbox is not an exact prefix of the sealed source")
        objects = _retention(rows)
        baseline = _retention(restored)
        local = connection.execute("SELECT * FROM sonn_governance.content_objects WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchall()
        if {(str(row["project_id"]), str(row["content_id"])) for row in local} != set(baseline):
            raise Conflict("restored content identities differ from the restored audit prefix")
        for row in local:
            key = (str(row["project_id"]), str(row["content_id"]))
            proof, old = objects.get(key), baseline[key]
            if (not proof or row["revision"] != old["revision"] or row["revision"] > proof["revision"]
                    or row["held"] != (old["state"] == "held")
                    or (row["deleted_at"] is not None) != (old["state"] == "deleted")):
                raise Conflict("restored content lacks matching immutable retention evidence")
        result = {"quarantined": True, "restore_id": restore_id, "checkpoint_sha256": checkpoint_sha256,
            "objects": len(objects), "deleted": 0, "held_with_reason_unresolved": 0, "absent_objects": 0,
            "audit_sequence_next": checkpoint["audit_sequence_next"]}
        result.update(restore_sharing.reconcile_objects(connection, tenant_id, rows, restored, checkpoint_sha256))
        present = {(str(row["project_id"]), str(row["content_id"])) for row in local}
        for (project, content), proof in sorted(objects.items()):
            event = proof["event"]
            connection.execute("INSERT INTO sonn_restore.retention VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (tenant_id, project, content, checkpoint_sha256, proof["state"], proof["revision"], Jsonb(event)))
            if (project, content) not in present:
                result["absent_objects"] += 1
            if proof["state"] == "held":
                result["held_with_reason_unresolved"] += 1
            if proof["state"] == "deleted":
                result["deleted"] += 1
                connection.execute("UPDATE sonn_governance.content_objects SET ciphertext=NULL,content_sha256=NULL,"
                    "size_bytes=NULL,media_type=NULL,held=false,hold_actor=NULL,hold_reason=NULL,deleted_at=%s,revision=%s "
                    "WHERE tenant_id=%s AND project_id=%s AND content_id=%s",
                    (event["occurred_at"], proof["revision"], tenant_id, project, content))
        # ALTER SEQUENCE RESTART is transactional, unlike setval(). An interrupted
        # apply rolls back sequence advancement together with all tombstones.
        connection.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(sequence_name, sql.Literal(checkpoint["audit_sequence_next"])))
        connection.execute("CREATE FUNCTION sonn_restore.reject_content_change() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'restored content remains quarantined'; END $$")
        connection.execute("CREATE TRIGGER restore_content_quarantine BEFORE INSERT OR UPDATE OR DELETE ON "
            "sonn_governance.content_objects FOR EACH ROW EXECUTE FUNCTION sonn_restore.reject_content_change()")
        restore_sharing.protect_objects(connection)
        connection.execute("INSERT INTO sonn_restore.reconciliations(checkpoint_sha256,checkpoint,result) VALUES(%s,%s,%s)",
                           (checkpoint_sha256, Jsonb(checkpoint), Jsonb(result)))
        # A destination custodian changing the archive while applying cannot turn
        # a stale preflight into a committed reconciliation.
        after = _archive(archive_dsn, archive_id=archive_id, source_id=source_id, tenant_id=tenant_id)
        if _entries(after) != checkpoint["records"]:
            raise Conflict("archive changed during reconciliation")
        _no_other_clients(connection)
        return result
