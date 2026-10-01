"""Independent review of exact sensitive self-grants; no implicit role expansion."""

import hashlib
import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from .models import AccessDenied, Conflict, InvalidRequest, SENSITIVE_PERMISSIONS, digest, identifier


def sensitive_additions(connection, tenant_id, actor_id, payload):
    """Effective additions include broadening a project permission to the tenant."""
    if payload["actor_id"] != actor_id:
        return False
    tenant = {row["permission"] for row in connection.execute(
        "SELECT permission FROM sonn_governance.tenant_grants WHERE tenant_id=%s AND actor_id=%s",
        (tenant_id, actor_id),
    ).fetchall()}
    if (set(payload["tenant_permissions"]) & SENSITIVE_PERMISSIONS) - tenant:
        return True
    current = {(str(row["project_id"]), row["permission"]) for row in connection.execute(
        "SELECT project_id,permission FROM sonn_governance.project_grants WHERE tenant_id=%s AND actor_id=%s",
        (tenant_id, actor_id),
    ).fetchall()}
    return any(permission not in tenant and (project, permission) not in current
               for project, permissions in payload["project_permissions"].items()
               for permission in set(permissions) & SENSITIVE_PERMISSIONS)


def request_grant(connection, tenant_id, principal, payload, revision):
    request_id = str(uuid4())
    fingerprint = digest({"requester": principal.actor_id, "payload": payload, "authorization_revision": revision})
    connection.execute("INSERT INTO sonn_governance.grant_requests VALUES(%s,%s,%s,%s,%s,%s,%s,'pending',NULL,NULL)",
                       (tenant_id, request_id, principal.actor_id, payload["actor_id"], revision, Jsonb(payload), fingerprint))
    return {"state": "pending", "grant_request_id": request_id, "sha256": fingerprint}


class GrantApprovals:
    """Review commands require a distinct current member and exact desired state."""

    def __init__(self, store):
        self.store = store

    def inspect(self, principal, tenant_id, request_id):
        identifier(tenant_id)
        identifier(request_id)
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, None, "membership_admin")
            request = connection.execute(
                "SELECT * FROM sonn_governance.grant_requests WHERE tenant_id=%s AND request_id=%s",
                (tenant_id, request_id),
            ).fetchone()
            if not request:
                raise AccessDenied("resource is unavailable")
            self.store._audit(connection, principal, tenant_id, operation="query_grants")
            self.store._identity(connection, principal)
            return {"request_id": request_id, "requester": request["requester"], "target_actor": request["target_actor"],
                    "authorization_revision": request["authorization_revision"], "payload": request["payload"],
                    "sha256": request["sha256"], "state": request["state"], "decided_by": request["decided_by"]}

    def command(self, principal, command):
        semantics = command.semantics()
        if command.operation not in {"decide_grant", "revoke_grant"} or command.project_id is not None:
            raise InvalidRequest("unsupported grant command")
        payload = semantics["payload"]
        keys = {"request_id", "sha256", "approve"} if command.operation == "decide_grant" else {"request_id", "sha256"}
        if set(payload) != keys or ("approve" in payload and type(payload["approve"]) is not bool):
            raise InvalidRequest("invalid grant decision")
        identifier(payload["request_id"])
        if not isinstance(payload["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", payload["sha256"]) is None:
            raise InvalidRequest("invalid grant digest")
        with self.store._connection() as connection:
            tenant = self.store._authorize(connection, principal, command.tenant_id, None,
                                           "membership_admin", edit_membership=True)
            if command.operation == "decide_grant":
                self.store._authorize(connection, principal, command.tenant_id, None, "membership_approve")
            request = connection.execute(
                "SELECT * FROM sonn_governance.grant_requests WHERE tenant_id=%s AND request_id=%s FOR UPDATE",
                (command.tenant_id, payload["request_id"]),
            ).fetchone()
            if not request:
                raise AccessDenied("resource is unavailable")
            if (command.operation == "decide_grant"
                    and principal.actor_id in {request["requester"], request["target_actor"]}):
                raise AccessDenied("independent approver is required")
            key = f"{command.tenant_id}:{principal.actor_id}:{command.command_id}"
            number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (number,))
            previous = connection.execute(
                "SELECT semantics_sha256,result FROM sonn_governance.command_receipts WHERE tenant_id=%s "
                "AND actor_id=%s AND namespace='governance.v1' AND command_id=%s",
                (command.tenant_id, principal.actor_id, command.command_id),
            ).fetchone()
            if previous:
                if previous["semantics_sha256"] != digest(semantics):
                    raise Conflict("command identity has different semantics")
                self.store._identity(connection, principal)
                return previous["result"]
            if request["sha256"] != payload["sha256"] or request["state"] != "pending":
                raise Conflict("grant request changed")
            revision = tenant["authorization_revision"]
            if command.expected_revision != revision:
                raise Conflict("authorization revision changed")
            state = "revoked" if command.operation == "revoke_grant" else ("approved" if payload["approve"] else "rejected")
            if state == "approved":
                if request["authorization_revision"] != revision:
                    raise Conflict("grant request is stale")
                # The original requestor must still be active and able to manage
                # membership; approval never revives revoked requester authority.
                member = connection.execute(
                    "SELECT active,provisioned_active FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s",
                    (command.tenant_id, request["requester"]),
                ).fetchone()
                permitted = connection.execute(
                    "SELECT 1 FROM sonn_governance.tenant_grants WHERE tenant_id=%s AND actor_id=%s AND permission='membership_admin'",
                    (command.tenant_id, request["requester"]),
                ).fetchone()
                if not member or not member["active"] or not member["provisioned_active"] or not permitted:
                    raise AccessDenied("resource is unavailable")
                for project in request["payload"]["project_permissions"]:
                    if not connection.execute(
                        "SELECT 1 FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s AND active",
                        (command.tenant_id, project),
                    ).fetchone():
                        raise AccessDenied("resource is unavailable")
                revision += 1
                self.store._set_membership(connection, command.tenant_id, request["payload"], revision)
            operation = {"approved": "approve_grant", "rejected": "reject_grant", "revoked": "revoke_grant"}[state]
            audit_id = self.store._audit(connection, principal, command.tenant_id, operation=operation,
                                         revision=revision, semantics_sha256=digest(semantics))
            connection.execute(
                "UPDATE sonn_governance.grant_requests SET state=%s,decided_by=%s,decision_audit_id=%s "
                "WHERE tenant_id=%s AND request_id=%s",
                (state, principal.actor_id, audit_id, command.tenant_id, payload["request_id"]),
            )
            result = {"state": state, "grant_request_id": payload["request_id"], "sha256": request["sha256"],
                      "revision": revision, "audit_id": audit_id}
            connection.execute("INSERT INTO sonn_governance.command_receipts VALUES(%s,%s,'governance.v1',%s,%s,%s,%s)",
                               (command.tenant_id, principal.actor_id, command.command_id, digest(semantics), Jsonb(result), audit_id))
            self.store._identity(connection, principal)
            return result
