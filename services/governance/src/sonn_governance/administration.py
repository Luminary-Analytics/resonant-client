"""Bounded human administration views over current server-side grant sources."""

import re

from .models import AccessDenied, InvalidRequest, digest, identifier


class Administration:
    """Inspect an explicitly addressed member; no tenant or identity discovery."""

    def __init__(self, store):
        self.store = store

    def membership(self, principal, tenant_id, actor_id, *, after=None, limit=50):
        identifier(tenant_id)
        if (type(actor_id) is not str or not re.fullmatch(r"[a-f0-9]{64}", actor_id)
                or type(limit) is not int or not 1 <= limit <= 100):
            raise InvalidRequest("invalid membership page")
        if after is not None:
            identifier(after)
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, None, "membership_admin")
            member = connection.execute("SELECT active,provisioned_active,revision FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s",
                                        (tenant_id, actor_id)).fetchone()
            if not member:
                raise AccessDenied("resource is unavailable")
            revision = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s", (tenant_id,)).fetchone()["authorization_revision"]
            tenant_permissions = {}
            for source, table in (("manual", "tenant_grants"), ("provisioned", "scim_tenant_grants")):
                tenant_permissions[source] = [row["permission"] for row in connection.execute(
                    f"SELECT DISTINCT permission FROM sonn_governance.{table} WHERE tenant_id=%s AND actor_id=%s ORDER BY permission", (tenant_id, actor_id))]
            projects = connection.execute(
                "SELECT project_id FROM (SELECT project_id FROM sonn_governance.project_grants WHERE tenant_id=%s AND actor_id=%s "
                "UNION SELECT project_id FROM sonn_governance.scim_project_grants WHERE tenant_id=%s AND actor_id=%s) grants "
                "WHERE (%s::uuid IS NULL OR project_id>%s::uuid) ORDER BY project_id LIMIT %s",
                (tenant_id, actor_id, tenant_id, actor_id, after, after, limit + 1)).fetchall()
            rows = []
            for project in projects[:limit]:
                row = {"project_id": str(project["project_id"])}
                for source, table in (("manual", "project_grants"), ("provisioned", "scim_project_grants")):
                    row[source] = [entry["permission"] for entry in connection.execute(
                        f"SELECT DISTINCT permission FROM sonn_governance.{table} WHERE tenant_id=%s AND actor_id=%s AND project_id=%s ORDER BY permission",
                        (tenant_id, actor_id, project["project_id"]))]
                rows.append(row)
            self.store._audit(connection, principal, tenant_id, operation="query_grants",
                              semantics_sha256=digest({"kind": "membership", "actor_id": actor_id, "after": after, "limit": limit}))
            self.store._identity(connection, principal)
            return {"actor_id": actor_id, **member, "authorization_revision": revision,
                    "tenant_permissions": tenant_permissions, "project_permissions": rows,
                    "next_after": rows[-1]["project_id"] if len(projects) > limit else None}
