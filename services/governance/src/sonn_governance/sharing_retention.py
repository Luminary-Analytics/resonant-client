"""Explicit bilateral retention of encrypted sharing content, without disclosure."""

from psycopg.types.json import Jsonb

from .managed_collaboration import ManagedCollaboration
from .models import AccessDenied, Conflict, InvalidRequest, digest, identifier

_RESOURCES = {"terms": ("sharing_grants", "grant_id", "terms_sha256"),
              "message": ("sharing_messages", "message_id", "body_sha256")}
_OPERATIONS = {"hold_sharing_content": "hold_sharing_{kind}",
               "release_sharing_content_hold": "release_sharing_{kind}_hold",
               "delete_sharing_content": "delete_sharing_{kind}"}


class SharingRetention:
    """Current retention_admin on both projects precedes metadata and replay.

    Deletion removes the active encrypted payload and its direct hash. Immutable
    prior receipts, disclosure accounting, accepted work and backups remain.
    No key ring is needed: retention authority never implies content access.
    """

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _target(tenant_id, kind, resource_id):
        identifier(tenant_id)
        identifier(resource_id)
        if type(kind) is not str or kind not in _RESOURCES:
            raise InvalidRequest("invalid sharing content kind")
        return _RESOURCES[kind]

    def _load(self, connection, principal, tenant_id, kind, resource_id):
        table, key, _ = self._target(tenant_id, kind, resource_id)
        self.store._identity(connection, principal)
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        # Match sharing's tenant -> graph lock order before either project.
        tenant = connection.execute("SELECT active FROM sonn_governance.tenants WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()
        if not tenant or not tenant["active"]:
            raise AccessDenied("sharing resource is unavailable")
        ManagedCollaboration._lock(connection, tenant_id)
        row = connection.execute(f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s AND {key}=%s FOR UPDATE", (tenant_id, resource_id)).fetchone()
        grant = row if kind == "terms" else connection.execute(
            "SELECT * FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND grant_id=%s",
            (tenant_id, row["grant_id"])).fetchone() if row else None
        if not grant:
            raise AccessDenied("sharing resource is unavailable")
        projects = [side["project_id"] for side in grant["captured"]]
        for project in sorted(set(projects)):
            self.store._authorize(connection, principal, tenant_id, project, "retention_admin", edit_membership=True)
        return row, projects

    @staticmethod
    def _metadata(row, kind, resource_id, projects):
        return {"kind": kind, "resource_id": resource_id, "grant_id": str(row["grant_id"]),
                "project_ids": list(dict.fromkeys(projects)), "revision": row["retention_revision"],
                "held": row["held"], "hold_reason": row["hold_reason"],
                "created_at": row["created_at"].isoformat(),
                "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None}

    def inspect(self, principal, tenant_id, kind, resource_id):
        """Return only the exact resource's safe retention state, never bytes."""
        self._target(tenant_id, kind, resource_id)
        with self.store._connection() as connection:
            row, projects = self._load(connection, principal, tenant_id, kind, resource_id)
            self.store._audit(connection, principal, tenant_id, operation=f"inspect_sharing_{kind}_retention",
                project_id=projects[0], decision_id=resource_id, revision=row["retention_revision"])
            self.store._identity(connection, principal)
            return self._metadata(row, kind, resource_id, projects)

    def retain(self, principal, tenant_id, kind, resource_id, *, command_id, expected_revision, operation, reason=None):
        """Hold, release or delete one resource in an audited revision transaction."""
        table, key, sha = self._target(tenant_id, kind, resource_id)
        identifier(command_id)
        if (type(operation) is not str or operation not in _OPERATIONS
                or type(expected_revision) is not int or expected_revision < 1
                or operation == "hold_sharing_content" and (type(reason) is not str or reason not in {"owner_request", "security_review", "legal_review"})
                or operation != "hold_sharing_content" and reason is not None):
            raise InvalidRequest("invalid sharing retention command")
        with self.store._connection() as connection:
            row, projects = self._load(connection, principal, tenant_id, kind, resource_id)
            semantics = {"tenant_id": tenant_id, "kind": kind, "resource_id": resource_id, "project_ids": projects,
                         "operation": operation, "expected_revision": expected_revision, "reason": reason}
            old = connection.execute("SELECT semantics_sha256,result FROM sonn_governance.sharing_retention_receipts "
                "WHERE tenant_id=%s AND actor_id=%s AND command_id=%s", (tenant_id, principal.actor_id, command_id)).fetchone()
            if old:
                if old["semantics_sha256"] != digest(semantics):
                    raise Conflict("sharing retention command identity changed")
                self.store._identity(connection, principal)
                return old["result"]
            if row["retention_revision"] != expected_revision or row["deleted_at"]:
                raise Conflict("sharing retention revision changed")
            if operation == "delete_sharing_content" and row["held"]:
                raise Conflict("sharing content is retained under an explicit hold")
            target = (tenant_id, resource_id)
            if operation == "hold_sharing_content":
                connection.execute(f"UPDATE sonn_governance.{table} SET held=true,hold_actor=%s,hold_reason=%s,"
                    f"retention_revision=retention_revision+1 WHERE tenant_id=%s AND {key}=%s", (principal.actor_id, reason, *target))
            elif operation == "release_sharing_content_hold":
                connection.execute(f"UPDATE sonn_governance.{table} SET held=false,hold_actor=NULL,hold_reason=NULL,"
                    f"retention_revision=retention_revision+1 WHERE tenant_id=%s AND {key}=%s", target)
            else:
                connection.execute(f"UPDATE sonn_governance.{table} SET ciphertext=NULL,key_id=NULL,nonce=NULL,{sha}=NULL,"
                    f"deleted_at=clock_timestamp(),retention_revision=retention_revision+1 WHERE tenant_id=%s AND {key}=%s", target)
            row = connection.execute(f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s AND {key}=%s", target).fetchone()
            result = self._metadata(row, kind, resource_id, projects)
            audit = self.store._audit(connection, principal, tenant_id, operation=_OPERATIONS[operation].format(kind=kind),
                project_id=projects[0], decision_id=resource_id, revision=result["revision"], semantics_sha256=digest(semantics))
            connection.execute("INSERT INTO sonn_governance.sharing_retention_receipts "
                "(tenant_id,actor_id,command_id,semantics_sha256,result,audit_id) VALUES(%s,%s,%s,%s,%s,%s)",
                (tenant_id, principal.actor_id, command_id, digest(semantics), Jsonb(result), audit))
            self.store._identity(connection, principal)
            return result
