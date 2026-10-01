"""Immutable blob evidence with explicit run and recipient authorization.

Blob content is flushed and verified before committing its reference in the
swarm database. A failed reference commit can leave an unreferenced blob; it
cannot acknowledge nonexistent evidence. Retention must collect those blobs
separately, never as part of a worker request. This is a local runtime boundary,
not protection against the machine owner or an operating-system sandbox.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import AttemptContext, Conflict, RunAuthority, Scope, ScopeDenied, require_id
from .store import SwarmStore


_TEXT_KINDS = frozenset({"text", "terminal", "diff", "trace", "dom", "accessibility"})
_KINDS = _TEXT_KINDS | {"binary", "image", "audio", "video"}
_ORIGINS = frozenset({"tool_result", "handoff", "context_archive"})


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _mkdir_durable(path: Path) -> None:
    """Flush newly created parent entries on platforms with directory fsync."""
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _sync_directory(created.parent)


@dataclass(frozen=True, slots=True)
class SwarmArtifact:
    """An immutable, attributable reference; possession grants no read access."""

    id: str
    run_id: str
    attempt_id: str
    epoch: int
    origin: str
    model_request_id: str | None
    tool_call_id: str | None
    sha256: str
    size: int
    kind: str
    media_type: str
    label: str

    def to_dict(self) -> dict:
        """Return provenance without leaking a local storage path."""
        return asdict(self)


class SwarmArtifacts:
    """Authorize every artifact operation against captured runtime identities.

    Only trusted runtime adapters instantiate this service and its contexts.
    File content never selects an identity, database, recipient or storage path.
    A published artifact starts private to its producing attempt. The producer
    may share it with another active attempt in this run; copying its textual
    reference into a message does not do that automatically.
    """

    def __init__(self, store: SwarmStore, root: str | Path | None = None):
        self.store = store
        self.root = Path(root or store.path.parent / "artifacts" / "objects").resolve()
        _mkdir_durable(self.root)

    def _live(self, connection: sqlite3.Connection, context: AttemptContext):
        attempt = self.store._attempt(connection, context)
        self.store._admitting(self.store._run(connection, context.scope, context.run_id))
        if attempt["state"] not in {"leased", "running"}:
            raise Conflict("Artifact access requires an active attempt")
        return attempt

    @staticmethod
    def _ref(connection: sqlite3.Connection, run_id: str, artifact_id: str) -> SwarmArtifact:
        row = connection.execute(
            "SELECT * FROM artifact_refs WHERE id=? AND run_id=?", (artifact_id, run_id),
        ).fetchone()
        if row is None:
            raise ScopeDenied("Artifact is unavailable in this run")
        return SwarmArtifact(**dict(row))

    def _blob_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Invalid artifact digest")
        target = self.root / digest[:2] / digest
        # Resolve the directory, not the blob. On Windows, resolving a file that
        # another publisher is linking, reading or unlinking can keep the \\?\
        # prefix (realpath re-checks the file to strip it, and that check can
        # fail mid-race), which then reads as an escape. The digest is 64 hex
        # characters, so only a link in the objects tree could lead elsewhere:
        # a linked directory fails the resolve and a linked blob fails lstat.
        if not target.parent.resolve().is_relative_to(self.root) or target.is_symlink():
            raise ScopeDenied("Artifact storage path escaped its runtime directory")
        return target

    def _write_blob(self, content: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(content).hexdigest()
        target = self._blob_path(digest)
        _mkdir_durable(target.parent)
        target = self._blob_path(digest)
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("Existing artifact blob failed its content digest")
            return digest, len(content)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Publish without replacing an immutable address. Replacing a blob
            # another worker is reading fails on Windows; overwriting a racing
            # corrupted entry would also conceal broken evidence. A hard link
            # atomically exposes the fully flushed bytes, or reports a winner.
            try:
                os.link(temporary, self._blob_path(digest))
            except FileExistsError:
                pass
            _sync_directory(target.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if target.read_bytes() != content:
            raise ValueError("Artifact blob verification failed")
        return digest, len(content)

    def publish_text(
        self, context: AttemptContext, text: str, *, origin: str = "handoff",
        model_request_id: str | None = None, tool_call_id: str | None = None,
        label: str = "", kind: str = "text",
    ) -> SwarmArtifact:
        """Persist UTF-8 evidence, initially visible only to its producer."""
        if not isinstance(text, str):
            raise ValueError("Text artifacts require a string")
        return self.publish_bytes(
            context, text.encode("utf-8"), origin=origin, model_request_id=model_request_id,
            tool_call_id=tool_call_id, label=label, kind=kind, media_type="text/plain; charset=utf-8",
        )

    def publish_bytes(
        self, context: AttemptContext, content: bytes, *, origin: str = "handoff",
        model_request_id: str | None = None, tool_call_id: str | None = None,
        label: str = "", kind: str = "binary", media_type: str = "application/octet-stream",
    ) -> SwarmArtifact:
        """Commit verified content and immutable provenance before acknowledgement.

        Request/action identity is supplied by the trusted execution adapter.
        An artifact alone does not establish that a check or model request ran.
        """
        if not isinstance(content, bytes) or kind not in _KINDS or origin not in _ORIGINS:
            raise ValueError("Invalid artifact content, kind or origin")
        if not isinstance(label, str) or len(label) > 512:
            raise ValueError("Artifact label must be at most 512 characters")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 256:
            raise ValueError("Invalid artifact media type")
        if origin == "tool_result" and (not model_request_id or not tool_call_id):
            raise ValueError("Tool evidence requires its model request and tool call")
        if tool_call_id is not None and origin != "tool_result":
            raise ValueError("Only tool evidence can claim a tool call")
        for value in (model_request_id, tool_call_id):
            if value is not None:
                require_id(value)
        # Avoid writing data for a caller already revoked. Recheck at commit,
        # because pause/stop/recovery can occur while the blob is being written.
        with self.store._connection() as connection:
            self._live(connection, context)
            self._provenance(connection, context, origin, model_request_id)
        digest, size = self._write_blob(content)
        artifact = SwarmArtifact(
            "sart_" + uuid.uuid4().hex, context.run_id, context.attempt_id, context.epoch,
            origin, model_request_id, tool_call_id, digest, size, kind, media_type, label,
        )
        with self.store._connection(write=True) as connection:
            self._live(connection, context)
            self._provenance(connection, context, origin, model_request_id)
            connection.execute(
                "INSERT INTO artifact_refs(id,run_id,attempt_id,epoch,origin,model_request_id,"
                "tool_call_id,sha256,size,kind,media_type,label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(asdict(artifact).values()),
            )
            connection.execute("INSERT INTO artifact_grants(artifact_id,recipient_attempt_id) VALUES(?,?)",
                               (artifact.id, context.attempt_id))
            self.store._event(connection, context.run_id, "artifact_published", artifact.to_dict())
        return artifact

    def publish_tool_observation(
        self, context: AttemptContext, receipt_id: str, output: str,
    ) -> SwarmArtifact:
        """Retain an already-admitted result even while pausing or stopping.

        This trusted executor-only seam admits no tool or artifact disclosure.
        It requires the current epoch/lease and an unresolved action belonging to
        the captured running worker. A returning fenced worker cannot use it.
        """
        require_id(receipt_id)
        if type(output) is not str:
            raise ValueError("Tool observations require text")

        def observe(connection):
            attempt = self.store._attempt(connection, context)
            if attempt["state"] != "running" or attempt["process_state"] != "running":
                raise Conflict("Tool observations require the original running executor")
            action = connection.execute(
                "SELECT * FROM action_receipts WHERE id=? AND attempt_id=? AND epoch=? AND state='admitted'",
                (receipt_id, context.attempt_id, context.epoch),
            ).fetchone()
            if action is None:
                raise ScopeDenied("Admitted tool observation is unavailable to this executor")
            self._provenance(connection, context, "tool_result", action["request_id"])
            return action

        with self.store._connection() as connection:
            observe(connection)
        digest, size = self._write_blob(output.encode("utf-8"))
        with self.store._connection(write=True) as connection:
            action = observe(connection)
            prior = connection.execute(
                "SELECT * FROM artifact_refs WHERE attempt_id=? AND model_request_id=? AND tool_call_id=? AND origin='tool_result'",
                (context.attempt_id, action["request_id"], action["call_id"]),
            ).fetchone()
            if prior is not None:
                artifact = SwarmArtifact(**dict(prior))
                if (artifact.sha256, artifact.size) != (digest, size):
                    raise Conflict("A tool call cannot replace its retained observation")
                return artifact
            artifact = SwarmArtifact("sart_" + uuid.uuid4().hex, context.run_id, context.attempt_id,
                context.epoch, "tool_result", action["request_id"], action["call_id"], digest, size,
                "text", "text/plain; charset=utf-8", f"Observed {action['tool_name']} result")
            connection.execute(
                "INSERT INTO artifact_refs(id,run_id,attempt_id,epoch,origin,model_request_id,"
                "tool_call_id,sha256,size,kind,media_type,label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(asdict(artifact).values()),
            )
            connection.execute("INSERT INTO artifact_grants(artifact_id,recipient_attempt_id) VALUES(?,?)",
                               (artifact.id, context.attempt_id))
            self.store._event(connection, context.run_id, "artifact_published", artifact.to_dict())
            return artifact

    @staticmethod
    def _provenance(connection, context, origin, model_request_id) -> None:
        if model_request_id is None:
            return
        request = connection.execute(
            "SELECT state,purpose FROM model_requests WHERE id=? AND attempt_id=? AND epoch=?",
            (model_request_id, context.attempt_id, context.epoch),
        ).fetchone()
        if request is None:
            raise ScopeDenied("Artifact request provenance is unavailable to this attempt")
        if origin == "tool_result" and (request["state"] != "completed" or request["purpose"] != "main"):
            raise Conflict("Tool evidence requires a completed originating main request")

    def share(self, context: AttemptContext, artifact_id: str, recipient: AttemptContext) -> None:
        """Authorize a same-run peer explicitly; only the producer may reshare."""
        with self.store._connection(write=True) as connection:
            self._share(connection, context, artifact_id, recipient)

    def _share(self, connection, context, artifact_id, recipient) -> None:
        """Compose explicit disclosure with message acceptance in one transaction."""
        self._live(connection, context)
        if (context.scope, context.run_id, context.epoch) != (
            recipient.scope, recipient.run_id, recipient.epoch,
        ):
            raise ScopeDenied("Artifact sharing requires this run and epoch")
        self._live(connection, recipient)
        artifact = self._authorized(connection, context, artifact_id)
        if artifact.attempt_id != context.attempt_id or artifact.epoch != context.epoch:
            raise ScopeDenied("Only the current producing attempt may share its artifact")
        prior = connection.execute(
            "SELECT revoked FROM artifact_grants WHERE artifact_id=? AND recipient_attempt_id=?",
            (artifact_id, recipient.attempt_id),
        ).fetchone()
        if prior is not None and prior["revoked"]:
            raise ScopeDenied("Artifact disclosure was revoked by the supervisor")
        changed = connection.execute(
            "INSERT OR IGNORE INTO artifact_grants(artifact_id,recipient_attempt_id) VALUES(?,?)",
            (artifact_id, recipient.attempt_id),
        ).rowcount
        if changed:
            self.store._event(connection, context.run_id, "artifact_shared", {
                "artifact_id": artifact_id, "recipient_attempt_id": recipient.attempt_id,
            })

    def revoke(self, authority: RunAuthority, artifact_id: str, recipient: AttemptContext) -> None:
        """Revoke future access without deleting retained content or prior evidence."""
        with self.store._connection(write=True) as connection:
            self.store._authority(connection, authority)
            self.store._same_run(authority, recipient)
            self.store._attempt(connection, recipient)
            self._ref(connection, authority.run_id, artifact_id)
            changed = connection.execute(
                "INSERT INTO artifact_grants(artifact_id,recipient_attempt_id,revoked) VALUES(?,?,1) "
                "ON CONFLICT(artifact_id,recipient_attempt_id) DO UPDATE SET revoked=1 "
                "WHERE revoked=0",
                (artifact_id, recipient.attempt_id),
            ).rowcount
            if changed:
                self.store._event(connection, authority.run_id, "artifact_access_revoked", {
                    "artifact_id": artifact_id, "recipient_attempt_id": recipient.attempt_id,
                })

    def _authorized(self, connection, context, artifact_id) -> SwarmArtifact:
        self._live(connection, context)
        artifact = self._ref(connection, context.run_id, artifact_id)
        if not connection.execute(
            "SELECT 1 FROM artifact_grants WHERE artifact_id=? AND recipient_attempt_id=? AND revoked=0",
            (artifact_id, context.attempt_id),
        ).fetchone():
            raise ScopeDenied("Artifact has not been disclosed to this attempt")
        return artifact

    def _read_blob(self, artifact: SwarmArtifact) -> bytes:
        content = self._blob_path(artifact.sha256).read_bytes()
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError("Artifact evidence failed content verification")
        return content

    def read_text_page(
        self, context: AttemptContext, artifact_id: str, offset: int = 0, limit: int = 8000,
    ) -> str:
        """Read verified, disclosed text; offsets are Unicode characters."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("Invalid artifact page offset or limit")
        with self.store._connection() as connection:
            artifact = self._authorized(connection, context, artifact_id)
        if artifact.kind not in _TEXT_KINDS:
            raise ValueError("This artifact is not text; no visual interpretation is available")
        content = self._read_blob(artifact).decode("utf-8")
        # File I/O can race a control command. Recheck before disclosing bytes.
        with self.store._connection() as connection:
            self._authorized(connection, context, artifact_id)
        page = content[offset:offset + limit]
        if offset + len(page) < len(content):
            page += f"\n[More evidence: artifact_read offset={offset + len(page)}]"
        return page

    def inspect(self, scope: Scope, run_id: str, artifact_id: str) -> SwarmArtifact:
        """Read historical provenance as the trusted run owner, not as a worker."""
        with self.store._connection() as connection:
            self.store._run(connection, scope, run_id)
            return self._ref(connection, run_id, artifact_id)

    @staticmethod
    def reference(artifact: SwarmArtifact) -> str:
        """Represent evidence without a storage path or implied disclosure grant."""
        return (f"[artifact:{artifact.id} kind={artifact.kind} bytes={artifact.size} "
                f"sha256={artifact.sha256}]")
