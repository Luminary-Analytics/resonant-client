"""
Resonant Client GUI — ASGI Application

Starlette app with WebSocket endpoint for streaming EngineEvents
to the web-based frontend. The engine runs in a background thread;
events are pushed through a queue to the async WebSocket handler.
"""

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import difflib
from pathlib import Path
import uuid
from datetime import date, datetime
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..events import EngineEvent, make_event
from ..backends import (
    ClaudeBackend, OpenAIBackend,
    ClaudeCodeBackend, CodexBackend, MLXBackend, _find_cli,
)
from ..engine import Session
from .sessions import ProjectManager
from .settings import SettingsManager
from .costs import CostTracker
from .project_instructions import load_project_instructions, get_instruction_info
from .command_projects import CommandProjectStore
from .command_tasks import CommandTaskStore
from .task_runner import TaskRunner
from .scheduler import Scheduler
from .runtime import BackendSpec
from .harness_state import HarnessWorkspace
from .harness_orchestrator import HarnessOrchestrator
from ..engine.hooks import HookRunner
from ..engine.mcp import MCPManager
from ..engine.memory import EngramIntegration
from ..engine.diff_review import generate_review
from ..engine.rag import CodebaseIndex

# Shared reference to the pywebview window (set by server.py when using native mode)
_webview_window = None

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────

_GUI_DIR = Path(__file__).parent
_TEMPLATES_DIR = _GUI_DIR / "templates"
_STATIC_DIR = _GUI_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ── Application State ─────────────────────────────────────────────────

