"""Strict host-reported metadata and durable remote pause/stop requests.

The service records provenance and delivery; it never interprets a host report
as independent verification, changes a local DAG, or constructs local authority.
Controls are exact, expiring requests. The receiving local supervisor must check
its captured epoch/revision and durably deduplicate before an effect.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from psycopg.types.json import Jsonb

from .hosts import HostGovernance
from .models import AccessDenied, Conflict, InvalidRequest, digest, identifier

_STATES = {"draft", "planned", "running", "pausing", "paused", "stopping", "review", "blocked", "recovery_required", "completed", "cancelled", "failed"}
_ALERTS = {"none", "needs_input", "stalled", "allowance_exhausted", "lease_expired", "failed_check", "uncertain_effect", "unconfirmed_stop"}
_COUNTS = {"workers_active", "workers_pending", "requests_known", "requests_held", "requests_unknown", "checks_passed", "checks_failed"}


def projection_document(value):
    """Reject freeform text, unknown fields, boolean counts and unknown versions."""
    expected = {"version", "epoch", "local_revision", "state", "alert", "counts"}
    if type(value) is not dict or set(value) != expected or type(value["version"]) is not int or value["version"] != 1:
        raise InvalidRequest("unsupported metadata projection")
    if (type(value["epoch"]) is not int or not 1 <= value["epoch"] <= 2**53
            or type(value["local_revision"]) is not int or not 0 <= value["local_revision"] <= 2**53
            or type(value["state"]) is not str or value["state"] not in _STATES
            or type(value["alert"]) is not str or value["alert"] not in _ALERTS
            or type(value["counts"]) is not dict or set(value["counts"]) != _COUNTS
            or any(type(v) is not int or not 0 <= v <= 10**9 for v in value["counts"].values())):
        raise InvalidRequest("invalid bounded metadata")
    return {**value, "counts": dict(value["counts"])}


class RunMonitoring:
    """One transaction boundary for reporting and delivery, separate from execution."""

    def __init__(self, hosts: HostGovernance):
        self.hosts, self.store = hosts, hosts.store

    @staticmethod
    def _binding(connection, tenant_id, binding_id, host=None, *, lock="UPDATE"):
        identifier(binding_id)
        row = connection.execute(
            f"SELECT * FROM sonn_governance.managed_runs WHERE tenant_id=%s AND binding_id=%s FOR {lock}",
            (tenant_id, binding_id),
        ).fetchone()
        if row is None or host and (row["host_id"] != host["host_id"] or row["project_id"] != host["project_id"]):
            raise AccessDenied("resource is unavailable")
        return row

    @staticmethod
    def _replay(connection, principal, tenant_id, command_id, semantics):
        identifier(command_id)
        key = int.from_bytes(hashlib.sha256(f"monitor:{tenant_id}:{principal.actor_id}:{command_id}".encode()).digest()[:8], "big", signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        row = connection.execute("SELECT sha256,result FROM sonn_governance.monitor_receipts WHERE tenant_id=%s AND actor_id=%s AND command_id=%s",
            (tenant_id, principal.actor_id, command_id)).fetchone()
        if row and row["sha256"] != digest(semantics):
            raise Conflict("command identity has different semantics")
        return row["result"] if row else None

    def _record(self, connection, principal, tenant_id, project_id, command_id, semantics, operation, result):
        audit_id = self.store._audit(connection, principal, tenant_id, operation=operation, project_id=project_id,
                                    semantics_sha256=digest(semantics))
        connection.execute("INSERT INTO sonn_governance.monitor_receipts VALUES(%s,%s,%s,%s,%s,%s)",
            (tenant_id, principal.actor_id, command_id, digest(semantics), Jsonb(result), audit_id))
        return result

    def register(self, principal, *, command_id, lease_id, local_run_id, session_id):
        """Bind an opaque run to the current certified host and enrolling member."""
        for value in (command_id, lease_id, local_run_id, session_id):
            identifier(value)
        semantics = {"operation": "register_run", "lease_id": lease_id, "local_run_id": local_run_id, "session_id": session_id}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return replay
            if connection.execute("SELECT 1 FROM sonn_governance.managed_runs WHERE tenant_id=%s AND host_id=%s AND local_run_id=%s",
                                  (host["tenant_id"], host["host_id"], local_run_id)).fetchone():
                raise Conflict("local run is already bound")
            binding_id = str(uuid4())
            connection.execute("INSERT INTO sonn_governance.managed_runs(tenant_id,project_id,binding_id,host_id,host_generation,"
                "owner_actor,local_run_id,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (host["tenant_id"], host["project_id"], binding_id, host["host_id"], host["generation"], host["enrolled_by"], local_run_id, session_id))
            result = self._record(connection, principal, host["tenant_id"], host["project_id"], command_id, semantics, "register_run",
                                  {"binding_id": binding_id, "revision": 1, "ownership_generation": 1})
            self.hosts._certificate(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            return result

    def ingest(self, principal, *, command_id, binding_id, sequence, projection):
        """Retain bounded ordered metadata; gaps and revoked owners stay visible."""
        identifier(command_id)
        if type(sequence) is not int or not 1 <= sequence <= 2**53:
            raise InvalidRequest("invalid event sequence")
        projection = projection_document(projection)
        semantics = {"operation": "ingest_run", "binding_id": binding_id, "sequence": sequence, "projection": projection}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            row = self._binding(connection, host["tenant_id"], binding_id, host)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return replay
            if sequence > row["sequence"] + 256:
                raise Conflict("event gap exceeds bounded window")
            old = connection.execute("SELECT sha256 FROM sonn_governance.run_events WHERE tenant_id=%s AND binding_id=%s AND sequence=%s",
                                     (host["tenant_id"], binding_id, sequence)).fetchone()
            if old and old["sha256"] != digest(projection):
                raise Conflict("event sequence has different semantics")
            member = connection.execute("SELECT active,provisioned_active FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s",
                                        (host["tenant_id"], row["owner_actor"])).fetchone()
            active = connection.execute("SELECT t.active AND p.active AS active FROM sonn_governance.tenants t JOIN sonn_governance.projects p USING(tenant_id) "
                                        "WHERE t.tenant_id=%s AND p.project_id=%s", (host["tenant_id"], host["project_id"])).fetchone()
            latest = row["projection"]
            regressed = latest and (projection["epoch"] < latest["epoch"] or projection["local_revision"] < latest["local_revision"])
            quarantined = (host["state"] != "active" or host["generation"] != row["host_generation"]
                           or not member or not member["active"] or not member["provisioned_active"]
                           or not self.store._has_permission(connection, host["tenant_id"], row["owner_actor"], host["project_id"], "host_admin")
                           or not active or not active["active"] or bool(regressed))
            if not old:
                connection.execute("INSERT INTO sonn_governance.run_events(tenant_id,binding_id,sequence,sha256,projection,quarantined) VALUES(%s,%s,%s,%s,%s,%s)",
                                   (host["tenant_id"], binding_id, sequence, digest(projection), Jsonb(projection), bool(quarantined)))
            current, latest = row["sequence"], row["projection"]
            if not quarantined:
                pending = connection.execute("SELECT * FROM sonn_governance.run_events WHERE tenant_id=%s AND binding_id=%s AND sequence>%s ORDER BY sequence LIMIT 256",
                                              (host["tenant_id"], binding_id, current)).fetchall()
                for event in pending:
                    if event["sequence"] != current + 1 or event["quarantined"]:
                        break
                    candidate = event["projection"]
                    if latest and (candidate["epoch"] < latest["epoch"] or candidate["local_revision"] < latest["local_revision"]):
                        break
                    current, latest = event["sequence"], candidate
            connection.execute("UPDATE sonn_governance.managed_runs SET sequence=%s,projection=%s,revision=revision+1,last_contact_at=clock_timestamp() "
                               "WHERE tenant_id=%s AND binding_id=%s", (current, Jsonb(latest) if latest else None, host["tenant_id"], binding_id))
            result = self._record(connection, principal, host["tenant_id"], host["project_id"], command_id, semantics, "ingest_run",
                {"binding_id": binding_id, "revision": row["revision"] + 1, "sequence": current,
                 "status": "quarantined" if quarantined else "gap" if current < sequence else "recorded",
                 "expected_sequence": current + 1, "evidence_source": "host_report"})
            self.hosts._certificate(connection, principal)
            return result

    def inspect(self, principal, tenant_id, project_id, *, after=None, limit=20):
        """List authorized opaque bindings and bounded metadata without content."""
        identifier(tenant_id)
        identifier(project_id)
        if after is not None:
            identifier(after)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidRequest("invalid page limit")
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, project_id, "metadata_read")
            rows = connection.execute("SELECT r.*,h.state AS host_state,h.generation AS current_generation FROM sonn_governance.managed_runs r "
                "JOIN sonn_governance.hosts h USING(tenant_id,host_id) WHERE r.tenant_id=%s AND r.project_id=%s "
                "AND (%s::uuid IS NULL OR binding_id>%s::uuid) ORDER BY binding_id LIMIT %s",
                (tenant_id, project_id, after, after, limit + 1)).fetchall()
            result = []
            for row in rows[:limit]:
                pending = connection.execute("SELECT count(*) AS n FROM sonn_governance.run_events WHERE tenant_id=%s AND binding_id=%s AND sequence>%s",
                    (tenant_id, row["binding_id"], row["sequence"])).fetchone()["n"]
                controls = connection.execute("SELECT * FROM sonn_governance.remote_controls WHERE tenant_id=%s AND binding_id=%s ORDER BY requested_at DESC LIMIT 20",
                    (tenant_id, row["binding_id"])).fetchall()
                result.append({"binding_id": str(row["binding_id"]), "host_id": str(row["host_id"]), "revision": row["revision"],
                    "sequence": row["sequence"], "projection": row["projection"], "pending_events": pending,
                    "host_state": row["host_state"], "last_contact_at": row["last_contact_at"].isoformat() if row["last_contact_at"] else None,
                    "evidence_source": "host_report", "controls": [self._control_view(item) for item in controls]})
            self.store._audit(connection, principal, tenant_id, operation="query_runs", project_id=project_id)
            self.store._identity(connection, principal)
            return {"runs": result, "next_after": str(rows[limit-1]["binding_id"]) if len(rows) > limit else None}

    @staticmethod
    def _control_view(row):
        return {"control_id": str(row["control_id"]), "operation": row["operation"], "expected_epoch": row["expected_epoch"],
                "expected_local_revision": row["expected_local_revision"], "policy_revision": row["policy_revision"],
                "requested_at": row["requested_at"].isoformat(), "expires_at": row["expires_at"].isoformat(),
                "received_at": row["received_at"].isoformat() if row["received_at"] else None, "outcome": row["outcome"],
                "reported_processes_stopped": row["reported_processes_stopped"], "evidence_source": "host_report"}

    def request_control(self, principal, tenant_id, project_id, *, binding_id, command_id, expected_revision, expected_epoch,
                        expected_local_revision, operation):
        """Queue a current exact pause/stop request without claiming host execution."""
        for value in (tenant_id, project_id, binding_id, command_id):
            identifier(value)
        if (type(operation) is not str or operation not in {"pause", "stop"}
                or any(type(v) is not int or v < 0 for v in (expected_revision, expected_epoch, expected_local_revision))
                or expected_epoch == 0):
            raise InvalidRequest("invalid remote control")
        semantics = {"operation": operation, "binding_id": binding_id, "expected_revision": expected_revision,
                     "expected_epoch": expected_epoch, "expected_local_revision": expected_local_revision}
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, project_id, "control_execute")
            # Match the host admission order: tenant, project, then run binding.
            policy = connection.execute("SELECT policy_revision FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR SHARE", (tenant_id, project_id)).fetchone()
            row = self._binding(connection, tenant_id, binding_id)
            if str(row["project_id"]) != project_id:
                raise AccessDenied("resource is unavailable")
            replay = self._replay(connection, principal, tenant_id, command_id, semantics)
            if replay:
                self.store._identity(connection, principal)
                return replay
            if (row["revision"] != expected_revision or not row["projection"]
                    or row["projection"]["epoch"] != expected_epoch or row["projection"]["local_revision"] != expected_local_revision):
                raise Conflict("managed run revision changed")
            authorization = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s", (tenant_id,)).fetchone()
            control_id = str(uuid4())
            control = connection.execute("INSERT INTO sonn_governance.remote_controls(tenant_id,binding_id,control_id,actor_id,operation,"
                "expected_epoch,expected_local_revision,policy_revision,authorization_revision,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '60 seconds') RETURNING *",
                (tenant_id, binding_id, control_id, principal.actor_id, operation, expected_epoch, expected_local_revision,
                 policy["policy_revision"], authorization["authorization_revision"])).fetchone()
            result = self._record(connection, principal, tenant_id, project_id, command_id, semantics, "request_control", self._control_view(control))
            self.store._identity(connection, principal)
            return result

    def poll_controls(self, principal, *, binding_id, lease_id):
        """Return still-authorized requests; polling never acknowledges an effect."""
        identifier(lease_id)
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            _, policy = self.hosts._lease(connection, host, lease_id)
            row = self._binding(connection, host["tenant_id"], binding_id, host)
            if host["generation"] != row["host_generation"]:
                raise AccessDenied("resource is unavailable")
            revision = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s", (host["tenant_id"],)).fetchone()["authorization_revision"]
            controls = connection.execute("SELECT * FROM sonn_governance.remote_controls WHERE tenant_id=%s AND binding_id=%s "
                "AND outcome IS NULL AND expires_at>clock_timestamp() AND policy_revision=%s AND authorization_revision=%s ORDER BY requested_at LIMIT 20",
                (host["tenant_id"], binding_id, policy["revision"], revision)).fetchall()
            self.store._audit(connection, principal, host["tenant_id"], operation="poll_controls", project_id=host["project_id"])
            self.hosts._certificate(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            return {"binding_id": binding_id, "controls": [self._control_view(control) for control in controls
                if control["expires_at"] > self.hosts._now(connection) and row["projection"]
                and control["expected_epoch"] == row["projection"]["epoch"]
                and control["expected_local_revision"] == row["projection"]["local_revision"]]}

    def observe_control(self, principal, *, binding_id, control_id, command_id, outcome, processes_stopped=False):
        """Retain host acknowledgement or final report, even after revocation.

        An expired/unreceived command cannot be newly acknowledged or applied.
        A previously received effect may report uncertainty or actual observations
        after authority expires; this grants no new dispatch permission.
        """
        for value in (binding_id, control_id, command_id):
            identifier(value)
        if (type(outcome) is not str or outcome not in {"received", "applied", "denied", "uncertain"}
                or type(processes_stopped) is not bool):
            raise InvalidRequest("invalid control observation")
        semantics = {"operation": "observe_control", "binding_id": binding_id, "control_id": control_id,
                     "outcome": outcome, "processes_stopped": processes_stopped}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            # Serialize project changes before taking the run lock. Late final
            # observations remain admissible even if the project was disabled.
            connection.execute("SELECT project_id FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR UPDATE",
                               (host["tenant_id"], host["project_id"]))
            binding = self._binding(connection, host["tenant_id"], binding_id, host)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return {**replay, "dispatch_permitted": False}
            row = connection.execute("SELECT * FROM sonn_governance.remote_controls WHERE tenant_id=%s AND binding_id=%s AND control_id=%s FOR UPDATE",
                                     (host["tenant_id"], binding_id, control_id)).fetchone()
            if not row:
                raise AccessDenied("resource is unavailable")
            if processes_stopped and (outcome != "applied" or row["operation"] != "stop"):
                raise InvalidRequest("process observation does not match stop outcome")
            if row["outcome"] is not None:
                raise Conflict("control already has a terminal observation")
            if row["received_at"] is None:
                if outcome != "received":
                    raise Conflict("control must be acknowledged before outcome")
                host = self.hosts._host(connection, principal)
                policy = self.hosts._policy(connection, host)
                authorization = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s", (host["tenant_id"],)).fetchone()
                if (row["expires_at"] <= self.hosts._now(connection) or row["authorization_revision"] != authorization["authorization_revision"]
                        or row["policy_revision"] != policy["revision"] or host["generation"] != binding["host_generation"]
                        or not binding["projection"] or binding["projection"]["epoch"] != row["expected_epoch"]
                        or binding["projection"]["local_revision"] != row["expected_local_revision"]):
                    raise AccessDenied("resource is unavailable")
                connection.execute("UPDATE sonn_governance.remote_controls SET received_at=clock_timestamp() WHERE tenant_id=%s AND control_id=%s", (host["tenant_id"], control_id))
            elif outcome == "received":
                raise Conflict("control already acknowledged; replay original command")
            else:
                connection.execute("UPDATE sonn_governance.remote_controls SET outcome=%s,outcome_at=clock_timestamp(),reported_processes_stopped=%s "
                    "WHERE tenant_id=%s AND control_id=%s", (outcome, processes_stopped, host["tenant_id"], control_id))
            row = connection.execute("SELECT * FROM sonn_governance.remote_controls WHERE tenant_id=%s AND control_id=%s", (host["tenant_id"], control_id)).fetchone()
            result = self._record(connection, principal, host["tenant_id"], host["project_id"], command_id, semantics, "observe_control",
                                  {**self._control_view(row), "dispatch_permitted": outcome == "received"})
            self.hosts._certificate(connection, principal)
            if outcome == "received" and row["expires_at"] <= self.hosts._now(connection):
                raise AccessDenied("resource is unavailable")
            return result
