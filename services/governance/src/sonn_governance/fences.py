"""Durable negative admission receipts for an exact original host lease.

A failed read or an absent row alone is never a refund. This observation API
atomically persists a tombstone checked by every subsequent admission. Existing
resources are reported present without inferring their execution outcome.
"""

from uuid import uuid4

from .models import AccessDenied, InvalidRequest, digest, identifier


KINDS = frozenset({"request", "worker", "tool", "effect"})


def require_unfenced(connection, tenant_id, kind, resource_id):
    """Called under the same host/project locks as the original admission."""
    if connection.execute("SELECT 1 FROM sonn_governance.resource_fences WHERE tenant_id=%s AND kind=%s AND resource_id=%s",
                          (tenant_id, kind, resource_id)).fetchone():
        raise AccessDenied("resource identity was permanently fenced")


class ResourceFences:
    """Observation-only absence fencing; no lease renewal or positive permit."""

    def __init__(self, hosts):
        self.hosts, self.store = hosts, hosts.store

    def fence_absent(self, principal, *, resource_kind, resource_id, lease_id):
        for value in (resource_id, lease_id):
            identifier(value)
        if type(resource_kind) is not str or resource_kind not in KINDS:
            raise InvalidRequest("invalid resource fence kind")
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            # The original lease proves host/project ownership, not live
            # execution authority. Observation intentionally permits expiry and
            # revocation, while TLS/certificate authentication remains required.
            lease = connection.execute("SELECT * FROM sonn_governance.policy_leases WHERE tenant_id=%s AND lease_id=%s AND host_id=%s AND project_id=%s",
                (host["tenant_id"], lease_id, host["host_id"], host["project_id"])).fetchone()
            if not lease or lease["runner_protocol"] != 2:
                raise AccessDenied("original resource lease is unavailable")
            connection.execute("SELECT project_id FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR UPDATE",
                (host["tenant_id"], host["project_id"]))
            old = connection.execute("SELECT * FROM sonn_governance.resource_fences WHERE tenant_id=%s AND kind=%s AND resource_id=%s",
                (host["tenant_id"], resource_kind, resource_id)).fetchone()
            if old and (old["host_id"] != host["host_id"] or str(old["lease_id"]) != lease_id):
                raise AccessDenied("resource fence is unavailable")
            if resource_kind == "tool":
                existing = connection.execute("SELECT w.host_id FROM sonn_governance.tool_admissions a JOIN sonn_governance.worker_slots w USING(tenant_id,worker_id) "
                    "WHERE a.tenant_id=%s AND a.action_id=%s", (host["tenant_id"], resource_id)).fetchone()
            else:
                table, column = {"request": ("host_requests", "request_id"), "worker": ("worker_slots", "worker_id"),
                                 "effect": ("owner_effects", "effect_id")}[resource_kind]
                existing = connection.execute(f"SELECT host_id FROM sonn_governance.{table} WHERE tenant_id=%s AND {column}=%s",
                    (host["tenant_id"], resource_id)).fetchone()
            if existing:
                if old or existing["host_id"] != host["host_id"]:
                    raise AccessDenied("resource fence is unavailable")
                self.hosts._certificate(connection, principal)
                return {"kind": resource_kind, "resource_id": resource_id, "lease_id": lease_id, "state": "present",
                        "source": "server_non_admission_fence", "dispatch_permitted": False}
            if old is None:
                fence_id = str(uuid4())
                audit = self.store._audit(connection, principal, host["tenant_id"], operation="fence_absent", project_id=host["project_id"],
                    decision_id=resource_id, semantics_sha256=digest({"kind": resource_kind, "resource_id": resource_id, "lease_id": lease_id}))
                connection.execute("INSERT INTO sonn_governance.resource_fences(tenant_id,project_id,host_id,kind,resource_id,lease_id,fence_id,audit_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (host["tenant_id"], host["project_id"], host["host_id"], resource_kind, resource_id, lease_id, fence_id, audit))
            else:
                fence_id = str(old["fence_id"])
            self.hosts._certificate(connection, principal)
            return {"kind": resource_kind, "resource_id": resource_id, "lease_id": lease_id, "fence_id": fence_id, "state": "fenced_absent",
                    "source": "server_non_admission_fence", "dispatch_permitted": False}
