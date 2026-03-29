"""
Resonant Client GUI — ASGI Application

Starlette app with WebSocket endpoint for streaming EngineEvents
to the web-based frontend. The engine runs in a background thread;
events are pushed through a queue to the async WebSocket handler.
"""

import asyncio
import ast
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
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
from ..engine import Session, AGENT_TOOLS
from ..network_defaults import resolve_resonant_api_url
from .sessions import ProjectManager
from .settings import SettingsManager
from .costs import CostTracker
from .project_instructions import load_project_instructions, get_instruction_info
from .command_projects import CommandProjectStore
from .command_tasks import CommandTaskStore
from .task_runner import TaskRunner
from .scheduler import Scheduler
from .runtime import BackendSpec
from ..harness import EvaluatorReport, HarnessWorkspace, HarnessOrchestrator, HarnessService
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
        self.harness_service = HarnessService(
            normalize_session_mode=self.normalize_session_mode,
            normalize_session_role=self.normalize_session_role,
        )
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
        self.refresh_network_defaults()
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
        return self.harness_service.build_instructions(
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
        )

    def build_harness_output_contract(
        self,
        *,
        session_mode: str,
        session_role: str,
    ) -> str:
        return self.harness_service.build_output_contract(
            session_mode=session_mode,
            session_role=session_role,
        )

    def get_harness_summary(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        return self.harness_service.get_summary(target_path)

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

    def get_harness_generator_mode(self) -> str:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_MODE", "hybrid") or "").strip().lower()
        if raw in {"full", "artifacts", "patch", "structured", "hybrid"}:
            return raw
        return "hybrid"

    def get_harness_generator_artifact_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_ARTIFACT_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 448

    def get_harness_generator_structured_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_STRUCTURED_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 1024

    def get_harness_generator_patch_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_PATCH_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 768

    def get_harness_generator_repair_max_tokens(self) -> int:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_REPAIR_MAX_TOKENS", "") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except ValueError:
                pass
        return 384

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
    def _format_numbered_window(
        path: Path,
        *,
        start_line: int,
        end_line: int,
        padding: int = 12,
        max_lines: int = 96,
        max_chars: int = 3400,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        lines = text.splitlines()
        if not lines:
            return ""
        lo = max(1, int(start_line) - padding)
        hi = min(len(lines), int(end_line) + padding)
        selected = lines[lo - 1 : hi]
        if len(selected) > max_lines:
            hi = lo + max_lines - 1
            selected = lines[lo - 1 : hi]
        output = "\n".join(f"{line_no:>4}: {line}" for line_no, line in enumerate(selected, start=lo))
        if len(output) > max_chars:
            output = output[:max_chars].rstrip() + "\n...[truncated]"
        return output

    @staticmethod
    def _extract_line_hint_window(file_path: str, hints: list[str]) -> tuple[int, int, str] | None:
        normalized_path = file_path.replace("\\", "/").strip()
        normalized_name = Path(normalized_path).name
        for raw_hint in hints:
            hint = str(raw_hint or "").strip()
            if not hint:
                continue
            match = re.search(
                r"(?:(?P<path>`?[^`\s:]+(?:/[^`\s:]+)*`?)\s*:\s*)?"
                r"(?:(?:after|around)\s+line\s+|line\s+)?"
                r"(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?",
                hint,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            hint_path = str(match.group("path") or "").replace("\\", "/").strip("` ").strip()
            if hint_path and hint_path not in {normalized_path, normalized_name}:
                continue
            start_line = int(match.group("start"))
            end_line = int(match.group("end") or match.group("start"))
            return start_line, end_line, hint
        return None

    @staticmethod
    def _requests_broad_repo_scope(text: str) -> bool:
        lowered = " ".join(str(text or "").lower().split())
        if not lowered:
            return False

        negative_phrases = (
            "no repo-wide",
            "without repo-wide",
            "avoid repo-wide",
            "not repo-wide",
            "does not require repo-wide",
            "doesn't require repo-wide",
            "without requiring repo-wide",
            "no whole repo",
            "without whole repo",
            "avoid whole repo",
            "does not require whole repo",
            "doesn't require whole repo",
            "no full codebase",
            "without full codebase",
            "avoid full codebase",
            "does not require full codebase",
            "doesn't require full codebase",
            "no entire repository",
            "without entire repository",
            "avoid entire repository",
            "does not require entire repository",
            "doesn't require entire repository",
            "no across the repo",
            "without across the repo",
            "avoid across the repo",
            "does not require across the repo",
            "doesn't require across the repo",
        )
        for phrase in negative_phrases:
            lowered = lowered.replace(phrase, "")

        return any(
            token in lowered
            for token in (
                "entire repository",
                "whole repo",
                "full codebase",
                "across the repo",
                "repo-wide",
            )
        )

    @staticmethod
    def _extract_patch_scaffold(file_path: str, hints: list[str]) -> list[str]:
        normalized_path = file_path.replace("\\", "/").strip()
        normalized_name = Path(normalized_path).name
        notes: list[str] = []
        seen: set[str] = set()

        def add_note(value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            notes.append(text)

        for raw_hint in hints:
            hint = str(raw_hint or "").strip()
            if not hint:
                continue
            parsed: Any = None
            if hint.startswith("{") and hint.endswith("}"):
                try:
                    parsed = ast.literal_eval(hint)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                scoped = parsed.get(normalized_path) or parsed.get(normalized_name) or parsed
                if isinstance(scoped, dict):
                    for value in scoped.values():
                        add_note(value)
                else:
                    add_note(scoped)
                continue
            if re.search(r"\bline\s+\d+", hint, flags=re.IGNORECASE):
                continue
            add_note(hint)
        return notes[:4]

    @staticmethod
    def _format_anchor_windows(
        path: Path,
        anchors: list[str],
        *,
        padding: int = 10,
        max_windows: int = 3,
        max_chars: int = 3400,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        lines = text.splitlines()
        if not lines:
            return ""

        anchor_names: list[str] = []
        for item in anchors:
            for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\(\)", str(item or "")):
                if name not in anchor_names:
                    anchor_names.append(name)

        windows: list[tuple[int, int, str]] = []
        for name in anchor_names:
            def_pattern = re.compile(rf"^\s*def\s+{re.escape(name)}\s*\(")
            call_pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
            target_line = 0
            for line_no, line in enumerate(lines, start=1):
                if def_pattern.search(line):
                    target_line = line_no
                    break
            if not target_line:
                for line_no, line in enumerate(lines, start=1):
                    if call_pattern.search(line):
                        target_line = line_no
                        break
            if target_line:
                windows.append(
                    (
                        max(1, target_line - padding),
                        min(len(lines), target_line + padding),
                        name,
                    )
                )
            if len(windows) >= max_windows:
                break

        if not windows:
            return ""

        rendered: list[str] = []
        for start_line, end_line, name in windows:
            rendered.append(f"--- context around {name}() ---")
            rendered.extend(
                f"{line_no:>4}: {line}"
                for line_no, line in enumerate(lines[start_line - 1 : end_line], start=start_line)
            )
        output = "\n".join(rendered)
        if len(output) > max_chars:
            output = output[:max_chars].rstrip() + "\n...[truncated]"
        return output

    @staticmethod
    def _filter_tool_definitions(allowed_names: list[str]) -> list[dict[str, Any]]:
        allowed = set(allowed_names)
        return [
            tool
            for tool in AGENT_TOOLS
            if tool.get("function", {}).get("name", "") in allowed
        ]

    def extract_harness_referenced_files(
        self,
        project_path: Optional[str] = None,
        *texts: Any,
        limit: int = 4,
    ) -> list[str]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        project_root = Path(target_path).resolve()
        candidates: list[str] = []
        seen_raw: set[str] = set()
        fenced_pattern = re.compile(r"`([^`\n]+)`")
        path_patterns = (
            re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9_.-]+)"),
            re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)"),
        )

        for raw_text in texts:
            text = str(raw_text or "")
            if not text.strip():
                continue
            for match in fenced_pattern.finditer(text):
                candidate = match.group(1).strip()
                if candidate and candidate not in seen_raw:
                    seen_raw.add(candidate)
                    candidates.append(candidate)
            for pattern in path_patterns:
                for match in pattern.finditer(text):
                    candidate = match.group(1).strip()
                    if candidate and candidate not in seen_raw:
                        seen_raw.add(candidate)
                        candidates.append(candidate)

        referenced: list[str] = []
        seen_display: set[str] = set()
        for raw in candidates:
            cleaned = raw.strip().strip("`'\"()[]{}<>.,;:")
            if cleaned.startswith("./"):
                cleaned = cleaned[2:]
            if not cleaned:
                continue
            resolved = self._resolve_harness_touched_path(target_path, cleaned)
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            display_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
            if display_path not in seen_display:
                seen_display.add(display_path)
                referenced.append(display_path)
            if len(referenced) >= limit:
                break
        return referenced

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
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1600)
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))

        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
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
                hint_window = self._extract_line_hint_window(raw_path, line_hints)
                if hint_window:
                    start_line, end_line, _ = hint_window
                    excerpt = self._format_numbered_window(
                        resolved,
                        start_line=start_line,
                        end_line=end_line,
                        padding=16,
                        max_lines=100,
                        max_chars=3200,
                    )
                else:
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
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
            "acceptance_check_coverage": coverage,
        }

    def should_use_harness_generator_artifact_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        mode = self.get_harness_generator_mode()
        if mode == "full":
            return False

        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        read_only = self._is_read_only_harness_request(
            prompt,
            summary.get("contract_objective", ""),
            summary.get("contract_feature_name", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
        )
        if not read_only and self._harness_generator_needs_evidence_followup(target_path):
            return True
        if not read_only:
            return False

        referenced_files = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        if mode == "artifacts":
            return bool(referenced_files)
        return bool(referenced_files)

    def _harness_generator_needs_evidence_followup(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        contract_status = str(summary.get("contract_status") or "").strip()
        evaluator_verdict = str(summary.get("evaluator_verdict") or "").strip()
        if contract_status != "needs_revision" or evaluator_verdict != "revise":
            return False
        if self._harness_generator_needs_frontier_repair(target_path):
            return False
        if self._normalize_string_list(summary.get("blockers")):
            return False
        if not self._normalize_string_list(summary.get("touched_files")):
            return False

        combined = "\n".join(
            [
                "\n".join(self._normalize_string_list(summary.get("required_revisions"))),
                "\n".join(self._normalize_string_list(summary.get("findings"))),
                str(summary.get("last_validation") or ""),
            ]
        ).lower()
        evidence_tokens = (
            "missing evidence",
            "insufficient evidence",
            "not enough evidence",
            "cannot verify",
            "record",
            "validation",
            "confirm",
            "show",
            "callable",
            "empty dict",
            "no other files are modified",
        )
        return bool(combined and any(token in combined for token in evidence_tokens))

    def should_use_harness_generator_structured_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        mode = self.get_harness_generator_mode()
        if mode == "full":
            return False

        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        scaffold_target_files = self._normalize_string_list(summary.get("target_files"))
        if self._is_read_only_harness_request(
            prompt,
            summary.get("contract_objective", ""),
            summary.get("contract_feature_name", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
        ):
            return False

        referenced_files = scaffold_target_files or self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            "\n".join(self._normalize_string_list(summary.get("touched_files"))),
            limit=3,
        )
        if not referenced_files or len(referenced_files) > 2:
            return False

        combined = " ".join(
            [
                str(prompt or ""),
                str(summary.get("contract_objective") or ""),
                str(summary.get("contract_feature_name") or ""),
                "\n".join(self._normalize_string_list(summary.get("deliverables"))),
                "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            ]
        ).lower()
        if self._requests_broad_repo_scope(combined):
            return False
        return True

    def get_harness_generator_strategy(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        mode = self.get_harness_generator_mode()
        if self.should_use_harness_generator_artifact_mode(project_path, prompt):
            return "artifacts"
        if mode != "full" and self.can_use_harness_generator_repair_mode(project_path, prompt):
            return "repair"
        if mode in {"patch", "structured", "hybrid"} and self.can_use_harness_generator_patch_mode(project_path, prompt):
            return "patch"
        if mode in {"structured", "hybrid"} and self.should_use_harness_generator_structured_mode(project_path, prompt):
            return "structured"
        return "full"

    def build_harness_generator_artifact_bundle(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1400)
        referenced_paths = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            handoff_text,
        ]

        for raw_path in referenced_paths[:4]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                excerpt = self._format_numbered_excerpt(resolved, max_lines=100, max_chars=3200)
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

    def build_harness_generator_structured_bundle(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1400)
        scaffold_target_files = self._normalize_string_list(summary.get("target_files"))
        referenced_paths = scaffold_target_files or self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            "\n".join(self._normalize_string_list(summary.get("touched_files"))),
            limit=3,
        )
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))

        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            handoff_text,
        ]

        for raw_path in referenced_paths[:3]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                excerpt = self._format_numbered_excerpt(resolved, max_lines=120, max_chars=3600)
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
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
            "acceptance_check_coverage": coverage,
        }

    def can_use_harness_generator_patch_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        if not self.should_use_harness_generator_structured_mode(project_path, prompt):
            return False
        bundle = self.build_harness_generator_structured_bundle(project_path, prompt)
        files = bundle.get("files") or []
        if len(files) != 1:
            return False
        file_item = files[0]
        if not bool(file_item.get("exists")):
            return False
        summary = bundle.get("summary") or {}
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        if self._extract_line_hint_window(str(file_item.get("path") or ""), line_hints):
            return True
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        try:
            return resolved_path.stat().st_size <= 9000
        except OSError:
            return False

    def can_use_harness_generator_repair_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        target_path = os.path.normpath(project_path or self.project.project_path)
        if not self._harness_generator_needs_frontier_repair(target_path):
            return False
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        files = bundle.get("files") or []
        if len(files) != 1:
            return False
        file_item = files[0]
        if not bool(file_item.get("exists")):
            return False
        return bool(self._extract_harness_repair_context(target_path, file_item, bundle.get("summary") or {}))

    def _extract_harness_repair_traceback(
        self,
        project_path: str,
        file_item: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_path = Path(str(file_item.get("resolved_path") or "")).resolve()
        candidate_labels = {
            str(file_item.get("path") or "").replace("\\", "/"),
            resolved_path.name,
            str(resolved_path),
        }
        combined_sources = [
            *self._normalize_string_list(summary.get("validation_artifacts")),
            *self._normalize_string_list(summary.get("findings")),
            *self._normalize_string_list(summary.get("required_revisions")),
            str(summary.get("last_validation") or ""),
        ]
        combined = "\n".join(part for part in combined_sources if str(part or "").strip())

        best_line = 0
        best_path = ""
        for match in re.finditer(r'File\s+"([^"]+)",\s+line\s+(\d+)', combined):
            raw_path = str(match.group(1) or "").strip()
            line_number = int(match.group(2))
            normalized = raw_path.replace("\\", "/")
            if normalized in candidate_labels or Path(normalized).name in candidate_labels:
                best_path = raw_path
                best_line = line_number
                break

        error_line = ""
        for raw_line in combined.splitlines():
            stripped = raw_line.strip()
            lowered = stripped.lower()
            if any(
                token in lowered
                for token in (
                    "syntaxerror",
                    "indentationerror",
                    "unexpected indent",
                    "expected an indented block",
                    "invalid syntax",
                    "parse error",
                    "runtimeerror",
                    "importerror",
                    "nameerror",
                    "typeerror",
                    "attributeerror",
                )
            ):
                error_line = stripped
                break

        return {
            "line_number": best_line,
            "path": best_path,
            "error_line": error_line,
            "combined": combined,
        }

    def _extract_harness_repair_context(
        self,
        project_path: str,
        file_item: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        traceback_data = self._extract_harness_repair_traceback(project_path, file_item, summary)
        line_number = int(traceback_data.get("line_number") or 0)
        if line_number > 0:
            file_context = self._format_numbered_window(
                resolved_path,
                start_line=line_number,
                end_line=line_number,
                padding=8,
                max_lines=48,
                max_chars=1800,
            )
        else:
            file_context = self._format_numbered_excerpt(
                resolved_path,
                max_lines=48,
                max_chars=1800,
            )
        return {
            "line_number": line_number,
            "error_line": str(traceback_data.get("error_line") or "").strip(),
            "combined": str(traceback_data.get("combined") or "").strip(),
            "file_context": file_context,
            "edit_snippets": self._extract_edit_snippet_artifacts(summary),
        }

    def build_harness_generator_repair_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        file_item = (bundle.get("files") or [{}])[0]
        repair = self._extract_harness_repair_context(target_path, file_item, summary)
        validation_commands = self._normalize_harness_validation_commands(
            summary.get("validation_commands"),
            project_path=target_path,
        )
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        error_line = repair.get("error_line") or "unknown runtime/parse error"
        error_trace = self._truncate_text(str(repair.get("error_line") or ""), max_chars=220)
        lines = [
            "Generator repair mode for a blocked single-file sprint.",
            "Fix only the concrete failure shown below.",
            "Edit only the target file and keep the original sprint scope.",
            "Do not re-plan, do not explore the repo, and do not rewrite unrelated parts of the file.",
            "Repair only the failed edit window or its immediate surroundings.",
            "Do not touch the shebang, __future__ import, or unrelated imports unless the failure is on that exact line.",
            "Use file_edit for the patch and at most one cheap bash validation command.",
            "Use the suggested validation command exactly as written after the repair.",
            "",
            f"Target file: {file_item.get('path') or '(unknown)'}",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Blocking failure: {error_line}",
        ]
        if repair.get("line_number"):
            lines.append(f"Repair focus line: {repair['line_number']}")
        lines.extend(
            [
                "",
                "Required revision:",
            ]
        )
        lines.extend(f"- {item}" for item in revisions[:3] or ["Fix the blocking failure and keep the sprint intent intact."])
        lines.extend(
            [
                "",
                "Keep this intended behavior after the repair:",
            ]
        )
        lines.extend(f"- {item}" for item in deliverables[:3] or ["(none)"])
        lines.extend(
            [
                "",
                "Acceptance checks to preserve:",
            ]
        )
        lines.extend(f"- {item}" for item in checks[:4] or ["(none)"])
        if validation_commands:
            lines.extend(["", "Suggested validation command (use exactly this command):"])
            lines.append(f"- {validation_commands[0]}")
        edit_snippets = repair.get("edit_snippets") or []
        if edit_snippets:
            lines.extend(["", "Last attempted edit snippets:"])
            lines.extend(edit_snippets[:4])
        lines.extend(
            [
                "",
                "Recent failure evidence:",
                error_trace or "(none)",
                "",
                "Current target file excerpt:",
                repair.get("file_context") or "[missing file excerpt]",
                "",
                "Required output behavior:",
                "- apply the minimal repair directly",
                "- record the exact touched file",
                "- record one concise validation summary",
                "- map satisfied checks into acceptance_evidence",
                "- finish with a valid ```resonant-harness JSON block for generator_update",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_patch_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        file_item = (bundle.get("files") or [{}])[0]
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        validation_commands = self._normalize_harness_validation_commands(
            summary.get("validation_commands"),
            project_path=target_path,
        )
        edit_strategy = str(summary.get("edit_strategy") or "").strip()
        scaffold_notes = self._extract_patch_scaffold(str(file_item.get("path") or ""), line_hints)
        hint_window = self._extract_line_hint_window(str(file_item.get("path") or ""), line_hints)
        if hint_window:
            start_line, end_line, raw_hint = hint_window
            file_context = self._format_numbered_window(
                resolved_path,
                start_line=start_line,
                end_line=end_line,
                padding=12,
                max_lines=96,
                max_chars=3400,
            )
        elif scaffold_notes:
            raw_hint = ""
            file_context = self._format_anchor_windows(
                resolved_path,
                scaffold_notes,
                padding=10,
                max_windows=3,
                max_chars=3400,
            ) or self._format_numbered_excerpt(resolved_path, max_lines=80, max_chars=3000)
        else:
            raw_hint = ""
            file_context = self._format_numbered_excerpt(resolved_path, max_lines=100, max_chars=3800)
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))

        lines = [
            "Single-file patch generator mode.",
            "Make the smallest patch that satisfies the sprint.",
            "Edit only the target file shown below.",
            "Do not explore the repo or open unrelated files.",
            "Do not rewrite the file prologue, shebang, __future__ imports, or unrelated import blocks unless the acceptance checks require it.",
            "Prefer the smallest local replacement over generating new scaffolding or broad rewrites.",
            "Use file_edit for the patch and at most one cheap bash validation command if it is obvious.",
            "If the file context below is insufficient or the change needs another file, stop and record a blocker instead.",
            "",
            f"Target file: {file_item.get('path') or '(unknown)'}",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            *( [f"Line hint: {raw_hint}"] if raw_hint else [] ),
            *( [f"Edit strategy: {edit_strategy}"] if edit_strategy else [] ),
            *(["Patch scaffold:"] + [f"- {item}" for item in scaffold_notes] if scaffold_notes else []),
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:4] or ["(none)"])
        lines.extend(["", "Acceptance checks:"])
        lines.extend(f"- {item}" for item in checks[:4] or ["(none)"])
        if validation_commands:
            lines.extend(["", "Suggested validation commands:"])
            lines.extend(f"- {item}" for item in validation_commands[:1])
        lines.extend(
            [
                "",
                "Required output behavior:",
                "- apply the patch directly",
                "- record the exact touched file",
                "- record one concise validation summary",
                "- map satisfied checks into acceptance_evidence",
                "- finish with a valid ```resonant-harness JSON block for generator_update",
                "",
                "Target file contents:",
                file_context or "[missing file excerpt]",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_structured_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        blockers = self._normalize_string_list(summary.get("blockers"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))

        lines = [
            "Compact structured generator mode for a small code-changing sprint.",
            "Keep the implementation narrowly scoped to the referenced files below unless you hit a real blocker.",
            "Use the provided file excerpts first, then use tools only if needed.",
            "Allowed tools are limited to file_read, file_edit, and cheap bash validation commands.",
            "Do not broaden into repo-wide exploration; if the sprint cannot be completed within the shown file scope, record a blocker instead.",
            "After making changes, record exact touched_files, concise validation checks, validation artifacts, and acceptance evidence aligned to the contract checks.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:6] or ["(none)"])
        lines.append("")
        lines.append("Acceptance checks:")
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        if blockers:
            lines.append("")
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:6])
        if next_steps:
            lines.append("")
            lines.append("Current next steps:")
            lines.extend(f"- {item}" for item in next_steps[:6])
        if revisions:
            lines.append("")
            lines.append("Required revisions from evaluator:")
            lines.extend(f"- {item}" for item in revisions[:6])

        lines.extend(
            [
                "",
                "Current harness summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
            ]
        )
        if validation_artifacts:
            lines.append("")
            lines.append("Existing validation artifacts:")
            lines.extend(f"- {item}" for item in validation_artifacts[:8])
        if acceptance_evidence:
            lines.append("")
            lines.append("Existing acceptance evidence:")
            lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
                for check, evidence in list(acceptance_evidence.items())[:8]
            )

        lines.extend(
            [
                "",
                "Existing handoff excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess from current evidence:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item.get("matched") else "unmatched"
            lines.append(f"- {marker}: {item.get('check') or '(unknown)'}")

        lines.append("")
        lines.append("Referenced file excerpts:")
        for file_item in bundle["files"]:
            lines.append(f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})")
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.extend(
            [
                "Keep the prose concise.",
                "Then finish with a valid ```resonant-harness JSON block for generator_update that includes:",
                "- progress.summary",
                "- progress.last_validation",
                "- progress.touched_files",
                "- progress.validation_checks",
                "- progress.validation_artifacts",
                "- progress.acceptance_evidence",
                "- handoff_markdown with concise implementation notes and file references",
                "- sprint_status set to implemented if the sprint is done, otherwise needs_revision or failed if blocked",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_artifact_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        bundle = self.build_harness_generator_artifact_bundle(target_path, prompt)
        summary = bundle["summary"]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        blockers = self._normalize_string_list(summary.get("blockers"))

        lines = [
            "Artifact-only generator mode.",
            "Do not modify repository files.",
            "Use this mode to capture grounded findings or missing evidence without reopening edits.",
            "Do not use tools unless the runtime already supplied enough artifacts to answer honestly.",
            "Use only the harness context and file excerpts below.",
            "Capture concise, grounded findings in handoff/progress artifacts.",
            "If a check is not supported by the provided evidence, do not invent it; note the gap honestly.",
            "Set sprint_status to implemented once the artifact update is recorded.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:6] or ["(none)"])
        lines.append("")
        lines.append("Acceptance checks:")
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        if blockers:
            lines.append("")
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:6])
        if revisions:
            lines.append("")
            lines.append("Required revisions from evaluator:")
            lines.extend(f"- {item}" for item in revisions[:6])

        lines.extend(
            [
                "",
                "Current harness summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
                "",
                "Existing handoff excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess from the included evidence:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item.get("matched") else "unmatched"
            lines.append(f"- {marker}: {item.get('check') or '(unknown)'}")

        lines.append("")
        lines.append("Referenced file excerpts:")
        for file_item in bundle["files"]:
            lines.append(f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})")
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.extend(
            [
                "Keep the prose to at most 6 short lines.",
                "Then finish with a valid ```resonant-harness JSON block for generator_update that includes:",
                "- progress.summary",
                "- progress.last_validation",
                "- progress.validation_checks",
                "- progress.validation_artifacts",
                "- progress.acceptance_evidence",
                "- handoff_markdown with concise findings and file references",
                "- sprint_status set to implemented",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _strip_list_marker(value: str) -> str:
        return re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", str(value or "").strip())

    def _choose_supporting_line_for_check(self, check: str, lines: list[str]) -> str:
        check_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(check or "").lower())
            if len(token) > 2 and token not in {
                "the", "and", "with", "that", "this", "from", "into", "then", "what",
                "when", "where", "which", "does", "have", "names", "includes", "include",
                "explains", "primary", "concrete", "visible", "code", "main",
            }
        }
        if not check_tokens:
            return ""

        best_line = ""
        best_score = 0
        for raw_line in lines:
            line = self._strip_list_marker(raw_line)
            if not line:
                continue
            line_tokens = set(re.findall(r"[a-z0-9]+", line.lower()))
            score = len(check_tokens & line_tokens)
            if score > best_score:
                best_score = score
                best_line = line

        minimum = max(2, min(len(check_tokens), 3))
        return best_line if best_score >= minimum else ""

    def align_acceptance_evidence_to_contract(
        self,
        *,
        acceptance_checks: list[str],
        evidence: dict[str, str],
        objective: str = "",
        user_request: str = "",
    ) -> dict[str, str]:
        if not acceptance_checks or not evidence:
            return {}

        normalized_exact = {
            self._normalize_acceptance_check_phrase(key): str(value).strip()
            for key, value in evidence.items()
            if self._normalize_acceptance_check_phrase(key) and str(value).strip()
        }
        raw_entries = [
            (str(key).strip(), str(value).strip())
            for key, value in evidence.items()
            if str(key).strip() and str(value).strip()
        ]
        aligned: dict[str, str] = {}

        for check in acceptance_checks:
            phrase = self._normalize_acceptance_check_phrase(check)
            if phrase and phrase in normalized_exact:
                aligned[check] = self._truncate_text(normalized_exact[phrase], max_chars=220)
                continue

            best_value = ""
            best_score = 0
            check_tokens = set(re.findall(r"[a-z0-9]+", phrase.replace("_", " ")))
            for raw_key, raw_value in raw_entries:
                key_tokens = set(re.findall(r"[a-z0-9]+", raw_key.lower().replace("_", " ")))
                score = len(check_tokens & key_tokens)
                if score > best_score:
                    best_score = score
                    best_value = raw_value
            if best_value and best_score >= 2:
                aligned[check] = self._truncate_text(best_value, max_chars=220)

        if self._is_read_only_harness_request(objective, user_request):
            for check in acceptance_checks:
                lowered = check.lower()
                if (
                    check not in aligned
                    and "no repository files" in lowered
                    and ("read-only" in lowered or "repository files" in lowered or "modified" in lowered)
                ):
                    aligned[check] = "Artifact-only read-only sprint; no repository files were modified."

        return aligned

    def infer_generator_artifact_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
        prompt: str = "",
    ) -> dict[str, Any] | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None

        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None
        if not self.should_use_harness_generator_artifact_mode(target_path, prompt):
            return None

        lines = [
            self._strip_list_marker(line)
            for line in stripped.splitlines()
            if self._strip_list_marker(line)
        ]
        if not lines:
            return None

        findings: list[str] = []
        seen_findings: set[str] = set()
        for line in lines:
            normalized = line.strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen_findings:
                continue
            seen_findings.add(lowered)
            findings.append(self._truncate_text(normalized, max_chars=260))
            if len(findings) >= 6:
                break
        if not findings:
            return None

        referenced_files = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        acceptance_evidence: dict[str, str] = {}
        for check in acceptance_checks[:8]:
            supporting = self._choose_supporting_line_for_check(check, findings)
            if supporting:
                acceptance_evidence[check] = self._truncate_text(supporting, max_chars=220)

        validation_checks = []
        if referenced_files:
            validation_checks.append(f"Reviewed read-only evidence in: {', '.join(referenced_files[:4])}")
        validation_checks.extend(findings[:3])
        validation_artifacts = []
        if referenced_files:
            validation_artifacts.append(
                f"Artifact-only read-only audit captured findings from {', '.join(referenced_files[:4])}."
            )
        validation_artifacts.append("Findings were recorded in handoff_markdown and harness progress fields.")

        summary_text = self._truncate_text(" | ".join(findings[:2]), max_chars=220)
        handoff_lines = [
            "# Read-only audit handoff",
            "",
            "## Summary",
            summary_text or "Completed a read-only artifact audit.",
            "",
        ]
        if referenced_files:
            handoff_lines.append("## Referenced files")
            handoff_lines.extend(f"- `{path}`" for path in referenced_files[:6])
            handoff_lines.append("")
        handoff_lines.append("## Findings")
        handoff_lines.extend(f"- {item}" for item in findings)

        return {
            "action": "generator_update",
            "progress": {
                "summary": summary_text or "Completed the read-only sprint with grounded audit findings.",
                "last_validation": "Completed an artifact-only read-only audit over the referenced file excerpts.",
                "validation_checks": validation_checks[:6],
                "validation_artifacts": validation_artifacts[:4],
                "acceptance_evidence": acceptance_evidence,
                "touched_files": referenced_files[:6],
                "current_phase": "implementation",
            },
            "handoff_markdown": "\n".join(handoff_lines),
            "sprint_status": "implemented",
        }

    @staticmethod
    def _extract_tool_arguments_from_event(event: dict[str, Any]) -> dict[str, Any]:
        args = event.get("arguments", {})
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
        return {}

    def extract_generator_structured_event_summary(
        self,
        *,
        project_path: Optional[str] = None,
        display_events: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        touched_files: list[str] = []
        seen_files: set[str] = set()
        validation_artifacts: list[str] = []
        seen_artifacts: set[str] = set()

        for event in display_events:
            if event.get("event") == EngineEvent.TOOL_CALL.value:
                tool_name = str(event.get("name") or "").strip()
                arguments = self._extract_tool_arguments_from_event(event)
                if tool_name in {"file_edit", "file_write"}:
                    raw_path = str(arguments.get("path") or "").strip()
                    if raw_path:
                        display_path = os.path.relpath(
                            str(self._resolve_harness_touched_path(target_path, raw_path)),
                            target_path,
                        ).replace(os.sep, "/")
                        if display_path not in seen_files:
                            seen_files.add(display_path)
                            touched_files.append(display_path)
                elif tool_name == "bash":
                    command = self._truncate_text(str(arguments.get("command") or "").strip(), max_chars=120)
                    if command:
                        artifact = f"Ran validation command: {command}"
                        if artifact not in seen_artifacts:
                            seen_artifacts.add(artifact)
                            validation_artifacts.append(artifact)
            elif event.get("event") == EngineEvent.TOOL_RESULT.value:
                tool_name = str(event.get("name") or "").strip()
                if tool_name == "bash":
                    output = self._truncate_text(str(event.get("output") or "").strip(), max_chars=220)
                    if output:
                        artifact = f"Bash result: {output}"
                        if artifact not in seen_artifacts:
                            seen_artifacts.add(artifact)
                            validation_artifacts.append(artifact)

        return touched_files[:6], validation_artifacts[:6]

    def extract_generator_edit_snippets(
        self,
        *,
        project_path: Optional[str] = None,
        display_events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        snippets: list[dict[str, str]] = []
        seen_files: set[str] = set()

        for event in display_events:
            if event.get("event") != EngineEvent.TOOL_CALL.value:
                continue
            if str(event.get("name") or "").strip() != "file_edit":
                continue
            arguments = self._extract_tool_arguments_from_event(event)
            raw_path = str(arguments.get("path") or "").strip()
            if not raw_path:
                continue
            display_path = os.path.relpath(
                str(self._resolve_harness_touched_path(target_path, raw_path)),
                target_path,
            ).replace(os.sep, "/")
            if display_path in seen_files:
                continue
            seen_files.add(display_path)
            snippets.append(
                {
                    "path": display_path,
                    "old_text": self._truncate_text(str(arguments.get("old_text") or "").strip(), max_chars=500),
                    "new_text": self._truncate_text(str(arguments.get("new_text") or "").strip(), max_chars=500),
                }
            )
            if len(snippets) >= 2:
                break

        return snippets

    def _preferred_harness_python(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path or os.getcwd())
        candidates = [
            Path(target_path) / ".venv" / "bin" / "python",
            self.resolve_local_coding_model_python(),
            Path("/opt/homebrew/bin/python3.11"),
            Path(sys.executable).resolve(),
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
        return "python3"

    def _sanitize_harness_validation_command(
        self,
        command: str,
        *,
        project_path: Optional[str] = None,
    ) -> str:
        cleaned = re.sub(r"\s+#.*$", "", str(command or "").strip()).strip()
        preferred_python = self._preferred_harness_python(project_path)
        for prefix in ("python3.11 ", "python3 ", "python "):
            if cleaned.startswith(prefix):
                cleaned = f"{preferred_python} {cleaned[len(prefix):]}"
                break
        return cleaned

    @staticmethod
    def _extract_validation_artifact_candidates(command: str) -> list[str]:
        candidates = re.findall(r"--(?:summary-output|summary|output)\s+([^\s|]+)", command)
        if "validation_summary.json" in command and "validation_summary.json" not in candidates:
            candidates.append("validation_summary.json")
        return candidates[:3]

    @staticmethod
    def _module_name_from_target_file(raw_path: str) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        if not path.endswith(".py"):
            return ""
        module = path[:-3].strip("/").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return module

    def _extract_harness_target_function_name(self, summary: dict[str, Any]) -> str:
        candidates = [
            *self._normalize_string_list(summary.get("acceptance_checks")),
            *self._normalize_string_list(summary.get("deliverables")),
            str(summary.get("contract_objective") or ""),
        ]
        patterns = (
            r"\bfunction\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\b",
            r"`([A-Za-z_][A-Za-z0-9_]*)\([^`]*\)`",
            r"\b([A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s+function\b",
        )
        for text in candidates:
            stripped = str(text or "").strip()
            if not stripped:
                continue
            for pattern in patterns:
                match = re.search(pattern, stripped, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
        return ""

    def _infer_static_acceptance_evidence(
        self,
        *,
        acceptance_checks: list[str],
        summary: dict[str, Any],
    ) -> dict[str, str]:
        target_files = {
            item.replace("\\", "/")
            for item in self._normalize_string_list(summary.get("target_files"))
        }
        touched_files = {
            item.replace("\\", "/")
            for item in self._normalize_string_list(summary.get("touched_files"))
        }
        aligned: dict[str, str] = {}
        for check in acceptance_checks:
            lowered = check.lower()
            if (
                "no other files" in lowered
                and "modified" in lowered
                and touched_files
                and (not target_files or touched_files.issubset(target_files))
            ):
                aligned[check] = self._truncate_text(
                    "Only recorded touched files: " + ", ".join(sorted(touched_files)),
                    max_chars=220,
                )
        return aligned

    def _acceptance_evidence_covers_contract(
        self,
        *,
        acceptance_checks: list[str],
        evidence: dict[str, str],
    ) -> bool:
        if not acceptance_checks:
            return False
        normalized = {
            self._normalize_acceptance_check_phrase(key)
            for key, value in evidence.items()
            if self._normalize_acceptance_check_phrase(key) and str(value or "").strip()
        }
        return all(
            self._normalize_acceptance_check_phrase(check) in normalized
            for check in acceptance_checks
        )

    def _build_derived_harness_validation_commands(
        self,
        *,
        project_path: Optional[str] = None,
        summary: dict[str, Any],
    ) -> list[str]:
        target_files = self._normalize_string_list(summary.get("target_files")) or self._normalize_string_list(
            summary.get("touched_files")
        )
        if len(target_files) != 1:
            return []
        target_file = target_files[0]
        module_basename = Path(target_file).stem
        module_dir = Path(target_file).parent.as_posix() or "."
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_basename):
            return []
        function_name = self._extract_harness_target_function_name(summary)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function_name):
            return []

        python_bin = shlex.quote(self._preferred_harness_python(project_path))
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        derived: list[str] = []
        for check in acceptance_checks:
            lowered = check.lower()
            if "callable" in lowered:
                code = (
                    f"import pathlib, sys; sys.path.insert(0, str(pathlib.Path({module_dir!r}).resolve())); "
                    f"from {module_basename} import {function_name}; "
                    f"print('CALLABLE_OK' if callable({function_name}) else 'CALLABLE_FAIL')"
                )
                derived.append(f"{python_bin} -c {shlex.quote(code)}")
            if "empty dict" in lowered and "no duplicates" in lowered:
                code = (
                    f"import pathlib, sys; sys.path.insert(0, str(pathlib.Path({module_dir!r}).resolve())); "
                    f"from {module_basename} import {function_name}; "
                    f"result = {function_name}([{{'id': 'alpha'}}, {{'id': 'beta'}}]); "
                    "print('EMPTY_OK' if result == {} else repr(result))"
                )
                derived.append(f"{python_bin} -c {shlex.quote(code)}")

        deduped: list[str] = []
        for command in derived:
            if command not in deduped:
                deduped.append(command)
        return deduped[:2]

    @staticmethod
    def _validation_command_has_placeholder(command: str) -> bool:
        text = str(command or "")
        return bool(re.search(r"<[^>\n]+>", text))

    def apply_generator_post_patch_safety_gate(
        self,
        *,
        project_path: Optional[str] = None,
        payload: dict[str, Any],
        generator_mode: str,
        display_events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        if generator_mode not in {"patch", "repair", "structured"}:
            return payload, ""
        if not isinstance(payload, dict):
            return payload, ""

        generator_payload = payload.get("generator_update") if isinstance(payload.get("generator_update"), dict) else {}
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        if not progress and isinstance(generator_payload.get("progress"), dict):
            progress = dict(generator_payload.get("progress") or {})
        else:
            progress = dict(progress or {})

        summary = self.get_harness_summary(target_path)
        raw_touched = self._normalize_string_list(progress.get("touched_files")) or self._normalize_string_list(
            summary.get("target_files")
        )
        python_targets: list[tuple[str, Path]] = []
        seen_targets: set[str] = set()
        for raw_path in raw_touched:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            if resolved.suffix != ".py" or not resolved.exists() or not resolved.is_file():
                continue
            display_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
            if display_path in seen_targets:
                continue
            seen_targets.add(display_path)
            python_targets.append((display_path, resolved))
        if not python_targets:
            return payload, ""

        python_bin = self._preferred_harness_python(target_path)
        command = [python_bin, "-m", "py_compile", *[str(path) for _, path in python_targets[:2]]]
        try:
            completed = subprocess.run(
                command,
                cwd=target_path,
                text=True,
                capture_output=True,
                timeout=20,
            )
            output = "\n".join(
                part for part in (str(completed.stdout or "").strip(), str(completed.stderr or "").strip()) if part
            ).strip()
        except Exception as exc:
            completed = None
            output = f"Failed to start syntax gate: {exc}"

        if completed is not None and completed.returncode == 0:
            return payload, ""

        edit_snippets = self.extract_generator_edit_snippets(project_path=target_path, display_events=display_events)
        validation_checks = self._normalize_string_list(progress.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(progress.get("validation_artifacts"))
        blockers = self._normalize_string_list(progress.get("blockers"))
        gate_output = self._truncate_text(output or "py_compile failed without output", max_chars=260)
        gate_command = " ".join(command)
        gate_message = self._truncate_text(f"Post-patch syntax gate failed: {gate_output}", max_chars=220)

        if gate_message not in validation_checks:
            validation_checks.append(gate_message)
        gate_artifacts = [
            self._truncate_text(f"Post-patch syntax gate command: {gate_command}", max_chars=220),
            gate_message,
        ]
        for snippet in edit_snippets:
            path = snippet.get("path") or "(unknown)"
            old_text = str(snippet.get("old_text") or "").strip()
            new_text = str(snippet.get("new_text") or "").strip()
            if old_text:
                gate_artifacts.append(f"Edited snippet before ({path}):\n{old_text}")
            if new_text:
                gate_artifacts.append(f"Edited snippet after ({path}):\n{new_text}")

        merged_artifacts: list[str] = []
        for artifact in [*gate_artifacts, *validation_artifacts]:
            if artifact and artifact not in merged_artifacts:
                merged_artifacts.append(artifact)

        blocker = "Fix the syntax/runtime failure before claiming implementation."
        if blocker not in blockers:
            blockers.append(blocker)

        summary_text = self._truncate_text(
            f"Patch introduced a blocking syntax/runtime failure in {python_targets[0][0]}. Repair is required.",
            max_chars=220,
        )
        progress.update(
            {
                "summary": summary_text,
                "last_validation": gate_message,
                "validation_checks": validation_checks[:8],
                "validation_artifacts": merged_artifacts[:8],
                "acceptance_evidence": {},
                "blockers": blockers[:4],
                "next_steps": [
                    "Repair the broken patch in the target file only.",
                    "Rerun the validation command after the repair.",
                ],
                "current_phase": "blocked",
            }
        )
        payload["progress"] = progress
        payload["sprint_status"] = "failed"
        payload["handoff_markdown"] = "\n".join(
            [
                "# Repair required",
                "",
                f"## Summary\n{summary_text}",
                "",
                "## Blocking validation",
                f"- {gate_message}",
                "",
                "## Target files",
                *[f"- `{path}`" for path, _ in python_targets[:4]],
            ]
        )
        if isinstance(generator_payload, dict):
            generator_payload["progress"] = progress
            generator_payload["sprint_status"] = "failed"
            generator_payload["handoff_markdown"] = payload.get("handoff_markdown", "")
            payload["generator_update"] = generator_payload

        return payload, gate_message

    @staticmethod
    def _extract_edit_snippet_artifacts(summary: dict[str, Any]) -> list[str]:
        snippets: list[str] = []
        for item in summary.get("validation_artifacts") or []:
            text = str(item or "").strip()
            if text.startswith("Edited snippet before") or text.startswith("Edited snippet after"):
                snippets.append(text)
        return snippets[:4]

    def run_harness_generator_validation_probes(
        self,
        *,
        project_path: Optional[str] = None,
        summary: dict[str, Any],
    ) -> tuple[list[str], list[str], dict[str, str]]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        commands = self._normalize_string_list(summary.get("validation_commands"))
        for command in self._build_derived_harness_validation_commands(project_path=target_path, summary=summary):
            if command not in commands:
                commands.append(command)
        if not commands:
            return [], [], self._infer_static_acceptance_evidence(
                acceptance_checks=acceptance_checks,
                summary=summary,
            )

        revision_focus = " ".join(
            self._normalize_string_list(summary.get("required_revisions"))
            + self._normalize_string_list(summary.get("next_steps"))
            + acceptance_checks
        ).lower()

        ranked_commands: list[tuple[int, str]] = []
        for index, raw_command in enumerate(commands):
            command = self._sanitize_harness_validation_command(raw_command, project_path=target_path)
            if not command:
                continue
            lowered = command.lower()
            score = 0
            if any(token in lowered for token in ("whitespace", "missing", "empty", "grep", "/tmp/test_", "echo '{")):
                score += 3
            if any(token in lowered for token in ("--summary-output", "--summary", "validation_summary.json")):
                score += 2
            if "whitespace" in revision_focus and "whitespace" in lowered:
                score += 3
            if "missing" in revision_focus and "missing" in lowered:
                score += 2
            if any(token in revision_focus for token in ("valid", "pass validation", "without errors")) and any(
                token in lowered for token in ("/tmp/test_valid", "valid")
            ):
                score += 2
            if any(token in lowered for token in ("python", "pytest", "uv", "bash")):
                score += 1
            score += max(0, 2 - index)
            ranked_commands.append((score, command))

        preferred: list[str] = []
        for _, command in sorted(ranked_commands, key=lambda item: (-item[0], item[1])):
            if command not in preferred:
                preferred.append(command)

        validation_checks: list[str] = []
        validation_artifacts: list[str] = []
        acceptance_evidence: dict[str, str] = self._infer_static_acceptance_evidence(
            acceptance_checks=acceptance_checks,
            summary=summary,
        )

        for command in preferred[:3]:
            if self._validation_command_has_placeholder(command):
                validation_artifacts.append(
                    self._truncate_text(
                        f"Skipped placeholder validation command: {command}",
                        max_chars=220,
                    )
                )
                continue
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=target_path,
                    text=True,
                    capture_output=True,
                    timeout=25,
                )
            except Exception as exc:
                validation_artifacts.append(self._truncate_text(f"Auto validation failed to start: {exc}", max_chars=220))
                continue

            output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part).strip()
            output_lower = output.lower()
            unusable_failure = completed.returncode != 0 and any(
                token in output_lower
                for token in (
                    "syntax error near unexpected token",
                    "command not found",
                    "no such file or directory",
                )
            )
            validation_artifacts.append(self._truncate_text(f"Auto validation command: {command}", max_chars=220))
            validation_artifacts.append(
                self._truncate_text(
                    f"Auto validation exit={completed.returncode}: {output or '[no output]'}",
                    max_chars=260,
                )
            )

            if completed.returncode == 0:
                validation_checks.append(self._truncate_text(f"Validation succeeded: {command}", max_chars=160))
                if "CALLABLE_OK" in output:
                    for check in acceptance_checks:
                        if "callable" in check.lower():
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` confirmed the function is callable.", max_chars=180),
                            )
                if "EMPTY_OK" in output:
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if "empty dict" in lowered and "no duplicates" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` returned {{}} for non-duplicate input.", max_chars=180),
                            )
                if re.search(r"\b[a-z0-9._-]+\s*:\s*\d+\b", output, re.IGNORECASE):
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if "occurrence count" in lowered or "count for each duplicated id" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` printed duplicate ids with counts.", max_chars=180),
                            )
                        if "duplicate id" in lowered or "duplicate ids" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` printed duplicate ids in the validation output.", max_chars=180),
                            )
                for check in acceptance_checks:
                    lowered = check.lower()
                    if any(token in lowered for token in ("exits without error", "without error", "exit without error")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0.", max_chars=140),
                        )
                    if any(token in lowered for token in ("validates successfully", "exit code 0", "no change in behavior")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0.", max_chars=140),
                        )
                    if any(token in lowered for token in ("valid training", "pass validation", "without errors")) and any(
                        token in command.lower() for token in ("test_valid", "valid")
                    ):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0 for the valid fixture.", max_chars=160),
                        )
            else:
                validation_checks.append(
                    self._truncate_text(f"Validation failed with exit {completed.returncode}: {command}", max_chars=180)
                )
                if unusable_failure:
                    continue
                for check in acceptance_checks:
                    lowered = check.lower()
                    if any(token in lowered for token in ("non-zero exit code", "exit with error", "exit code is non-zero")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited {completed.returncode}.", max_chars=140),
                        )
                    if "whitespace" in lowered and "whitespace" in output_lower:
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported whitespace-only content.", max_chars=160),
                        )
                    if any(token in lowered for token in ("missing", "empty content", "empty strings", "empty")) and any(
                        token in output_lower for token in ("missing", "empty")
                    ):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported missing or empty content.", max_chars=160),
                        )
                    if "line number" in lowered and re.search(r":\d+:", output):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported a file:line diagnostic.", max_chars=160),
                        )

            for raw_candidate in self._extract_validation_artifact_candidates(command):
                resolved = self._resolve_harness_touched_path(target_path, raw_candidate)
                if not resolved.exists() or not resolved.is_file():
                    continue
                rel_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
                validation_artifacts.append(f"Validation artifact created: {rel_path}")
                try:
                    payload = json.loads(resolved.read_text(encoding="utf-8"))
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    keys = sorted(str(key) for key in payload.keys())
                    validation_checks.append(self._truncate_text(f"{rel_path} keys: {', '.join(keys)}", max_chars=180))
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if any(token in lowered for token in ("json file is created", "summary json", "artifact is written", "artifact is produced")):
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{rel_path}` was created and parsed as JSON.", max_chars=160),
                            )
                        if "counts are accurate" in lowered:
                            total = payload.get("total_files_checked") or payload.get("total_files")
                            passed = payload.get("passed_files")
                            failed = payload.get("failed_files")
                            if all(isinstance(item, int) for item in (total, passed, failed)) and passed + failed == total:
                                acceptance_evidence.setdefault(
                                    check,
                                    f"`{rel_path}` reports total={total}, passed={passed}, failed={failed}.",
                                )

        return validation_checks[:6], validation_artifacts[:6], acceptance_evidence

    def infer_generator_structured_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
        prompt: str = "",
        display_events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None
        if not self.should_use_harness_generator_structured_mode(target_path, prompt):
            return None

        touched_files, event_artifacts = self.extract_generator_structured_event_summary(
            project_path=target_path,
            display_events=display_events,
        )
        if not touched_files:
            return None

        stripped = str(text or "").strip()
        lines = [
            self._strip_list_marker(line)
            for line in stripped.splitlines()
            if self._strip_list_marker(line)
        ]
        findings: list[str] = []
        seen_findings: set[str] = set()
        for line in lines:
            normalized = line.strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen_findings:
                continue
            seen_findings.add(lowered)
            findings.append(self._truncate_text(normalized, max_chars=260))
            if len(findings) >= 6:
                break

        if not findings:
            findings = [f"Implemented the requested narrow change in {', '.join(touched_files[:3])}."]

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        acceptance_evidence: dict[str, str] = {}
        for check in acceptance_checks[:8]:
            supporting = self._choose_supporting_line_for_check(check, findings)
            if supporting:
                acceptance_evidence[check] = self._truncate_text(supporting, max_chars=220)

        validation_checks = [f"Updated file scope: {', '.join(touched_files[:4])}"]
        validation_checks.extend(findings[:3])
        validation_artifacts = list(event_artifacts[:4])
        if not validation_artifacts:
            validation_artifacts.append(
                f"Compact structured generator updated {', '.join(touched_files[:4])}."
            )
        combined_probe_seed = "\n".join(
            [
                "\n".join(findings),
                "\n".join(event_artifacts),
                "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            ]
        )
        existing_coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_probe_seed)
        should_probe = not event_artifacts or any(not item.get("matched") for item in existing_coverage)
        if should_probe:
            probe_checks, probe_artifacts, probe_evidence = self.run_harness_generator_validation_probes(
                project_path=target_path,
                summary=summary,
            )
            validation_checks.extend(item for item in probe_checks if item not in validation_checks)
            validation_artifacts.extend(item for item in probe_artifacts if item not in validation_artifacts)
            for check, evidence in probe_evidence.items():
                acceptance_evidence.setdefault(check, evidence)

        filtered_findings = [
            item
            for item in findings
            if item
            and item not in {"```json", "```", "{", "}"}
            and not item.startswith("\"name\":")
            and not item.startswith("\"arguments\":")
            and not item.startswith("\"path\":")
            and not item.startswith("\"old_text\":")
            and not item.startswith("\"new_text\":")
        ]
        findings = filtered_findings or [f"Implemented the requested narrow change in {', '.join(touched_files[:3])}."]

        summary_text = self._truncate_text(" | ".join(findings[:2]), max_chars=220)
        handoff_lines = [
            "# Structured implementation handoff",
            "",
            "## Summary",
            summary_text or "Completed a compact structured implementation update.",
            "",
            "## Touched files",
        ]
        handoff_lines.extend(f"- `{path}`" for path in touched_files[:6])
        handoff_lines.extend(["", "## Findings"])
        handoff_lines.extend(f"- {item}" for item in findings)

        return {
            "action": "generator_update",
            "progress": {
                "summary": summary_text or "Completed the compact structured sprint update.",
                "last_validation": event_artifacts[0] if event_artifacts else "Completed a compact structured implementation update.",
                "touched_files": touched_files[:6],
                "validation_checks": validation_checks[:6],
                "validation_artifacts": validation_artifacts[:6],
                "acceptance_evidence": acceptance_evidence,
                "current_phase": "implementation",
            },
            "handoff_markdown": "\n".join(handoff_lines),
            "sprint_status": "implemented",
        }

    def can_use_harness_structured_evaluator(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        if not touched_files:
            return False
        bundle = self.build_harness_structured_evidence_bundle(target_path)
        return any(item.get("exists") for item in bundle["files"])

    def can_use_harness_explicit_artifact_evaluator(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(target_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        if not acceptance_checks:
            return False
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        if not acceptance_evidence:
            return False
        normalized_keys = {
            self._normalize_acceptance_check_phrase(check)
            for check in acceptance_evidence.keys()
            if self._normalize_acceptance_check_phrase(check)
        }
        covered = [
            check for check in acceptance_checks
            if self._normalize_acceptance_check_phrase(check) in normalized_keys
        ]
        if len(covered) != len(acceptance_checks):
            return False

        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        last_validation = str(summary.get("last_validation") or "").strip()
        return bool(last_validation or validation_checks or validation_artifacts)

    def get_harness_evaluator_strategy(self, project_path: Optional[str] = None) -> str:
        mode = self.get_harness_evaluator_mode()
        if mode == "full":
            return "full"
        if mode == "artifacts":
            return "artifacts"
        if mode == "structured":
            if self.can_use_harness_structured_evaluator(project_path):
                return "structured"
            if self.can_use_harness_explicit_artifact_evaluator(project_path):
                return "artifacts"
            return "full"
        if self.should_use_harness_artifact_evaluator(project_path):
            return "artifacts"
        if self.can_use_harness_structured_evaluator(project_path):
            return "structured"
        if self.can_use_harness_explicit_artifact_evaluator(project_path):
            return "artifacts"
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
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
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
        if validation_artifacts:
            lines.append("Validation artifacts:")
            lines.extend(f"- {item}" for item in validation_artifacts[:12])
        if acceptance_evidence:
            lines.append("Explicit acceptance evidence:")
            lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
                for check, evidence in list(acceptance_evidence.items())[:8]
            )

        validation_check_lines = [f"- {item}" for item in validation_checks[:12]] or ["(none)"]
        validation_artifact_lines = [f"- {item}" for item in validation_artifacts[:12]] or ["(none)"]
        acceptance_evidence_lines = [
            f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
            for check, evidence in list(acceptance_evidence.items())[:8]
        ] or ["(none)"]

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
                *validation_check_lines,
                "",
                "Validation artifacts:",
                *validation_artifact_lines,
                "",
                "Explicit acceptance evidence:",
                *acceptance_evidence_lines,
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
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        validation_check_lines = [f"- {item}" for item in validation_checks[:12]] or ["(none)"]
        validation_artifact_lines = [f"- {item}" for item in validation_artifacts[:12]] or ["(none)"]
        acceptance_evidence_lines = [
            f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
            for check, evidence in list(acceptance_evidence.items())[:8]
        ] or ["(none)"]

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
                *validation_check_lines,
                "",
                "Validation artifacts:",
                *validation_artifact_lines,
                "",
                "Explicit acceptance evidence:",
                *acceptance_evidence_lines,
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
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        last_validation = str(summary.get("last_validation") or "").strip()
        handoff_excerpt = self._truncate_text(harness.read_handoff(), max_chars=1200)
        evidence_present = bool(last_validation or validation_checks or validation_artifacts or acceptance_evidence or handoff_excerpt)

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

        normalized_evidence = {
            self._normalize_acceptance_check_phrase(check): evidence
            for check, evidence in acceptance_evidence.items()
            if self._normalize_acceptance_check_phrase(check) and str(evidence).strip()
        }
        explicit_matches = []
        for check in acceptance_checks:
            normalized_check = self._normalize_acceptance_check_phrase(check)
            if normalized_check and normalized_check in normalized_evidence:
                explicit_matches.append(check)

        matched_checks = []
        for item in coverage:
            check = str(item.get("check") or "")
            if item.get("matched") or check in explicit_matches:
                matched_checks.append(check)
        unmatched_checks = [check for check in acceptance_checks if check not in matched_checks]
        has_complete_coverage = bool(coverage) and not unmatched_checks

        if has_complete_coverage and evidence_present and (
            evaluation_mode != "structured" or existing_file_count > 0
        ):
            findings = (
                validation_checks[:3]
                or validation_artifacts[:3]
                or list(acceptance_evidence.values())[:3]
                or [last_validation or "Acceptance checks are covered by the harness evidence bundle."]
            )
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
            elif unmatched_checks and not validation_checks and not validation_artifacts and not acceptance_evidence and (
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

    def _coerce_evaluator_verdict_payload(
        self,
        *,
        payload: dict[str, Any],
        harness: HarnessWorkspace,
        assistant_text: str = "",
    ) -> dict[str, Any]:
        current_contract = harness.read_sprint_contract()
        sprint_id = str(payload.get("sprint_id") or current_contract.sprint_id).strip()
        verdict = str(
            payload.get("verdict")
            or payload.get("evaluator_verdict")
            or payload.get("evaluation_verdict")
            or payload.get("status")
            or payload.get("result")
            or ""
        ).strip().lower()
        required_revisions = self._normalize_string_list(
            payload.get("required_revisions") or payload.get("required_actions")
        )
        findings = self._normalize_string_list(payload.get("findings"))
        passed_checks = self._normalize_string_list(payload.get("passed_checks"))
        failed_checks = self._normalize_string_list(payload.get("failed_checks"))

        rationale = str(
            payload.get("rationale")
            or payload.get("reason")
            or payload.get("explanation")
            or payload.get("notes")
            or payload.get("summary")
            or ""
        ).strip()
        combined = "\n".join(
            part
            for part in (
                verdict,
                rationale,
                assistant_text,
                "\n".join(required_revisions),
                "\n".join(findings),
            )
            if str(part or "").strip()
        ).lower()

        if verdict not in {"pass", "revise", "blocked"}:
            if any(
                token in combined
                for token in (
                    "blocked",
                    "hard blocker",
                    "cannot proceed",
                    "does not run",
                    "fails immediately",
                    "syntaxerror",
                    "indentationerror",
                    "unexpected indent",
                )
            ):
                verdict = "blocked"
            elif required_revisions or any(
                token in combined
                for token in (
                    "revise",
                    "revising",
                    "needs revision",
                    "required action",
                    "cannot verify",
                    "missing evidence",
                    "insufficient evidence",
                    "not enough evidence",
                    "not pass",
                    "not passable",
                    "does not pass",
                    "not supported from the evidence",
                    "not supported by the evidence",
                )
            ):
                verdict = "revise"
            elif re.search(r"\bpassed\b", combined) or re.search(r"\bpass\b", combined) or any(
                token in combined
                for token in (
                    "all acceptance checks are satisfied",
                    "satisfies the acceptance checks",
                    "passes evaluator checks",
                    "no revisions are needed",
                    "no revision is needed",
                )
            ):
                verdict = "pass"

        if not findings and rationale:
            findings = [self._truncate_text(rationale, max_chars=220)]
        if not findings and assistant_text:
            findings = [
                self._truncate_text(line, max_chars=220)
                for line in self._normalize_string_list(assistant_text)[:3]
            ]
        if verdict == "pass" and not passed_checks:
            passed_checks = self._normalize_string_list(current_contract.acceptance_checks)[:8]
        if verdict in {"revise", "blocked"} and not failed_checks:
            failed_checks = required_revisions[:8]
        if verdict == "blocked" and not required_revisions:
            required_revisions = failed_checks[:8] or [
                "Clear the blocker or add enough evaluation evidence to support a verdict."
            ]
        if verdict == "revise" and not required_revisions:
            required_revisions = failed_checks[:8] or [
                "Add clearer evidence or validation output for the unmet acceptance checks."
            ]

        score = payload.get("score")
        if score is None and verdict in {"pass", "revise", "blocked"}:
            score = {"pass": 1.0, "revise": 0.5, "blocked": 0.0}[verdict]

        return {
            "sprint_id": sprint_id,
            "verdict": verdict,
            "findings": findings[:8],
            "required_revisions": required_revisions[:8],
            "passed_checks": passed_checks[:8],
            "failed_checks": failed_checks[:8],
            "score": score,
        }

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
        if not self.available_backends:
            self.detect_backends()
        codex_cli = _find_cli("codex")
        claude_cli = _find_cli("claude")
        forced_provider = str(os.environ.get("RESONANT_HARNESS_TEACHER_PROVIDER", "") or "").strip()
        forced_model = str(os.environ.get("RESONANT_HARNESS_TEACHER_MODEL", "") or "").strip()

        if forced_provider:
            if forced_provider == "codex" and codex_cli:
                return forced_provider, forced_model or "gpt-5.4"
            if forced_provider == "claude" and claude_cli:
                return forced_provider, forced_model or "claude-opus-4-6"
            raise ValueError(f"Forced harness teacher provider '{forced_provider}' is not available")

        prefer_claude = normalized_role == "evaluator" or "blocked" in lowered_reason or "verdict" in lowered_reason
        providers: list[tuple[str, str]] = []
        if prefer_claude:
            providers.extend(
                [
                    ("claude", "claude-opus-4-6"),
                    ("codex", "gpt-5.4-mini"),
                    ("codex", "gpt-5.4"),
                ]
            )
        else:
            providers.extend(
                [
                    ("codex", "gpt-5.4-mini"),
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
        if isinstance(value, dict):
            result = []
            for raw_key, raw_value in value.items():
                key = str(raw_key).strip()
                val = str(raw_value).strip()
                text = f"{key}: {val}" if key and val else key or val
                text = text.strip()
                if text:
                    result.append(text)
            return result
        if isinstance(value, (list, tuple, set)):
            result = []
            for item in value:
                text = str(item).strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    @classmethod
    def _normalize_contract_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return cls._normalize_string_list(value)
        if isinstance(value, (list, tuple, set)):
            result: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    title = str(item.get("title") or item.get("id") or "").strip()
                    description = str(item.get("description") or item.get("objective") or "").strip()
                    acceptance = str(item.get("acceptance") or "").strip()
                    parts = [part for part in (title, description) if part]
                    text = ": ".join(parts) if parts else ""
                    if acceptance:
                        text = f"{text} Acceptance: {acceptance}" if text else f"Acceptance: {acceptance}"
                    text = cls._truncate_text(text, max_chars=320)
                    if text:
                        result.append(text)
                    continue
                text = cls._truncate_text(str(item).strip(), max_chars=320)
                if text:
                    result.append(text)
            return result
        text = cls._truncate_text(str(value).strip(), max_chars=320)
        return [text] if text else []

    @staticmethod
    def _extract_explicit_harness_objective_text(text: str) -> str:
        raw = str(text or "")
        if not raw.strip():
            return ""
        match = re.search(
            r"TOP-LEVEL OBJECTIVE:\s*(.*?)\s*(?:OBJECTIVE HANDLING RULE:|$)",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()
        return raw.strip()

    @classmethod
    def _is_read_only_harness_request(cls, *texts: str) -> bool:
        combined = " ".join(
            cls._extract_explicit_harness_objective_text(text).lower()
            for text in texts
            if cls._extract_explicit_harness_objective_text(text)
        )
        if not combined:
            return False
        tokens = (
            "read-only",
            "read only",
            "read files only",
            "do not modify repository files",
            "do not modify files",
            "no code changes",
            "without making changes",
            "audit only",
            "inspect only",
            "capture findings through harness summary and handoff artifacts only",
            "read-only objective",
        )
        return any(token in combined for token in tokens)

    @classmethod
    def _sanitize_read_only_contract(
        cls,
        *,
        user_request: str,
        objective: str,
        feature_name: str,
        deliverables: list[str],
        acceptance_checks: list[str],
        evaluator_focus: list[str],
    ) -> tuple[str, list[str], list[str], list[str], bool]:
        if not cls._is_read_only_harness_request(user_request, objective, feature_name):
            return objective, deliverables, acceptance_checks, evaluator_focus, False

        write_tokens = (
            "create ",
            "write ",
            "modify ",
            "update ",
            "edit ",
            "patch ",
            "implement ",
            "test file",
            "test files",
            "unit test",
            "pytest",
            "script/",
            "scripts/",
            "production code",
            "code change",
        )

        def keep_item(text: str) -> bool:
            lowered = text.lower()
            return not any(token in lowered for token in write_tokens)

        sanitized_deliverables = [item for item in deliverables if keep_item(item)][:4]
        sanitized_acceptance = [item for item in acceptance_checks if keep_item(item)][:6]
        sanitized_focus = [item for item in evaluator_focus if keep_item(item)][:5]

        sanitized_objective = objective.strip()
        if sanitized_objective and "read-only" not in sanitized_objective.lower():
            sanitized_objective = f"Read-only audit. {sanitized_objective}"

        if not sanitized_deliverables:
            sanitized_deliverables = [
                "Inspect only the referenced files and capture grounded findings in harness handoff or progress artifacts.",
                "Record concise file evidence and line references for the accepted findings.",
            ]

        if not sanitized_acceptance:
            sanitized_acceptance = [
                "Handoff or progress artifacts reference the audited files by name.",
                "Findings distinguish the requested behaviors with concrete code evidence.",
                "No repository files are modified; the sprint stays read-only.",
            ]
        elif not any("read-only" in item.lower() or "no repository files" in item.lower() for item in sanitized_acceptance):
            sanitized_acceptance.append("No repository files are modified; the sprint stays read-only.")

        if not sanitized_focus:
            sanitized_focus = [
                "Verify the findings are grounded in the audited files with direct evidence.",
                "Reject the sprint if it introduced file modifications or test-writing work.",
            ]

        changed = (
            sanitized_objective != objective
            or sanitized_deliverables != deliverables
            or sanitized_acceptance != acceptance_checks
            or sanitized_focus != evaluator_focus
        )
        return sanitized_objective, sanitized_deliverables, sanitized_acceptance, sanitized_focus, changed

    @staticmethod
    def _normalize_string_mapping(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        result: dict[str, str] = {}
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = str(raw_key).strip()
                if isinstance(raw_value, bool):
                    val = "PASS" if raw_value else ""
                else:
                    val = str(raw_value).strip()
                if key and val:
                    result[key] = val
            return result
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, dict):
                    key = str(item.get("check") or item.get("key") or "").strip()
                    val = str(item.get("evidence") or item.get("value") or "").strip()
                else:
                    key = str(item).strip()
                    val = key
                if key and val:
                    result[key] = val
            return result
        return {}

    @staticmethod
    def normalize_harness_contract_status(status: str, *, session_role: str) -> str:
        return HarnessService.normalize_contract_status(status, session_role=session_role)

    @staticmethod
    def normalize_harness_action(action: str, *, session_role: str) -> str:
        raw = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not raw:
            return ""
        aliases = {
            "planner_update": "planner_update",
            "plan_update": "planner_update",
            "plan_complete": "planner_update",
            "planning_complete": "planner_update",
            "sprint_definition_complete": "planner_update",
            "sprint_defined": "planner_update",
            "sprint_contract_complete": "planner_update",
            "generator_update": "generator_update",
            "implementation_update": "generator_update",
            "implementation_complete": "generator_update",
            "code_update": "generator_update",
            "repair_update": "generator_update",
            "repair_complete": "generator_update",
            "repair_result": "generator_update",
            "evaluator_verdict": "evaluator_verdict",
            "evaluation_verdict": "evaluator_verdict",
            "evaluation_complete": "evaluator_verdict",
            "evaluation_result": "evaluator_verdict",
            "verdict": "evaluator_verdict",
        }
        if raw in aliases:
            return aliases[raw]
        if raw in {"complete", "completed", "done"}:
            if session_role == "planner":
                return "planner_update"
            if session_role == "generator":
                return "generator_update"
            if session_role == "evaluator":
                return "evaluator_verdict"
        if session_role == "planner" and any(token in raw for token in ("plan", "planner", "sprint", "contract")):
            return "planner_update"
        if session_role == "generator" and any(token in raw for token in ("generate", "generator", "implement", "code", "patch", "edit")):
            return "generator_update"
        if session_role == "evaluator" and any(token in raw for token in ("evaluate", "evaluator", "verdict", "review", "check")):
            return "evaluator_verdict"
        return raw

    def _normalize_harness_validation_commands(
        self,
        value: Any,
        *,
        project_path: Optional[str] = None,
    ) -> list[str]:
        commands: list[str] = []
        for item in self._normalize_string_list(value):
            cleaned = self._sanitize_harness_validation_command(item, project_path=project_path)
            if cleaned and self._looks_like_shell_command(cleaned) and cleaned not in commands:
                commands.append(cleaned)
        return commands[:6]

    @staticmethod
    def _looks_like_shell_command(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        shell_starts = (
            "python",
            "python3",
            "pytest",
            "node",
            "npm",
            "npx",
            "uv",
            "bash",
            "sh ",
            "./",
            "cat ",
            "grep ",
            "rg ",
            "ls ",
        )
        if text.startswith(shell_starts):
            return True
        return any(token in text for token in (" --", " | ", " && ", " > ", "2>&1", "echo $?"))

    def _extract_planner_contract_payload(
        self,
        *,
        payload: dict[str, Any],
        planner_payload: dict[str, Any],
        current_contract: Any,
    ) -> dict[str, Any]:
        candidate_contracts = [
            payload.get("sprint_contract"),
            payload.get("sprint"),
            payload.get("next_sprint_contract"),
            payload.get("contract"),
            planner_payload.get("sprint_contract"),
            planner_payload.get("sprint"),
            planner_payload.get("next_sprint_contract"),
            planner_payload.get("next_contract"),
            planner_payload.get("contract"),
        ]
        contract_data = next((item for item in candidate_contracts if isinstance(item, dict)), {})
        contract = dict(contract_data) if isinstance(contract_data, dict) else {}

        direct_fields = (
            "sprint_id",
            "feature_name",
            "objective",
            "deliverables",
            "acceptance_checks",
            "acceptance_focus",
            "evaluator_focus",
            "target_files",
            "target_line_hints",
            "validation_commands",
            "edit_strategy",
            "status",
        )
        for source in (planner_payload, payload):
            if not isinstance(source, dict):
                continue
            for key in direct_fields:
                if key not in contract and source.get(key) not in (None, "", [], {}):
                    contract[key] = source.get(key)

        scope = contract.get("scope")
        if not isinstance(scope, dict):
            scope = planner_payload.get("scope") if isinstance(planner_payload.get("scope"), dict) else {}
        if scope:
            if "target_files" not in contract and scope.get("target_files") not in (None, "", [], {}):
                contract["target_files"] = scope.get("target_files")
            if "target_line_hints" not in contract:
                for key in ("target_line_hints", "line_hints", "line_targets"):
                    if scope.get(key) not in (None, "", [], {}):
                        contract["target_line_hints"] = scope.get(key)
                        break
            if "edit_strategy" not in contract:
                for key in ("edit_strategy", "change_type", "approach"):
                    value = str(scope.get(key) or "").strip()
                    if value:
                        contract["edit_strategy"] = value
                        break

        if "acceptance_checks" not in contract and contract.get("acceptance_focus") not in (None, "", [], {}):
            contract["acceptance_checks"] = contract.get("acceptance_focus")
        if "evaluator_focus" not in contract and planner_payload.get("evaluator_checks") not in (None, "", [], {}):
            contract["evaluator_focus"] = planner_payload.get("evaluator_checks")
        if "deliverables" not in contract and planner_payload.get("key_constraints") not in (None, "", [], {}):
            contract["deliverables"] = planner_payload.get("key_constraints")
        if "validation_commands" not in contract:
            for key in ("validation_plan", "validation_steps", "validation_approach"):
                if planner_payload.get(key) not in (None, "", [], {}):
                    contract["validation_commands"] = planner_payload.get(key)
                    break
        if "feature_name" not in contract:
            title = str(planner_payload.get("title") or payload.get("title") or "").strip()
            if title:
                contract["feature_name"] = title
        if "status" not in contract:
            for key in ("contract_status", "status", "phase"):
                value = str(planner_payload.get(key) or payload.get(key) or "").strip()
                if value:
                    contract["status"] = value
                    break

        if "sprint_id" not in contract and current_contract.sprint_id:
            contract["sprint_id"] = current_contract.sprint_id
        if "feature_name" not in contract and current_contract.feature_name:
            contract["feature_name"] = current_contract.feature_name
        if "objective" not in contract and current_contract.objective:
            contract["objective"] = current_contract.objective
        return contract

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
        action = self.normalize_harness_action(
            payload.get("action") or payload.get("event") or "",
            session_role=session_role,
        )

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
            planner_payload = payload.get("planner_update") if isinstance(payload.get("planner_update"), dict) else {}
            if not isinstance(planner_payload, dict):
                planner_payload = {}

            spec_data = (
                payload.get("spec")
                or planner_payload.get("spec")
                or planner_payload.get("spec_updates")
                or {}
            )
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

            current_contract = harness.read_sprint_contract()
            contract_data = self._extract_planner_contract_payload(
                payload=payload,
                planner_payload=planner_payload,
                current_contract=current_contract,
            )
            if isinstance(contract_data, dict):
                sprint_id = str(contract_data.get("sprint_id") or current_contract.sprint_id).strip()
                objective = str(contract_data.get("objective") or current_contract.objective).strip()
                feature_name = str(contract_data.get("feature_name") or current_contract.feature_name).strip()
                deliverables = self._normalize_contract_list(
                    contract_data.get("deliverables", current_contract.deliverables)
                )
                acceptance_checks = self._normalize_contract_list(
                    contract_data.get(
                        "acceptance_checks",
                        contract_data.get("acceptance_focus", current_contract.acceptance_checks),
                    )
                )
                evaluator_focus = self._normalize_contract_list(
                    contract_data.get("evaluator_focus", current_contract.evaluator_focus)
                )
                target_files = self._normalize_string_list(
                    contract_data.get("target_files", current_contract.target_files)
                )
                target_line_hints = self._normalize_string_list(
                    contract_data.get("target_line_hints", current_contract.target_line_hints)
                )
                validation_commands = self._normalize_harness_validation_commands(
                    contract_data.get("validation_commands", current_contract.validation_commands),
                    project_path=project_path,
                )
                edit_strategy = str(contract_data.get("edit_strategy") or current_contract.edit_strategy).strip()
                objective, deliverables, acceptance_checks, evaluator_focus, contract_sanitized = (
                    self._sanitize_read_only_contract(
                        user_request=user_request,
                        objective=objective,
                        feature_name=feature_name,
                        deliverables=deliverables,
                        acceptance_checks=acceptance_checks,
                        evaluator_focus=evaluator_focus,
                    )
                )
                if sprint_id and objective:
                    harness.set_active_sprint(
                        sprint_id=sprint_id,
                        feature_name=feature_name,
                        objective=objective,
                        deliverables=deliverables,
                        acceptance_checks=acceptance_checks,
                        evaluator_focus=evaluator_focus,
                        target_files=target_files,
                        target_line_hints=target_line_hints,
                        validation_commands=validation_commands,
                        edit_strategy=edit_strategy,
                        status=self.normalize_harness_contract_status(
                            str(contract_data.get("status") or current_contract.status or "proposed").strip(),
                            session_role="planner",
                        ) or "proposed",
                        role="planner",
                    )
                    if contract_sanitized:
                        harness.append_run_event(
                            "planner_contract_sanitized",
                            {
                                "sprint_id": sprint_id,
                                "feature_name": feature_name,
                                "objective": objective,
                                "deliverables": deliverables,
                                "acceptance_checks": acceptance_checks,
                                "evaluator_focus": evaluator_focus,
                                "target_files": target_files,
                                "target_line_hints": target_line_hints,
                                "validation_commands": validation_commands,
                                "edit_strategy": edit_strategy,
                            },
                        )
                elif contract_data:
                    contract_updates: dict[str, Any] = {}
                    for key in ("sprint_id", "feature_name", "objective", "status"):
                        value = str(contract_data.get(key) or "").strip()
                        if value:
                            if key == "status":
                                value = self.normalize_harness_contract_status(value, session_role="planner")
                            contract_updates[key] = value
                    if "deliverables" in contract_data:
                        contract_updates["deliverables"] = deliverables
                    if "acceptance_checks" in contract_data:
                        contract_updates["acceptance_checks"] = acceptance_checks
                    if "evaluator_focus" in contract_data:
                        contract_updates["evaluator_focus"] = evaluator_focus
                    if "target_files" in contract_data:
                        contract_updates["target_files"] = target_files
                    if "target_line_hints" in contract_data:
                        contract_updates["target_line_hints"] = target_line_hints
                    if "validation_commands" in contract_data:
                        contract_updates["validation_commands"] = validation_commands
                    if "edit_strategy" in contract_data:
                        contract_updates["edit_strategy"] = edit_strategy
                    if contract_updates:
                        harness.update_sprint_contract(**contract_updates)

            progress_data = (
                payload.get("progress")
                or payload.get("progress_state")
                or planner_payload.get("progress")
                or planner_payload.get("progress_state")
                or {}
            )
            if not isinstance(progress_data, dict):
                progress_data = {}
            if not progress_data:
                for key in ("summary", "revision_reason"):
                    value = str(planner_payload.get(key) or "").strip()
                    if value:
                        progress_data["summary"] = value
                        break
                for key in ("next_steps", "blockers", "touched_files", "validation_checks"):
                    if planner_payload.get(key) not in (None, "", [], {}):
                        progress_data[key] = planner_payload.get(key)
                phase_value = str(planner_payload.get("phase") or "").strip()
                if phase_value:
                    progress_data["current_phase"] = (
                        "implementation" if "generator" in phase_value or "ready" in phase_value else "planning"
                    )
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

            handoff_markdown = str(payload.get("handoff_markdown") or planner_payload.get("handoff_markdown") or "").strip()
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            sprint_id = harness.read_sprint_contract().sprint_id
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "harness_payload": payload,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return f"Applied planner harness update{f' for {sprint_id}' if sprint_id else ''}"

        if action == "generator_update":
            generator_payload = payload.get("generator_update") if isinstance(payload.get("generator_update"), dict) else {}
            if not isinstance(generator_payload, dict):
                generator_payload = {}
            progress_data = (
                payload.get("progress")
                or generator_payload.get("progress")
                or {}
            )
            if not isinstance(progress_data, dict):
                progress_data = {}
            if not progress_data:
                for source in (generator_payload, payload):
                    if not isinstance(source, dict):
                        continue
                    for key, target_key in (
                        ("summary", "summary"),
                        ("repair_summary", "summary"),
                        ("validation_summary", "last_validation"),
                        ("last_validation", "last_validation"),
                        ("product_goal", "product_goal"),
                        ("handoff_summary", "summary"),
                    ):
                        value = str(source.get(key) or "").strip()
                        if value and target_key not in progress_data:
                            progress_data[target_key] = value
                    for key in (
                        "blockers",
                        "next_steps",
                        "touched_files",
                        "validation_checks",
                        "validation_artifacts",
                    ):
                        if source.get(key) not in (None, "", [], {}):
                            progress_data[key] = source.get(key)
                    if source.get("acceptance_evidence") not in (None, "", [], {}):
                        progress_data["acceptance_evidence"] = source.get("acceptance_evidence")
                    if source.get("validation_command") not in (None, ""):
                        progress_data.setdefault(
                            "last_validation",
                            f"Ran validation command: {str(source.get('validation_command') or '').strip()}",
                        )
                        progress_data.setdefault("validation_artifacts", [])
                        progress_data["validation_artifacts"] = list(progress_data["validation_artifacts"]) + [
                            f"Validation command: {str(source.get('validation_command') or '').strip()}"
                        ]
                    if source.get("validation_output") not in (None, ""):
                        progress_data.setdefault("validation_artifacts", [])
                        exit_code = source.get("exit_code")
                        exit_suffix = ""
                        if isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool):
                            exit_suffix = f" exit={int(exit_code)}"
                        progress_data["validation_artifacts"] = list(progress_data["validation_artifacts"]) + [
                            f"Validation output{exit_suffix}: {str(source.get('validation_output') or '').strip()}"
                        ]
                    if source.get("current_phase") not in (None, ""):
                        progress_data["current_phase"] = source.get("current_phase")
            if progress_data.get("last_validation") and "validation_artifacts" not in progress_data:
                progress_data["validation_artifacts"] = [progress_data["last_validation"]]
            if progress_data.get("touched_files") and "validation_checks" not in progress_data:
                progress_data["validation_checks"] = [
                    "Updated file scope: " + ", ".join(self._normalize_string_list(progress_data.get("touched_files"))[:3])
                ]
            progress_updates: dict[str, Any] = {"active_role": "generator"}
            current_contract = harness.read_sprint_contract()
            for key in ("summary", "product_goal", "last_validation"):
                value = str(progress_data.get(key) or "").strip()
                if value:
                    progress_updates[key] = value
            for key in ("blockers", "next_steps", "touched_files", "validation_checks", "validation_artifacts"):
                if key in progress_data:
                    progress_updates[key] = self._normalize_string_list(progress_data.get(key))
            if "acceptance_evidence" in progress_data:
                normalized_evidence = self._normalize_string_mapping(progress_data.get("acceptance_evidence"))
                progress_updates["acceptance_evidence"] = self.align_acceptance_evidence_to_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=normalized_evidence,
                    objective=str(current_contract.objective or ""),
                    user_request=user_request,
                )
            current_phase = str(progress_data.get("current_phase") or "implementation").strip()
            if current_phase:
                progress_updates["current_phase"] = current_phase
            if progress_updates.get("touched_files") and "blockers" not in progress_updates:
                progress_updates["blockers"] = []
            if progress_updates.get("last_validation") and "next_steps" not in progress_updates:
                progress_updates["next_steps"] = []
            sprint_status = str(
                payload.get("sprint_status")
                or generator_payload.get("sprint_status")
                or payload.get("status")
                or generator_payload.get("status")
                or ""
            ).strip()
            sprint_status = self.normalize_harness_contract_status(sprint_status, session_role="generator")
            if not sprint_status:
                inferred_evidence = self._normalize_string_mapping(progress_data.get("acceptance_evidence"))
                touched_files = self._normalize_string_list(progress_data.get("touched_files"))
                blockers = self._normalize_string_list(progress_data.get("blockers"))
                last_validation = str(progress_data.get("last_validation") or "").strip()
                if touched_files and not blockers and (last_validation or inferred_evidence):
                    sprint_status = "implemented"
            successful_generator_update = sprint_status in {"approved", "implemented", "passed"}
            if successful_generator_update:
                target_files = self._normalize_string_list(current_contract.target_files)
                merged_evidence = self._normalize_string_mapping(progress_updates.get("acceptance_evidence"))
                for check, evidence in self._infer_static_acceptance_evidence(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    summary={
                        "target_files": target_files,
                        "touched_files": self._normalize_string_list(progress_updates.get("touched_files")),
                    },
                ).items():
                    merged_evidence.setdefault(check, evidence)

                if not self._acceptance_evidence_covers_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=merged_evidence,
                ):
                    probe_checks, probe_artifacts, probe_evidence = self.run_harness_generator_validation_probes(
                        project_path=project_path or self.project.project_path,
                        summary={
                            "acceptance_checks": self._normalize_string_list(current_contract.acceptance_checks),
                            "validation_commands": self._normalize_string_list(current_contract.validation_commands),
                            "required_revisions": self._normalize_string_list(
                                harness.read_evaluator_report().required_revisions
                            ),
                            "next_steps": self._normalize_string_list(progress_updates.get("next_steps")),
                            "summary": str(progress_updates.get("summary") or ""),
                            "last_validation": str(progress_updates.get("last_validation") or ""),
                            "validation_checks": self._normalize_string_list(progress_updates.get("validation_checks")),
                            "validation_artifacts": self._normalize_string_list(progress_updates.get("validation_artifacts")),
                            "acceptance_evidence": merged_evidence,
                            "target_files": target_files,
                            "touched_files": self._normalize_string_list(progress_updates.get("touched_files")),
                            "deliverables": self._normalize_string_list(current_contract.deliverables),
                            "contract_objective": str(current_contract.objective or ""),
                        },
                    )
                    if probe_checks:
                        merged_checks = self._normalize_string_list(progress_updates.get("validation_checks"))
                        for item in probe_checks:
                            if item not in merged_checks:
                                merged_checks.append(item)
                        progress_updates["validation_checks"] = merged_checks[:8]
                    if probe_artifacts:
                        merged_artifacts = self._normalize_string_list(progress_updates.get("validation_artifacts"))
                        for item in probe_artifacts:
                            if item not in merged_artifacts:
                                merged_artifacts.append(item)
                        progress_updates["validation_artifacts"] = merged_artifacts[:8]
                    for check, evidence in probe_evidence.items():
                        merged_evidence.setdefault(check, evidence)

                progress_updates["acceptance_evidence"] = self.align_acceptance_evidence_to_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=merged_evidence,
                    objective=str(current_contract.objective or ""),
                    user_request=user_request,
                )
                repair_summary = str(
                    generator_payload.get("repair_summary")
                    or payload.get("repair_summary")
                    or progress_updates.get("summary")
                    or ""
                ).strip()
                if repair_summary and not progress_updates.get("last_validation"):
                    progress_updates["last_validation"] = repair_summary
                if progress_updates.get("last_validation") and "validation_artifacts" not in progress_updates:
                    touched_files = self._normalize_string_list(progress_updates.get("touched_files"))
                    success_artifacts = [str(progress_updates.get("last_validation") or "").strip()]
                    if touched_files:
                        success_artifacts.append(
                            "Touched files: " + ", ".join(touched_files[:3])
                        )
                    progress_updates["validation_artifacts"] = [item for item in success_artifacts if item][:6]
                progress_updates["blockers"] = []
                progress_updates["next_steps"] = []
                progress_updates["current_phase"] = "evaluation"
            harness.update_progress(**progress_updates)

            handoff_markdown = str(
                payload.get("handoff_markdown")
                or generator_payload.get("handoff_markdown")
                or ""
            ).strip()
            if not handoff_markdown and successful_generator_update:
                touched_files = self._normalize_string_list(progress_updates.get("touched_files"))
                validation_summary = str(progress_updates.get("last_validation") or progress_updates.get("summary") or "").strip()
                handoff_lines = [
                    "# Generator Handoff",
                    "",
                    "## Summary",
                    str(progress_updates.get("summary") or "Implemented the requested sprint update."),
                ]
                if validation_summary:
                    handoff_lines.extend(["", "## Validation", f"- {validation_summary}"])
                if touched_files:
                    handoff_lines.extend(["", "## Touched Files", *[f"- `{path}`" for path in touched_files[:6]]])
                handoff_markdown = "\n".join(handoff_lines)
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            if sprint_status in {"proposed", "approved", "implemented", "needs_revision", "passed", "failed"}:
                harness.set_contract_status(status=sprint_status, role="generator")
            if sprint_status in {"approved", "implemented", "passed"}:
                harness.update_progress(blockers=[], next_steps=[])
            if current_contract.sprint_id and sprint_status != "failed":
                harness.write_evaluator_report(
                    EvaluatorReport(
                        sprint_id=current_contract.sprint_id,
                        verdict="unknown",
                        score=None,
                        findings=[],
                        required_revisions=[],
                        passed_checks=[],
                        failed_checks=[],
                    )
                )

            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": harness.read_sprint_contract().sprint_id,
                    "sprint_status": sprint_status or "",
                    "harness_payload": payload,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return "Applied generator harness update"

        if action == "evaluator_verdict":
            evaluator_payload = self._coerce_evaluator_verdict_payload(
                payload=payload,
                harness=harness,
                assistant_text=assistant_text,
            )
            sprint_id = str(evaluator_payload.get("sprint_id") or "").strip()
            verdict = str(evaluator_payload.get("verdict") or "").strip()
            if not sprint_id or verdict not in {"pass", "revise", "blocked"}:
                raise ValueError("Evaluator verdict requires sprint_id and verdict")
            harness.record_evaluator_verdict(
                sprint_id=sprint_id,
                verdict=verdict,
                findings=self._normalize_string_list(evaluator_payload.get("findings")),
                required_revisions=self._normalize_string_list(evaluator_payload.get("required_revisions")),
                passed_checks=self._normalize_string_list(evaluator_payload.get("passed_checks")),
                failed_checks=self._normalize_string_list(evaluator_payload.get("failed_checks")),
                score=evaluator_payload.get("score"),
            )
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "verdict": verdict,
                    "harness_payload": payload,
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
        target_path = os.path.normpath(project_path or self.project.project_path)
        return self.harness_service.build_resume_prompt(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
        )

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
                    "For one-file coding tasks, include a compact patch scaffold in sprint_contract: "
                    "target_files, target_line_hints, validation_commands, and edit_strategy. "
                    "If the objective asks for bullets, findings, or an audit summary, place that "
                    "content in handoff_markdown and concise progress/spec fields, then finish with "
                    "a valid planner_update resonant-harness block. Put the concrete handoff under "
                    "`sprint_contract` itself; do not replace it with alternate wrapper keys."
                ),
                "generator": (
                    "Treat the objective as implementation guidance for the active sprint. "
                    "If the objective is explicitly read-only, do not modify repository files; only "
                    "read, analyze, and update harness artifacts. "
                    "Keep the final response brief, record validation in progress.last_validation, "
                    "store short artifacts in progress.validation_artifacts, map satisfied acceptance "
                    "checks into progress.acceptance_evidence, "
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

        self.refresh_network_defaults()
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

        if session_role == "generator" and forced_backend and self._harness_generator_needs_frontier_repair(project_path):
            forced_backend = ""
            forced_model = ""

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

        if session_role == "generator" and self._harness_generator_needs_frontier_repair(project_path):
            preferences = {
                **preferences,
                "generator": ["claude-code", "codex", "mlx", "openai", "claude", "ollama", "lmstudio"],
            }

        for backend_type in preferences.get(session_role, preferences["generator"]):
            info = self.available_backends.get(backend_type)
            if not info:
                continue
            models = list(info.get("models") or [])
            preferred_mlx_model = preferred_mlx_model_by_role.get(session_role, "adapter-router")
            if self.backend_spec and self.backend_spec.backend_type == backend_type and self.backend_spec.model:
                model = self.backend_spec.model
            elif session_role == "generator" and backend_type == "claude-code" and self._harness_generator_needs_frontier_repair(project_path):
                model = "sonnet" if "sonnet" in models else (models[0] if models else "")
            elif session_role == "generator" and backend_type == "codex" and self._harness_generator_needs_frontier_repair(project_path):
                model = "gpt-5.4" if "gpt-5.4" in models else (models[0] if models else "")
            elif backend_type == "mlx" and preferred_mlx_model in models:
                model = preferred_mlx_model
            else:
                model = models[0] if models else ""
            spec = self.build_backend_spec(backend_type, model=model or None, project_path=project_path)
            return spec.backend_type, spec.model

        raise ValueError(f"No available backend for harness role '{session_role}'")

    def _harness_generator_needs_frontier_repair(self, project_path: Optional[str] = None) -> bool:
        project_path = os.path.normpath(project_path or self.project.project_path)
        summary = self.get_harness_summary(project_path)
        contract_status = str(summary.get("contract_status") or "").strip()
        evaluator_verdict = str(summary.get("evaluator_verdict") or "").strip()
        if contract_status not in {"implemented", "needs_revision", "failed"}:
            return False
        combined = "\n".join(
            [
                "\n".join(self._normalize_string_list(summary.get("findings"))),
                "\n".join(self._normalize_string_list(summary.get("required_revisions"))),
                "\n".join(self._normalize_string_list(summary.get("validation_artifacts"))),
                str(summary.get("last_validation") or ""),
            ]
        ).lower()
        if any(
            token in combined
            for token in (
                "syntaxerror",
                "syntax error",
                "indentationerror",
                "indentation error",
                "expected an indented block",
                "unexpected indent",
                "invalid syntax",
                "parse error",
                "traceback (most recent call last)",
                "modulenotfounderror",
                "module not found",
                "importerror",
                "import error",
                "nameerror",
                "typeerror",
                "attributeerror",
                "runtimeerror",
                "runtime error",
            )
        ):
            return True
        if evaluator_verdict not in {"revise", "blocked"}:
            return False

        bundle = self.build_harness_generator_structured_bundle(project_path)
        files = bundle.get("files") or []
        if len(files) != 1 or not bool(files[0].get("exists")):
            return False
        traceback_data = self._extract_harness_repair_traceback(project_path, files[0], summary)
        return bool(traceback_data.get("line_number") or traceback_data.get("error_line"))

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
        if forced_backend.lower() in {"disabled", "none", "off", "false", "no"}:
            return "", ""
        repair_needed = session_role == "generator" and self._harness_generator_needs_frontier_repair(project_path)

        if forced_backend and not repair_needed:
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
        if repair_needed:
            retry_preferences = {
                **retry_preferences,
                "generator": ["claude-code", "codex", "mlx", "openai", "claude", "ollama", "lmstudio"],
            }

        for backend_type in retry_preferences.get(session_role, retry_preferences["evaluator"]):
            if backend_type == failed_backend:
                continue
            info = self.available_backends.get(backend_type)
            if not info:
                continue
            models = list(info.get("models") or [])
            preferred_mlx_model = preferred_mlx_model_by_role.get(session_role, "adapter-router")
            if backend_type == "claude-code" and repair_needed:
                model = "sonnet" if "sonnet" in models else (models[0] if models else "")
            elif backend_type == "codex" and repair_needed and "gpt-5.4" in models:
                model = "gpt-5.4"
            elif backend_type == "mlx" and preferred_mlx_model in models:
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
        generator_mode = "full"
        if normalized_role == "evaluator":
            evaluation_mode = self.get_harness_evaluator_strategy(project_path)
        elif normalized_role == "generator":
            generator_mode = self.get_harness_generator_strategy(project_path, prompt)

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

        if normalized_role == "generator" and generator_mode == "artifacts":
            effective_prompt = self.build_harness_generator_artifact_prompt(project_path, prompt)
            allowed_tools = []
            max_tokens_override = self.get_harness_generator_artifact_max_tokens()
        elif normalized_role == "generator" and generator_mode == "repair":
            effective_prompt = self.build_harness_generator_repair_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_repair_max_tokens()
        elif normalized_role == "generator" and generator_mode == "patch":
            effective_prompt = self.build_harness_generator_patch_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_patch_max_tokens()
        elif normalized_role == "generator" and generator_mode == "structured":
            effective_prompt = self.build_harness_generator_structured_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_read", "file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_structured_max_tokens()
        elif evaluation_mode == "artifacts":
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
        post_apply_error = ""
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
                        if evaluation_mode in {"artifacts", "structured"} or generator_mode in {"artifacts", "patch", "structured"}:
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

        if not error and pending_harness_payload is None and normalized_role == "generator" and generator_mode == "artifacts":
            inferred_payload = self.infer_generator_artifact_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
                prompt=effective_prompt,
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload
        if not error and pending_harness_payload is None and normalized_role == "generator" and generator_mode in {"repair", "patch", "structured"}:
            inferred_payload = self.infer_generator_structured_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
                prompt=effective_prompt,
                display_events=display_events,
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload

        if not error and pending_harness_payload is None and evaluation_mode in {"artifacts", "structured"}:
            inferred_payload = self.infer_evidence_only_evaluator_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload

        if not error and pending_harness_payload is not None and normalized_role == "generator":
            pending_harness_payload, post_apply_error = self.apply_generator_post_patch_safety_gate(
                project_path=project_path,
                payload=pending_harness_payload,
                generator_mode=generator_mode,
                display_events=display_events,
            )

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
                if post_apply_error and not error:
                    error = post_apply_error
            except Exception as exc:
                error = f"Failed to apply harness update: {exc}"
        elif not error and deferred_parse_error:
            error = deferred_parse_error
        elif not error:
            error = "No resonant-harness update emitted by automated role run"

        if timed_out and pending_harness_payload is None:
            error = f"Timed out after {float(timeout_seconds):.1f}s"

        return {
            "result": "\n\n".join(collected_text).strip(),
            "error": error,
            "steps": steps,
            "display_events": display_events,
            "backend_type": spec.backend_type,
            "model": spec.model,
            "timed_out": timed_out,
            "artifact_only": evaluation_mode == "artifacts" or generator_mode in {"artifacts", "structured", "repair"},
            "evaluation_mode": evaluation_mode,
            "role_mode": generator_mode if normalized_role == "generator" else evaluation_mode,
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
        self.refresh_network_defaults()
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
            self.backend_spec.backend_type in {"resonant", "ollama", "claude", "openai", "lmstudio", "mlx"} and
            section in {"api_keys", "engram", "general", "network"}
        ):
            try:
                self.backend = self.backend_spec.create_backend(self.settings)
                if self.session:
                    self.session.backend = self.backend
            except Exception:
                logger.warning("Failed to refresh current backend after settings update", exc_info=True)

        return self.settings.get_masked()

    def refresh_network_defaults(self):
        settings_data = self.settings.get_all()
        self.api_url = resolve_resonant_api_url(settings_data=settings_data)
        self.ollama_url = (
            str(
                os.environ.get(
                    "OLLAMA_URL",
                    os.environ.get("OLLAMA_HOST", "http://10.0.0.133:11434"),
                )
                or ""
            ).rstrip("/")
        )
        self.lmstudio_url = str(os.environ.get("LMSTUDIO_URL", "") or "").rstrip("/")

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
        state.refresh_network_defaults()
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
                        f"You are a project coordinator working in: {path}\n\n"
                        f"## Strategy\n{strategy}\n\n"
                        f"## Instructions\n"
                        f"You MUST complete ALL of the following:\n"
                        f"1. First, list the files in the project directory to understand the current state\n"
                        f"2. Create a clear task plan and explain what you will build\n"
                        f"3. Execute EVERY task — write all the code files, create directories, install dependencies\n"
                        f"4. After each file is created, briefly confirm what was done\n"
                        f"5. When ALL tasks are complete, give a final summary\n\n"
                        f"IMPORTANT: Do not just plan — actually CREATE all the files and write all the code. "
                        f"Use the Write tool to create files, Bash to run commands (npm init, npm install, etc). "
                        f"Be thorough and complete every task in the strategy."
                    )
                    backend_type = msg.get("backend", "") or getattr(state.backend, "name", "")
                    model = msg.get("model", "") or getattr(state.backend, "model", "")
                    if not backend_type:
                        # Try to pick the first available backend
                        for bname in ("codex", "claude-code", "openai", "resonant"):
                            if bname in state.available_backends:
                                backend_type = bname
                                binfo = state.available_backends[bname]
                                if isinstance(binfo, dict) and binfo.get("models"):
                                    models_list = binfo["models"]
                                    if isinstance(models_list, list) and models_list:
                                        model = models_list[0] if isinstance(models_list[0], str) else models_list[0].get("id", "")
                                    elif isinstance(models_list, dict):
                                        model = next(iter(models_list.values()), {}).get("id", "")
                                break
                    if not backend_type:
                        raise ValueError("No AI backend available. Please select a backend first.")
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

            # ── Command Center: Files & Preview ──────────────
            elif command == "command_project_files":
                project_id = msg.get("project_id", "")
                proj = state.command_project_store.get_project(project_id)
                if proj:
                    import glob as _glob
                    project_path = os.path.normpath(proj.path)
                    files = []
                    for entry in sorted(os.scandir(project_path), key=lambda e: (not e.is_dir(), e.name.lower())):
                        if entry.name.startswith('.'):
                            continue
                        try:
                            stat = entry.stat()
                            files.append({
                                "name": entry.name,
                                "path": os.path.join(project_path, entry.name),
                                "is_dir": entry.is_dir(),
                                "size": stat.st_size if not entry.is_dir() else 0,
                                "modified": stat.st_mtime,
                            })
                            # Also list files inside directories (1 level deep)
                            if entry.is_dir():
                                for sub in sorted(os.scandir(entry.path), key=lambda e: e.name.lower()):
                                    if sub.name.startswith('.'):
                                        continue
                                    sub_stat = sub.stat()
                                    files.append({
                                        "name": f"  {entry.name}/{sub.name}",
                                        "path": os.path.join(entry.path, sub.name),
                                        "is_dir": sub.is_dir(),
                                        "size": sub_stat.st_size if not sub.is_dir() else 0,
                                        "modified": sub_stat.st_mtime,
                                    })
                        except Exception:
                            pass
                    await ws.send_json({"event": "command_project_files", "project_id": project_id, "files": files})
                else:
                    await ws.send_json({"event": "error", "message": f"Project {project_id} not found"})

            elif command == "command_project_read_file":
                project_id = msg.get("project_id", "")
                file_path = msg.get("path", "")
                proj = state.command_project_store.get_project(project_id)
                if proj and file_path:
                    try:
                        # Security: ensure path is under project directory
                        norm_path = os.path.normpath(file_path)
                        norm_proj = os.path.normpath(proj.path)
                        if not norm_path.startswith(norm_proj):
                            raise ValueError("Path outside project directory")
                        with open(norm_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read(100_000)  # max 100KB
                        await ws.send_json({
                            "event": "command_project_file_content",
                            "project_id": project_id,
                            "path": file_path,
                            "content": content,
                        })
                    except Exception as e:
                        await ws.send_json({"event": "error", "message": f"Cannot read file: {e}"})
                else:
                    await ws.send_json({"event": "error", "message": "Project or file not found"})

            elif command == "command_project_preview":
                project_id = msg.get("project_id", "")
                proj = state.command_project_store.get_project(project_id)
                if proj:
                    project_path = os.path.normpath(proj.path)
                    # Look for index.html or any HTML file
                    index_path = os.path.join(project_path, "index.html")
                    if os.path.exists(index_path):
                        preview_url = f"/preview/{project_id}/index.html"
                        await ws.send_json({"event": "command_project_preview", "url": preview_url})
                    else:
                        html_files = [f for f in os.listdir(project_path) if f.endswith('.html')]
                        if html_files:
                            preview_url = f"/preview/{project_id}/{html_files[0]}"
                            await ws.send_json({"event": "command_project_preview", "url": preview_url})
                        else:
                            await ws.send_json({"event": "command_project_preview", "error": "No HTML files found in project. The project may still be building."})
                else:
                    await ws.send_json({"event": "error", "message": f"Project {project_id} not found"})

            # ── Command Center: Chat with Coordinator ─────────
            elif command == "command_project_chat":
                project_id = msg.get("project_id", "")
                message = msg.get("message", "").strip()
                proj = state.command_project_store.get_project(project_id)
                if not proj:
                    await ws.send_json({"event": "error", "message": f"Project {project_id} not found"})
                    continue
                if not message:
                    await ws.send_json({"event": "error", "message": "Message is required"})
                    continue

                try:
                    # Build or reuse coordinator session for this project
                    session_key = f"_coordinator_session_{project_id}"
                    coordinator_session = getattr(state, session_key, None)

                    if coordinator_session is None:
                        # Create a new coordinator session
                        backend_type = getattr(state.backend, "name", "")
                        model = getattr(state.backend, "model", "")
                        if not backend_type:
                            for bname in ("codex", "claude-code", "openai", "resonant"):
                                if bname in state.available_backends:
                                    backend_type = bname
                                    binfo = state.available_backends[bname]
                                    if isinstance(binfo, dict) and binfo.get("models"):
                                        models_list = binfo["models"]
                                        if isinstance(models_list, list) and models_list:
                                            model = models_list[0] if isinstance(models_list[0], str) else models_list[0].get("id", "")
                                    break
                        if not backend_type:
                            raise ValueError("No AI backend available")

                        spec = state.build_backend_spec(backend_type, model=model or None, project_path=proj.path)
                        coordinator_session = state.build_session(
                            backend=spec.create_backend(state.settings),
                            backend_spec=spec,
                            project_path=proj.path,
                            session_mode="code",
                            session_role="generator",
                        )
                        # Add project context to the first message
                        context_prefix = (
                            f"You are the project coordinator for: {proj.name}\n"
                            f"Project path: {proj.path}\n"
                            f"Strategy: {proj.strategy}\n\n"
                            f"You can use your tools (shell, read, write, etc.) to work on this project. "
                            f"When asked to do something, actually do it using your tools — don't just describe what you would do.\n\n"
                        )
                        message = context_prefix + "User says: " + message
                        setattr(state, session_key, coordinator_session)

                    # Run the coordinator session in a background thread and stream results
                    async def _run_coordinator_chat(session, prompt, pid):
                        try:
                            _ws = ws  # capture ws reference
                            _loop = asyncio.get_event_loop()
                            _texts = []

                            def _run():
                                # CRITICAL: Reset cancel state before reusing session
                                session.reset_cancel()

                                for event in session.run(prompt):
                                    event_type = event.get("event", "")
                                    if event_type == "text.delta":
                                        delta = event.get("text", "")
                                        if delta:
                                            _texts.append(delta)
                                            # Stream delta to frontend
                                            try:
                                                asyncio.run_coroutine_threadsafe(
                                                    _ws.send_json({
                                                        "event": "command_project_chat_delta",
                                                        "project_id": pid,
                                                        "text": delta,
                                                    }),
                                                    _loop,
                                                )
                                            except Exception:
                                                pass
                                    elif event_type == "text.done":
                                        text = event.get("text", "")
                                        if text:
                                            _texts.append(text)
                                    elif event_type == "tool_call":
                                        # Stream tool call info so UI shows activity
                                        tool_name = event.get("name", "tool")
                                        try:
                                            asyncio.run_coroutine_threadsafe(
                                                _ws.send_json({
                                                    "event": "command_project_chat_delta",
                                                    "project_id": pid,
                                                    "text": f"\n🔧 Using tool: {tool_name}...\n",
                                                    "is_tool": True,
                                                }),
                                                _loop,
                                            )
                                        except Exception:
                                            pass
                                    elif event_type == "tool_result":
                                        # Show brief tool result
                                        result_text = str(event.get("result", ""))[:100]
                                        try:
                                            asyncio.run_coroutine_threadsafe(
                                                _ws.send_json({
                                                    "event": "command_project_chat_delta",
                                                    "project_id": pid,
                                                    "text": f"✓ Done\n",
                                                    "is_tool": True,
                                                }),
                                                _loop,
                                            )
                                        except Exception:
                                            pass
                                    # All other events (step.start, step.end, etc.) are silently processed
                                return "".join(_texts) if _texts else "(no response)"

                            result = await asyncio.wait_for(
                                _loop.run_in_executor(None, _run),
                                timeout=180,  # 3 minute timeout
                            )

                            await ws.send_json({
                                "event": "command_project_chat_response",
                                "project_id": pid,
                                "response": result,
                            })

                            # Also add to project activity
                            state.command_project_store.add_activity(pid, "agent", "Coordinator", result[:200])

                        except asyncio.TimeoutError:
                            partial = "".join(_texts) if _texts else ""
                            await ws.send_json({
                                "event": "command_project_chat_response",
                                "project_id": pid,
                                "response": (partial + "\n\n⚠️ Response timed out after 3 minutes.") if partial else "⚠️ Response timed out after 3 minutes. The AI backend may be slow or unreachable.",
                            })
                        except Exception as e:
                            await ws.send_json({
                                "event": "command_project_chat_response",
                                "project_id": pid,
                                "response": f"Error: {e}",
                            })

                    asyncio.create_task(_run_coordinator_chat(coordinator_session, message, project_id))

                except Exception as e:
                    await ws.send_json({
                        "event": "command_project_chat_response",
                        "project_id": project_id,
                        "response": f"Failed to start coordinator: {e}",
                    })

            # ── Command Center: Initiative ────────────────────
            elif command == "command_project_initiative":
                project_id = msg.get("project_id", "")
                prompt = msg.get("prompt", "").strip()
                proj = state.command_project_store.get_project(project_id)
                if not proj:
                    await ws.send_json({"event": "error", "message": f"Project {project_id} not found"})
                    continue
                if not prompt:
                    await ws.send_json({"event": "error", "message": "Initiative prompt is required"})
                    continue
                try:
                    backend_type = msg.get("backend", "") or getattr(state.backend, "name", "")
                    model = msg.get("model", "") or getattr(state.backend, "model", "")
                    if not backend_type:
                        for bname in ("codex", "claude-code", "openai", "resonant"):
                            if bname in state.available_backends:
                                backend_type = bname
                                binfo = state.available_backends[bname]
                                if isinstance(binfo, dict) and binfo.get("models"):
                                    models_list = binfo["models"]
                                    if isinstance(models_list, list) and models_list:
                                        model = models_list[0] if isinstance(models_list[0], str) else models_list[0].get("id", "")
                                break
                    if not backend_type:
                        raise ValueError("No AI backend available")
                    spec = state.build_backend_spec(backend_type, model=model or None, project_path=proj.path)
                    initiative_prompt = (
                        f"You are an autonomous coding agent working on the project at: {proj.path}\n\n"
                        f"## Project Context\n{proj.strategy}\n\n"
                        f"## Your Task\n{prompt}\n\n"
                        f"## CRITICAL INSTRUCTIONS\n"
                        f"You MUST act autonomously. Do NOT ask questions or wait for input. "
                        f"Start immediately by creating files using your write/shell tools. "
                        f"Write complete, production-quality code. Do not describe what you would do — actually do it. "
                        f"Create all necessary files, write all the code, and verify it works. "
                        f"When done, summarize what you created."
                    )

                    def _make_init_event_handler(pid):
                        def handler(task_id, event):
                            state._push_agent_event(task_id, event)
                            if event.get("event") == "text.done":
                                text = event.get("text", "")[:200]
                                if text.strip():
                                    state.command_project_store.add_activity(pid, "agent", "Worker", text)
                        return handler

                    bg_task = state.task_runner.submit(
                        name=prompt[:60],
                        prompt=initiative_prompt,
                        session_factory=state.make_background_session,
                        backend_type=backend_type,
                        model=spec.model,
                        project_path=proj.path,
                        session_mode="code",
                        session_role="generator",
                        backend_spec=spec.to_dict(),
                        on_event=_make_init_event_handler(proj.id),
                    )
                    # Add agent to project
                    agents = proj.agents.copy() if hasattr(proj, 'agents') and proj.agents else []
                    agents.append({
                        "id": bg_task.id, "name": prompt[:60],
                        "role": "worker", "status": "running",
                        "model": spec.model, "steps": 0, "elapsed": 0,
                    })
                    state.command_project_store.update_project(proj.id, agents=agents, status="running")
                    proj.add_activity("system", "System", f"Initiative launched: {prompt[:80]}")
                    state.command_project_store._persist(proj)
                    proj = state.command_project_store.get_project(proj.id)
                    await ws.send_json({"event": "command_project_status", "project": proj.to_dict()})
                except Exception as e:
                    await ws.send_json({"event": "error", "message": f"Failed to launch initiative: {e}"})

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


async def preview_file(request):
    """Serve project files for preview. URL: /preview/{project_id}/{path:path}"""
    from starlette.responses import FileResponse, JSONResponse
    project_id = request.path_params.get("project_id", "")
    file_path = request.path_params.get("path", "index.html")
    proj = state.command_project_store.get_project(project_id) if hasattr(state, "command_project_store") else None
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    full_path = os.path.normpath(os.path.join(proj.path, file_path))
    # Security: ensure the file is within the project directory
    if not full_path.startswith(os.path.normpath(proj.path)):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isfile(full_path):
        return JSONResponse({"error": f"File not found: {file_path}"}, status_code=404)
    return FileResponse(full_path)


# ── Starlette App ─────────────────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/preview/{project_id}/{path:path}", preview_file),
        WebSocketRoute("/ws", websocket_endpoint),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
)
