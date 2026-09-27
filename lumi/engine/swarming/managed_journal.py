"""Durable local receipts for a captured managed run, without network or keys.

This is a trusted host API, not authentication. The caller separately enforces
the local supervisor and the server's current authority. One file binds exactly
one run/epoch; personal runs never construct this journal. No lease, dispatch
permit, or control permit is restored to a new object after a restart. SQLite
transactions end before callers perform network, provider, or runner effects.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

_SESSION_NAMESPACE = UUID("e9b91bc6-ce6e-5a79-a1ec-e6a750fb5cf2")


class ManagedJournalError(Exception):
    """A managed receipt cannot safely authorize the requested transition."""


class JournalConflict(ManagedJournalError):
    """Immutable identity or durable state differs from the request."""


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {name}")
    return value


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("invalid remote identifier")
    return value


def _json(value, maximum=65536):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("managed metadata exceeds journal limit")
    return encoded


def _deadline(value):
    if type(value) is not str:
        raise ValueError("invalid expiry")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()):
        raise ValueError("expiry requires an absolute timezone")
    return parsed.timestamp()


def absence_receipt(receipt, kind, remote_id, lease_id):
    """Validate an authenticated fixed-route response, never a browser proof."""
    if (type(receipt) is not dict or set(receipt) != {"kind", "resource_id", "lease_id", "state", "source", "dispatch_permitted", "fence_id"}
            or receipt["kind"] != kind or receipt["resource_id"] != remote_id
            or receipt["lease_id"] != lease_id
            or receipt["state"] != "fenced_absent" or receipt["source"] != "server_non_admission_fence"
            or receipt["dispatch_permitted"] is not False):
        raise JournalConflict("server did not prove and fence this exact absent admission")
    _uuid(receipt["fence_id"])
    return _json(receipt)


@dataclass(frozen=True, slots=True)
class ManagedBinding:
    """Public identity only; certificate keys and server credentials stay outside."""

    server_url: str
    certificate_sha256: str
    tenant_id: str
    project_id: str
    host_id: str
    host_generation: int
    owner_id: str
    local_project_id: str
    session_id: str
    run_id: str
    epoch: int

    def __post_init__(self):
        parts = urlsplit(self.server_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in {"", "/"}):
            raise ValueError("managed server requires a captured HTTPS origin")
        if not re.fullmatch(r"[0-9a-f]{64}", self.certificate_sha256):
            raise ValueError("invalid certificate fingerprint")
        if type(self.owner_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.owner_id):
            raise ValueError("managed owner must be an enrolled actor identity")
        for value in (self.tenant_id, self.project_id, self.host_id):
            _uuid(value)
        for value in (self.owner_id, self.local_project_id, self.session_id, self.run_id):
            _text(value, "local identity")
        for value in (self.epoch, self.host_generation):
            if type(value) is not int or value < 1:
                raise ValueError("invalid ownership generation")


_SCHEMA = """
CREATE TABLE binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1), document TEXT NOT NULL,
 remote_binding_id TEXT, report_sequence INTEGER NOT NULL DEFAULT 0,
 wire_run_id TEXT NOT NULL, wire_session_id TEXT NOT NULL, registration_command_id TEXT NOT NULL,
 registration_lease_id TEXT);
CREATE TABLE requests(local_id TEXT PRIMARY KEY, remote_id TEXT NOT NULL UNIQUE,
 semantics TEXT NOT NULL, instance_id TEXT NOT NULL, phase TEXT NOT NULL,
 lease TEXT, reserve_receipt TEXT, binding_receipt TEXT, start_receipt TEXT, outcome TEXT, created_at REAL NOT NULL,
 start_attempted INTEGER NOT NULL DEFAULT 0, dispatch_claimed INTEGER NOT NULL DEFAULT 0, recovery_evidence TEXT);
CREATE TABLE controls(id TEXT PRIMARY KEY, document TEXT NOT NULL, instance_id TEXT NOT NULL,
 receive_command_id TEXT NOT NULL UNIQUE, local_command_id TEXT NOT NULL UNIQUE,
 phase TEXT NOT NULL, receipt TEXT, outcome TEXT);
CREATE TABLE effects(local_id TEXT NOT NULL, kind TEXT NOT NULL, remote_id TEXT NOT NULL UNIQUE,
 semantics TEXT NOT NULL, instance_id TEXT NOT NULL, phase TEXT NOT NULL,
 lease TEXT, receipt TEXT, outcome TEXT, claimed INTEGER NOT NULL DEFAULT 0, recovery_evidence TEXT, PRIMARY KEY(kind,local_id));
CREATE TABLE outbox(id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL,
 semantic_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, receipt TEXT, superseded_by TEXT REFERENCES outbox(id),
 last_attempt INTEGER NOT NULL DEFAULT 0,
 UNIQUE(kind,semantic_key));
CREATE TABLE recovery_receipts(kind TEXT NOT NULL, local_id TEXT NOT NULL, outcome TEXT NOT NULL,
 evidence_sha256 TEXT NOT NULL, PRIMARY KEY(kind,local_id,outcome));
