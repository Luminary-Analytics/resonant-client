"""Archived sharing-payload retention inside a permanently quarantined restore.

The canonical audit project is the grant's origin. Bilateral authorization is
enforced at the original retention command; this reducer grants no authority.
Old creation records without exact object bindings cannot be reconstructed.
"""

from __future__ import annotations

import json

from psycopg import sql
from psycopg.types.json import Jsonb

from .models import Conflict

_EVENTS = {
    "sharing_offer": ("terms", "retained", True),
    "sharing_send": ("message", "retained", True),
    **{operation: (kind, state, False)
       for kind in ("terms", "message")
       for operation, state in ((f"hold_sharing_{kind}", "held"),
                                (f"release_sharing_{kind}_hold", "retained"),
                                (f"delete_sharing_{kind}", "deleted"))},
}
_TABLES = {"terms": ("sharing_grants", "grant_id", "terms_sha256"),
           "message": ("sharing_messages", "message_id", "body_sha256")}


def retention_records(rows):
    """Reduce exact typed object/revision evidence, never a semantic hash guess."""
    objects = {}
    for row in rows:
        event = json.loads(bytes(row["payload"]))
        contract = _EVENTS.get(event["operation"])
        if contract is None:
            continue
        kind, state, creation = contract
        if event["project_id"] is None or event["decision_id"] is None:
            raise Conflict("historical sharing audit lacks exact object binding")
        key = (kind, event["decision_id"])
        old = objects.get(key)
        revision = event["resource_revision"]
        if (type(revision) is not int or revision != (old["revision"] + 1 if old else 1)
                or (old is None) != creation
                or old and (old["state"] == "deleted" or old["project_id"] != event["project_id"])
                or state == "deleted" and old["state"] == "held"):
            raise Conflict("sharing retention evidence has missing or contradictory revisions")
        objects[key] = {"project_id": event["project_id"], "revision": revision,
                        "state": state, "event": event}
    return objects


def verify_objects(connection, tenant_id, records):
    """Require the restored/source payload rows to match their audit prefix."""
    objects = retention_records(records)
    version = connection.execute("SELECT version FROM sonn_governance.schema_version WHERE singleton").fetchone()["version"]
    existing = {}
    for kind, (table, identity, fingerprint) in _TABLES.items():
        rows = connection.execute(sql.SQL(
            "SELECT payload.*,origin.project_id AS origin_project_id FROM sonn_governance.{} payload "
            + ("JOIN sonn_governance.sharing_grants grant_row USING(tenant_id,grant_id) " if kind == "message" else "")
            + "JOIN sonn_governance.managed_runs origin ON origin.tenant_id=payload.tenant_id "
            + ("AND origin.binding_id=grant_row.origin_binding " if kind == "message" else "AND origin.binding_id=payload.origin_binding ")
            + "WHERE payload.tenant_id=%s").format(sql.Identifier(table)), (tenant_id,)).fetchall()
        if rows and version < 15:
            raise Conflict("sharing retention recovery requires schema 15 and bound creation history")
        for row in rows:
            key = (kind, str(row[identity]))
            proof = objects.get(key)
            if (not proof or str(row["origin_project_id"]) != proof["project_id"]
                    or row["retention_revision"] != proof["revision"]
                    or row["held"] != (proof["state"] == "held")
                    or (row["deleted_at"] is not None) != (proof["state"] == "deleted")
                    or (proof["state"] == "deleted" and any(row[field] is not None for field in
                        ("ciphertext", "key_id", "nonce", fingerprint)))):
                raise Conflict("sharing payload differs from immutable retention evidence")
            existing[key] = row
    if set(existing) != set(objects):
        raise Conflict("sharing payload identities differ from their audit prefix")
    return existing


def prepare_tables(connection):
    connection.execute("CREATE TABLE sonn_restore.sharing_retention(tenant_id uuid NOT NULL,"
        "kind text NOT NULL CHECK(kind IN ('terms','message')),project_id uuid NOT NULL,resource_id uuid NOT NULL,"
        "checkpoint_sha256 text NOT NULL,state text NOT NULL,resource_revision bigint NOT NULL,evidence jsonb NOT NULL,"
        "PRIMARY KEY(tenant_id,kind,resource_id))")


def reconcile_objects(connection, tenant_id, records, restored_records, checkpoint_sha256):
    """Remove verified deleted payloads, retain unknown holds, never reopen."""
    objects = retention_records(records)
    existing = verify_objects(connection, tenant_id, restored_records)
    baseline = retention_records(restored_records)
    for key, old in baseline.items():
        proof = objects.get(key)
        if not proof or proof["project_id"] != old["project_id"] or proof["revision"] < old["revision"]:
            raise Conflict("sharing retention tail contradicts restored history")
    result = {"sharing_objects": len(objects), "sharing_deleted": 0,
              "sharing_held_with_reason_unresolved": 0, "sharing_absent_objects": 0}
    for (kind, identity), proof in sorted(objects.items()):
        event = proof["event"]
        connection.execute("INSERT INTO sonn_restore.sharing_retention VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, kind, proof["project_id"], identity, checkpoint_sha256, proof["state"], proof["revision"], Jsonb(event)))
        if (kind, identity) not in existing:
            result["sharing_absent_objects"] += 1
        if proof["state"] == "held":
            result["sharing_held_with_reason_unresolved"] += 1
        if proof["state"] != "deleted":
            continue
        result["sharing_deleted"] += 1
        table, column, fingerprint = _TABLES[kind]
        connection.execute(sql.SQL("UPDATE sonn_governance.{} SET ciphertext=NULL,key_id=NULL,nonce=NULL,{}=NULL,"
            "held=false,hold_actor=NULL,hold_reason=NULL,deleted_at=%s,retention_revision=%s "
            + "WHERE tenant_id=%s AND {}=%s").format(sql.Identifier(table), sql.Identifier(fingerprint), sql.Identifier(column)),
            (event["occurred_at"], proof["revision"], tenant_id, identity))
    return result


def protect_objects(connection):
    """The common reject function is installed by the content restore first."""
    for table, _, _ in _TABLES.values():
        connection.execute(sql.SQL("CREATE TRIGGER restore_sharing_quarantine BEFORE INSERT OR UPDATE OR DELETE ON "
            "sonn_governance.{} FOR EACH ROW EXECUTE FUNCTION sonn_restore.reject_content_change()").format(sql.Identifier(table)))
