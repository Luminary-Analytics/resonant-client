"""Bounded owner inspection of retained run history and immutable evidence.

The caller supplies a scope captured by the trusted desktop host. History and
each content page reauthorize all four scope fields; possession of an identifier
does not disclose another conversation. This is not a worker capability API.
"""
from __future__ import annotations

import codecs
import hashlib
from typing import Any

from .artifacts import SwarmArtifacts, _TEXT_KINDS
from .models import Scope, ScopeDenied, require_id
from .store import SwarmStore


class SwarmInspection:
    """Read retained owner evidence without granting execution authority."""

    def __init__(self, store: SwarmStore):
        self.store = store

    def history(self, scope: Scope, *, before_run_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        """List newest runs, using an authorized run as a stable keyset cursor."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("History page size must be from 1 to 50")
        if before_run_id is not None:
            require_id(before_run_id)
        with self.store._connection() as connection:
            before = None
            if before_run_id is not None:
                self.store._run(connection, scope, before_run_id)
                before = connection.execute("SELECT rowid FROM runs WHERE id=?", (before_run_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT id,objective,state,revision,request_limit,"
                "(SELECT MIN(occurred_at) FROM events WHERE run_id=runs.id) AS started_at "
                "FROM runs WHERE tenant_id=? AND owner_id=? AND project_id=? AND session_id=? "
                "AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                (*scope.values(), before, before, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                self.store._run(connection, scope, row["id"])
                items.append({"run_id": row["id"], "objective": row["objective"][:300],
                    "objective_truncated": len(row["objective"]) > 300, "state": row["state"],
                    "revision": row["revision"], "request_limit": row["request_limit"], "started_at": row["started_at"]})
        return {"items": items, "next_before_run_id": items[-1]["run_id"] if len(rows) > limit else None}

    def read_artifact(self, scope: Scope, run_id: str, artifact_id: str, *, offset: int = 0,
                      limit: int = 8000) -> dict[str, Any]:
        """Verify the complete blob before disclosing a bounded Unicode page.

        Memory use is bounded by a decoding chunk and the requested page. A
        mismatch anywhere in the retained bytes, including after this page,
        prevents disclosure. Text is returned unchanged for textContent display;
        it is evidence, never executable markup or model instructions.
        """
        require_id(run_id)
        require_id(artifact_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("Invalid evidence page offset or limit")
        artifacts = SwarmArtifacts(self.store)
        reference = artifacts.inspect(scope, run_id, artifact_id)
        text_kind = reference.kind in _TEXT_KINDS
        decoder = codecs.getincrementaldecoder("utf-8")("strict") if text_kind else None
        digest, size, characters = hashlib.sha256(), 0, 0
        page: list[str] = []

        def collect(text: str) -> None:
            nonlocal characters
            start, end = max(0, offset - characters), min(len(text), offset + limit - characters)
            if start < end:
                page.append(text[start:end])
            characters += len(text)

        try:
            with artifacts._blob_path(reference.sha256).open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    size += len(chunk)
                    if decoder:
                        collect(decoder.decode(chunk))
                if decoder:
                    collect(decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            raise ValueError("Retained text evidence is not valid UTF-8") from None
        except OSError:
            raise ValueError("Retained evidence content is unavailable") from None
        if digest.hexdigest() != reference.sha256 or size != reference.size:
            raise ValueError("Retained evidence failed complete content verification")
        # Authorization and immutable provenance are checked again after I/O.
        if artifacts.inspect(scope, run_id, artifact_id) != reference:
            raise ScopeDenied("Evidence provenance changed while reading")
        if text_kind and offset > characters:
            raise ValueError("Evidence page starts after the retained text")
        text = "".join(page) if text_kind else None
        return {"run_id": run_id, "artifact": reference.to_dict(), "format": "text" if text_kind else "unsupported",
            "offset": offset, "text": text, "total_characters": characters if text_kind else None,
            "next_offset": offset + len(text) if text_kind and offset + len(text) < characters else None,
            "verified_sha256": reference.sha256,
            "message": ("Complete retained content verified before this text page was disclosed." if text_kind else
                        "This retained artifact is not text. No text preview or visual interpretation is available.")}
