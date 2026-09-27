"""Trusted bridge from one native Session to durable swarm request/action state.

The guard stays in the host runtime; supervisor authority is never placed in a
prompt or exposed as a tool argument. It admits workspace reads and fixed,
attempt-bound collaboration tools in this slice.
Realpath checks reject ordinary path/symlink escapes, but are not an OS sandbox
or protection against a concurrent privileged filesystem attacker. No worker is
launched, lease renewed, or uncertain call replayed by constructing this guard.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
import uuid

from ..execution_guard import (
    FILE_TOOL_NAMES, SWARM_TOOL_NAMES, WRITE_TOOL_NAMES, ExecutionGuardError, ObservationOutcome, RequestPurpose,
    RequestRefused, ToolScopeRefused,
)
from .artifacts import SwarmArtifacts
from .models import (AdmissionClosed, AttemptContext, Command, Conflict, RevisionConflict, RunAuthority, ScopeDenied,
                     SwarmError, require_id)
from .policy import AssignmentGrant, ModelSelection, PolicyProfile, SwarmPolicy, normalize_scope
from .supervisor import SwarmSupervisor
from .tools import validate_swarm_arguments


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class _ArtifactReader:
    """Session-compatible read view which cannot select another attempt."""

    def __init__(self, guard: SwarmExecutionGuard):
        self._guard = guard

    def read_text_page(self, artifact_id: str, offset: int = 0, limit: int = 8000) -> str:
        return self._guard.artifacts.read_text_page(self._guard.context, artifact_id, offset, limit)



class TeamToolRefused(ToolScopeRefused, ScopeDenied):
    """A call outside the assignment: a refusal the boundary survives, and still a ScopeDenied."""

class SwarmExecutionGuard:
    """Bind native request and result observations to one captured assignment.

    The caller must first record ``worker_started`` from its actual in-process
    dispatch, attach ``artifact_reader`` to Session, and keep the supervisor lease
    live independently. A guard that encounters ambiguous persistence closes;
    explicit supervisor reconciliation owns any subsequent recovery.

    ``governance`` (organization.TeamGovernance) checks each request against
    the organization's rules and the budgets before its allowance is reserved,
    and records its usage and audit entries. The guard runs in the host for
    in-process and process participants alike, so both go through it.
    """

    def __init__(
        self, supervisor: SwarmSupervisor, authority: RunAuthority, context: AttemptContext,
        workspace: str | Path, grant: AssignmentGrant, *,
        artifacts: SwarmArtifacts | None = None, retain_inputs: bool = False,
        mailbox: Any = None, integration: Any = None, writer_id: str | None = None,
        input_observer: Any = None, wait_for_admission: Any = None, managed: Any = None,
        governance: Any = None,
    ) -> None:
        if type(retain_inputs) is not bool:
            raise ValueError("Input retention must be explicitly enabled or disabled")
        if input_observer is not None and not callable(input_observer):
            raise ValueError("Request input observation must be a trusted callable")
        if wait_for_admission is not None and not callable(wait_for_admission):
            raise ValueError("Admission waiting must be a trusted callable")
        root = Path(workspace)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Execution requires an existing absolute captured workspace")
        if not isinstance(grant, AssignmentGrant):
            raise ValueError("Execution requires a captured assignment grant")
        if (integration is None) != (writer_id is None):
            raise ValueError("Writer execution requires both a trusted integration adapter and writer identity")
        if not grant.tools <= FILE_TOOL_NAMES | SWARM_TOOL_NAMES | (WRITE_TOOL_NAMES if writer_id else frozenset()):
            raise ScopeDenied("The execution grant exceeds this native file-tool envelope")
        if bool(grant.write_roots) != bool(writer_id):
            raise ScopeDenied("Write grants require an explicit isolated writer binding")
        self.supervisor = supervisor
        self.store = supervisor.store
        self.authority = authority
        self.context = context
        self.workspace = root.resolve(strict=True)
        self.grant = grant
        self.artifacts = artifacts or SwarmArtifacts(self.store)
        if self.artifacts.store.path != self.store.path:
            raise ScopeDenied("Artifact storage must belong to this swarm store")
        self.retain_inputs = retain_inputs
        self.mailbox = mailbox
        self._input_observer = input_observer
        self._wait_for_admission = wait_for_admission
        self._managed = managed
        self._governance = governance
        # Each started request's purpose and start time, for its usage record.
        self._requests: dict[str, tuple[str, float]] = {}
        if managed is not None:
            managed._context(context)
        self.artifact_reader = _ArtifactReader(self)
        self.closed = False
        self._lock = threading.RLock()
        self.integration, self.writer_id = integration, writer_id
        self._writer: dict[str, Any] | None = None
        self.write_tools = frozenset()
        if writer_id is not None:
            require_id(writer_id)
            if integration.store.path != self.store.path:
                raise ScopeDenied("Writer integration belongs to another store")
            writer = integration.validate_writer(context, writer_id)
            if Path(writer["path"]).resolve(strict=True) != self.workspace:
                raise ScopeDenied("Writer workspace differs from its committed isolated path")
            self._writer = dict(writer)
            self.write_tools = grant.tools & WRITE_TOOL_NAMES
        with self.store._connection() as connection:
            self._live(connection, admission=False)
            self._revision = self.store._run(connection, context.scope, context.run_id)["revision"]

    def _live(self, connection, *, admission: bool = True):
        run = self.store._authority(connection, self.authority)
        self.store._same_run(self.authority, self.context)
        attempt = self.store._attempt(connection, self.context)
        if AssignmentGrant.from_dict(json.loads(attempt["grant_json"])) != self.grant:
            raise ScopeDenied("Captured assignment permissions changed or were revoked")
        profile = PolicyProfile.from_dict(json.loads(run["policy_json"]))
        current = SwarmPolicy.admit(profile, model=self.grant.model, tools=tuple(self.grant.tools),
                                    read_roots=self.grant.read_roots, write_roots=self.grant.write_roots)
        if current != self.grant:
            raise ScopeDenied("Captured policy identity is no longer current")
        if self._writer is not None:
            row = connection.execute("SELECT * FROM writer_worktrees WHERE id=? AND run_id=? AND attempt_id=? AND epoch=?",
                                     (self.writer_id, self.context.run_id, self.context.attempt_id,
                                      self.context.epoch)).fetchone()
            if row is None or any(row[key] != self._writer[key] for key in ("path", "base_revision", "repo_key")):
                raise ScopeDenied("Captured writer identity or isolation path changed")
            if admission and row["state"] != "active":
                raise Conflict("Writer effects require an active isolated worktree")
            if admission and row["manifest_json"] != self._writer["manifest_json"]:
                raise ScopeDenied("Writer manifest changed after binding")
        if admission:
            if self.closed:
                raise ExecutionGuardError("Execution guard is closed; explicit reconciliation is required")
            self.store._admitting(run)
            if attempt["cancel_requested"]:
                raise AdmissionClosed("This participant was cancelled")
            if attempt["pause_requested"]:
                raise AdmissionClosed("This participant is paused")
            reservation = connection.execute("SELECT state FROM reservations WHERE attempt_id=?",
                                             (self.context.attempt_id,)).fetchone()
            if reservation is None or reservation["state"] != "reserved":
                raise Conflict("Unresolved assignment accounting prevents admission")
            if attempt["state"] != "running" or attempt["process_state"] != "running":
                raise Conflict("Admission requires an observed running attempt")
        return attempt

    def _admission(self, operation):
        """Pause retries one uncommitted stage, never a provider or tool effect."""
        while True:
            try:
                return operation()
            except AdmissionClosed:
                if self._wait_for_admission is None:
                    raise
                # The callback also rejects cancellation, Stop and lost leases.
                # It runs after the failed stage released its DB connection.
                self._wait_for_admission()

    def _validate_writer(self) -> None:
        """Perform filesystem/Git checks outside SQLite and runner control locks."""
        if self._writer is not None:
            writer = self.integration.validate_writer(self.context, self.writer_id)
            if any(writer[key] != self._writer[key] for key in ("path", "base_revision", "repo_key", "manifest_json")):
                raise ScopeDenied("Writer lease no longer matches its captured workspace and base")

    def _command(self, kind: str, payload: dict[str, Any]):
        # RevisionConflict is emitted before mutation. Only that known rejection
        # is safe to refresh/retry. A successful commit followed by I/O failure
        # must remain ambiguous and must never invoke a provider twice.
        for _ in range(8):
            command = Command(uuid.uuid4().hex, self.context.run_id, self._revision,
                              self.context.epoch, kind, payload)
            try:
                receipt = self.supervisor.handle(command, self.authority)
            except RevisionConflict:
                with self.store._connection() as connection:
                    self._live(connection, admission=False)
                    self._revision = self.store._run(
                        connection, self.context.scope, self.context.run_id)["revision"]
                continue
            self._revision = receipt.revision
            return receipt
        raise RevisionConflict("Concurrent run changes prevented this admission; retry requires a new turn")

    def _request(self, connection, request_id: str, *, for_tool: bool = False):
        row = connection.execute(
            "SELECT * FROM model_requests WHERE id=? AND attempt_id=? AND epoch=?",
            (request_id, self.context.attempt_id, self.context.epoch),
        ).fetchone()
        if row is None:
            raise ScopeDenied("Request does not belong to this captured attempt")
        if for_tool and (row["state"] != "completed" or row["purpose"] != "main"):
            raise Conflict("Tool actions require a completed originating main request")
        return row

    def begin_request(self, *, purpose: RequestPurpose, inputs: dict[str, Any]) -> str:
        """Persist exact input identity and reserved allowance before invocation."""
        with self._lock:
            request_id = None
            try:
                if purpose not in {"primary", "planning", "compression"} or type(inputs) is not dict:
                    raise ValueError("Unsupported request purpose or input envelope")
                selected = ModelSelection.from_dict(inputs.get("_model_selection"))
                if selected != self.grant.model:
                    raise ScopeDenied("The native backend differs from the explicitly assigned model")
                encoded = _json(inputs)
                def preflight():
                    self._validate_writer()
                    with self.store._connection() as connection:
                        self._live(connection)
                        if connection.execute(
                            "SELECT 1 FROM model_requests WHERE attempt_id=? AND state IN ('reserved','started','uncertain')",
                            (self.context.attempt_id,),
                        ).fetchone() or connection.execute(
                            "SELECT 1 FROM action_receipts WHERE attempt_id=? AND state IN ('admitted','uncertain')",
                            (self.context.attempt_id,),
                        ).fetchone():
                            raise Conflict("Unresolved execution prevents another request")
                self._admission(preflight)
                if self._governance is not None:
                    # The organization's rules and the budgets, for this
                    # attempt's own model, before any allowance is reserved:
                    # a refusal leaves nothing uncertain.
                    reason = self._governance.request_refusal(self.context, purpose, self._model())
                    if reason:
                        self._request_refused(purpose, reason)
                        raise RequestRefused(reason)
                request_id = "sreq_" + uuid.uuid4().hex
                self._admission(lambda: self._command("reserve_request", {"attempt_id": self.context.attempt_id,
                    "attempt_epoch": self.context.epoch, "request_id": request_id,
                    "purpose": "main" if purpose == "primary" else "auxiliary"}))
                artifact_id = None
                if self.retain_inputs:
                    artifact_id = self._admission(lambda: self.artifacts.publish_text(
                        self.context, encoded, origin="context_archive", model_request_id=request_id,
                        label="Exact native request inputs").id)
                def record_input():
                    with self.store._connection(write=True) as connection:
                        self._live(connection)
                        self._request(connection, request_id)
                        if connection.execute(
                            "SELECT 1 FROM model_requests WHERE attempt_id=? AND id!=? "
                            "AND state IN ('reserved','started','uncertain')",
                            (self.context.attempt_id, request_id),
                        ).fetchone():
                            raise Conflict("Another unresolved request prevents input admission")
                        connection.execute(
                            "INSERT INTO request_inputs(request_id,input_sha256,purpose,input_artifact_id) VALUES(?,?,?,?)",
                            (request_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), purpose, artifact_id))
                        if self._input_observer is not None:
                            self._input_observer(connection, self.context, inputs, request_id)
                        if self.mailbox is not None:
                            self.mailbox.attach_context(connection, inputs, request_id)
                        self.store._event(connection, self.context.run_id, "request_input_recorded", {
                            "request_id": request_id, "purpose": purpose,
                            "input_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                            "input_artifact_id": artifact_id})
                self._admission(record_input)
                if self._managed is not None:
                    # Remote sends never occur in the retryable Pause stage or
                    # under SQLite/runner locks. One local intent owns one send.
                    self._managed.prepare_request(self.context, request_id, purpose=purpose,
                        model=selected.to_dict(), input_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest())
                self._admission(lambda: self._command("start_request", {"request_id": request_id, "attempt_epoch": self.context.epoch}))
                if self._managed is not None:
                    self._admission(lambda: self._claim_managed("claim_request", request_id))
                self._requests[request_id] = (purpose, time.monotonic())
                return request_id
            except BaseException:
                self.closed = True
                if self._managed is not None and request_id is not None:
                    self._managed.abandon_request(self.context, request_id)
                raise

    def _model(self) -> tuple[str, str]:
        """This attempt's assigned (provider, model): an orchestrator's, or its workers' own."""
        return self.grant.model.provider, self.grant.model.model

    def _request_refused(self, purpose: str, reason: str) -> None:
        """Keep a refused request visible in the run's history; it has no request record."""
        try:
            with self.store._connection(write=True) as connection:
                self._live(connection, admission=False)
                self.store._event(connection, self.context.run_id, "request_refused", {
                    "attempt_id": self.context.attempt_id, "purpose": purpose, "reason": reason[:500]})
        except (OSError, sqlite3.Error, SwarmError):
            pass  # The refusal stands; the audit log already records it.

    def _claim_managed(self, method, identity):
        # No network: synchronize the last local admission check with the
        # journal's one-use claim. Stop cannot commit between these two writes.
        with self.store._connection(write=True) as connection:
            self._live(connection)
            getattr(self._managed, method)(self.context, identity)

    def end_request(
        self, request_id: str, *, outcome: ObservationOutcome,
        usage: dict[str, Any] | None, error: str,
    ) -> None:
        """Retain observed usage verbatim; null usage is never converted to zero."""
        with self._lock:
            try:
                if outcome not in {"completed", "uncertain"} or (usage is not None and type(usage) is not dict):
                    raise ValueError("Invalid request observation")
                if type(error) is not str:
                    raise ValueError("Observation error must be text")
                usage_json = None if usage is None else _json(usage)
                with self.store._connection(write=True) as connection:
                    self._live(connection, admission=False)
                    request = self._request(connection, request_id)
                    if request["state"] != "started":
                        raise Conflict("Only an observed started request can be settled by this guard")
                    changed = connection.execute(
                        "UPDATE request_inputs SET usage_json=?,observation_error=?,observation_outcome=? "
                        "WHERE request_id=? AND observation_outcome IS NULL",
                        (usage_json, error, outcome, request_id)).rowcount
                    if not changed:
                        raise Conflict("Request observation is absent or already recorded")
                    self.store._event(connection, self.context.run_id, "request_observed", {
                        "request_id": request_id, "outcome": outcome, "usage": usage, "error": error})
                purpose, started = self._requests.pop(request_id, ("primary", None))
                if self._governance is not None:
                    try:
                        # Once per request, whatever settles next: the provider
                        # has answered, so its usage is real.
                        self._governance.record_request(self.context, purpose, model=self._model(), stats=usage,
                            elapsed=time.monotonic() - started if started is not None else 0.0)
                    except Exception:  # noqa: BLE001 - usage records never decide a request's outcome
                        pass
                self._command("settle_request", {"request_id": request_id, "attempt_epoch": self.context.epoch,
                              "outcome": outcome, "used": 1 if outcome == "completed" else None})
                if self._managed is not None:
                    self._managed.observe_request(self.context, request_id, outcome=outcome)
                if outcome == "uncertain":
                    self.closed = True
            except BaseException:
                self.closed = True
                raise

    def _path(self, raw: Any, *, roots: tuple[str, ...] | None = None) -> Path:
        if type(raw) is not str or not raw:
            raise ValueError("File tools require an explicit nonempty path")
        if os.name != "nt" and "\\" in raw:
            # Policy roots accept Windows separators for portable configuration,
            # but native POSIX tools would open a literal backslash filename.
            # Never authorize a normalized path different from the effect path.
            raise ScopeDenied("Native POSIX file paths must use forward separators")
        supplied = Path(raw)
        if supplied.is_absolute():
            try:
                relative = supplied.relative_to(self.workspace).as_posix()
            except ValueError as exc:
                raise ScopeDenied("File path is outside the captured workspace") from exc
        else:
            relative = raw
        lexical = normalize_scope(relative)
        roots = self.grant.read_roots if roots is None else roots
        if self._writer is not None and any(part.casefold() == ".git" for part in lexical.split("/")):
            raise ScopeDenied("Writer tools cannot access Git administration")
        if not any(root == "." or lexical == root or lexical.startswith(root + "/")
                   for root in roots):
            raise ScopeDenied("File path exceeds the assignment roots")
        target = (self.workspace / lexical).resolve()
        if self._writer is not None and target != self.workspace / lexical:
            raise ScopeDenied("Writer tools cannot traverse filesystem aliases")
        if not target.is_relative_to(self.workspace):
            raise ScopeDenied("File link resolves outside the captured workspace")
        actual = target.relative_to(self.workspace).as_posix()
        if self._writer is not None and any(part.casefold() == ".git" for part in actual.split("/")):
            raise ScopeDenied("Writer links cannot access Git administration")
        if not any(root == "." or actual == root or actual.startswith(root + "/")
                   for root in roots):
            raise ScopeDenied("File link resolves outside the assignment roots")
        return target

    @staticmethod
    def _pattern(value: Any) -> None:
        if type(value) is not str or not value:
            raise ValueError("Search patterns must be nonempty text")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ":" in normalized or ".." in normalized.split("/"):
            raise ScopeDenied("Search file patterns cannot select another root")
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("Search file patterns cannot contain controls")

    def _tool_scope(self, name: str, arguments: dict[str, Any]) -> None:
        if name not in FILE_TOOL_NAMES | SWARM_TOOL_NAMES | self.write_tools or name not in self.grant.tools or type(arguments) is not dict:
            raise ScopeDenied("Tool is outside the captured file-only assignment")
        if name in SWARM_TOOL_NAMES:
            if self._writer is not None and name == "swarm_submit":
                raise ScopeDenied("Writer submission requires trusted post-cleanup finalization")
            validate_swarm_arguments(name, arguments)
            return
        fields = {"file_read": {"path", "offset", "limit"},
                  "glob": {"path", "pattern", "offset", "limit"},
                  "grep": {"path", "pattern", "glob", "offset", "limit"},
                  "artifact_read": {"artifact_id", "offset", "limit"},
                  "file_write": {"path", "content", "allow_leading_dash"},
                  "file_edit": {"path", "old_text", "new_text", "replace_all", "allow_leading_dash"}}[name]
        if set(arguments) - fields:
            raise ValueError("Unexpected tool arguments")
        if name == "artifact_read":
            require_id(arguments.get("artifact_id"))
            return
        if name in WRITE_TOOL_NAMES:
            text_fields = ("content",) if name == "file_write" else ("old_text", "new_text")
            if any(type(arguments.get(key)) is not str for key in text_fields):
                raise ValueError("File mutations require explicit text arguments")
            if any(key in arguments and type(arguments[key]) is not bool for key in ("allow_leading_dash", "replace_all")):
                raise ValueError("File mutation options must be booleans")
            target = self._path(arguments.get("path"), roots=self.grant.write_roots)
            if name == "file_edit":
                self._path(arguments.get("path"))  # Editing reads existing content.
            if target.exists() and (not target.is_file() or target.stat().st_nlink > 1):
                raise ScopeDenied("Writer tools require an unaliased regular file target")
            return
        target = self._path(arguments.get("path", "." if name != "file_read" else None))
        if target.exists() and not (target.is_file() or (name != "file_read" and target.is_dir())):
            raise ScopeDenied("File tools require a regular file or search directory")
        if name == "glob":
            self._pattern(arguments.get("pattern"))
        if name == "grep":
            if type(arguments.get("pattern")) is not str:
                raise ValueError("Content search requires a text pattern")
            if arguments.get("glob"):
                self._pattern(arguments["glob"])
        if name in {"glob", "grep"} and target.is_dir():
            # Existing search tools have platform-specific recursion semantics.
            # Fail closed on aliases anywhere in their tree rather than assume
            # every backend consistently skips symlinks and Windows junctions.
            for directory, dirs, files in os.walk(target, followlinks=False):
                for child in dirs + files:
                    path = Path(directory) / child
                    if path.is_symlink() or path.resolve() != path.absolute():
                        raise ScopeDenied("Search trees with filesystem aliases require narrower file reads")
                    self._path(str(path))
                    if not (path.is_dir() or path.is_file()):
                        raise ScopeDenied("Search trees may contain only regular files and directories")

    def check_tool(self, request_id: str, name: str, arguments: dict[str, Any]) -> None:
        """Recheck current grant, request origin, actual paths and artifact disclosure."""
        return self._admission(lambda: self._check_tool_once(request_id, name, arguments))

    def _check_tool_once(self, request_id: str, name: str, arguments: dict[str, Any]) -> None:
        with self._lock:
            try:
                with self.store._connection() as connection:
                    self._live(connection)
                    self._request(connection, request_id, for_tool=True)
                # Tree traversal stays outside SQLite locks so stop/lease
                # commands remain writable while a search scope is inspected.
                self._validate_writer()
                try:
                    self._tool_scope(name, arguments)
                    with self.store._connection() as connection:
                        self._live(connection)
                        self._request(connection, request_id, for_tool=True)
                        if name == "artifact_read":
                            self.artifacts._authorized(connection, self.context, arguments["artifact_id"])
                except ScopeDenied as exc:
                    # A path outside the assignment or an undisclosed artifact:
                    # nothing was admitted, so refuse this call and keep the
                    # worker running. It hears why and can narrow the call.
                    self._refused(request_id, name, str(exc))
                    # Name the granted paths: live models kept guessing otherwise.
                    scope = f"You may read: {', '.join(self.grant.read_roots) or 'no paths'}"
                    if self.grant.write_roots:
                        scope += f"; you may write: {', '.join(self.grant.write_roots)}"
                    raise TeamToolRefused(f"{exc}. {scope}.") from None
            except (AdmissionClosed, ToolScopeRefused):
                raise  # Nothing was admitted; resume can repeat this preflight.
            except BaseException:
                self.closed = True
                raise

    def _refused(self, request_id: str, name: str, reason: str) -> None:
        """Keep a refused call visible in the run's history; it has no receipt."""
        with self.store._connection(write=True) as connection:
            self._live(connection)
            self.store._event(connection, self.context.run_id, "tool_refused", {
                "attempt_id": self.context.attempt_id, "request_id": request_id,
                "tool": name, "reason": reason[:500]})

    def begin_tool(
        self, request_id: str, call_id: str, name: str,
        arguments: dict[str, Any], arguments_sha256: str,
    ) -> str:
        """Commit one non-replayable action intent before invoking its read tool."""
        if self._managed is None:
            return self._admission(lambda: self._begin_tool_once(request_id, call_id, name, arguments, arguments_sha256))
        with self._lock:
            receipt_id = "sact_" + uuid.uuid4().hex
            try:
                require_id(call_id)
                if _digest(arguments) != arguments_sha256:
                    raise ValueError("Tool argument content does not match its declared identity")
                self._admission(lambda: self.check_tool(request_id, name, arguments))
                self._managed.prepare_action(self.context, receipt_id, request_id, name=name,
                                             arguments_sha256=arguments_sha256)
                # Recheck paths, authority and unresolved receipts after HTTP.
                # A paused retry reuses this intent and never sends twice.
                self._admission(lambda: self._begin_tool_once(request_id, call_id, name, arguments,
                                                              arguments_sha256, receipt_id=receipt_id))
                self._admission(lambda: self._claim_managed("claim_action", receipt_id))
                return receipt_id
            except BaseException:
                self.closed = True
                self._managed.abandon_action(self.context, receipt_id)
                raise

    def _begin_tool_once(self, request_id, call_id, name, arguments, arguments_sha256, *, receipt_id=None):
        with self._lock:
            try:
                require_id(call_id)
                if _digest(arguments) != arguments_sha256:
                    raise ValueError("Tool argument content does not match its declared identity")
                with self.store._connection() as connection:
                    self._live(connection)
                    self._request(connection, request_id, for_tool=True)
                self._validate_writer()
                self._tool_scope(name, arguments)
                receipt_id = receipt_id or "sact_" + uuid.uuid4().hex
                with self.store._connection(write=True) as connection:
                    self._live(connection)
                    self._request(connection, request_id, for_tool=True)
                    if name == "artifact_read":
                        self.artifacts._authorized(connection, self.context, arguments["artifact_id"])
                    if connection.execute("SELECT 1 FROM action_receipts WHERE request_id=? AND call_id=?",
                                          (request_id, call_id)).fetchone():
                        raise Conflict("A tool call identity cannot be executed twice")
                    if connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND state IN ('admitted','uncertain')",
                                          (self.context.attempt_id,)).fetchone():
                        raise Conflict("An unresolved action prevents another effect")
                    connection.execute(
                        "INSERT INTO action_receipts(id,attempt_id,epoch,request_id,call_id,tool_name,arguments_sha256,state) "
                        "VALUES(?,?,?,?,?,?,?,'admitted')", (receipt_id, self.context.attempt_id,
                        self.context.epoch, request_id, call_id, name, arguments_sha256))
                    self.store._event(connection, self.context.run_id, "action_admitted", {
                        "receipt_id": receipt_id, "request_id": request_id, "call_id": call_id,
                        "tool_name": name, "arguments_sha256": arguments_sha256})
                return receipt_id
            except AdmissionClosed:
                raise  # The atomic action transaction did not commit.
            except BaseException:
                self.closed = True
                raise

    def _action(self, connection, receipt_id: str):
        row = connection.execute("SELECT * FROM action_receipts WHERE id=? AND attempt_id=? AND epoch=?",
                                 (receipt_id, self.context.attempt_id, self.context.epoch)).fetchone()
        if row is None:
            raise ScopeDenied("Action receipt does not belong to this captured attempt")
        if row["state"] != "admitted":
            raise Conflict("Action observation is already recorded or requires reconciliation")
        return row

    def end_tool(
        self, receipt_id: str, *, outcome: ObservationOutcome, output: str,
        is_error: bool, metadata: dict[str, Any],
    ) -> None:
        """Retain tool output as attributed evidence before Session exposes it."""
        with self._lock:
            try:
                if (outcome not in {"completed", "uncertain"} or type(output) is not str
                        or type(is_error) is not bool or type(metadata) is not dict):
                    raise ValueError("Invalid tool observation")
                encoded_metadata = _json(metadata)
                with self.store._connection() as connection:
                    self._live(connection, admission=False)
                    self._action(connection, receipt_id)
                artifact_id = None
                if outcome == "completed":
                    artifact_id = self.artifacts.publish_tool_observation(self.context, receipt_id, output).id
                with self.store._connection(write=True) as connection:
                    self._live(connection, admission=False)
                    self._action(connection, receipt_id)
                    connection.execute("UPDATE action_receipts SET state=?,output_artifact_id=?,is_error=?,metadata_json=? WHERE id=?",
                                       (outcome, artifact_id, int(is_error), encoded_metadata, receipt_id))
                    self.store._event(connection, self.context.run_id, "action_observed", {
                        "receipt_id": receipt_id, "outcome": outcome, "output_artifact_id": artifact_id,
                        "is_error": is_error})
                self._command("checkpoint", {})
                if self._managed is not None:
                    self._managed.observe_action(self.context, receipt_id, outcome=outcome)
                if outcome == "uncertain":
                    self.closed = True
            except BaseException:
                # Keep a previously committed admitted receipt unresolved when
                # output persistence fails; recovery must inspect it, never replay.
                self.closed = True
                raise