PRAGMA user_version=1;
"""


class ManagedJournal:
    """Small synchronous receipt store; each call uses its own FULL transaction."""

    def __init__(self, path: str | Path, binding: ManagedBinding, *, clock=time.time):
        self.path = Path(path)
        self.binding = binding
        self.clock = clock
        self.instance_id = str(uuid4())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True, initialize=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise JournalConflict("unversioned journal is not empty")
                # executescript commits an open transaction, so execute each
                # statement separately to keep schema and binding atomic.
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            elif version != 1:
                raise JournalConflict("unsupported managed journal version")
            document = _json(asdict(binding))
            row = connection.execute("SELECT document FROM binding").fetchone()
            if row and row[0] != document:
                raise JournalConflict("journal belongs to a different captured binding")
            if not row:
                connection.execute("INSERT INTO binding(singleton,document,wire_run_id,wire_session_id,registration_command_id) "
                                   "VALUES(1,?,?,?,?)", (document, str(uuid4()), self._stable_session_id(), str(uuid4())))

    def _stable_session_id(self):
        binding = self.binding
        return str(uuid5(_SESSION_NAMESPACE, _json([binding.tenant_id, binding.owner_id,
                                                    binding.local_project_id, binding.session_id])))

    @property
    def sharing_eligible(self) -> bool:
        """Historical random session identities retain history, not new sharing."""
        with self._connection() as connection:
            return connection.execute("SELECT wire_session_id FROM binding").fetchone()[0] == self._stable_session_id()

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if initialize:
                if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise JournalConflict("unsupported managed journal version")
                connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now):
            raise JournalConflict("invalid local clock")
        return now

    @staticmethod
    def _request(connection, local_id):
        _text(local_id, "local request")
        row = connection.execute("SELECT * FROM requests WHERE local_id=?", (local_id,)).fetchone()
        if not row:
            raise JournalConflict("request is unavailable")
        return row

    @staticmethod
    def _view(row):
        result = dict(row)
        result.pop("instance_id", None)
        for name in ("semantics", "lease", "reserve_receipt", "binding_receipt", "start_receipt", "document", "receipt", "payload"):
            if name in result and result[name] is not None:
                result[name] = json.loads(result[name])
        if "remote_id" in result:
            name = {"worker": "remote_worker_id", "action": "remote_action_id"}.get(result.get("kind"), "remote_request_id")
            result[name] = result.pop("remote_id")
        return result

    def request(self, local_request_id: str, semantics: dict) -> dict:
        """Persist an immutable remote UUID before any outbound reservation."""
        _text(local_request_id, "local request")
        if type(semantics) is not dict:
            raise ValueError("request semantics must be an object")
        if (set(semantics) != {"attempt_id", "attempt_epoch", "purpose", "model", "input_sha256", "policy_revision"}
                or semantics["purpose"] not in {"primary", "planning", "compression"}
                or type(semantics["attempt_epoch"]) is not int or semantics["attempt_epoch"] != self.binding.epoch
                or type(semantics["policy_revision"]) is not int or semantics["policy_revision"] < 1
                or type(semantics["model"]) is not dict or set(semantics["model"]) != {"provider", "model"}
                or type(semantics["input_sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", semantics["input_sha256"])):
            raise ValueError("invalid immutable request metadata")
        _text(semantics["attempt_id"], "attempt")
        for value in semantics["model"].values():
            _text(value, "model selection")
        encoded = _json(semantics, 8192)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM requests WHERE local_id=?", (local_request_id,)).fetchone()
            if row:
                if row["semantics"] != encoded:
                    raise JournalConflict("request identity was reused for different semantics")
            else:
                connection.execute("INSERT INTO requests(local_id,remote_id,semantics,instance_id,phase,created_at) "
                                   "VALUES(?,?,?,?,'local_reserved',?)",
                                   (local_request_id, str(uuid4()), encoded, self.instance_id, self._now()))
                row = self._request(connection, local_request_id)
            return self._view(row)

    def mark_reserve_pending(self, local_id: str, lease: dict) -> bool:
        """Consume the one reservation send opportunity, before HTTP dispatch."""
        self._lease(lease)
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "local_reserved":
                return False
            if json.loads(row["semantics"])["policy_revision"] != lease["policy_revision"]:
                raise JournalConflict("request policy differs from lease")
            connection.execute("UPDATE requests SET phase='reserve_pending',lease=? WHERE local_id=?", (_json(lease), local_id))
            return True

    def reserve_rejected(self, local_id: str) -> None:
        """Record only a definite first server denial, never an ambiguous error."""
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "reserve_pending":
                raise JournalConflict("no current reservation dispatch")
            connection.execute("UPDATE requests SET phase='not_admitted',outcome='never_started' WHERE local_id=?", (local_id,))

    def _lease(self, lease):
        if type(lease) is not dict:
            raise ValueError("lease must be an object")
        for key in ("tenant_id", "project_id", "host_id", "host_generation", "owner_id"):
            if lease.get(key) != getattr(self.binding, key):
                raise JournalConflict("lease belongs to another captured host binding")
        if type(lease["host_generation"]) is not int:
            raise JournalConflict("invalid host generation")
        _uuid(lease.get("lease_id"))
        if (lease.get("protocol_version") != 1 or type(lease.get("protocol_version")) is not int
                or lease.get("runner_protocol") != 2 or type(lease.get("runner_protocol")) is not int
                or lease.get("offline_request_allowance") != 0
                or type(lease.get("offline_request_allowance")) is not int
                or type(lease.get("policy_revision")) is not int or lease["policy_revision"] < 1
                or _deadline(lease.get("expires_at")) <= self._now()):
            raise JournalConflict("lease is expired or unsupported")
        if not isinstance(lease.get("policy"), dict):
            raise ValueError("lease policy is missing")
        expected = hashlib.sha256(_json(lease["policy"]).encode()).hexdigest()
        if lease.get("policy_sha256") != expected:
            raise JournalConflict("lease policy digest differs")

    def reserve_receipt(self, local_id: str, lease: dict, receipt: dict) -> None:
        """Bind a confirmed reservation to the exact current online lease."""
        self._lease(lease)
        encoded = _json(receipt)
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if (row["instance_id"] != self.instance_id or row["phase"] != "reserve_pending"
                    or row["lease"] != _json(lease)
                    or receipt.get("request_id") != row["remote_id"] or receipt.get("lease_id") != lease["lease_id"]
                    or receipt.get("state") != "reserved" or type(receipt.get("units")) is not int or receipt["units"] != 1
                    or json.loads(row["semantics"]).get("policy_revision") != lease["policy_revision"]):
                raise JournalConflict("reservation receipt does not match pending intent")
            connection.execute("UPDATE requests SET phase='remote_reserved',lease=?,reserve_receipt=? WHERE local_id=?",
                               (_json(lease), encoded, local_id))
            self._lease(lease)

    def bind_request_receipt(self, local_id: str, worker_id: str, receipt: dict) -> None:
        """Retain exact originating worker and input attribution before start."""
        _uuid(worker_id)
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            semantics = json.loads(row["semantics"])
            worker = connection.execute("SELECT * FROM effects WHERE kind='worker' AND remote_id=?", (worker_id,)).fetchone()
            if (row["instance_id"] != self.instance_id or row["phase"] != "remote_reserved"
                    or not worker or worker["phase"] != "invoked" or worker["instance_id"] != self.instance_id
                    or json.loads(worker["semantics"])["attempt_id"] != semantics["attempt_id"]
                    or json.loads(worker["semantics"])["attempt_epoch"] != semantics["attempt_epoch"]
                    or receipt.get("request_id") != row["remote_id"] or receipt.get("worker_id") != worker_id
                    or receipt.get("purpose") != semantics["purpose"] or receipt.get("bound") is not True):
                raise JournalConflict("request binding does not match current worker and intent")
            encoded = _json(receipt)
            if row["binding_receipt"] is not None and row["binding_receipt"] != encoded:
                raise JournalConflict("request binding receipt changed")
            connection.execute("UPDATE requests SET binding_receipt=? WHERE local_id=?", (encoded, local_id))
            self._lease(json.loads(row["lease"]))

    def begin_start(self, local_id: str) -> bool:
        """Record possible remote start before sending; this method never retries."""
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "remote_reserved":
                return False
            if row["binding_receipt"] is None:
                raise JournalConflict("request has no durable originating worker binding")
            self._lease(json.loads(row["lease"]))
            connection.execute("UPDATE requests SET phase='start_pending',start_attempted=1 WHERE local_id=?", (local_id,))
            return True

    def start_receipt(self, local_id: str, receipt: dict) -> None:
        """Only a first positive reply to this object's pending send is a permit."""
        encoded = _json(receipt)
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if (row["instance_id"] != self.instance_id or row["phase"] != "start_pending"
                    or receipt.get("request_id") != row["remote_id"] or receipt.get("state") != "started"
                    or type(receipt.get("dispatch_permitted")) is not bool):
                raise JournalConflict("start receipt does not match a live pending send")
            self._lease(json.loads(row["lease"]))
            phase = "permitted" if receipt["dispatch_permitted"] else "uncertain"
            connection.execute("UPDATE requests SET phase=?,start_receipt=? WHERE local_id=?", (phase, encoded, local_id))
            if phase == "uncertain":
                self._observe(connection, row, "uncertain")

    def claim_dispatch(self, local_id: str) -> bool:
        """Consume the local permit durably before a provider call; no replay."""
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "permitted":
                return False
            self._lease(json.loads(row["lease"]))
            connection.execute("UPDATE requests SET phase='invoked',dispatch_claimed=1 WHERE local_id=?", (local_id,))
            return True

    @staticmethod
    def _enqueue(connection, kind, key, payload):
        encoded = _json(payload)
        previous = connection.execute("SELECT * FROM outbox WHERE semantic_key=?", (key,)).fetchone()
        if previous:
            if previous["kind"] != kind or previous["payload"] != encoded:
                raise JournalConflict("outbox key was reused for different semantics")
            return previous["id"]
        if connection.execute("SELECT count(*) FROM outbox WHERE receipt IS NULL AND superseded_by IS NULL").fetchone()[0] >= 10000:
            raise JournalConflict("managed observation outbox is full")
        ordinal = connection.execute("SELECT coalesce(max(ordinal),0)+1 FROM outbox").fetchone()[0]
        identity = str(uuid4())
        connection.execute("INSERT INTO outbox(id,ordinal,kind,semantic_key,payload) VALUES(?,?,?,?,?)",
                           (identity, ordinal, kind, key, encoded))
        return identity

    def _observe(self, connection, row, outcome):
        if outcome not in {"completed", "failed", "never_started", "uncertain"}:
            raise ValueError("invalid request observation")
        if row["outcome"] in {"completed", "failed", "never_started"}:
            if row["outcome"] == outcome:
                return
            raise JournalConflict("terminal request observation is immutable")
        if outcome == "never_started" and row["phase"] not in {"local_reserved", "remote_reserved"}:
            raise JournalConflict("possible remote start or reservation cannot be refunded")
        if outcome == "completed" and (row["phase"] != "invoked" or row["instance_id"] != self.instance_id):
            raise JournalConflict("completion requires this object's admitted invocation")
        if outcome == "failed" and (not row["start_attempted"] or row["instance_id"] != self.instance_id):
            raise JournalConflict("failed request has no original remote start attempt")
        if row["phase"] == "not_admitted":
            raise JournalConflict("request was never admitted")
        # A purely local reservation has no remote ledger entry to settle.
        if row["phase"] != "local_reserved":
            identity = self._enqueue(connection, "settle", f"request:{row['remote_id']}:{outcome}",
                                     {"request_id": row["remote_id"], "outcome": outcome})
            if outcome != "uncertain":
                connection.execute("UPDATE outbox SET superseded_by=? WHERE semantic_key=? AND receipt IS NULL",
                                   (identity, f"request:{row['remote_id']}:uncertain"))
        phase = "uncertain" if outcome == "uncertain" else "settled"
        connection.execute("UPDATE requests SET phase=?,outcome=? WHERE local_id=?", (phase, outcome, row["local_id"]))

    def observe(self, local_id: str, outcome: str) -> None:
        """Retain an exact observation for later delivery; uncertainty holds quota."""
        with self._connection(write=True) as connection:
            self._observe(connection, self._request(connection, local_id), outcome)

    def completed_request_observation(self, local_id: str) -> dict:
        """Return the exact completed-call observation required by a tool.

        This exposes no new dispatch permit. Only this live journal instance's
        actually claimed request may supply a tool's settlement dependency.
        """
        with self._connection() as connection:
            request = self._request(connection, local_id)
            if (request["instance_id"] != self.instance_id or not request["dispatch_claimed"]
                    or request["outcome"] != "completed"):
                raise JournalConflict("tool requires its current completed model request")
            row = connection.execute("SELECT * FROM outbox WHERE semantic_key=?",
                (f"request:{request['remote_id']}:completed",)).fetchone()
            expected = {"request_id": request["remote_id"], "outcome": "completed"}
            if (row is None or row["kind"] != "settle" or row["payload"] != _json(expected)
                    or row["superseded_by"] is not None):
                raise JournalConflict("completed request settlement is unavailable")
            return self._view(row)

    @staticmethod
    def _evidence(value):
        if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("invalid recovery evidence identity")

    @staticmethod
    def _record_recovery(connection, kind, local_id, outcome, evidence_sha256):
        previous = connection.execute("SELECT evidence_sha256 FROM recovery_receipts WHERE kind=? AND local_id=? AND outcome=?",
                                      (kind, local_id, outcome)).fetchone()
        if previous and previous[0] != evidence_sha256:
            raise JournalConflict("recovery evidence is immutable for this observation")
        if not previous:
            connection.execute("INSERT INTO recovery_receipts VALUES(?,?,?,?)", (kind, local_id, outcome, evidence_sha256))

    def reconcile_request(self, local_id: str, outcome: str, evidence_sha256: str) -> None:
        """Record a trusted recovery observer's exact native-ledger observation.

        The recovery facade must derive evidence from captured immutable local
        receipts, not browser/model outcomes. A hash is provenance, not proof or
        authority by itself. This method never permits a provider invocation.
        """
        self._evidence(evidence_sha256)
        if outcome not in {"completed", "failed", "never_started", "uncertain"}:
            raise ValueError("invalid recovery request outcome")
        with self._connection(write=True) as connection:
            row = self._request(connection, local_id)
            if row["outcome"] in {"completed", "failed", "never_started"} and row["outcome"] != outcome:
                raise JournalConflict("terminal request observation is immutable")
            if outcome == "never_started" and (row["start_attempted"] or row["dispatch_claimed"]):
                raise JournalConflict("a possible durable remote start cannot be refunded")
            if outcome == "completed" and not row["dispatch_claimed"]:
                raise JournalConflict("request has no original dispatch claim")
            if outcome == "failed" and not row["start_attempted"]:
                raise JournalConflict("failed request has no original remote start attempt")
            evidence = _json({"outcome": outcome, "sha256": evidence_sha256})
            self._record_recovery(connection, "request", local_id, outcome, evidence_sha256)
            # A not-admitted/purely-local request has no remote ledger row.
            if row["lease"] is not None and row["phase"] != "not_admitted":
                identity = self._enqueue(connection, "settle", f"request:{row['remote_id']}:{outcome}",
                                         {"request_id": row["remote_id"], "outcome": outcome})
                if outcome != "uncertain":
                    connection.execute("UPDATE outbox SET superseded_by=? WHERE semantic_key=? AND receipt IS NULL",
                                       (identity, f"request:{row['remote_id']}:uncertain"))
            connection.execute("UPDATE requests SET phase=?,outcome=?,recovery_evidence=? WHERE local_id=?",
                               ("uncertain" if outcome == "uncertain" else "settled", outcome, evidence, local_id))

    @staticmethod
    def _worker_key(attempt_id, attempt_epoch):
        _text(attempt_id, "attempt")
        if type(attempt_epoch) is not int or attempt_epoch < 1:
            raise ValueError("invalid attempt epoch")
        return _json([attempt_id, attempt_epoch])

    def _effect(self, connection, kind, local_id):
        row = connection.execute("SELECT * FROM effects WHERE kind=? AND local_id=?", (kind, local_id)).fetchone()
        if not row:
            raise JournalConflict("managed effect is unavailable")
        return row

    def _prepare_effect(self, kind, local_id, semantics):
        encoded = _json(semantics, 8192)
        with self._connection(write=True) as connection:
            self._remote(connection)
            previous = connection.execute("SELECT * FROM effects WHERE kind=? AND local_id=?", (kind, local_id)).fetchone()
            if previous:
                if previous["semantics"] != encoded:
                    raise JournalConflict("effect identity was reused for different semantics")
                return self._view(previous)
            if kind == "action":
                request = connection.execute("SELECT * FROM requests WHERE remote_id=?", (semantics["request_id"],)).fetchone()
                worker = connection.execute("SELECT * FROM effects WHERE remote_id=? AND kind='worker'", (semantics["worker_id"],)).fetchone()
                if (not request or request["outcome"] != "completed" or not request["binding_receipt"]
                        or json.loads(request["binding_receipt"])["worker_id"] != semantics["worker_id"]
                        or json.loads(request["semantics"])["purpose"] != "primary"
                        or not worker or worker["phase"] != "invoked" or worker["instance_id"] != self.instance_id):
                    raise JournalConflict("action has no completed originating request and live worker")
            connection.execute("INSERT INTO effects(local_id,kind,remote_id,semantics,instance_id,phase) VALUES(?,?,?,?,?,'prepared')",
                               (local_id, kind, str(uuid4()), encoded, self.instance_id))
            return self._view(self._effect(connection, kind, local_id))

    def worker(self, attempt_id: str, attempt_epoch: int, kind: str = "worker") -> dict:
        """Persist an opaque remote slot identity for one local participant."""
        if kind not in {"worker", "coordinator"} or attempt_epoch != self.binding.epoch:
            raise JournalConflict("worker does not match captured run epoch")
        key = self._worker_key(attempt_id, attempt_epoch)
        return self._prepare_effect("worker", key, {"attempt_id": attempt_id, "attempt_epoch": attempt_epoch, "kind": kind})

    def action(self, local_id: str, semantics: dict) -> dict:
        """Record only action attribution and argument hash, never argument bytes."""
        _text(local_id, "local action")
        if type(semantics) is not dict or set(semantics) != {"worker_id", "request_id", "tool_name", "arguments_sha256"}:
            raise ValueError("invalid action metadata")
        for key in ("worker_id", "request_id"):
            _uuid(semantics[key])
        if (type(semantics["tool_name"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", semantics["tool_name"])
                or type(semantics["arguments_sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", semantics["arguments_sha256"])):
            raise ValueError("invalid action identity")
        return self._prepare_effect("action", local_id, semantics)

    def _begin_effect(self, kind, local_id, lease):
        self._lease(lease)
        with self._connection(write=True) as connection:
            row = self._effect(connection, kind, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "prepared":
                return False
            connection.execute("UPDATE effects SET phase='pending',lease=? WHERE kind=? AND local_id=?", (_json(lease), kind, local_id))
            return True

    def begin_worker(self, attempt_id: str, attempt_epoch: int, lease: dict) -> bool:
        """Consume the only slot reservation send opportunity."""
        return self._begin_effect("worker", self._worker_key(attempt_id, attempt_epoch), lease)

    def begin_action(self, local_id: str, lease: dict) -> bool:
        """Consume the only tool authorization send opportunity."""
        return self._begin_effect("action", local_id, lease)

    def _effect_receipt(self, kind, local_id, lease, receipt):
        self._lease(lease)
        with self._connection(write=True) as connection:
            row = self._effect(connection, kind, local_id)
            identity = "worker_id" if kind == "worker" else "action_id"
            if (row["instance_id"] != self.instance_id or row["phase"] != "pending"
                    or row["lease"] != _json(lease)
                    or receipt.get(identity) != row["remote_id"]
                    or receipt.get("state") != ("held" if kind == "worker" else "admitted")
                    or type(receipt.get("dispatch_permitted")) is not bool):
                raise JournalConflict("effect receipt does not match a live pending send")
            phase = "permitted" if receipt["dispatch_permitted"] else "uncertain"
            connection.execute("UPDATE effects SET phase=?,lease=?,receipt=? WHERE kind=? AND local_id=?",
                               (phase, _json(lease), _json(receipt), kind, local_id))
            if phase == "uncertain":
                self._observe_effect(connection, row, "uncertain")
            self._lease(lease)

    def worker_receipt(self, attempt_id: str, attempt_epoch: int, lease: dict, receipt: dict) -> None:
        """Capture a first server slot permit without launching a local process."""
        self._effect_receipt("worker", self._worker_key(attempt_id, attempt_epoch), lease, receipt)

    def action_receipt(self, local_id: str, lease: dict, receipt: dict) -> None:
        """Capture a first server tool permit without running a local tool."""
        self._effect_receipt("action", local_id, lease, receipt)

    def _claim_effect(self, kind, local_id):
        with self._connection(write=True) as connection:
            row = self._effect(connection, kind, local_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "permitted":
                return False
            self._lease(json.loads(row["lease"]))
            connection.execute("UPDATE effects SET phase='invoked',claimed=1 WHERE kind=? AND local_id=?", (kind, local_id))
            return True

    def claim_worker(self, attempt_id: str, attempt_epoch: int) -> bool:
        """Consume a live slot permit once immediately before participant launch."""
        return self._claim_effect("worker", self._worker_key(attempt_id, attempt_epoch))

    def claim_action(self, local_id: str) -> bool:
        """Consume a live tool permit once immediately before local tool dispatch."""
        return self._claim_effect("action", local_id)

    def _observe_effect(self, connection, row, outcome):
        worker = row["kind"] == "worker"
        allowed = {"stopped", "never_started", "uncertain"} if worker else {"completed", "uncertain"}
        if outcome not in allowed:
            raise ValueError("invalid effect observation")
        if row["outcome"] is not None and row["outcome"] != "uncertain":
            if row["outcome"] == outcome:
                return
            raise JournalConflict("terminal effect observation is immutable")
        if outcome == "never_started" and row["claimed"]:
            raise JournalConflict("a consumed worker permit cannot be declared never started")
        if outcome in {"stopped", "completed"} and not row["claimed"]:
            raise JournalConflict("effect has no locally consumed permit")
        if not worker and outcome == "completed" and row["instance_id"] != self.instance_id:
            raise JournalConflict("historical action has no current result observation")
        # An explicit stopped observation is supplied by the trusted process
        # owner/recovery observer, never inferred here from a reopened file.
        if row["phase"] != "prepared":
            identity = "worker_id" if worker else "action_id"
            outbox_id = self._enqueue(connection, "observe_worker" if worker else "observe_tool",
                                      f"effect:{row['remote_id']}:{outcome}", {identity: row["remote_id"], "outcome": outcome})
            if outcome != "uncertain" and row["outcome"] == "uncertain":
                # Keep the earlier exact bytes and their provenance, but do not
                # send a stale uncertainty after a concrete terminal observation.
                # This is not a delivery acknowledgement or automatic settlement.
                connection.execute("UPDATE outbox SET superseded_by=? WHERE semantic_key=? AND receipt IS NULL",
                                   (outbox_id, f"effect:{row['remote_id']}:uncertain"))
        connection.execute("UPDATE effects SET phase=?,outcome=? WHERE kind=? AND local_id=?",
                           ("uncertain" if outcome == "uncertain" else "observed", outcome, row["kind"], row["local_id"]))

    def observe_worker(self, attempt_id: str, attempt_epoch: int, outcome: str) -> None:
        """Retain actual stop/known-unstarted evidence; reopening never calls this."""
        with self._connection(write=True) as connection:
            self._observe_effect(connection, self._effect(connection, "worker", self._worker_key(attempt_id, attempt_epoch)), outcome)

    def observe_action(self, local_id: str, outcome: str) -> None:
        """Retain concrete tool completion or uncertainty independently of leases."""
        with self._connection(write=True) as connection:
            self._observe_effect(connection, self._effect(connection, "action", local_id), outcome)

    def reconcile_action(self, local_id: str, outcome: str, evidence_sha256: str) -> None:
        """Retain an exact local action receipt supplied by the recovery facade."""
        self._evidence(evidence_sha256)
        if outcome not in {"completed", "uncertain"}:
            raise ValueError("invalid recovery action outcome")
        with self._connection(write=True) as connection:
            row = self._effect(connection, "action", local_id)
            if outcome == "completed" and not row["claimed"]:
                raise JournalConflict("action has no original dispatch claim")
            if row["outcome"] == "completed" and outcome != "completed":
                raise JournalConflict("terminal action observation is immutable")
            evidence = _json({"outcome": outcome, "sha256": evidence_sha256})
            self._record_recovery(connection, "action", local_id, outcome, evidence_sha256)
            if row["lease"] is not None:
                identity = self._enqueue(connection, "observe_tool", f"effect:{row['remote_id']}:{outcome}",
                                         {"action_id": row["remote_id"], "outcome": outcome})
                if outcome != "uncertain":
                    connection.execute("UPDATE outbox SET superseded_by=? WHERE semantic_key=? AND receipt IS NULL",
                                       (identity, f"effect:{row['remote_id']}:uncertain"))
            connection.execute("UPDATE effects SET phase=?,outcome=?,recovery_evidence=? WHERE kind='action' AND local_id=?",
                               ("uncertain" if outcome == "uncertain" else "observed", outcome, evidence, local_id))

    def reconcile_worker(self, attempt_id: str, attempt_epoch: int, outcome: str, evidence_sha256: str) -> None:
        """Retain exact evidence from the trusted owning-process recovery observer."""
        self._evidence(evidence_sha256)
        local_id = self._worker_key(attempt_id, attempt_epoch)
        with self._connection(write=True) as connection:
            row = self._effect(connection, "worker", local_id)
            self._observe_effect(connection, row, outcome)
            self._record_recovery(connection, "worker", local_id, outcome, evidence_sha256)
            connection.execute("UPDATE effects SET recovery_evidence=? WHERE kind='worker' AND local_id=?",
                               (_json({"outcome": outcome, "sha256": evidence_sha256}), local_id))

    def _absence_row(self, connection, kind, local_id):
        if kind not in {"request", "worker", "action"}:
            raise ValueError("unsupported managed resource kind")
        row = self._request(connection, local_id) if kind == "request" else self._effect(connection, kind, local_id)
        claimed = row["start_attempted"] or row["dispatch_claimed"] if kind == "request" else row["claimed"]
        if (row["instance_id"] == self.instance_id or claimed or row["lease"] is None
                or row["outcome"] not in (None, "uncertain", "never_started")):
            raise JournalConflict("only an old unclaimed admission can be fenced absent")
        return row

    def absence_intent(self, kind: str, local_id: str) -> dict:
        """Return original identities for one explicit server absence check."""
        with self._connection() as connection:
            row = self._absence_row(connection, kind, local_id)
            return {"resource_kind": "tool" if kind == "action" else kind, "resource_id": row["remote_id"],
                    "lease_id": json.loads(row["lease"])["lease_id"]}

    def record_absence(self, kind: str, local_id: str, receipt: dict) -> None:
        """Retain an immutable non-admission fence; never fabricate settlement."""
        with self._connection(write=True) as connection:
            row = self._absence_row(connection, kind, local_id)
            wire_kind = "tool" if kind == "action" else kind
            encoded = absence_receipt(receipt, wire_kind, row["remote_id"], json.loads(row["lease"])["lease_id"])
            payload = {"resource_kind": wire_kind, "resource_id": row["remote_id"], "lease_id": json.loads(row["lease"])["lease_id"]}
            identity = self._enqueue(connection, "non_admission_fence", f"fence:{wire_kind}:{row['remote_id']}", payload)
            old = connection.execute("SELECT receipt FROM outbox WHERE id=?", (identity,)).fetchone()[0]
            if old is not None and old != encoded:
                raise JournalConflict("non-admission fence receipt changed")
            connection.execute("UPDATE outbox SET receipt=? WHERE id=?", (encoded, identity))
            prefix = "request" if kind == "request" else "effect"
            connection.execute("UPDATE outbox SET superseded_by=? WHERE semantic_key LIKE ? AND receipt IS NULL",
                               (identity, f"{prefix}:{row['remote_id']}:%"))
            proof = hashlib.sha256(encoded.encode()).hexdigest()
            self._record_recovery(connection, kind, local_id, "fenced_absent", proof)
            evidence = _json({"outcome": "never_started", "source": "server_non_admission_fence", "sha256": proof})
            if kind == "request":
                connection.execute("UPDATE requests SET phase='not_admitted',outcome='never_started',recovery_evidence=? WHERE local_id=?", (evidence, local_id))
            else:
                connection.execute("UPDATE effects SET phase='not_admitted',outcome='never_started',recovery_evidence=? WHERE kind=? AND local_id=?", (evidence, kind, local_id))

    def bind_remote(self, binding_id: str) -> None:
        """Pin the server's run registration result without refreshing ownership."""
        _uuid(binding_id)
        with self._connection(write=True) as connection:
            current = connection.execute("SELECT remote_binding_id FROM binding").fetchone()[0]
            if current is not None and current != binding_id:
                raise JournalConflict("remote run binding cannot change")
            connection.execute("UPDATE binding SET remote_binding_id=?", (binding_id,))

    def registration(self, lease: dict) -> dict:
        """Capture one immutable registration intent, using opaque wire UUIDs.

        A lost response may retry this exact intent while its original lease is
        valid. A fresh lease cannot silently replace an ambiguous registration.
        """
        self._lease(lease)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM binding").fetchone()
            if row["registration_lease_id"] not in (None, lease["lease_id"]):
                raise JournalConflict("registration is bound to its original lease")
            connection.execute("UPDATE binding SET registration_lease_id=?", (lease["lease_id"],))
            return {"command_id": row["registration_command_id"], "lease_id": lease["lease_id"],
                    "local_run_id": row["wire_run_id"], "session_id": row["wire_session_id"]}

    @staticmethod
    def _remote(connection):
        identity = connection.execute("SELECT remote_binding_id FROM binding").fetchone()[0]
        if not identity:
            raise JournalConflict("run has no remote registration")
        return identity

    def prepare_control(self, control: dict) -> dict:
        """Capture only server-issued exact pause/stop authority, never a model tool."""
        keys = ("control_id", "operation", "expected_epoch", "expected_local_revision", "policy_revision", "expires_at")
        document = {key: control[key] for key in keys}
        _uuid(document["control_id"])
        if (document["operation"] not in {"pause", "stop"}
                or document["expected_epoch"] != self.binding.epoch
                or any(type(document[k]) is not int or document[k] < 0 for k in keys[2:5])
                or document["policy_revision"] < 1 or _deadline(document["expires_at"]) <= self._now()):
            raise JournalConflict("control is expired or does not match captured authority")
        encoded = _json(document)
        with self._connection(write=True) as connection:
            self._remote(connection)
            row = connection.execute("SELECT * FROM controls WHERE id=?", (document["control_id"],)).fetchone()
            if row and row["document"] != encoded:
                raise JournalConflict("control identity was reused for different semantics")
            if not row:
                connection.execute("INSERT INTO controls(id,document,instance_id,receive_command_id,local_command_id,phase) "
                                   "VALUES(?,?,?,?,?,'prepared')",
                                   (document["control_id"], encoded, self.instance_id, str(uuid4()), str(uuid4())))
                row = self._control(connection, document["control_id"])
            return self._view(row)

    @staticmethod
    def _control(connection, control_id):
        _uuid(control_id)
        row = connection.execute("SELECT * FROM controls WHERE id=?", (control_id,)).fetchone()
        if not row:
            raise JournalConflict("control is unavailable")
        return row

    def begin_control_receive(self, control_id: str) -> bool:
        """Persist the acknowledgement send boundary; repeat sends grant nothing."""
        with self._connection(write=True) as connection:
            row = self._control(connection, control_id)
            if row["instance_id"] != self.instance_id or row["phase"] != "prepared":
                return False
            if _deadline(json.loads(row["document"])["expires_at"]) <= self._now():
                raise JournalConflict("control expired")
            connection.execute("UPDATE controls SET phase='receive_pending' WHERE id=?", (control_id,))
            return True

    def control_receipt(self, control_id: str, receipt: dict) -> None:
        """Persist a first server receive permit before local command dispatch."""
        with self._connection(write=True) as connection:
            row = self._control(connection, control_id)
            document = json.loads(row["document"])
            if (row["instance_id"] != self.instance_id or row["phase"] != "receive_pending"
                    or any(receipt.get(key) != value for key, value in document.items())
                    or type(receipt.get("dispatch_permitted")) is not bool
                    or _deadline(document["expires_at"]) <= self._now()):
                raise JournalConflict("control receipt does not match live pending intent")
            phase = "permitted" if receipt["dispatch_permitted"] else "uncertain"
            connection.execute("UPDATE controls SET phase=?,receipt=? WHERE id=?", (phase, _json(receipt), control_id))
            if phase == "uncertain":
                self._observe_control(connection, row, "uncertain", False)

    def claim_control(self, control_id: str) -> dict | None:
        """Consume authority once; the runner must still enforce exact revision."""
        with self._connection(write=True) as connection:
            row = self._control(connection, control_id)
            document = json.loads(row["document"])
            if row["instance_id"] != self.instance_id or row["phase"] != "permitted":
                return None
            if _deadline(document["expires_at"]) <= self._now():
                raise JournalConflict("control expired")
            connection.execute("UPDATE controls SET phase='claimed' WHERE id=?", (control_id,))
            return {**document, "command_id": row["local_command_id"]}

    def _observe_control(self, connection, row, outcome, processes_stopped):
        if (outcome not in {"applied", "denied", "uncertain"} or type(processes_stopped) is not bool
                or (processes_stopped and (outcome != "applied" or json.loads(row["document"])["operation"] != "stop"))):
            raise ValueError("invalid control observation")
        observation = _json({"outcome": outcome, "processes_stopped": processes_stopped})
        if row["outcome"] is not None:
            if row["outcome"] != observation:
                raise JournalConflict("control observation is immutable")
            return
        if outcome in {"applied", "denied"} and (row["phase"] != "claimed" or row["instance_id"] != self.instance_id):
            raise JournalConflict("control has no current consumed permit")
        if row["phase"] == "prepared":
            raise JournalConflict("control has no receive intent")
        payload = {"binding_id": self._remote(connection), "control_id": row["id"], "command_id": str(uuid4()),
                   "outcome": outcome, "processes_stopped": processes_stopped}
        self._enqueue(connection, "observe_control", f"control:{row['id']}:outcome", payload)
        connection.execute("UPDATE controls SET phase=?,outcome=? WHERE id=?",
                           ("uncertain" if outcome == "uncertain" else "observed", observation, row["id"]))

    def observe_control(self, control_id: str, outcome: str, processes_stopped: bool = False) -> None:
        """Persist host observation separately from the server's command request."""
        with self._connection(write=True) as connection:
            self._observe_control(connection, self._control(connection, control_id), outcome, processes_stopped)

    def enqueue_report(self, key: str, projection: dict) -> str:
        """Queue bounded metadata with durable monotonic sequence and command ID."""
        _text(key, "report key")
        projection = projection_document(projection)
        encoded = _json(projection, 16384)
        if type(projection) is not dict or projection.get("epoch") != self.binding.epoch:
            raise JournalConflict("report does not match captured epoch")
        with self._connection(write=True) as connection:
            previous = connection.execute("SELECT * FROM outbox WHERE semantic_key=?", (f"report:{key}",)).fetchone()
            if previous:
                if _json(json.loads(previous["payload"])["projection"]) != encoded:
                    raise JournalConflict("report key was reused for different metadata")
                return previous["id"]
            sequence = connection.execute("UPDATE binding SET report_sequence=report_sequence+1 RETURNING report_sequence").fetchone()[0]
            return self._enqueue(connection, "ingest", f"report:{key}",
                                 {"binding_id": self._remote(connection), "sequence": sequence,
                                  "command_id": str(uuid4()), "projection": projection})

    def pending(self, limit: int = 100) -> list[dict]:
        """Return fairly scheduled observations, never request/control dispatches."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid outbox limit")
        with self._connection() as connection:
            return [self._view(row) for row in connection.execute(
                "SELECT * FROM outbox WHERE receipt IS NULL AND superseded_by IS NULL ORDER BY last_attempt,ordinal LIMIT ?", (limit,))]

    def mark_delivery_attempt(self, outbox_id: str) -> None:
        """Advance retry fairness durably before an observation's network call.

        An unresolved request that never reached the server must not monopolize
        every bounded batch and prevent later worker cleanup from being reported.
        This marker grants no execution or outcome authority.
        """
        _uuid(outbox_id)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not row:
                raise JournalConflict("outbox item is unavailable")
            if row["receipt"] is not None or row["superseded_by"] is not None:
                return
            attempt = connection.execute("SELECT coalesce(max(last_attempt),0)+1 FROM outbox").fetchone()[0]
            connection.execute("UPDATE outbox SET last_attempt=? WHERE id=?", (attempt, outbox_id))

    def acknowledge(self, outbox_id: str, receipt: dict) -> None:
        """Retain an exact acknowledgement independently of unrelated failures."""
        _uuid(outbox_id)
        encoded = _json(receipt)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not row:
                raise JournalConflict("outbox item is unavailable")
            if row["receipt"]:
                if row["receipt"] != encoded:
                    raise JournalConflict("outbox receipt changed")
                return
            payload = json.loads(row["payload"])
            fields = {"settle": ("request_id",), "observe_control": ("control_id",), "ingest": ("binding_id", "sequence"),
                      "observe_worker": ("worker_id",), "observe_tool": ("action_id",)}[row["kind"]]
            if any(receipt.get(key) != payload[key] for key in fields):
                raise JournalConflict("outbox acknowledgement has foreign identity")
            if row["kind"] in {"settle", "observe_worker", "observe_tool"} and receipt.get("state") != payload["outcome"]:
                raise JournalConflict("settlement acknowledgement differs")
            if row["kind"] == "observe_control" and receipt.get("outcome") != payload["outcome"]:
                raise JournalConflict("control acknowledgement differs")
            connection.execute("UPDATE outbox SET receipt=? WHERE id=?", (encoded, outbox_id))

    def fence_restart(self) -> None:
        """Explicitly close old dispatch opportunities, retaining possible effects.

        No process-stop or provider-success conclusion is made. This invalidates
        old objects too: their rows no longer have an admissible phase.
        """
        with self._connection(write=True) as connection:
            for row in connection.execute("SELECT * FROM requests WHERE instance_id<>? AND outcome IS NULL "
                                          "AND phase NOT IN ('not_admitted','settled')", (self.instance_id,)).fetchall():
                outcome = "never_started" if row["phase"] == "local_reserved" else "uncertain"
                self._observe(connection, row, outcome)
            for row in connection.execute("SELECT * FROM controls WHERE instance_id<>? AND outcome IS NULL",
                                          (self.instance_id,)).fetchall():
                if row["phase"] == "prepared":
                    connection.execute("UPDATE controls SET phase='not_dispatched' WHERE id=?", (row["id"],))
                elif row["phase"] != "not_dispatched":
                    self._observe_control(connection, row, "uncertain", False)
            for row in connection.execute("SELECT * FROM effects WHERE instance_id<>? AND outcome IS NULL",
                                          (self.instance_id,)).fetchall():
                self._observe_effect(connection, row, "never_started" if row["kind"] == "worker" and row["phase"] == "prepared" else "uncertain")

    def inspect(self) -> dict:
        """Scoped bounded history without private keys or reusable authority."""
        with self._connection() as connection:
            return {"mode": "managed", "binding": asdict(self.binding),
                    "remote_binding_id": connection.execute("SELECT remote_binding_id FROM binding").fetchone()[0],
                    "requests": [self._view(row) for row in connection.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT 100")],
                    "controls": [self._view(row) for row in connection.execute("SELECT * FROM controls ORDER BY rowid DESC LIMIT 100")],
                    "effects": [self._view(row) for row in connection.execute("SELECT * FROM effects ORDER BY rowid DESC LIMIT 100")],
                    "pending_observations": connection.execute("SELECT count(*) FROM outbox WHERE receipt IS NULL AND superseded_by IS NULL").fetchone()[0],
                    "offline_request_allowance": 0}

    def recovery_records(self, kind: str, after: str | None = None, limit: int = 100) -> dict:
        """Page all retained records for a trusted recovery observer, not a GUI."""
        tables = {"requests": ("requests", "local_id", "1=1"), "workers": ("effects", "local_id", "kind='worker'"),
                  "actions": ("effects", "local_id", "kind='action'"), "controls": ("controls", "id", "1=1")}
        if kind not in tables or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid recovery page")
        if after is not None and (type(after) is not str or len(after) > 1024):
            raise ValueError("invalid recovery cursor")
        table, key, predicate = tables[kind]
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM {table} WHERE {predicate} AND {key}>? ORDER BY {key} LIMIT ?",
                                      (after or "", limit + 1)).fetchall()
            return {"records": [self._view(row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1][key] if len(rows) > limit else None}

    def recovery_summary(self) -> dict:
        """Count every unresolved row; the bounded GUI history is never a gate."""
        with self._connection() as connection:
            return {"requests_unknown": connection.execute("SELECT count(*) FROM requests WHERE outcome IS NULL OR outcome='uncertain'").fetchone()[0],
                    "effects_unknown": connection.execute("SELECT count(*) FROM effects WHERE outcome IS NULL OR outcome='uncertain'").fetchone()[0],
                    "controls_unknown": connection.execute("SELECT count(*) FROM controls WHERE phase NOT IN ('observed','not_dispatched')").fetchone()[0],
                    "pending_observations": connection.execute("SELECT count(*) FROM outbox WHERE receipt IS NULL AND superseded_by IS NULL").fetchone()[0]}


def projection_document(value: dict) -> dict:
    """Local mirror of protocol-1 metadata: no freeform prompts or tool content."""
    states = {"draft", "planned", "running", "pausing", "paused", "stopping", "review", "blocked", "recovery_required",
              "completed", "cancelled", "failed"}
    alerts = {"none", "needs_input", "stalled", "allowance_exhausted", "lease_expired", "failed_check", "uncertain_effect", "unconfirmed_stop"}
    counts = {"workers_active", "workers_pending", "requests_known", "requests_held", "requests_unknown", "checks_passed", "checks_failed"}
    if (type(value) is not dict or set(value) != {"version", "epoch", "local_revision", "state", "alert", "counts"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["epoch"]) is not int or not 1 <= value["epoch"] <= 2**53
            or type(value["local_revision"]) is not int or not 0 <= value["local_revision"] <= 2**53
            or type(value["state"]) is not str or value["state"] not in states
            or type(value["alert"]) is not str or value["alert"] not in alerts
            or type(value["counts"]) is not dict or set(value["counts"]) != counts
            or any(type(v) is not int or not 0 <= v <= 10**9 for v in value["counts"].values())):
        raise ValueError("invalid bounded managed metadata")
    return {**value, "counts": dict(value["counts"])}
