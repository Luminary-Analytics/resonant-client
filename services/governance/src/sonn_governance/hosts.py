"""Certificate-bound enrollment and online request-unit accounting.

This ledger trusts authenticated host observations. It does not proxy provider
requests, verify a remote executor, measure dollars, or grant offline execution.
Every new request needs online reserve/start; uncertain reservations remain held.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .models import (
    AccessDenied, CommandEnvelope, Conflict, HostPrincipal, InvalidRequest, Principal,
    Query, bounded_text, digest, identifier,
)
from .store import GovernanceStore
from .fences import require_unfenced

_DENIED = "resource is unavailable"


class HostGovernance:
    """Trusted human and mTLS adapter facade; no header-supplied host identity."""

    def __init__(self, store: GovernanceStore, *, minimum_runner_protocol: int = 1) -> None:
        if type(minimum_runner_protocol) is not int or minimum_runner_protocol not in {1, 2}:
            raise InvalidRequest("unsupported runner protocol")
        self.store = store
        self.minimum_runner_protocol = minimum_runner_protocol

    @staticmethod
    def _now(connection):
        return connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]

    @classmethod
    def _certificate(cls, connection, principal: HostPrincipal) -> None:
        if not isinstance(principal, HostPrincipal) or principal.expires_at <= cls._now(connection).timestamp():
            raise AccessDenied(_DENIED)

    def _host(self, connection, principal: HostPrincipal, *, observation=False, pending=False):
        self._certificate(connection, principal)
        binding = connection.execute(
            "SELECT tenant_id,host_id FROM sonn_governance.host_directory WHERE certificate_sha256=%s",
            (principal.certificate_sha256,),
        ).fetchone()
        if not binding:
            raise AccessDenied(_DENIED)
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (str(binding["tenant_id"]),))
        tenant = connection.execute(
            "SELECT active FROM sonn_governance.tenants WHERE tenant_id=%s FOR SHARE", (binding["tenant_id"],)
        ).fetchone()
        host = connection.execute(
            "SELECT * FROM sonn_governance.hosts WHERE tenant_id=%s AND host_id=%s FOR UPDATE",
            (binding["tenant_id"], binding["host_id"]),
        ).fetchone()
        if (not tenant or not host or host["certificate_sha256"] != principal.certificate_sha256
                or (not observation and not tenant["active"])
                or (not observation and host["state"] not in ({"pending", "active"} if pending else {"active"}))):
            raise AccessDenied(_DENIED)
        if not observation:
            member = connection.execute(
                "SELECT active,provisioned_active FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s FOR SHARE",
                (host["tenant_id"], host["enrolled_by"]),
            ).fetchone()
            if (not member or not member["active"] or not member["provisioned_active"]
                    or not self.store._has_permission(connection, host["tenant_id"], host["enrolled_by"], host["project_id"], "host_admin")):
                raise AccessDenied(_DENIED)
        self._certificate(connection, principal)
        return host

    @staticmethod
    def _policy(connection, host):
        project = connection.execute(
            "SELECT * FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR UPDATE",
            (host["tenant_id"], host["project_id"]),
        ).fetchone()
        if not project or not project["active"]:
            raise AccessDenied(_DENIED)
        policy = connection.execute(
            "SELECT * FROM sonn_governance.policies WHERE tenant_id=%s AND project_id=%s AND revision=%s",
            (host["tenant_id"], host["project_id"], project["policy_revision"]),
        ).fetchone()
        if not policy:
            raise Conflict("project policy has not been configured")
        return policy

    def _lease(self, connection, host, lease_id):
        row = connection.execute(
            "SELECT * FROM sonn_governance.policy_leases WHERE tenant_id=%s AND lease_id=%s AND host_id=%s",
            (host["tenant_id"], lease_id, host["host_id"]),
        ).fetchone()
        if (not row or row["host_generation"] != host["generation"]
                or row["runner_protocol"] < self.minimum_runner_protocol
                or row["expires_at"] <= self._now(connection)):
            raise AccessDenied(_DENIED)
        policy = self._policy(connection, host)
        if row["expires_at"] <= self._now(connection):
            raise AccessDenied(_DENIED)
        if row["policy_revision"] != policy["revision"] or row["policy_sha256"] != policy["sha256"]:
            raise Conflict("policy lease requires renewal")
        return row, policy

    @staticmethod
    def _receipt(connection, host, namespace, command_id, semantics):
        row = connection.execute(
            "SELECT semantics_sha256,result FROM sonn_governance.host_receipts "
            "WHERE tenant_id=%s AND host_id=%s AND namespace=%s AND command_id=%s",
            (host["tenant_id"], host["host_id"], namespace, command_id),
        ).fetchone()
        if row and row["semantics_sha256"] != digest(semantics):
            raise Conflict("command identity has different semantics")
        return row["result"] if row else None

    def _record(self, connection, principal, host, namespace, command_id, semantics, result, operation):
        audit_id = self.store._audit(
            connection, principal, host["tenant_id"], operation=operation,
            project_id=host["project_id"], semantics_sha256=digest(semantics),
        )
        connection.execute(
            "INSERT INTO sonn_governance.host_receipts VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (host["tenant_id"], host["host_id"], namespace, command_id, digest(semantics), Jsonb(result), audit_id),
        )
        # A blocked audit/receipt insert must not extend an admission deadline.
        self._certificate(connection, principal)
        lease_id = result.get("lease_id")
        if namespace == "start":
            lease_id = self._request(connection, host, command_id)["lease_id"]
        if lease_id is not None:
            self._lease(connection, host, lease_id)
        return result

    def owner_command(self, principal: Principal, command: CommandEnvelope) -> dict:
        """Register a pending certificate or revoke an exact current host revision."""
        semantics = command.semantics()
        if command.operation not in {"enroll_host", "revoke_host"} or command.project_id is None:
            raise InvalidRequest("unsupported host command")
        payload = semantics["payload"]
        enrollment = command.operation == "enroll_host"
        expected = {"host_id", "certificate_sha256"} if enrollment else {"host_id"}
        if set(payload) != expected:
            raise InvalidRequest("invalid host command fields")
        identifier(payload["host_id"])
        if enrollment and (not isinstance(payload["certificate_sha256"], str)
                           or re.fullmatch(r"[a-f0-9]{64}", payload["certificate_sha256"]) is None):
            raise InvalidRequest("invalid certificate identity")
        try:
            with self.store._connection() as connection:
                self.store._authorize(connection, principal, command.tenant_id, command.project_id, "host_admin")
                key = f"{command.tenant_id}:{principal.actor_id}:{command.command_id}"
                number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (number,))
                previous = connection.execute(
                    "SELECT semantics_sha256,result FROM sonn_governance.command_receipts "
                    "WHERE tenant_id=%s AND actor_id=%s AND namespace='governance.v1' AND command_id=%s",
                    (command.tenant_id, principal.actor_id, command.command_id),
                ).fetchone()
                if previous:
                    if previous["semantics_sha256"] != digest(semantics):
                        raise Conflict("command identity has different semantics")
                    self.store._identity(connection, principal)
                    return previous["result"]
                host = connection.execute(
                    "SELECT * FROM sonn_governance.hosts WHERE tenant_id=%s AND host_id=%s FOR UPDATE",
                    (command.tenant_id, payload["host_id"]),
                ).fetchone()
                if enrollment:
                    if host or command.expected_revision != 0:
                        raise Conflict("host revision changed")
                    challenge = secrets.token_urlsafe(32)
                    expires = self._now(connection) + timedelta(seconds=300)
                    connection.execute(
                        "INSERT INTO sonn_governance.hosts(tenant_id,project_id,host_id,certificate_sha256,state,"
                        "revision,generation,challenge_sha256,challenge_expires_at,enrolled_by) "
                        "VALUES(%s,%s,%s,%s,'pending',1,1,%s,%s,%s)",
                        (command.tenant_id, command.project_id, payload["host_id"], payload["certificate_sha256"],
                         hashlib.sha256(challenge.encode()).hexdigest(), expires, principal.actor_id),
                    )
                    connection.execute("INSERT INTO sonn_governance.host_directory VALUES(%s,%s,%s)",
                                       (payload["certificate_sha256"], command.tenant_id, payload["host_id"]))
                    result = {"host_id": payload["host_id"], "state": "pending", "revision": 1,
                              "challenge": challenge, "challenge_expires_at": expires.isoformat()}
                else:
                    if not host or str(host["project_id"]) != command.project_id:
                        raise AccessDenied(_DENIED)
                    if host["revision"] != command.expected_revision:
                        raise Conflict("host revision changed")
                    if host["state"] == "revoked":
                        raise Conflict("host is already revoked")
                    connection.execute(
                        "UPDATE sonn_governance.hosts SET state='revoked',revision=revision+1,generation=generation+1 "
                        "WHERE tenant_id=%s AND host_id=%s", (command.tenant_id, payload["host_id"]),
                    )
                    result = {"host_id": payload["host_id"], "state": "revoked", "revision": host["revision"] + 1}
                audit_id = self.store._audit(connection, principal, command.tenant_id, operation=command.operation,
                                              project_id=command.project_id, revision=result["revision"],
                                              semantics_sha256=digest(semantics))
                connection.execute(
                    "INSERT INTO sonn_governance.command_receipts VALUES(%s,%s,'governance.v1',%s,%s,%s,%s)",
                    (command.tenant_id, principal.actor_id, command.command_id, digest(semantics), Jsonb(result), audit_id),
                )
                self.store._identity(connection, principal)
                return result
        except UniqueViolation as exc:
            raise Conflict("enrollment identity is already reserved") from exc

    def activate(self, principal: HostPrincipal, challenge: str) -> dict:
        """Prove possession of the enrolled certificate and one-time challenge."""
        bounded_text(challenge, 128, "challenge")
        fingerprint = hashlib.sha256(challenge.encode()).hexdigest()
        with self.store._connection() as connection:
            host = self._host(connection, principal, pending=True)
            if not hmac.compare_digest(host["challenge_sha256"], fingerprint):
                raise AccessDenied(_DENIED)
            if host["activation_result"]:
                return host["activation_result"]
            if host["challenge_expires_at"] <= self._now(connection):
                raise AccessDenied(_DENIED)
            result = {"tenant_id": str(host["tenant_id"]), "project_id": str(host["project_id"]),
                      "host_id": str(host["host_id"]), "host_generation": host["generation"],
                      "owner_id": host["enrolled_by"], "state": "active", "revision": host["revision"] + 1}
            connection.execute(
                "UPDATE sonn_governance.hosts SET state='active',revision=revision+1,activation_result=%s "
                "WHERE tenant_id=%s AND host_id=%s", (Jsonb(result), host["tenant_id"], host["host_id"]),
            )
            self.store._audit(connection, principal, host["tenant_id"], operation="activate_host",
                              project_id=host["project_id"], revision=result["revision"])
            self._certificate(connection, principal)
            if host["challenge_expires_at"] <= self._now(connection):
                raise AccessDenied(_DENIED)
            return result

    def lease(self, principal: HostPrincipal, command_id: str, expected_policy_revision: int,
              runner_protocol: int = 1) -> dict:
        """Issue a 60-second online policy snapshot, with zero offline allowance."""
        identifier(command_id)
        if type(expected_policy_revision) is not int or expected_policy_revision < 1:
            raise InvalidRequest("invalid policy revision")
        if type(runner_protocol) is not int or runner_protocol not in {1, 2}:
            raise InvalidRequest("unsupported runner protocol")
        if runner_protocol < self.minimum_runner_protocol:
            raise AccessDenied("runner protocol upgrade required")
        semantics = {"expected_policy_revision": expected_policy_revision, "protocol_version": 1}
        if runner_protocol != 1:
            semantics["runner_protocol"] = runner_protocol
        with self.store._connection() as connection:
            host = self._host(connection, principal)
            policy = self._policy(connection, host)
            if policy["revision"] != expected_policy_revision:
                raise Conflict("policy revision changed")
            previous = self._receipt(connection, host, "lease", command_id, semantics)
            if previous:
                self._lease(connection, host, previous["lease_id"])
                return previous
            issued = self._now(connection)
            expires = min(issued + timedelta(seconds=60),
                          datetime.fromtimestamp(principal.expires_at, tz=timezone.utc))
            lease_id = str(uuid4())
            connection.execute(
                "INSERT INTO sonn_governance.policy_leases(tenant_id,project_id,lease_id,host_id,host_generation,"
                "policy_revision,policy_sha256,document,expires_at,runner_protocol) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (host["tenant_id"], host["project_id"], lease_id, host["host_id"], host["generation"],
                 policy["revision"], policy["sha256"], Jsonb(policy["document"]), expires, runner_protocol),
            )
            result = {"tenant_id": str(host["tenant_id"]), "project_id": str(host["project_id"]),
                      "host_id": str(host["host_id"]), "lease_id": lease_id, "protocol_version": 1,
                      "host_generation": host["generation"], "runner_protocol": runner_protocol, "owner_id": host["enrolled_by"],
                      "policy_revision": policy["revision"], "policy_sha256": policy["sha256"],
                      "policy": policy["document"], "issued_at": issued.isoformat(), "expires_at": expires.isoformat(),
                      "offline_request_allowance": 0, "enforcement_mode": "managed_local_reporting"}
            self._certificate(connection, principal)
            return self._record(connection, principal, host, "lease", command_id, semantics, result, "issue_lease")

    def reserve(self, principal: HostPrincipal, lease_id: str, request_id: str) -> dict:
        """Reserve one project-wide request unit; policy revisions never reset it."""
        identifier(lease_id)
        identifier(request_id)
        semantics = {"lease_id": lease_id, "request_id": request_id, "units": 1}
        with self.store._connection() as connection:
            host = self._host(connection, principal)
            _, policy = self._lease(connection, host, lease_id)
            require_unfenced(connection, host["tenant_id"], "request", request_id)
            previous = self._receipt(connection, host, "reserve", request_id, semantics)
            if previous:
                return previous
            if connection.execute(
                "SELECT 1 FROM sonn_governance.host_requests WHERE tenant_id=%s AND request_id=%s",
                (host["tenant_id"], request_id),
            ).fetchone():
                raise AccessDenied(_DENIED)
            used = connection.execute(
                "SELECT coalesce(sum(coalesce(consumed,1)),0) AS used FROM sonn_governance.host_requests "
                "WHERE tenant_id=%s AND project_id=%s", (host["tenant_id"], host["project_id"]),
            ).fetchone()["used"]
            if used >= policy["document"]["request_limit"]:
                raise Conflict("request allowance exhausted")
            connection.execute(
                "INSERT INTO sonn_governance.host_requests(tenant_id,project_id,request_id,host_id,lease_id,state) "
                "VALUES(%s,%s,%s,%s,%s,'reserved')",
                (host["tenant_id"], host["project_id"], request_id, host["host_id"], lease_id),
            )
            result = {"request_id": request_id, "lease_id": lease_id, "state": "reserved", "units": 1}
            self._lease(connection, host, lease_id)
            self._certificate(connection, principal)
            return self._record(connection, principal, host, "reserve", request_id, semantics, result, "reserve_request")

    @staticmethod
    def _request(connection, host, request_id):
        row = connection.execute(
            "SELECT * FROM sonn_governance.host_requests WHERE tenant_id=%s AND request_id=%s AND host_id=%s FOR UPDATE",
            (host["tenant_id"], request_id, host["host_id"]),
        ).fetchone()
        if not row:
            raise AccessDenied(_DENIED)
        return row

    def start(self, principal: HostPrincipal, request_id: str) -> dict:
        """Record invocation before dispatch; retries never grant another dispatch."""
        identifier(request_id)
        semantics = {"request_id": request_id}
        with self.store._connection() as connection:
            host = self._host(connection, principal)
            require_unfenced(connection, host["tenant_id"], "request", request_id)
            row = self._request(connection, host, request_id)
            lease_row, policy = self._lease(connection, host, row["lease_id"])
            if lease_row["runner_protocol"] == 2:
                from .managed_resources import ManagedResources

                ManagedResources.require_bound_start(connection, host, row, policy)
            previous = self._receipt(connection, host, "start", request_id, semantics)
            if previous:
                return {**previous, "dispatch_permitted": False}
            if row["state"] != "reserved":
                raise Conflict("request is not available to start")
            connection.execute(
                "UPDATE sonn_governance.host_requests SET state='started',started=true WHERE tenant_id=%s AND request_id=%s",
                (host["tenant_id"], request_id),
            )
            result = {"request_id": request_id, "state": "started", "dispatch_permitted": True}
            self._lease(connection, host, row["lease_id"])
            self._certificate(connection, principal)
            return self._record(connection, principal, host, "start", request_id, semantics, result, "start_request")

    def settle(self, principal: HostPrincipal, request_id: str, outcome: str) -> dict:
        """Retain an attributed host observation; never infer provider success/cost."""
        identifier(request_id)
        if not isinstance(outcome, str) or outcome not in {"completed", "failed", "never_started", "uncertain"}:
            raise InvalidRequest("invalid observation")
        with self.store._connection() as connection:
            host = self._host(connection, principal, observation=True)
            row = self._request(connection, host, request_id)
            if row["state"] in {"completed", "failed", "never_started"} and row["state"] != outcome:
                raise Conflict("terminal observation is immutable")
            if outcome == "never_started" and row["started"]:
                raise Conflict("a durable start cannot be refunded")
            if outcome in {"completed", "failed"} and not row["started"]:
                raise Conflict("request has no admitted invocation")
            consumed = 1 if outcome in {"completed", "failed"} else (0 if outcome == "never_started" else None)
            result = {"request_id": request_id, "state": outcome, "consumed_units": consumed,
                      "held_units": 1 if consumed is None else 0, "source": "authenticated_host_observation"}
            if row["state"] != outcome:
                connection.execute(
                    "UPDATE sonn_governance.host_requests SET state=%s,consumed=%s WHERE tenant_id=%s AND request_id=%s",
                    (outcome, consumed, host["tenant_id"], request_id),
                )
                self.store._audit(connection, principal, host["tenant_id"], operation="settle_request",
                                  project_id=host["project_id"], semantics_sha256=digest(result))
            self._certificate(connection, principal)
            return result

    def inspect(self, principal: Principal, query: Query) -> dict:
        """Bounded owner metadata without certificate/challenge/receipt secrets."""
        query.validate()
        if query.project_id is None or query.kind != "metadata" or query.after:
            raise InvalidRequest("host inspection requires project metadata scope")
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, query.tenant_id, query.project_id, "metadata_read")
            rows = connection.execute(
                "SELECT host_id,state,revision,generation FROM sonn_governance.hosts "
                "WHERE tenant_id=%s AND project_id=%s ORDER BY host_id LIMIT %s",
                (query.tenant_id, query.project_id, query.limit),
            ).fetchall()
            usage = connection.execute(
                "SELECT coalesce(sum(consumed),0) AS consumed_units, count(*) FILTER(WHERE consumed IS NULL) AS held_units "
                "FROM sonn_governance.host_requests WHERE tenant_id=%s AND project_id=%s",
                (query.tenant_id, query.project_id),
            ).fetchone()
            self.store._audit(connection, principal, query.tenant_id, operation="query_hosts", project_id=query.project_id)
            self.store._identity(connection, principal)
            return {"hosts": [{**row, "host_id": str(row["host_id"])} for row in rows],
                    "usage": {key: int(value) for key, value in usage.items()},
                    "enforcement_mode": "managed_local_reporting", "offline_request_allowance": 0}
