"""Owner-authored steering and exact generated-input attribution.

These trusted local helpers never change participant grants or infer that a
model read, understood, obeyed, or completed an instruction. A receipt records
only the first primary input containing the complete immutable directive.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .models import AdmissionClosed, AttemptContext, Conflict, ScopeDenied
from .store import SwarmStore, canonical_json

_PREFIX = "Owner guidance (generated task input; existing scope, model and permissions remain unchanged):\n"


class OwnerGuidance:
    """Bind a participant's queued owner instructions to native request input."""

    def __init__(self, store: SwarmStore, context: AttemptContext) -> None:
        self.store, self.context = store, context

    def _participant(self, connection: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
        run = self.store._run(connection, self.context.scope, self.context.run_id)
        attempt = self.store._attempt(connection, self.context)
        if not run["managed"] or attempt["state"] not in ("leased", "running"):
            raise Conflict("Guidance is available only to an active managed participant")
        if attempt["cancel_requested"] or run["state"] not in ("running", "pausing", "paused"):
            raise AdmissionClosed("Participant guidance admission is closed")
        return run, attempt

    def pending(self) -> list[dict[str, Any]]:
        """Return bounded pending directives for this captured participant only."""
        with self.store._connection() as connection:
            self._participant(connection)
            return [dict(row) for row in connection.execute(
                "SELECT d.* FROM owner_directives d LEFT JOIN owner_directive_receipts r ON r.directive_id=d.id "
                "WHERE d.run_id=? AND d.attempt_id=? AND d.epoch=? AND r.directive_id IS NULL ORDER BY d.rowid LIMIT 64",
                (self.context.run_id, self.context.attempt_id, self.context.epoch),
            )]

    @staticmethod
    def generated_text(directive: dict[str, Any]) -> str:
        """Format an immutable directive; formatting alone grants no authority."""
        return _PREFIX + canonical_json({key: directive[key] for key in (
            "id", "run_id", "attempt_id", "epoch", "owner_id", "text", "sha256",
        )})

    def record_input(
        self, connection: sqlite3.Connection, context: AttemptContext,
        inputs: dict[str, Any], request_id: str,
    ) -> None:
        """Attest primary input inside the guard's pre-invocation transaction.

        The caller must pass the same transaction that persisted request_inputs.
        A quoted tool/assistant response or human text cannot create a receipt.
        Failure rolls back input, guidance receipt and provider admission together.
        """
        if context != self.context:
            raise ScopeDenied("Guidance input does not belong to its captured participant")
        run, attempt = self._participant(connection)
        self.store._admitting(run)
        self.store._attempt_admitting(attempt)
        request = connection.execute(
            "SELECT i.*,r.attempt_id,r.epoch,r.state,r.purpose AS request_purpose FROM request_inputs i "
            "JOIN model_requests r ON r.id=i.request_id WHERE i.request_id=?", (request_id,),
        ).fetchone()
        if (request is None or request["attempt_id"] != context.attempt_id or request["epoch"] != context.epoch
                or request["state"] != "reserved"):
            raise ScopeDenied("Guidance input requires this participant's reserved native request")
        digest = hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()
        if digest != request["input_sha256"]:
            raise Conflict("Guidance input differs from the persisted native input")
        if request["purpose"] != "primary":
            return
        if request["request_purpose"] != "main":
            raise Conflict("Guidance attribution requires a primary main request")
        history = inputs.get("conversation_history", [])
        if not isinstance(history, list):
            raise ValueError("Native conversation history must be an array")
        rows = connection.execute("SELECT * FROM owner_directives WHERE run_id=? AND attempt_id=? AND epoch=? ORDER BY rowid",
                                  (context.run_id, context.attempt_id, context.epoch)).fetchall()
        known = {self.generated_text(dict(row)): row for row in rows}
        included: set[str] = set()
        revision = connection.execute(
            "SELECT COUNT(*) FROM request_inputs i JOIN model_requests r ON r.id=i.request_id "
            "WHERE r.attempt_id=? AND i.purpose='primary'", (context.attempt_id,),
        ).fetchone()[0]
        for entry in history:
            if not isinstance(entry, dict) or entry.get("role") != "user" or entry.get("input_origin") != "generated":
                continue
            text = entry.get("content")
            if not isinstance(text, str):
                continue
            if text.startswith("<runtime_message>\n") and text.endswith("\n</runtime_message>"):
                text = text[len("<runtime_message>\n"):-len("\n</runtime_message>")]
            if not text.startswith(_PREFIX):
                continue
            directive = known.get(text)
            if directive is None or entry.get("message_id") is not None:
                raise ScopeDenied("Generated owner guidance must match this participant's exact directive")
            directive_id = directive["id"]
            if directive_id in included:
                raise Conflict("A primary input contains duplicate owner guidance")
            included.add(directive_id)
            if connection.execute("SELECT 1 FROM owner_directive_receipts WHERE directive_id=?", (directive_id,)).fetchone():
                continue
            connection.execute("INSERT INTO owner_directive_receipts VALUES(?,?,?,?,?,?)",
                               (directive_id, context.attempt_id, context.epoch, request_id, digest, revision))
            self.store._event(connection, context.run_id, "owner_guidance_input", {
                "directive_id": directive_id, "attempt_id": context.attempt_id, "request_id": request_id,
                "input_sha256": digest, "input_revision": revision,
            })
