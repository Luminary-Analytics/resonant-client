"""PostgreSQL authority for tenant grants and versioned policy decisions.

This is a trusted service API, not an identity verifier. Use a dedicated runtime
login without ownership/BYPASSRLS privileges. Connections are short lived; tenant
context is SET LOCAL and cannot survive a transaction. No desktop database,
project path, user content, or local swarming Scope enters this module.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from importlib.resources import files
from typing import Iterator
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import (
    AccessDenied, CommandEnvelope, Conflict, InvalidRequest, Principal, Query,
    UnsupportedVersion, digest, identifier, membership_document,
    policy_document,
)
from .restore_guard import require_available

SCHEMA_VERSION = 15
_DENIED = "resource is unavailable"


def migrate(owner_dsn: str, *, application_role: str) -> None:
    """Install the initial schema with operator credentials, never runtime login.

    Role creation/credentials are operator responsibilities. Unknown schemas are
    rejected. Initial DDL and grants commit atomically under a migration lock.
    """
    with psycopg.connect(owner_dsn, row_factory=dict_row, connect_timeout=5) as connection:
        require_available(connection)
        connection.execute("SELECT pg_advisory_xact_lock(734025001)")
        role = connection.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (application_role,)
        ).fetchone()
        if role is None or role["rolsuper"] or role["rolbypassrls"]:
            raise InvalidRequest("application role must exist without elevated privileges")
        exists = connection.execute(
            "SELECT to_regclass('sonn_governance.schema_version') AS relation"
        ).fetchone()["relation"]
        if exists:
            version = connection.execute(
                "SELECT version FROM sonn_governance.schema_version WHERE singleton"
            ).fetchone()
            if version is None or version["version"] not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, SCHEMA_VERSION}:
                raise UnsupportedVersion("unsupported governance schema")
        else:
            migration = files("sonn_governance").joinpath("migrations/001_initial.sql").read_text("utf-8")
            connection.execute(migration)
        version = connection.execute(
            "SELECT version FROM sonn_governance.schema_version WHERE singleton"
        ).fetchone()["version"]
        if version == 1:
            connection.execute(files("sonn_governance").joinpath("migrations/002_hosts.sql").read_text("utf-8"))
        if version <= 2:
            connection.execute(files("sonn_governance").joinpath("migrations/003_content.sql").read_text("utf-8"))
        if version <= 3:
            connection.execute(files("sonn_governance").joinpath("migrations/004_host_owner.sql").read_text("utf-8"))
        if version <= 4:
            connection.execute(files("sonn_governance").joinpath("migrations/005_grant_approvals.sql").read_text("utf-8"))
        if version <= 5:
            connection.execute(files("sonn_governance").joinpath("migrations/006_monitoring.sql").read_text("utf-8"))
        if version <= 6:
            connection.execute(files("sonn_governance").joinpath("migrations/007_scim.sql").read_text("utf-8"))
        if version <= 7:
            connection.execute(files("sonn_governance").joinpath("migrations/008_archive.sql").read_text("utf-8"))
        if version <= 8:
            connection.execute(files("sonn_governance").joinpath("migrations/009_archive_binding.sql").read_text("utf-8"))
        if version <= 9:
            connection.execute(files("sonn_governance").joinpath("migrations/010_managed_resources.sql").read_text("utf-8"))
        if version <= 10:
            connection.execute(files("sonn_governance").joinpath("migrations/011_request_failures.sql").read_text("utf-8"))
        if version <= 11:
            connection.execute(files("sonn_governance").joinpath("migrations/012_owner_effects.sql").read_text("utf-8"))
        if version <= 12:
            connection.execute(files("sonn_governance").joinpath("migrations/013_managed_collaboration.sql").read_text("utf-8"))
        if version <= 13:
            connection.execute(files("sonn_governance").joinpath("migrations/014_resource_fences.sql").read_text("utf-8"))
        if version <= 14:
            connection.execute(files("sonn_governance").joinpath("migrations/015_sharing_retention.sql").read_text("utf-8"))
        name = sql.Identifier(application_role)
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA sonn_governance TO {}").format(name))
        connection.execute(sql.SQL(
            "GRANT SELECT ON ALL TABLES IN SCHEMA sonn_governance TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT,UPDATE ON sonn_governance.memberships TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(authorization_revision) ON sonn_governance.tenants TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(policy_revision) ON sonn_governance.projects TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT,DELETE ON sonn_governance.tenant_grants,sonn_governance.project_grants TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.policies,sonn_governance.audit,"
            "sonn_governance.command_receipts TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT USAGE ON ALL SEQUENCES IN SCHEMA sonn_governance TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT,UPDATE ON sonn_governance.hosts,sonn_governance.host_requests TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.host_directory,sonn_governance.policy_leases,"
            "sonn_governance.host_receipts TO {}"
        ).format(name))
        connection.execute(sql.SQL("GRANT INSERT ON sonn_governance.grant_requests TO {}").format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(state,decided_by,decision_audit_id) ON sonn_governance.grant_requests TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.managed_runs,sonn_governance.run_events,"
            "sonn_governance.remote_controls,sonn_governance.monitor_receipts TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(revision,sequence,projection,last_contact_at) ON sonn_governance.managed_runs TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(received_at,outcome,outcome_at,reported_processes_stopped) ON sonn_governance.remote_controls TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.scim_users,sonn_governance.scim_groups TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(user_name,user_name_key,active,deleted,revision,modified_at) ON sonn_governance.scim_users TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(display_name,deleted,revision,modified_at) ON sonn_governance.scim_groups TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT,DELETE ON sonn_governance.scim_members,sonn_governance.scim_tenant_rules,"
            "sonn_governance.scim_project_rules,sonn_governance.scim_tenant_grants,sonn_governance.scim_project_grants TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.content_objects,sonn_governance.content_receipts TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(revision,ciphertext,content_sha256,size_bytes,media_type,held,hold_actor,hold_reason,deleted_at) "
            "ON sonn_governance.content_objects TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.worker_slots,sonn_governance.request_bindings,"
            "sonn_governance.tool_admissions,sonn_governance.resource_receipts,sonn_governance.owner_effects TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT UPDATE(state,observed_at) ON sonn_governance.worker_slots,sonn_governance.tool_admissions,sonn_governance.owner_effects TO {}"
        ).format(name))
        connection.execute(sql.SQL(
            "GRANT INSERT ON sonn_governance.sharing_policies,sonn_governance.sharing_grants,sonn_governance.sharing_messages,"
            "sonn_governance.sharing_acceptances,sonn_governance.sharing_receipts TO {}"
        ).format(name))
        connection.execute(sql.SQL("GRANT UPDATE(approved_at,revoked_at) ON sonn_governance.sharing_grants TO {}").format(name))
        connection.execute(sql.SQL("GRANT UPDATE(delivered_at) ON sonn_governance.sharing_messages TO {}").format(name))
        connection.execute(sql.SQL("GRANT INSERT ON sonn_governance.resource_fences TO {}").format(name))
        connection.execute(sql.SQL("GRANT INSERT ON sonn_governance.sharing_retention_receipts TO {}").format(name))
        for table, sha in (("sharing_grants", "terms_sha256"), ("sharing_messages", "body_sha256")):
            connection.execute(sql.SQL("GRANT UPDATE(retention_revision,held,hold_actor,hold_reason,deleted_at,ciphertext,key_id,nonce,{}) "
                "ON sonn_governance.{} TO {}").format(sql.Identifier(sha), sql.Identifier(table), name))


def bootstrap(owner_dsn: str, *, tenant_id: str, administrator: Principal,
              project_ids: list[str]) -> None:
    """Operator-only initial tenant enrollment; never exposed on public HTTP.

    Initial administrator receives membership_admin, membership_approve and policy_admin.
    Metadata/content/control/audit rights require separate recorded grants.
    No existing tenant or membership is silently rewritten.
    """
    identifier(tenant_id)
    for project_id in project_ids:
        identifier(project_id)
    if len(set(project_ids)) != len(project_ids):
        raise InvalidRequest("duplicate project")
    with psycopg.connect(owner_dsn, connect_timeout=5) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        connection.execute("INSERT INTO sonn_governance.tenants(tenant_id) VALUES(%s)", (tenant_id,))
        connection.execute(
            "INSERT INTO sonn_governance.memberships(tenant_id,actor_id,active,revision) VALUES(%s,%s,true,0)",
            (tenant_id, administrator.actor_id),
        )
        for permission in ("membership_admin", "membership_approve", "policy_admin"):
            connection.execute(
                "INSERT INTO sonn_governance.tenant_grants VALUES(%s,%s,%s)",
                (tenant_id, administrator.actor_id, permission),
            )
        for project_id in project_ids:
            connection.execute(
                "INSERT INTO sonn_governance.projects(tenant_id,project_id) VALUES(%s,%s)",
                (tenant_id, project_id),
            )
        connection.execute(
            "INSERT INTO sonn_governance.audit(tenant_id,actor_id,operation,resource_revision) "
            "VALUES(%s,%s,'bootstrap',0)", (tenant_id, administrator.actor_id),
        )


class GovernanceStore:
    """Small deny-by-default transactional interface for verified identities."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with self._connection() as connection:
            self._validate_runtime_role(connection)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout = '10s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout = '15s'")
            connection.execute("SET LOCAL synchronous_commit = on")
            require_available(connection)
            version = connection.execute(
                "SELECT version FROM sonn_governance.schema_version WHERE singleton"
            ).fetchone()
            if version is None or version["version"] != SCHEMA_VERSION:
                raise UnsupportedVersion("unsupported governance schema")
            yield connection

    @staticmethod
    def _validate_runtime_role(connection) -> None:
        role = connection.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        dangerous = connection.execute(
            "SELECT has_schema_privilege(current_user,'sonn_governance','CREATE') AS schema_create,"
            "has_table_privilege(current_user,'sonn_governance.audit','UPDATE,DELETE,TRUNCATE') AS audit_mutate,"
            "has_table_privilege(current_user,'sonn_governance.command_receipts','UPDATE,DELETE,TRUNCATE') "
            "AS receipt_mutate, "
            "has_table_privilege(current_user,'sonn_governance.policies','UPDATE,DELETE,TRUNCATE') AS policy_mutate, "
            "has_table_privilege(current_user,'sonn_governance.host_receipts','UPDATE,DELETE,TRUNCATE') AS host_receipt_mutate, "
            "has_table_privilege(current_user,'sonn_governance.policy_leases','UPDATE,DELETE,TRUNCATE') AS lease_mutate, "
            "has_table_privilege(current_user,'sonn_governance.host_directory','UPDATE,DELETE,TRUNCATE') AS directory_mutate, "
            "has_table_privilege(current_user,'sonn_governance.content_receipts','UPDATE,DELETE,TRUNCATE') AS content_receipt_mutate, "
            "has_table_privilege(current_user,'sonn_governance.run_events','UPDATE,DELETE,TRUNCATE') AS run_event_mutate, "
            "has_table_privilege(current_user,'sonn_governance.monitor_receipts','UPDATE,DELETE,TRUNCATE') AS monitor_receipt_mutate, "
            "has_table_privilege(current_user,'sonn_governance.scim_credentials','INSERT,UPDATE,DELETE,TRUNCATE') AS provisioning_credential_mutate, "
            "EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='sonn_governance' AND pg_has_role(current_user,c.relowner,'MEMBER')) AS owner_member"
        ).fetchone()
        if role["rolsuper"] or role["rolbypassrls"] or any(dangerous.values()):
            raise InvalidRequest("runtime database role has unsafe privileges")
        for table in ("schema_version", "audit", "command_receipts", "policies", "host_receipts",
                      "policy_leases", "host_directory", "content_receipts", "run_events", "monitor_receipts",
                      "scim_credentials", "request_bindings", "resource_receipts", "sharing_policies", "sharing_acceptances", "sharing_receipts", "resource_fences", "sharing_retention_receipts"):
            resource = f"sonn_governance.{table}"
            unsafe = connection.execute(
                "SELECT has_table_privilege(current_user,%s,'UPDATE,DELETE,TRUNCATE,TRIGGER') OR "
                "has_any_column_privilege(current_user,%s,'UPDATE') AS dangerous", (resource, resource),
            ).fetchone()["dangerous"]
            if unsafe:
                raise InvalidRequest("runtime database role has unsafe immutable-record privileges")
        if connection.execute(
            "SELECT has_any_column_privilege(current_user,'sonn_governance.scim_credentials','INSERT') AS dangerous"
        ).fetchone()["dangerous"]:
            raise InvalidRequest("runtime database role has unsafe provisioning privileges")
        for table in ("audit_outbox", "archive_deliveries", "archive_requirements"):
            resource = f"sonn_governance.{table}"
            unsafe = connection.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') OR "
                "has_any_column_privilege(current_user,%s,'INSERT,UPDATE') AS dangerous", (resource, resource)).fetchone()["dangerous"]
            if unsafe:
                raise InvalidRequest("runtime database role has unsafe archival privileges")
        mutable_resources = {table: ["state", "observed_at"] for table in ("worker_slots", "tool_admissions", "owner_effects")}
        mutable_resources.update(sharing_grants=["approved_at", "revoked_at"], sharing_messages=["delivered_at"])
        retention = ["retention_revision", "held", "hold_actor", "hold_reason", "deleted_at", "ciphertext", "key_id", "nonce"]
        mutable_resources["sharing_grants"] += [*retention, "terms_sha256"]
        mutable_resources["sharing_messages"] += [*retention, "body_sha256"]
        for table, columns in mutable_resources.items():
            resource = f"sonn_governance.{table}"
            unsafe = connection.execute(
                "SELECT has_table_privilege(current_user,%s,'UPDATE,DELETE,TRUNCATE,TRIGGER') OR EXISTS("
                "SELECT 1 FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped "
                "AND NOT (attname=ANY(%s)) AND has_column_privilege(current_user,%s,attname,'UPDATE')) AS dangerous",
                (resource, resource, columns, resource)).fetchone()["dangerous"]
            if unsafe:
                raise InvalidRequest("runtime database role has unsafe resource identity privileges")

    @staticmethod
    def _has_permission(connection, tenant_id, actor_id, project_id, permission) -> bool:
        """Union independent manual and provisioned grant sources without copying roles."""
        return bool(connection.execute(
            "SELECT 1 FROM sonn_governance.tenant_grants WHERE tenant_id=%s AND actor_id=%s AND permission=%s "
            "UNION ALL SELECT 1 FROM sonn_governance.scim_tenant_grants WHERE tenant_id=%s AND actor_id=%s AND permission=%s "
            "UNION ALL SELECT 1 FROM sonn_governance.project_grants WHERE tenant_id=%s AND actor_id=%s AND project_id=%s AND permission=%s "
            "UNION ALL SELECT 1 FROM sonn_governance.scim_project_grants WHERE tenant_id=%s AND actor_id=%s AND project_id=%s AND permission=%s LIMIT 1",
            (tenant_id, actor_id, permission, tenant_id, actor_id, permission,
             tenant_id, actor_id, project_id, permission, tenant_id, actor_id, project_id, permission),
        ).fetchone())

    @staticmethod
    def _identity(connection, principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "human":
            raise AccessDenied(_DENIED)
        current_time = connection.execute("SELECT extract(epoch FROM clock_timestamp()) AS now").fetchone()["now"]
        if principal.expires_at <= float(current_time):
            raise AccessDenied(_DENIED)

    @classmethod
    def _authorize(cls, connection, principal: Principal, tenant_id: str,
                   project_id: str | None, permission: str, *, edit_membership: bool = False) -> dict | None:
        cls._identity(connection, principal)
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        # Grant changes take the exclusive tenant lock first. Requests holding a
        # share lock linearize before revocation, and requests after it see the
        # committed grants. No stale token roles or membership cache participates.
        lock = "FOR UPDATE" if edit_membership else "FOR SHARE"
        tenant = connection.execute(
            f"SELECT * FROM sonn_governance.tenants WHERE tenant_id=%s {lock}", (tenant_id,)
        ).fetchone()
        if tenant is None or not tenant["active"]:
            raise AccessDenied(_DENIED)
        member = connection.execute(
            "SELECT active,provisioned_active FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s FOR SHARE",
            (tenant_id, principal.actor_id),
        ).fetchone()
        if member is None or not member["active"] or not member["provisioned_active"]:
            raise AccessDenied(_DENIED)
        project = None
        if project_id is not None:
            project = connection.execute(
                "SELECT * FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s",
                (tenant_id, project_id),
            ).fetchone()
            if project is None or not project["active"]:
                raise AccessDenied(_DENIED)
        if not cls._has_permission(connection, tenant_id, principal.actor_id, project_id, permission):
            raise AccessDenied(_DENIED)
        # Waiting for a conflicting grant transaction must not extend token life.
        cls._identity(connection, principal)
        return tenant if edit_membership else project

    @staticmethod
    def _audit(connection, principal: Principal, tenant_id: str, *, operation: str,
               project_id: str | None = None, revision: int | None = None,
               decision_id: str | None = None, semantics_sha256: str | None = None) -> int:
        return connection.execute(
            "INSERT INTO sonn_governance.audit(tenant_id,actor_id,operation,project_id,resource_revision,"
            "decision_id,semantics_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING audit_id",
            (tenant_id, principal.actor_id, operation, project_id, revision, decision_id, semantics_sha256),
        ).fetchone()["audit_id"]

    def query(self, principal: Principal, query: Query) -> dict:
        """Return bounded authorized metadata; no content or raw snapshot surface."""
        query.validate()
        permission = {"metadata": "metadata_read", "policy": "policy_admin", "audit": "audit_read"}[query.kind]
        with self._connection() as connection:
            project = self._authorize(connection, principal, query.tenant_id, query.project_id, permission)
            if query.kind == "metadata":
                result = {"tenant_id": query.tenant_id, "project_id": query.project_id,
                          "active": project["active"], "policy_revision": project["policy_revision"]}
            elif query.kind == "policy":
                policy = connection.execute(
                    "SELECT revision,document,sha256 FROM sonn_governance.policies "
                    "WHERE tenant_id=%s AND project_id=%s AND revision=%s",
                    (query.tenant_id, query.project_id, project["policy_revision"]),
                ).fetchone()
                result = {"tenant_id": query.tenant_id, "project_id": query.project_id,
                          "revision": project["policy_revision"], "policy": policy["document"] if policy else None,
                          "sha256": policy["sha256"] if policy else None}
            else:
                rows = connection.execute(
                    "SELECT audit_id,actor_id,operation,project_id,resource_revision,decision_id,"
                    "semantics_sha256,occurred_at FROM sonn_governance.audit WHERE tenant_id=%s AND audit_id>%s "
                    "AND (%s::uuid IS NULL OR project_id=%s::uuid) ORDER BY audit_id LIMIT %s",
                    (query.tenant_id, query.after, query.project_id, query.project_id, query.limit),
                ).fetchall()
                result = {"events": [{**row, "project_id": str(row["project_id"]) if row["project_id"] else None,
                                      "decision_id": str(row["decision_id"]) if row["decision_id"] else None,
                                      "occurred_at": row["occurred_at"].isoformat()} for row in rows]}
            self._audit(connection, principal, query.tenant_id, operation=f"query_{query.kind}",
                        project_id=query.project_id)
            self._identity(connection, principal)
            return result

    def command(self, principal: Principal, command: CommandEnvelope) -> dict:
        """Commit authority, revision, immutable receipt and audit as one decision."""
        semantics = command.semantics()
        operation = semantics["operation"]
        if operation not in {"set_policy", "set_membership"}:
            raise InvalidRequest("unsupported core operation")
        membership = operation == "set_membership"
        if membership and command.project_id is not None:
            raise InvalidRequest("membership commands require tenant scope")
        if not membership and command.project_id is None:
            raise InvalidRequest("policy commands require a project")
        payload = semantics["payload"]
        if membership:
            membership_document(payload)
        elif set(payload) != {"policy"}:
            raise InvalidRequest("invalid policy command")
        else:
            policy_document(payload["policy"])
        fingerprint = digest(semantics)
        with self._connection() as connection:
            target = self._authorize(
                connection, principal, command.tenant_id, command.project_id,
                "membership_admin" if membership else "policy_admin", edit_membership=membership,
            )
            if membership:
                # Validate all foreign references before replay; absence is not
                # distinguished from a foreign tenant's project identifier.
                for project_id in payload["project_permissions"]:
                    if not connection.execute(
                        "SELECT 1 FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s AND active",
                        (command.tenant_id, project_id),
                    ).fetchone():
                        raise AccessDenied(_DENIED)
            # A per-actor key lock covers absent receipt rows and cross-project
            # reuse. The complete semantics include operation, revision and scope.
            key = f"{command.tenant_id}:{principal.actor_id}:{command.command_id}"
            key_number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (key_number,))
            receipt = connection.execute(
                "SELECT semantics_sha256,result FROM sonn_governance.command_receipts "
                "WHERE tenant_id=%s AND actor_id=%s AND namespace='governance.v1' AND command_id=%s",
                (command.tenant_id, principal.actor_id, command.command_id),
            ).fetchone()
            if receipt:
                if receipt["semantics_sha256"] != fingerprint:
                    raise Conflict("command identity has different semantics")
                self._identity(connection, principal)
                return receipt["result"]
            if not membership:
                target = connection.execute(
                    "SELECT * FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR UPDATE",
                    (command.tenant_id, command.project_id),
                ).fetchone()
            revision_key = "authorization_revision" if membership else "policy_revision"
            if target[revision_key] != command.expected_revision:
                raise Conflict("resource revision changed")
            revision = command.expected_revision + 1
            pending = None
            if membership:
                from .grants import request_grant, sensitive_additions

                if sensitive_additions(connection, command.tenant_id, principal.actor_id, payload):
                    revision = command.expected_revision
                    pending = request_grant(connection, command.tenant_id, principal, payload, revision)
                else:
                    self._set_membership(connection, command.tenant_id, payload, revision)
            else:
                connection.execute(
                    "INSERT INTO sonn_governance.policies VALUES(%s,%s,%s,%s,%s)",
                    (command.tenant_id, command.project_id, revision, Jsonb(payload["policy"]), digest(payload["policy"])),
                )
                connection.execute(
                    "UPDATE sonn_governance.projects SET policy_revision=%s WHERE tenant_id=%s AND project_id=%s",
                    (revision, command.tenant_id, command.project_id),
                )
            decision_id = str(uuid4())
            audit_id = self._audit(connection, principal, command.tenant_id,
                                   operation="request_grant" if pending else operation,
                                   project_id=command.project_id, revision=revision,
                                   decision_id=decision_id, semantics_sha256=fingerprint)
            result = {"decision_id": decision_id, "revision": revision, "audit_id": audit_id,
                      "operation": operation, "project_id": command.project_id}
            if pending:
                result.update(pending)
            connection.execute(
                "INSERT INTO sonn_governance.command_receipts VALUES(%s,%s,'governance.v1',%s,%s,%s,%s)",
                (command.tenant_id, principal.actor_id, command.command_id, fingerprint, Jsonb(result), audit_id),
            )
            self._identity(connection, principal)
            return result

    @staticmethod
    def _set_membership(connection, tenant_id: str, payload: dict, revision: int) -> None:
        actor_id = payload["actor_id"]
        connection.execute(
            "INSERT INTO sonn_governance.memberships(tenant_id,actor_id,active,revision) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(tenant_id,actor_id) DO UPDATE SET active=excluded.active,revision=excluded.revision",
            (tenant_id, actor_id, payload["active"], revision),
        )
        connection.execute("DELETE FROM sonn_governance.tenant_grants WHERE tenant_id=%s AND actor_id=%s",
                           (tenant_id, actor_id))
        connection.execute("DELETE FROM sonn_governance.project_grants WHERE tenant_id=%s AND actor_id=%s",
                           (tenant_id, actor_id))
        for permission in payload["tenant_permissions"]:
            connection.execute("INSERT INTO sonn_governance.tenant_grants VALUES(%s,%s,%s)",
                               (tenant_id, actor_id, permission))
        for project_id, permissions in payload["project_permissions"].items():
            for permission in permissions:
                connection.execute("INSERT INTO sonn_governance.project_grants VALUES(%s,%s,%s,%s)",
                                   (tenant_id, actor_id, project_id, permission))
        connection.execute("UPDATE sonn_governance.tenants SET authorization_revision=%s WHERE tenant_id=%s",
                           (revision, tenant_id))
