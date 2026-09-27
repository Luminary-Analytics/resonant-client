"""Explicit local run-to-run collaboration; no discovery or worker dispatch.

Trusted callers supply each operation's captured RunAuthority. Messages are
untrusted proposals, never supervisor commands. Both runs approve immutable
disclosure terms; receiver acceptance creates its own scoped, budgeted attempt.
This initial capability is restricted to one owner, project and SQLite store.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math

from .models import (AdmissionClosed, CommandReceipt, Conflict, RevisionConflict,
                     RunAuthority, Scope, ScopeDenied, require_id)
from .store import _id, canonical_json

KINDS = frozenset({"question", "finding", "artifact_offer", "work_request", "work_result"})
DATA_CLASSES = frozenset({"summary", "code", "artifact_reference"})


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value, limit, label):
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(f"Invalid bounded {label}")


@dataclass(frozen=True, slots=True)
class CollaborationTerms:
    """Owner-approved ceilings; request units are not token or dollar budgets."""

    purpose: str
    kinds: frozenset[str]
    data_classes: frozenset[str]
    expires_at: float
    revoker_run_ids: frozenset[str]
    max_messages: int = 20
    max_total_bytes: int = 65536
    max_message_bytes: int = 8000
    max_hops: int = 1
    max_fanout: int = 1
    max_requests: int = 4
    cost_limit_usd: None = None
    payer: str = "receiving_run"
    cancellation: str = "close_future_admission"

    def __post_init__(self):
        _text(self.purpose, 2000, "purpose")
        for field, allowed in (("kinds", KINDS), ("data_classes", DATA_CLASSES)):
            value = getattr(self, field)
            if type(value) is not frozenset or not value or not value <= allowed:
                raise ValueError(f"Explicit supported {field} required")
        if type(self.revoker_run_ids) is not frozenset or not 1 <= len(self.revoker_run_ids) <= 2:
            raise ValueError("Specify one or both run revokers")
        for value in self.revoker_run_ids:
            require_id(value)
        for field, minimum, maximum in (("max_messages", 1, 1000), ("max_total_bytes", 1, 1048576),
                ("max_message_bytes", 1, 65536), ("max_hops", 1, 8), ("max_fanout", 1, 8), ("max_requests", 0, 1000)):
            value = getattr(self, field)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Invalid {field}")
        if (type(self.expires_at) not in (float, int) or not math.isfinite(self.expires_at)
                or self.max_message_bytes > self.max_total_bytes or self.cost_limit_usd is not None
                or self.payer != "receiving_run" or self.cancellation != "close_future_admission"):
            raise ValueError("Unsupported expiry, budget or cancellation contract")

    def to_dict(self):
        value = asdict(self)
        for field in ("kinds", "data_classes", "revoker_run_ids"):
            value[field] = sorted(value[field])
        return value

    @classmethod
    def from_dict(cls, value):
        fields = set(cls.__dataclass_fields__)
        if type(value) is not dict or set(value) != fields:
            raise ValueError("Invalid collaboration terms fields")
        result = dict(value)
        for field in ("kinds", "data_classes", "revoker_run_ids"):
            items = result[field]
            if type(items) is not list or any(type(item) is not str for item in items) or len(items) != len(set(items)):
                raise ValueError("Terms need unique string lists")
            result[field] = frozenset(items)
        return cls(**result)


class SwarmCollaboration:
    """Short atomic transactions; no callbacks, provider access or peer authority."""

    def __init__(self, supervisor):
        self.supervisor, self.store = supervisor, supervisor.store

    @staticmethod
    def require_unbound_assignment(connection, work_item_id):
        """Native retries need a fresh explicit request, never a second allocation."""
        if connection.execute("SELECT 1 FROM collaboration_acceptances WHERE work_item_id=?", (work_item_id,)).fetchone():
            raise Conflict("Collaborative work already has its accepted attempt; a fresh request or independent task is required")

    def _run(self, connection, authority):
        if type(authority) is not RunAuthority:
            raise ScopeDenied("Captured supervisor authority required")
        run = self.store._authority(connection, authority)
        if not run["managed"]:
            raise ScopeDenied("Managed run required")
        return run

    @staticmethod
    def _same_owner(first, second):
        if (first["tenant_id"] != f"personal:{first['owner_id']}" or second["tenant_id"] != f"personal:{second['owner_id']}"
                or any(first[field] != second[field] for field in ("tenant_id", "owner_id", "project_id"))
                or first["id"] == second["id"] or first["session_id"] == second["session_id"]):
            raise ScopeDenied("Collaboration requires distinct conversations with the same owner and project")

    def _grant(self, connection, run, grant_id, *, live=True, offered=False):
        require_id(grant_id)
        grant = connection.execute("SELECT * FROM collaboration_grants WHERE id=? AND (origin_run_id=? OR receiver_run_id=?)",
                                   (grant_id, run["id"], run["id"])).fetchone()
        if grant is None:
            raise ScopeDenied("Collaboration grant unavailable")
        terms = CollaborationTerms.from_dict(json.loads(grant["terms_json"]))
        if _digest(terms.to_dict()) != grant["terms_sha256"]:
            raise Conflict("Retained grant terms changed")
        if live:
            if grant["state"] not in ({"offered", "active"} if offered else {"active"}) or terms.expires_at <= self.store.clock():
                raise AdmissionClosed("Collaboration grant is inactive or expired")
            for side in ("origin", "receiver"):
                peer = connection.execute("SELECT * FROM runs WHERE id=?", (grant[f"{side}_run_id"],)).fetchone()
                if (peer is None or peer["epoch"] != grant[f"{side}_epoch"]
                        or _digest(json.loads(peer["policy_json"])) != grant[f"{side}_policy_sha256"]
                        or peer["lease_until"] <= self.store.clock()):
                    raise AdmissionClosed("Collaboration participant authority changed or expired")
                self.store._admitting(peer)
                if peer["id"] != run["id"]:
                    self._same_owner(run, peer)
        return grant, terms

    def _replay(self, connection, run, authority, command_id, revision, semantics):
        require_id(command_id)
        if type(revision) is not int or revision < 0:
            raise ValueError("Explicit run revision required")
        result = self.store._duplicate(connection, run["id"], f"collaboration:{authority.supervisor_id}", command_id, semantics)
        if result is not None:
            return CommandReceipt(**result)
        if run["revision"] != revision:
            raise RevisionConflict("Run revision changed; refresh before collaboration")
        return None

    def _finish(self, connection, run, authority, command_id, semantics, result):
        # Time may advance during contract validation; no late successful write
        # can extend the captured supervisor lease or collaboration expiry.
        self._run(connection, authority)
        if semantics["operation"] in {"offer", "accept_grant", "send"}:
            self._grant(connection, run, result.get("grant_id", semantics.get("grant_id")), offered=semantics["operation"] == "offer")
            if semantics["operation"] == "send":
                self._ancestors(connection, run, semantics["parent_id"])
        elif semantics["operation"] in {"deliver", "accept_work"}:
            self._message(connection, run, semantics["message_id"])
        connection.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (run["id"],))
        self.store._event(connection, run["id"], f"collaboration_{semantics['operation']}",
                          {"command_id": command_id, "result": result})
        current = connection.execute("SELECT revision,event_sequence,state FROM runs WHERE id=?", (run["id"],)).fetchone()
        receipt = CommandReceipt(current["revision"], current["event_sequence"], current["state"], result)
        self.store._remember(connection, run["id"], f"collaboration:{authority.supervisor_id}", command_id, semantics, asdict(receipt))
        return receipt

    def offer(self, authority, *, command_id, expected_revision, receiver_scope, receiver_run_id, terms):
        """Offer exact terms to one already-known run; this grants no disclosure."""
        if type(receiver_scope) is not Scope or type(terms) is not CollaborationTerms:
            raise ValueError("Explicit receiver scope and immutable terms required")
        semantics = {"operation": "offer", "receiver_scope": asdict(receiver_scope),
                     "receiver_run_id": receiver_run_id, "terms": terms.to_dict()}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            peer = self.store._run(connection, receiver_scope, receiver_run_id)
            self._same_owner(run, peer)
            for participant in (run, peer):
                self.store._admitting(participant)
                if not participant["managed"] or participant["lease_until"] <= self.store.clock():
                    raise AdmissionClosed("Current managed participant required")
            if (terms.expires_at <= self.store.clock() or terms.expires_at > self.store.clock() + 86400
                    or not terms.revoker_run_ids <= {run["id"], peer["id"]}):
                raise ValueError("Grant expiry must be within one day and revokers must be participants")
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            grant_id, digest = _id(), _digest(terms.to_dict())
            connection.execute("INSERT INTO collaboration_grants(id,origin_run_id,receiver_run_id,origin_epoch,receiver_epoch,"
                "origin_policy_sha256,receiver_policy_sha256,terms_json,terms_sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,'offered',?)",
                (grant_id, run["id"], peer["id"], run["epoch"], peer["epoch"], _digest(json.loads(run["policy_json"])),
                 _digest(json.loads(peer["policy_json"])), canonical_json(terms.to_dict()), digest, self.store.clock()))
            self.store._event(connection, peer["id"], "collaboration_offer_pending", {"grant_id": grant_id, "terms_sha256": digest})
            return self._finish(connection, run, authority, command_id, semantics, {"grant_id": grant_id, "terms_sha256": digest, "state": "offered"})

    def accept_grant(self, authority, *, command_id, expected_revision, grant_id, terms_sha256):
        """The addressed receiver explicitly approves the same immutable contract."""
        semantics = {"operation": "accept_grant", "grant_id": grant_id, "terms_sha256": terms_sha256}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            grant, _ = self._grant(connection, run, grant_id, offered=True)
            if run["id"] != grant["receiver_run_id"] or terms_sha256 != grant["terms_sha256"]:
                raise ScopeDenied("Exact receiver approval required")
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            if grant["state"] != "offered":
                raise Conflict("Grant is already decided")
            connection.execute("UPDATE collaboration_grants SET state='active',accepted_at=? WHERE id=?", (self.store.clock(), grant_id))
            return self._finish(connection, run, authority, command_id, semantics, {"grant_id": grant_id, "state": "active"})

    def _ancestors(self, connection, run, parent_id):
        chain = []
        while parent_id:
            if len(chain) >= 8 or any(row[0]["id"] == parent_id for row in chain):
                raise Conflict("Invalid causal chain")
            message = connection.execute("SELECT * FROM collaboration_messages WHERE id=?", (parent_id,)).fetchone()
            if message is None:
                raise ScopeDenied("Causal parent unavailable")
            if not chain and (message["recipient_run_id"] != run["id"] or message["delivered_at"] is None):
                raise ScopeDenied("Forwarding requires an actually delivered parent")
            grant_run = connection.execute("SELECT * FROM runs WHERE id=?", (message["sender_run_id"],)).fetchone()
            _, terms = self._grant(connection, grant_run, message["grant_id"])
            chain.append((message, terms))
            parent_id = message["parent_id"]
        return chain

    @staticmethod
    def _message_view(row, *, content=False):
        result = {key: row[key] for key in ("id", "grant_id", "sender_run_id", "recipient_run_id", "kind", "data_class",
            "body_sha256", "origin_message_id", "parent_id", "hop", "created_at", "delivered_at")}
        if content:
            result.update(body=row["body"], artifacts=json.loads(row["artifacts_json"]))
        return result

    def send(self, authority, *, command_id, expected_revision, grant_id, kind, data_class, body="", artifact_ids=(), parent_id=None):
        """Queue only explicitly selected content; a reference grants no blob access."""
        if type(kind) is not str or type(data_class) is not str or kind not in KINDS or data_class not in DATA_CLASSES or type(body) is not str:
            raise ValueError("Unsupported message kind or data class")
        if type(artifact_ids) is not tuple or any(type(value) is not str for value in artifact_ids) or len(artifact_ids) != len(set(artifact_ids)) or len(artifact_ids) > 16:
            raise ValueError("Artifact references require at most 16 unique explicit IDs")
        if data_class == "artifact_reference":
            if kind != "artifact_offer" or body or not artifact_ids:
                raise ValueError("Artifact offers contain only authorized reference metadata")
        elif kind == "artifact_offer" or artifact_ids:
            raise ValueError("Artifact references require their explicit disclosure class")
        else:
            _text(body, 65536, "message")
        semantics = {"operation": "send", "grant_id": grant_id, "kind": kind, "data_class": data_class,
                     "body": body, "artifact_ids": list(artifact_ids), "parent_id": parent_id}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            grant, terms = self._grant(connection, run, grant_id)
            if kind not in terms.kinds or data_class not in terms.data_classes:
                raise ScopeDenied("Message exceeds the disclosure grant")
            chain = self._ancestors(connection, run, parent_id)
            hop = chain[0][0]["hop"] + 1 if chain else 1
            limits = [terms, *(item[1] for item in chain)]
            if hop > min(item.max_hops for item in limits) or any(kind not in item.kinds or data_class not in item.data_classes for item in limits):
                raise ScopeDenied("Message exceeds its inherited causal grant")
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            if parent_id and connection.execute("SELECT COUNT(*) FROM collaboration_messages WHERE parent_id=?", (parent_id,)).fetchone()[0] >= min(item.max_fanout for item in limits):
                raise Conflict("Causal fanout limit reached")
            references = []
            for artifact_id in artifact_ids:
                reference = connection.execute("SELECT id,sha256,size FROM artifact_refs WHERE run_id=? AND id=?", (run["id"], artifact_id)).fetchone()
                if reference is None:
                    raise ScopeDenied("Artifact reference unavailable in the sending run")
                references.append(dict(reference))
            size = len(body.encode("utf-8")) + len(canonical_json(references).encode("utf-8"))
            totals = connection.execute("SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM collaboration_messages WHERE grant_id=?", (grant_id,)).fetchone()
            if size > min(item.max_message_bytes for item in limits) or totals[0] >= terms.max_messages or totals[1] + size > terms.max_total_bytes:
                raise Conflict("Grant message or disclosure-byte allowance exhausted")
            if chain:
                causal = connection.execute("SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM collaboration_messages WHERE origin_message_id=?", (chain[0][0]["origin_message_id"],)).fetchone()
                if causal[0] >= min(item.max_messages for item in limits) or causal[1] + size > min(item.max_total_bytes for item in limits):
                    raise Conflict("Inherited causal message or disclosure-byte allowance exhausted")
            message_id = _id()
            target = grant["receiver_run_id"] if run["id"] == grant["origin_run_id"] else grant["origin_run_id"]
            origin = chain[0][0]["origin_message_id"] if chain else message_id
            if connection.execute("SELECT 1 FROM collaboration_messages WHERE origin_message_id=? AND recipient_run_id=? AND kind=?", (origin, target, kind)).fetchone():
                raise Conflict("This causal request already reached that run")
            connection.execute("INSERT INTO collaboration_messages(id,grant_id,sender_run_id,recipient_run_id,kind,data_class,body,body_sha256,"
                "artifacts_json,byte_count,origin_message_id,parent_id,hop,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, grant_id, run["id"], target, kind, data_class, body, _digest({"body": body, "artifacts": references}),
                 canonical_json(references), size, origin, parent_id, hop, self.store.clock()))
            self.store._event(connection, target, "collaboration_message_pending", {"grant_id": grant_id, "message_id": message_id, "kind": kind})
            return self._finish(connection, run, authority, command_id, semantics, {"message_id": message_id, "state": "queued", "origin_message_id": origin, "hop": hop})

    def _message(self, connection, run, message_id):
        require_id(message_id)
        message = connection.execute("SELECT * FROM collaboration_messages WHERE id=? AND recipient_run_id=?", (message_id, run["id"])).fetchone()
        if message is None:
            raise ScopeDenied("Addressed message unavailable")
        self._verify_message(message)
        grant, terms = self._grant(connection, run, message["grant_id"])
        self._ancestors(connection, connection.execute("SELECT * FROM runs WHERE id=?", (message["sender_run_id"],)).fetchone(), message["parent_id"])
        return message, grant, terms

    @staticmethod
    def _verify_message(message):
        if _digest({"body": message["body"], "artifacts": json.loads(message["artifacts_json"])}) != message["body_sha256"]:
            raise Conflict("Retained message content changed")

    def deliver(self, authority, *, command_id, expected_revision, message_id):
        """Record host receipt before returning content; never model comprehension."""
        semantics = {"operation": "deliver", "message_id": message_id}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            message, _, _ = self._message(connection, run, message_id)
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            if message["delivered_at"] is not None:
                raise Conflict("Already delivered; replay original command or inspect retained receipt")
            connection.execute("UPDATE collaboration_messages SET delivered_at=?,delivered_epoch=? WHERE id=?", (self.store.clock(), run["epoch"], message_id))
            current = connection.execute("SELECT * FROM collaboration_messages WHERE id=?", (message_id,)).fetchone()
            return self._finish(connection, run, authority, command_id, semantics, self._message_view(current, content=True))

    def accept_work(self, authority, *, command_id, expected_revision, message_id, work_item, model, requests, worker_id, evidence):
        """Receiver chooses its own assignment, model and budget; no dispatch occurs."""
        _text(evidence, 4000, "acceptance evidence")
        if type(requests) is not int or requests < 1:
            raise ValueError("Positive receiver request allowance required")
        payload = json.loads(canonical_json({"work_item": work_item, "model": model}))
        semantics = {"operation": "accept_work", "message_id": message_id, **payload, "requests": requests, "worker_id": worker_id, "evidence": evidence}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            message, grant, terms = self._message(connection, run, message_id)
            if message["kind"] != "work_request" or message["delivered_at"] is None or message["delivered_epoch"] != run["epoch"]:
                raise Conflict("Only a delivered work proposal may be accepted")
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            if connection.execute("SELECT 1 FROM collaboration_acceptances WHERE message_id=?", (message_id,)).fetchone():
                raise Conflict("Work request already accepted")
            chain = self._ancestors(connection, connection.execute("SELECT * FROM runs WHERE id=?", (message["sender_run_id"],)).fetchone(), message["parent_id"])
            used = connection.execute("SELECT COALESCE(SUM(a.request_allowance),0) FROM collaboration_acceptances a JOIN collaboration_messages m ON m.id=a.message_id WHERE m.origin_message_id=?", (message["origin_message_id"],)).fetchone()[0]
            direct = connection.execute("SELECT COALESCE(SUM(a.request_allowance),0) FROM collaboration_acceptances a JOIN collaboration_messages m ON m.id=a.message_id WHERE m.grant_id=?", (grant["id"],)).fetchone()[0]
            if used + requests > min(item.max_requests for item in [terms, *(item[1] for item in chain)]) or direct + requests > terms.max_requests:
                raise Conflict("Collaboration request ceiling exhausted; no budget is transferred")
            cycle = connection.execute("WITH RECURSIVE waiting(run_id) AS (SELECT ? UNION SELECT m.recipient_run_id FROM waiting w "
                "JOIN collaboration_messages m ON m.sender_run_id=w.run_id JOIN collaboration_acceptances a ON a.message_id=m.id "
                "JOIN work_items i ON i.id=a.work_item_id WHERE i.state NOT IN ('accepted','failed','cancelled')) SELECT 1 FROM waiting WHERE run_id=? LIMIT 1",
                (run["id"], message["sender_run_id"])).fetchone()
            if cycle:
                raise Conflict("Cross-run work dependency would form a cycle")
            item = payload["work_item"]
            if type(item) is not dict or connection.execute("SELECT 1 FROM work_items WHERE id=?", (item.get("id"),)).fetchone():
                raise Conflict("Receiver must create a fresh own work item")
            retained = [json.loads(row[0]) for row in connection.execute("SELECT specification FROM work_items WHERE run_id=?", (run["id"],))]
            self.supervisor._plan(connection, run, work_items=[*retained, item])
            claim = self.supervisor._assign(connection, run, work_item_id=item["id"], worker_id=worker_id, requests=requests, model=payload["model"])
            connection.execute("INSERT INTO collaboration_acceptances(message_id,receiver_run_id,work_item_id,attempt_id,request_allowance,evidence,accepted_at) VALUES(?,?,?,?,?,?,?)",
                (message_id, run["id"], item["id"], claim["attempt_id"], requests, evidence, self.store.clock()))
            return self._finish(connection, run, authority, command_id, semantics,
                {"message_id": message_id, "payer_run_id": run["id"], "payer_owner_id": run["owner_id"], "work_item_id": item["id"], "claim": claim, "state": "accepted_pending_dispatch"})

    def revoke(self, authority, *, command_id, expected_revision, grant_id, evidence):
        """Close future delivery/admission; accepted peer work keeps its ownership."""
        _text(evidence, 4000, "revocation evidence")
        semantics = {"operation": "revoke", "grant_id": grant_id, "evidence": evidence}
        with self.store._connection(write=True) as connection:
            run = self._run(connection, authority)
            grant, terms = self._grant(connection, run, grant_id, live=False)
            if run["id"] not in terms.revoker_run_ids:
                raise ScopeDenied("This participant cannot revoke the grant")
            replay = self._replay(connection, run, authority, command_id, expected_revision, semantics)
            if replay:
                return replay
            if grant["state"] == "revoked":
                raise Conflict("Grant is already revoked")
            connection.execute("UPDATE collaboration_grants SET state='revoked',revoked_at=?,revoked_by_run_id=?,revocation_evidence=? WHERE id=?",
                               (self.store.clock(), run["id"], evidence, grant_id))
            return self._finish(connection, run, authority, command_id, semantics, {"grant_id": grant_id, "state": "revoked", "accepted_peer_work_cancelled": False})

    def inspect(self, authority, grant_id):
        """Inspect only an explicit grant; no session/run discovery or transcript."""
        with self.store._connection() as connection:
            run = self._run(connection, authority)
            grant, terms = self._grant(connection, run, grant_id, live=False)
            messages = connection.execute("SELECT * FROM collaboration_messages WHERE grant_id=? ORDER BY rowid LIMIT 1000", (grant_id,)).fetchall()
            for message in messages:
                self._verify_message(message)
            return {"grant_id": grant_id, "origin_run_id": grant["origin_run_id"], "receiver_run_id": grant["receiver_run_id"],
                "terms": terms.to_dict(), "terms_sha256": grant["terms_sha256"], "state": grant["state"],
                "messages": [self._message_view(row, content=row["sender_run_id"] == run["id"] or row["delivered_at"] is not None) for row in messages]}
