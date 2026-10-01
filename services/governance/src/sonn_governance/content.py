"""Explicit encrypted content and audited retention, separate from metadata.

Trusted adapters supply verified principals. No automatic desktop upload or
network endpoint is installed here. Database/backup deletion is an operator
workflow; a live-store tombstone does not erase historical backups or local files.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.types.json import Jsonb

from .content_screening import screen
from .models import AccessDenied, Conflict, GovernanceError, InvalidRequest, canonical_json, digest, identifier
from .store import GovernanceStore


class ContentUnavailable(GovernanceError):
    """A safe response for missing keys or failed authenticated decryption."""


class ContentKeys:
    """An operator-supplied in-memory AES-256 key ring, never serialized in views.

    Key rotation writes with active_id and retains old keys for existing objects.
    Losing a key fails reads closed. Keys must be protected outside the database.
    Uses the maintained cryptography AESGCM API with fresh random 96-bit nonces:
    https://cryptography.io/en/46.0.0/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM
    """

    def __init__(self, keys: Mapping[str, bytes], active_id: str):
        if (not isinstance(keys, Mapping) or not 1 <= len(keys) <= 16 or active_id not in keys
                or any(type(name) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name)
                       or type(key) is not bytes or len(key) != 32 for name, key in keys.items())):
            raise InvalidRequest("invalid operator content key configuration")
        self._keys = dict(keys)
        self.active_id = active_id

    def encrypt(self, content: bytes, associated_data: bytes):
        nonce = os.urandom(12)
        return self.active_id, nonce, AESGCM(self._keys[self.active_id]).encrypt(nonce, content, associated_data)

    def decrypt(self, key_id: str, nonce: bytes, ciphertext: bytes, associated_data: bytes) -> bytes:
        try:
            return AESGCM(self._keys[key_id]).decrypt(nonce, ciphertext, associated_data)
        except (KeyError, InvalidTag, ValueError):
            raise ContentUnavailable("retained content is unavailable") from None


class ContentStore:
    """Audited bounded content operations with current explicit grants and policy."""

    def __init__(self, core: GovernanceStore, keys: ContentKeys):
        self.core, self.keys = core, keys

    @staticmethod
    def _target(tenant_id, project_id, content_id):
        for value in (tenant_id, project_id, content_id):
            identifier(value)
        return tenant_id, project_id, content_id

    def _authorize(self, connection, principal, tenant_id, project_id, permission, *, content=False):
        self.core._authorize(connection, principal, tenant_id, project_id, permission)
        # Serialize a disclosure/upload against policy revocation, just as the
        # core tenant lock serializes it against membership revocation.
        project = connection.execute(
            "SELECT policy_revision FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s FOR SHARE",
            (tenant_id, project_id),
        ).fetchone()
        if content:
            policy = connection.execute(
                "SELECT document FROM sonn_governance.policies WHERE tenant_id=%s AND project_id=%s AND revision=%s",
                (tenant_id, project_id, project["policy_revision"]),
            ).fetchone()
            if policy is None or policy["document"].get("content_mode") != "explicit":
                raise AccessDenied("resource is unavailable")

    @staticmethod
    def _row(connection, target, *, lock="SHARE"):
        row = connection.execute(
            f"SELECT *,expires_at<=clock_timestamp() AS expired FROM sonn_governance.content_objects "
            f"WHERE tenant_id=%s AND project_id=%s AND content_id=%s FOR {lock}", target,
        ).fetchone()
        if row is None:
            raise AccessDenied("resource is unavailable")
        return row

    @staticmethod
    def _metadata(row):
        return {"content_id": str(row["content_id"]), "revision": row["revision"],
                "created_at": row["created_at"].isoformat(), "expires_at": row["expires_at"].isoformat(),
                "held": row["held"], "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None}

    @staticmethod
    def _aad(target, sha256, size, media_type):
        return canonical_json({"version": 1, "tenant_id": target[0], "project_id": target[1],
            "content_id": target[2], "sha256": sha256, "size": size, "media_type": media_type}).encode("utf-8")

    @staticmethod
    def _replay(connection, principal, tenant_id, command_id, semantics):
        identifier(command_id)
        lock_key = int.from_bytes(hashlib.sha256(f"content:{tenant_id}:{principal.actor_id}:{command_id}".encode()).digest()[:8], "big", signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        old = connection.execute(
            "SELECT semantics_sha256,result FROM sonn_governance.content_receipts WHERE tenant_id=%s AND actor_id=%s AND command_id=%s",
            (tenant_id, principal.actor_id, command_id),
        ).fetchone()
        if old and old["semantics_sha256"] != digest(semantics):
            raise Conflict("command identity has different semantics")
        return old["result"] if old else None

    def _remember(self, connection, principal, target, command_id, semantics, operation, result):
        audit_id = self.core._audit(connection, principal, target[0], operation=operation,
            project_id=target[1], revision=result["revision"], decision_id=target[2], semantics_sha256=digest(semantics))
        connection.execute("INSERT INTO sonn_governance.content_receipts VALUES(%s,%s,%s,%s,%s,%s)",
            (target[0], principal.actor_id, command_id, digest(semantics), Jsonb(result), audit_id))
        self.core._identity(connection, principal)
        return result

    def publish(self, principal, tenant_id: str, project_id: str, content_id: str, *, command_id: str,
                content: bytes, media_type: str, retention_seconds: int):
        """Publish immutable bounded bytes only under current explicit upload policy."""
        target = self._target(tenant_id, project_id, content_id)
        identifier(command_id)
        if (type(content) is not bytes or not 1 <= len(content) <= 1048576
                or type(media_type) is not str or media_type not in {"text/plain", "application/json", "image/png"}
                or type(retention_seconds) is not int or not 1 <= retention_seconds <= 2592000):
            raise InvalidRequest("invalid bounded content")
        screen(content, media_type)
        sha256 = hashlib.sha256(content).hexdigest()
        semantics = {"operation": "publish_content", "target": target, "content_sha256": sha256,
                     "size": len(content), "media_type": media_type, "retention_seconds": retention_seconds}
        with self.core._connection() as connection:
            self._authorize(connection, principal, tenant_id, project_id, "content_write", content=True)
            replay = self._replay(connection, principal, tenant_id, command_id, semantics)
            if replay:
                row = self._row(connection, target)
                if row["deleted_at"] or row["expired"]:
                    raise AccessDenied("resource is unavailable")
                self.core._identity(connection, principal)
                return replay
            # The target lock also serializes two distinct command IDs trying
            # to publish the same immutable object before its row exists.
            key = int.from_bytes(hashlib.sha256(canonical_json(target).encode()).digest()[:8], "big", signed=True)
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            if connection.execute("SELECT 1 FROM sonn_governance.content_objects WHERE tenant_id=%s AND project_id=%s AND content_id=%s", target).fetchone():
                raise Conflict("content identity already exists")
            key_id, nonce, ciphertext = self.keys.encrypt(content, self._aad(target, sha256, len(content), media_type))
            row = connection.execute(
                "INSERT INTO sonn_governance.content_objects(tenant_id,project_id,content_id,key_id,nonce,ciphertext,"
                "content_sha256,size_bytes,media_type,created_by,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "clock_timestamp()+(%s * interval '1 second')) RETURNING *",
                (*target, key_id, nonce, ciphertext, sha256, len(content), media_type, principal.actor_id, retention_seconds),
            ).fetchone()
            return self._remember(connection, principal, target, command_id, semantics, "publish_content", self._metadata(row))

    def inspect(self, principal, tenant_id, project_id, content_id):
        """Operational metadata excludes plaintext, hashes, media, paths and keys."""
        target = self._target(tenant_id, project_id, content_id)
        with self.core._connection() as connection:
            self._authorize(connection, principal, tenant_id, project_id, "metadata_read")
            result = self._metadata(self._row(connection, target))
            self.core._audit(connection, principal, tenant_id, operation="inspect_content", project_id=project_id, decision_id=content_id)
            self.core._identity(connection, principal)
            return result

    def read(self, principal, tenant_id, project_id, content_id, *, offset=0, limit=16384):
        """Authenticate the entire bounded object before returning a byte page."""
        target = self._target(tenant_id, project_id, content_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise InvalidRequest("invalid content page")
        with self.core._connection() as connection:
            self._authorize(connection, principal, tenant_id, project_id, "content_read", content=True)
            row = self._row(connection, target)
            if row["deleted_at"] or row["expired"]:
                raise AccessDenied("resource is unavailable")
            plaintext = self.keys.decrypt(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]),
                self._aad(target, row["content_sha256"], row["size_bytes"], row["media_type"]))
            if len(plaintext) != row["size_bytes"] or hashlib.sha256(plaintext).hexdigest() != row["content_sha256"]:
                raise ContentUnavailable("retained content is unavailable")
            if offset > len(plaintext):
                raise InvalidRequest("content page is outside retained bytes")
            page = plaintext[offset:offset + limit]
            result = {"content_id": content_id, "sha256": row["content_sha256"], "media_type": row["media_type"],
                      "offset": offset, "data": page, "size": len(plaintext),
                      "next_offset": offset + len(page) if offset + len(page) < len(plaintext) else None}
            self.core._audit(connection, principal, tenant_id, operation="read_content", project_id=project_id,
                             revision=row["revision"], decision_id=content_id, semantics_sha256=digest({"target": target, "offset": offset, "limit": limit}))
            self.core._identity(connection, principal)
            if not connection.execute("SELECT %s::timestamptz>clock_timestamp() AS valid", (row["expires_at"],)).fetchone()["valid"]:
                raise AccessDenied("resource is unavailable")
            return result

    def retain(self, principal, tenant_id, project_id, content_id, *, command_id, expected_revision,
               operation, reason=None):
        """Hold, release, or delete one exact object; holds never grant read access.

        A delete explicitly removes the active ciphertext even before expiry.
        Expiry already denies reads. Delete refuses a held object. Key/nonce and
        minimum ownership/time tombstones remain, not the plaintext fingerprint.
        """
        target = self._target(tenant_id, project_id, content_id)
        identifier(command_id)
        if (type(operation) is not str or operation not in {"hold_content", "release_content_hold", "delete_content"}
                or type(expected_revision) is not int or expected_revision < 1
                or (operation == "hold_content" and (type(reason) is not str or reason not in {"owner_request", "security_review", "legal_review"}))
                or (operation != "hold_content" and reason is not None)):
            raise InvalidRequest("invalid retention command")
        semantics = {"operation": operation, "target": target, "expected_revision": expected_revision, "reason": reason}
        with self.core._connection() as connection:
            self._authorize(connection, principal, tenant_id, project_id, "retention_admin")
            replay = self._replay(connection, principal, tenant_id, command_id, semantics)
            if replay:
                self.core._identity(connection, principal)
                return replay
            row = self._row(connection, target, lock="UPDATE")
            if row["revision"] != expected_revision or row["deleted_at"]:
                raise Conflict("content revision changed")
            if operation == "delete_content" and row["held"]:
                raise Conflict("content is retained under an explicit hold")
            if operation == "hold_content":
                connection.execute("UPDATE sonn_governance.content_objects SET held=true,hold_actor=%s,hold_reason=%s,revision=revision+1 "
                    "WHERE tenant_id=%s AND project_id=%s AND content_id=%s", (principal.actor_id, reason, *target))
            elif operation == "release_content_hold":
                connection.execute("UPDATE sonn_governance.content_objects SET held=false,hold_actor=NULL,hold_reason=NULL,revision=revision+1 "
                    "WHERE tenant_id=%s AND project_id=%s AND content_id=%s", target)
            else:
                connection.execute("UPDATE sonn_governance.content_objects SET ciphertext=NULL,content_sha256=NULL,size_bytes=NULL,"
                    "media_type=NULL,deleted_at=clock_timestamp(),revision=revision+1 WHERE tenant_id=%s AND project_id=%s AND content_id=%s", target)
            result = self._metadata(self._row(connection, target))
            return self._remember(connection, principal, target, command_id, semantics, operation, result)
