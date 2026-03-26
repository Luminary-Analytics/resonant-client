"""
Background planner/generator/evaluator cycle runner for Resonant harnesses.

This orchestrator is intentionally conservative: it chooses the next role from
the current `.resonant-harness` state, runs a single role session, then
re-checks the harness before deciding whether to continue.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from .harness_state import HarnessWorkspace

logger = logging.getLogger(__name__)


class HarnessCycleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class HarnessCycleStep:
    role: str
    backend_type: str
    model: str
    status: str
    started_at: str
    completed_at: str = ""
    result: str = ""
    error: str = ""
    summary_before: dict[str, Any] = field(default_factory=dict)
    summary_after: dict[str, Any] = field(default_factory=dict)
    auto_transition: str = ""
    steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "backend_type": self.backend_type,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "summary_before": self.summary_before,
            "summary_after": self.summary_after,
            "auto_transition": self.auto_transition,
            "steps": self.steps,
        }

    def to_full_dict(self) -> dict[str, Any]:
        return {
            **self.to_dict(),
            "result": self.result,
        }


@dataclass
class HarnessCycleRun:
    id: str
    name: str
    project_path: str
    objective: str
    max_loops: int
    status: HarnessCycleStatus = HarnessCycleStatus.PENDING
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    current_role: str = ""
    current_loop: int = 0
    message: str = ""
    error: str = ""
    steps: list[HarnessCycleStep] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        active_step = self.steps[-1].to_dict() if self.steps else None
        return {
            "id": self.id,
            "name": self.name,
            "project_path": self.project_path,
            "objective": self.objective,
            "max_loops": self.max_loops,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "current_role": self.current_role,
            "current_loop": self.current_loop,
            "message": self.message,
            "error": self.error,
            "step_count": len(self.steps),
            "active_step": active_step,
        }

    def to_full_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["steps"] = [step.to_full_dict() for step in self.steps]
        return data


class HarnessOrchestrator:
    def __init__(
        self,
        *,
        summary_getter: Callable[[str], dict[str, Any]],
        prompt_builder: Callable[[str, str, str | None], str],
        backend_selector: Callable[[str, str | None], tuple[str, str]],
        retry_backend_selector: Optional[Callable[[str, str, str | None], tuple[str, str]]] = None,
        role_timeout_getter: Optional[Callable[[str], float | None]] = None,
        retry_timeout_getter: Optional[Callable[[str], float | None]] = None,
        role_runner: Callable[..., dict[str, Any]],
        teacher_escalator: Optional[Callable[..., dict[str, Any]]] = None,
        max_concurrent: int = 1,
        max_teacher_recoveries: int = 2,
    ):
        self._get_summary = summary_getter
        self._build_prompt = prompt_builder
        self._select_backend = backend_selector
        self._select_retry_backend = retry_backend_selector
        self._get_role_timeout = role_timeout_getter
        self._get_retry_timeout = retry_timeout_getter
        self._run_role = role_runner
        self._teacher_escalate = teacher_escalator
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent)
        self._runs: dict[str, HarnessCycleRun] = {}
        self._lock = threading.Lock()
        self._max_teacher_recoveries = max(0, max_teacher_recoveries)

    def start_cycle(
        self,
        *,
        project_path: str,
        name: str = "",
        objective: str = "",
        max_loops: int = 6,
    ) -> HarnessCycleRun:
        with self._lock:
            for existing in self._runs.values():
                if (
                    existing.project_path == project_path
                    and existing.status in {HarnessCycleStatus.PENDING, HarnessCycleStatus.RUNNING}
                ):
                    raise ValueError("A harness cycle is already running for this project")
        run = HarnessCycleRun(
            id=uuid.uuid4().hex[:12],
            name=name or "Harness Cycle",
            project_path=project_path,
            objective=objective.strip(),
            max_loops=max(1, max_loops),
        )
        with self._lock:
            self._runs[run.id] = run
        self._pool.submit(self._run_cycle, run)
        return run

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda item: item.created_at, reverse=True)[:limit]
            return [run.to_dict() for run in runs]

    def get_run(self, run_id: str) -> Optional[HarnessCycleRun]:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run.status not in {HarnessCycleStatus.PENDING, HarnessCycleStatus.RUNNING}:
                return False
            run.cancel_event.set()
            return True

    def _run_cycle(self, run: HarnessCycleRun) -> None:
        run.status = HarnessCycleStatus.RUNNING
        run.started_at = datetime.now().isoformat()
        harness = HarnessWorkspace(run.project_path)
        harness.ensure_layout()
        harness.append_run_event(
            "cycle_started",
            {"cycle_id": run.id, "name": run.name, "objective": run.objective, "max_loops": run.max_loops},
        )

        no_progress_count = 0
        blocked_count = 0
        teacher_recovery_count = 0

        try:
            while run.current_loop < run.max_loops and not run.cancel_event.is_set():
                summary_before = self._get_summary(run.project_path)
                next_role = self._choose_next_role(summary_before)
                if next_role is None:
                    run.status = HarnessCycleStatus.COMPLETED
                    run.message = self._completion_message(summary_before)
                    break

                run.current_role = next_role
                backend_type, model = self._select_backend(next_role, run.project_path)
                timeout_seconds = self._get_role_timeout(next_role) if self._get_role_timeout else None
                prompt = self._build_prompt(next_role, run.project_path, run.objective)
                step = HarnessCycleStep(
                    role=next_role,
                    backend_type=backend_type,
                    model=model,
                    status="running",
                    started_at=datetime.now().isoformat(),
                    summary_before=summary_before,
                )
                run.steps.append(step)
                harness.append_run_event(
                    "cycle_step_started",
                    {
                        "cycle_id": run.id,
                        "role": next_role,
                        "backend_type": backend_type,
                        "model": model,
                        "loop_index": run.current_loop + 1,
                    },
                )

                try:
                    result = self._run_role(
                        project_path=run.project_path,
                        session_role=next_role,
                        prompt=prompt,
                        backend_type=backend_type,
                        model=model,
                        cancel_event=run.cancel_event,
                        timeout_seconds=timeout_seconds,
                    )
                    step.result = str(result.get("result") or "")
                    step.error = str(result.get("error") or "")
                    step.steps = int(result.get("steps") or 0)
                    step.status = "failed" if step.error else "completed"
                except Exception as exc:
                    step.error = str(exc)
                    step.status = "failed"
                    logger.exception("Harness cycle step failed")

                summary_after = self._get_summary(run.project_path)
                step.summary_after = summary_after
                step.completed_at = datetime.now().isoformat()

                if next_role == "planner" and self._should_auto_approve(summary_after):
                    harness.set_contract_status(status="approved", role="planner")
                    summary_after = self._get_summary(run.project_path)
                    step.summary_after = summary_after
                    step.auto_transition = "approved"

                if step.status == "failed":
                    recovered, summary_after, retry_error = self._attempt_role_retry(
                        run=run,
                        harness=harness,
                        role=next_role,
                        failed_backend=backend_type,
                        prompt=prompt,
                        summary_before=summary_before,
                        summary_after=summary_after,
                    )
                    if recovered:
                        step.status = "retried"
                        step.error = ""
                        step.summary_after = summary_after
                    else:
                        step.error = retry_error or step.error

                run.current_loop += 1
                run.current_role = ""

                harness.append_run_event(
                    "cycle_step_completed",
                    {
                        "cycle_id": run.id,
                        "role": next_role,
                        "status": step.status,
                        "error": step.error,
                        "loop_index": run.current_loop,
                        "auto_transition": step.auto_transition,
                        "contract_status": summary_after.get("contract_status", ""),
                        "evaluator_verdict": summary_after.get("evaluator_verdict", ""),
                    },
                )

                if run.cancel_event.is_set():
                    run.status = HarnessCycleStatus.CANCELLED
                    run.message = "Harness cycle cancelled"
                    break

                if step.status == "failed":
                    run.status = HarnessCycleStatus.FAILED
                    run.error = step.error or f"{next_role} step failed"
                    break

                if self._summary_signature(summary_before) == self._summary_signature(summary_after):
                    no_progress_count += 1
                else:
                    no_progress_count = 0

                if summary_after.get("contract_status") == "failed" and summary_after.get("evaluator_verdict") == "blocked":
                    blocked_count += 1
                else:
                    blocked_count = 0

                if summary_after.get("contract_status") == "passed":
                    run.status = HarnessCycleStatus.COMPLETED
                    run.message = "Sprint passed evaluator checks"
                    break

                if no_progress_count >= 2:
                    recovered, recovery_message = self._attempt_teacher_recovery(
                        run=run,
                        harness=harness,
                        reason="no_progress_twice",
                        failed_role=next_role,
                        summary_before=summary_before,
                        summary_after=summary_after,
                        recovery_index=teacher_recovery_count + 1,
                    )
                    if recovered:
                        teacher_recovery_count += 1
                        no_progress_count = 0
                        blocked_count = 0
                        continue
                    run.status = HarnessCycleStatus.FAILED
                    run.error = recovery_message or "Harness state did not change across two consecutive automated steps"
                    break

                if blocked_count >= 2:
                    recovered, recovery_message = self._attempt_teacher_recovery(
                        run=run,
                        harness=harness,
                        reason="blocked_twice",
                        failed_role=next_role,
                        summary_before=summary_before,
                        summary_after=summary_after,
                        recovery_index=teacher_recovery_count + 1,
                    )
                    if recovered:
                        teacher_recovery_count += 1
                        no_progress_count = 0
                        blocked_count = 0
                        continue
                    run.status = HarnessCycleStatus.FAILED
                    run.error = recovery_message or "Evaluator blocked the cycle twice in a row"
                    break

            if run.status == HarnessCycleStatus.RUNNING:
                if run.cancel_event.is_set():
                    run.status = HarnessCycleStatus.CANCELLED
                    run.message = "Harness cycle cancelled"
                elif run.current_loop >= run.max_loops:
                    run.status = HarnessCycleStatus.FAILED
                    run.error = f"Reached max_loops={run.max_loops}"
                else:
                    run.status = HarnessCycleStatus.COMPLETED
                    run.message = "Harness cycle finished"
        finally:
            run.completed_at = datetime.now().isoformat()
            harness.append_run_event(
                "cycle_finished",
                {
                    "cycle_id": run.id,
                    "status": run.status.value,
                    "message": run.message,
                    "error": run.error,
                    "step_count": len(run.steps),
                },
            )

    def _attempt_teacher_recovery(
        self,
        *,
        run: HarnessCycleRun,
        harness: HarnessWorkspace,
        reason: str,
        failed_role: str,
        summary_before: dict[str, Any],
        summary_after: dict[str, Any],
        recovery_index: int,
    ) -> tuple[bool, str]:
        if not self._teacher_escalate:
            return False, "No teacher escalator is configured for harness recovery"
        if recovery_index > self._max_teacher_recoveries:
            return False, f"Reached max_teacher_recoveries={self._max_teacher_recoveries}"

        step = HarnessCycleStep(
            role="teacher",
            backend_type="teacher",
            model="",
            status="running",
            started_at=datetime.now().isoformat(),
            summary_before=summary_after,
        )
        run.steps.append(step)
        harness.append_run_event(
            "cycle_teacher_recovery_started",
            {
                "cycle_id": run.id,
                "reason": reason,
                "failed_role": failed_role,
                "recovery_index": recovery_index,
            },
        )

        try:
            result = self._teacher_escalate(
                project_path=run.project_path,
                failed_role=failed_role,
                reason=reason,
                objective=run.objective,
            )
            step.backend_type = str(result.get("teacher_provider") or "teacher")
            step.model = str(result.get("teacher_model") or "")
            step.result = str(result.get("result") or result.get("status_message") or "")
            step.error = str(result.get("error") or "")
            step.status = "failed" if step.error else "completed"
        except Exception as exc:
            step.error = str(exc)
            step.status = "failed"
            logger.exception("Harness teacher recovery failed")

        summary_recovered = self._get_summary(run.project_path)
        step.summary_after = summary_recovered
        step.completed_at = datetime.now().isoformat()
        if step.status == "completed" and self._summary_signature(summary_after) == self._summary_signature(summary_recovered):
            step.status = "failed"
            step.error = "Teacher intervention did not change harness state"

        harness.append_run_event(
            "cycle_teacher_recovery_completed",
            {
                "cycle_id": run.id,
                "reason": reason,
                "failed_role": failed_role,
                "recovery_index": recovery_index,
                "status": step.status,
                "error": step.error,
                "teacher_provider": step.backend_type,
                "teacher_model": step.model,
            },
        )

        if step.status == "completed":
            run.message = f"Recovered via {step.backend_type} {step.model}".strip()
            return True, run.message
        return False, step.error or "Teacher recovery failed"

    def _attempt_role_retry(
        self,
        *,
        run: HarnessCycleRun,
        harness: HarnessWorkspace,
        role: str,
        failed_backend: str,
        prompt: str,
        summary_before: dict[str, Any],
        summary_after: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str]:
        if not self._is_retryable_failure(role=role, error=run.steps[-1].error):
            return False, summary_after, run.steps[-1].error
        if not self._select_retry_backend:
            return False, summary_after, run.steps[-1].error

        retry_backend, retry_model = self._select_retry_backend(role, failed_backend, run.project_path)
        if not retry_backend:
            return False, summary_after, run.steps[-1].error

        timeout_seconds = self._get_retry_timeout(role) if self._get_retry_timeout else None
        retry_step = HarnessCycleStep(
            role=role,
            backend_type=retry_backend,
            model=retry_model,
            status="running",
            started_at=datetime.now().isoformat(),
            summary_before=summary_after,
        )
        run.steps.append(retry_step)
        harness.append_run_event(
            "cycle_step_retry_started",
            {
                "cycle_id": run.id,
                "role": role,
                "failed_backend": failed_backend,
                "retry_backend": retry_backend,
                "retry_model": retry_model,
                "loop_index": run.current_loop + 1,
            },
        )

        try:
            result = self._run_role(
                project_path=run.project_path,
                session_role=role,
                prompt=prompt,
                backend_type=retry_backend,
                model=retry_model,
                cancel_event=run.cancel_event,
                timeout_seconds=timeout_seconds,
            )
            retry_step.result = str(result.get("result") or "")
            retry_step.error = str(result.get("error") or "")
            retry_step.steps = int(result.get("steps") or 0)
            retry_step.status = "failed" if retry_step.error else "completed"
        except Exception as exc:
            retry_step.error = str(exc)
            retry_step.status = "failed"
            logger.exception("Harness role retry failed")

        summary_retry = self._get_summary(run.project_path)
        retry_step.summary_after = summary_retry
        retry_step.completed_at = datetime.now().isoformat()
        harness.append_run_event(
            "cycle_step_retry_completed",
            {
                "cycle_id": run.id,
                "role": role,
                "failed_backend": failed_backend,
                "retry_backend": retry_backend,
                "retry_model": retry_model,
                "status": retry_step.status,
                "error": retry_step.error,
                "contract_status": summary_retry.get("contract_status", ""),
                "evaluator_verdict": summary_retry.get("evaluator_verdict", ""),
            },
        )

        if retry_step.status == "completed":
            return True, summary_retry, ""
        return False, summary_retry, retry_step.error or run.steps[-2].error

    @staticmethod
    def _is_retryable_failure(*, role: str, error: str) -> bool:
        if role != "evaluator":
            return False
        lowered = str(error or "").lower()
        return any(
            token in lowered
            for token in (
                "timed out",
                "timeout",
                "interrupted",
                "cli error",
                "no resonant-harness update",
            )
        )

    @staticmethod
    def _summary_signature(summary: dict[str, Any]) -> str:
        stable = {
            "active_sprint_id": summary.get("active_sprint_id", ""),
            "current_phase": summary.get("current_phase", ""),
            "contract_status": summary.get("contract_status", ""),
            "contract_objective": summary.get("contract_objective", ""),
            "evaluator_verdict": summary.get("evaluator_verdict", ""),
            "summary": summary.get("summary", ""),
            "next_steps": list(summary.get("next_steps") or []),
            "required_revisions": list(summary.get("required_revisions") or []),
        }
        return json.dumps(stable, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _should_auto_approve(summary: dict[str, Any]) -> bool:
        return bool(
            summary.get("active_sprint_id")
            and summary.get("contract_objective")
            and summary.get("contract_status") == "proposed"
            and list(summary.get("acceptance_checks") or [])
        )

    @staticmethod
    def _completion_message(summary: dict[str, Any]) -> str:
        if summary.get("contract_status") == "passed":
            return "Sprint already passed"
        if not summary.get("active_sprint_id"):
            return "No active sprint"
        return "No further harness action required"

    @staticmethod
    def _choose_next_role(summary: dict[str, Any]) -> str | None:
        contract_status = str(summary.get("contract_status") or "").strip()
        evaluator_verdict = str(summary.get("evaluator_verdict") or "").strip()
        active_sprint_id = str(summary.get("active_sprint_id") or "").strip()

        if contract_status == "passed":
            return None
        if not active_sprint_id:
            return "planner"
        if contract_status in {"", "proposed", "failed"}:
            return "planner"
        if evaluator_verdict == "blocked":
            return "planner"
        if contract_status in {"approved", "needs_revision"}:
            return "generator"
        if contract_status == "implemented":
            return "evaluator"
        return "planner"
