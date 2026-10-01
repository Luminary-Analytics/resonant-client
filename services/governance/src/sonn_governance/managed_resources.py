"""Protocol-2 host admission for shared slots and originating-request tool actions.

These are server decisions and attributed host observations. They constrain a
conforming managed client; they do not control arbitrary local CPU or credentials
outside this gateway. No tool arguments, model input or source bytes enter it.
"""

from __future__ import annotations

import re

from psycopg.types.json import Jsonb

from .models import OWNER_EFFECTS, AccessDenied, Conflict, InvalidRequest, digest, identifier
from .monitoring import RunMonitoring
from .fences import ResourceFences, require_unfenced


class ManagedResources:
    """Exact, once-only remote admissions with conservative unresolved accounting."""

    def __init__(self, hosts):
        self.hosts, self.store = hosts, hosts.store

    def fence_absent(self, principal, *, resource_kind, resource_id, lease_id):
        return ResourceFences(self.hosts).fence_absent(principal, resource_kind=resource_kind, resource_id=resource_id, lease_id=lease_id)

    @staticmethod
    def _sha(value):
        if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise InvalidRequest("invalid content identity")

    def _lease(self, connection, host, lease_id):
        row, policy = self.hosts._lease(connection, host, lease_id)
        if row["runner_protocol"] != 2:
            raise AccessDenied("protocol 2 admission is required")
        return row, policy

    @staticmethod
    def _slot(connection, host, worker_id, *, live=True):
        identifier(worker_id)
        if live:
            require_unfenced(connection, host["tenant_id"], "worker", worker_id)
        slot = connection.execute("SELECT * FROM sonn_governance.worker_slots WHERE tenant_id=%s AND worker_id=%s AND host_id=%s FOR UPDATE",
                                  (host["tenant_id"], worker_id, host["host_id"])).fetchone()
        if not slot or live and (slot["state"] != "held" or slot["host_generation"] != host["generation"]):
            raise AccessDenied("resource is unavailable")
        return slot

    @staticmethod
    def _replay(connection, host, namespace, command_id, semantics):
        row = connection.execute("SELECT sha256,result FROM sonn_governance.resource_receipts WHERE tenant_id=%s AND host_id=%s AND namespace=%s AND command_id=%s",
            (host["tenant_id"], host["host_id"], namespace, command_id)).fetchone()
        if row and row["sha256"] != digest(semantics):
            raise Conflict("resource identity has different semantics")
        return row["result"] if row else None

    def _record(self, connection, principal, host, namespace, command_id, semantics, result, lease_id):
        audit_id = self.store._audit(connection, principal, host["tenant_id"], operation=namespace, project_id=host["project_id"], semantics_sha256=digest(semantics))
        connection.execute("INSERT INTO sonn_governance.resource_receipts VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (host["tenant_id"], host["host_id"], namespace, command_id, digest(semantics), Jsonb(result), audit_id))
        self.hosts._certificate(connection, principal)
        self._lease(connection, host, lease_id)
        return result

    def reserve_worker(self, principal, *, lease_id, binding_id, worker_id, kind):
        """Reserve a project slot; a lost response never permits another launch."""
        for value in (lease_id, binding_id, worker_id):
            identifier(value)
        if type(kind) is not str or kind not in {"worker", "coordinator"}:
            raise InvalidRequest("invalid worker kind")
        semantics = {"binding_id": binding_id, "worker_id": worker_id, "kind": kind}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            _, policy = self._lease(connection, host, lease_id)
            binding = RunMonitoring._binding(connection, host["tenant_id"], binding_id, host)
            require_unfenced(connection, host["tenant_id"], "worker", worker_id)
            if binding["host_generation"] != host["generation"]:
                raise AccessDenied("resource is unavailable")
            replay = self._replay(connection, host, "reserve_worker", worker_id, semantics)
            if replay:
                return {**replay, "dispatch_permitted": False}
            if connection.execute("SELECT 1 FROM sonn_governance.worker_slots WHERE tenant_id=%s AND worker_id=%s", (host["tenant_id"], worker_id)).fetchone():
                raise AccessDenied("resource is unavailable")
            used = connection.execute("SELECT count(*) AS n FROM sonn_governance.worker_slots WHERE tenant_id=%s AND project_id=%s AND kind=%s AND state IN ('held','uncertain')",
                                      (host["tenant_id"], host["project_id"], kind)).fetchone()["n"]
            maximum = policy["document"]["max_workers"] if kind == "worker" else 1
            if used >= maximum:
                raise Conflict("project worker allowance exhausted")
            connection.execute("INSERT INTO sonn_governance.worker_slots(tenant_id,project_id,worker_id,binding_id,host_id,host_generation,kind,state) VALUES(%s,%s,%s,%s,%s,%s,%s,'held')",
                               (host["tenant_id"], host["project_id"], worker_id, binding_id, host["host_id"], host["generation"], kind))
            return self._record(connection, principal, host, "reserve_worker", worker_id, semantics,
                                {"worker_id": worker_id, "state": "held", "dispatch_permitted": True}, lease_id)

    def observe_worker(self, principal, *, worker_id, outcome):
        """A slot remains held until an explicit stopped/not-started host observation."""
        if type(outcome) is not str or outcome not in {"stopped", "never_started", "uncertain"}:
            raise InvalidRequest("invalid worker observation")
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            # Keep the common tenant->host->project->slot order used by admission.
            connection.execute("SELECT project_id FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR UPDATE", (host["tenant_id"], host["project_id"]))
            slot = self._slot(connection, host, worker_id, live=False)
            if slot["state"] in {"stopped", "never_started"} and slot["state"] != outcome:
                raise Conflict("terminal worker observation is immutable")
            if outcome == "never_started" and connection.execute("SELECT 1 FROM sonn_governance.request_bindings WHERE tenant_id=%s AND worker_id=%s", (host["tenant_id"], worker_id)).fetchone():
                raise Conflict("worker already owns a bound request")
            if slot["state"] != outcome:
                connection.execute("UPDATE sonn_governance.worker_slots SET state=%s,observed_at=clock_timestamp() WHERE tenant_id=%s AND worker_id=%s", (outcome, host["tenant_id"], worker_id))
                self.store._audit(connection, principal, host["tenant_id"], operation="observe_worker", project_id=host["project_id"], semantics_sha256=digest({"worker_id": worker_id, "outcome": outcome}))
            self.hosts._certificate(connection, principal)
            return {"worker_id": worker_id, "state": outcome, "slot_held": outcome == "uncertain", "source": "authenticated_host_observation"}

    def bind_request(self, principal, *, lease_id, worker_id, request_id, purpose, model, input_sha256):
        """Bind a reserved request before start; current policy preserves exact model."""
        for value in (lease_id, worker_id, request_id):
            identifier(value)
        self._sha(input_sha256)
        if (type(purpose) is not str or purpose not in {"primary", "planning", "compression"}
                or type(model) is not dict or set(model) != {"provider", "model"}
                or any(type(value) is not str or not 1 <= len(value) <= 256 for value in model.values())):
            raise InvalidRequest("invalid request binding")
        semantics = {"worker_id": worker_id, "request_id": request_id, "purpose": purpose, "model": model, "input_sha256": input_sha256}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            _, policy = self._lease(connection, host, lease_id)
            self._slot(connection, host, worker_id)
            request = self.hosts._request(connection, host, request_id)
            if str(request["lease_id"]) != lease_id or model not in policy["document"]["allowed_models"]:
                raise AccessDenied("resource is unavailable")
            replay = self._replay(connection, host, "bind_request", request_id, semantics)
            if replay:
                return replay
            if request["state"] != "reserved":
                raise Conflict("only a reserved request can be bound")
            from .managed_collaboration import ManagedCollaboration
            ManagedCollaboration.require_request_allowance(connection, host, worker_id)
            connection.execute("INSERT INTO sonn_governance.request_bindings VALUES(%s,%s,%s,%s,%s,%s)",
                               (host["tenant_id"], request_id, worker_id, purpose, Jsonb(model), input_sha256))
            return self._record(connection, principal, host, "bind_request", request_id, semantics,
                                {"request_id": request_id, "worker_id": worker_id, "purpose": purpose, "bound": True}, lease_id)

    @classmethod
    def require_bound_start(cls, connection, host, request, policy):
        """Called by the existing request start transaction for protocol-2 leases."""
        binding = connection.execute("SELECT * FROM sonn_governance.request_bindings WHERE tenant_id=%s AND request_id=%s", (host["tenant_id"], request["request_id"])).fetchone()
        if not binding or binding["model"] not in policy["document"]["allowed_models"]:
            raise AccessDenied("request binding is unavailable")
        cls._slot(connection, host, str(binding["worker_id"]))

    def authorize_tool(self, principal, *, lease_id, worker_id, request_id, action_id, tool_name, arguments_sha256):
        """Admit one action tied to a completed primary request of this exact worker."""
        for value in (lease_id, worker_id, request_id, action_id):
            identifier(value)
        self._sha(arguments_sha256)
        if type(tool_name) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool_name):
            raise InvalidRequest("invalid tool identity")
        semantics = {"worker_id": worker_id, "request_id": request_id, "action_id": action_id, "tool_name": tool_name, "arguments_sha256": arguments_sha256}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            _, policy = self._lease(connection, host, lease_id)
            self._slot(connection, host, worker_id)
            request = self.hosts._request(connection, host, request_id)
            binding = connection.execute("SELECT * FROM sonn_governance.request_bindings WHERE tenant_id=%s AND request_id=%s", (host["tenant_id"], request_id)).fetchone()
            if (request["state"] != "completed" or not binding or str(binding["worker_id"]) != worker_id
                    or binding["purpose"] != "primary" or binding["model"] not in policy["document"]["allowed_models"]
                    or tool_name not in policy["document"]["allowed_tools"]):
                raise AccessDenied("originating request or tool permission is unavailable")
            replay = self._replay(connection, host, "authorize_tool", action_id, semantics)
            require_unfenced(connection, host["tenant_id"], "tool", action_id)
            if replay:
                return {**replay, "dispatch_permitted": False}
            if connection.execute("SELECT 1 FROM sonn_governance.tool_admissions WHERE tenant_id=%s AND action_id=%s", (host["tenant_id"], action_id)).fetchone():
                raise AccessDenied("resource is unavailable")
            if connection.execute("SELECT count(*) AS n FROM sonn_governance.tool_admissions WHERE tenant_id=%s AND request_id=%s", (host["tenant_id"], request_id)).fetchone()["n"] >= 100:
                raise Conflict("originating request action limit exhausted")
            connection.execute("INSERT INTO sonn_governance.tool_admissions(tenant_id,action_id,worker_id,request_id,tool_name,arguments_sha256,state) VALUES(%s,%s,%s,%s,%s,%s,'admitted')",
                               (host["tenant_id"], action_id, worker_id, request_id, tool_name, arguments_sha256))
            return self._record(connection, principal, host, "authorize_tool", action_id, semantics,
                                {"action_id": action_id, "state": "admitted", "dispatch_permitted": True}, lease_id)

    def observe_tool(self, principal, *, action_id, outcome):
        """Retain actual host completion/uncertainty, without another dispatch permit."""
        identifier(action_id)
        if type(outcome) is not str or outcome not in {"completed", "uncertain"}:
            raise InvalidRequest("invalid action observation")
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            action = connection.execute("SELECT a.* FROM sonn_governance.tool_admissions a JOIN sonn_governance.worker_slots w USING(tenant_id,worker_id) "
                "WHERE a.tenant_id=%s AND a.action_id=%s AND w.host_id=%s FOR UPDATE OF a", (host["tenant_id"], action_id, host["host_id"])).fetchone()
            if not action:
                raise AccessDenied("resource is unavailable")
            if action["state"] == "completed" and outcome != "completed":
                raise Conflict("terminal action observation is immutable")
            if action["state"] != outcome:
                connection.execute("UPDATE sonn_governance.tool_admissions SET state=%s,observed_at=clock_timestamp() WHERE tenant_id=%s AND action_id=%s", (outcome, host["tenant_id"], action_id))
                self.store._audit(connection, principal, host["tenant_id"], operation="observe_tool", project_id=host["project_id"], semantics_sha256=digest({"action_id": action_id, "outcome": outcome}))
            self.hosts._certificate(connection, principal)
            return {"action_id": action_id, "state": outcome, "source": "authenticated_host_observation", "dispatch_permitted": False}

    def authorize_effect(self, principal, *, lease_id, binding_id, effect_id, kind, semantics_sha256):
        """Admit an owner Git/check invocation, without borrowing model authority.

        Only an explicit v2 policy and the enrolled owner's current control grant
        authorize these effects. The digest binds the local immutable invocation;
        command arguments and paths remain in the host's private journal.
        """
        for value in (lease_id, binding_id, effect_id):
            identifier(value)
        self._sha(semantics_sha256)
        if type(kind) is not str or kind not in OWNER_EFFECTS:
            raise InvalidRequest("invalid owner effect kind")
        semantics = {"binding_id": binding_id, "effect_id": effect_id, "kind": kind, "semantics_sha256": semantics_sha256}
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal)
            _, policy = self._lease(connection, host, lease_id)
            binding = RunMonitoring._binding(connection, host["tenant_id"], binding_id, host)
            if (binding["host_generation"] != host["generation"]
                    or policy["document"].get("policy_version") != 2
                    or kind not in policy["document"].get("allowed_effects", [])
                    or not self.store._has_permission(connection, host["tenant_id"], host["enrolled_by"], host["project_id"], "control_execute")):
                raise AccessDenied("owner effect permission is unavailable")
            replay = self._replay(connection, host, "authorize_effect", effect_id, semantics)
            require_unfenced(connection, host["tenant_id"], "effect", effect_id)
            if replay:
                return {**replay, "dispatch_permitted": False}
            if connection.execute("SELECT 1 FROM sonn_governance.owner_effects WHERE tenant_id=%s AND effect_id=%s", (host["tenant_id"], effect_id)).fetchone():
                raise AccessDenied("resource is unavailable")
            connection.execute("INSERT INTO sonn_governance.owner_effects(tenant_id,project_id,effect_id,binding_id,host_id,host_generation,kind,semantics_sha256,state) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'admitted')", (host["tenant_id"], host["project_id"], effect_id, binding_id,
                    host["host_id"], host["generation"], kind, semantics_sha256))
            return self._record(connection, principal, host, "authorize_effect", effect_id, semantics,
                {"effect_id": effect_id, "state": "admitted", "dispatch_permitted": True}, lease_id)

    def observe_effect(self, principal, *, effect_id, outcome):
        """Record host-attributed cleanup after revocation; never return a permit."""
        identifier(effect_id)
        if type(outcome) is not str or outcome not in {"completed", "failed", "never_started", "uncertain"}:
            raise InvalidRequest("invalid owner effect observation")
        with self.store._connection() as connection:
            host = self.hosts._host(connection, principal, observation=True)
            effect = connection.execute("SELECT * FROM sonn_governance.owner_effects WHERE tenant_id=%s AND effect_id=%s AND host_id=%s FOR UPDATE",
                (host["tenant_id"], effect_id, host["host_id"])).fetchone()
            if effect is None:
                raise AccessDenied("resource is unavailable")
            if effect["state"] in {"completed", "failed", "never_started"} and effect["state"] != outcome:
                raise Conflict("terminal owner effect observation is immutable")
            if effect["state"] != outcome:
                connection.execute("UPDATE sonn_governance.owner_effects SET state=%s,observed_at=clock_timestamp() WHERE tenant_id=%s AND effect_id=%s",
                    (outcome, host["tenant_id"], effect_id))
                self.store._audit(connection, principal, host["tenant_id"], operation="observe_effect", project_id=host["project_id"],
                    semantics_sha256=digest({"effect_id": effect_id, "outcome": outcome}))
            self.hosts._certificate(connection, principal)
            return {"effect_id": effect_id, "state": outcome, "source": "authenticated_host_observation", "dispatch_permitted": False}
