"""Local swarm lifecycle and projection over the authoritative engine store."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
import logging
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any
import uuid

from ...gui.runtime import BackendSpec
from ...policy import blocked_reason, current as current_policy
from ..artifacts import project_state_dir
from ..exclusions import ExclusionRules
from . import AttemptContext, Command, Scope, SwarmStore, SwarmSupervisor
from .coordinator import ANSWER_WORKER_PREFIX, CoordinatorPlans, OrchestratorAnswers
from . import collaboration_desktop, managed_collaboration_desktop
from . import connections as team_connections
from .autopilot import MAX_ROUNDS, TeamAutopilot
from .chat_context import chat_context, team_record
from .integration import CheckSpec, SwarmIntegration
from .models import Conflict, IdempotencyConflict, RevisionConflict, ScopeDenied, SwarmError, require_id
from .policy import PolicyProfile, normalize_scopes, team_provider
from .recovery import SwarmRecovery, record_run_host
from .scheduler import SwarmScheduler
from .tools import SWARM_TOOL_NAMES
from .workers import SwarmWorkerRunner

logger = logging.getLogger(__name__)

_READ_TOOLS = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
_WRITE_TOOLS = frozenset({"file_write", "file_edit"})
_TERMINAL = frozenset({"completed", "cancelled", "failed"})
_FIELDS = {"command", "action", "project", "session_id", "run_id", "request_id", "expected_revision",
           "enabled", "objective", "tasks", "request_limit", "max_workers", "after",
           "attempt_id", "attempt_epoch", "candidate_revision", "evidence", "plan_mode",
           "coordinator_requests", "worker_requests", "proposal_id", "sha256", "accept"}
_FIELDS |= {"model_request_id", "action_id", "outcome", "used", "retry_work_items"}
_FIELDS |= {"write_roots", "checks", "writer_ids", "candidate_id", "check_key", "expected_base",
            "target_revision", "approval_id", "work_item_id", "operation_id", "text", "effect_kind", "effect_id"}
_FIELDS |= {"before_run_id", "limit", "artifact_id", "offset"}
_FIELDS |= {"read_roots", "autonomy", "worker_model"}
_FIELDS |= {"execution_mode"}
_FIELDS |= {"managed_epoch", "kind", "process_id", "local_id"}
_FIELDS |= collaboration_desktop.FIELDS
_FIELDS |= managed_collaboration_desktop.FIELDS
_MANAGED_RECOVERY_ACTIONS = {"managed_recovery_inspect", "managed_reconcile_request", "managed_reconcile_action",
                             "managed_reconcile_worker", "managed_reconcile_effect", "managed_retry_observations", "managed_fence_absent"}


# Team actions available while an organization policy applies: reading,
# stopping, revoking and recovery bookkeeping. Every other action can start
# model requests, processes, file changes or sharing (policy_refusal).
_POLICY_SAFE_ACTIONS = frozenset({
    "view", "events", "inspect", "inspect_candidate", "inspect_process", "history",
    "read_artifact", "export_report", "collaboration_inspect", "managed_sharing_inspect",
    "managed_recovery_inspect", "stop", "pause", "pause_worker", "cancel_worker", "revoke",
    "collaboration_revoke", "managed_sharing_revoke", "reject_result", "reconcile_action",
    "reconcile_application", "reconcile_effect", "reconcile_operation", "reconcile_process",
    "reconcile_request", "managed_reconcile_request", "managed_reconcile_action",
    "managed_reconcile_worker", "managed_reconcile_effect", "managed_retry_observations",
    "managed_fence_absent",
})


def policy_refusal(action: str) -> str:
    """Why this team action can't run on this computer, or an empty string.

    Team workers don't yet take an organization's model, mode, shell, approval
    or sharing rules, so the preview starts no new team work where a policy
    applies, as tasks from chat never run on a managed computer. Viewing,
    stopping and recovering retained work stays available.
    """
    if action in _POLICY_SAFE_ACTIONS:
        return ""
    reason = blocked_reason()
    if reason:
        return reason
    policy = current_policy()
    if policy is not None:
        return (f"{policy.organization}'s policy applies on this computer, and the Team preview "
                "doesn't follow organization rules yet, so it can't start or change team work here.")
    return ""


def _path_key(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


@dataclass(frozen=True)
class CapturedSession:
    """Trusted host context; never deserialized from a socket payload."""

    scope: Scope
    workspace: str
    backend_spec: BackendSpec
    instructions: str = ""


class SwarmRuntime:
    """One local foreground swarm owner, independent of browser connection."""

    def __init__(self, settings, *, backend_factory=None, state_root=None, managed_desktop=None):
        self.settings = settings
        self._managed_readers = backend_factory is None
        self._factory = backend_factory or (lambda spec: spec.create_backend(settings))
        self._state_root = state_root or project_state_dir
        self._managed_desktop = managed_desktop
        self._managed_attachments: dict[str, Any] = {}
        self._managed_recoveries: dict[str, Any] = {}
        self._managed_recovery_pages: dict[str, dict[str, Any]] = {}
        self._managed_recovery_operations: dict[str, dict[str, Any]] = {}
        self._managed_sharing: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stores: dict[str, SwarmStore] = {}
        self._runners: dict[str, tuple[CapturedSession, SwarmWorkerRunner]] = {}
        self._schedulers: dict[str, SwarmScheduler] = {}
        # Orchestrator loops for teams whose owner granted autonomy at start.
        self._autopilots: dict[str, TeamAutopilot] = {}
        # Each team's workers' model (private, key resolved), when not the orchestrator's.
        self._worker_specs: dict[str, BackendSpec] = {}
        self._workflows: dict[str, Any] = {}
        self._candidate_details: dict[tuple[str, str], dict[str, Any]] = {}
        self._closed = False
        self._recoveries: dict[str, tuple[CapturedSession, SwarmRecovery]] = {}
        self._recovery_observations: dict[str, dict[str, dict[str, Any]]] = {}
        self._active: set[str] = set()
        self._collaboration_runs: set[str] = set()
        self._collaboration_idle: set[str] = set()
        self._watched: dict[tuple[str, str, str, str], SwarmStore] = {}
        self._storage_uncertain = False
        self._discovery_errors: set[str] = set()
        self._observer_stop = threading.Event()
        self._observer: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        """Read the cached durable admission gate without blocking the UI loop."""
        return bool(self._active or self._storage_uncertain or self._discovery_errors)

    @property
    def navigation_busy(self) -> bool:
        """An owned run pins navigation; orphaned work remains inspectable."""
        return any(run_id in self._runners and run_id not in self._collaboration_idle for run_id in tuple(self._active))

    def _watch(self, store: SwarmStore, scope: Scope) -> None:
        key = (str(store.path), scope.tenant_id, scope.owner_id, scope.project_id)
        if key in self._watched:
            return
        self._watched[key] = store
        self._refresh_ownership()
        if self._observer is None:
            self._observer = threading.Thread(target=self._observe, daemon=True, name="swarm-ownership")
            self._observer.start()

    def _refresh_ownership(self) -> None:
        # Called off the UI loop under _lock. State is independent of whether a
        # browser panel is open or polling. Unknown storage never releases work.
        active: set[str] = set()
        for (_, tenant, owner, project), store in tuple(self._watched.items()):
            with store._connection() as connection:
                active.update(row[0] for row in connection.execute(
                    "SELECT id FROM runs WHERE tenant_id=? AND owner_id=? AND project_id=? AND managed=1 "
                    "AND state NOT IN ('completed','cancelled','failed')", (tenant, owner, project)))
        self._active = active
        idle = set()
        for run_id in self._collaboration_runs & active:
            pair = self._runners.get(run_id)
            if pair and not any(row["alive"] for row in pair[1].inspect_all()):
                snapshot = pair[1].store.snapshot(pair[0].scope, run_id)
                if collaboration_desktop.navigation_idle(snapshot):
                    idle.add(run_id)
        self._collaboration_idle = idle
        self._storage_uncertain = False

    def _observe(self) -> None:
        while not self._observer_stop.wait(.2):
            with self._lock:
                try:
                    self._refresh_ownership()
                except (OSError, sqlite3.Error, SwarmError):
                    self._storage_uncertain = True

    def watch_project(self, workspace: str, scope: Scope) -> None:
        """Discover existing local ownership at startup, without creating state."""
        with self._lock:
            path = Path(self._state_root(workspace)) / "swarm" / "state.sqlite"
            if not path.is_file():
                return
            key = _path_key(workspace)
            store = self._stores.get(key)
            if store is None:
                try:
                    store = self._stores[key] = SwarmStore(path)
                except (OSError, sqlite3.Error, SwarmError):
                    # Keep navigation and inspection available when a retained
                    # database cannot be interpreted by this app version.
                    self._discovery_errors.add(key)
                    return
            self._discovery_errors.discard(key)
            self._watch(store, scope)
            if self._managed_desktop is not None:
                try:
                    managed_scope = self._managed_desktop.scope(scope, workspace)
                except ScopeDenied:
                    pass  # A fixed organization binding does not claim other projects.
                else:
                    self._watch(store, managed_scope)

    def exclusions_for(self, workspace: str) -> ExclusionRules:
        """Files the project's workers may never read, list or send (engine/exclusions.py).

        The same rules a chat in the project gets: Settings'
        ``privacy.excluded_paths``, the project's .lumiignore and the
        organization's ``files.exclude``, relative to the project root.
        """
        getter = getattr(self.settings, "get", None)

        def settings_patterns() -> list[str]:
            try:
                return (getter("privacy", "excluded_paths", []) or []) if callable(getter) else []
            except TypeError:
                # A plain mapping of settings (tests) has no section lookup.
                return []

        return ExclusionRules.for_project(
            workspace,
            settings_patterns=settings_patterns,
            policy_patterns=lambda: current_policy().exclude if current_policy() else (),
        )

    def team_model(self, spec: BackendSpec) -> dict[str, dict[str, Any]]:
        """The connections a team on this captured model needs; Conflict says why it can't run.

        A native provider needs none. A connection (``conn-<id>``) must still
        exist and be OpenAI-compatible (swarming/connections.py). It is read
        once here, so a run keeps its endpoint and headers while Settings change.
        """
        if not spec.model or not team_provider(spec.backend_type):
            raise Conflict("Choose a native provider or an OpenAI-compatible connection, and a model, before starting a team")
        try:
            connection = team_connections.resolve(self.settings, spec.backend_type)
        except ValueError as exc:
            raise Conflict(str(exc)) from None
        return {spec.backend_type: connection} if connection else {}

    def _worker_spec(self, capture: CapturedSession, choice: Any) -> BackendSpec | None:
        """The workers' model when the owner chose one other than the session's; else None.

        The orchestrator plans, answers and reports with the session's model.
        Workers may run another native provider or OpenAI-compatible
        connection (a cheaper, faster or local model), built as ``lumi run``
        builds a session's backend (headless.build_spec).
        """
        if choice is None:
            return None
        if (type(choice) is not dict or set(choice) != {"provider", "model"}
                or any(type(value) is not str or not value.strip() for value in choice.values())):
            raise ValueError("Choose the workers' provider and model")
        provider, model = choice["provider"].strip().lower(), choice["model"].strip()
        if (provider, model) == (capture.backend_spec.backend_type, capture.backend_spec.model):
            return None
        if not team_provider(provider):
            raise Conflict("Team workers use a native provider or an OpenAI-compatible connection")
        from ...headless import build_spec
        return build_spec(self.settings, provider, model, capture.workspace)  # UsageError is a ValueError

    def _saved_worker_spec(self, capture: CapturedSession, setup: dict[str, Any]) -> BackendSpec:
        """A retained team's workers' model with its key resolved, or the orchestrator's."""
        spec = self._worker_spec(capture, setup.get("worker_model"))
        if spec is None:
            return capture.backend_spec
        spec.api_key = spec.resolve_api_key(self.settings)
        return spec

    def _provider_label(self, backend_type: str) -> str:
        """A connection's own name ("NVIDIA NIM") for display; a native provider's id."""
        try:
            connection = team_connections.resolve(self.settings, backend_type)
        except ValueError:
            connection = None
        return connection["name"] if connection else backend_type

    def _team_unavailable(self, spec: BackendSpec) -> str:
        try:
            self.team_model(spec)
        except Conflict as exc:
            return str(exc)
        return ""

    def execution_capture(self, capture: CapturedSession, mode: str) -> CapturedSession:
        """Resolve a new explicit scope without changing a saved personal run."""
        if mode == "personal":
            return capture
        if mode != "managed" or self._managed_desktop is None:
            raise Conflict("Managed teams require an explicit operator configuration at application startup")
        return replace(capture, scope=self._managed_desktop.scope(capture.scope, capture.workspace))

    @staticmethod
    def _execution_mode(capture):
        if capture.scope.tenant_id.startswith("personal:") and capture.scope.tenant_id != f"personal:{capture.scope.owner_id}":
            raise ScopeDenied("Personal team owner does not match its tenant scope")
        return "personal" if capture.scope.tenant_id == f"personal:{capture.scope.owner_id}" else "managed"

    def _validate_execution_mode(self, capture, message):
        mode = self._execution_mode(capture)
        if message.get("execution_mode", mode) != mode:
            raise ScopeDenied("An existing team's execution owner cannot be changed")
        if mode == "managed":
            if self._managed_desktop is None:
                raise Conflict("Restore the captured managed configuration before inspecting this organization team")
            configured = self._managed_desktop
            expected = configured.scope(Scope.personal(configured.config.local_owner_id, capture.scope.project_id, capture.scope.session_id), capture.workspace)
            if capture.scope != expected:
                raise ScopeDenied("Managed team owner differs from the configured organization")

    def _managed_view(self, capture, run_id):
        attachment = self._managed_attachments.get(run_id)
        if attachment is not None:
            return {"available": True, **attachment.view()}
        if self._managed_desktop is None:
            return {"available": False}
        try:
            if self._execution_mode(capture) == "personal":
                self._managed_desktop.scope(capture.scope, capture.workspace)
        except ScopeDenied:
            return {"available": False}
        return self._managed_desktop.configured_view()

    def _store(self, capture: CapturedSession) -> SwarmStore:
        key = _path_key(capture.workspace)
        if key not in self._stores:
            self._stores[key] = SwarmStore(Path(self._state_root(capture.workspace)) / "swarm" / "state.sqlite")
        self._discovery_errors.discard(key)
        self._watch(self._stores[key], capture.scope)
        return self._stores[key]

    def chat_context(self, workspace: str, scope: Scope, run_id: str) -> dict[str, str]:
        """One of a conversation's retained teams, as that conversation's context (``@team:``).

        Reads only: a project that never ran a team gets no team state from
        this. The scope is the conversation's own, so another conversation's
        team is refused like any run outside its scope (chat_context.py).
        """
        require_id(run_id)
        store = self._stores.get(_path_key(workspace))
        if store is None:
            path = Path(self._state_root(workspace)) / "swarm" / "state.sqlite"
            if not path.is_file():
                raise ScopeDenied("Run is unavailable in this scope")
            store = SwarmStore(path, read_only=True)
        return chat_context(store.snapshot(scope, run_id))

    def captured_run(self, run_id: str, project: str, session_id: str) -> CapturedSession | None:
        """Resolve controls for a known run without consulting current selection."""
        pair = self._runners.get(run_id) or self._recoveries.get(run_id)
        if pair is None:
            return None
        capture = pair[0]
        if capture.scope.session_id != session_id or _path_key(capture.workspace) != _path_key(project):
            raise ScopeDenied("The requested team does not belong to this conversation")
        return capture

    @staticmethod
    def _command(supervisor, authority, kind, payload, *, key=None, revision=None):
        for _ in range(12):
            current = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"] if revision is None else revision
            try:
                return supervisor.handle(Command(key or uuid.uuid4().hex, authority.run_id,
                    current, authority.epoch, kind, payload), authority)
            except RevisionConflict:
                if revision is not None:
                    raise
        raise RevisionConflict("Team state is changing; refresh before retrying")

    def _latest(self, store, scope) -> str | None:
        with store._connection() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE tenant_id=? AND owner_id=? AND project_id=? AND session_id=? AND managed=1 "
                "ORDER BY rowid DESC LIMIT 1", scope.values(),
            ).fetchone()
            return row[0] if row else None

    @staticmethod
    def _setup(store, run_id, *, required=False):
        with store._connection() as connection:
            record = connection.execute("SELECT payload,result FROM commands WHERE run_id=? AND actor='desktop-setup' "
                                        "ORDER BY rowid LIMIT 1", (run_id,)).fetchone()
        if record is None:
            if required:
                raise Conflict("This retained team has no captured desktop configuration")
            return {}, {}
        return json.loads(record["payload"]), json.loads(record["result"])

    @staticmethod
    def _writer_configuration(message):
        roots, checks = message.get("write_roots", []), message.get("checks", [])
        if type(roots) is not list or type(checks) is not list or len(checks) > 32:
            raise ValueError("Writer configuration needs relative write roots and at most 32 named checks")
        roots = list(normalize_scopes(tuple(roots)))
        parsed = []
        for item in checks:
            if type(item) is not dict or set(item) - {"key", "argv", "timeout_seconds"} or type(item.get("argv")) is not list:
                raise ValueError("Each trusted check needs a key, argument array and optional timeout")
            check = CheckSpec(item.get("key"), tuple(item["argv"]), item.get("timeout_seconds", 120))
            if check.key == "owner_review" or len(check.argv) > 128 or sum(len(arg.encode()) for arg in check.argv) > 16384:
                raise ValueError("Use a distinct bounded command for each writer check")
            parsed.append({"key": check.key, "argv": list(check.argv), "timeout_seconds": check.timeout_seconds})
        if len({item["key"] for item in parsed}) != len(parsed) or bool(roots) != bool(parsed):
            raise ValueError("Writer access requires unique named checks and explicit write roots together")
        return roots, parsed

    def _view(self, capture, store, run_id=None, *, after=0):
        run_id = run_id or self._latest(store, capture.scope)
        snapshot = store.snapshot(capture.scope, run_id) if run_id else None
        # Counted before the panel's bounded message list is cut.
        record = team_record(snapshot) if snapshot else None
        if snapshot:
            # The owner can inspect all evidence, but one panel response stays
            # bounded. Full history remains in replay/artifact interfaces.
            snapshot["messages"] = snapshot["messages"][-100:]
            snapshot["receipts"] = snapshot["receipts"][-200:]
            directive_receipts = {row["directive_id"]: row for row in snapshot["owner_directive_receipts"]}
            active_attempts = {row["id"] for row in snapshot["attempts"]
                               if row["process_state"] != "stopped"}
            visible_directives = {row["id"] for row in snapshot["owner_directives"][-100:]}
            visible_directives.update(row["id"] for row in snapshot["owner_directives"]
                                      if row["attempt_id"] in active_attempts and row["id"] not in directive_receipts)
            snapshot["owner_directives"] = [row for row in snapshot["owner_directives"] if row["id"] in visible_directives]
            snapshot["owner_directive_receipts"] = [row for row in snapshot["owner_directive_receipts"]
                                                     if row["directive_id"] in visible_directives]
            unresolved_requests = {row["id"] for row in snapshot["model_requests"]
                                   if row["state"] in {"reserved", "started", "uncertain"}}
            for table, key, unresolved in (
                ("model_requests", "id", unresolved_requests),
                ("request_inputs", "request_id", unresolved_requests),
                ("action_receipts", "id", {row["id"] for row in snapshot["action_receipts"] if row["state"] != "completed"}),
            ):
                # Never hide an older unresolved record behind settled history:
                # it still owns allowance/effects and needs an operator decision.
                recent = {row[key] for row in snapshot[table][-100:]}
                snapshot[table] = [row for row in snapshot[table] if row[key] in recent | unresolved]
            for process in snapshot.get("process_observations", []):
                # The durable token identifies the host's private OS job. A
                # browser can inspect liveness but cannot acquire that handle.
                process.pop("launch_token", None)
            snapshot["integration_processes"] = [{key: row.get(key) for key in (
                "id", "epoch", "effect_kind", "effect_id", "argv_sha256", "host_id", "pid", "created_at", "state", "invoked", "exit_code",
            )} for row in snapshot.get("integration_processes", [])]
            if snapshot["run"]["state"] in _TERMINAL:
                self._active.discard(run_id)
            pair = self._runners.get(run_id)
            snapshot["workers"] = pair[1].inspect_all() if pair else []
            scheduler = self._schedulers.get(run_id)
            snapshot["scheduling"] = scheduler.inspect() if scheduler else None
            recovered = self._recoveries.get(run_id)
            snapshot["recovery"] = recovered[1].inspect()["recovery"] if recovered else None
            snapshot["recovery_observations"] = list(self._recovery_observations.get(run_id, {}).values())
            snapshot["recovery_needed"] = bool(not pair and snapshot["run"]["state"] not in _TERMINAL)
            managed_recovery = self._managed_recoveries.get(run_id)
            if managed_recovery is not None:
                snapshot["managed_recovery"] = {**managed_recovery.inspect(**self._managed_recovery_pages.get(run_id, {})),
                    "operation": self._managed_recovery_operations.get(run_id)}
            setup, result = self._setup(store, run_id)
            snapshot["writer_setup"] = ({**result.get("writer_base", {}), "write_roots": setup["write_roots"],
                                         "checks": setup["checks"]} if setup.get("write_roots") else None)
            chosen = setup.get("worker_model")
            snapshot["worker_model"] = ({**chosen, "label": self._provider_label(chosen["provider"])}
                                        if chosen else None)
            workflow = self._workflows.get(run_id)
            if "integration_operations" in snapshot:
                # The private captured supervisor identity is never a browser capability.
                if workflow:
                    snapshot["integration_operations"] = workflow.inspect()
                else:
                    from .workflow import IntegrationWorkflow
                    snapshot["integration_operations"] = [IntegrationWorkflow.inspect_row(row)
                                                          for row in snapshot["integration_operations"]]
            snapshot["candidate_details"] = [value for (owner, _), value in self._candidate_details.items() if owner == run_id]
        unavailable = self._team_unavailable(capture.backend_spec)
        return {"available": not unavailable,
                "enabled": self.settings.get("swarming", "enabled", False) is True,
                "storage_attention": bool(self._storage_uncertain or self._discovery_errors),
                "model": {"provider": capture.backend_spec.backend_type, "model": capture.backend_spec.model,
                          "label": self._provider_label(capture.backend_spec.backend_type)},
                "execution_mode": self._execution_mode(capture), "managed": self._managed_view(capture, run_id),
                "run": snapshot,
                "coordinator_planning": self._planning_view(store, run_id, snapshot) if snapshot else None,
                "autonomy": self._autonomy_view(store, run_id, snapshot),
                # Beside the orchestrator's model-written report (chat_context.team_record).
                "team_record": record,
                "collaboration": collaboration_desktop.view(store, capture.scope, run_id) if run_id and capture.scope.tenant_id == f"personal:{capture.scope.owner_id}" else None,
                "managed_collaboration": self._managed_sharing[run_id].view() if run_id in self._managed_sharing else managed_collaboration_desktop.historical_view(store, capture.scope, run_id),
                "events": [asdict(event) for event in store.events(capture.scope, run_id, after=after)] if run_id else [],
                "message": unavailable or "Workers share scoped findings and isolated changes. Submitted results await independent review."}

    def _autonomy_view(self, store, run_id, snapshot=None):
        """The orchestrator loop's status, or the retained grant once this host no longer runs it."""
        if not run_id:
            return None
        autopilot = self._autopilots.get(run_id)
        if autopilot is not None:
            return autopilot.inspect()
        grant = self._setup(store, run_id)[0].get("autonomy")
        if not grant:
            return None
        if snapshot is None:
            return {"enabled": True, "active": False, "rounds": grant["rounds"], "apply": grant.get("apply") is True,
                    "phase": "stopped", "detail": "The orchestrator loop isn't running on this host.",
                    "final_report": None}
        # Rebuilt from the retained plans (autopilot.TeamAutopilot.retained_plans).
        report, worked, _ = TeamAutopilot.retained_plans(snapshot)
        done = snapshot["run"]["state"] in _TERMINAL
        return {"enabled": True, "active": False, "rounds": grant["rounds"], "apply": grant.get("apply") is True,
                "round": max(1, min(worked, grant["rounds"])), "closing": False,
                "phase": "finished" if done and snapshot["run"]["state"] == "completed" else "stopped",
                "detail": (f"The team is {snapshot['run']['state']}." if done else
                           "The orchestrator loop isn't running on this host. Recover and continue the team to resume it."),
                "final_report": report}

    def _planning_view(self, store, run_id, snapshot):
        setup, _ = self._setup(store, run_id)
        with store._connection() as connection:
            remaining = max(0, snapshot["run"]["request_limit"] - store._allocated(connection, run_id))
        original = next((row for row in snapshot["attempts"] if row["kind"] == "coordinator"), None)
        roots = json.loads(original["grant_json"])["read_roots"] if original else []
        reason = None
        if setup.get("plan_mode") != "coordinator":
            reason = "Follow-up planning requires a team created with coordinator planning"
        elif run_id not in self._runners or run_id not in self._schedulers:
            reason = "Follow-up planning requires this team's current execution host and scheduler"
        elif snapshot["run"]["state"] != "running":
            reason = "Resume a running team before requesting another plan"
        elif any(row["kind"] == "coordinator" and (row["state"] in {"leased", "running", "uncertain"}
                 or row["process_state"] != "stopped") for row in snapshot["attempts"]):
            reason = "The current coordinator must finish or be reconciled first"
        elif remaining < 1:
            reason = "The original team request allowance has no unallocated requests"
        return {"available": reason is None, "reason": reason, "remaining_requests": remaining,
                "default_requests": min(setup.get("coordinator_requests", 3), remaining) if remaining else 1,
                "read_roots": roots, "worker_requests": setup.get("worker_requests")}

    def _request_plan(self, capture, message, *, closing=False, retry_reason=""):
        """Admit one explicit fresh planner, then launch outside the UI/control lock.

        ``closing`` comes only from the orchestrator loop (autopilot.py): its
        last turn writes the final report and may not start more work.
        """
        message = copy.deepcopy(message)
        with self._lock:
            allowed = {"command", "action", "project", "session_id", "run_id", "request_id", "expected_revision",
                       "execution_mode", "coordinator_requests", "read_roots"}
            if set(message) - allowed:
                raise ValueError("Follow-up planning cannot change the team's captured worker contract")
            require_id(message.get("request_id"))
            if self._closed:
                raise Conflict("This desktop runtime has closed; reopen the retained team")
            run_id = message.get("run_id")
            require_id(run_id)
            pair = self._runners.get(run_id)
            if pair is None or run_id not in self._schedulers:
                raise Conflict("Follow-up planning requires this team's current execution host and scheduler")
            if pair[0].scope != capture.scope:
                raise ScopeDenied("Team scope differs from its captured owner")
            self.captured_run(run_id, capture.workspace, capture.scope.session_id)
            store, runner = pair[1].store, pair[1]
            setup, _ = self._setup(store, run_id, required=True)
            if setup.get("plan_mode") != "coordinator":
                raise Conflict("Follow-up planning requires a team created with coordinator planning")
            requests, revision = message.get("coordinator_requests"), message.get("expected_revision")
            if type(requests) is not int or not 1 <= requests <= 1000:
                raise ValueError("Choose an explicit coordinator request allowance from 1 to 1000")
            if type(revision) is not int or revision < 0:
                raise ValueError("Refresh the team before requesting a follow-up plan")
            snapshot = store.snapshot(capture.scope, run_id)
            roots = message.get("read_roots", self._planning_view(store, run_id, snapshot)["read_roots"])
            if type(roots) is not list or any(type(root) is not str for root in roots):
                raise ValueError("Coordinator read roots must be an explicit list; [] means findings only")
            roots = list(normalize_scopes(tuple(roots)))
            tools = _READ_TOOLS - {"swarm_submit"}
            if not roots:
                tools -= {"file_read", "glob", "grep"}
            payload = {"worker_id": "coordinator-" + hashlib.sha256(message["request_id"].encode()).hexdigest(),
                       "requests": requests, "model": {"provider": pair[0].backend_spec.backend_type,
                                                         "model": pair[0].backend_spec.model},
                       "tools": sorted(tools), "read_roots": roots}
            # Only the first acknowledged atomic commit may dispatch. Replays
            # return their durable result without a second runner.start call.
            receipt, fresh = runner.supervisor.handle_once(Command(message["request_id"], run_id, revision,
                runner.authority.epoch, "start_coordinator", payload), runner.authority)
            if not fresh:
                return self._view(capture, store, run_id)
            context = AttemptContext(capture.scope, run_id, receipt.result["attempt_id"],
                                     receipt.result["worker_id"], runner.authority.epoch)
            plans = CoordinatorPlans(runner.supervisor, runner.authority, follow_up=True,
                autonomous=bool(setup.get("autonomy")), closing=closing is True, retry_reason=retry_reason,
                applies_changes=(setup.get("autonomy") or {}).get("apply") is True,
                allowed_criteria=frozenset({"owner_review"} | {check["key"] for check in setup.get("checks", [])}))
            spec = copy.deepcopy(pair[0].backend_spec)
        # Stop can close runner admission while slow scope/process setup runs.
        # Any ambiguous launch leaves its committed pending intent/reservation;
        # repeating the owner request never obtains another dispatch opportunity.
        self._start_turn(runner, context, spec, plans)
        with self._lock:
            return self._view(capture, store, run_id)

    def _start_turn(self, runner, context, spec, plans) -> None:
        """Launch an admitted orchestrator turn; one that never launched is settled as such.

        Its admission is already committed. If the runner refused it before
        taking the turn (the team paused in between, or its input was refused),
        nothing was invoked: record that, as the scheduler does for a closed
        dispatch, so the turn doesn't hold the team forever. Any other failure
        may have crossed the launcher, so the turn stays pending for recovery
        and is never replayed.
        """
        try:
            runner.start_coordinator(context, spec, plans)
        except (SwarmError, ValueError):
            if context.attempt_id not in runner._workers:
                try:
                    self._command(runner.supervisor, runner.authority, "worker_stopped", {
                        "attempt_id": context.attempt_id, "attempt_epoch": context.epoch, "outcome": "cancelled",
                        "evidence": "The host refused to launch this orchestrator turn before invoking it"})
                except Exception:  # noqa: BLE001 - the original refusal is the one to report
                    logger.exception("Could not settle an orchestrator turn that never launched")
            raise

    def _answer_workers(self, capture, *, run_id: str, questions: list[int], request_id: str,
                        expected_revision: int, requests: int):
        """Admit one short orchestrator turn that answers workers mid-round (autopilot.py).

        Only the orchestrator loop starts it, for a team whose owner let the
        orchestrator run it. It spends unallocated team requests, has only
        ``swarm_send`` and proposes no work (coordinator.OrchestratorAnswers).
        """
        with self._lock:
            require_id(request_id)
            if self._closed:
                raise Conflict("This desktop runtime has closed; reopen the retained team")
            pair = self._runners.get(run_id)
            if pair is None:
                raise Conflict("Answering workers requires this team's current execution host")
            if pair[0].scope != capture.scope:
                raise ScopeDenied("Team scope differs from its captured owner")
            store, runner = pair[1].store, pair[1]
            if not self._setup(store, run_id, required=True)[0].get("autonomy"):
                raise Conflict("Only an orchestrator the owner let run the team answers its workers")
            if type(requests) is not int or not 1 <= requests <= 1000:
                raise ValueError("Choose an explicit answer request allowance from 1 to 1000")
            # Only swarm_send: its input carries the questions, their senders'
            # ids and the team's findings. Live answer turns with read tools
            # spent their requests exploring the project and never answered.
            tools = {"swarm_send"}
            payload = {"worker_id": ANSWER_WORKER_PREFIX + hashlib.sha256(request_id.encode()).hexdigest()[:32],
                       "requests": requests, "model": {"provider": pair[0].backend_spec.backend_type,
                                                        "model": pair[0].backend_spec.model},
                       "tools": sorted(tools), "read_roots": []}
            receipt, fresh = runner.supervisor.handle_once(Command(request_id, run_id, expected_revision,
                runner.authority.epoch, "start_coordinator", payload), runner.authority)
            if not fresh:
                return
            context = AttemptContext(capture.scope, run_id, receipt.result["attempt_id"],
                                     receipt.result["worker_id"], runner.authority.epoch)
            answers = OrchestratorAnswers(runner.supervisor, runner.authority, questions=tuple(questions))
            spec = copy.deepcopy(pair[0].backend_spec)
        # As for planning: launch outside the control lock; a committed turn
        # whose launch is ambiguous stays visible, never replayed.
        self._start_turn(runner, context, spec, answers)

    def _start(self, capture, store, message):
        if self.settings.get("swarming", "version", 1) != 1 or self.settings.get("swarming", "enabled", False) is not True:
            raise Conflict("Enable the swarming preview before starting a team")
        connections = self.team_model(capture.backend_spec)
        worker_spec = self._worker_spec(capture, message.get("worker_model"))
        if worker_spec is not None:
            connections = {**connections, **self.team_model(worker_spec)}
        objective = message.get("objective")
        tasks = message.get("tasks")
        plan_mode = message.get("plan_mode", "manual")
        if plan_mode not in {"manual", "coordinator"}:
            raise ValueError("Choose manual investigations or coordinator planning")
        # The owner's autonomy grant (autopilot.py): the orchestrator plans,
        # dispatches and continues in rounds without approving each step, and
        # with ``apply`` also applies writers' changes that pass every check.
        autonomy = message.get("autonomy")
        if autonomy is not None:
            if plan_mode != "coordinator":
                raise ValueError("Only an orchestrator-planned team can run itself")
            if (type(autonomy) is not dict or not {"rounds"} <= set(autonomy) <= {"rounds", "apply"}
                    or type(autonomy["rounds"]) is not int or not 1 <= autonomy["rounds"] <= MAX_ROUNDS):
                raise ValueError(f"Let the orchestrator run one to {MAX_ROUNDS} rounds")
            if type(autonomy.get("apply", False)) is not bool:
                raise ValueError("Choose whether the orchestrator applies checked changes")
        coordinator_requests = message.get("coordinator_requests", 3)
        worker_requests = message.get("worker_requests", 4)
        requests = message.get("request_limit", 20)
        workers = message.get("max_workers", 2)
        write_roots, checks = self._writer_configuration(message)
        applies = autonomy is not None and autonomy.get("apply") is True
        if applies and not write_roots:
            raise ValueError("Applying checked changes needs writable folders and verification checks")
        managed = self._execution_mode(capture) == "managed"
        if managed and message.get("execution_mode") != "managed":
            raise ScopeDenied("Select managed execution explicitly for each new organization team")
        if write_roots:
            support = SwarmIntegration.writer_support()
            if not support["supported"]:
                raise Conflict(support["reason"])
        if type(objective) is not str or not objective.strip() or len(objective.encode()) > 8192:
            raise ValueError("Describe a team objective of at most 8 KiB")
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("Choose one to eight worker slots")
        if plan_mode == "coordinator":
            if tasks not in (None, []):
                raise ValueError("Coordinator planning begins from the objective; manual tasks require manual mode")
            tasks = []
        elif type(tasks) is not list or not 1 <= len(tasks) <= workers:
            raise ValueError("Provide one task per available worker slot")
        if (type(coordinator_requests) is not int or not 1 <= coordinator_requests <= 1000
                or type(worker_requests) is not int or not 1 <= worker_requests <= 1000):
            raise ValueError("Coordinator and worker request allowances must be positive integers up to 1000")
        minimum_requests = (coordinator_requests + worker_requests if plan_mode == "coordinator"
                            else worker_requests * len(tasks) if write_roots else len(tasks))
        if type(requests) is not int or not minimum_requests <= requests <= 1000:
            raise ValueError("Request allowance must cover each task and be at most 1000")
        run_id = "swarm_" + hashlib.sha256(message["request_id"].encode()).hexdigest()
        setup_payload = {"objective": objective, "tasks": tasks, "request_limit": requests,
                         "plan_mode": plan_mode,
                         **({"coordinator_requests": coordinator_requests, "worker_requests": worker_requests}
                            if plan_mode == "coordinator" else {}),
                         **({"autonomy": {"rounds": autonomy["rounds"], **({"apply": True} if applies else {})}}
                            if autonomy is not None else {}),
                         "max_workers": workers, "model": {"provider": capture.backend_spec.backend_type,
                                                           "model": capture.backend_spec.model},
                         **({"worker_model": {"provider": worker_spec.backend_type, "model": worker_spec.model}}
                            if worker_spec is not None else {})}
        if write_roots:
            setup_payload.update(write_roots=write_roots, checks=checks, worker_requests=worker_requests)
        if managed:
            setup_payload["execution_mode"] = "managed"
        # A lost socket acknowledgement cannot launch the same request twice.
        existing = self._runners.get(run_id)
        if existing:
            self.captured_run(run_id, capture.workspace, capture.scope.session_id)
            if existing[0].scope != capture.scope:
                raise ScopeDenied("Team scope differs from its captured owner")
            with store._connection() as connection:
                if self.store_setup(connection, store, run_id, message["request_id"], setup_payload) is None:
                    raise IdempotencyConflict("Team setup is incomplete; inspect retained state before continuing")
            return run_id
        if self.busy:
            raise Conflict("Finish or stop the current team before starting another")
        latest = self._latest(store, capture.scope)
        if latest and store.snapshot(capture.scope, latest)["run"]["state"] not in _TERMINAL:
            raise Conflict("The previous team requires review, stop or explicit recovery")
        work_items = []
        for index, task in enumerate(tasks):
            if type(task) is not dict or set(task) - {"objective", "read_roots", "role", "write_roots", "criteria"}:
                raise ValueError("Each task requires an objective and explicit scope/acceptance configuration")
            text = task.get("objective")
            if type(text) is not str or not text.strip() or len(text.encode()) > 8192:
                raise ValueError("Each worker needs a nonempty task of at most 8 KiB")
            roots = task.get("read_roots", ["."])
            if type(roots) is not list:
                raise ValueError("Read roots must be a list of relative project paths")
            roots = list(normalize_scopes(tuple(roots)))
            role = task.get("role", "explore")
            writes, criteria = task.get("write_roots", []), task.get("criteria", ["owner_review"])
            if role not in {"explore", "implement"} or type(writes) is not list or type(criteria) is not list:
                raise ValueError("Choose an investigation or a scoped implementation task")
            writes = list(normalize_scopes(tuple(writes)))
            if role == "implement":
                if (not write_roots or not writes or not criteria or any(type(item) is not str for item in criteria)
                        or len(criteria) != len(set(criteria)) or set(criteria) - {check["key"] for check in checks}):
                    raise ValueError("Each writer requires explicit write roots and named trusted checks")
                if any(not any(parent == "." or root == parent or root.startswith(parent + "/")
                               for parent in write_roots) for root in writes):
                    raise ScopeDenied("Task write roots exceed the configured team write access")
            elif writes or criteria != ["owner_review"]:
                raise ValueError("Investigation tasks require owner review and cannot write")
            work_items.append({"id": f"{run_id}_task_{index + 1}", "objective": text, "role": role,
                "read_roots": roots, "write_roots": writes,
                "tools": sorted(_READ_TOOLS | (_WRITE_TOOLS if writes else frozenset())), "criteria": criteria})
        supervisor = SwarmSupervisor(store)
        # Resolve credential material once for this run; later settings edits
        # cannot give peers different accounts during concurrent construction.
        # Source metadata stays intact and the private key is never serialized.
        private_spec = copy.deepcopy(capture.backend_spec)
        private_spec.api_key = private_spec.resolve_api_key(self.settings)
        capture = replace(capture, backend_spec=private_spec)
        # The workers' model gets its key the same way, once per run.
        if worker_spec is not None:
            worker_spec.api_key = worker_spec.resolve_api_key(self.settings)
        workers_spec = worker_spec or capture.backend_spec
        integration = SwarmIntegration(store, capture.workspace,
            root=Path(self._state_root(capture.workspace)) / "swarm" / "worktrees") if write_roots else None
        writer_base = integration.capture_base() if integration else None
        authority = supervisor.create(capture.scope, supervisor_id=uuid.uuid4().hex, objective=objective,
            request_limit=requests, run_id=run_id, lease_seconds=60,
            policy=PolicyProfile(1, _READ_TOOLS | (_WRITE_TOOLS if write_roots else frozenset()),
                frozenset({capture.backend_spec.backend_type, workers_spec.backend_type}), max_workers=workers,
                write_roots=tuple(write_roots)))
        self._active.add(run_id)
        self._worker_specs[run_id] = workers_spec
        runner = None
        try:
            record_run_host(store, authority)
            attachment = self._managed_desktop.attach(authority, capture.workspace, self._state_root(capture.workspace)) if managed else None
            if attachment is not None:
                self._managed_attachments[run_id] = attachment
                effects = attachment.effects(store)  # Coverage exists before any owner effect, including reader-only epochs.
                if integration is not None:
                    integration = SwarmIntegration(store, capture.workspace,
                        root=Path(self._state_root(capture.workspace)) / "swarm" / "worktrees", managed_effects=effects)
            runner = SwarmWorkerRunner(supervisor, authority, capture.workspace,
                                       backend_factory=self._factory, project_instructions=capture.instructions,
                                       exclusions=self.exclusions_for(capture.workspace), connections=connections,
                                       managed_readers=self._managed_readers, integration=integration,
                                       managed_runtime=attachment.runtime if attachment is not None else None,
                                       **({"writer_process_factory": None} if not self._managed_readers else {}))
            self._runners[run_id] = (capture, runner)
            with store._connection(write=True) as connection:
                store._remember(connection, run_id, "desktop-setup", message["request_id"], setup_payload,
                                {"run_id": run_id, **({"writer_base": writer_base} if writer_base else {})})
            if attachment is not None:
                attachment.start_pump(runner, lambda: store.snapshot(authority.scope, authority.run_id))
            if plan_mode == "coordinator":
                self._schedulers[run_id] = SwarmScheduler(runner, workers_spec,
                    requests_per_worker=worker_requests, base_revision=writer_base["base_revision"] if writer_base else None)
                assigned = self._command(supervisor, authority, "start_coordinator", {
                    "worker_id": "coordinator", "requests": coordinator_requests,
                    "model": {"provider": capture.backend_spec.backend_type, "model": capture.backend_spec.model},
                    "tools": sorted(_READ_TOOLS - {"swarm_submit"}), "read_roots": ["."],
                }).result
                context = AttemptContext(capture.scope, run_id, assigned["attempt_id"], assigned["worker_id"], authority.epoch)
                runner.start_coordinator(context, capture.backend_spec,
                    CoordinatorPlans(supervisor, authority, autonomous=autonomy is not None, applies_changes=applies,
                                     allowed_criteria=frozenset({"owner_review"} | {check["key"] for check in checks})))
                if autonomy is not None:
                    autopilot = self._autopilots[run_id] = TeamAutopilot(self, run_id, rounds=autonomy["rounds"],
                                                                         apply=applies)
                    autopilot.start()
                return run_id
            self._command(supervisor, authority, "plan", {"work_items": work_items})
            if integration:
                scheduler = self._schedulers[run_id] = SwarmScheduler(runner, workers_spec,
                    requests_per_worker=worker_requests, base_revision=writer_base["base_revision"])
                scheduler.start()
                return run_id
            # Persist all participants before the first native call, so every
            # worker can discover its peers without a scheduling-order race.
            contexts = []
            for index, item in enumerate(work_items):
                allocation = requests // len(tasks) + (1 if index < requests % len(tasks) else 0)
                assigned = self._command(supervisor, authority, "assign", {
                    "work_item_id": item["id"], "worker_id": f"reader-{index + 1}", "requests": allocation,
                    "model": {"provider": workers_spec.backend_type, "model": workers_spec.model},
                }).result
                contexts.append(AttemptContext(capture.scope, run_id, assigned["attempt_id"], assigned["worker_id"], authority.epoch))
            for context in contexts:
                runner.start(context, workers_spec)
        except BaseException:
            # No automatic replay. Any committed dispatch whose launch cannot
            # be confirmed remains visible for explicit host reconciliation.
            if runner is not None:
                runner.stop()
            else:
                stopped = self._command(supervisor, authority, "stop", {})
                if stopped.state in _TERMINAL:
                    self._active.discard(run_id)
            raise
        return run_id

    @staticmethod
    def store_setup(connection, store, run_id, command_id, payload):
        """Validate a lost-acknowledgement retry against the committed setup."""
        return store._duplicate(connection, run_id, "desktop-setup", command_id, payload)

    def operate(self, capture: CapturedSession, message: dict[str, Any]) -> dict[str, Any]:
        """Apply a validated local UI intent off the event-loop thread."""
        refusal = policy_refusal(str(message.get("action", "view")))
        if refusal:
            raise Conflict(refusal)
        self._validate_execution_mode(capture, message)
        if message.get("action") == "request_plan":
            return self._request_plan(capture, message)
        if message.get("action") in managed_collaboration_desktop.ACTIONS:
            return managed_collaboration_desktop.operate(self, capture, message)
        if message.get("action") in {"history", "read_artifact"}:
            return self._inspect_retained(capture, message)
        if message.get("action") in _MANAGED_RECOVERY_ACTIONS:
            return self._managed_recovery_operation(capture, message)
        prepared_integration = self._prepare_recovery_integration(capture, message)
        with self._lock:
            if set(message) - _FIELDS:
                raise ValueError("Unsupported team command fields")
            require_id(message.get("request_id"))
            action = message.get("action", "view")
            if self._closed and action not in {"view", "events", "collaboration_inspect"}:
                raise Conflict("This desktop runtime has closed; reopen the retained team")
            store = self._store(capture)
            run_id = message.get("run_id")
            pair = self._runners.get(run_id)
            if pair is not None and pair[0].scope != capture.scope:
                raise ScopeDenied("Team scope differs from its captured owner")
            recovered = self._recoveries.get(run_id)
            if recovered is not None and recovered[0].scope != capture.scope:
                raise ScopeDenied("Recovery scope differs from its captured owner")
            if action in collaboration_desktop.ACTIONS:
                run_id = collaboration_desktop.operate(self, capture, store, message)
            elif action == "configure":
                if type(message.get("enabled")) is not bool:
                    raise ValueError("Swarming enabled must be true or false")
                self.settings.set("swarming", None, {"version": 1, "enabled": message["enabled"]})
            elif action == "start":
                run_id = self._start(capture, store, message)
            elif action in {"pause", "resume", "stop"}:
                if action == "stop" and pair is None and recovered is not None:
                    recovered[1].command("stop", {}, command_id=message["request_id"],
                                         expected_revision=message.get("expected_revision"))
                    return self._view(capture, store, run_id)
                if not run_id or run_id not in self._runners:
                    raise Conflict("This team needs explicit host recovery before controls can resume")
                self.captured_run(run_id, capture.workspace, capture.scope.session_id)
                if type(message.get("expected_revision")) is not int or message["expected_revision"] < 0:
                    raise ValueError("Refresh the team before changing its state")
                runner = self._runners[run_id][1]
                getattr(runner, action)(command_id=message["request_id"], expected_revision=message["expected_revision"])
                if action == "stop" and run_id in self._schedulers:
                    self._schedulers[run_id].close()
                if action == "stop" and run_id in self._autopilots:
                    self._autopilots[run_id].close()
            elif action in {"pause_worker", "resume_worker", "cancel_worker", "steer_worker"}:
                if pair is None:
                    raise Conflict("Individual controls require this team's current execution host")
                if type(message.get("expected_revision")) is not int or message["expected_revision"] < 0:
                    raise ValueError("Refresh the team before changing a worker")
                control = getattr(pair[1], action)
                kwargs = {"command_id": message["request_id"], "expected_revision": message["expected_revision"]}
                if action == "steer_worker":
                    kwargs["text"] = message.get("text")
                control(message.get("attempt_id"), message.get("attempt_epoch"), **kwargs)
            elif action in {"review_read_result", "complete", "decide_proposal", "accept_writer", "reject_result", "retry_work", "set_concurrency"}:
                recovery_review = action in {"accept_writer", "reject_result"} and recovered is not None
                if not run_id or (pair is None and not recovery_review):
                    raise Conflict("This team needs explicit host recovery before review can continue")
                if type(message.get("expected_revision")) is not int or message["expected_revision"] < 0:
                    raise ValueError("Refresh the team before changing its state")
                payload = {field: message.get(field) for field in (
                    "attempt_id", "attempt_epoch", "candidate_revision", "evidence")} if action == "review_read_result" else {}
                if action == "decide_proposal":
                    payload = {field: message.get(field) for field in ("proposal_id", "sha256", "accept", "evidence")}
                elif action == "accept_writer":
                    payload = {field: message.get(field) for field in ("attempt_id", "attempt_epoch", "candidate_id", "evidence")}
                elif action == "reject_result":
                    payload = {field: message.get(field) for field in ("attempt_id", "attempt_epoch", "evidence")}
                elif action == "retry_work":
                    payload = {field: message.get(field) for field in ("work_item_id", "evidence")}
                elif action == "set_concurrency":
                    payload = {"max_workers": message.get("max_workers")}
                runner = pair[1] if pair else recovered[1]
                kind = {"reject_result": "reject", "retry_work": "retry"}.get(action, action)
                self._command(runner.supervisor, runner.authority, kind, payload,
                              key=message["request_id"], revision=message["expected_revision"])
                if action == "complete" and run_id in self._schedulers:
                    self._schedulers[run_id].close()
                if action == "decide_proposal" and message.get("accept") is True:
                    scheduler = self._schedulers.get(run_id)
                    if scheduler is None:
                        raise Conflict("The admitted plan requires an explicit captured dispatch owner")
                    scheduler.start()
                elif action == "retry_work":
                    if run_id not in self._schedulers:
                        # Manual readers initially launch as a captured batch.
                        # Explicit repair reuses the last assignment's disclosed
                        # allowance and the remaining total, without adding spend.
                        with store._connection() as connection:
                            previous = connection.execute("SELECT a.work_item_id,r.amount FROM reservations r JOIN attempts a ON a.id=r.attempt_id "
                                "WHERE a.run_id=? AND a.kind='worker' ORDER BY a.rowid", (run_id,)).fetchall()
                        allowances = {}
                        for item in previous:
                            allowances.setdefault(item["work_item_id"], item["amount"])
                        if payload["work_item_id"] not in allowances:
                            raise Conflict("Repair requires a retained original request allowance")
                        self._schedulers[run_id] = SwarmScheduler(runner, self._worker_specs.get(run_id, pair[0].backend_spec),
                            requests_per_worker=allowances[payload["work_item_id"]], requests_by_work=allowances)
                    self._schedulers[run_id].start()
            elif action in {"prepare_candidate", "run_check", "apply_candidate", "reconcile_application", "reconcile_operation", "reconcile_effect"}:
                self._integration_operation(capture, store, run_id, pair, recovered, action, message, prepared_integration)
            elif action == "inspect_candidate":
                self._inspect_candidate(capture, store, run_id, message.get("candidate_id"))
            elif action == "export_report":
                from .report import export_report
                if not run_id:
                    raise ValueError("Select a retained team to export")
                return {**self._view(capture, store, run_id), "report": export_report(store, capture.scope, run_id)}
            elif action == "recover":
                if not run_id:
                    raise ValueError("Select a saved team to inspect recovery")
                snapshot = store.snapshot(capture.scope, run_id)
                pair = self._runners.get(run_id)
                if pair:
                    raise Conflict("The original host still owns this team; inspect or stop its workers first")
                recovery_status = recovered[1].inspect()["recovery"] if recovered else None
                if recovery_status is not None:
                    if recovery_status["owns_lease"]:
                        self._fence_managed_recovery(capture, recovered[1])
                        return self._view(capture, store, run_id)
                    if recovery_status["acquired_epoch"] is not None:
                        # This is a new explicit takeover after a prior owner's
                        # lease was lost, not a retry of an ambiguous first call.
                        recovered[1].close()
                        recovered = None
                if message.get("expected_revision") != snapshot["run"]["revision"]:
                    raise RevisionConflict("Refresh the retained team before taking ownership")
                if recovered is None:
                    recovery = SwarmRecovery(SwarmSupervisor(store), capture.scope, run_id)
                    # Retain its takeover identity even if the storage response
                    # is lost. A socket retry must not manufacture another epoch.
                    self._recoveries[run_id] = (capture, recovery)
                else:
                    recovery = recovered[1]
                expected_epoch = recovery.inspect()["recovery"]["requested_epoch"]
                recovery.acquire(expected_epoch=expected_epoch or snapshot["run"]["epoch"], lease_seconds=60)
                self._fence_managed_recovery(capture, recovery)
            elif action in {"inspect_process", "reconcile_process", "reconcile_request", "reconcile_action", "continue_recovered"}:
                if recovered is None:
                    raise Conflict("Take ownership of the expired team before reconciling it")
                recovery = recovered[1]
                if action in {"inspect_process", "reconcile_process"}:
                    if action == "reconcile_process" and message.get("expected_revision") != store.snapshot(capture.scope, run_id)["run"]["revision"]:
                        raise RevisionConflict("Refresh the retained team before recording host observations")
                    observation = getattr(recovery, action)(message.get("attempt_id"))
                    self._recovery_observations.setdefault(run_id, {})[observation["attempt_id"]] = observation
                elif action in {"reconcile_request", "reconcile_action"}:
                    payload = {"outcome": message.get("outcome"), "evidence": message.get("evidence")}
                    if action == "reconcile_request":
                        payload.update(request_id=message.get("model_request_id"), used=message.get("used"))
                    else:
                        payload["action_id"] = message.get("action_id")
                    recovery.command(action, payload, command_id=message["request_id"],
                                     expected_revision=message.get("expected_revision"))
                else:
                    self._continue_recovered(capture, store, recovery, message, prepared_integration)
            elif action not in {"view", "events"}:
                raise ValueError("Unsupported team action")
            result = self._view(capture, store, run_id, after=message.get("after", 0))
            if run_id and (message.get("grant_id") or message.get("before_grant_id")):
                result["collaboration"] = collaboration_desktop.view(store, capture.scope, run_id,
                    grant_id=message.get("grant_id"), before_grant_id=message.get("before_grant_id"),
                    before_message_id=message.get("before_message_id"))
            return result

    def _fence_managed_recovery(self, capture, recovery):
        """Local-only takeover preparation; never renew a retained remote lease."""
        if self._execution_mode(capture) != "managed":
            return
        from .managed_recovery import ManagedRecovery
        existing = self._managed_recoveries.get(recovery.run_id)
        if existing is None or existing.authority != recovery.authority:
            existing = ManagedRecovery(self._managed_desktop, recovery, capture.workspace, self._state_root(capture.workspace))
            self._managed_recoveries[recovery.run_id] = existing
            self._managed_recovery_pages.pop(recovery.run_id, None)
            self._managed_recovery_operations.pop(recovery.run_id, None)
        existing.fence()

    def _managed_recovery_operation(self, capture, message):
        """Queue proof-derived observations; HTTP never holds the Stop lock."""
        with self._lock:
            if set(message) - _FIELDS or any(key in message for key in ("outcome", "used", "evidence")):
                raise ValueError("Managed recovery accepts identities only; outcomes come from native proof")
            require_id(message.get("request_id"))
            if self._closed:
                raise Conflict("This desktop runtime has closed")
            run_id = message.get("run_id")
            recovery = self._managed_recoveries.get(run_id)
            pair = self._recoveries.get(run_id)
            if recovery is None or pair is None or pair[0].scope != capture.scope:
                raise ScopeDenied("Acquire this managed team's recovery before inspecting its retained permits")
            store = self._store(capture)
            store.snapshot(capture.scope, run_id)
            action = message["action"]
            if action == "managed_recovery_inspect":
                page = {"epoch": message.get("managed_epoch"), "kind": message.get("kind", "requests"),
                        "after": message.get("after"), "limit": message.get("limit", 25)}
                recovery.inspect(**page)  # Validate before retaining a selection.
                self._managed_recovery_pages[run_id] = page
                return self._view(capture, store, run_id)
            if message.get("expected_revision") != store.snapshot(capture.scope, run_id)["run"]["revision"]:
                raise RevisionConflict("Refresh the team before reconciling managed observations")
            previous = self._managed_recovery_operations.get(run_id)
            semantics = {key: message.get(key) for key in ("action", "managed_epoch", "model_request_id", "action_id", "attempt_id", "process_id", "kind", "local_id")}
            if previous and previous["request_id"] == message["request_id"]:
                if previous["selection"] != semantics:
                    raise IdempotencyConflict("Managed recovery request identity changed")
                return self._view(capture, store, run_id)
            if previous and previous["state"] == "running":
                raise Conflict("A managed observation is already being reconciled")
            methods = {"managed_reconcile_request": ("reconcile_request", "model_request_id"),
                       "managed_reconcile_action": ("reconcile_action", "action_id"),
                       "managed_reconcile_worker": ("reconcile_worker", "attempt_id"),
                       "managed_reconcile_effect": ("reconcile_effect", "process_id")}
            if action == "managed_fence_absent":
                recovery._journal(message.get("managed_epoch"))
                require_id(message.get("local_id"))
                if message.get("kind") not in {"requests", "workers", "actions", "effects"}:
                    raise ValueError("Select an admission record type")
                method, args = "fence_absent", (message["managed_epoch"], message["kind"], message["local_id"])
            elif action != "managed_retry_observations":
                method, field = methods[action]
                recovery._journal(message.get("managed_epoch"))
                require_id(message.get(field))
                args = (message["managed_epoch"], message[field])
            else:
                method, args = "flush", ()
            operation = {"request_id": message["request_id"], "state": "running", "selection": semantics}
            self._managed_recovery_operations[run_id] = operation

            def reconcile():
                try:
                    result = getattr(recovery, method)(*args)
                    update = {"state": "finished", "delivery": result.get("delivery", result)}
                except Exception:
                    # Neither provider text nor transport/certificate errors
                    # become a public diagnostic or proof supplied by a browser.
                    update = {"state": "failed", "error": "Native proof or remote delivery is unavailable. Resolve local records and retry observations."}
                with self._lock:
                    operation.update(update)

            threading.Thread(target=reconcile, daemon=True, name="swarm-managed-recovery").start()
            return self._view(capture, store, run_id)

    def _inspect_retained(self, capture, message):
        """Read owner evidence without holding up Stop during blob verification."""
        from .inspection import SwarmInspection
        with self._lock:
            if set(message) - _FIELDS:
                raise ValueError("Unsupported team command fields")
            require_id(message.get("request_id"))
            store = self._store(capture)
            run_id = message.get("run_id")
            if run_id:
                store.snapshot(capture.scope, run_id)
        inspection = SwarmInspection(store)
        if message["action"] == "history":
            key, result = "history", inspection.history(capture.scope,
                before_run_id=message.get("before_run_id"), limit=message.get("limit", 20))
        else:
            key, result = "artifact_page", inspection.read_artifact(capture.scope, run_id,
                message.get("artifact_id"), offset=message.get("offset", 0), limit=message.get("limit", 8000))
        with self._lock:
            return {**self._view(capture, store, run_id, after=message.get("after", 0)), key: result}

    def _prepare_recovery_integration(self, capture, message):
        """Do slow Git ownership discovery outside the foreground control lock."""
        if message.get("action") not in {"continue_recovered", "reconcile_application", "reconcile_operation", "reconcile_effect"}:
            return None
        with self._lock:
            if set(message) - _FIELDS or self._closed:
                return None  # The ordinary dispatcher reports invalid intent.
            run_id = message.get("run_id")
            recovered = self._recoveries.get(run_id)
            if recovered is None or recovered[0].scope != capture.scope or run_id in self._runners:
                return None
            store = self._store(capture)
            setup, _ = self._setup(store, run_id, required=True)
            if not setup.get("write_roots"):
                return None
            authority = recovered[1].authority  # Requires an acquired owner before Git discovery.
            old = self._workflows.get(run_id)
            if old and old.authority == authority and not (
                    message.get("action") == "continue_recovered" and self._execution_mode(capture) == "managed"):
                return old.integration
            effects = None
            if message.get("action") == "continue_recovered" and self._execution_mode(capture) == "managed":
                retained = self._managed_recoveries.get(run_id)
                if retained is None:
                    raise Conflict("Fence retained managed permits before continuing this team")
                effects = retained.prepare_resume().effects(store)
        return SwarmIntegration(store, capture.workspace,
            root=Path(self._state_root(capture.workspace)) / "swarm" / "worktrees", managed_effects=effects)

    def _integration_operation(self, capture, store, run_id, pair, recovered, action, message, prepared_integration=None):
        from .workflow import IntegrationWorkflow
        if pair is None and (action not in {"reconcile_application", "reconcile_operation", "reconcile_effect"} or recovered is None):
            raise Conflict("Continue this recovered team before creating new integration effects")
        setup, result = self._setup(store, run_id, required=True)
        if not setup.get("write_roots"):
            raise ScopeDenied("This team has no captured writer verification contract")
        host = pair[1] if pair else recovered[1]
        previous = self._workflows.get(run_id)
        if previous and previous.authority != host.authority:
            previous.close(timeout=0)
            del self._workflows[run_id]
        if run_id not in self._workflows:
            integration = pair[1].integration if pair else prepared_integration
            if integration is None:
                raise Conflict("Captured writer integration is unavailable; refresh this team's recovery")
            self._workflows[run_id] = IntegrationWorkflow(host.supervisor, host.authority, integration)
        workflow = self._workflows[run_id]
        kind = "apply" if action == "apply_candidate" else action
        if action == "prepare_candidate":
            identities = message.get("writer_ids")
            if (type(identities) is not list or not identities or len(identities) > 256
                    or any(type(item) is not str for item in identities) or len(set(identities)) != len(identities)):
                raise ValueError("Select unique finalized writer results for this candidate")
            snapshot = store.snapshot(capture.scope, run_id)
            writers = {row["id"]: row for row in snapshot["writer_worktrees"]}
            attempts = {row["id"]: row for row in snapshot["attempts"]}
            work = {row["id"]: json.loads(row["specification"]) for row in snapshot["work_items"]}
            # Writers start from the captured base, or from a revision this
            # team applied (scheduler.writer_base); nothing else is theirs.
            bases = {result["writer_base"]["base_revision"]} | {
                row["observed_revision"] for row in snapshot["integration_applications"] if row["state"] == "applied"}
            mapping = {}
            for identity in identities:
                writer = writers.get(identity)
                if writer is None:
                    raise ScopeDenied("Writer result is unavailable in this team")
                if (writer["base_revision"] not in bases
                        or json.loads(writer["manifest_json"])["target_branch"] != result["writer_base"]["target_branch"]):
                    raise Conflict("Writer input differs from this team's captured Git base or branch")
                work_id = attempts[writer["attempt_id"]]["work_item_id"]
                mapping[work_id] = {criterion: criterion for criterion in work[work_id]["criteria"]}
            payload = {"writer_ids": identities, "checks": setup["checks"], "criterion_checks": mapping}
        elif action == "run_check":
            payload = {"candidate_id": message.get("candidate_id"), "check_key": message.get("check_key")}
        elif action == "apply_candidate":
            payload = {field: message.get(field) for field in ("candidate_id", "expected_base", "target_revision", "evidence")}
            payload["approval_seconds"] = 60
        elif action == "reconcile_application":
            payload = {"approval_id": message.get("approval_id")}
        elif action == "reconcile_effect":
            payload = {field: message.get(field) for field in ("effect_kind", "effect_id", "evidence")}
        else:
            payload = {"operation_id": message.get("operation_id"), "evidence": message.get("evidence")}
        workflow.submit(kind, payload, command_id=message["request_id"], expected_revision=message.get("expected_revision"))

    def _inspect_candidate(self, capture, store, run_id, candidate_id):
        """Inspect large Git diffs asynchronously so Stop never waits on rendering."""
        require_id(candidate_id)
        snapshot = store.snapshot(capture.scope, run_id)
        if not any(row["id"] == candidate_id for row in snapshot["integration_candidates"]):
            raise ScopeDenied("Candidate is unavailable in this team")
        key = (run_id, candidate_id)
        if self._closed or self._candidate_details.get(key, {}).get("state") == "loading":
            return
        self._candidate_details[key] = {"candidate_id": candidate_id, "state": "loading"}
        def inspect():
            try:
                integration = SwarmIntegration(store, capture.workspace,
                    root=Path(self._state_root(capture.workspace)) / "swarm" / "worktrees")
                details = {**integration.inspect_candidate(capture.scope, run_id, candidate_id), "state": "ready"}
            except Exception:
                details = {"candidate_id": candidate_id, "state": "failed",
                           "error": "Candidate inspection failed; its immutable input may have changed"}
            with self._lock:
                self._candidate_details[key] = details
        threading.Thread(target=inspect, daemon=True, name="swarm-candidate-inspection").start()

    def _continue_recovered(self, capture, store, recovery, message, prepared_integration=None):
        """Join an explicitly reconciled run to a fresh, captured local runner."""
        if any(identity != recovery.run_id for identity in self._active):
            raise Conflict("Other retained teams must be stopped or reconciled before new execution")
        setup, result = self._setup(store, recovery.run_id, required=True)
        managed = self._execution_mode(capture) == "managed"
        if setup["model"] != {"provider": capture.backend_spec.backend_type, "model": capture.backend_spec.model}:
            raise Conflict("Restore this team's saved provider and model before continuing")
        allowance = message.get("worker_requests", setup.get("worker_requests", 4))
        if type(allowance) is not int or not 1 <= allowance <= 1000:
            raise ValueError("Choose a positive per-worker request allowance up to 1000")
        if recovery.run_id in self._runners:
            scheduler = self._schedulers.get(recovery.run_id)
            if scheduler is None or scheduler.requests_per_worker != allowance:
                raise IdempotencyConflict("Recovery continuation already captured another dispatch configuration")
            recovery.command("recover", {"retry_work_items": message.get("retry_work_items", [])},
                command_id=message["request_id"], expected_revision=message.get("expected_revision"))
            return  # A lost acknowledgement never replaces the live owner.
        attachment = None
        if managed:
            retained = self._managed_recoveries.get(recovery.run_id)
            if retained is None:
                raise Conflict("Fence retained managed permits before continuing this team")
            attachment = retained.prepare_resume()
            effects = attachment.effects(store)
            if prepared_integration is not None and prepared_integration.managed_effects is not effects:
                # A previous reconciliation workflow has no fresh dispatch
                # authority. Do not reuse that observer for new Git effects.
                raise Conflict("Refresh the captured managed writer integration before continuing")
        private_spec = copy.deepcopy(capture.backend_spec)
        private_spec.api_key = private_spec.resolve_api_key(self.settings)
        capture = replace(capture, backend_spec=private_spec)
        workers_spec = self._saved_worker_spec(capture, setup)
        authority = recovery.authority
        record_run_host(store, authority)
        integration = prepared_integration if setup.get("write_roots") else None
        if setup.get("write_roots") and integration is None:
            raise Conflict("Captured writer integration is unavailable; refresh this team's recovery")
        writer_base = result.get("writer_base", {})
        if integration and not writer_base.get("base_revision"):
            raise Conflict("Writer recovery requires the original captured Git base")
        runner = SwarmWorkerRunner(recovery.supervisor, authority, capture.workspace,
                                   backend_factory=self._factory, project_instructions=capture.instructions,
                                   exclusions=self.exclusions_for(capture.workspace),
                                   connections={**self.team_model(capture.backend_spec), **self.team_model(workers_spec)},
                                   managed_readers=self._managed_readers, integration=integration,
                                   managed_runtime=attachment.runtime if attachment else None,
                                   **({"writer_process_factory": None} if not self._managed_readers else {}))
        scheduler = SwarmScheduler(runner, workers_spec, requests_per_worker=allowance,
                                   base_revision=writer_base.get("base_revision"))
        try:
            receipt = recovery.command("recover", {"retry_work_items": message.get("retry_work_items", [])},
                command_id=message["request_id"], expected_revision=message.get("expected_revision"))
        except BaseException:
            # No participant has been started by constructing either object.
            scheduler.close()
            raise
        if receipt.state not in _TERMINAL:
            self._runners[recovery.run_id] = (capture, runner)
            self._schedulers[recovery.run_id] = scheduler
            self._worker_specs[recovery.run_id] = workers_spec
            if attachment:
                self._managed_attachments[recovery.run_id] = attachment
                attachment.start_pump(runner, lambda: store.snapshot(capture.scope, recovery.run_id))
            scheduler.start()
            # The owner continued a team they let the orchestrator run: its loop
            # goes on from the retained plans (autopilot.TeamAutopilot.resumed).
            grant = setup.get("autonomy")
            previous = self._autopilots.get(recovery.run_id)
            if grant and (previous is None or not previous.inspect()["active"]):
                autopilot = self._autopilots[recovery.run_id] = TeamAutopilot.resumed(
                    self, recovery.run_id, grant, store.snapshot(capture.scope, recovery.run_id))
                autopilot.start()

    def close(self) -> None:
        """Stop only the workers owned by this desktop runtime."""
        with self._lock:
            self._closed = True
        if self._managed_desktop is not None:
            self._managed_desktop.close()
        self._observer_stop.set()
        if self._observer is not None:
            self._observer.join(timeout=1)
        for autopilot in tuple(self._autopilots.values()):
            autopilot.close()
        for scheduler in tuple(self._schedulers.values()):
            scheduler.close()
        for workflow in tuple(self._workflows.values()):
            workflow.close(timeout=1)
        for _, recovery in tuple(self._recoveries.values()):
            recovery.close()
        for _, runner in tuple(self._runners.values()):
            try:
                runner.close(timeout=2)
            except (SwarmError, OSError):
                # Durable uncertainty survives shutdown; never invent a stop.
                continue
