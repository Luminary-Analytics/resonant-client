"""
Workspace-local harness artifacts for long-running agentic development.

The harness keeps structured state in the project itself so generator,
evaluator, and planner sessions can hand work off without relying on
conversation history alone.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


HARNESS_DIRNAME = ".resonant-harness"


@dataclass
class ProgressState:
    product_goal: str = ""
    current_phase: str = "planning"
    active_sprint_id: str = ""
    active_role: str = ""
    summary: str = ""
    blockers: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    last_validation: str = ""
    validation_checks: list[str] = field(default_factory=list)
    validation_artifacts: list[str] = field(default_factory=list)
    acceptance_evidence: dict[str, str] = field(default_factory=dict)
    last_updated: float = field(default_factory=time.time)


@dataclass
class SprintContract:
    sprint_id: str = ""
    feature_name: str = ""
    objective: str = ""
    deliverables: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    evaluator_focus: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    target_line_hints: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    edit_strategy: str = ""
    status: str = "proposed"
    last_updated: float = field(default_factory=time.time)


@dataclass
class EvaluatorReport:
    sprint_id: str = ""
    verdict: str = "unknown"
    score: float | None = None
    findings: list[str] = field(default_factory=list)
    required_revisions: list[str] = field(default_factory=list)
    passed_checks: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)


@dataclass
class ProductSpec:
    title: str = ""
    summary: str = ""
    user_stories: list[str] = field(default_factory=list)
    sprint_order: list[str] = field(default_factory=list)
    design_principles: list[str] = field(default_factory=list)
    technical_notes: list[str] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class HarnessWorkspace:
    """Structured artifact directory inside the user's project workspace."""

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = self.project_path / HARNESS_DIRNAME

    @property
    def spec_path(self) -> Path:
        return self.root / "spec.json"

    @property
    def progress_path(self) -> Path:
        return self.root / "progress_state.json"

    @property
    def sprint_contract_path(self) -> Path:
        return self.root / "sprint_contract.json"

    @property
    def evaluator_report_path(self) -> Path:
        return self.root / "evaluator_report.json"

    @property
    def handoff_path(self) -> Path:
        return self.root / "handoff.md"

    @property
    def run_history_path(self) -> Path:
        return self.root / "run_history.jsonl"

    @property
    def teacher_escalations_path(self) -> Path:
        return self.root / "teacher_escalations.jsonl"

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.progress_path.exists():
            self.write_progress(ProgressState())
        if not self.spec_path.exists():
            self.write_spec(ProductSpec())
        if not self.sprint_contract_path.exists():
            self.write_sprint_contract(SprintContract())
        if not self.evaluator_report_path.exists():
            self.write_evaluator_report(EvaluatorReport())
        if not self.handoff_path.exists():
            self.handoff_path.write_text(
                "# Resonant Harness Handoff\n\n- Summary:\n- Current sprint:\n- Next action:\n",
                encoding="utf-8",
            )
        if not self.run_history_path.exists():
            self.run_history_path.write_text("", encoding="utf-8")
        if not self.teacher_escalations_path.exists():
            self.teacher_escalations_path.write_text("", encoding="utf-8")

    def read_progress(self) -> ProgressState:
        return ProgressState(**_read_json(self.progress_path))

    def write_progress(self, progress: ProgressState) -> None:
        progress.last_updated = time.time()
        _write_json(self.progress_path, asdict(progress))

    def read_spec(self) -> ProductSpec:
        return ProductSpec(**_read_json(self.spec_path))

    def write_spec(self, spec: ProductSpec) -> None:
        spec.last_updated = time.time()
        _write_json(self.spec_path, asdict(spec))

    def update_spec(self, **changes: Any) -> ProductSpec:
        spec = self.read_spec()
        for key, value in changes.items():
            if hasattr(spec, key):
                setattr(spec, key, value)
        self.write_spec(spec)
        return spec

    def read_sprint_contract(self) -> SprintContract:
        return SprintContract(**_read_json(self.sprint_contract_path))

    def write_sprint_contract(self, contract: SprintContract) -> None:
        contract.last_updated = time.time()
        _write_json(self.sprint_contract_path, asdict(contract))

    def read_evaluator_report(self) -> EvaluatorReport:
        return EvaluatorReport(**_read_json(self.evaluator_report_path))

    def write_evaluator_report(self, report: EvaluatorReport) -> None:
        report.last_updated = time.time()
        _write_json(self.evaluator_report_path, asdict(report))

    def read_handoff(self) -> str:
        if not self.handoff_path.exists():
            return ""
        return self.handoff_path.read_text(encoding="utf-8")

    def write_handoff(self, text: str) -> None:
        self.handoff_path.parent.mkdir(parents=True, exist_ok=True)
        self.handoff_path.write_text(text.rstrip() + "\n", encoding="utf-8")

    def read_run_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.run_history_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.run_history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events[-limit:] if limit else events

    def append_run_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "timestamp": time.time(),
            "event": event_type,
            "payload": payload,
        }
        self.run_history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.run_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_teacher_escalations(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.teacher_escalations_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.teacher_escalations_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-limit:] if limit else rows

    def append_teacher_escalation(self, record: dict[str, Any]) -> None:
        self.teacher_escalations_path.parent.mkdir(parents=True, exist_ok=True)
        with self.teacher_escalations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def update_progress(self, **changes: Any) -> ProgressState:
        progress = self.read_progress()
        for key, value in changes.items():
            if hasattr(progress, key):
                setattr(progress, key, value)
        self.write_progress(progress)
        return progress

    def update_sprint_contract(self, **changes: Any) -> SprintContract:
        contract = self.read_sprint_contract()
        for key, value in changes.items():
            if hasattr(contract, key):
                setattr(contract, key, value)
        self.write_sprint_contract(contract)
        return contract

    def update_evaluator_report(self, **changes: Any) -> EvaluatorReport:
        report = self.read_evaluator_report()
        for key, value in changes.items():
            if hasattr(report, key):
                setattr(report, key, value)
        self.write_evaluator_report(report)
        return report

    def set_active_sprint(
        self,
        *,
        sprint_id: str,
        feature_name: str,
        objective: str,
        deliverables: list[str] | None = None,
        acceptance_checks: list[str] | None = None,
        evaluator_focus: list[str] | None = None,
        target_files: list[str] | None = None,
        target_line_hints: list[str] | None = None,
        validation_commands: list[str] | None = None,
        edit_strategy: str = "",
        status: str = "proposed",
        role: str = "planner",
    ) -> tuple[ProgressState, SprintContract]:
        contract = SprintContract(
            sprint_id=sprint_id,
            feature_name=feature_name,
            objective=objective,
            deliverables=deliverables or [],
            acceptance_checks=acceptance_checks or [],
            evaluator_focus=evaluator_focus or [],
            target_files=target_files or [],
            target_line_hints=target_line_hints or [],
            validation_commands=validation_commands or [],
            edit_strategy=edit_strategy or "",
            status=status,
        )
        self.write_sprint_contract(contract)
        # Starting a new sprint must clear the previous evaluator state so
        # generator/evaluator prompts do not inherit stale verdicts or revisions.
        self.write_evaluator_report(
            EvaluatorReport(
                sprint_id=sprint_id,
                verdict="unknown",
                score=None,
                findings=[],
                required_revisions=[],
                passed_checks=[],
                failed_checks=[],
            )
        )
        progress = self.read_progress()
        progress.active_sprint_id = sprint_id
        progress.active_role = role
        progress.current_phase = "planning" if role == "planner" else "implementation"
        progress.summary = objective or ""
        progress.blockers = []
        progress.next_steps = []
        progress.touched_files = []
        progress.last_validation = ""
        progress.validation_checks = []
        progress.validation_artifacts = []
        progress.acceptance_evidence = {}
        self.write_progress(progress)
        self.append_run_event(
            "set_active_sprint",
            {
                "sprint_id": sprint_id,
                "feature_name": feature_name,
                "objective": objective,
                "deliverables": list(deliverables or []),
                "acceptance_checks": list(acceptance_checks or []),
                "evaluator_focus": list(evaluator_focus or []),
                "target_files": list(target_files or []),
                "target_line_hints": list(target_line_hints or []),
                "validation_commands": list(validation_commands or []),
                "edit_strategy": edit_strategy or "",
                "status": status,
                "role": role,
            },
        )
        return progress, contract

    def record_evaluator_verdict(
        self,
        *,
        sprint_id: str,
        verdict: str,
        findings: list[str] | None = None,
        required_revisions: list[str] | None = None,
        passed_checks: list[str] | None = None,
        failed_checks: list[str] | None = None,
        score: float | None = None,
    ) -> tuple[ProgressState, SprintContract, EvaluatorReport]:
        report = EvaluatorReport(
            sprint_id=sprint_id,
            verdict=verdict,
            score=score,
            findings=findings or [],
            required_revisions=required_revisions or [],
            passed_checks=passed_checks or [],
            failed_checks=failed_checks or [],
        )
        self.write_evaluator_report(report)

        contract = self.read_sprint_contract()
        if contract.sprint_id == sprint_id:
            contract.status = {
                "pass": "passed",
                "revise": "needs_revision",
                "blocked": "failed",
            }.get(verdict, contract.status)
            self.write_sprint_contract(contract)

        progress = self.read_progress()
        progress.active_role = "evaluator"
        progress.current_phase = {
            "pass": "completed",
            "revise": "revision",
            "blocked": "blocked",
        }.get(verdict, progress.current_phase)
        if required_revisions:
            progress.next_steps = list(required_revisions)
        self.write_progress(progress)
        self.append_run_event(
            "evaluator_verdict",
            {
                "sprint_id": sprint_id,
                "verdict": verdict,
                "score": score,
                "findings": list(findings or []),
                "required_revisions": list(required_revisions or []),
                "passed_checks": list(passed_checks or []),
                "failed_checks": list(failed_checks or []),
            },
        )
        return progress, contract, report

    def set_contract_status(
        self,
        *,
        status: str,
        role: str = "",
    ) -> tuple[ProgressState, SprintContract]:
        contract = self.read_sprint_contract()
        contract.status = status
        self.write_sprint_contract(contract)

        progress = self.read_progress()
        phase_map = {
            "proposed": "planning",
            "approved": "implementation",
            "implemented": "evaluation",
            "needs_revision": "revision",
            "passed": "completed",
            "failed": "blocked",
        }
        default_role_map = {
            "proposed": "planner",
            "approved": "generator",
            "implemented": "evaluator",
            "needs_revision": "generator",
            "passed": "evaluator",
            "failed": "evaluator",
        }
        progress.current_phase = phase_map.get(status, progress.current_phase)
        progress.active_role = role or default_role_map.get(status, progress.active_role)
        self.write_progress(progress)
        self.append_run_event(
            "contract_status",
            {
                "sprint_id": contract.sprint_id,
                "status": status,
                "role": role or default_role_map.get(status, ""),
            },
        )
        return progress, contract
