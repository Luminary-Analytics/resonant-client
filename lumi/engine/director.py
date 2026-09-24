"""Durable frontier-director orchestration for heterogeneous worker models.

Director Mode is deliberately an opt-in layer around :class:`Session`.  The
ordinary single-agent loop remains the default.  When enabled, a frontier
model owns a persistent task graph, delegates bounded work to configured
worker models, and may accept results only after deterministic evidence gates
have passed.
"""

from __future__ import annotations

import json
import hashlib
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from .artifacts import project_state_dir


class DirectorPhase(str, Enum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    DISPATCH = "dispatch"
    COLLECT = "collect"
    VALIDATE = "validate"
    REVIEW = "review"
    REVISE = "revise"
    INTEGRATE = "integrate"
    REPORT = "report"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DirectorTaskStatus(str, Enum):
    PLANNED = "planned"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    REVIEW = "review"
    REVISION = "revision"
    ACCEPTED = "accepted"
    INTEGRATED = "integrated"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            DirectorTaskStatus.INTEGRATED,
            DirectorTaskStatus.BLOCKED,
            DirectorTaskStatus.FAILED,
            DirectorTaskStatus.CANCELLED,
        }


class DirectorDecisionAction(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    REASSIGN = "reassign"
    BLOCK = "block"
    ESCALATE = "escalate"
    INTEGRATE = "integrate"


@dataclass(slots=True)
class WorkerSpec:
    """One selectable worker-model profile."""

    id: str
    backend_type: str
    model: str
    roles: list[str] = field(default_factory=list)
    thinking_mode: str = ""
    enabled: bool = True
    max_parallel: int | None = None
    capabilities: list[str] = field(default_factory=list)
    quality_weight: float = 1.0
    priority: int = 0
    system_suffix: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "WorkerSpec":
        backend_type = str(data.get("backend_type") or data.get("backend") or "").strip()
        model = str(data.get("model") or "").strip()
        worker_id = str(data.get("id") or f"worker-{index + 1}").strip()
        roles = [str(value).strip() for value in (data.get("roles") or []) if str(value).strip()]
        capabilities = [
            str(value).strip()
            for value in (data.get("capabilities") or [])
            if str(value).strip()
        ]
        raw_parallel = data.get("max_parallel")
        try:
            max_parallel = int(raw_parallel) if raw_parallel not in (None, "") else None
        except (TypeError, ValueError):
            max_parallel = None
        if max_parallel is not None and max_parallel <= 0:
            max_parallel = None
        try:
            quality_weight = max(0.0, float(data.get("quality_weight") or 1.0))
        except (TypeError, ValueError):
            quality_weight = 1.0
        try:
            priority = int(data.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return cls(
            id=worker_id,
            backend_type=backend_type,
            model=model,
            roles=roles,
            thinking_mode=str(data.get("thinking_mode") or ""),
            enabled=data.get("enabled") is not False,
            max_parallel=max_parallel,
            capabilities=capabilities,
            quality_weight=quality_weight,
            priority=priority,
            system_suffix=str(data.get("system_suffix") or ""),
        )

    def supports(self, role: str, capabilities: Iterable[str] = ()) -> bool:
        if not self.enabled or not self.model:
            return False
        normalized_role = str(role or "").strip()
        if self.roles and normalized_role not in self.roles:
            return False
        available = set(self.capabilities)
        return all(str(value) in available for value in capabilities)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DirectorConfig:
    """Session-local Director Mode configuration.

    Token, context, time, and cost budgets are intentionally absent from the
    default contract.  Resonant's quality-first policy treats optional budget
    guardrails as a user choice, not an implicit execution cap.
    """

    enabled: bool = False
    director_backend_type: str = ""
    director_model: str = ""
    director_thinking_mode: str = "max"
    workers: list[WorkerSpec] = field(default_factory=list)
    max_parallel_workers: int = 4
    require_independent_review: bool = True
    require_validation: bool = True
    auto_integrate: bool = False
    adaptive_scheduling: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DirectorConfig":
        raw = data if isinstance(data, dict) else {}
        director = raw.get("director") if isinstance(raw.get("director"), dict) else {}
        workers_raw = raw.get("workers") if isinstance(raw.get("workers"), list) else []
        workers = [
            WorkerSpec.from_dict(item, index=index)
            for index, item in enumerate(workers_raw)
            if isinstance(item, dict)
        ]
        try:
            max_parallel = int(raw.get("max_parallel_workers") or 4)
        except (TypeError, ValueError):
            max_parallel = 4
        return cls(
            enabled=raw.get("enabled") is True,
            director_backend_type=str(
                director.get("backend_type")
                or director.get("backend")
                or raw.get("director_backend_type")
                or ""
            ).strip(),
            director_model=str(
                director.get("model") or raw.get("director_model") or ""
            ).strip(),
            director_thinking_mode=str(
                director.get("thinking_mode")
                or raw.get("director_thinking_mode")
                or "max"
            ).strip(),
            workers=workers,
            max_parallel_workers=max(1, min(16, max_parallel)),
            require_independent_review=raw.get("require_independent_review") is not False,
            require_validation=raw.get("require_validation") is not False,
            auto_integrate=raw.get("auto_integrate") is True,
            adaptive_scheduling=raw.get("adaptive_scheduling") is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "director": {
                "backend_type": self.director_backend_type,
                "model": self.director_model,
                "thinking_mode": self.director_thinking_mode,
            },
            "workers": [worker.to_dict() for worker in self.workers],
            "max_parallel_workers": self.max_parallel_workers,
            "require_independent_review": self.require_independent_review,
            "require_validation": self.require_validation,
            "auto_integrate": self.auto_integrate,
            "adaptive_scheduling": self.adaptive_scheduling,
        }


@dataclass(slots=True)
class ValidationEvidence:
    name: str
    passed: bool
    evidence: str = ""
    source: str = "runtime"
    attempt: int = 0
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationEvidence":
        return cls(
            name=str(data.get("name") or "validation"),
            passed=bool(data.get("passed")),
            evidence=str(data.get("evidence") or ""),
            source=str(data.get("source") or "runtime"),
            attempt=max(0, int(data.get("attempt") or 0)),
            created_at=float(data.get("created_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DirectorTask:
    id: str
    title: str
    objective: str
    role: str = "implement"
    agent_type: str = "build"
    dependencies: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    preferred_worker_id: str = ""
    assigned_worker_id: str = ""
    status: str = DirectorTaskStatus.PLANNED.value
    attempts: int = 0
    attempt_started_at: float = 0.0
    performance_recorded_attempts: list[int] = field(default_factory=list)
    revision_of: str = ""
    agent_ids: list[str] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    validations: list[ValidationEvidence] = field(default_factory=list)
    worktree: dict[str, Any] | None = None
    review_notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "DirectorTask":
        task_id = str(data.get("id") or f"task-{index + 1}").strip()
        validations = [
            value if isinstance(value, ValidationEvidence) else ValidationEvidence.from_dict(value)
            for value in (data.get("validations") or [])
            if isinstance(value, (dict, ValidationEvidence))
        ]
        return cls(
            id=task_id,
            title=str(data.get("title") or data.get("objective") or task_id).strip(),
            objective=str(data.get("objective") or data.get("prompt") or "").strip(),
            role=str(data.get("role") or "implement"),
            agent_type=str(data.get("agent_type") or "build"),
            dependencies=[str(value) for value in (data.get("dependencies") or [])],
            write_scope=[str(value) for value in (data.get("write_scope") or [])],
            acceptance_checks=[str(value) for value in (data.get("acceptance_checks") or [])],
            required_capabilities=[str(value) for value in (data.get("required_capabilities") or [])],
            artifact_ids=[str(value) for value in (data.get("artifact_ids") or [])],
            preferred_worker_id=str(data.get("preferred_worker_id") or data.get("worker_id") or ""),
            assigned_worker_id=str(data.get("assigned_worker_id") or ""),
            status=str(data.get("status") or DirectorTaskStatus.PLANNED.value),
            attempts=int(data.get("attempts") or 0),
            attempt_started_at=float(data.get("attempt_started_at") or 0.0),
            performance_recorded_attempts=[
                int(value) for value in (data.get("performance_recorded_attempts") or [])
            ],
            revision_of=str(data.get("revision_of") or ""),
            agent_ids=[str(value) for value in (data.get("agent_ids") or [])],
            handoffs=[dict(value) for value in (data.get("handoffs") or []) if isinstance(value, dict)],
            validations=validations,
            worktree=dict(data["worktree"]) if isinstance(data.get("worktree"), dict) else None,
            review_notes=[str(value) for value in (data.get("review_notes") or [])],
            blockers=[str(value) for value in (data.get("blockers") or [])],
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["validations"] = [value.to_dict() for value in self.validations]
        return data


@dataclass(slots=True)
class DirectorDecision:
    task_id: str
    action: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    requested_changes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DirectorDecision":
        return cls(
            task_id=str(data.get("task_id") or ""),
            action=str(data.get("action") or ""),
            reason=str(data.get("reason") or ""),
            evidence=[str(value) for value in (data.get("evidence") or [])],
            requested_changes=[str(value) for value in (data.get("requested_changes") or [])],
            created_at=float(data.get("created_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkerPerformanceStore:
    """Historical, evidence-based worker routing metrics."""

    def __init__(self, project_path: str | Path, root: str | Path | None = None):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "director"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "worker-performance.json"
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def record(
        self,
        worker_id: str,
        role: str,
        *,
        accepted: bool,
        elapsed_seconds: float = 0.0,
        validations_passed: int = 0,
        revisions: int = 0,
    ) -> None:
        key = f"{worker_id}:{role}"
        with self._lock:
            row = self._data.setdefault(key, {
                "attempts": 0,
                "accepted": 0,
                "elapsed_seconds": 0.0,
                "validations_passed": 0,
                "revisions": 0,
            })
            row["attempts"] += 1
            row["accepted"] += int(accepted)
            row["elapsed_seconds"] += max(0.0, float(elapsed_seconds))
            row["validations_passed"] += max(0, int(validations_passed))
            row["revisions"] += max(0, int(revisions))
            self.path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
            )

    def metrics(self, worker_id: str, role: str) -> dict[str, Any]:
        return dict(self._data.get(f"{worker_id}:{role}") or {})

    def score(self, worker: WorkerSpec, role: str) -> float:
        row = self.metrics(worker.id, role)
        attempts = max(0, int(row.get("attempts") or 0))
        accepted = max(0, int(row.get("accepted") or 0))
        elapsed = max(0.0, float(row.get("elapsed_seconds") or 0.0))
        revisions = max(0, int(row.get("revisions") or 0))
        # Bayesian prior prevents a worker with one lucky run from dominating.
        acceptance_rate = (accepted + 2.0) / (attempts + 3.0)
        average_elapsed = elapsed / attempts if attempts else 0.0
        revision_rate = revisions / attempts if attempts else 0.0
        latency_factor = 1.0 / (1.0 + average_elapsed / 1800.0)
        return (
            worker.quality_weight * 100.0 * acceptance_rate
            + worker.priority * 5.0
            + latency_factor * 10.0
            - revision_rate * 15.0
        )


class WorkerScheduler:
    """Select an eligible worker deterministically from configured pools."""

    def __init__(self, performance: WorkerPerformanceStore | None = None):
        self.performance = performance

    def select(
        self,
        workers: Iterable[WorkerSpec],
        *,
        role: str,
        capabilities: Iterable[str] = (),
        preferred_worker_id: str = "",
        active_counts: dict[str, int] | None = None,
    ) -> WorkerSpec | None:
        counts = active_counts or {}
        eligible = []
        for worker in workers:
            if not worker.supports(role, capabilities):
                continue
            if worker.max_parallel is not None and counts.get(worker.id, 0) >= worker.max_parallel:
                continue
            eligible.append(worker)
        if not eligible:
            return None
        if preferred_worker_id:
            preferred = next((item for item in eligible if item.id == preferred_worker_id), None)
            if preferred is not None:
                return preferred
        return max(
            eligible,
            key=lambda item: (
                self.performance.score(item, role) if self.performance else item.quality_weight * 100,
                item.priority,
                item.id,
            ),
        )


class DirectorBenchmarkStore:
    """Comparable single-agent and Director-mode outcome telemetry.

    The store deliberately centers task outcomes and validation evidence.
    Token usage is retained when a provider reports it, but it is not used as
    an implicit quality or scheduling penalty.
    """

    def __init__(self, project_path: str | Path, root: str | Path | None = None):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "director"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "benchmarks.jsonl"
        self._lock = threading.RLock()

    def record(
        self,
        *,
        mode: str,
        objective: str,
        outcome: str,
        elapsed_seconds: float,
        steps: int,
        tool_calls: int,
        validation_tools: int,
        changed_files: int,
        director_run_id: str = "",
        provider_stats: dict[str, Any] | None = None,
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        normalized = " ".join(str(objective or "").casefold().split())
        record = {
            "id": f"bench_{uuid.uuid4().hex[:12]}",
            "task_key": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            "mode": "director" if mode == "director" else "single",
            "objective": str(objective or ""),
            "outcome": str(outcome or "unknown"),
            "elapsed_seconds": max(0.0, float(elapsed_seconds)),
            "steps": max(0, int(steps)),
            "tool_calls": max(0, int(tool_calls)),
            "validation_tools": max(0, int(validation_tools)),
            "changed_files": max(0, int(changed_files)),
            "director_run_id": str(director_run_id or ""),
            "provider_stats": dict(provider_stats or {}),
            "quality_score": (
                max(0.0, min(1.0, float(quality_score)))
                if quality_score is not None else None
            ),
            "created_at": time.time(),
        }
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def list(self, *, task_key: str = "") -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return [row for row in rows if not task_key or row.get("task_key") == task_key]

    def comparison(self, *, task_key: str = "") -> dict[str, Any]:
        rows = self.list(task_key=task_key)
        result: dict[str, Any] = {"task_key": task_key, "modes": {}, "samples": len(rows)}
        for mode in ("single", "director"):
            selected = [row for row in rows if row.get("mode") == mode]
            if not selected:
                result["modes"][mode] = {"samples": 0}
                continue
            scored = [row["quality_score"] for row in selected if row.get("quality_score") is not None]
            successful = sum(
                row.get("outcome") in {
                    "answered", "changed_verified", "no_changes_needed",
                }
                for row in selected
            )
            result["modes"][mode] = {
                "samples": len(selected),
                "successful": successful,
                "success_rate": successful / len(selected),
                "average_elapsed_seconds": sum(row.get("elapsed_seconds", 0) for row in selected) / len(selected),
                "average_steps": sum(row.get("steps", 0) for row in selected) / len(selected),
                "average_tool_calls": sum(row.get("tool_calls", 0) for row in selected) / len(selected),
                "average_quality_score": sum(scored) / len(scored) if scored else None,
            }
        return result


class DirectorRun:
    """Persistent supervisory state for one Director Mode session."""

    def __init__(
        self,
        project_path: str | Path,
        *,
        config: DirectorConfig | dict[str, Any] | None = None,
        run_id: str = "",
        objective: str = "",
        root: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "director"
        self.root.mkdir(parents=True, exist_ok=True)
        self.id = run_id or f"dir_{uuid.uuid4().hex[:12]}"
        self.path = self.root / f"{self.id}.json"
        self.events_path = self.root / f"{self.id}.jsonl"
        self.config = config if isinstance(config, DirectorConfig) else DirectorConfig.from_dict(config)
        self.objective = objective
        self.phase = DirectorPhase.UNDERSTAND.value
        self.tasks: dict[str, DirectorTask] = {}
        self.decisions: list[DirectorDecision] = []
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.completed_at = 0.0
        self.error = ""
        self._on_event = on_event or (lambda event: None)
        self._lock = threading.RLock()
        self.performance = WorkerPerformanceStore(self.project_path, root=self.root)
        self.scheduler = WorkerScheduler(self.performance)
        self._save()
        self._emit("director.created")

    @classmethod
    def load(
        cls,
        project_path: str | Path,
        run_id: str,
        *,
        root: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> "DirectorRun":
        project = Path(project_path).expanduser().resolve()
        directory = Path(root) if root else project_state_dir(project) / "director"
        data = json.loads((directory / f"{run_id}.json").read_text(encoding="utf-8"))
        run = cls.__new__(cls)
        run.project_path = project
        run.root = directory
        run.id = str(data["id"])
        run.path = directory / f"{run.id}.json"
        run.events_path = directory / f"{run.id}.jsonl"
        run.config = DirectorConfig.from_dict(data.get("config"))
        run.objective = str(data.get("objective") or "")
        run.phase = str(data.get("phase") or DirectorPhase.UNDERSTAND.value)
        run.tasks = {
            str(item["id"]): DirectorTask.from_dict(item, index=index)
            for index, item in enumerate(data.get("tasks") or [])
            if isinstance(item, dict) and item.get("id")
        }
        run.decisions = [
            DirectorDecision.from_dict(item)
            for item in (data.get("decisions") or [])
            if isinstance(item, dict)
        ]
        run.created_at = float(data.get("created_at") or time.time())
        run.updated_at = float(data.get("updated_at") or run.created_at)
        run.completed_at = float(data.get("completed_at") or 0.0)
        run.error = str(data.get("error") or "")
        run._on_event = on_event or (lambda event: None)
        run._lock = threading.RLock()
        run.performance = WorkerPerformanceStore(project, root=directory)
        run.scheduler = WorkerScheduler(run.performance)
        return run

    def set_objective(self, objective: str) -> None:
        with self._lock:
            if not self.objective:
                self.objective = str(objective or "").strip()
                self.updated_at = time.time()
                self._save()

    def create_plan(self, tasks: Iterable[dict[str, Any]]) -> list[DirectorTask]:
        parsed = [DirectorTask.from_dict(item, index=index) for index, item in enumerate(tasks)]
        if not parsed:
            raise ValueError("Director plan requires at least one task")
        ids = [task.id for task in parsed]
        if len(ids) != len(set(ids)):
            raise ValueError("Director task ids must be unique")
        known = set(ids)
        for task in parsed:
            unknown = [value for value in task.dependencies if value not in known]
            if unknown:
                raise ValueError(f"Task {task.id} has unknown dependencies: {', '.join(unknown)}")
            if task.id in task.dependencies:
                raise ValueError(f"Task {task.id} cannot depend on itself")
        self._assert_acyclic(parsed)
        with self._lock:
            protected_statuses = {
                DirectorTaskStatus.QUEUED.value,
                DirectorTaskStatus.RUNNING.value,
                DirectorTaskStatus.VALIDATING.value,
                DirectorTaskStatus.REVIEW.value,
                DirectorTaskStatus.REVISION.value,
                DirectorTaskStatus.ACCEPTED.value,
                DirectorTaskStatus.INTEGRATED.value,
            }
            omitted = [
                task.id for task in self.tasks.values()
                if task.status in protected_statuses and task.id not in known
            ]
            if omitted:
                raise ValueError(
                    "Plan update cannot remove active or accepted tasks: "
                    + ", ".join(omitted)
                )
            for task in parsed:
                prior = self.tasks.get(task.id)
                if prior is None:
                    continue
                if prior.status in protected_statuses:
                    if prior.status != DirectorTaskStatus.REVISION.value:
                        # An in-flight or accepted assignment is an immutable
                        # contract. Steering may add/reorder future work, while
                        # direct agent steering handles the active assignment.
                        task.title = prior.title
                        task.objective = prior.objective
                        task.role = prior.role
                        task.agent_type = prior.agent_type
                        task.dependencies = list(prior.dependencies)
                        task.write_scope = list(prior.write_scope)
                        task.acceptance_checks = list(prior.acceptance_checks)
                        task.required_capabilities = list(prior.required_capabilities)
                        task.artifact_ids = list(prior.artifact_ids)
                        task.preferred_worker_id = prior.preferred_worker_id
                    task.status = prior.status
                    task.attempts = prior.attempts
                    task.attempt_started_at = prior.attempt_started_at
                    task.performance_recorded_attempts = list(
                        prior.performance_recorded_attempts
                    )
                    task.assigned_worker_id = prior.assigned_worker_id
                    task.agent_ids = list(prior.agent_ids)
                    task.handoffs = list(prior.handoffs)
                    task.validations = list(prior.validations)
                    task.worktree = dict(prior.worktree) if prior.worktree else None
                    task.review_notes = list(prior.review_notes)
                    task.blockers = list(prior.blockers)
                    task.created_at = prior.created_at
                elif prior.status == DirectorTaskStatus.READY.value:
                    # Recompute readiness after dependency edits.
                    task.status = DirectorTaskStatus.PLANNED.value
            self.tasks = {task.id: task for task in parsed}
            self.phase = DirectorPhase.PLAN.value
            self._refresh_ready()
            self.updated_at = time.time()
            self._save()
        self._emit("director.plan.updated")
        return list(self.tasks.values())

    def ready_tasks(self) -> list[DirectorTask]:
        with self._lock:
            self._refresh_ready()
            return [
                task for task in self.tasks.values()
                if task.status == DirectorTaskStatus.READY.value
            ]

    def select_worker(self, task_id: str, active_counts: dict[str, int] | None = None) -> WorkerSpec:
        task = self._require_task(task_id)
        worker = self.scheduler.select(
            self.config.workers,
            role=task.role,
            capabilities=task.required_capabilities,
            preferred_worker_id=task.preferred_worker_id,
            active_counts=active_counts,
        )
        if worker is None:
            raise RuntimeError(f"No eligible worker configured for role '{task.role}'")
        return worker

    def mark_dispatched(self, task_id: str, *, worker_id: str, agent_id: str = "") -> DirectorTask:
        task = self._require_task(task_id)
        if task.status not in {
            DirectorTaskStatus.READY.value,
            DirectorTaskStatus.REVISION.value,
            DirectorTaskStatus.QUEUED.value,
        }:
            raise ValueError(f"Task {task.id} cannot dispatch from {task.status}")
        with self._lock:
            task.assigned_worker_id = worker_id
            if agent_id and agent_id not in task.agent_ids:
                task.agent_ids.append(agent_id)
            task.attempts += 1
            task.attempt_started_at = time.time()
            # Blockers belong to an execution attempt. Preserve the complete
            # handoff/decision history, but do not let a resolved blocker from
            # an earlier revision permanently poison the current gate.
            task.blockers.clear()
            task.status = DirectorTaskStatus.RUNNING.value
            task.updated_at = time.time()
            self.phase = DirectorPhase.DISPATCH.value
            self.updated_at = task.updated_at
            self._save()
        self._emit("director.task.updated", task_id=task.id)
        return task

    def attach_agent(self, task_id: str, agent_id: str) -> None:
        task = self._require_task(task_id)
        with self._lock:
            if agent_id and agent_id not in task.agent_ids:
                task.agent_ids.append(agent_id)
                task.updated_at = time.time()
                self._save()

    def record_handoff(
        self,
        task_id: str,
        handoff: dict[str, Any],
        *,
        worktree: dict[str, Any] | None = None,
    ) -> DirectorTask:
        task = self._require_task(task_id)
        with self._lock:
            task.handoffs.append(dict(handoff))
            task.worktree = dict(worktree) if isinstance(worktree, dict) else task.worktree
            blockers = handoff.get("blockers") or []
            if blockers:
                task.blockers.extend(str(value) for value in blockers)
            task.status = DirectorTaskStatus.VALIDATING.value
            task.updated_at = time.time()
            self.phase = DirectorPhase.VALIDATE.value
            self.updated_at = task.updated_at
            self._save()
        self._emit("director.task.handoff", task_id=task.id)
        return task

    def record_validation(
        self,
        task_id: str,
        *,
        name: str,
        passed: bool,
        evidence: str = "",
        source: str = "runtime",
    ) -> DirectorTask:
        task = self._require_task(task_id)
        with self._lock:
            task.validations.append(ValidationEvidence(
                name=name,
                passed=passed,
                evidence=evidence,
                source=source,
                attempt=task.attempts,
            ))
            task.status = DirectorTaskStatus.REVIEW.value
            task.updated_at = time.time()
            self.phase = DirectorPhase.REVIEW.value
            self.updated_at = task.updated_at
            self._save()
        self._emit("director.task.validation", task_id=task.id)
        return task

    def acceptance_gate(self, task_id: str) -> tuple[bool, list[str]]:
        task = self._require_task(task_id)
        reasons: list[str] = []
        current_validations = [
            item for item in task.validations if item.attempt == task.attempts
        ]
        if not task.handoffs:
            reasons.append("No worker handoff is available")
        else:
            latest = task.handoffs[-1]
            if str(latest.get("outcome") or "") != "completed":
                reasons.append(f"Latest worker outcome is {latest.get('outcome') or 'unknown'}")
            if latest.get("blockers"):
                reasons.append("Worker reported unresolved blockers")
        if task.blockers:
            reasons.append("Task has unresolved blockers")
        if self.config.require_validation:
            if not current_validations:
                reasons.append("No deterministic validation evidence is available")
            elif not all(item.passed for item in current_validations):
                reasons.append("One or more validations failed")
        if task.acceptance_checks:
            handoff_validations = (
                [str(value) for value in (task.handoffs[-1].get("validation") or [])]
                if task.handoffs else []
            )
            haystack = "\n".join(
                [item.name + " " + item.evidence for item in current_validations]
                + handoff_validations
            ).lower()
            missing = [value for value in task.acceptance_checks if value.lower() not in haystack]
            if missing:
                reasons.append("Acceptance checks lack evidence: " + ", ".join(missing))
        return not reasons, reasons

    def decide(self, decision: DirectorDecision | dict[str, Any]) -> DirectorTask:
        value = decision if isinstance(decision, DirectorDecision) else DirectorDecision.from_dict(decision)
        task = self._require_task(value.task_id)
        try:
            action = DirectorDecisionAction(value.action)
        except ValueError as exc:
            raise ValueError(f"Unknown director decision: {value.action}") from exc
        with self._lock:
            if action in {DirectorDecisionAction.ACCEPT, DirectorDecisionAction.INTEGRATE} and (
                task.status not in {
                    DirectorTaskStatus.VALIDATING.value,
                    DirectorTaskStatus.REVIEW.value,
                    DirectorTaskStatus.ACCEPTED.value,
                }
            ):
                raise ValueError(
                    f"Task {task.id} cannot apply {action.value} from {task.status}"
                )
            if action in {DirectorDecisionAction.REVISE, DirectorDecisionAction.REASSIGN} and (
                task.status not in {
                    DirectorTaskStatus.VALIDATING.value,
                    DirectorTaskStatus.REVIEW.value,
                }
            ):
                raise ValueError(f"Task {task.id} cannot be revised from {task.status}")
            if action in {DirectorDecisionAction.ACCEPT, DirectorDecisionAction.INTEGRATE}:
                allowed, reasons = self.acceptance_gate(task.id)
                if not allowed:
                    raise ValueError("Acceptance gate rejected the decision: " + "; ".join(reasons))
                task.status = (
                    DirectorTaskStatus.INTEGRATED.value
                    if action == DirectorDecisionAction.INTEGRATE
                    else DirectorTaskStatus.ACCEPTED.value
                )
            elif action in {DirectorDecisionAction.REVISE, DirectorDecisionAction.REASSIGN}:
                task.status = DirectorTaskStatus.REVISION.value
                task.review_notes.extend(value.requested_changes or [value.reason])
                if action == DirectorDecisionAction.REASSIGN:
                    task.assigned_worker_id = ""
                    task.preferred_worker_id = ""
            elif action in {DirectorDecisionAction.BLOCK, DirectorDecisionAction.ESCALATE}:
                task.status = DirectorTaskStatus.BLOCKED.value
                task.blockers.append(value.reason or action.value)
            task.updated_at = time.time()
            self.decisions.append(value)
            if (
                task.assigned_worker_id
                and task.attempts not in task.performance_recorded_attempts
                and action in {
                    DirectorDecisionAction.ACCEPT,
                    DirectorDecisionAction.INTEGRATE,
                    DirectorDecisionAction.REVISE,
                    DirectorDecisionAction.REASSIGN,
                    DirectorDecisionAction.BLOCK,
                    DirectorDecisionAction.ESCALATE,
                }
            ):
                accepted = action in {
                    DirectorDecisionAction.ACCEPT,
                    DirectorDecisionAction.INTEGRATE,
                }
                current_validations = [
                    item for item in task.validations if item.attempt == task.attempts
                ]
                self.performance.record(
                    task.assigned_worker_id,
                    task.role,
                    accepted=accepted,
                    elapsed_seconds=max(0.0, time.time() - task.attempt_started_at),
                    validations_passed=sum(item.passed for item in current_validations),
                    revisions=0 if accepted else 1,
                )
                task.performance_recorded_attempts.append(task.attempts)
            self.phase = (
                DirectorPhase.REVISE.value
                if task.status == DirectorTaskStatus.REVISION.value
                else DirectorPhase.INTEGRATE.value
                if task.status in {DirectorTaskStatus.ACCEPTED.value, DirectorTaskStatus.INTEGRATED.value}
                else DirectorPhase.REVIEW.value
            )
            self._refresh_ready()
            self.updated_at = task.updated_at
            self._save()
        self._emit("director.decision", task_id=task.id, decision=value.to_dict())
        return task

    def complete(self) -> None:
        incomplete = [
            task.id for task in self.tasks.values()
            if task.status not in {
                DirectorTaskStatus.ACCEPTED.value,
                DirectorTaskStatus.INTEGRATED.value,
            }
        ]
        if incomplete:
            raise ValueError("Cannot complete director run; unresolved tasks: " + ", ".join(incomplete))
        with self._lock:
            self.phase = DirectorPhase.COMPLETED.value
            self.completed_at = time.time()
            self.updated_at = self.completed_at
            self._save()
        self._emit("director.completed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_path": str(self.project_path),
            "objective": self.objective,
            "phase": self.phase,
            "config": self.config.to_dict(),
            "tasks": [task.to_dict() for task in self.tasks.values()],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }

    def system_prompt(self) -> str:
        workers = "\n".join(
            f"- {worker.id}: {worker.backend_type}/{worker.model}; roles={','.join(worker.roles) or 'any'}; "
            f"capabilities={','.join(worker.capabilities) or 'standard'}"
            for worker in self.config.workers if worker.enabled
        ) or "- No worker pool configured; request configuration before dispatch."
        return f"""You are the frontier Director for this Resonant session.

You own understanding, decomposition, delegation, evidence review, revision,
acceptance, safe integration, and the final response. Do not perform broad
implementation work yourself when an eligible worker can do it.

Optimize first for a correct, production-quality, fully verified user outcome,
then completion reliability and wall-clock performance. Token and compute use
are secondary diagnostics. Do not reduce context, output, validation, or useful
worker effort merely to save tokens.

REQUIRED CONTROL FLOW:
1. Create a dependency-aware task graph with director_plan before delegation.
2. Give each task a bounded objective, write scope, dependencies, acceptance
   checks, capabilities, worker role, and relevant artifact_ids. Preserve
   multimodal inputs for workers that declare the required capability.
3. Dispatch ready tasks with task/task_batch and include director_task_id.
   Parallelize independent work; never batch tasks that depend on each other.
4. Inspect structured handoffs, diffs, artifacts, and actual validation output.
5. Record review decisions with director_decide. Acceptance is rejected by the
   runtime until deterministic evidence gates pass.
6. Revise or reassign weak work. Integrate only accepted isolated writer work.
7. Finish only after director_complete succeeds.

Never claim a check ran without runtime evidence. Never discard a worker's full
transcript or artifact; request it through @agent or @artifact when summaries
are insufficient. User steering updates the active plan without cancelling
unrelated accepted work.

Configured worker pool:
{workers}
""".strip()

    def _require_task(self, task_id: str) -> DirectorTask:
        try:
            return self.tasks[str(task_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown director task: {task_id}") from exc

    def _refresh_ready(self) -> None:
        accepted = {
            task.id for task in self.tasks.values()
            if task.status in {DirectorTaskStatus.ACCEPTED.value, DirectorTaskStatus.INTEGRATED.value}
        }
        for task in self.tasks.values():
            if task.status == DirectorTaskStatus.PLANNED.value and all(
                dependency in accepted for dependency in task.dependencies
            ):
                task.status = DirectorTaskStatus.READY.value

    @staticmethod
    def _assert_acyclic(tasks: Iterable[DirectorTask]) -> None:
        graph = {task.id: list(task.dependencies) for task in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError(f"Director task graph contains a cycle at {node}")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph.get(node, []):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for task_id in graph:
            visit(task_id)

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        temp.replace(self.path)

    def _emit(self, event: str, **payload: Any) -> None:
        value = {
            "event": event,
            "director_run_id": self.id,
            "phase": self.phase,
            "timestamp": time.time(),
            "run": self.to_dict(),
            **payload,
        }
        try:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        try:
            self._on_event(value)
        except Exception:
            pass
