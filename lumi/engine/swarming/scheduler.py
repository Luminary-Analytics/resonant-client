"""Dispatch ready, authorized graph work without occupying the UI thread.

The supervisor owns readiness, scopes, capacity and accounting. This host module
only joins a committed assignment to its local runner. Pending or ambiguous
launches are never replayed automatically after host loss.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid

from ...gui.runtime import BackendSpec
from .models import AllowanceExceeded, AttemptContext, Command, Conflict, RevisionConflict, ScopeDenied, SwarmError, require_id
from .policy import PolicyDenied
from .workers import DispatchClosed, DispatchGate, SwarmWorkerRunner


class SwarmScheduler:
    """Schedule an admitted plan using one captured model and visible allowance.

    Writer dispatch additionally requires the runner's integration module and an
    explicitly captured full base revision. ``start`` begins scheduling; ``close``
    closes dispatch only. Run Stop and owned execution remain the runner's job.
    """

    def __init__(self, runner: SwarmWorkerRunner, backend_spec: BackendSpec, *, requests_per_worker: int,
                 base_revision: str | None = None, requests_by_work: dict[str, int] | None = None):
        if type(requests_per_worker) is not int or requests_per_worker < 1:
            raise ValueError("Each worker requires an explicit positive request allowance")
        self.runner, self.store, self.authority = runner, runner.store, runner.authority
        self._spec = copy.deepcopy(backend_spec)
        self.requests_per_worker, self.base_revision = requests_per_worker, base_revision
        self._requests_by_work = dict(requests_by_work or {})
        for identity, allowance in self._requests_by_work.items():
            require_id(identity)
            if type(allowance) is not int or allowance < 1:
                raise ValueError("Per-task request allowances must be explicit positive integers")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._dispatch_gate = DispatchGate()
        self._thread: threading.Thread | None = None
        self._blocked: dict[str, str] = {}
        self._error = ""

    def _command(self, kind, payload):
        for _ in range(12):
            revision = self.store.snapshot(self.authority.scope, self.authority.run_id)["run"]["revision"]
            try:
                return self.runner.supervisor.handle(Command(uuid.uuid4().hex, self.authority.run_id,
                    revision, self.authority.epoch, kind, payload), self.authority)
            except RevisionConflict:
                continue  # Only a definite pre-commit rejection is retried.
        raise RevisionConflict("Concurrent team changes prevented dispatch")

    def dispatch_ready(self) -> list[str]:
        """Launch newly admitted items once, leaving proposals and reviews alone."""
        with self._lock:
            if self._stop.is_set() or self._error:
                return []
            snapshot = self.store.snapshot(self.authority.scope, self.authority.run_id)
            if snapshot["run"]["state"] in {"completed", "cancelled", "failed"}:
                self._stop.set()
                self._dispatch_gate.close()
                return []
            if snapshot["run"]["state"] != "running":
                return []
            launched: list[str] = []
            self._blocked = {}
            for item in snapshot["work_items"]:
                if item["state"] != "ready" or self._stop.is_set():
                    continue
                spec = json.loads(item["specification"])
                if spec["write_roots"] and (self.runner.integration is None or self.base_revision is None):
                    self._blocked[item["id"]] = "Writer work requires captured isolation and a base revision"
                    continue
                try:
                    assigned = self._command("assign", {"work_item_id": item["id"],
                        "worker_id": "worker-" + uuid.uuid4().hex,
                        "requests": self._requests_by_work.get(item["id"], self.requests_per_worker),
                        "model": {"provider": self._spec.backend_type, "model": self._spec.model}}).result
                except (Conflict, ScopeDenied, AllowanceExceeded, PolicyDenied) as exc:
                    # A definite admission rejection leaves no dispatch. Capacity
                    # or dependencies can change; the next pass consults the graph.
                    self._blocked = {**self._blocked, item["id"]: str(exc)}
                    continue
                context = AttemptContext(self.authority.scope, self.authority.run_id,
                    assigned["attempt_id"], assigned["worker_id"], self.authority.epoch)
                if self._stop.is_set():
                    self._not_launched(context)
                    break
                writer_id = None
                try:
                    if spec["write_roots"]:
                        writer = self.runner.integration.create_writer(self.authority, context,
                                                                        base_revision=self.base_revision)
                        writer_id = writer["id"]
                except BaseException:
                    # The host has not called runner.start. Record known launch
                    # absence while preserving any uncertain Git intent separately.
                    self._command("worker_stopped", {"attempt_id": context.attempt_id, "attempt_epoch": context.epoch,
                        "outcome": "failed", "evidence": "Host failed isolation before invoking the worker launcher"})
                    raise
                try:
                    self.runner.start(context, copy.deepcopy(self._spec), writer_id=writer_id,
                                      dispatch_gate=self._dispatch_gate)
                except DispatchClosed:
                    self._not_launched(context)
                    break
                except BaseException:
                    # Launch may have crossed an external-effect boundary. Never
                    # claim absence, retry it, or free that assignment blindly.
                    self.runner.stop()
                    raise
                launched.append(context.attempt_id)
            return launched

    def _not_launched(self, context: AttemptContext) -> None:
        """Settle only a dispatch the captured host has proved it never invoked."""
        self._command("worker_stopped", {"attempt_id": context.attempt_id, "attempt_epoch": context.epoch,
            "outcome": "cancelled", "evidence": "Local dispatch closed before invoking the worker launcher"})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.dispatch_ready()
            except (SwarmError, ValueError, OSError) as exc:
                # Diagnostics do not include provider credentials or arbitrary
                # exception bodies from mutable backend construction.
                self._error = f"Dispatch requires inspection ({type(exc).__name__})"
                return
            except Exception:
                self._error = "Dispatch failed; inspect retained launch state before continuing"
                return
            self._stop.wait(.2)

    def start(self) -> None:
        """Begin asynchronous dispatch after the caller's explicit plan decision."""
        with self._lock:
            if self._stop.is_set():
                raise Conflict("Closed scheduling requires a new captured host owner")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="swarm-dispatch")
                self._thread.start()

    def inspect(self) -> dict:
        """Return bounded scheduling blockers without model or credential state."""
        # Git isolation may be slow. Inspection and Stop must not wait behind
        # dispatch's lock; immutable cached replacements make this read cheap.
        return {"active": bool(self._thread and self._thread.is_alive()), "blocked": dict(self._blocked),
                "error": self._error, "requests_per_worker": self.requests_per_worker,
                "requests_by_work": dict(self._requests_by_work)}

    def close(self) -> None:
        """Revoke local dispatch and join its loop; no process exit is inferred."""
        self._stop.set()
        self._dispatch_gate.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