class AppState:
    """Shared application state."""

    SESSION_MAX_STEPS = 25
    SESSION_MAX_TOKENS = 4096
    HARNESS_ROLE_MAX_TOKENS = {
        "planner": 1024,
        "generator": 1536,
        "evaluator": 384,
    }
    CODE_SESSION_ROLES = {"planner", "generator", "evaluator"}

    def __init__(self):
        self.available_backends: dict = {}
        self.backend = None
        self.backend_spec: Optional[BackendSpec] = None
        self.session: Optional[Session] = None
        self.api_url = ""
        self.ollama_url = ""
        self.lmstudio_url = ""
        self.active_thread: Optional[threading.Thread] = None
        self.cancel_requested = threading.Event()
        # Permission / choice flow
        self.permission_response = threading.Event()
        self.permission_result = [True]
        self.choice_response = threading.Event()
        self.choice_result = [""]
        # Project / session manager
        self.project = ProjectManager()
        self._first_message_sent = False
        # Settings & cost tracking
        self.settings = SettingsManager()
        self.permission_mode = str(
            self.settings.get("general", "default_permission_mode", "bypass") or "bypass"
        )
        self.costs = CostTracker()
        self._budget_alert_days: set[str] = set()
        # Project instructions (RESONANT.md)
        self._project_instructions: str | None = None
        # Background tasks & scheduler
        self.task_runner = TaskRunner()
        self.scheduler = Scheduler(self.task_runner)
        # Command center
        self.command_task_store = CommandTaskStore()
        self.command_project_store = CommandProjectStore()
        self.command_feed: list[dict] = []
        self._ws_ref = None
        self._ws_loop = None
        self._monitored_task_ids: set[str] = set()
        self._scheduler_started = False
        # Extension systems
        self.hook_runner = HookRunner(self.settings)
        self.mcp_manager = MCPManager(self.settings)
        self.base_engram = EngramIntegration(self.settings)
        self.base_engram.set_mcp_manager(self.mcp_manager)
        self.engram = self.base_engram.clone(namespace=self._project_namespace(self.project.project_path))
        self.harness = HarnessWorkspace(self.project.project_path)
        self.codebase_index: Optional[CodebaseIndex] = None
        self.harness_orchestrator = HarnessOrchestrator(
            summary_getter=lambda project_path: self.get_harness_summary(project_path),
            prompt_builder=lambda session_role, project_path, objective="": self._build_harness_cycle_prompt(
                session_role=session_role,
                project_path=project_path,
                objective=objective,
            ),
            backend_selector=lambda session_role, project_path=None: self.select_harness_backend(
                session_role=session_role,
                project_path=project_path,
            ),
            retry_backend_selector=lambda session_role, failed_backend="", project_path=None: self.select_harness_retry_backend(
                session_role=session_role,
                failed_backend=failed_backend,
                project_path=project_path,
            ),
            role_timeout_getter=lambda session_role: self.get_harness_role_timeout_seconds(session_role),
            retry_timeout_getter=lambda session_role: self.get_harness_role_retry_timeout_seconds(session_role),
            role_runner=lambda **kwargs: self.run_harness_role_once(**kwargs),
            teacher_escalator=lambda **kwargs: self.run_harness_teacher_escalation(**kwargs),
        )
        self.scheduler.set_backend_factory(lambda _task: self.make_background_session)
        self.scheduler.set_special_executor(self.run_scheduled_task)
        self.apply_project_context(self.project.project_path, refresh_index=True)

    def _push_agent_event(self, task_id: str, event: dict):
        """Push a live agent event to the frontend via WebSocket (called from worker thread)."""
        ws = self._ws_ref
        loop = self._ws_loop
        if not ws or not loop or task_id not in self._monitored_task_ids:
            return
        try:
            payload = {"event": "command_agent_event", "task_id": task_id, **event}
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
        except Exception:
            pass

    @staticmethod
    def _normalize_path(project_path: str) -> str:
        return os.path.normpath(project_path).replace("\\", "/").lower()

    def _project_namespace(self, project_path: str) -> str:
        normalized = os.path.normpath(project_path).replace("\\", "/").lower()
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"project:{digest}"

    def _session_auto_approve(self, mode: Optional[str] = None) -> bool:
        return (mode or self.permission_mode) != "ask"

    @staticmethod
    def normalize_session_mode(value: str) -> str:
        return "chat" if value == "chat" else "code"

    @classmethod
    def normalize_session_role(cls, session_mode: str, value: str) -> str:
        session_mode = cls.normalize_session_mode(session_mode)
        if session_mode == "chat":
            return "chat"
        return value if value in cls.CODE_SESSION_ROLES else "generator"

    def build_harness_instructions(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return ""

        harness = HarnessWorkspace(project_path)
        role_block = {
            "planner": (
                "You are the planner session. Start by reading the harness files, then update "
                "`spec.json`, `progress_state.json`, and `sprint_contract.json`. Define the next "
                "sprint in concrete, testable terms before implementation starts."
            ),
            "generator": (
                "You are the generator session. Read the current harness state first. Implement only "
                "the active sprint, run the cheapest relevant validation, then update "
                "`progress_state.json` and `handoff.md` before finishing. Do not start coding if the "
                "sprint contract is not approved or marked for revision."
            ),
            "evaluator": (
                "You are the evaluator session. Read the sprint contract and current progress first. "
                "Verify the implementation against the stated acceptance checks, run focused tests or "
                "tool-based checks, then write a concrete verdict to `evaluator_report.json`."
            ),
        }[session_role]

        return (
            "\n\n--- HARNESS INSTRUCTIONS (.resonant-harness) ---\n"
            f"Session role: {session_role}\n"
            f"Harness root: {harness.root}\n"
            f"Read first:\n"
            f"- {harness.spec_path}\n"
            f"- {harness.progress_path}\n"
            f"- {harness.sprint_contract_path}\n"
            f"- {harness.evaluator_report_path}\n"
            f"- {harness.handoff_path}\n\n"
            f"{role_block}\n"
            "If the harness files are mostly empty, initialize only the minimum state needed for the current request.\n"
            f"{self.build_harness_output_contract(session_mode=session_mode, session_role=session_role)}\n"
            "--- END HARNESS INSTRUCTIONS ---"
        )

    def build_harness_output_contract(
        self,
        *,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return ""

        templates = {
            "planner": {
                "action": "planner_update",
                "spec": {
                    "title": "",
                    "summary": "",
                    "user_stories": [],
                    "sprint_order": [],
                    "design_principles": [],
                    "technical_notes": [],
                },
                "progress": {
                    "product_goal": "",
                    "summary": "",
                    "blockers": [],
                    "next_steps": [],
                    "current_phase": "planning",
                },
                "sprint_contract": {
                    "sprint_id": "",
                    "feature_name": "",
                    "objective": "",
                    "deliverables": [],
                    "acceptance_checks": [],
                    "evaluator_focus": [],
                    "status": "proposed",
                },
            },
            "generator": {
                "action": "generator_update",
                "progress": {
                    "summary": "",
                    "blockers": [],
                    "next_steps": [],
                    "touched_files": [],
                    "last_validation": "",
                    "validation_checks": [],
                    "current_phase": "implementation",
                },
                "sprint_status": "implemented",
                "handoff_markdown": "# Summary\n\n# Next Action\n",
            },
            "evaluator": {
                "action": "evaluator_verdict",
                "sprint_id": "",
                "verdict": "revise",
                "score": 0.0,
                "findings": [],
                "required_revisions": [],
                "passed_checks": [],
                "failed_checks": [],
            },
        }
        template_text = json.dumps(templates[session_role], indent=2, ensure_ascii=False)
        return (
            "End your final assistant response with a fenced code block named "
            "`resonant-harness` containing valid JSON for your role. Always include the explicit "
            "`action` field shown below. If the objective asks for bullets, a report, or an audit, "
            "put that human-readable content in `handoff_markdown` or concise summary fields, then "
            "still end with the full harness block. Use only concrete values, keep lists short, and "
            "omit fields you cannot justify.\n"
            f"```resonant-harness\n{template_text}\n```"
        )

    def get_harness_summary(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        progress = harness.read_progress()
        contract = harness.read_sprint_contract()
        report = harness.read_evaluator_report()
        recent_history = harness.read_run_history(limit=5)
        recent_teacher_escalations = harness.read_teacher_escalations(limit=3)
        return {
            "root": str(harness.root),
            "spec_path": str(harness.spec_path),
            "progress_path": str(harness.progress_path),
            "sprint_contract_path": str(harness.sprint_contract_path),
            "evaluator_report_path": str(harness.evaluator_report_path),
            "handoff_path": str(harness.handoff_path),
            "run_history_path": str(harness.run_history_path),
            "teacher_escalations_path": str(harness.teacher_escalations_path),
            "current_phase": progress.current_phase,
            "active_sprint_id": progress.active_sprint_id,
            "active_role": progress.active_role,
            "summary": progress.summary,
            "blockers": list(progress.blockers),
            "next_steps": list(progress.next_steps),
            "touched_files": list(progress.touched_files),
            "last_validation": progress.last_validation,
            "validation_checks": list(progress.validation_checks),
            "contract_status": contract.status,
            "contract_feature_name": contract.feature_name,
            "contract_objective": contract.objective,
            "deliverables": list(contract.deliverables),
            "acceptance_checks": list(contract.acceptance_checks),
            "evaluator_focus": list(contract.evaluator_focus),
            "evaluator_verdict": report.verdict,
            "findings": list(report.findings),
            "required_revisions": list(report.required_revisions),
            "recent_run_events": recent_history,
            "recent_teacher_escalations": recent_teacher_escalations,
        }

    @staticmethod
    def _truncate_text(value: str, *, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 1)].rstrip() + "…"

    def get_harness_evaluator_mode(self) -> str:
        raw = str(os.environ.get("RESONANT_HARNESS_EVALUATOR_MODE", "hybrid") or "").strip().lower()
        if raw in {"full", "artifacts", "structured", "hybrid"}:
            return raw
        return "hybrid"

    def get_harness_evaluator_artifact_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_EVALUATOR_ARTIFACT_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 192

    def get_harness_evaluator_structured_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_EVALUATOR_STRUCTURED_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 256

    def should_use_harness_artifact_evaluator(self, project_path: Optional[str] = None) -> bool:
        mode = self.get_harness_evaluator_mode()
        if mode == "full":
            return False
        if mode == "artifacts":
            return True

        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        contract = harness.read_sprint_contract()
        progress = harness.read_progress()

        objective_lower = str(contract.objective or "").strip().lower()
        feature_lower = str(contract.feature_name or "").strip().lower()

        explicit_read_only_tokens = (
            "read-only",
            "read files only",
            "do not modify repository files",
        )
        if any(token in objective_lower for token in explicit_read_only_tokens):
            return True

        reporting_tokens = (
            "summarize",
            "summary",
            "audit",
            "compare",
            "inventory",
            "explain",
            "record findings",
            "capture findings",
            "handoff artifact",
            "table",
            "bullet",
            "bullets",
        )
        if objective_lower.startswith("read ") and any(token in objective_lower for token in reporting_tokens):
            return True

        if (
            not list(progress.touched_files or [])
            and any(token in feature_lower for token in ("audit", "summary", "inventory", "validation"))
            and any(token in objective_lower for token in ("read ", "record findings", "capture findings", "handoff"))
        ):
            return True

        return False

    @staticmethod
    def _resolve_harness_touched_path(project_path: str, raw_path: str) -> Path:
        raw = str(raw_path or "").strip()
        if not raw:
            return Path(project_path)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(project_path) / candidate
        try:
            return candidate.resolve()
        except Exception:
            return candidate

    @staticmethod
    def _format_numbered_excerpt(path: Path, *, max_lines: int = 80, max_chars: int = 2400) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...[truncated]"
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + ["...[truncated]"]
        return "\n".join(f"{index:>4}: {line}" for index, line in enumerate(lines, start=1))

    @staticmethod
    def _normalize_acceptance_check_phrase(check: str) -> str:
        phrase = re.sub(
            r"^(mention|include|cover|state|validate|return|record|show|verify|use)\s+",
            "",
            str(check).strip().lower(),
        )
        phrase = re.sub(r"\s+", " ", phrase).strip(" .")
        return phrase

    def _build_acceptance_check_coverage(
        self,
        acceptance_checks: list[str],
        evidence_text: str,
    ) -> list[dict[str, Any]]:
        lowered = str(evidence_text or "").lower()
        coverage = []
        for check in acceptance_checks[:8]:
            phrase = self._normalize_acceptance_check_phrase(check)
            coverage.append(
                {
                    "check": check,
                    "matched": bool(phrase and phrase in lowered),
                    "normalized_phrase": phrase,
                }
            )
        return coverage

    def build_harness_structured_evidence_bundle(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1600)

        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            handoff_text,
        ]

        for raw_path in touched_files[:4]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                try:
                    file_record["size_bytes"] = resolved.stat().st_size
                except OSError:
                    file_record["size_bytes"] = None
                excerpt = self._format_numbered_excerpt(resolved)
                file_record["excerpt"] = excerpt
                evidence_parts.append(excerpt)
            else:
                file_record["excerpt"] = "[missing file]"
                evidence_parts.append(f"{raw_path}: missing file")
            files.append(file_record)

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        combined_evidence = "\n".join(part for part in evidence_parts if part)
        coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_evidence)

        return {
            "summary": summary,
            "handoff_excerpt": handoff_text,
            "files": files,
            "acceptance_check_coverage": coverage,
        }

    def can_use_harness_structured_evaluator(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        if not touched_files:
            return False
        bundle = self.build_harness_structured_evidence_bundle(target_path)
        return any(item.get("exists") for item in bundle["files"])

    def get_harness_evaluator_strategy(self, project_path: Optional[str] = None) -> str:
        mode = self.get_harness_evaluator_mode()
        if mode == "full":
            return "full"
        if mode == "artifacts":
            return "artifacts"
        if mode == "structured":
            return "structured" if self.can_use_harness_structured_evaluator(project_path) else "full"
        if self.should_use_harness_artifact_evaluator(project_path):
            return "artifacts"
        if self.can_use_harness_structured_evaluator(project_path):
            return "structured"
        return "full"

    def build_harness_evaluator_artifact_prompt(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1800)

        lines = [
            "Artifact-only evaluator mode.",
            "Do not inspect repository files and do not use tools.",
            "Judge the sprint only from the harness artifacts below.",
            "Pass only if the existing evidence already satisfies the acceptance checks.",
            "If the evidence is incomplete but recoverable, return revise with concrete required revisions.",
            "Use blocked only for a hard blocker or missing evidence that prevents a meaningful verdict.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            f"Last evaluator verdict: {summary['evaluator_verdict'] or 'unknown'}",
            "",
        ]

        blockers = self._normalize_string_list(summary.get("blockers"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        touched_files = self._normalize_string_list(summary.get("touched_files"))

        if checks:
            lines.append("Acceptance checks:")
            lines.extend(f"- {item}" for item in checks[:8])
        if blockers:
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:8])
        if next_steps:
            lines.append("Current next steps:")
            lines.extend(f"- {item}" for item in next_steps[:8])
        if revisions:
            lines.append("Required revisions from prior evaluator:")
            lines.extend(f"- {item}" for item in revisions[:8])
        if touched_files:
            lines.append("Touched files:")
            lines.extend(f"- {item}" for item in touched_files[:12])
        if validation_checks:
            lines.append("Recorded validation checks:")
            lines.extend(f"- {item}" for item in validation_checks[:12])

        lines.extend(
            [
                "",
                "Progress summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
                "",
                "Recorded validation checks:",
                *[f"- {item}" for item in validation_checks[:12] or ["(none)"]],
                "",
                "Handoff artifact excerpt:",
                handoff_text or "(none)",
                "",
                "Keep the prose to at most 4 short lines, then finish with a valid ```resonant-harness JSON block for evaluator_verdict.",
            ]
        )
        return "\n".join(lines)

    def build_harness_structured_evaluator_prompt(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        bundle = self.build_harness_structured_evidence_bundle(target_path)
        summary = bundle["summary"]

        lines = [
            "Structured evaluator mode.",
            "Do not use tools and do not inspect any files beyond the evidence included below.",
            "Judge the sprint only from the compact evidence bundle.",
            "Prefer pass only when the evidence clearly satisfies the acceptance checks.",
            "Return revise when the implementation might be correct but the evidence is incomplete or a check is unsupported.",
            "Return blocked only for a hard blocker or clearly missing implementation evidence.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Acceptance checks:",
        ]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        lines.extend(
            [
                "",
                "Progress summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
                "",
                "Recorded validation checks:",
                *[f"- {item}" for item in validation_checks[:12] or ["(none)"]],
                "",
                "Handoff artifact excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item["matched"] else "unmatched"
            lines.append(f"- {marker}: {item['check']}")

        lines.append("")
        lines.append("Touched file evidence:")
        for file_item in bundle["files"]:
            lines.append(
                f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})"
            )
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.append("Keep the prose to at most 6 short lines, then finish with a valid ```resonant-harness JSON block for evaluator_verdict.")
        return "\n".join(lines)

    def precheck_harness_evaluator_payload(
        self,
        *,
        project_path: Optional[str] = None,
        evaluation_mode: str,
    ) -> dict[str, Any] | None:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        if not acceptance_checks:
            return None

        blockers = self._normalize_string_list(summary.get("blockers"))
        required_revisions = self._normalize_string_list(summary.get("required_revisions"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        last_validation = str(summary.get("last_validation") or "").strip()
        handoff_excerpt = self._truncate_text(harness.read_handoff(), max_chars=1200)
        evidence_present = bool(last_validation or validation_checks or handoff_excerpt)

        if blockers:
            findings = blockers[:3]
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "blocked",
                "findings": findings,
                "passed_checks": [],
                "failed_checks": findings,
                "required_revisions": findings,
                "score": 0.0,
            }

        if evaluation_mode == "structured":
            bundle = self.build_harness_structured_evidence_bundle(target_path)
            coverage = bundle["acceptance_check_coverage"]
            files = bundle["files"]
            existing_file_count = sum(1 for item in files if item.get("exists"))
        else:
            evidence_text = "\n".join(
                part
                for part in (
                    summary.get("summary") or "",
                    last_validation,
                    "\n".join(validation_checks),
                    handoff_excerpt,
                )
                if part
            )
            coverage = self._build_acceptance_check_coverage(acceptance_checks, evidence_text)
            existing_file_count = 0

        matched_checks = [item["check"] for item in coverage if item.get("matched")]
        unmatched_checks = [item["check"] for item in coverage if not item.get("matched")]
        has_complete_coverage = bool(coverage) and not unmatched_checks

        if has_complete_coverage and evidence_present and (
            evaluation_mode != "structured" or existing_file_count > 0
        ):
            findings = validation_checks[:3] or [last_validation or "Acceptance checks are covered by the harness evidence bundle."]
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "pass",
                "findings": findings[:3],
                "passed_checks": matched_checks[:8],
                "failed_checks": [],
                "required_revisions": [],
                "score": 1.0,
            }

        obvious_revisions = required_revisions[:3]
        if not obvious_revisions:
            if evaluation_mode == "structured" and touched_files and existing_file_count == 0:
                obvious_revisions = ["Touched files were recorded, but the compact file evidence is missing."]
            elif unmatched_checks and not validation_checks and (
                len(unmatched_checks) >= max(2, len(acceptance_checks) // 2)
            ):
                obvious_revisions = unmatched_checks[:3]

        if obvious_revisions:
            findings = obvious_revisions[:3]
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "revise",
                "findings": findings,
                "passed_checks": matched_checks[:8],
                "failed_checks": findings,
                "required_revisions": findings,
                "score": 0.5,
            }

        return None

    def infer_evidence_only_evaluator_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
    ) -> dict[str, Any] | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None

        lowered = stripped.lower()
        summary = self.get_harness_summary(project_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))

        normalized_check_phrases = []
        for check in acceptance_checks:
            phrase = self._normalize_acceptance_check_phrase(check)
            if phrase:
                normalized_check_phrases.append(phrase)

        verdict = ""
        if any(token in lowered for token in (" blocked.", " blocked ", "hard blocker", "cannot proceed")):
            verdict = "blocked"
        elif any(
            token in lowered
            for token in (
                "✗",
                "not mentioned in evidence",
                "not covered",
                "not met",
                "missing from evidence",
                "needs revision",
                "need revision",
                "revise",
                "i need to examine",
                "i need to inspect",
                "i need to verify",
                "to properly evaluate",
                "insufficient evidence",
                "missing evidence",
                "cannot verify",
                "not enough evidence",
            )
        ):
            verdict = "revise"
        elif any(
            token in lowered
            for token in (
                "pass.",
                "pass ",
                "passed ",
                "no revisions are needed",
                "no revision is needed",
                "satisfies the acceptance checks",
            )
        ):
            verdict = "pass"
        elif normalized_check_phrases and all(phrase in lowered for phrase in normalized_check_phrases):
            verdict = "pass"

        if not verdict:
            return None

        sprint_id = str(summary.get("active_sprint_id") or "").strip()

        candidate_lines = []
        for raw_line in stripped.splitlines():
            cleaned = raw_line.strip().lstrip("-* ").strip()
            if not cleaned:
                continue
            if cleaned.startswith("```") or cleaned in {"{", "}"}:
                continue
            if cleaned.lower().startswith(("artifact-only evaluator mode", "finish with a short summary")):
                continue
            candidate_lines.append(self._truncate_text(cleaned, max_chars=220))

        findings = []
        for item in candidate_lines:
            if item not in findings:
                findings.append(item)
            if len(findings) >= 3:
                break
        if not findings:
            findings = [self._truncate_text(stripped, max_chars=220)]

        payload: dict[str, Any] = {
            "action": "evaluator_verdict",
            "sprint_id": sprint_id,
            "verdict": verdict,
            "findings": findings,
            "passed_checks": [],
            "failed_checks": [],
            "required_revisions": [],
            "score": None,
        }

        if verdict == "pass":
            payload["passed_checks"] = acceptance_checks[:8]
            payload["score"] = 1.0
        elif verdict == "revise":
            revisions = acceptance_checks[:3] or ["Record more concrete validation evidence in progress.last_validation and handoff.md."]
            payload["required_revisions"] = revisions
            payload["failed_checks"] = revisions
            payload["score"] = 0.5
        else:
            blockers = self._normalize_string_list(summary.get("blockers"))
            payload["required_revisions"] = blockers[:3] or ["Clear the blocker or add enough evaluation evidence to support a verdict."]
            payload["failed_checks"] = payload["required_revisions"]
            payload["score"] = 0.0

        return payload

    @staticmethod
    def resolve_local_coding_model_root() -> Path:
        return Path(
            os.environ.get("LOCAL_CODING_MODEL_ROOT", "/Users/richbellantoni/Repos/LocalCodingModel")
        ).expanduser().resolve()

    def resolve_local_coding_model_python(self) -> Path:
        root = self.resolve_local_coding_model_root()
        venv_python = root / ".venv" / "bin" / "python"
        if venv_python.exists():
            return venv_python
        return Path(sys.executable).resolve()

    def select_harness_teacher(
        self,
        *,
        session_role: str,
        reason: str = "",
    ) -> tuple[str, str]:
        normalized_role = self.normalize_session_role("code", session_role)
        lowered_reason = reason.lower()
        codex_cli = _find_cli("codex")
        claude_cli = _find_cli("claude")

        prefer_claude = normalized_role == "evaluator" or "blocked" in lowered_reason or "verdict" in lowered_reason
        providers: list[tuple[str, str]] = []
        if prefer_claude:
            providers.extend(
                [
                    ("claude", "claude-opus-4-6"),
                    ("codex", "gpt-5.4"),
                ]
            )
        else:
            providers.extend(
                [
                    ("codex", "gpt-5.4"),
                    ("claude", "claude-opus-4-6"),
                ]
            )

        for provider, model in providers:
            if provider == "codex" and codex_cli:
                return provider, model
            if provider == "claude" and claude_cli:
                return provider, model

        raise ValueError("No teacher CLI is available for harness escalation")

    def wrap_user_message_for_harness(
        self,
        *,
        user_msg: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return user_msg

        harness = HarnessWorkspace(self.project.project_path)
        summary = self.get_harness_summary(self.project.project_path)
        role_requirements = {
            "planner": "Create or refine the spec and propose the next sprint contract. Keep implementation out unless the user explicitly asks for it.",
            "generator": "Implement only the active sprint. Update progress and handoff artifacts before finishing.",
            "evaluator": "Verify against the sprint contract. Write a clear pass, revise, or blocked verdict with concrete required revisions.",
        }[session_role]
        return (
            f"HARNESS ROLE: {session_role}\n"
            f"HARNESS ROOT: {summary['root']}\n"
            "READ THESE FILES BEFORE ACTING:\n"
            f"- {summary['spec_path']}\n"
            f"- {summary['progress_path']}\n"
            f"- {summary['sprint_contract_path']}\n"
            f"- {summary['evaluator_report_path']}\n"
            f"- {summary['handoff_path']}\n\n"
            f"ROLE REQUIREMENTS: {role_requirements}\n\n"
            f"FINAL OUTPUT CONTRACT:\n{self.build_harness_output_contract(session_mode=session_mode, session_role=session_role)}\n\n"
            "USER REQUEST:\n"
            f"{user_msg}"
        )

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, (list, tuple, set)):
            result = []
            for item in value:
                text = str(item).strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def normalize_harness_contract_status(status: str, *, session_role: str) -> str:
        raw = str(status or "").strip().lower()
        if not raw:
            return ""
        aliases = {
            "propose": "proposed",
            "proposed": "proposed",
            "ready": "approved",
            "ready_for_implementation": "approved",
            "implementation_ready": "approved",
            "ready_to_implement": "approved",
            "ready_to_execute": "approved",
            "ready_for_execution": "approved",
            "execution_ready": "approved",
            "ready_to_start": "approved",
            "approve": "approved",
            "approved": "approved",
            "revise": "needs_revision",
            "revision": "needs_revision",
            "needs_revision": "needs_revision",
            "implement": "implemented",
            "implemented": "implemented",
            "ready_for_evaluation": "implemented",
            "evaluation_ready": "implemented",
            "ready_to_evaluate": "implemented",
            "ready_to_review": "implemented",
            "review": "implemented",
            "under_review": "implemented",
            "fail": "failed",
            "failed": "failed",
            "block": "failed",
            "blocked": "failed",
            "pass": "passed",
            "passed": "passed",
        }
        if raw in aliases:
            return aliases[raw]
        if raw in {"complete", "completed", "done"}:
            if session_role == "planner":
                return "approved"
            if session_role == "generator":
                return "implemented"
            return "passed"
        return raw

    def extract_harness_update(
        self,
        *,
        text: str,
        session_mode: str,
        session_role: str,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat" or not text:
            return text, None, None

        matches = list(
            re.finditer(r"```resonant-harness\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        )
        if not matches:
            return text, None, None

        match = matches[-1]
        payload_text = match.group(1).strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return text, None, f"Invalid resonant-harness JSON for {session_role}: {exc.msg}"

        cleaned = f"{text[:match.start()]}{text[match.end():]}".strip()
        return cleaned, payload, None

    @staticmethod
    def rewrite_last_assistant_message(session: Session, original_text: str, cleaned_text: str) -> None:
        if not original_text or original_text == cleaned_text:
            return
        for item in reversed(session.conversation_history):
            if item.get("role") == "assistant" and item.get("content") == original_text:
                item["content"] = cleaned_text
                return

    def apply_harness_update(
        self,
        *,
        session_mode: str,
        session_role: str,
        payload: dict[str, Any],
        project_path: Optional[str] = None,
        assistant_text: str = "",
        user_request: str = "",
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return "Ignored harness update for chat session"

        harness = HarnessWorkspace(project_path or self.project.project_path)
        harness.ensure_layout()
        action = str(payload.get("action") or "").strip()

        if not action:
            # A fenced `resonant-harness` block is already role-scoped by the
            # active session. Default to that role's action so minor teacher or
            # planner omissions do not discard otherwise usable harness state.
            action = {
                "planner": "planner_update",
                "generator": "generator_update",
                "evaluator": "evaluator_verdict",
            }[session_role]

        if action == "planner_update":
            spec_data = payload.get("spec") or {}
            if isinstance(spec_data, dict):
                spec_updates: dict[str, Any] = {}
                for key in ("title", "summary"):
                    value = str(spec_data.get(key) or "").strip()
                    if value:
                        spec_updates[key] = value
                for key in ("user_stories", "sprint_order", "design_principles", "technical_notes"):
                    if key in spec_data:
                        spec_updates[key] = self._normalize_string_list(spec_data.get(key))
                if spec_updates:
                    harness.update_spec(**spec_updates)

            contract_data = payload.get("sprint_contract") or {}
            if isinstance(contract_data, dict):
                current_contract = harness.read_sprint_contract()
                sprint_id = str(contract_data.get("sprint_id") or current_contract.sprint_id).strip()
                objective = str(contract_data.get("objective") or current_contract.objective).strip()
                feature_name = str(contract_data.get("feature_name") or current_contract.feature_name).strip()
                if sprint_id and objective:
                    harness.set_active_sprint(
                        sprint_id=sprint_id,
                        feature_name=feature_name,
                        objective=objective,
                        deliverables=self._normalize_string_list(
                            contract_data.get("deliverables", current_contract.deliverables)
                        ),
                        acceptance_checks=self._normalize_string_list(
                            contract_data.get("acceptance_checks", current_contract.acceptance_checks)
                        ),
                        evaluator_focus=self._normalize_string_list(
                            contract_data.get("evaluator_focus", current_contract.evaluator_focus)
                        ),
                        status=self.normalize_harness_contract_status(
                            str(contract_data.get("status") or current_contract.status or "proposed").strip(),
                            session_role="planner",
                        ) or "proposed",
                        role="planner",
                    )
                elif contract_data:
                    contract_updates: dict[str, Any] = {}
                    for key in ("sprint_id", "feature_name", "objective", "status"):
                        value = str(contract_data.get(key) or "").strip()
                        if value:
                            if key == "status":
                                value = self.normalize_harness_contract_status(value, session_role="planner")
                            contract_updates[key] = value
                    for key in ("deliverables", "acceptance_checks", "evaluator_focus"):
                        if key in contract_data:
                            contract_updates[key] = self._normalize_string_list(contract_data.get(key))
                    if contract_updates:
                        harness.update_sprint_contract(**contract_updates)

            progress_data = payload.get("progress") or {}
            if isinstance(progress_data, dict):
                progress_updates: dict[str, Any] = {"active_role": "planner"}
                for key in ("product_goal", "summary", "last_validation"):
                    value = str(progress_data.get(key) or "").strip()
                    if value:
                        progress_updates[key] = value
                for key in ("blockers", "next_steps", "touched_files", "validation_checks"):
                    if key in progress_data:
                        progress_updates[key] = self._normalize_string_list(progress_data.get(key))
                current_phase = str(progress_data.get("current_phase") or "planning").strip()
                if current_phase:
                    progress_updates["current_phase"] = current_phase
                if progress_updates:
                    harness.update_progress(**progress_updates)

            handoff_markdown = str(payload.get("handoff_markdown") or "").strip()
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            sprint_id = harness.read_sprint_contract().sprint_id
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return f"Applied planner harness update{f' for {sprint_id}' if sprint_id else ''}"

        if action == "generator_update":
            progress_data = payload.get("progress") or {}
            progress_updates: dict[str, Any] = {"active_role": "generator"}
            if isinstance(progress_data, dict):
                for key in ("summary", "product_goal", "last_validation"):
                    value = str(progress_data.get(key) or "").strip()
                    if value:
                        progress_updates[key] = value
                for key in ("blockers", "next_steps", "touched_files", "validation_checks"):
                    if key in progress_data:
                        progress_updates[key] = self._normalize_string_list(progress_data.get(key))
                current_phase = str(progress_data.get("current_phase") or "implementation").strip()
                if current_phase:
                    progress_updates["current_phase"] = current_phase
            harness.update_progress(**progress_updates)

            handoff_markdown = str(payload.get("handoff_markdown") or "").strip()
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            sprint_status = str(payload.get("sprint_status") or "").strip()
            sprint_status = self.normalize_harness_contract_status(sprint_status, session_role="generator")
            if sprint_status in {"proposed", "approved", "implemented", "needs_revision", "passed", "failed"}:
                harness.set_contract_status(status=sprint_status, role="generator")

            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": harness.read_sprint_contract().sprint_id,
                    "sprint_status": sprint_status or "",
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return "Applied generator harness update"

        if action == "evaluator_verdict":
            sprint_id = str(payload.get("sprint_id") or harness.read_sprint_contract().sprint_id).strip()
            verdict = str(payload.get("verdict") or "").strip()
            if not sprint_id or verdict not in {"pass", "revise", "blocked"}:
                raise ValueError("Evaluator verdict requires sprint_id and verdict")
            harness.record_evaluator_verdict(
                sprint_id=sprint_id,
                verdict=verdict,
                findings=self._normalize_string_list(payload.get("findings")),
                required_revisions=self._normalize_string_list(payload.get("required_revisions")),
                passed_checks=self._normalize_string_list(payload.get("passed_checks")),
                failed_checks=self._normalize_string_list(payload.get("failed_checks")),
                score=payload.get("score"),
            )
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "verdict": verdict,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return f"Applied evaluator verdict {verdict} for {sprint_id}"

        raise ValueError(f"Unknown harness action: {action}")

    def build_harness_resume_prompt(
        self,
        *,
        session_mode: str,
        session_role: str,
        project_path: Optional[str] = None,
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return "Resume the chat conversation naturally from the existing context."

        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        spec = harness.read_spec()

        read_order = {
            "planner": [
                summary["spec_path"],
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
            "generator": [
                summary["spec_path"],
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
            "evaluator": [
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
        }[session_role]

        common_lines = [
            f"Resume this project as the {session_role} session using the harness state, not long chat history.",
            "Start by reading these files in order:",
            *[f"{index}. {path}" for index, path in enumerate(read_order, start=1)],
            "",
        ]

        if session_role != "evaluator":
            common_lines.extend(
                [
                f"Product title: {spec.title or 'Unknown'}",
                f"Product summary: {spec.summary or 'Not set'}",
                f"Current phase: {summary['current_phase'] or 'unknown'}",
                ]
            )

        common_lines.extend(
            [
                f"Active sprint: {summary['active_sprint_id'] or 'none'}",
                f"Sprint objective: {summary['contract_objective'] or 'none'}",
                f"Contract status: {summary['contract_status'] or 'unknown'}",
                f"Last evaluator verdict: {summary['evaluator_verdict'] or 'unknown'}",
            ]
        )

        blockers = self._normalize_string_list(summary.get("blockers"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))

        if blockers:
            common_lines.append("Current blockers:")
            common_lines.extend(f"- {item}" for item in blockers[:5])
        if next_steps:
            common_lines.append("Current next steps:")
            common_lines.extend(f"- {item}" for item in next_steps[:5])
        if checks:
            common_lines.append("Acceptance checks:")
            common_lines.extend(f"- {item}" for item in checks[:5])
        if validation_checks:
            common_lines.append("Recorded validation checks:")
            common_lines.extend(f"- {item}" for item in validation_checks[:5])
        if revisions:
            common_lines.append("Required revisions from evaluator:")
            common_lines.extend(f"- {item}" for item in revisions[:5])

        role_lines = {
            "planner": [
                "",
                "Your task:",
                "- refine or complete the spec only where needed",
                "- define or revise the next sprint contract in concrete, testable terms",
                "- do not execute the sprint itself; leave the audit, implementation, or verification work to later roles",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for planner_update",
            ],
            "generator": [
                "",
                "Your task:",
                "- implement only the active sprint",
                "- if the contract is not approved or needs_revision, stop and say that first",
                "- run the cheapest relevant validation before finishing",
                "- record exact validation evidence in progress.last_validation and short check bullets in progress.validation_checks",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for generator_update",
            ],
            "evaluator": [
                "",
                "Your task:",
                "- verify the current implementation against the sprint contract",
                "- state pass, revise, or blocked with concrete findings",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for evaluator_verdict",
            ],
        }[session_role]

        return "\n".join(common_lines + role_lines)

    def apply_permission_mode(self, mode: str, session: Optional[Session] = None) -> str:
        self.permission_mode = mode or "bypass"
        target = session or self.session
        if target:
            target.auto_approve = self._session_auto_approve(self.permission_mode)
        return self.permission_mode

    def _api_key_details(self, provider: str, env_var: str) -> tuple[str, str, str, str]:
        settings_value = str(self.settings.get("api_keys", provider, "") or "")
        if settings_value:
            return settings_value, "settings", "", provider

        env_value = os.environ.get(env_var, "")
        if env_value:
            return env_value, "env", env_var, ""

        return "", "", "", ""

    def apply_project_context(self, project_path: str, refresh_index: bool = True) -> str:
        project_path = os.path.normpath(project_path or self.project.project_path or os.getcwd())
        os.chdir(project_path)

        if self._normalize_path(project_path) != self._normalize_path(self.project.project_path):
            self.project.set_project(project_path)
        else:
            self.project.project_path = project_path
            self.project._ensure_storage()
            self.project._save_recent_project()

        self._project_instructions = load_project_instructions(project_path)
        self.engram = self.base_engram.clone(namespace=self._project_namespace(project_path))
        self.engram.set_mcp_manager(self.mcp_manager)
        self.harness = HarnessWorkspace(project_path)
        self.harness.ensure_layout()

        current_index_path = (
            self._normalize_path(str(self.codebase_index.project_path))
            if self.codebase_index else ""
        )
        if refresh_index or not self.codebase_index or current_index_path != self._normalize_path(project_path):
            self.codebase_index = CodebaseIndex(project_path, engram=self.engram)
        else:
            self.codebase_index._engram = self.engram

        if self.session:
            self._wire_session(
                self.session,
                project_path=project_path,
                engram=self.engram,
                codebase_index=self.codebase_index,
            )

        return project_path

    def _build_harness_cycle_prompt(
        self,
        *,
        session_role: str,
        project_path: str | None = None,
        objective: str = "",
    ) -> str:
        prompt = self.build_harness_resume_prompt(
            session_mode="code",
            session_role=session_role,
            project_path=project_path,
        )
        objective = objective.strip()
        if objective:
            role_guidance = {
                "planner": (
                    "Convert the objective into harness artifacts, not a standalone answer. "
                    "Your job is to define the sprint and leave execution to generator/evaluator; "
                    "do not perform the audit, code change, or validation work yourself unless the "
                    "objective explicitly says the planner must do it. "
                    "If the objective is explicitly read-only, keep the sprint deliverables read-only "
                    "and artifact-focused instead of inventing code changes. "
                    "If the objective asks for bullets, findings, or an audit summary, place that "
                    "content in handoff_markdown and concise progress/spec fields, then finish with "
                    "a valid planner_update resonant-harness block."
                ),
                "generator": (
                    "Treat the objective as implementation guidance for the active sprint. "
                    "If the objective is explicitly read-only, do not modify repository files; only "
                    "read, analyze, and update harness artifacts. "
                    "Keep the final response brief, record validation in progress.last_validation, "
                    "and finish with a valid generator_update resonant-harness block."
                ),
                "evaluator": (
                    "Treat the objective as evaluation scope. Put human-readable findings in the "
                    "normal response and required_revisions/failed_checks, then finish with a valid "
                    "evaluator_verdict resonant-harness block."
                ),
            }[session_role]
            prompt = (
                f"TOP-LEVEL OBJECTIVE:\n{objective}\n\n"
                f"OBJECTIVE HANDLING RULE:\n{role_guidance}\n\n"
                f"{prompt}"
            )
        return prompt

    def _wire_session(
        self,
        session: Session,
        *,
        project_path: Optional[str] = None,
        project_instructions: Optional[str] = None,
        engram: Optional[EngramIntegration] = None,
        codebase_index: Optional[CodebaseIndex] = None,
    ) -> Session:
        target_path = project_path or self.project.project_path
        session.project_instructions = (
            project_instructions
            if project_instructions is not None
            else load_project_instructions(target_path)
        )
        session.hook_runner = self.hook_runner
        session.mcp_tools = self.mcp_manager.get_all_tools()
        session._mcp_manager = self.mcp_manager
        session._engram = engram or self.engram
        session._codebase_index = codebase_index or self.codebase_index
        session.auto_approve = self._session_auto_approve()
        return session

    def detect_backends(self):
        """Detect available backends with parallel network checks."""
        import httpx
        from concurrent.futures import ThreadPoolExecutor, as_completed

        api_url = self.api_url
        ollama_url = self.ollama_url
        available = {}

        # Short connect timeout so unreachable hosts don't block startup
        _timeout = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=4.0)

        # ── Parallel network checks ──────────────────────────────────
        def _check_resonant():
            try:
                resp = httpx.get(f"{api_url}/health", timeout=_timeout)
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "ready":
                    return "resonant", {"url": api_url, "health": data}
            except Exception:
                pass
            return None, None

        def _check_ollama():
            try:
                resp = httpx.get(f"{ollama_url}/api/tags", timeout=_timeout)
                resp.raise_for_status()
                data = resp.json()
                models = [m["name"] for m in data.get("models", [])
                          if not any(kw in m["name"].lower()
                                     for kw in ("embed", "bert", "bge", "nomic"))]
                if models:
                    return "ollama", {"url": ollama_url, "models": models}
            except Exception:
                pass
            return None, None

        def _check_lmstudio_url(url):
            """Check a single LM Studio URL candidate."""
            try:
                resp = httpx.get(f"{url}/models", timeout=_timeout)
                resp.raise_for_status()
                data = resp.json()
                models = [m["id"] for m in data.get("data", [])]
                if models:
                    return url, models
            except Exception:
                pass
            return None, None

        def _check_lmstudio():
            lmstudio_url = os.environ.get("LMSTUDIO_URL", "").rstrip("/")
            if lmstudio_url:
                url, models = _check_lmstudio_url(lmstudio_url)
                if url:
                    self.lmstudio_url = url
                    return "lmstudio", {"url": url, "models": models}
                return None, None
            # Try candidates in parallel
            candidates = [c for c in [self.lmstudio_url, "http://10.0.0.133:1234/v1", "http://localhost:1234/v1"] if c]
            with ThreadPoolExecutor(max_workers=len(candidates)) as p:
                futs = {p.submit(_check_lmstudio_url, c): c for c in candidates}
                for fut in as_completed(futs):
                    url, models = fut.result()
                    if url:
                        self.lmstudio_url = url
                        return "lmstudio", {"url": url, "models": models}
            return None, None

        # Run network checks in parallel
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fn) for fn in [_check_resonant, _check_ollama, _check_lmstudio]]
            for f in as_completed(futures):
                key, val = f.result()
                if key:
                    available[key] = val

        # ── Local / API-key checks (instant) ─────────────────────────
        anthropic_key, anthropic_source, anthropic_env, anthropic_setting = self._api_key_details(
            "anthropic", "ANTHROPIC_API_KEY"
        )
        if anthropic_key:
            try:
                import anthropic  # noqa: F401
                available["claude"] = {
                    "models": ClaudeBackend.MODELS,
                    "api_key_source": anthropic_source,
                    "api_key_env": anthropic_env,
                    "api_key_setting": anthropic_setting,
                }
            except ImportError:
                pass

        openai_key, openai_source, openai_env, openai_setting = self._api_key_details(
            "openai", "OPENAI_API_KEY"
        )
        if openai_key:
            try:
                import openai  # noqa: F401
                available["openai"] = {
                    "models": OpenAIBackend.MODELS,
                    "api_key_source": openai_source,
                    "api_key_env": openai_env,
                    "api_key_setting": openai_setting,
                }
            except ImportError:
                pass

        # Claude Code CLI
        if _find_cli("claude"):
            try:
                import subprocess as _sp
                result = _sp.run(
                    [_find_cli("claude"), "--version"],
                    capture_output=True, text=True, timeout=5,
                    shell=(sys.platform == "win32"),
                )
                if result.returncode == 0:
                    available["claude-code"] = {
                        "models": list(ClaudeCodeBackend.MODELS),
                        "model_labels": ClaudeCodeBackend.MODEL_LABELS,
                        "permission_mode": (
                            self.backend_spec.permission_mode
                            if self.backend_spec and self.backend_spec.backend_type == "claude-code"
                            else "bypassPermissions"
                        ),
                    }
            except Exception:
                pass

        # Codex CLI
        if _find_cli("codex"):
            try:
                import subprocess as _sp
                result = _sp.run(
                    [_find_cli("codex"), "--version"],
                    capture_output=True, text=True, timeout=5,
                    shell=(sys.platform == "win32"),
                )
                if result.returncode == 0:
                    available["codex"] = {
                        "models": list(CodexBackend.MODELS),
                        "model_labels": CodexBackend.MODEL_LABELS,
                    }
            except Exception:
                pass

        # Local MLX stack from LocalCodingModel repo
        mlx_root = os.environ.get("LOCAL_CODING_MODEL_ROOT", "/Users/richbellantoni/Repos/LocalCodingModel")
        mlx_python = Path(mlx_root) / ".venv" / "bin" / "python"
        mlx_adapter_root = Path(mlx_root) / "outputs" / "adapters"
        if mlx_python.exists() and mlx_adapter_root.exists():
            available["mlx"] = {
                "models": list(MLXBackend.MODELS),
                "local_root": mlx_root,
            }

        self.available_backends = available
        return available

    def select_harness_backend(
        self,
        *,
        session_role: str,
        project_path: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.available_backends:
            self.detect_backends()

        project_path = os.path.normpath(project_path or self.project.project_path)
        preferences = {
            "planner": ["claude-code", "codex", "openai", "claude", "mlx", "ollama", "lmstudio"],
            "generator": ["mlx", "codex", "claude-code", "openai", "claude", "ollama", "lmstudio"],
            "evaluator": ["claude-code", "codex", "claude", "openai", "mlx", "ollama", "lmstudio"],
        }
        preferred_mlx_model_by_role = {
            "planner": "fast-14b",
            "generator": "adapter-router",
            "evaluator": "fast-14b",
        }
        role_env = session_role.upper()
        forced_backend = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_MODEL", "") or "").strip()

        if forced_backend:
            info = self.available_backends.get(forced_backend)
            if not info:
                raise ValueError(
                    f"Forced harness backend '{forced_backend}' for role '{session_role}' is not available"
                )
            models = list(info.get("models") or [])
            preferred_mlx_model = preferred_mlx_model_by_role.get(session_role, "adapter-router")
            if forced_model:
                model = forced_model
            elif forced_backend == "mlx" and preferred_mlx_model in models:
                model = preferred_mlx_model
            else:
                model = models[0] if models else ""
            spec = self.build_backend_spec(forced_backend, model=model or None, project_path=project_path)
            return spec.backend_type, spec.model

        for backend_type in preferences.get(session_role, preferences["generator"]):
            info = self.available_backends.get(backend_type)
            if not info:
                continue
            models = list(info.get("models") or [])
            preferred_mlx_model = preferred_mlx_model_by_role.get(session_role, "adapter-router")
            if self.backend_spec and self.backend_spec.backend_type == backend_type and self.backend_spec.model:
                model = self.backend_spec.model
            elif backend_type == "mlx" and preferred_mlx_model in models:
                model = preferred_mlx_model
            else:
                model = models[0] if models else ""
            spec = self.build_backend_spec(backend_type, model=model or None, project_path=project_path)
            return spec.backend_type, spec.model

        raise ValueError(f"No available backend for harness role '{session_role}'")

    def select_harness_retry_backend(
        self,
        *,
        session_role: str,
        failed_backend: str = "",
        project_path: Optional[str] = None,
    ) -> tuple[str, str]:
        if not self.available_backends:
            self.detect_backends()

        project_path = os.path.normpath(project_path or self.project.project_path)
        role_env = session_role.upper()
        forced_backend = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_MODEL", "") or "").strip()

        if forced_backend:
            info = self.available_backends.get(forced_backend)
            if not info:
                raise ValueError(
                    f"Forced harness retry backend '{forced_backend}' for role '{session_role}' is not available"
                )
            models = list(info.get("models") or [])
            model = forced_model or (models[0] if models else "")
            spec = self.build_backend_spec(forced_backend, model=model or None, project_path=project_path)
            return spec.backend_type, spec.model

        retry_preferences = {
            "planner": ["codex", "claude-code", "openai", "claude", "mlx", "ollama", "lmstudio"],
            "generator": ["codex", "claude-code", "mlx", "openai", "claude", "ollama", "lmstudio"],
            "evaluator": ["claude-code", "claude", "codex", "openai", "mlx", "ollama", "lmstudio"],
        }
        preferred_mlx_model_by_role = {
            "planner": "fast-14b",
            "generator": "adapter-router",
            "evaluator": "fast-14b",
        }

        for backend_type in retry_preferences.get(session_role, retry_preferences["evaluator"]):
            if backend_type == failed_backend:
                continue
            info = self.available_backends.get(backend_type)
            if not info:
                continue
            models = list(info.get("models") or [])
            preferred_mlx_model = preferred_mlx_model_by_role.get(session_role, "adapter-router")
            if backend_type == "mlx" and preferred_mlx_model in models:
                model = preferred_mlx_model
            else:
                model = models[0] if models else ""
            spec = self.build_backend_spec(backend_type, model=model or None, project_path=project_path)
            return spec.backend_type, spec.model

        return "", ""

    def get_harness_role_timeout_seconds(self, session_role: str) -> float | None:
        role_env = session_role.upper()
        raw = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_TIMEOUT_SECONDS", "") or "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def get_harness_role_retry_timeout_seconds(self, session_role: str) -> float | None:
        role_env = session_role.upper()
        raw = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_TIMEOUT_SECONDS", "") or "").strip()
        if not raw:
            return self.get_harness_role_timeout_seconds(session_role)
        try:
            value = float(raw)
        except ValueError:
            return self.get_harness_role_timeout_seconds(session_role)
        return value if value > 0 else self.get_harness_role_timeout_seconds(session_role)

    def get_harness_role_max_tokens(self, session_role: str) -> int:
        role_env = session_role.upper()
        raw = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return int(self.HARNESS_ROLE_MAX_TOKENS.get(session_role, self.SESSION_MAX_TOKENS))

    def build_harness_role_session(
        self,
        *,
        project_path: Optional[str] = None,
        session_role: str,
        backend_type: Optional[str] = None,
        model: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        allowed_tools: Optional[list[dict[str, Any]]] = None,
        max_tokens_override: Optional[int] = None,
    ) -> tuple[Session, BackendSpec]:
        project_path = os.path.normpath(project_path or self.project.project_path)
        normalized_role = self.normalize_session_role("code", session_role)
        if not backend_type:
            backend_type, selected_model = self.select_harness_backend(
                session_role=normalized_role,
                project_path=project_path,
            )
            model = model or selected_model
        spec = self.build_backend_spec(backend_type, model=model or None, project_path=project_path)
        max_tokens = max_tokens_override or self.get_harness_role_max_tokens(normalized_role)
        backend = spec.create_backend(self.settings)
        session = self.build_session(
            backend=backend,
            backend_spec=spec,
            project_path=project_path,
            cancel_event=cancel_event,
            session_mode="code",
            session_role=normalized_role,
            max_tokens=max_tokens,
            allowed_tools=allowed_tools,
        )
        return session, spec

    def run_harness_role_once(
        self,
        *,
        project_path: str,
        session_role: str,
        prompt: str,
        backend_type: Optional[str] = None,
        model: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        role_cancel_event = threading.Event()
        timeout_stop = threading.Event()
        timed_out = False

        if cancel_event is not None:
            def _watch_external_cancel() -> None:
                cancel_event.wait()
                role_cancel_event.set()

            threading.Thread(target=_watch_external_cancel, daemon=True).start()

        if timeout_seconds and timeout_seconds > 0:
            def _watch_timeout() -> None:
                nonlocal timed_out
                if not timeout_stop.wait(timeout_seconds):
                    timed_out = True
                    role_cancel_event.set()

            threading.Thread(target=_watch_timeout, daemon=True).start()

        normalized_role = self.normalize_session_role("code", session_role)
        evaluation_mode = "full"
        if normalized_role == "evaluator":
            evaluation_mode = self.get_harness_evaluator_strategy(project_path)

        if normalized_role == "evaluator" and evaluation_mode in {"artifacts", "structured"}:
            prechecked_payload = self.precheck_harness_evaluator_payload(
                project_path=project_path,
                evaluation_mode=evaluation_mode,
            )
            if prechecked_payload is not None:
                self.apply_harness_update(
                    session_mode="code",
                    session_role=session_role,
                    payload=prechecked_payload,
                    project_path=project_path,
                    assistant_text="",
                    user_request=prompt,
                )
                return {
                    "result": "",
                    "error": "",
                    "steps": 0,
                    "display_events": [],
                    "backend_type": "precheck",
                    "model": "deterministic",
                    "timed_out": False,
                    "artifact_only": evaluation_mode == "artifacts",
                    "evaluation_mode": evaluation_mode,
                    "prechecked": True,
                }

        if evaluation_mode == "artifacts":
            effective_prompt = self.build_harness_evaluator_artifact_prompt(project_path)
            allowed_tools = []
            max_tokens_override = self.get_harness_evaluator_artifact_max_tokens()
        elif evaluation_mode == "structured":
            effective_prompt = self.build_harness_structured_evaluator_prompt(project_path)
            allowed_tools = []
            max_tokens_override = self.get_harness_evaluator_structured_max_tokens()
        else:
            effective_prompt = prompt
            allowed_tools = None
            max_tokens_override = None

        session, spec = self.build_harness_role_session(
            project_path=project_path,
            session_role=session_role,
            backend_type=backend_type,
            model=model,
            cancel_event=role_cancel_event,
            allowed_tools=allowed_tools,
            max_tokens_override=max_tokens_override,
        )
        collected_text: list[str] = []
        display_events: list[dict[str, Any]] = []
        steps = 0
        error = ""
        deferred_parse_error = ""
        pending_harness_payload: dict[str, Any] | None = None
        pending_harness_text = ""

        try:
            for event in session.run(effective_prompt):
                display_events.append(event)
                event_type = event.get("event", "")
                if event_type == EngineEvent.TEXT_DONE.value:
                    text = str(event.get("text") or "").strip()
                    cleaned_text, harness_payload, parse_error = self.extract_harness_update(
                        text=text,
                        session_mode="code",
                        session_role=session_role,
                    )
                    if parse_error:
                        if evaluation_mode in {"artifacts", "structured"}:
                            deferred_parse_error = parse_error
                        elif not error:
                            error = parse_error
                    if harness_payload is not None:
                        pending_harness_payload = harness_payload
                        pending_harness_text = cleaned_text
                    if cleaned_text:
                        collected_text.append(cleaned_text)
                elif event_type == EngineEvent.STEP_END.value:
                    steps += 1
                elif event_type == EngineEvent.ERROR.value:
                    message = str(event.get("message") or "Unknown error")
                    if role_cancel_event.is_set() and message == "Interrupted":
                        error = ""
                    elif message != "Interrupted":
                        error = message

                if role_cancel_event.is_set():
                    session.cancel()
        finally:
            timeout_stop.set()

        if not error and pending_harness_payload is None and evaluation_mode in {"artifacts", "structured"}:
            inferred_payload = self.infer_evidence_only_evaluator_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload

        if not error and pending_harness_payload is not None:
            try:
                self.apply_harness_update(
                    session_mode="code",
                    session_role=session_role,
                    payload=pending_harness_payload,
                    project_path=project_path,
                    assistant_text=pending_harness_text,
                    user_request=effective_prompt,
                )
            except Exception as exc:
                error = f"Failed to apply harness update: {exc}"
        elif not error and deferred_parse_error:
            error = deferred_parse_error
        elif not error:
            error = "No resonant-harness update emitted by automated role run"

        if timed_out:
            error = f"Timed out after {float(timeout_seconds):.1f}s"

        return {
            "result": "\n\n".join(collected_text).strip(),
            "error": error,
            "steps": steps,
            "display_events": display_events,
            "backend_type": spec.backend_type,
            "model": spec.model,
            "timed_out": timed_out,
            "artifact_only": evaluation_mode == "artifacts",
            "evaluation_mode": evaluation_mode,
            "prechecked": False,
        }

    def run_harness_teacher_escalation(
        self,
        *,
        project_path: str,
        failed_role: str,
        reason: str,
        objective: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        normalized_role = self.normalize_session_role("code", failed_role or "generator")
        provider, model = self.select_harness_teacher(
            session_role=normalized_role,
            reason=reason,
        )
        root = self.resolve_local_coding_model_root()
        python = self.resolve_local_coding_model_python()
        script_path = root / "scripts" / "collect_harness_teacher_response.py"
        if not script_path.exists():
            raise FileNotFoundError(f"Harness teacher collector not found: {script_path}")

        command = [
            str(python),
            str(script_path),
            "--provider",
            provider,
            "--model",
            model,
            "--project-path",
            target_path,
            "--reason",
            reason,
            "--failed-role",
            normalized_role,
        ]
        if objective.strip():
            command.extend(["--objective", objective.strip()])

        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()

        record: dict[str, Any] | None = None
        try:
            import subprocess as _sp

            result = _sp.run(
                command,
                capture_output=True,
                text=True,
                check=True,
            )
            stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not stdout_lines:
                raise ValueError("Harness teacher collector returned no JSON output")
            record = json.loads(stdout_lines[-1])
            response = record.get("response") or {}
            recommended_role = self.normalize_session_role(
                "code",
                str(response.get("recommended_role") or normalized_role),
            )
            assistant_markdown = str(response.get("assistant_response_markdown") or "").strip()
            if not assistant_markdown:
                raise ValueError("Harness teacher response is missing assistant_response_markdown")

            cleaned_text, harness_payload, parse_error = self.extract_harness_update(
                text=assistant_markdown,
                session_mode="code",
                session_role=recommended_role,
            )
            if parse_error:
                raise ValueError(parse_error)
            if harness_payload is None:
                raise ValueError("Harness teacher response did not emit a resonant-harness block")

            status_message = self.apply_harness_update(
                session_mode="code",
                session_role=recommended_role,
                payload=harness_payload,
                project_path=target_path,
                assistant_text=cleaned_text,
                user_request=str(record.get("recovery_request") or reason),
            )

            applied_record = {
                **record,
                "status": "applied",
                "recommended_role": recommended_role,
                "parsed_payload": harness_payload,
                "cleaned_assistant_text": cleaned_text,
                "status_message": status_message,
                "applied_at": time.time(),
            }
            harness.append_teacher_escalation(applied_record)
            harness.append_run_event(
                "teacher_intervention",
                {
                    "teacher_provider": provider,
                    "teacher_model": model,
                    "failed_role": normalized_role,
                    "reason": reason,
                    "recommended_role": recommended_role,
                    "status": "applied",
                    "recovery_kind": response.get("recovery_kind", ""),
                },
            )
            return {
                "result": cleaned_text,
                "error": "",
                "teacher_provider": provider,
                "teacher_model": model,
                "recommended_role": recommended_role,
                "status_message": status_message,
                "record": applied_record,
            }
        except Exception as exc:
            failure_record = {
                "record_type": "harness_teacher_response",
                "teacher_provider": provider,
                "teacher_model": model,
                "project_path": target_path,
                "failed_role": normalized_role,
                "reason": reason,
                "objective": objective.strip(),
                "status": "failed",
                "error": str(exc),
                "captured_record": record,
                "captured_at": time.time(),
            }
            harness.append_teacher_escalation(failure_record)
            harness.append_run_event(
                "teacher_intervention",
                {
                    "teacher_provider": provider,
                    "teacher_model": model,
                    "failed_role": normalized_role,
                    "reason": reason,
                    "status": "failed",
                    "error": str(exc),
                },
            )
            raise

    def run_scheduled_task(self, task) -> dict[str, Any]:
        if getattr(task, "task_kind", "session") != "harness_cycle":
            raise ValueError(f"Unsupported scheduled task kind: {getattr(task, 'task_kind', '')}")

        project_path = os.path.normpath(getattr(task, "project_path", "") or self.project.project_path)
        objective = str(getattr(task, "prompt", "") or "").strip()
        run = self.harness_orchestrator.start_cycle(
            project_path=project_path,
            name=f"[scheduled] {task.name}",
            objective=objective,
            max_loops=max(1, int(getattr(task, "max_loops", 6) or 6)),
        )
        return run.to_dict()

    def build_backend_spec(
        self,
        backend_type: str,
        model: str | None = None,
        project_path: str | None = None,
    ) -> BackendSpec:
        project_path = os.path.normpath(project_path or self.project.project_path)

        if (
            self.backend_spec and
            self.backend_spec.backend_type == backend_type and
            (not model or self.backend_spec.model == model)
        ):
            spec = BackendSpec.from_dict(self.backend_spec.to_dict(include_sensitive=True))
            if model:
                spec.model = model
            if backend_type in {"claude-code", "codex"}:
                spec.cwd = project_path
            return spec

        info = self.available_backends.get(backend_type)
        if not info:
            raise ValueError(f"Backend '{backend_type}' not available")

        models = info.get("models") or []
        selected_model = model or (models[0] if models else "")
        spec = BackendSpec(backend_type=backend_type, model=selected_model)

        if backend_type == "resonant":
            spec.url = info.get("url", "")
        elif backend_type == "ollama":
            spec.url = info.get("url", "")
        elif backend_type == "claude":
            spec.api_key_source = info.get("api_key_source", "")
            spec.api_key_env = info.get("api_key_env", "")
            spec.api_key_setting = info.get("api_key_setting", "")
        elif backend_type == "openai":
            spec.api_key_source = info.get("api_key_source", "")
            spec.api_key_env = info.get("api_key_env", "")
            spec.api_key_setting = info.get("api_key_setting", "")
        elif backend_type == "lmstudio":
            spec.base_url = info.get("url", "")
            spec.api_key = "lm-studio"
            spec.api_key_source = "literal"
        elif backend_type == "mlx":
            spec.local_root = info.get("local_root", "")
        elif backend_type == "claude-code":
            spec.cwd = project_path
            spec.permission_mode = info.get("permission_mode", "bypassPermissions")
        elif backend_type == "codex":
            spec.cwd = project_path

        return spec

    def build_session(
        self,
        backend=None,
        *,
        backend_spec: Optional[BackendSpec | dict[str, Any]] = None,
        project_path: Optional[str] = None,
        auto_approve: Optional[bool] = None,
        cancel_event: Optional[threading.Event] = None,
        session_mode: str = "code",
        session_role: str = "generator",
        max_tokens: Optional[int] = None,
        allowed_tools: Optional[list[dict[str, Any]]] = None,
    ) -> Session:
        project_path = os.path.normpath(project_path or self.project.project_path)
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)
        spec = (
            backend_spec if isinstance(backend_spec, BackendSpec)
            else BackendSpec.from_dict(backend_spec)
            if backend_spec else None
        )
        backend = backend or (spec.create_backend(self.settings) if spec else None)
        if backend is None:
            raise ValueError("Backend or backend spec is required to build a session")

        if self._normalize_path(project_path) == self._normalize_path(self.project.project_path):
            project_instructions = self._project_instructions or load_project_instructions(project_path)
            engram = self.engram
            codebase_index = self.codebase_index
        else:
            project_instructions = load_project_instructions(project_path)
            engram = self.base_engram.clone(namespace=self._project_namespace(project_path))
            engram.set_mcp_manager(self.mcp_manager)
            codebase_index = CodebaseIndex(project_path, engram=engram)

        harness_instructions = self.build_harness_instructions(
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
        )
        if harness_instructions:
            project_instructions = (project_instructions or "").strip()
            project_instructions = (
                f"{project_instructions}\n{harness_instructions}".strip()
                if project_instructions
                else harness_instructions
            )

        session = Session(
            backend=backend,
            max_steps=self.SESSION_MAX_STEPS,
            max_tokens=max_tokens or self.SESSION_MAX_TOKENS,
            auto_approve=self._session_auto_approve() if auto_approve is None else auto_approve,
            allowed_tools=allowed_tools,
            project_instructions=project_instructions,
            cancel_event=cancel_event,
        )
        return self._wire_session(
            session,
            project_path=project_path,
            project_instructions=project_instructions,
            engram=engram,
            codebase_index=codebase_index,
        )

    def create_backend(
        self,
        backend_type: str,
        model: str = None,
        *,
        session_mode: str = "code",
        session_role: str = "generator",
    ):
        """Create a backend and session."""
        spec = self.build_backend_spec(backend_type, model=model, project_path=self.project.project_path)
        self.backend = spec.create_backend(self.settings)
        self.backend_spec = spec
        self._project_instructions = load_project_instructions(self.project.project_path)
        self.session = self.build_session(
            backend=self.backend,
            backend_spec=spec,
            project_path=self.project.project_path,
            session_mode=session_mode,
            session_role=session_role,
        )
        self.apply_permission_mode(self.permission_mode, session=self.session)
        return self.backend

    def apply_settings(self, section: str = "", key: str | None = None):
        if section == "mcp_servers":
            self.mcp_manager.disconnect_all()

        self.hook_runner.reload()
        self.base_engram.reload()
        self.base_engram.set_mcp_manager(self.mcp_manager)
        self.apply_project_context(self.project.project_path, refresh_index=True)
        self.detect_backends()

        if section == "general" and key == "default_permission_mode":
            configured_mode = str(
                self.settings.get("general", "default_permission_mode", self.permission_mode) or "bypass"
            )
            self.apply_permission_mode(configured_mode, session=self.session)
        elif self.session:
            self.apply_permission_mode(self.permission_mode, session=self.session)

        if (
            self.backend_spec and
            self.backend_spec.backend_type in {"claude", "openai", "lmstudio", "mlx"} and
            section in {"api_keys", "engram", "general"}
        ):
            try:
                self.backend = self.backend_spec.create_backend(self.settings)
                if self.session:
                    self.session.backend = self.backend
            except Exception:
                logger.warning("Failed to refresh current backend after settings update", exc_info=True)

        return self.settings.get_masked()

    def update_setting_value(
        self,
        section: str,
        key: str | None,
        value: Any,
        *,
        clear_secret: bool = False,
    ) -> dict:
        if section:
            if section == "api_keys" and key:
                if clear_secret:
                    self.settings.set(section, key, "")
                elif value not in ("", None):
                    self.settings.set(section, key, value)
            elif key:
                self.settings.set(section, key, value)
            else:
                self.settings.update_section(section, value or {})

        return self.apply_settings(section, key)

    def make_background_session(self, task) -> Session:
        project_path = os.path.normpath(task.project_path or self.project.project_path)
        spec = (
            BackendSpec.from_dict(task.backend_spec)
            if task.backend_spec else
            self.build_backend_spec(task.backend_type, model=task.model, project_path=project_path)
        )
        backend = spec.create_backend(self.settings)
        return self.build_session(
            backend=backend,
            backend_spec=spec,
            project_path=project_path,
            cancel_event=task.cancel_event,
            session_mode=self.normalize_session_mode(getattr(task, "session_mode", "code")),
            session_role=self.normalize_session_role(
                getattr(task, "session_mode", "code"),
                getattr(task, "session_role", "generator"),
            ),
        )

    def get_init_data(self, refresh_only: bool = False) -> dict:
        """Get initial state for the frontend."""
        backends_info = {}
        for key, info in self.available_backends.items():
            entry = {"name": key}
            if "models" in info:
                entry["models"] = info["models"]
            if "model_labels" in info:
                entry["model_labels"] = info["model_labels"]
            if "health" in info:
                entry["patterns"] = info["health"].get("memory_patterns", 0)
            backends_info[key] = entry

        current_backend = ""
        current_model = ""
        handles_tools = False
        if self.backend:
            current_backend = getattr(self.backend, "name", "")
            current_model = getattr(self.backend, "model", "")
            handles_tools = getattr(self.backend, "handles_tools", False)

        return {
            "event": "init",
            "refresh_only": refresh_only,
            "backends": backends_info,
            "current_backend": current_backend,
            "current_model": current_model,
            "handles_tools": handles_tools,
            "permission_mode": self.permission_mode,
            "cwd": self.project.project_path.replace("\\", "/"),
            "sessions": self.project.list_sessions(),
            "all_sessions": self.project.list_all_sessions(),
            "current_session_id": self.project.current_session.id if self.project.current_session else "",
            "current_session_mode": self.project.current_session.session_mode if self.project.current_session else "code",
            "current_session_role": self.project.current_session.session_role if self.project.current_session else "generator",
            "recent_projects": self.project.get_recent_projects(),
            "settings": self.settings.get_masked(),
            "resonant_md": get_instruction_info(self.project.project_path),
            "rag": self.codebase_index.get_stats() if self.codebase_index else {"total_files": 0, "is_indexed": False},
            "chat_groups": self.project.list_chat_groups(),
            "harness": self.get_harness_summary(self.project.project_path),
            "harness_cycles": self.harness_orchestrator.list_runs(),
        }


state = AppState()


# ── WebSocket Handler ─────────────────────────────────────────────────

async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket handler — bidirectional communication with frontend."""
    await ws.accept()

    # Store WebSocket ref for live agent event streaming
    state._ws_ref = ws
    state._ws_loop = asyncio.get_event_loop()

    # Initialize if needed
    if not state.available_backends:
        state.api_url = os.environ.get("RESONANT_API", "http://localhost:8000").rstrip("/")
        state.ollama_url = os.environ.get(
            "OLLAMA_URL",
            os.environ.get("OLLAMA_HOST", "http://10.0.0.133:11434"),
        ).rstrip("/")
        state.lmstudio_url = os.environ.get("LMSTUDIO_URL", "").rstrip("/")
        state.project._save_recent_project()
        # Send sessions immediately so sidebar populates while backends are detected
        await ws.send_json({
            "event": "sessions_updated",
            "sessions": state.project.list_sessions(),
            "current_session_id": state.project.current_session.id if state.project.current_session else "",
        })
        await asyncio.get_event_loop().run_in_executor(None, state.detect_backends)

    # Initialize codebase index if not already set
    if not state.codebase_index and state.project:
        state.codebase_index = CodebaseIndex(state.project.project_path, engram=state.engram)

    # Start scheduler daemon (once)
    if not state._scheduler_started:
        state.scheduler.start()
        state._scheduler_started = True

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            command = msg.get("command", "")

            if command == "init":
                await ws.send_json(state.get_init_data())

            elif command == "get_harness_state":
                await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})

            elif command == "get_harness_resume_prompt":
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                prompt = state.build_harness_resume_prompt(
                    session_mode=session_mode,
                    session_role=session_role,
                    project_path=state.project.project_path,
                )
                await ws.send_json({
                    "event": "resume_prompt",
                    "session_mode": state.normalize_session_mode(session_mode),
                    "session_role": state.normalize_session_role(session_mode, session_role),
                    "prompt": prompt,
                })

            elif command == "harness_cycle_start":
                max_loops = int(msg.get("max_loops") or 6)
                name = (msg.get("name") or "").strip()
                objective = (msg.get("objective") or "").strip()
                run = state.harness_orchestrator.start_cycle(
                    project_path=state.project.project_path,
                    name=name or "Harness Cycle",
                    objective=objective,
                    max_loops=max_loops,
                )
                await ws.send_json({"event": "harness_cycle_started", "run": run.to_dict()})
                await ws.send_json({"event": "harness_cycle_list", "runs": state.harness_orchestrator.list_runs()})

            elif command == "harness_cycle_list":
                await ws.send_json({"event": "harness_cycle_list", "runs": state.harness_orchestrator.list_runs()})

            elif command == "harness_cycle_result":
                run_id = (msg.get("run_id") or "").strip()
                run = state.harness_orchestrator.get_run(run_id)
                if run:
                    await ws.send_json({"event": "harness_cycle_result", "run": run.to_full_dict()})
                else:
                    await ws.send_json({"event": "error", "message": f"Harness cycle {run_id} not found"})

            elif command == "harness_cycle_cancel":
                run_id = (msg.get("run_id") or "").strip()
                cancelled = state.harness_orchestrator.cancel(run_id)
                await ws.send_json({"event": "harness_cycle_cancelled", "run_id": run_id, "success": cancelled})
                await ws.send_json({"event": "harness_cycle_list", "runs": state.harness_orchestrator.list_runs()})

            elif command == "harness_teacher_recover":
                reason = (msg.get("reason") or "").strip() or "manual_recovery"
                failed_role = state.normalize_session_role(
                    "code",
                    (msg.get("failed_role") or state.get_harness_summary().get("active_role") or "generator"),
                )
                objective = (msg.get("objective") or "").strip()
                try:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.run_harness_teacher_escalation(
                            project_path=state.project.project_path,
                            failed_role=failed_role,
                            reason=reason,
                            objective=objective,
                        ),
                    )
                    await ws.send_json(
                        {
                            "event": "harness_teacher_recovered",
                            "data": {
                                "teacher_provider": result.get("teacher_provider", ""),
                                "teacher_model": result.get("teacher_model", ""),
                                "recommended_role": result.get("recommended_role", ""),
                                "status_message": result.get("status_message", ""),
                            },
                        }
                    )
                    await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})
                except Exception as exc:
                    await ws.send_json({"event": "error", "message": f"Teacher recovery failed: {exc}"})

            elif command == "set_harness_sprint":
                sprint_id = (msg.get("sprint_id") or "").strip()
                feature_name = (msg.get("feature_name") or "").strip()
                objective = (msg.get("objective") or "").strip()
                if not sprint_id or not objective:
                    await ws.send_json({"event": "error", "message": "sprint_id and objective are required"})
                    continue
                state.harness.set_active_sprint(
                    sprint_id=sprint_id,
                    feature_name=feature_name,
                    objective=objective,
                    deliverables=list(msg.get("deliverables") or []),
                    acceptance_checks=list(msg.get("acceptance_checks") or []),
                    evaluator_focus=list(msg.get("evaluator_focus") or []),
                    status=(msg.get("status") or "proposed"),
                    role=state.normalize_session_role("code", msg.get("session_role", "planner")),
                )
                await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})
                await ws.send_json({"event": "status_msg", "message": f"Updated sprint {sprint_id}"})

            elif command == "set_harness_contract_status":
                status_value = (msg.get("status") or "").strip()
                if status_value not in {"proposed", "approved", "implemented", "needs_revision", "passed", "failed"}:
                    await ws.send_json({"event": "error", "message": "valid contract status is required"})
                    continue
                state.harness.set_contract_status(
                    status=status_value,
                    role=state.normalize_session_role("code", msg.get("session_role", "planner")),
                )
                await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})
                await ws.send_json({"event": "status_msg", "message": f"Set sprint contract to {status_value}"})

            elif command == "set_evaluator_verdict":
                sprint_id = (msg.get("sprint_id") or "").strip()
                verdict = (msg.get("verdict") or "").strip()
                if not sprint_id or verdict not in {"pass", "revise", "blocked"}:
                    await ws.send_json({"event": "error", "message": "valid sprint_id and verdict are required"})
                    continue
                state.harness.record_evaluator_verdict(
                    sprint_id=sprint_id,
                    verdict=verdict,
                    findings=list(msg.get("findings") or []),
                    required_revisions=list(msg.get("required_revisions") or []),
                    passed_checks=list(msg.get("passed_checks") or []),
                    failed_checks=list(msg.get("failed_checks") or []),
                    score=msg.get("score"),
                )
                await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})
                await ws.send_json({"event": "status_msg", "message": f"Evaluator marked sprint {sprint_id} as {verdict}"})

            elif command == "select_backend":
                backend_type = msg.get("backend", "")
                model = msg.get("model", "")
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.create_backend(
                            backend_type,
                            model or None,
                            session_mode=session_mode,
                            session_role=session_role,
                        ),
                    )
                    # Auto-create a new session when backend is selected
                    state.project.create_session(
                        backend_type=backend_type,
                        model=model or getattr(state.backend, "model", ""),
                        session_mode=session_mode,
                        session_role=session_role,
                    )
                    state._first_message_sent = False
                    await ws.send_json(state.get_init_data())
                    await ws.send_json({"event": "status_msg", "message": f"Connected to {backend_type}"})
                except Exception as e:
                    await ws.send_json({"event": "error", "message": str(e)})

            elif command == "message":
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if not state.session:
                    await ws.send_json({"event": "error", "message": "No backend selected"})
                    continue
                if state.active_thread and state.active_thread.is_alive():
                    await ws.send_json({"event": "error", "message": "Already running"})
                    continue

                # Auto-title session from first message
                if not state._first_message_sent:
                    state.project.update_session_title(text)

                # Parse attached images (base64 from frontend paste/upload)
                images = None
                raw_images = msg.get("images", [])
                if raw_images:
                    import base64 as _b64
                    images = []
                    for img in raw_images:
                        data = img.get("data", "")
                        media_type = img.get("media_type", "image/png")
                        try:
                            images.append((_b64.b64decode(data), media_type))
                        except Exception:
                            pass

                session_mode = (
                    state.project.current_session.session_mode
                    if state.project.current_session else "code"
                )
                session_role = (
                    state.project.current_session.session_role
                    if state.project.current_session else "generator"
                )
                if session_mode == "code" and session_role == "generator" and not state._first_message_sent:
                    harness_summary = state.get_harness_summary(state.project.project_path)
                    if (
                        not harness_summary.get("active_sprint_id")
                        or harness_summary.get("contract_status") not in {"approved", "needs_revision"}
                    ):
                        await ws.send_json({
                            "event": "error",
                            "message": "Generator session requires an approved or needs_revision sprint contract",
                        })
                        continue
                text_for_session = text
                if not state._first_message_sent:
                    text_for_session = state.wrap_user_message_for_harness(
                        user_msg=text,
                        session_mode=session_mode,
                        session_role=session_role,
                    )
                    state._first_message_sent = True

                state.cancel_requested.clear()
                state.session.reset_cancel()
                display_events = await _run_session_streaming(
                    ws,
                    state.session,
                    text_for_session,
                    images=images,
                    display_user_msg=text,
                    session_mode=session_mode,
                    session_role=session_role,
                )

                # Save session after each message exchange (with display events for replay)
                state.project.save_current_session(state.session, display_events=display_events)
                # Send updated session list
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "cancel":
                state.cancel_requested.set()
                if state.session:
                    state.session.cancel()
                await ws.send_json({"event": "status_msg", "message": "Cancelling..."})

            elif command == "clear":
                # Create a new session (don't destroy old one)
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                if state.backend:
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    state.session = state.build_session(
                        backend=state.backend,
                        backend_spec=state.backend_spec,
                        project_path=state.project.project_path,
                        session_mode=session_mode,
                        session_role=session_role,
                    )
                    state.project.create_session(
                        backend_type=backend_type,
                        model=model,
                        session_mode=session_mode,
                        session_role=session_role,
                    )
                    state._first_message_sent = False
                    state.costs.reset_session()
                await ws.send_json({
                    "event": "session_cleared",
                    "sessions": state.project.list_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                    "session_mode": session_mode,
                    "session_role": session_role,
                })

            elif command == "switch_model":
                model = msg.get("model", "")
                backend_type = msg.get("backend", "")
                if not backend_type and state.backend and hasattr(state.backend, "name"):
                    backend_type = getattr(state.backend, "name", "")
                if backend_type:
                    try:
                        session_mode = (
                            state.project.current_session.session_mode
                            if state.project.current_session else "code"
                        )
                        session_role = (
                            state.project.current_session.session_role
                            if state.project.current_session else "generator"
                        )
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: state.create_backend(
                                backend_type,
                                model,
                                session_mode=session_mode,
                                session_role=session_role,
                            ),
                        )
                        await ws.send_json(state.get_init_data())
                    except Exception as e:
                        await ws.send_json({"event": "error", "message": str(e)})

            elif command == "set_permission_mode":
                mode = msg.get("mode", "bypass")
                state.apply_permission_mode(mode)

            elif command == "switch_session":
                session_id = msg.get("session_id", "")
                # If session is from a different project, switch project first
                project_path = msg.get("project_path", "")
                if project_path and state._normalize_path(project_path) != state._normalize_path(state.project.project_path):
                    if os.path.isdir(project_path):
                        state.apply_project_context(project_path, refresh_index=True)
                        state.backend = None
                        state.backend_spec = None
                        state.session = None
                        state._first_message_sent = False
                        await asyncio.get_event_loop().run_in_executor(None, state.detect_backends)
                record = state.project.load_session(session_id)
                if record:
                    # Recreate backend + session with saved conversation history
                    try:
                        backend_type = record.backend_type
                        model = record.model
                        if backend_type:
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: state.create_backend(
                                    backend_type,
                                    model or None,
                                    session_mode=record.session_mode or "code",
                                    session_role=record.session_role or "generator",
                                ),
                            )
                        else:
                            state.backend = None
                            state.backend_spec = None
                            state.session = None
                        if state.session and record.conversation_history:
                            state.session.conversation_history = record.conversation_history
                        state._first_message_sent = record.message_count > 0

                        # Send session_loaded with display events for replay
                        await ws.send_json({
                            "event": "session_loaded",
                            "session_id": record.id,
                            "title": record.title,
                            "backend_type": record.backend_type,
                            "model": record.model,
                            "message_count": record.message_count,
                            "session_mode": record.session_mode or "code",
                            "session_role": record.session_role or "generator",
                            "display_events": record.display_events,
                            "sessions": state.project.list_sessions(),
                            "current_session_id": record.id,
                        })
                        # Send lightweight init refresh (skip re-detecting backends)
                        await ws.send_json(state.get_init_data(refresh_only=True))
                    except Exception as e:
                        await ws.send_json({"event": "error", "message": str(e)})
                else:
                    await ws.send_json({"event": "error", "message": f"Session {session_id} not found"})

            elif command == "delete_session":
                session_id = msg.get("session_id", "")
                state.project.delete_session(session_id)
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "rename_session":
                session_id = msg.get("session_id", "")
                new_title = msg.get("title", "").strip()
                if session_id and new_title:
                    record = state.project.load_session(session_id)
                    if record:
                        record.title = new_title
                        record.save()
                        # Restore current session pointer if it changed
                        if state.project.current_session and state.project.current_session.id != session_id:
                            state.project.load_session(state.project.current_session.id)
                    await ws.send_json({
                        "event": "sessions_updated",
                        "sessions": state.project.list_sessions(),
                        "all_sessions": state.project.list_all_sessions(),
                        "current_session_id": state.project.current_session.id if state.project.current_session else "",
                    })

            # ── Chat Group Commands ───────────────────────────
            elif command == "create_chat_group":
                name = msg.get("name", "").strip()
                groups = state.project.create_chat_group(name)
                await ws.send_json({"event": "chat_groups", "groups": groups})

            elif command == "rename_chat_group":
                old_name = msg.get("old_name", "")
                new_name = msg.get("new_name", "").strip()
                groups = state.project.rename_chat_group(old_name, new_name)
                await ws.send_json({"event": "chat_groups", "groups": groups})
                # Sessions may have changed group names
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "delete_chat_group":
                name = msg.get("name", "")
                groups = state.project.delete_chat_group(name)
                await ws.send_json({"event": "chat_groups", "groups": groups})
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "set_session_group":
                session_id = msg.get("session_id", "")
                group = msg.get("group", "")
                state.project.set_session_group(session_id, group)
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "set_project":
                project_path = msg.get("path", "").strip()
                if project_path and os.path.isdir(project_path):
                    state.apply_project_context(project_path, refresh_index=True)
                    # Reset backend + session
                    state.backend = None
                    state.backend_spec = None
                    state.session = None
                    state._first_message_sent = False
                    state.costs.reset_session()
                    # Re-detect backends
                    await asyncio.get_event_loop().run_in_executor(None, state.detect_backends)
                    await ws.send_json(state.get_init_data())
                else:
                    await ws.send_json({"event": "error", "message": f"Invalid directory: {project_path}"})

            elif command == "folder_dialog":
                # Open native folder picker via pywebview (or tkinter fallback)
                def _pick_folder():
                    global _webview_window
                    if _webview_window:
                        try:
                            import webview
                            result = _webview_window.create_file_dialog(
                                webview.FOLDER_DIALOG,
                            )
                            if result and len(result) > 0:
                                return result[0]
                        except Exception as e:
                            logger.warning(f"pywebview folder dialog failed: {e}")
                        # Don't fall through to tkinter when pywebview is active —
                        # if the dialog failed it's likely because the window is closing.
                        return None
                    # Fallback: tkinter (only when pywebview is not available)
                    try:
                        import tkinter as tk
                        from tkinter import filedialog
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes('-topmost', True)
                        folder = filedialog.askdirectory(title="Select Project Folder")
                        root.destroy()
                        return folder or None
                    except Exception as e:
                        logger.warning(f"tkinter folder dialog failed: {e}")
                        return None

                picked = await asyncio.get_event_loop().run_in_executor(None, _pick_folder)
                if picked:
                    await ws.send_json({"event": "folder_picked", "path": picked})

            elif command == "list_dirs":
                # List subdirectories for folder browsing
                parent = msg.get("path", "").strip()
                try:
                    if not parent:
                        # List drives on Windows, root on Unix
                        if os.name == "nt":
                            import string
                            dirs = [f"{d}:\\" for d in string.ascii_uppercase
                                    if os.path.exists(f"{d}:\\")]
                        else:
                            dirs = ["/"]
                    else:
                        p = Path(parent)
                        dirs = sorted([
                            str(d) for d in p.iterdir()
                            if d.is_dir() and not d.name.startswith(".")
                        ][:50])
                    await ws.send_json({"event": "dir_list", "path": parent, "dirs": dirs})
                except Exception as e:
                    await ws.send_json({"event": "dir_list", "path": parent, "dirs": [], "error": str(e)})

            elif command == "approve":
                state.permission_result[0] = msg.get("approved", True)
                state.permission_response.set()

            elif command == "choice_select":
                state.choice_result[0] = msg.get("selected", "")
                state.choice_response.set()

            # ── Settings ────────────────────────────────────
            elif command == "get_settings":
                await ws.send_json({"event": "settings", "data": state.settings.get_masked()})

            elif command == "update_settings":
                section = msg.get("section", "")
                key = msg.get("key")
                value = msg.get("value")
                clear_secret = bool(msg.get("clear_secret", False))
                data = state.update_setting_value(
                    section,
                    key,
                    value,
                    clear_secret=clear_secret,
                )
                await ws.send_json({"event": "settings", "data": data})
                await ws.send_json(state.get_init_data(refresh_only=True))

            # ── Cost Tracking ───────────────────────────────
            elif command == "get_costs":
                await ws.send_json({"event": "costs", "data": state.costs.get_all_costs()})

            # ── Dispatch (Background Tasks) ──────────────────
            elif command == "dispatch":
                prompt = msg.get("prompt", "").strip()
                name = msg.get("name", "").strip()
                backend_type = msg.get("backend", getattr(state.backend, "name", ""))
                model = msg.get("model", getattr(state.backend, "model", ""))
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                if not prompt:
                    await ws.send_json({"event": "error", "message": "No prompt provided"})
                    continue
                try:
                    spec = state.build_backend_spec(
                        backend_type,
                        model=model or None,
                        project_path=state.project.project_path,
                    )
                    task = state.task_runner.submit(
                        name=name,
                        prompt=prompt,
                        session_factory=state.make_background_session,
                        backend_type=backend_type,
                        model=spec.model,
                        project_path=state.project.project_path,
                        session_mode=state.normalize_session_mode(session_mode),
                        session_role=state.normalize_session_role(session_mode, session_role),
                        backend_spec=spec.to_dict(),
                    )
                    await ws.send_json({
                        "event": "dispatch_submitted",
                        "task": task.to_dict(),
                    })
                except Exception as e:
                    await ws.send_json({"event": "error", "message": str(e)})

            elif command == "dispatch_list":
                tasks = state.task_runner.list_tasks()
                await ws.send_json({"event": "dispatch_list", "tasks": tasks})

            elif command == "dispatch_result":
                task_id = msg.get("task_id", "")
                task = state.task_runner.get_task(task_id)
                if task:
                    await ws.send_json({"event": "dispatch_result", "task": task.to_full_dict()})
                else:
                    await ws.send_json({"event": "error", "message": f"Task {task_id} not found"})

            elif command == "dispatch_cancel":
                task_id = msg.get("task_id", "")
                cancelled = state.task_runner.cancel(task_id)
                await ws.send_json({"event": "dispatch_cancelled", "task_id": task_id, "success": cancelled})
                # Refresh list
                tasks = state.task_runner.list_tasks()
                await ws.send_json({"event": "dispatch_list", "tasks": tasks})

            # ── Command Center: Projects ────────────────────
            elif command == "command_project_list":
                projects = state.command_project_store.list_projects()
                await ws.send_json({"event": "command_project_list", "projects": projects})

            elif command == "command_project_create":
                name = msg.get("name", "").strip()
                path = msg.get("path", "").strip() or state.project.project_path
                strategy = msg.get("strategy", "").strip()
                if not strategy:
                    await ws.send_json({"event": "error", "message": "Strategy is required"})
                    continue
                proj = state.command_project_store.create_project(
                    name=name or path.replace("\\", "/").split("/")[-1],
                    path=path,
                    strategy=strategy,
                )
                proj.status = "planning"
                proj.add_activity("system", "System", f"Project created. Strategy: {strategy[:100]}...")
                state.command_project_store._persist(proj)

                # Spawn coordinator agent
                try:
                    coordinator_prompt = (
                        f"You are a project coordinator for the project at: {path}\n\n"
                        f"## Strategy\n{strategy}\n\n"
                        f"## Your Job\n"
                        f"1. Analyze the codebase at the project path\n"
                        f"2. Break the strategy into concrete, ordered tasks\n"
                        f"3. For each task, describe what needs to be done clearly\n"
                        f"4. Execute each task yourself, one at a time\n"
                        f"5. After completing each task, summarize what was done\n\n"
                        f"Work through all tasks methodically. Be thorough but efficient."
                    )
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    spec = state.build_backend_spec(backend_type, model=model or None, project_path=path)

                    def _make_coordinator_event_handler(pid):
                        def handler(task_id, event):
                            state._push_agent_event(task_id, event)
                            # Also post text completions to project activity
                            if event.get("event") == "text.done":
                                text = event.get("text", "")[:200]
                                if text.strip():
                                    state.command_project_store.add_activity(
                                        pid, "agent", "Coordinator", text
                                    )
                        return handler

                    bg_task = state.task_runner.submit(
                        name=f"Coordinator: {name}",
                        prompt=coordinator_prompt,
                        session_factory=state.make_background_session,
                        backend_type=backend_type,
                        model=spec.model,
                        project_path=path,
                        session_mode="code",
                        session_role="generator",
                        backend_spec=spec.to_dict(),
                        on_event=_make_coordinator_event_handler(proj.id),
                    )
                    state.command_project_store.update_project(
                        proj.id,
                        coordinator_task_id=bg_task.id,
                        status="running",
                        agents=[{
                            "id": bg_task.id, "name": f"Coordinator: {name}",
                            "role": "coordinator", "status": "running",
                            "model": spec.model, "steps": 0, "elapsed": 0,
                        }],
                    )
                    proj = state.command_project_store.get_project(proj.id)
                except Exception as e:
                    state.command_project_store.update_project(proj.id, status="failed")
                    proj = state.command_project_store.get_project(proj.id)
                    proj.add_activity("system", "System", f"Failed to launch coordinator: {e}")
                    state.command_project_store._persist(proj)

                await ws.send_json({
                    "event": "command_project_created",
                    "project": proj.to_dict(),
                    "projects": state.command_project_store.list_projects(),
                })

            elif command == "command_project_status":
                project_id = msg.get("project_id", "")
                proj = state.command_project_store.get_project(project_id)
                if proj:
                    # Refresh agent status from task runner
                    for agent in proj.agents:
                        bg = state.task_runner.get_task(agent.get("id", ""))
                        if bg:
                            agent["status"] = bg.status.value
                            agent["steps"] = bg.steps
                            agent["elapsed"] = round(bg.elapsed, 2)
                    # Check if coordinator finished
                    if proj.coordinator_task_id:
                        coord = state.task_runner.get_task(proj.coordinator_task_id)
                        if coord and coord.status.value in ("completed", "failed", "cancelled"):
                            if proj.status == "running":
                                new_status = "completed" if coord.status.value == "completed" else "failed"
                                state.command_project_store.update_project(proj.id, status=new_status)
                                proj.status = new_status
                    state.command_project_store._persist(proj)
                    await ws.send_json({"event": "command_project_status", "project": proj.to_dict()})
                else:
                    await ws.send_json({"event": "error", "message": f"Project {project_id} not found"})

            # ── Command Center: Fleet ───────────────────────
            elif command == "command_fleet":
                tasks = state.task_runner.list_tasks(limit=100)
                # Also include harness cycle runs if available
                cycles = []
                if hasattr(state, "harness_orchestrator") and state.harness_orchestrator:
                    try:
                        cycles = state.harness_orchestrator.list_runs()
                    except Exception:
                        pass
                # Merge into a unified agent list
                agents = []
                for t in tasks:
                    agents.append({
                        "id": t.get("id", ""),
                        "name": t.get("name", ""),
                        "prompt": t.get("prompt", ""),
                        "status": t.get("status", ""),
                        "model": t.get("model", ""),
                        "steps": t.get("steps", 0),
                        "elapsed": t.get("elapsed", 0),
                        "created_at": t.get("created_at", ""),
                        "source": "dispatch",
                    })
                for c in cycles:
                    agents.append({
                        "id": c.get("id", ""),
                        "name": c.get("name", "harness cycle"),
                        "prompt": "",
                        "status": c.get("status", ""),
                        "model": "",
                        "steps": len(c.get("steps", [])),
                        "elapsed": 0,
                        "created_at": c.get("started_at", ""),
                        "source": "harness",
                    })
                # Sort: running first, then by created_at descending
                status_order = {"running": 0, "pending": 1, "completed": 2, "failed": 3, "cancelled": 4}
                agents.sort(key=lambda a: (status_order.get(a["status"], 9), a.get("created_at", "")), reverse=False)
                await ws.send_json({"event": "command_fleet", "agents": agents})

            elif command == "command_spawn":
                prompt = msg.get("prompt", "").strip()
                name = msg.get("name", "").strip()
                session_role = msg.get("session_role", "generator")
                if not prompt:
                    await ws.send_json({"event": "error", "message": "No prompt provided"})
                    continue
                try:
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    spec = state.build_backend_spec(
                        backend_type,
                        model=model or None,
                        project_path=state.project.project_path,
                    )
                    task = state.task_runner.submit(
                        name=name or prompt[:50],
                        prompt=prompt,
                        session_factory=state.make_background_session,
                        backend_type=backend_type,
                        model=spec.model,
                        project_path=state.project.project_path,
                        session_mode="code",
                        session_role=state.normalize_session_role("code", session_role),
                        backend_spec=spec.to_dict(),
                        on_event=state._push_agent_event,
                    )
                    await ws.send_json({"event": "command_spawn_ok", "task": task.to_dict()})
                except Exception as e:
                    await ws.send_json({"event": "error", "message": f"Failed to spawn agent: {e}"})

            # ── Command Center: Task Board ──────────────────
            elif command == "command_task_list":
                tasks = state.command_task_store.list_tasks()
                await ws.send_json({"event": "command_task_list", "tasks": tasks})

            elif command == "command_task_create":
                title = msg.get("title", "").strip()
                if not title:
                    await ws.send_json({"event": "error", "message": "Task title required"})
                    continue
                task = state.command_task_store.create_task(
                    title=title,
                    description=msg.get("description", ""),
                    priority=msg.get("priority", "medium"),
                    tags=msg.get("tags", []),
                )
                await ws.send_json({"event": "command_task_created", "task": task.to_dict()})
                await ws.send_json({"event": "command_task_list", "tasks": state.command_task_store.list_tasks()})

            elif command == "command_task_update":
                task_id = msg.get("id", "")
                updates = {k: v for k, v in msg.items() if k not in ("command", "id")}
                task = state.command_task_store.update_task(task_id, **updates)
                if task:
                    await ws.send_json({"event": "command_task_list", "tasks": state.command_task_store.list_tasks()})
                else:
                    await ws.send_json({"event": "error", "message": f"Task {task_id} not found"})

            elif command == "command_task_delete":
                task_id = msg.get("id", "")
                state.command_task_store.delete_task(task_id)
                await ws.send_json({"event": "command_task_list", "tasks": state.command_task_store.list_tasks()})

            elif command == "command_task_assign":
                task_id = msg.get("task_id", "")
                ct = state.command_task_store.get_task(task_id)
                if not ct:
                    await ws.send_json({"event": "error", "message": f"Task {task_id} not found"})
                    continue
                try:
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    spec = state.build_backend_spec(backend_type, model=model or None, project_path=state.project.project_path)
                    bg_task = state.task_runner.submit(
                        name=ct.title,
                        prompt=ct.description or ct.title,
                        session_factory=state.make_background_session,
                        backend_type=backend_type,
                        model=spec.model,
                        project_path=state.project.project_path,
                        session_mode="code",
                        session_role="generator",
                        backend_spec=spec.to_dict(),
                        on_event=state._push_agent_event,
                    )
                    state.command_task_store.update_task(task_id, status="running", assigned_agent_id=bg_task.id)
                    await ws.send_json({"event": "command_task_list", "tasks": state.command_task_store.list_tasks()})
                    await ws.send_json({"event": "command_spawn_ok", "task": bg_task.to_dict()})
                except Exception as e:
                    await ws.send_json({"event": "error", "message": f"Failed to assign task: {e}"})

            # ── Command Center: Monitor ──────────────────────
            elif command == "command_monitor_subscribe":
                task_id = msg.get("task_id", "")
                state._monitored_task_ids.add(task_id)
                # Send existing events for catch-up
                bg_task = state.task_runner.get_task(task_id)
                if bg_task:
                    await ws.send_json({
                        "event": "command_agent_history",
                        "task_id": task_id,
                        "events": bg_task.display_events[-200:],  # last 200 events
                        "status": bg_task.status.value,
                        "steps": bg_task.steps,
                        "elapsed": round(bg_task.elapsed, 2),
                    })

            elif command == "command_monitor_unsubscribe":
                task_id = msg.get("task_id", "")
                state._monitored_task_ids.discard(task_id)

            # ── Command Center: Comms Feed ───────────────────
            elif command == "command_feed_list":
                limit = msg.get("limit", 100)
                await ws.send_json({"event": "command_feed_list", "messages": state.command_feed[-limit:]})

            elif command == "command_feed_post":
                content = msg.get("content", "").strip()
                target = msg.get("target", "all")
                if content:
                    feed_msg = {
                        "id": uuid.uuid4().hex[:12],
                        "timestamp": datetime.now().isoformat(),
                        "sender_type": "user",
                        "sender_id": "user",
                        "sender_name": "You",
                        "target": target,
                        "content": content,
                        "message_type": "broadcast" if target == "all" else "instruction",
                    }
                    state.command_feed.append(feed_msg)
                    await ws.send_json({"event": "command_feed_posted", "message": feed_msg})

            # ── Scheduled Tasks ──────────────────────────────
            elif command == "schedule_create":
                name = msg.get("name", "").strip()
                prompt = msg.get("prompt", "").strip()
                schedule = msg.get("schedule", "").strip()
                task_kind = (msg.get("task_kind") or "session").strip() or "session"
                max_loops = max(1, int(msg.get("max_loops") or 6))
                backend_type = msg.get("backend", getattr(state.backend, "name", ""))
                model = msg.get("model", getattr(state.backend, "model", ""))
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                if not prompt or not schedule:
                    await ws.send_json({"event": "error", "message": "Prompt and schedule are required"})
                    continue
                try:
                    spec = None
                    if task_kind != "harness_cycle":
                        spec = state.build_backend_spec(
                            backend_type,
                            model=model or None,
                            project_path=state.project.project_path,
                        )
                    sched = state.scheduler.add(
                        name=name,
                        prompt=prompt,
                        schedule=schedule,
                        backend_type="" if task_kind == "harness_cycle" else backend_type,
                        model="" if task_kind == "harness_cycle" else spec.model,
                        task_kind=task_kind,
                        max_loops=max_loops,
                        session_mode=state.normalize_session_mode(session_mode),
                        session_role=state.normalize_session_role(session_mode, session_role),
                        backend_spec={} if task_kind == "harness_cycle" else spec.to_dict(),
                        project_path=state.project.project_path,
                    )
                    await ws.send_json({"event": "schedule_created", "schedule": sched.to_dict()})
                except ValueError as e:
                    await ws.send_json({"event": "error", "message": str(e)})

            elif command == "schedule_list":
                schedules = state.scheduler.list_schedules()
                await ws.send_json({"event": "schedule_list", "schedules": schedules})

            elif command == "schedule_update":
                task_id = msg.get("task_id", "")
                updates = {}
                for key in (
                    "name",
                    "prompt",
                    "schedule",
                    "backend_type",
                    "model",
                    "task_kind",
                    "max_loops",
                    "session_mode",
                    "session_role",
                    "enabled",
                ):
                    if key in msg:
                        updates[key] = msg[key]
                schedules = state.scheduler.list_schedules()
                existing = next((item for item in schedules if item["id"] == task_id), None)
                effective_kind = updates.get("task_kind", (existing or {}).get("task_kind", "session"))
                if effective_kind == "harness_cycle":
                    updates["backend_spec"] = {}
                    updates["backend_type"] = ""
                    updates["model"] = ""
                elif "backend_type" in updates or "model" in updates:
                    if existing:
                        spec = state.build_backend_spec(
                            updates.get("backend_type", existing.get("backend_type", "")),
                            model=updates.get("model", existing.get("model", "")) or None,
                            project_path=existing.get("project_path", state.project.project_path),
                        )
                        updates["backend_spec"] = spec.to_dict()
                        updates["model"] = spec.model
                state.scheduler.update(task_id, **updates)
                schedules = state.scheduler.list_schedules()
                await ws.send_json({"event": "schedule_list", "schedules": schedules})

            elif command == "schedule_delete":
                task_id = msg.get("task_id", "")
                state.scheduler.remove(task_id)
                schedules = state.scheduler.list_schedules()
                await ws.send_json({"event": "schedule_list", "schedules": schedules})

            # ── Git Integration ─────────────────────────────
            elif command == "git_status":
                result = await asyncio.get_event_loop().run_in_executor(None, _git_status)
                await ws.send_json({"event": "git_status", "data": result})

            elif command == "git_quick":
                action = msg.get("action", "")
                result = await asyncio.get_event_loop().run_in_executor(
                    None, _git_quick, action, msg
                )
                await ws.send_json({"event": "git_result", "action": action, "data": result})

            # ── RESONANT.md ─────────────────────────────────
            elif command == "get_resonant_md":
                instructions = load_project_instructions(state.project.project_path)
                info = get_instruction_info(state.project.project_path)
                await ws.send_json({
                    "event": "resonant_md",
                    "info": info,
                    "content": instructions or "",
                })

            elif command == "save_resonant_md":
                content = msg.get("content", "")
                _save_resonant_md(state.project.project_path, content)
                state._project_instructions = content if content.strip() else None
                if state.session:
                    state.session.project_instructions = state._project_instructions
                await ws.send_json({
                    "event": "resonant_md",
                    "info": get_instruction_info(state.project.project_path),
                    "content": content,
                })

            # ── MCP Servers ──────────────────────────────────
            elif command == "mcp_list":
                servers = state.mcp_manager.list_servers()
                health = state.mcp_manager.health_check()
                await ws.send_json({"event": "mcp_list", "servers": servers, "health": health})

            elif command == "mcp_connect":
                server_name = msg.get("name", "")
                if server_name:
                    success = await asyncio.get_event_loop().run_in_executor(
                        None, state.mcp_manager.connect, server_name
                    )
                    # Update session tools
                    if state.session:
                        state.session.mcp_tools = state.mcp_manager.get_all_tools()
                    servers = state.mcp_manager.list_servers()
                    await ws.send_json({"event": "mcp_list", "servers": servers, "connected": success})

            elif command == "mcp_disconnect":
                server_name = msg.get("name", "")
                if server_name:
                    state.mcp_manager.disconnect(server_name)
                    if state.session:
                        state.session.mcp_tools = state.mcp_manager.get_all_tools()
                    servers = state.mcp_manager.list_servers()
                    await ws.send_json({"event": "mcp_list", "servers": servers})

            # ── Engram Memory ──
            elif command == "engram_recall":
                query = msg.get("query", "")
                if query and state.engram.enabled:
                    memories = await asyncio.get_event_loop().run_in_executor(
                        None, state.engram.recall, query
                    )
                    await ws.send_json({"event": "engram_recall", "memories": memories})
                else:
                    await ws.send_json({"event": "engram_recall", "memories": [], "enabled": state.engram.enabled})

            elif command == "engram_remember":
                text = msg.get("text", "")
                if text and state.engram.enabled:
                    await asyncio.get_event_loop().run_in_executor(
                        None, state.engram.remember, text
                    )
                    await ws.send_json({"event": "engram_remembered", "ok": True})

            elif command == "engram_status":
                await ws.send_json({
                    "event": "engram_status",
                    "enabled": state.engram.enabled,
                    "server_url": state.engram._server_url,
                    "namespace": state.engram._namespace,
                    "has_mcp": state.engram._mcp_manager is not None,
                })

            # ── RAG / Codebase Index ──
            elif command == "rag_index":
                if not state.codebase_index:
                    project_path = state.project.project_path if state.project else os.getcwd()
                    state.codebase_index = CodebaseIndex(project_path, engram=state.engram)
                force = msg.get("force", False)
                stats = await asyncio.get_event_loop().run_in_executor(
                    None, state.codebase_index.index, force
                )
                await ws.send_json({"event": "rag_indexed", **stats})

            elif command == "rag_search":
                query = msg.get("query", "")
                if query and state.codebase_index:
                    results = state.codebase_index.search(query)
                    await ws.send_json({
                        "event": "rag_results",
                        "results": [r.to_dict() for r in results],
                    })
                else:
                    await ws.send_json({"event": "rag_results", "results": []})

            elif command == "rag_stats":
                if state.codebase_index:
                    await ws.send_json({"event": "rag_stats", **state.codebase_index.get_stats()})
                else:
                    await ws.send_json({"event": "rag_stats", "total_files": 0, "is_indexed": False})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")


async def _run_session_streaming(
    ws: WebSocket,
    session: Session,
    user_msg: str,
    images=None,
    *,
    display_user_msg: str | None = None,
    session_mode: str = "code",
    session_role: str = "generator",
):
    """Run Session.run() in a thread, streaming events to WebSocket.

    Returns a list of display events for session persistence/replay.
    """
    event_queue: queue.Queue = queue.Queue()
    display_events: list = []
    pending_harness_payload: dict[str, Any] | None = None
    pending_harness_text: str = ""
    harness_parse_error: str | None = None

    # Record the user message as a display event
    display_events.append({"event": "user_message", "text": display_user_msg or user_msg})

    def on_permission(tool_name, tool_args):
        """Push permission request to frontend with diff review data."""
        # Generate diff review for file-modifying tools
        project_path = state.project.project_path if state.project else ""
        review = generate_review(tool_name, tool_args, project_path)

        event_data = {
            "event": "tool_permission",
            "name": tool_name,
            "arguments": tool_args,
        }
        if review:
            event_data["review"] = review.to_dict()

        event_queue.put(event_data)
        while True:
            if session.cancel_requested or state.cancel_requested.is_set():
                return False
            if state.permission_response.wait(timeout=0.1):
                state.permission_response.clear()
                return state.permission_result[0]

    def on_choice(options):
        """Push choice request to frontend, block until response."""
        event_queue.put({
            "event": "choices",
            "options": options,
        })
        while True:
            if session.cancel_requested or state.cancel_requested.is_set():
                return options[0] if options else ""
            if state.choice_response.wait(timeout=0.1):
                state.choice_response.clear()
                return state.choice_result[0]

    def _engine_thread():
        try:
            for event in session.run(
                user_msg,
                on_permission=on_permission if not session.auto_approve else None,
                on_choice=on_choice,
                images=images,
            ):
                event_queue.put(event)
        except Exception as e:
            event_queue.put(make_event(EngineEvent.ERROR, message=str(e)))
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_engine_thread, daemon=True)
    state.active_thread = thread
    thread.start()

    # Events to skip when saving for replay (streaming deltas are redundant
    # because text.done captures the final text; status/session.start are ephemeral)
    SKIP_FOR_REPLAY = {"text.delta", "thinking.delta", "session.start", "status"}

    def _get_event():
        while True:
            try:
                return event_queue.get(timeout=0.5)
            except queue.Empty:
                continue

    loop = asyncio.get_event_loop()
    try:
        while True:
            event = await loop.run_in_executor(None, _get_event)
            if event is None:
                break
            # Enrich file_edit events with diff lines for frontend rendering
            if event.get("event") == EngineEvent.TOOL_CALL.value and event.get("name") == "file_edit":
                args = event.get("arguments", {})
                old_text = args.get("old_text", "")
                new_text = args.get("new_text", "")
                diff_lines = list(difflib.unified_diff(
                    old_text.split("\n"), new_text.split("\n"),
                    lineterm="", n=2,
                ))
                event["diff_lines"] = diff_lines

            # Enrich screenshot tool results with top-level image field
            if (event.get("event") == EngineEvent.TOOL_RESULT.value and
                    event.get("name") in ("browser_screenshot", "computer_screenshot")):
                meta = event.get("metadata", {})
                b64 = meta.get("screenshot_b64")
                if b64:
                    media_type = meta.get("media_type", "image/png")
                    event["image"] = {
                        "data": b64,
                        "media_type": media_type,
                    }
                    # Remove from metadata to avoid double-sending
                    meta.pop("screenshot_b64", None)

            # Track costs from status events
            event_type = event.get("event", "")
            if event_type == EngineEvent.TEXT_DONE.value:
                raw_text = str(event.get("text") or "")
                cleaned_text, harness_payload, parse_error = state.extract_harness_update(
                    text=raw_text,
                    session_mode=session_mode,
                    session_role=session_role,
                )
                if parse_error:
                    harness_parse_error = parse_error
                if harness_payload is not None:
                    pending_harness_payload = harness_payload
                    pending_harness_text = cleaned_text
                if cleaned_text != raw_text:
                    event = dict(event)
                    event["text"] = cleaned_text
                    state.rewrite_last_assistant_message(session, raw_text, cleaned_text)

            if event_type == "status":
                stats = event.get("stats", {})
                model = event.get("model", "")
                in_tok = stats.get("input_tokens", 0)
                out_tok = stats.get("output_tokens", 0)
                if (in_tok or out_tok) and state.settings.get("cost_tracking", "enabled", True):
                    cost = state.costs.record_usage(model, in_tok, out_tok)
                    stats["cost_usd"] = round(cost, 6)
                    stats["session_cost_usd"] = state.costs.get_session_cost()["cost_usd"]
                    budget_alert = state.settings.get("cost_tracking", "budget_alert_usd", None)
                    today = date.today().isoformat()
                    today_cost = state.costs.get_daily_cost(today)["cost_usd"]
                    if (
                        budget_alert is not None and
                        today_cost >= float(budget_alert) and
                        today not in state._budget_alert_days
                    ):
                        state._budget_alert_days.add(today)
                        await ws.send_json({
                            "event": "status_msg",
                            "message": f"Daily spend crossed ${float(budget_alert):.2f} (${today_cost:.4f} today)",
                        })

            # Collect display events for session replay (skip streaming deltas)
            if event_type not in SKIP_FOR_REPLAY:
                display_events.append(event)

            try:
                await ws.send_json(event)
            except Exception:
                session.cancel()
                break

        if harness_parse_error:
            await ws.send_json({"event": "error", "message": harness_parse_error})

        if pending_harness_payload is not None:
            try:
                status_message = state.apply_harness_update(
                    session_mode=session_mode,
                    session_role=session_role,
                    payload=pending_harness_payload,
                    project_path=state.project.project_path,
                    assistant_text=pending_harness_text,
                    user_request=display_user_msg or user_msg,
                )
                await ws.send_json({"event": "harness_state", "data": state.get_harness_summary()})
                if status_message:
                    await ws.send_json({"event": "status_msg", "message": status_message})
            except Exception as exc:
                await ws.send_json({"event": "error", "message": f"Failed to apply harness update: {exc}"})
    finally:
        state.active_thread = None

    return display_events


# ── Git Helpers ───────────────────────────────────────────────────────

def _git_run(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a git command and return (returncode, stdout)."""
    import subprocess
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=15,
            cwd=cwd or state.project.project_path,
            shell=(sys.platform == "win32"),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _git_status() -> dict:
    """Get git status for the current project."""
    cwd = state.project.project_path

    # Branch
    rc, branch = _git_run("branch", "--show-current", cwd=cwd)
    if rc != 0:
        return {"is_repo": False}

    # Status (porcelain)
    _, status_raw = _git_run("status", "--porcelain", cwd=cwd)
    changes = []
    for line in status_raw.split("\n"):
        line = line.strip()
        if line:
            status_code = line[:2].strip()
            filepath = line[3:]
            changes.append({"status": status_code, "file": filepath})

    # Recent commits
    _, log_raw = _git_run("log", "--oneline", "-10", cwd=cwd)
    commits = []
    for line in log_raw.split("\n"):
        line = line.strip()
        if line:
            parts = line.split(" ", 1)
            commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})

    return {
        "is_repo": True,
        "branch": branch.strip(),
        "changes": changes,
        "change_count": len(changes),
        "commits": commits,
    }


def _git_quick(action: str, msg: dict) -> dict:
    """Execute quick git actions."""
    cwd = state.project.project_path

    if action == "diff":
        _, output = _git_run("diff", cwd=cwd)
        return {"output": output}
    elif action == "diff_staged":
        _, output = _git_run("diff", "--staged", cwd=cwd)
        return {"output": output}
    elif action == "log":
        count = msg.get("count", 20)
        _, output = _git_run("log", "--oneline", f"-{count}", cwd=cwd)
        return {"output": output}
    elif action == "add":
        files = msg.get("files", [])
        if files:
            rc, output = _git_run("add", *files, cwd=cwd)
        else:
            rc, output = _git_run("add", "-A", cwd=cwd)
        return {"success": rc == 0, "output": output}
    elif action == "commit":
        message = msg.get("message", "")
        if not message:
            return {"success": False, "output": "No commit message"}
        rc, output = _git_run("commit", "-m", message, cwd=cwd)
        return {"success": rc == 0, "output": output}
    elif action == "stash":
        rc, output = _git_run("stash", cwd=cwd)
        return {"success": rc == 0, "output": output}
    elif action == "stash_pop":
        rc, output = _git_run("stash", "pop", cwd=cwd)
        return {"success": rc == 0, "output": output}
    else:
        return {"success": False, "output": f"Unknown action: {action}"}


# ── RESONANT.md Helpers ──────────────────────────────────────────────

def _save_resonant_md(project_path: str, content: str):
    """Save RESONANT.md to the project root."""
    path = Path(project_path) / "RESONANT.md"
    path.write_text(content, encoding="utf-8")


# ── HTTP Routes ───────────────────────────────────────────────────────

async def homepage(request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Starlette App ─────────────────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/", homepage),
        WebSocketRoute("/ws", websocket_endpoint),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
)
