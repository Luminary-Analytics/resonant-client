"""Independently credentialed append-only PostgreSQL audit archive and relay.

The runtime never receives archive/relay credentials. Real deployment must put
the destination and its backups under separate custody: one local fixture
cluster proves role boundaries, not protection from the machine/database owner.
No content blobs or transcript bytes are part of this metadata-only archive.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .identity import _unique_object
from .models import Conflict, InvalidRequest, UnsupportedVersion, identifier

_FIELDS = {"version", "tenant_id", "audit_id", "actor_id", "operation", "project_id", "resource_revision",
           "decision_id", "semantics_sha256", "occurred_at"}


def _safe_role(connection, *, schema, tables):
    role = connection.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
    if role["rolsuper"] or role["rolbypassrls"]:
        raise InvalidRequest("archive role has unsafe privileges")
    if connection.execute("SELECT has_schema_privilege(current_user,%s,'CREATE') AS dangerous", (schema,)).fetchone()["dangerous"]:
        raise InvalidRequest("archive role has unsafe privileges")
    for table in tables:
        dangerous = connection.execute("SELECT has_table_privilege(current_user,%s,'UPDATE,DELETE,TRUNCATE,TRIGGER') AS mutation,"
            "has_any_column_privilege(current_user,%s,'UPDATE') AS column_update,"
            "EXISTS(SELECT 1 FROM pg_class WHERE oid=%s::regclass AND pg_has_role(current_user,relowner,'MEMBER')) AS owner",
            (f"{schema}.{table}", f"{schema}.{table}", f"{schema}.{table}")).fetchone()
        if any(dangerous.values()):
            raise InvalidRequest("archive role has unsafe privileges")


def initialize_archive(owner_dsn: str, *, writer_role: str, archive_id: str) -> None:
    """Operator-only destination setup; no roles, passwords or databases created."""
    identifier(archive_id)
    with psycopg.connect(owner_dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(734025008)")
        role = connection.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (writer_role,)).fetchone()
        if not role or role["rolsuper"] or role["rolbypassrls"]:
            raise InvalidRequest("archive writer role must be restricted")
        exists = connection.execute("SELECT to_regclass('sonn_archive.configuration') AS relation").fetchone()["relation"]
        if exists:
            row = connection.execute("SELECT version,archive_id FROM sonn_archive.configuration WHERE singleton").fetchone()
            if not row or row["version"] != 1 or str(row["archive_id"]) != archive_id:
                raise UnsupportedVersion("archive identity or schema differs")
        else:
            connection.execute("CREATE SCHEMA sonn_archive")
            connection.execute("REVOKE ALL ON SCHEMA sonn_archive FROM PUBLIC")
            connection.execute("CREATE TABLE sonn_archive.configuration(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),"
                               "version integer NOT NULL CHECK(version=1),archive_id uuid NOT NULL)")
            connection.execute("INSERT INTO sonn_archive.configuration VALUES(true,1,%s)", (archive_id,))
            connection.execute("CREATE TABLE sonn_archive.records(source_id uuid NOT NULL,tenant_id uuid NOT NULL,audit_id bigint NOT NULL,"
                "sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 16384),"
                "receipt_id uuid NOT NULL UNIQUE,received_at timestamptz NOT NULL DEFAULT clock_timestamp(),"
                "PRIMARY KEY(source_id,tenant_id,audit_id))")
        name = sql.Identifier(writer_role)
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA sonn_archive TO {}").format(name))
        connection.execute(sql.SQL("GRANT SELECT ON sonn_archive.configuration,sonn_archive.records TO {}").format(name))
        connection.execute(sql.SQL("GRANT INSERT ON sonn_archive.records TO {}").format(name))


def configure_relay(owner_dsn: str, *, relay_role: str, tenant_id: str, archive_id: str, source_id: str, max_delay_seconds=300) -> None:
    """Operator-only source grants and explicit fail-closed archival requirement.

    Configure max_delay_seconds=None to drain before enabling a requirement. New
    leases/requests/content disclosure close after the configured lag; Stop,
    revocation, inspection and old uncertainty observations remain possible.
    """
    identifier(tenant_id)
    identifier(archive_id)
    identifier(source_id)
    if max_delay_seconds is not None and (type(max_delay_seconds) is not int or not 30 <= max_delay_seconds <= 86400):
        raise InvalidRequest("invalid archival delay")
    with psycopg.connect(owner_dsn, row_factory=dict_row, connect_timeout=5) as connection:
        role = connection.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (relay_role,)).fetchone()
        if not role or role["rolsuper"] or role["rolbypassrls"]:
            raise InvalidRequest("relay role must be restricted")
        name = sql.Identifier(relay_role)
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA sonn_governance TO {}").format(name))
        connection.execute(sql.SQL("GRANT SELECT ON sonn_governance.audit_outbox,sonn_governance.archive_deliveries,sonn_governance.archive_requirements TO {}").format(name))
        connection.execute(sql.SQL("GRANT INSERT ON sonn_governance.archive_deliveries TO {}").format(name))
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        old = connection.execute("SELECT archive_id,source_id FROM sonn_governance.archive_requirements WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()
        if old and (old["archive_id"] is not None or old["source_id"] is not None) and (str(old["archive_id"]), str(old["source_id"])) != (archive_id, source_id):
            raise Conflict("archive identity changes require an explicit custody migration")
        connection.execute("INSERT INTO sonn_governance.archive_requirements(tenant_id,max_delay_seconds,archive_id,source_id) VALUES(%s,%s,%s,%s) "
                           "ON CONFLICT(tenant_id) DO UPDATE SET max_delay_seconds=excluded.max_delay_seconds,archive_id=excluded.archive_id,source_id=excluded.source_id",
                           (tenant_id, max_delay_seconds, archive_id, source_id))


def _validate_payload(tenant_id, audit_id, payload, fingerprint):
    identifier(tenant_id)
    if (type(audit_id) is not int or not 1 <= audit_id <= 2**63-1 or type(payload) is not bytes
            or not 1 <= len(payload) <= 16384 or hashlib.sha256(payload).hexdigest() != fingerprint):
        raise InvalidRequest("invalid bounded audit envelope")
    value = json.loads(payload, object_pairs_hook=_unique_object)
    if type(value) is not dict or set(value) != _FIELDS or type(value["version"]) is not int or value["version"] != 1:
        raise InvalidRequest("unsupported audit envelope")
    if value["tenant_id"] != tenant_id or type(value["audit_id"]) is not int or value["audit_id"] != audit_id:
        raise InvalidRequest("audit identity differs")
    for name in ("project_id", "decision_id"):
        if value[name] is not None:
            identifier(value[name])
    if (type(value["actor_id"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["actor_id"])
            or type(value["operation"]) is not str or not re.fullmatch(r"[a-z_]{1,64}", value["operation"])
            or type(value["occurred_at"]) is not str or len(value["occurred_at"]) > 64
            or (value["resource_revision"] is not None and (type(value["resource_revision"]) is not int or value["resource_revision"] < 0))
            or (value["semantics_sha256"] is not None and (type(value["semantics_sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["semantics_sha256"])))):
        raise InvalidRequest("invalid audit metadata")
    try:
        if datetime.fromisoformat(value["occurred_at"]).tzinfo is None:
            raise ValueError
    except ValueError:
        raise InvalidRequest("invalid audit timestamp") from None


class AuditArchive:
    """Append exact records under a separate restricted destination credential."""

    def __init__(self, dsn: str, *, archive_id: str, source_id: str):
        self._dsn = dsn
        self.archive_id, self.source_id = identifier(archive_id), identifier(source_id)
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            _safe_role(connection, schema="sonn_archive", tables=("configuration", "records"))
            if connection.execute("SELECT has_any_column_privilege(current_user,'sonn_archive.configuration','INSERT') AS danger").fetchone()["danger"]:
                raise InvalidRequest("archive configuration must be operator-owned")
            self._identity(connection)

    def _identity(self, connection):
        row = connection.execute("SELECT version,archive_id FROM sonn_archive.configuration WHERE singleton").fetchone()
        if not row or row["version"] != 1 or str(row["archive_id"]) != self.archive_id:
            raise UnsupportedVersion("archive identity or schema differs")

    def append(self, tenant_id: str, audit_id: int, payload: bytes, fingerprint: str) -> dict:
        """An exact replay returns its receipt; changed history can never replace it."""
        _validate_payload(tenant_id, audit_id, payload, fingerprint)
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("SET LOCAL synchronous_commit=on")
            self._identity(connection)
            receipt_id = str(uuid4())
            connection.execute("INSERT INTO sonn_archive.records(source_id,tenant_id,audit_id,sha256,payload,receipt_id) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id,tenant_id,audit_id) DO NOTHING", (self.source_id, tenant_id, audit_id, fingerprint, payload, receipt_id))
            row = connection.execute("SELECT sha256,payload,receipt_id FROM sonn_archive.records WHERE source_id=%s AND tenant_id=%s AND audit_id=%s",
                                     (self.source_id, tenant_id, audit_id)).fetchone()
            if row["sha256"] != fingerprint or bytes(row["payload"]) != payload:
                raise Conflict("archive identity has different retained bytes")
            return {"archive_id": self.archive_id, "receipt_id": str(row["receipt_id"]), "sha256": fingerprint}


class AuditRelay:
    """Bounded explicit work; a durable outbox survives crashes on either side."""

    def __init__(self, source_dsn: str, archive: AuditArchive):
        self._dsn, self.archive = source_dsn, archive
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            _safe_role(connection, schema="sonn_governance", tables=("audit_outbox", "archive_deliveries", "audit", "policies"))
            tables = connection.execute("SELECT c.oid,n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='sonn_governance' AND c.relkind IN ('r','p','v','m','f')").fetchall()
            for table in tables:
                unsafe = connection.execute("SELECT has_table_privilege(current_user,%s,'UPDATE,DELETE,TRUNCATE,TRIGGER') OR "
                    "has_any_column_privilege(current_user,%s,'UPDATE') AS mutate, has_any_column_privilege(current_user,%s,'INSERT') AS insert",
                    (table["oid"], table["oid"], table["oid"])).fetchone()
                if unsafe["mutate"] or unsafe["insert"] and table["relname"] != "archive_deliveries":
                    raise InvalidRequest("relay must not share application authority")

    def _binding(self, connection, tenant_id):
        requirement = connection.execute("SELECT archive_id,source_id FROM sonn_governance.archive_requirements WHERE tenant_id=%s", (tenant_id,)).fetchone()
        if not requirement or (str(requirement["archive_id"]), str(requirement["source_id"])) != (self.archive.archive_id, self.archive.source_id):
            raise InvalidRequest("configured archive binding differs")
        inconsistent = connection.execute("SELECT 1 FROM sonn_governance.archive_deliveries d JOIN sonn_governance.audit_outbox o USING(tenant_id,audit_id) "
            "WHERE d.tenant_id=%s AND (d.archive_id<>%s::uuid OR d.source_id IS DISTINCT FROM %s::uuid OR d.sha256<>o.sha256) LIMIT 1",
            (tenant_id, self.archive.archive_id, self.archive.source_id)).fetchone()
        if inconsistent:
            raise Conflict("historical archive delivery requires custodian reconciliation")

    def drain(self, tenant_id: str, *, limit=100) -> dict:
        """Deliver at most one bounded page; a lost acknowledgement is safe to retry."""
        identifier(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequest("invalid relay batch")
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
            self._binding(connection, tenant_id)
            rows = connection.execute("SELECT o.* FROM sonn_governance.audit_outbox o LEFT JOIN sonn_governance.archive_deliveries d USING(tenant_id,audit_id) "
                "WHERE o.tenant_id=%s AND d.audit_id IS NULL ORDER BY o.audit_id LIMIT %s", (tenant_id, limit)).fetchall()
        delivered = 0
        for row in rows:
            receipt = self.archive.append(tenant_id, row["audit_id"], bytes(row["payload"]), row["sha256"])
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
                connection.execute("SET LOCAL statement_timeout='10s'")
                connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
                self._binding(connection, tenant_id)
                connection.execute("INSERT INTO sonn_governance.archive_deliveries(tenant_id,audit_id,archive_id,receipt_id,sha256,source_id) VALUES(%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(tenant_id,audit_id) DO NOTHING", (tenant_id, row["audit_id"], receipt["archive_id"], receipt["receipt_id"], receipt["sha256"], self.archive.source_id))
                saved = connection.execute("SELECT archive_id,receipt_id,sha256,source_id FROM sonn_governance.archive_deliveries WHERE tenant_id=%s AND audit_id=%s", (tenant_id, row["audit_id"])).fetchone()
                if (str(saved["archive_id"]) != receipt["archive_id"] or str(saved["source_id"]) != self.archive.source_id
                        or str(saved["receipt_id"]) != receipt["receipt_id"] or saved["sha256"] != receipt["sha256"]):
                    raise Conflict("archive delivery differs from retained receipt")
                delivered += 1
        return {"delivered": delivered, "batch_limit": limit, "archive_id": self.archive.archive_id}
