"""Bilateral managed sharing with encrypted selected content and separate payers.

The service admits disclosure and receiver-owned work, not task completion. Host
observations remain attributed observations. A grant never shares transcripts,
credentials, filesystem access or authority to control the other run.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from .content import ContentKeys, ContentUnavailable
from .content_screening import screen
from .models import AccessDenied, Conflict, InvalidRequest, bounded_text, canonical_json, digest, identifier
from .monitoring import RunMonitoring

KINDS = frozenset({"question", "finding", "artifact_offer", "work_request", "work_result"})
CLASSES = frozenset({"summary", "code", "artifact_reference"})
_BOUNDS = {"max_messages": (1, 1000), "max_total_bytes": (1, 1048576),
           "max_message_bytes": (1, 8192), "max_requests": (0, 1000),
           "max_hops": (1, 8), "max_fanout": (1, 8)}
_DENIED = "sharing resource is unavailable"


def _choices(value, choices):
    if (type(value) is not list or not value
            or any(type(item) is not str or item not in choices for item in value)
            or len(value) != len(set(value))):
        raise InvalidRequest("invalid sharing choices")
    return sorted(value)


def _bounds(value):
    for name, (low, high) in _BOUNDS.items():
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise InvalidRequest("invalid sharing limit")
    if value["max_message_bytes"] > value["max_total_bytes"]:
        raise InvalidRequest("message limit exceeds total disclosure limit")


def sharing_policy(value):
    """Validate the separate default-off project policy; no enrollment implied."""
    fields = {"version", "enabled", "allowed_peer_projects", "kinds", "data_classes", "max_ttl_seconds", *_BOUNDS}
    if type(value) is not dict or set(value) != fields or type(value["version"]) is not int or value["version"] != 1:
        raise InvalidRequest("invalid sharing policy fields")
    if type(value["enabled"]) is not bool:
        raise InvalidRequest("invalid sharing policy enabled flag")
    peers = value["allowed_peer_projects"]
    if type(peers) is not list or len(peers) > 32 or any(type(item) is not str for item in peers) or len(peers) != len(set(peers)):
        raise InvalidRequest("invalid sharing peer projects")
    for peer in peers:
        identifier(peer)
    if type(value["max_ttl_seconds"]) is not int or not 1 <= value["max_ttl_seconds"] <= 86400:
        raise InvalidRequest("invalid sharing duration")
    _bounds(value)
    return {**value, "allowed_peer_projects": sorted(peers), "kinds": _choices(value["kinds"], KINDS),
            "data_classes": _choices(value["data_classes"], CLASSES)}


def sharing_terms(value):
    """Exact selected purpose and ceilings; the receiver always pays for work."""
    fields = {"purpose", "kinds", "data_classes", "expires_at", "payer", "revokers", *_BOUNDS}
    if type(value) is not dict or set(value) != fields:
        raise InvalidRequest("invalid sharing terms fields")
    bounded_text(value["purpose"], 1000, "sharing purpose")
    if value["payer"] != "receiver" or value["revokers"] != "either_owner":
        raise InvalidRequest("invalid sharing ownership terms")
    if type(value["expires_at"]) is not int or not 1 <= value["expires_at"] <= 253402300799:
        raise InvalidRequest("invalid sharing expiry")
    _bounds(value)
    return {**value, "kinds": _choices(value["kinds"], KINDS), "data_classes": _choices(value["data_classes"], CLASSES)}


class ManagedCollaboration:
    """Current member/project/host/run checks precede every disclosure and replay."""

    def __init__(self, hosts, keys: ContentKeys):
        if not isinstance(keys, ContentKeys):
            raise InvalidRequest("explicit content keys are required")
        self.hosts, self.store, self.keys = hosts, hosts.store, keys

    @staticmethod
    def _lock(connection, tenant_id):
        # Serialize this bounded graph before locking either host. This prevents
        # opposite-direction offers from locking A then B / B then A.
        number = int.from_bytes(hashlib.sha256(f"sharing:{tenant_id}".encode()).digest()[:8], "big", signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (number,))

    def _begin(self, connection, principal, lease_id):
        identifier(lease_id)
        self.hosts._certificate(connection, principal)
        entry = connection.execute("SELECT tenant_id FROM sonn_governance.host_directory WHERE certificate_sha256=%s",
                                   (principal.certificate_sha256,)).fetchone()
        if not entry:
            raise AccessDenied(_DENIED)
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (str(entry["tenant_id"]),))
        # All governance mutators first hold this tenant row. Sharing upgrades
        # before any host/binding locks, avoiding host->binding inversions with
        # monitoring and opposite-direction pair admissions.
        connection.execute("SELECT tenant_id FROM sonn_governance.tenants WHERE tenant_id=%s FOR UPDATE", (entry["tenant_id"],))
        self._lock(connection, entry["tenant_id"])
        host = self.hosts._host(connection, principal)
        lease, _ = self.hosts._lease(connection, host, lease_id)
        if lease["runner_protocol"] != 2:
            raise AccessDenied(_DENIED)
        return host

    def _side(self, connection, tenant_id, binding_id):
        binding = RunMonitoring._binding(connection, tenant_id, binding_id, lock="SHARE")
        host = connection.execute("SELECT * FROM sonn_governance.hosts WHERE tenant_id=%s AND host_id=%s FOR SHARE",
                                  (tenant_id, binding["host_id"])).fetchone()
        member = connection.execute("SELECT active,provisioned_active FROM sonn_governance.memberships WHERE tenant_id=%s AND actor_id=%s",
                                    (tenant_id, binding["owner_actor"])).fetchone()
        if (not host or host["state"] != "active" or host["generation"] != binding["host_generation"]
                or host["enrolled_by"] != binding["owner_actor"] or not member or not member["active"] or not member["provisioned_active"]
                or any(not self.store._has_permission(connection, tenant_id, binding["owner_actor"], binding["project_id"], permission)
                       for permission in ("host_admin", "content_read", "content_write"))):
            raise AccessDenied(_DENIED)
        project = connection.execute("SELECT * FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR SHARE",
                                     (tenant_id, binding["project_id"])).fetchone()
        policy = connection.execute("SELECT * FROM sonn_governance.policies WHERE tenant_id=%s AND project_id=%s AND revision=%s",
                                    (tenant_id, binding["project_id"], project["policy_revision"])).fetchone() if project else None
        sharing = connection.execute("SELECT * FROM sonn_governance.sharing_policies WHERE tenant_id=%s AND project_id=%s ORDER BY revision DESC LIMIT 1",
                                     (tenant_id, binding["project_id"])).fetchone()
        projection = binding["projection"]
        now = self.hosts._now(connection)
        if (not project or not project["active"] or not policy or policy["document"].get("content_mode") != "explicit"
                or not sharing or not sharing["document"]["enabled"] or not projection or projection["state"] != "running"
                or not binding["last_contact_at"] or (now - binding["last_contact_at"]).total_seconds() > 60):
            raise AccessDenied(_DENIED)
        live_lease = connection.execute("SELECT 1 FROM sonn_governance.policy_leases WHERE tenant_id=%s AND host_id=%s AND host_generation=%s AND policy_revision=%s AND policy_sha256=%s AND runner_protocol=2 AND expires_at>clock_timestamp() LIMIT 1",
                                        (tenant_id, host["host_id"], host["generation"], policy["revision"], policy["sha256"])).fetchone()
        if not live_lease:
            raise AccessDenied(_DENIED)
        stopped = connection.execute("SELECT 1 FROM sonn_governance.remote_controls WHERE tenant_id=%s AND binding_id=%s AND operation='stop' AND expected_epoch=%s AND (outcome IN ('applied','uncertain') OR (outcome IS NULL AND expires_at>clock_timestamp())) LIMIT 1",
                                     (tenant_id, binding_id, projection["epoch"])).fetchone()
        if stopped:
            raise AccessDenied(_DENIED)
        captured = {"binding_id": binding_id, "host_id": str(host["host_id"]), "host_generation": host["generation"],
                    "owner_actor": binding["owner_actor"], "project_id": str(binding["project_id"]),
                    "session_id": str(binding["session_id"]), "epoch": projection["epoch"],
                    "policy_revision": policy["revision"], "sharing_revision": sharing["revision"]}
        return captured, sharing["document"]

    def _pair(self, connection, tenant_id, origin, receiver):
        # Bindings are supplied explicitly, never enumerated for discovery.
        source, a = self._side(connection, tenant_id, origin)
        target, b = self._side(connection, tenant_id, receiver)
        if origin == receiver or source["session_id"] == target["session_id"]:
            raise AccessDenied(_DENIED)
        if source["project_id"] != target["project_id"] and (
                target["project_id"] not in a["allowed_peer_projects"] or source["project_id"] not in b["allowed_peer_projects"]):
            raise AccessDenied(_DENIED)
        return [source, target], [a, b]

    @staticmethod
    def _owned(host, side):
        if side["host_id"] != str(host["host_id"]) or side["owner_actor"] != host["enrolled_by"]:
            raise AccessDenied(_DENIED)

    @staticmethod
    def _session(side):
        return digest({key: side[key] for key in ("owner_actor", "project_id", "session_id")})

    def _grant(self, connection, host, grant_id, *, approved=True):
        identifier(grant_id)
        row = connection.execute("SELECT * FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND grant_id=%s",
                                 (host["tenant_id"], grant_id)).fetchone()
        if not row or str(host["host_id"]) not in {side["host_id"] for side in row["captured"]}:
            raise AccessDenied(_DENIED)
        self._current(connection, host["tenant_id"], row, approved=approved)
        return row

    def _current(self, connection, tenant_id, row, *, approved=True):
        if row["deleted_at"] or row["revoked_at"] or row["expires_at"] <= self.hosts._now(connection) or approved and not row["approved_at"]:
            raise AccessDenied(_DENIED)
        captured, _ = self._pair(connection, tenant_id, str(row["origin_binding"]), str(row["receiver_binding"]))
        if captured != row["captured"]:
            raise AccessDenied(_DENIED)

    @staticmethod
    def _aad(tenant_id, object_id, kind, sha256):
        return canonical_json({"version": 1, "tenant": str(tenant_id), "id": str(object_id), "kind": kind, "sha256": sha256}).encode()

    def _decrypt(self, row, kind, sha_key):
        if row["deleted_at"]:
            raise AccessDenied(_DENIED)
        content = self.keys.decrypt(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]),
                                    self._aad(row["tenant_id"], row["grant_id"] if kind == "terms" else row["message_id"], kind, row[sha_key]))
        if hashlib.sha256(content).hexdigest() != row[sha_key]:
            raise ContentUnavailable("retained content is unavailable")
        return content.decode("utf-8")

    @staticmethod
    def _replay(connection, principal, tenant_id, command_id, semantics):
        identifier(command_id)
        row = connection.execute("SELECT sha256,result FROM sonn_governance.sharing_receipts WHERE tenant_id=%s AND actor_id=%s AND command_id=%s",
                                 (tenant_id, principal.actor_id, command_id)).fetchone()
        if row and row["sha256"] != digest(semantics):
            raise Conflict("sharing command identity has different semantics")
        return row["result"] if row else None

    def _record(self, connection, principal, host, command_id, semantics, result, operation, lease_id, grants=()):
        created = result.get("grant_id") if operation == "sharing_offer" else result.get("message_id") if operation == "sharing_send" else None
        audit = self.store._audit(connection, principal, host["tenant_id"], operation=operation,
                                  project_id=host["project_id"], semantics_sha256=digest(semantics),
                                  decision_id=created, revision=1 if created else None)
        connection.execute("INSERT INTO sonn_governance.sharing_receipts VALUES(%s,%s,%s,%s,%s,%s)",
                           (host["tenant_id"], principal.actor_id, command_id, digest(semantics), Jsonb(result), audit))
        self.hosts._certificate(connection, principal)
        self.hosts._lease(connection, host, lease_id)
        for grant in grants:
            self._current(connection, host["tenant_id"], grant, approved=bool(grant["approved_at"]))
        return result

    def set_policy(self, principal, *, tenant_id, project_id, command_id, expected_revision, policy):
        """Human policy administrator explicitly enables or revokes sharing."""
        for value in (tenant_id, project_id, command_id):
            identifier(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidRequest("invalid sharing policy revision")
        policy = sharing_policy(policy)
        semantics = {"operation": "sharing_policy", "project_id": project_id, "expected_revision": expected_revision, "policy": policy}
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, project_id, "policy_admin", edit_membership=True)
            self._lock(connection, tenant_id)
            replay = self._replay(connection, principal, tenant_id, command_id, semantics)
            if replay:
                self.store._identity(connection, principal)
                return replay
            previous = connection.execute("SELECT revision FROM sonn_governance.sharing_policies WHERE tenant_id=%s AND project_id=%s ORDER BY revision DESC LIMIT 1",
                                          (tenant_id, project_id)).fetchone()
            if (previous["revision"] if previous else 0) != expected_revision:
                raise Conflict("sharing policy revision changed")
            for peer in policy["allowed_peer_projects"]:
                if not connection.execute("SELECT 1 FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s AND active", (tenant_id, peer)).fetchone():
                    raise AccessDenied(_DENIED)
            revision = expected_revision + 1
            connection.execute("INSERT INTO sonn_governance.sharing_policies VALUES(%s,%s,%s,%s,%s)",
                               (tenant_id, project_id, revision, Jsonb(policy), digest(policy)))
            result = {"project_id": project_id, "revision": revision, "policy": policy}
            audit = self.store._audit(connection, principal, tenant_id, operation="sharing_policy", project_id=project_id, revision=revision, semantics_sha256=digest(semantics))
            connection.execute("INSERT INTO sonn_governance.sharing_receipts VALUES(%s,%s,%s,%s,%s,%s)",
                               (tenant_id, principal.actor_id, command_id, digest(semantics), Jsonb(result), audit))
            self.store._identity(connection, principal)
            return result

    def offer(self, principal, *, lease_id, command_id, origin_binding, receiver_binding, terms):
        """The origin explicitly selects a known receiver binding and exact terms."""
        for value in (command_id, origin_binding, receiver_binding):
            identifier(value)
        terms = sharing_terms(terms)
        semantics = {"operation": "sharing_offer", "origin_binding": origin_binding, "receiver_binding": receiver_binding, "terms": terms}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            captured, policies = self._pair(connection, host["tenant_id"], origin_binding, receiver_binding)
            self._owned(host, captured[0])
            now = self.hosts._now(connection).timestamp()
            for policy in policies:
                if (not now < terms["expires_at"] <= now + policy["max_ttl_seconds"]
                        or not set(terms["kinds"]) <= set(policy["kinds"])
                        or not set(terms["data_classes"]) <= set(policy["data_classes"])
                        or any(terms[key] > policy[key] for key in _BOUNDS)):
                    raise AccessDenied(_DENIED)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                self._grant(connection, host, replay["grant_id"], approved=False)
                return replay
            grant_id = str(uuid4())
            encoded = canonical_json(terms).encode()
            screen(encoded, "application/json")
            sha = hashlib.sha256(encoded).hexdigest()
            key, nonce, ciphertext = self.keys.encrypt(encoded, self._aad(host["tenant_id"], grant_id, "terms", sha))
            limits = {key: value for key, value in terms.items() if key != "purpose"}
            connection.execute("INSERT INTO sonn_governance.sharing_grants(tenant_id,grant_id,origin_binding,receiver_binding,captured,limits,terms_sha256,key_id,nonce,ciphertext,expires_at) "
                               "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (host["tenant_id"], grant_id, origin_binding, receiver_binding, Jsonb(captured), Jsonb(limits), sha, key, nonce, ciphertext,
                                datetime.fromtimestamp(terms["expires_at"], timezone.utc)))
            row = self._grant(connection, host, grant_id, approved=False)
            return self._record(connection, principal, host, command_id, semantics,
                                {"grant_id": grant_id, "terms_sha256": sha, "state": "offered"}, "sharing_offer", lease_id, [row])

    def inspect_policy(self, principal, *, tenant_id, project_id):
        """Explicit policy-admin inspection; an absent policy means disabled."""
        for value in (tenant_id, project_id):
            identifier(value)
        with self.store._connection() as connection:
            self.store._authorize(connection, principal, tenant_id, project_id, "policy_admin")
            row = connection.execute("SELECT revision,document FROM sonn_governance.sharing_policies WHERE tenant_id=%s AND project_id=%s ORDER BY revision DESC LIMIT 1", (tenant_id, project_id)).fetchone()
            self.store._audit(connection, principal, tenant_id, operation="sharing_inspect", project_id=project_id)
            self.store._identity(connection, principal)
            return {"project_id": project_id, "revision": row["revision"] if row else 0,
                    "policy": row["document"] if row else None, "enabled": bool(row and row["document"]["enabled"])}

    def approve(self, principal, *, lease_id, command_id, grant_id, terms_sha256):
        """Receiver approval binds the selected purpose and all immutable limits."""
        semantics = {"operation": "sharing_approve", "grant_id": grant_id, "terms_sha256": terms_sha256}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            grant = self._grant(connection, host, grant_id, approved=False)
            self._owned(host, grant["captured"][1])
            if terms_sha256 != grant["terms_sha256"]:
                raise Conflict("sharing terms changed")
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return replay
            connection.execute("UPDATE sonn_governance.sharing_grants SET approved_at=COALESCE(approved_at,clock_timestamp()) WHERE tenant_id=%s AND grant_id=%s", (host["tenant_id"], grant_id))
            return self._record(connection, principal, host, command_id, semantics,
                                {"grant_id": grant_id, "state": "approved"}, "sharing_approve", lease_id, [grant])

    def revoke(self, principal, *, lease_id, command_id, grant_id):
        """Either captured owner closes future disclosure without killing peer work."""
        semantics = {"operation": "sharing_revoke", "grant_id": grant_id}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            identifier(grant_id)
            grant = connection.execute("SELECT * FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND grant_id=%s", (host["tenant_id"], grant_id)).fetchone()
            if not grant or not any(side["host_id"] == str(host["host_id"]) and side["owner_actor"] == host["enrolled_by"] for side in grant["captured"]):
                raise AccessDenied(_DENIED)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return replay
            connection.execute("UPDATE sonn_governance.sharing_grants SET revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE tenant_id=%s AND grant_id=%s", (host["tenant_id"], grant_id))
            return self._record(connection, principal, host, command_id, semantics,
                                {"grant_id": grant_id, "state": "revoked"}, "sharing_revoke", lease_id)

    def _ancestors(self, connection, tenant_id, lineage):
        if not 1 <= len(lineage) <= 8 or len(set(lineage)) != len(lineage):
            raise AccessDenied(_DENIED)
        result = []
        for grant_id in lineage:
            row = connection.execute("SELECT * FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND grant_id=%s", (tenant_id, grant_id)).fetchone()
            if not row:
                raise AccessDenied(_DENIED)
            self._current(connection, tenant_id, row)
            result.append(row)
        return result

    def send(self, principal, *, lease_id, command_id, grant_id, kind, data_class, body, parent_id=None):
        """Queue only selected text/reference metadata; ancestor ceilings accumulate."""
        if type(kind) is not str or kind not in KINDS or type(data_class) is not str or data_class not in CLASSES:
            raise InvalidRequest("invalid sharing message kind")
        bounded_text(body, 8192, "sharing message")
        encoded = body.encode()
        screen(encoded, "text/plain")
        if data_class == "artifact_reference":
            try:
                ref = json.loads(body)
                if (type(ref) is not dict or set(ref) != {"artifact_id", "sha256", "byte_size"}
                        or type(ref["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"])
                        or type(ref["byte_size"]) is not int or not 0 <= ref["byte_size"] <= 2**53 - 1):
                    raise ValueError
                identifier(ref["artifact_id"])
            except (ValueError, TypeError):
                raise InvalidRequest("artifact reference contains unsupported metadata") from None
        semantics = {"operation": "sharing_send", "grant_id": grant_id, "kind": kind, "data_class": data_class,
                     "body_sha256": hashlib.sha256(encoded).hexdigest(), "parent_id": parent_id}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            grant = self._grant(connection, host, grant_id)
            self._owned(host, grant["captured"][0])
            lineage, path = [grant_id], [self._session(side) for side in grant["captured"]]
            if parent_id is not None:
                identifier(parent_id)
                parent = connection.execute("SELECT * FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND message_id=%s", (host["tenant_id"], parent_id)).fetchone()
                parent_grant = connection.execute("SELECT receiver_binding FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND grant_id=%s", (host["tenant_id"], parent["grant_id"])).fetchone() if parent else None
                if not parent or parent["deleted_at"] or not parent["delivered_at"] or parent_grant["receiver_binding"] != grant["origin_binding"]:
                    raise AccessDenied(_DENIED)
                lineage = parent["lineage"] + [grant_id]
                path = parent["binding_path"] + [self._session(grant["captured"][1])]
            if len(set(path)) != len(path):
                raise Conflict("sharing causal cycle is not allowed")
            ancestors = self._ancestors(connection, host["tenant_id"], lineage)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                current = connection.execute("SELECT deleted_at FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND message_id=%s",
                                             (host["tenant_id"], replay["message_id"])).fetchone()
                if not current or current["deleted_at"]:
                    raise AccessDenied(_DENIED)
                return replay
            for ancestor in ancestors:
                limit = ancestor["limits"]
                if (kind not in limit["kinds"] or data_class not in limit["data_classes"] or len(encoded) > limit["max_message_bytes"]
                        or len(lineage) > limit["max_hops"]):
                    raise AccessDenied(_DENIED)
                totals = connection.execute("SELECT count(*) AS n,COALESCE(sum(byte_size),0) AS bytes FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND lineage ? %s",
                                            (host["tenant_id"], str(ancestor["grant_id"]))).fetchone()
                if totals["n"] >= limit["max_messages"] or totals["bytes"] + len(encoded) > limit["max_total_bytes"]:
                    raise Conflict("sharing disclosure allowance exhausted")
                recipients = connection.execute("SELECT DISTINCT binding_path->>-1 AS recipient FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND lineage ? %s",
                                                (host["tenant_id"], str(ancestor["grant_id"]))).fetchall()
                if len({row["recipient"] for row in recipients} | {path[-1]}) > limit["max_fanout"]:
                    raise Conflict("sharing cumulative fanout allowance exhausted")
                if parent_id:
                    children = connection.execute("SELECT count(*) AS n FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND parent_id=%s", (host["tenant_id"], parent_id)).fetchone()["n"]
                    if children >= limit["max_fanout"]:
                        raise Conflict("sharing fanout allowance exhausted")
            message_id = str(uuid4())
            key, nonce, ciphertext = self.keys.encrypt(encoded, self._aad(host["tenant_id"], message_id, "message", semantics["body_sha256"]))
            connection.execute("INSERT INTO sonn_governance.sharing_messages(tenant_id,message_id,grant_id,parent_id,kind,data_class,body_sha256,byte_size,lineage,binding_path,key_id,nonce,ciphertext) "
                               "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (host["tenant_id"], message_id, grant_id, parent_id, kind, data_class, semantics["body_sha256"], len(encoded), Jsonb(lineage), Jsonb(path), key, nonce, ciphertext))
            return self._record(connection, principal, host, command_id, semantics,
                                {"message_id": message_id, "state": "queued"}, "sharing_send", lease_id, ancestors)

    def deliver(self, principal, *, lease_id, command_id, message_id):
        """Explicit receiver disclosure. Receipts contain no plaintext or filenames."""
        identifier(message_id)
        semantics = {"operation": "sharing_deliver", "message_id": message_id}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            message = connection.execute("SELECT * FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND message_id=%s", (host["tenant_id"], message_id)).fetchone()
            if not message or message["deleted_at"]:
                raise AccessDenied(_DENIED)
            grant = self._grant(connection, host, str(message["grant_id"]))
            self._owned(host, grant["captured"][1])
            ancestors = self._ancestors(connection, host["tenant_id"], message["lineage"])
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            body = self._decrypt(message, "message", "body_sha256")
            if not replay:
                connection.execute("UPDATE sonn_governance.sharing_messages SET delivered_at=COALESCE(delivered_at,clock_timestamp()) WHERE tenant_id=%s AND message_id=%s", (host["tenant_id"], message_id))
                self._record(connection, principal, host, command_id, semantics, {"message_id": message_id, "state": "delivered"}, "sharing_deliver", lease_id, ancestors)
            else:
                # A repeated content response is still a new disclosure. Re-run
                # the required archive gate, even for an identical command ID.
                self.store._audit(connection, principal, host["tenant_id"], operation="sharing_deliver",
                                  project_id=host["project_id"], semantics_sha256=digest(semantics))
                self.hosts._certificate(connection, principal)
                self.hosts._lease(connection, host, lease_id)
                for ancestor in ancestors:
                    self._current(connection, host["tenant_id"], ancestor)
            return {"message_id": message_id, "kind": message["kind"], "data_class": message["data_class"], "body": body,
                    "sha256": message["body_sha256"], "state": "delivered", "source": "peer_selected_content"}

    def accept_work(self, principal, *, lease_id, command_id, message_id, worker_id, request_limit, contract_sha256):
        """Bind an already reserved receiver worker; this never accepts its result."""
        for value in (message_id, worker_id):
            identifier(value)
        if type(request_limit) is not int or not 1 <= request_limit <= 1000 or type(contract_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", contract_sha256):
            raise InvalidRequest("invalid receiver work contract")
        semantics = {"operation": "sharing_accept", "message_id": message_id, "worker_id": worker_id,
                     "request_limit": request_limit, "contract_sha256": contract_sha256}
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            message = connection.execute("SELECT * FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND message_id=%s", (host["tenant_id"], message_id)).fetchone()
            if not message or message["deleted_at"] or message["kind"] != "work_request" or not message["delivered_at"]:
                raise AccessDenied(_DENIED)
            grant = self._grant(connection, host, str(message["grant_id"]))
            self._owned(host, grant["captured"][1])
            ancestors = self._ancestors(connection, host["tenant_id"], message["lineage"])
            slot = connection.execute("SELECT * FROM sonn_governance.worker_slots WHERE tenant_id=%s AND worker_id=%s FOR UPDATE", (host["tenant_id"], worker_id)).fetchone()
            if (not slot or slot["host_id"] != host["host_id"] or slot["host_generation"] != host["generation"]
                    or slot["binding_id"] != grant["receiver_binding"] or slot["state"] != "held" or slot["kind"] != "worker"):
                raise AccessDenied(_DENIED)
            replay = self._replay(connection, principal, host["tenant_id"], command_id, semantics)
            if replay:
                return {**replay, "dispatch_permitted": False}
            if connection.execute("SELECT 1 FROM sonn_governance.sharing_acceptances WHERE tenant_id=%s AND (message_id=%s OR worker_id=%s)", (host["tenant_id"], message_id, worker_id)).fetchone():
                raise Conflict("sharing work was already accepted")
            if connection.execute("SELECT 1 FROM sonn_governance.request_bindings WHERE tenant_id=%s AND worker_id=%s", (host["tenant_id"], worker_id)).fetchone():
                raise Conflict("receiver work has already started")
            edges = connection.execute("SELECT g.captured FROM sonn_governance.sharing_acceptances a JOIN sonn_governance.sharing_messages m USING(tenant_id,message_id) JOIN sonn_governance.sharing_grants g USING(tenant_id,grant_id) WHERE a.tenant_id=%s LIMIT 257", (host["tenant_id"],)).fetchall()
            if len(edges) > 256:
                raise Conflict("sharing dependency graph limit reached")
            edges = [(self._session(row["captured"][0]), self._session(row["captured"][1])) for row in edges]
            reached, pending = set(), [self._session(grant["captured"][1])]
            while pending:
                current = pending.pop()
                if current == self._session(grant["captured"][0]):
                    raise Conflict("sharing work dependency cycle is not allowed")
                if current not in reached:
                    reached.add(current)
                    pending.extend(target for source, target in edges if source == current)
            for ancestor in ancestors:
                reserved = connection.execute("SELECT COALESCE(sum(a.request_limit),0) AS n FROM sonn_governance.sharing_acceptances a JOIN sonn_governance.sharing_messages m USING(tenant_id,message_id) WHERE a.tenant_id=%s AND m.lineage ? %s", (host["tenant_id"], str(ancestor["grant_id"]))).fetchone()["n"]
                if reserved + request_limit > ancestor["limits"]["max_requests"]:
                    raise Conflict("sharing receiver request allowance exhausted")
            connection.execute("INSERT INTO sonn_governance.sharing_acceptances(tenant_id,message_id,receiver_binding,worker_id,request_limit,contract_sha256) VALUES(%s,%s,%s,%s,%s,%s)",
                               (host["tenant_id"], message_id, grant["receiver_binding"], worker_id, request_limit, contract_sha256))
            return self._record(connection, principal, host, command_id, semantics,
                                {"message_id": message_id, "worker_id": worker_id, "request_limit": request_limit,
                                 "state": "work_reserved", "dispatch_permitted": True}, "sharing_accept", lease_id, ancestors)

    @staticmethod
    def require_request_allowance(connection, host, worker_id):
        """Called inside bind_request after replay, before insert; all purposes count.

        Past acceptances remain bounded after origin Stop/revocation. They use the
        receiver's normal current policy/lease and project request accounting.
        """
        accepted = connection.execute("SELECT request_limit FROM sonn_governance.sharing_acceptances WHERE tenant_id=%s AND worker_id=%s", (host["tenant_id"], worker_id)).fetchone()
        if accepted:
            used = connection.execute("SELECT count(*) AS n FROM sonn_governance.request_bindings WHERE tenant_id=%s AND worker_id=%s", (host["tenant_id"], worker_id)).fetchone()["n"]
            if used >= accepted["request_limit"]:
                raise Conflict("sharing receiver request allowance exhausted")

    def inspect(self, principal, *, lease_id, binding_id, before_grant_id=None, limit=20):
        """Bounded captured-run metadata; does not discover peers or disclose text."""
        identifier(binding_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InvalidRequest("invalid sharing page size")
        if before_grant_id is not None:
            identifier(before_grant_id)
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            binding = RunMonitoring._binding(connection, host["tenant_id"], binding_id, host, lock="SHARE")
            if binding["owner_actor"] != host["enrolled_by"]:
                raise AccessDenied(_DENIED)
            rows = connection.execute("SELECT grant_id,origin_binding,receiver_binding,approved_at,revoked_at,expires_at,deleted_at FROM sonn_governance.sharing_grants WHERE tenant_id=%s AND (origin_binding=%s OR receiver_binding=%s) AND (%s::uuid IS NULL OR grant_id<%s::uuid) ORDER BY grant_id DESC LIMIT %s",
                                      (host["tenant_id"], binding_id, binding_id, before_grant_id, before_grant_id, limit + 1)).fetchall()
            now = self.hosts._now(connection)
            items = [{"grant_id": str(row["grant_id"]), "direction": "outgoing" if str(row["origin_binding"]) == binding_id else "incoming",
                      "state": "deleted" if row["deleted_at"] else "revoked" if row["revoked_at"] else "expired" if row["expires_at"] <= now else "approved" if row["approved_at"] else "offered"} for row in rows[:limit]]
            self.store._audit(connection, principal, host["tenant_id"], operation="sharing_inspect", project_id=host["project_id"])
            self.hosts._certificate(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            return {"items": items, "next_cursor": items[-1]["grant_id"] if len(rows) > limit else None}

    def inspect_terms(self, principal, *, lease_id, grant_id):
        """Explicit audited purpose disclosure to a currently authorized endpoint."""
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            grant = self._grant(connection, host, grant_id, approved=False)
            terms = json.loads(self._decrypt(grant, "terms", "terms_sha256"))
            self.store._audit(connection, principal, host["tenant_id"], operation="sharing_terms", project_id=host["project_id"])
            self.hosts._certificate(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            self._current(connection, host["tenant_id"], grant, approved=False)
            return {"grant_id": grant_id, "terms": terms, "terms_sha256": grant["terms_sha256"],
                    "state": "approved" if grant["approved_at"] else "offered"}

    def inspect_messages(self, principal, *, lease_id, grant_id, before_message_id=None, limit=20):
        """Selected-grant message IDs, never automatic content delivery."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InvalidRequest("invalid sharing page size")
        if before_message_id is not None:
            identifier(before_message_id)
        with self.store._connection() as connection:
            host = self._begin(connection, principal, lease_id)
            grant = self._grant(connection, host, grant_id)
            rows = connection.execute("SELECT message_id,kind,data_class,delivered_at,deleted_at FROM sonn_governance.sharing_messages WHERE tenant_id=%s AND grant_id=%s AND (%s::uuid IS NULL OR message_id<%s::uuid) ORDER BY message_id DESC LIMIT %s",
                                      (host["tenant_id"], grant_id, before_message_id, before_message_id, limit + 1)).fetchall()
            self.store._audit(connection, principal, host["tenant_id"], operation="sharing_inspect", project_id=host["project_id"])
            self.hosts._certificate(connection, principal)
            self.hosts._lease(connection, host, lease_id)
            self._current(connection, host["tenant_id"], grant)
            items = [{"message_id": str(row["message_id"]), "kind": row["kind"], "data_class": row["data_class"],
                      "state": "deleted" if row["deleted_at"] else "delivered" if row["delivered_at"] else "queued"} for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["message_id"] if len(rows) > limit else None}
