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
import shutil
import subprocess
import sys
import threading
import time
import difflib
from pathlib import Path
import uuid
from datetime import date
from typing import Any, Callable, Optional

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..events import EngineEvent, make_event
from ..backends import (
    CodexCliBackend,
    OllamaBackend,
    codex_cli_model_labels,
    resolve_codex_cli_path,
)
from ..engine import Session, AGENT_TOOLS
from ..engine.session import inspect_system_instructions
from ..network_defaults import default_thinking_for_model, resolve_ollama_url
from .sessions import ProjectManager
from .settings import SettingsManager
from .costs import CostTracker
from .project_instructions import (
    find_instruction_file,
    get_instruction_info,
    load_project_instructions,
)
from .runtime import BackendSpec
from .evaluation_dashboard import EvaluationManager
from ..harness import EvaluatorReport, HarnessWorkspace, HarnessOrchestrator, HarnessService
from ..orchestration import IntentService
from .autonomous_session import (
    build_roadmap_inspector_payload as _build_roadmap_inspector_payload,
    cleanup_finished_daemons as _cleanup_autonomous_daemons,
    find_orphaned_autonomous_missions as _find_orphaned_autonomous_missions,
    get_autonomous_daemon as _get_autonomous_daemon,
    list_autonomous_missions as _list_autonomous_missions,
    resume_autonomous_mission as _resume_autonomous_mission,
    start_autonomous_mission as _start_autonomous_mission,
    stop_autonomous_mission as _stop_autonomous_mission,
)
from ..engine.hooks import HookRunner
from ..engine.mcp import MCPManager
from ..engine.memory import EngramIntegration
from ..engine.diff_review import generate_review
from ..engine.rag import CodebaseIndex

# Shared reference to the pywebview window (set by server.py when using native mode)
_webview_window = None

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────
#
# Bug #20 fix — frozen-bundle template/static path resolution.
#
# In a PyInstaller frozen exe, modules are loaded from a PYZ archive (a
# zip embedded in the exe). `Path(__file__).parent` for gui/app.py
# resolves to a *virtual* path that doesn't exist on disk, so Jinja2
# fails to find templates/index.html → request returns 500 with
# "Internal Server Error".
#
# When frozen, PyInstaller exposes `sys._MEIPASS` pointing at the
# unpacked bundle root. Our datas= list in packaging/resonant.spec
# places templates + static at `resonant_client/gui/templates/` and
# `resonant_client/gui/static/` relative to that root. Use those real
# paths for Jinja2 + StaticFiles when frozen; fall back to the dev
# `Path(__file__).parent` layout otherwise.

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _GUI_DIR = Path(sys._MEIPASS) / "resonant_client" / "gui"
else:
    _GUI_DIR = Path(__file__).parent

_TEMPLATES_DIR = _GUI_DIR / "templates"
_STATIC_DIR = _GUI_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ── Application State ─────────────────────────────────────────────────

class AppState:
    """Shared application state."""

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
        # v0.4.4 (T1.4) — `api_url` (Resonant Engine remote) and
        # `lmstudio_url` (LM Studio probe) were retired. Both backends
        # were cut in v0.4.0 but the AppState fields lingered as dead
        # state. Only `ollama_url` survives (set by
        # `refresh_network_defaults` from the resolution chain).
        self.ollama_url = ""
        self.active_thread: Optional[threading.Thread] = None
        self.cancel_requested = threading.Event()
        # Permission / choice flow
        self.permission_response = threading.Event()
        self.permission_result = [True]
        self.choice_response = threading.Event()
        self.choice_result = [""]
        # v0.3.5 — await_user tool flow. The agent's on_user_input
        # callback (defined in `_run_session_streaming`) sets the event
        # and reads the result. The `user_input` WS command from the
        # GUI publishes the user's reply.
        self.user_input_response = threading.Event()
        self.user_input_result = [""]
        # Project / session manager
        self.project = ProjectManager()
        self._first_message_sent = False
        # Guards the connect/init/redetect-time default-backend
        # auto-create: two sockets (desktop window + browser tab) can
        # race the `if not state.backend` check on executor threads.
        self._default_session_lock = threading.Lock()
        # Settings & cost tracking
        self.settings = SettingsManager()
        self._migrate_stale_defaults()
        self._apply_big_context_preset()
        self.permission_mode = str(
            self.settings.get("general", "default_permission_mode", "bypass") or "bypass"
        )
        self.costs = CostTracker()
        self._budget_alert_days: set[str] = set()
        # v0.5.9a2 — per-iteration cost + model attribution. Updated
        # from two layers: the chat-stream `status` event handler
        # records each model's tokens; the autonomous-event forwarder
        # opens/closes buckets at iteration boundaries. The bucket
        # close emits an `autonomous_iteration_cost` event the GUI
        # attaches to the iter card.
        from .autonomous_iter_cost import AutonomousIterCostTracker
        self.iter_cost_tracker = AutonomousIterCostTracker()
        # The autonomous mission whose iteration is currently open.
        # Set by the autonomous-event forwarder when iter_started
        # fires, cleared on iter_complete/_failed. Status events
        # check this before routing tokens to the tracker.
        self._active_autonomous_intent_id: str = ""
        # Project instructions (RESONANT.md)
        self._project_instructions: str | None = None
        self._ws_ref = None
        self._ws_loop = None
        self.evaluations = EvaluationManager(on_event=self._push_ws_event)
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
        self.refresh_network_defaults()
        self.apply_project_context(self.project.project_path, refresh_index=True)

    def _push_ws_event(self, payload: dict) -> None:
        """Best-effort thread-safe delivery for background GUI services."""
        ws = self._ws_ref
        loop = self._ws_loop
        if ws is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
        except Exception:
            logger.debug("background websocket event failed", exc_info=True)

    def _migrate_stale_defaults(self) -> None:
        """
        After the April-2026 refocus, the flagship is Ollama + glm-5.2
        (v0.6.5 — was deepseek-v4-pro v0.5.2–v0.6.4, flash before that).
        Settings persisted from earlier versions may still pin "default_backend"
        to "resonant" or other backends that the user picked once and forgot
        about. We override only when the saved value points at a now-deprecated
        choice; explicit choices for a still-supported backend are preserved.
        """
        try:
            current_backend = str(self.settings.get("general", "default_backend", "") or "").strip()
            # If the user has resonant pinned but no explicit URL configured (the
            # common "leftover from a previous install" pattern), nudge to ollama.
            if current_backend == "resonant":
                try:
                    self.settings.set("general", "default_backend", "ollama")
                except Exception:
                    pass
            current_model = str(self.settings.get("general", "default_model", "") or "").strip()
            if not current_model and current_backend in ("", "ollama"):
                try:
                    # v0.6.5 — glm-5.2:cloud is the flagship default
                    # for new users. Tracks
                    # `network_defaults.get_default_model()`.
                    self.settings.set("general", "default_model", "glm-5.2:cloud")
                except Exception:
                    pass
        except Exception:
            pass

    def _apply_big_context_preset(self) -> None:
        """
        If `general.big_context_profile` is true and the user has NOT manually
        set RESONANT_OLLAMA_NUM_CTX/NUM_BATCH via env, bump them to the
        deepseek-v4-flash 1M-context profile (131072 ctx, 2048 batch).

        Env-var overrides win — we only set defaults if env is unset.
        """
        try:
            enabled = bool(self.settings.get("general", "big_context_profile", False))
        except Exception:
            enabled = False
        if not enabled:
            return
        if "RESONANT_OLLAMA_NUM_CTX" not in os.environ:
            os.environ["RESONANT_OLLAMA_NUM_CTX"] = "131072"
        if "RESONANT_OLLAMA_NUM_BATCH" not in os.environ:
            os.environ["RESONANT_OLLAMA_NUM_BATCH"] = "2048"

    @staticmethod
    def _normalize_path(project_path: str) -> str:
        return os.path.normpath(project_path).replace("\\", "/").lower()

    @staticmethod
    def _resolve_project_path(project_path: str) -> str:
        raw = str(project_path or "").strip().strip('"')
        if not raw:
            raise ValueError("Project path is required.")
        expanded = os.path.expandvars(os.path.expanduser(raw))
        try:
            return str(Path(expanded).resolve(strict=False))
        except OSError:
            return os.path.abspath(os.path.normpath(expanded))

    @classmethod
    def ensure_project_path(cls, project_path: str) -> str:
        """Resolve and materialize a selected project folder.

        Selecting a never-before-seen folder should create the project root and
        its Resonant session storage in one flow. Existing folders are reused.
        """
        resolved = cls._resolve_project_path(project_path)
        path = Path(resolved)
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(f"Project path is not a directory: {resolved}")
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _project_namespace(self, project_path: str) -> str:
        normalized = os.path.normpath(project_path).replace("\\", "/").lower()
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"project:{digest}"

    def _session_auto_approve(self, mode: Optional[str] = None) -> bool:
        return (mode or self.permission_mode) != "ask"

    @staticmethod
    def normalize_session_mode(value: str) -> str:
        return "code"

    @classmethod
    def normalize_session_role(cls, session_mode: str, value: str) -> str:
        return value if value in cls.CODE_SESSION_ROLES else "generator"

    def _get_remote_harness_step_payload(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
        objective: str = "",
        backend=None,
    ) -> dict[str, Any] | None:
        target_backend = backend or self.backend
        backend_name = str(getattr(target_backend, "name", "") or "").strip().lower()
        if backend_name != "resonant" or not hasattr(target_backend, "prepare_harness_step"):
            return None
        try:
            payload = target_backend.prepare_harness_step(
                project_path=project_path,
                session_mode=session_mode,
                session_role=session_role,
                objective=objective,
                execute=False,
            )
        except Exception as exc:
            logger.warning("Falling back to local harness step prep for %s: %s", project_path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def get_intent_service(self, *, on_event=None) -> IntentService:
        """Return the IntentService for the current backend + project.

        Stateful singleton: the same instance survives across WS commands so
        `start_intent` and a later `cancel(intent_id)` reach the same `_active`
        dict. Rebuilt whenever the backend or project changes (so a project
        switch doesn't leak active intents from the prior project).

        `on_event` is rebound on every call — the WebSocket-scoped emitter
        changes per connection.
        """
        from ..engine.tools import AGENT_TOOLS
        signature = (id(self.backend), self.project.project_path)
        existing = getattr(self, "_intent_service", None)
        if existing is None or getattr(self, "_intent_service_signature", None) != signature:
            self._intent_service = IntentService(
                project_path=self.project.project_path,
                backend=self.backend,
                all_tools=list(AGENT_TOOLS),
                project_instructions=(self._project_instructions or ""),
                settings=self.settings,
                on_event=on_event or (lambda ev: None),
                # v0.5.8a1 — wire the per-specialist Ollama model
                # resolver. None override → default backend.
                specialist_backend_resolver=self._build_specialist_backend,
            )
            self._intent_service_signature = signature
        elif on_event is not None:
            self._intent_service.on_event = on_event
        return self._intent_service

    def harness_enabled(self) -> bool:
        """Master switch for the sprint workflow.

        When False (the default): no `.resonant-harness/` directory is created,
        no harness preamble is injected into the system prompt, no message-wrap
        runs, and the role/badge UI stays hidden. The agent operates as a plain
        ReAct loop — the same flow Claude Code / Codex / OpenCode / Cursor offer.

        When True: planner / generator / evaluator roles, sprint contracts,
        evaluator reports, and the autonomous HarnessOrchestrator cycle all wake up.
        """
        return bool(self.settings.get("general", "harness_enabled", False))

    def build_harness_instructions(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
        backend=None,
        objective: str = "",
    ) -> str:
        # Master gate: sprint workflow is opt-in. When off, nothing about the
        # harness leaks into the system prompt.
        if not self.harness_enabled():
            return ""
        # Remote engines that own canonical harness state always take precedence.
        payload = self._get_remote_harness_step_payload(
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("instructions"):
            return str(payload["instructions"])
        # Local fallback: only inject the "Read first: spec.json / progress_state.json /
        # sprint_contract.json / ..." block when there's actually an active sprint.
        # Otherwise every casual question (e.g. "help me with desktop issues") wastes a
        # tool-call cycle reading empty harness files.
        try:
            summary = self.get_harness_summary(project_path) or {}
        except Exception:
            summary = {}
        has_active_sprint = bool(
            summary.get("active_sprint_id")
            and str(summary.get("contract_status") or "").strip()
            in {"approved", "needs_revision"}
        )
        if not has_active_sprint:
            return ""
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
        project_path: Optional[str] = None,
        backend=None,
        objective: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        payload = self._get_remote_harness_step_payload(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("output_contract"):
            return str(payload["output_contract"])
        return self.harness_service.build_output_contract(
            session_mode=session_mode,
            session_role=session_role,
        )

    def get_harness_summary(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self.project.project_path)
        backend_name = str(getattr(self.backend, "name", "") or "").strip().lower()
        if backend_name == "resonant" and hasattr(self.backend, "get_harness_state"):
            try:
                payload = self.backend.get_harness_state(target_path)
                summary = payload.get("summary")
                if isinstance(summary, dict) and summary:
                    return summary
            except Exception as exc:
                logger.warning("Falling back to local harness summary for %s: %s", target_path, exc)
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

    @staticmethod
    def _acceptance_check_tokens(check: str) -> list[str]:
        stopwords = {
            "the",
            "and",
            "with",
            "that",
            "this",
            "from",
            "into",
            "then",
            "when",
            "where",
            "which",
            "does",
            "have",
            "should",
            "while",
            "within",
            "without",
            "used",
            "using",
            "output",
            "includes",
            "include",
            "prints",
            "print",
            "counts",
            "count",
            "flag",
            "default",
            "existing",
            "current",
            "behavior",
            "remain",
            "remains",
            "unchanged",
            "change",
            "stays",
            "stay",
            "file",
            "files",
            "script",
        }
        tokens: list[str] = []
        for token in re.findall(r"[a-z0-9_:+-]+", str(check or "").lower()):
            cleaned = token.strip("_:+-")
            if not cleaned or len(cleaned) <= 1:
                continue
            if cleaned in stopwords or cleaned in {"n", "m"}:
                continue
            tokens.append(cleaned)
        return tokens

    def _build_acceptance_check_coverage(
        self,
        acceptance_checks: list[str],
        evidence_text: str,
    ) -> list[dict[str, Any]]:
        lowered = str(evidence_text or "").lower()
        coverage = []
        for check in acceptance_checks[:8]:
            phrase = self._normalize_acceptance_check_phrase(check)
            tokens = self._acceptance_check_tokens(check)
            overlap = [token for token in tokens if token in lowered]
            required_overlap = 0
            if tokens:
                required_overlap = max(2, min(len(tokens), 3))
                if len(tokens) <= 2:
                    required_overlap = len(tokens)
            matched = bool(phrase and phrase in lowered)
            if not matched and tokens:
                matched = len(overlap) >= required_overlap
            coverage.append(
                {
                    "check": check,
                    "matched": matched,
                    "normalized_phrase": phrase,
                    "matched_tokens": overlap[:6],
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
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
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
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
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
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
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
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))
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
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))

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
                    "\n".join(validation_artifacts),
                    "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
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
        """v0.4.0 — harness teacher escalation no longer has a frontier
        CLI to escalate to (Claude Code / Codex were cut). Always
        return the current Ollama backend with a stronger model than
        the running one when available — `deepseek-v4-pro:cloud` if
        the user has it, otherwise the running model is the ceiling.
        """
        if not self.available_backends:
            self.detect_backends()
        ollama_models = self.available_backends.get("ollama", {}).get("models", [])
        # Prefer pro-class models for escalation if present.
        for stronger in ("deepseek-v4-pro:cloud", "deepseek-v4:cloud"):
            if stronger in ollama_models:
                return "ollama", stronger
        # Fall back to whatever the running backend is using.
        running_model = getattr(self.backend, "model", "") if self.backend else ""
        if running_model:
            return "ollama", running_model
        if ollama_models:
            return "ollama", ollama_models[0]
        raise ValueError("No Ollama models available for harness escalation")

    def wrap_user_message_for_harness(
        self,
        *,
        user_msg: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self.normalize_session_mode(session_mode)
        session_role = self.normalize_session_role(session_mode, session_role)

        payload = self._get_remote_harness_step_payload(
            project_path=self.project.project_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=user_msg,
        )
        summary = payload.get("summary_before") if payload else None
        if not isinstance(summary, dict) or not summary:
            summary = self.get_harness_summary(self.project.project_path)
        role_requirements = {
            "planner": "Create or refine the spec and propose the next sprint contract. Keep implementation out unless the user explicitly asks for it.",
            "generator": "Implement only the active sprint. Update progress and handoff artifacts before finishing.",
            "evaluator": "Verify against the sprint contract. Write a clear pass, revise, or blocked verdict with concrete required revisions.",
        }[session_role]
        output_contract = (
            str(payload.get("output_contract") or "")
            if payload else
            self.build_harness_output_contract(
                session_mode=session_mode,
                session_role=session_role,
                project_path=self.project.project_path,
            )
        )
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
            f"FINAL OUTPUT CONTRACT:\n{output_contract}\n\n"
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
        if bool(planner_payload.get("ready_for_generator")):
            contract["status"] = "approved"
        if bool(planner_payload.get("handoff_ready")) and str(planner_payload.get("next_role") or "").strip() == "generator":
            contract["status"] = "approved"

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
        if not text:
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
        backend=None,
        objective: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self.project.project_path)
        payload = self._get_remote_harness_step_payload(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("resume_prompt"):
            return str(payload["resume_prompt"])
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
            backend = getattr(target, "backend", None)
            configure = getattr(backend, "configure_permission_mode", None)
            if callable(configure):
                configure(self.permission_mode)
        if self.backend_spec and self.backend_spec.backend_type == "codex":
            self.backend_spec.permission_mode = self.permission_mode
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
        project_path = self.ensure_project_path(project_path or self.project.project_path or os.getcwd())
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
        # Migrate legacy .resonant-harness/ to ~/.resonant/projects/<hash>/harness/
        # whether or not sprint mode is on — keeps the user's repo clean either way.
        # The notice gets surfaced via the next init payload (see _last_migration_notice).
        self._last_migration_notice = ""
        try:
            migrated = self.harness.maybe_migrate_legacy_layout()
            if migrated:
                self._last_migration_notice = (
                    f"Moved {migrated} harness file(s) from .resonant-harness/ to "
                    f"~/.resonant/projects/. You can `git rm -r .resonant-harness/` "
                    f"when you're ready."
                )
                logger.info(self._last_migration_notice)
        except Exception as exc:
            logger.warning("Harness legacy migration check failed: %s", exc)
        # Only materialize the new harness root when the user has opted in.
        if self.harness_enabled():
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
        from ..orchestration.skill_loader import build_skill_context
        session._skill_context_provider = lambda query: build_skill_context(
            query,
            project_path=target_path,
            max_skills=6,
        )
        session.auto_approve = self._session_auto_approve()
        return session

    def detect_backends(self):
        """v0.4.0 — Ollama-only detection. Single network probe to the
        configured Ollama URL (Mac Studio at 10.0.0.133 by default per
        the user's infra; falls back to whatever `ollama_url` resolves
        to). Returns `{"ollama": {url, models}}` on success or `{}`
        when Ollama isn't reachable — the welcome screen reads the
        empty dict and renders the Ollama setup wizard.
        """
        import httpx

        self.refresh_network_defaults()
        ollama_url = self.ollama_url
        available: dict = {}

        # Short connect timeout so an unreachable host doesn't block
        # startup — the wizard handles the unreachable case explicitly.
        _timeout = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=4.0)

        try:
            resp = httpx.get(f"{ollama_url}/api/tags", timeout=_timeout)
            resp.raise_for_status()
            data = resp.json()
            # Filter out non-chat models (embeddings, rerankers).
            models = [m["name"] for m in data.get("models", [])
                      if not any(kw in m["name"].lower()
                                 for kw in ("embed", "bert", "bge", "nomic"))]
            # Append cloud models that aren't already pulled locally so
            # the user sees them as options even on a fresh Ollama.
            local_set = {m.lower() for m in models}
            for cloud in OllamaBackend.CLOUD_MODELS:
                if cloud.lower() not in local_set:
                    models.append(cloud)
            if models:
                available["ollama"] = {"url": ollama_url, "models": models}
        except Exception:
            # Non-fatal — empty available means "show the Ollama wizard."
            pass

        codex_cli = resolve_codex_cli_path()
        if codex_cli:
            available["codex"] = {
                "models": CodexCliBackend.list_available_models(),
                "model_labels": codex_cli_model_labels(),
                "cli_path": codex_cli,
            }

        self.available_backends = available
        return available

    def select_harness_backend(
        self,
        *,
        session_role: str,
        project_path: Optional[str] = None,
    ) -> tuple[str, str]:
        """v0.4.0 — single-backend (Ollama). All harness roles share the
        same backend; only the *model* varies based on role + the user's
        forced overrides via `RESONANT_HARNESS_<ROLE>_MODEL`. Cut the
        whole multi-backend preference walk that existed pre-v0.4.0
        — there's only one backend now.

        Default model selection:
        - planner / evaluator → `deepseek-v4-pro:cloud` if available
          (more deliberate, better reasoning) else flash
        - generator → `deepseek-v4-flash:cloud` (faster turnaround)
        - falls through to whatever the running backend is using or
          the first available Ollama model
        """
        if not self.available_backends:
            self.detect_backends()
        project_path = os.path.normpath(project_path or self.project.project_path)

        info = self.available_backends.get("ollama")
        if not info:
            raise ValueError(
                "No Ollama backend available — start Ollama (or fix the URL) "
                "before running a harness role."
            )
        models = list(info.get("models") or [])

        role_env = session_role.upper()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_MODEL", "") or "").strip()

        if forced_model:
            spec = self.build_backend_spec("ollama", model=forced_model, project_path=project_path)
            return spec.backend_type, spec.model

        # Role-specific model preference order.
        role_preference = {
            "planner": ["deepseek-v4-pro:cloud", "deepseek-v4-flash:cloud"],
            "evaluator": ["deepseek-v4-pro:cloud", "deepseek-v4-flash:cloud"],
            "generator": ["deepseek-v4-flash:cloud", "deepseek-v4-pro:cloud"],
        }.get(session_role, ["deepseek-v4-flash:cloud"])

        chosen_model = ""
        # Honor the running backend's model first if it's still in the list.
        if (self.backend_spec and self.backend_spec.backend_type == "ollama"
                and self.backend_spec.model in models):
            chosen_model = self.backend_spec.model
        else:
            for candidate in role_preference:
                if candidate in models:
                    chosen_model = candidate
                    break
            if not chosen_model and models:
                chosen_model = models[0]

        if not chosen_model:
            raise ValueError(f"No model available for harness role '{session_role}'")

        spec = self.build_backend_spec("ollama", model=chosen_model, project_path=project_path)
        return spec.backend_type, spec.model

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
        """v0.4.0 — single-backend means cross-backend retry is gone.
        Retry uses the *other* deepseek model: pro→flash if pro failed,
        flash→pro if flash failed. Returns ("", "") when no retry
        candidate exists (e.g. only one model available, or env override
        disables it).
        """
        if not self.available_backends:
            self.detect_backends()
        project_path = os.path.normpath(project_path or self.project.project_path)
        role_env = session_role.upper()
        forced_backend = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_MODEL", "") or "").strip()
        if forced_backend.lower() in {"disabled", "none", "off", "false", "no"}:
            return "", ""

        info = self.available_backends.get("ollama")
        if not info:
            return "", ""
        models = list(info.get("models") or [])

        # Honor explicit override.
        if forced_model:
            spec = self.build_backend_spec("ollama", model=forced_model, project_path=project_path)
            return spec.backend_type, spec.model

        # Pick the OTHER deepseek tier as the retry. If the failed run
        # was on flash, escalate to pro; if it was on pro, fall back to
        # flash for a faster second pass.
        running_model = getattr(self.backend, "model", "") if self.backend else ""
        retry_pairs = [
            ("deepseek-v4-flash:cloud", "deepseek-v4-pro:cloud"),
            ("deepseek-v4-pro:cloud", "deepseek-v4-flash:cloud"),
        ]
        for primary, retry in retry_pairs:
            if running_model == primary and retry in models:
                spec = self.build_backend_spec("ollama", model=retry, project_path=project_path)
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

        # v0.4.0 — pre-cut, this branch routed Resonant Engine harness
        # cycles through `backend.prepare_harness_step` for remote
        # execution. With ResonantBackend gone, every harness role runs
        # the local session loop below.
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
            if backend_type == "codex":
                spec.cwd = project_path
                spec.permission_mode = self.permission_mode
            return spec

        # v0.4.0 — Ollama is the only supported backend. Reject anything
        # else with a message that points the user at the right tool
        # rather than silently failing.
        if backend_type == "codex":
            info = self.available_backends.get("codex") or {}
            models = info.get("models") or CodexCliBackend.list_available_models()
            selected_model = model or self._resolve_default_model(models)
            spec = BackendSpec(backend_type="codex", model=selected_model)
            spec.cwd = project_path
            spec.permission_mode = self.permission_mode
            return spec

        if backend_type != "ollama":
            raise ValueError(
                f"Backend '{backend_type}' is not supported. Resonant Client "
                f"supports Ollama and Codex."
            )

        info = self.available_backends.get("ollama")
        if not info:
            raise ValueError(
                "Ollama is not reachable. Check the URL in Settings → Network "
                "(default Mac Studio: http://10.0.0.133:11434) and that "
                "`ollama serve` is running."
            )

        models = info.get("models") or []
        selected_model = model or self._resolve_default_model(models)
        spec = BackendSpec(backend_type="ollama", model=selected_model)
        spec.url = info.get("url", "")
        # v0.6.5 — seed the per-model default thinking level on a freshly
        # built spec (no prior spec/session to inherit from). GLM-5.x
        # defaults to high-effort thinking; create_backend / swap_backend
        # still let a preserved per-session choice (including an explicit
        # "off") win on rebuilds, so this only sets the initial default.
        if not spec.thinking_mode:
            spec.thinking_mode = default_thinking_for_model(selected_model)
        return spec

    def _resolve_default_model(self, models: list[str]) -> str:
        """v0.5.7a1 — when no explicit model is supplied, honor the
        user-configured `general.default_model` setting if it's
        present in the detected models list (case-insensitive).
        Falls through to the previous behavior (first detected
        model) when the configured value is missing or unavailable.

        Linux-bridge field-observation #4: project switches were
        landing on `models[0]` (typically deepseek-v4-flash by
        Ollama's tag ordering) instead of the user's pinned
        `deepseek-v4-pro:cloud` default. The `apply_safe_default_backend`
        path only sets `default_model` once on first launch, so the
        setting was correct — it just wasn't being read at session-
        construction time.
        """
        if not models:
            return ""
        try:
            configured = str(
                self.settings.get("general", "default_model", "") or ""
            ).strip()
        except Exception:
            configured = ""
        if not configured:
            return models[0]
        # Case-insensitive lookup so `Deepseek-V4-Pro:Cloud` etc. don't
        # silently miss. Return the canonical form from the models list
        # so the spec carries the exact tag Ollama returned.
        configured_lower = configured.lower()
        for m in models:
            if m.lower() == configured_lower:
                return m
        # Configured model isn't currently available (not pulled, not
        # in CLOUD_MODELS, etc.). Fall back to first detected so the
        # session can still spin up — silent fallback matches the
        # pre-v0.5.7 behavior so we don't introduce a new failure mode.
        return models[0]

    def _resolve_specialist_model_override(self, specialization: str) -> str:
        """v0.5.8a1 — return the configured Ollama model override for a
        specialist, or "" to fall through to the default backend's model.

        Resolution order:
          1. `general.specialist_model_overrides[<spec>]` from settings.json
          2. `RESONANT_SPECIALIST_<NAME>_MODEL` env var (uppercase
             specialization, e.g. RESONANT_SPECIALIST_REFLECT_MODEL)

        Settings wins over env-var so persistent UI configuration is
        authoritative. Both empty → "" → caller uses default.

        Spec keys are lowercased NodeSpecialization values: "reflect",
        "plan_deep", "implement", "explore", "verify", "research",
        "plan", "repair".

        v0.5.8a1 motivation: linux-bridge run hit `verdict=stuck` on a
        path-mismatch that a stronger model would arguably resolve. The
        user can pin `deepseek-v4-pro:cloud` to REFLECT and PLAN_DEEP
        (the high-leverage "decide" moments) and leave `flash` for the
        IMPLEMENT/EXPLORE bulk — closing some of the model-capability
        gap without leaving the local-first positioning.
        """
        spec_key = (specialization or "").strip().lower()
        if not spec_key:
            return ""
        # 1. Settings.
        try:
            overrides = self.settings.get(
                "general", "specialist_model_overrides", {},
            ) or {}
        except Exception:
            overrides = {}
        if isinstance(overrides, dict):
            v = overrides.get(spec_key, "")
            if isinstance(v, str) and v.strip():
                return v.strip()
        # 2. Env var.
        env_key = f"RESONANT_SPECIALIST_{spec_key.upper()}_MODEL"
        return str(os.environ.get(env_key, "") or "").strip()

    def _build_specialist_backend(self, specialization: str) -> Optional[Any]:
        """v0.5.8a1 — production resolver passed to LocalSpecialistRunner.
        Returns a fresh OllamaBackend with the override model when one
        is configured for this specialist, OR None to signal "use the
        runner's default backend".

        We construct a fresh backend per call rather than caching:
          - OllamaBackend keeps per-instance caches keyed on `model`
            (tool support, vision support); reusing the class is fine.
          - The Settings reference, base_url, and thinking_mode are
            inherited from the active default backend so per-specialist
            overrides only swap the *model*, not the network or
            reasoning-effort config.
          - Fresh-per-call avoids stale-cache bugs when the user
            changes the override mid-session.
        """
        override = self._resolve_specialist_model_override(specialization)
        # We can only override an Ollama backend (the only supported
        # backend in v0.4.0+). Defensive: if some future codepath
        # somehow gets here with a non-Ollama default, fall through to
        # the default rather than crashing.
        if not isinstance(self.backend, OllamaBackend):
            logger.warning(
                "specialist override requested for %s but default backend "
                "is %s; falling through",
                specialization, type(self.backend).__name__,
            )
            return None
        try:
            # Reuse base_url + thinking from the default backend so
            # only the model changes. `thinking_mode` is the normalized
            # form (None / "low" / "med" / "high") that OllamaBackend
            # accepts back via its constructor.
            base_url = self.backend.base_url
            thinking = getattr(self.backend, "thinking_mode", None)
            target_model = override or self.backend.model
            hard_reasoning_phase = (specialization or "").strip().lower() in {
                "plan_deep", "reflect", "verify", "repair",
            }
            flagship = any(
                marker in target_model.lower()
                for marker in ("glm-5.2", "deepseek-v4")
            )
            if hard_reasoning_phase and flagship and thinking not in {None, "off"}:
                thinking = "max"
            if not override and thinking == getattr(self.backend, "thinking_mode", None):
                return None
            return OllamaBackend(
                base_url=base_url,
                model=target_model,
                thinking=thinking,
            )
        except Exception:
            logger.exception(
                "failed to build specialist backend for %s (model=%s); "
                "falling through to default",
                specialization, override,
            )
            return None

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
        effective_root = os.path.normpath(project_path or self.project.project_path)
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

        if self._normalize_path(effective_root) == self._normalize_path(self.project.project_path):
            project_instructions = self._project_instructions or load_project_instructions(effective_root)
            engram = self.engram
            codebase_index = self.codebase_index
        else:
            project_instructions = load_project_instructions(effective_root)
            engram = self.base_engram.clone(namespace=self._project_namespace(effective_root))
            engram.set_mcp_manager(self.mcp_manager)
            codebase_index = CodebaseIndex(effective_root, engram=engram)

        harness_instructions = self.build_harness_instructions(
            project_path=effective_root,
            session_mode=session_mode,
            session_role=session_role,
            backend=backend,
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
            max_tokens=max_tokens or self.SESSION_MAX_TOKENS,
            auto_approve=self._session_auto_approve() if auto_approve is None else auto_approve,
            allowed_tools=allowed_tools,
            project_instructions=project_instructions,
            cancel_event=cancel_event,
        )
        session.project_path = effective_root  # Tools + sandbox cwd

        # Attach sandbox for path safety (always on for sessions with a project)
        from ..engine.sandbox import PathSandbox
        session.sandbox = PathSandbox(effective_root, enabled=True)

        # Set autonomy tier based on permission mode
        if self.permission_mode == "ask":
            session.autonomy_tier = "suggest"
        elif self.permission_mode == "bypass":
            session.autonomy_tier = "full-auto"
        else:
            session.autonomy_tier = "auto-edit"

        # Attach JSONL event logger
        try:
            from ..engine.event_log import EventLogger
            import uuid as _uuid
            session.event_logger = EventLogger(
                session_id=_uuid.uuid4().hex[:12],
                enabled=bool(self.settings.get("event_logging", "enabled", True)),
            )
        except Exception:
            pass

        # Attach execution policy (tier-based defaults + project overrides)
        try:
            from ..engine.policies import policy_for_tier, ExecutionPolicy
            base_policy = policy_for_tier(session.autonomy_tier)
            # Check for project-level policy file
            project_policy = ExecutionPolicy.from_file(
                os.path.join(effective_root, "resonant-policy.json")
            )
            if project_policy:
                session.execution_policy = base_policy.merge(project_policy)
            else:
                session.execution_policy = base_policy
        except Exception:
            pass

        # Auto-feedback loops (lint / test) — settings-driven, off by default
        session.auto_lint_enabled = bool(
            self.settings.get("general", "auto_lint_after_edits", False)
        )
        session.auto_test_enabled = bool(
            self.settings.get("general", "auto_test_after_edits", False)
        )
        configured_test_cmd = str(
            self.settings.get("general", "auto_test_command", "") or ""
        ).strip()
        if configured_test_cmd:
            session.auto_test_command = configured_test_cmd

        return self._wire_session(
            session,
            project_path=effective_root,
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
        # Preserve thinking_mode from the previous spec (or current session) when
        # the backend is being rebuilt for the same model — UX-driven option swaps.
        prior_thinking = ""
        if self.backend_spec and self.backend_spec.backend_type == backend_type:
            prior_thinking = self.backend_spec.thinking_mode or ""
        if not prior_thinking and self.project.current_session:
            prior_thinking = getattr(self.project.current_session, "thinking_mode", "") or ""
        if prior_thinking and backend_type == "ollama":
            spec.thinking_mode = prior_thinking

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

    def default_chat_backend_choice(self) -> tuple[str, str]:
        """Return the default chat backend/model from detected providers."""
        configured_backend = str(
            self.settings.get("general", "default_backend", "") or ""
        ).strip().lower()
        backend_order = []
        if configured_backend:
            backend_order.append(configured_backend)
        backend_order.extend(k for k in ("ollama", "codex") if k not in backend_order)

        for backend_type in backend_order:
            info = self.available_backends.get(backend_type) or {}
            models = list(info.get("models") or [])
            if models:
                return backend_type, self._resolve_default_model(models)
        return "", ""

    def ensure_default_runtime_session(self, *, session_role: str = "generator") -> bool:
        """Create an in-memory backend/session so the composer is usable.

        This deliberately does not create a persisted SessionRecord. A sidebar
        session is written only once the user sends the first message, which
        keeps cold starts from filling the list with empty "New session" rows.
        """
        if self.backend:
            return False
        with self._default_session_lock:
            if self.backend:
                return False
            backend_type, model = self.default_chat_backend_choice()
            if not backend_type or not model:
                return False
            self.create_backend(
                backend_type,
                model,
                session_mode="code",
                session_role=session_role or "generator",
            )
            self._first_message_sent = False
            self.costs.reset_session()
            return True

    def ensure_persisted_current_session(self, *, session_role: str = "generator"):
        """Create the on-disk session record lazily on first user message."""
        if self.project.current_session or not self.backend:
            return self.project.current_session
        return self.project.create_session(
            backend_type=getattr(self.backend, "name", ""),
            model=getattr(self.backend, "model", ""),
            session_role=session_role or "generator",
        )

    # CLI-wrapper backends manage their own session via --resume <id> and
    # ignore the conversation_history list we pass to .stream(). When the
    # user swaps TO one of these mid-conversation, they need a heads-up
    # that history won't carry over.
    # v0.4.0 — kept as empty set for compatibility with existing call sites
    # (one branch in switch_model checks it). Pre-v0.4.0 this held the set
    # of backends whose CLI sessions ignored our conversation_history.
    CLI_WRAPPED_BACKENDS: set = {"codex"}

    def swap_backend(
        self,
        backend_type: str,
        model: str = None,
        *,
        session_mode: str = "code",
        session_role: str = "generator",
    ):
        """
        Swap the backend on the EXISTING session (preserves conversation_history,
        todos, allowed_tools, sandbox, autonomy_tier, event_logger, etc.).

        This is the right operation when the user changes the model/backend
        dropdown — they want to swap "the brain", not start over.

        Bug #9+#10 fix: previously the `switch_model` WebSocket handler called
        `create_backend()` which always rebuilds the session from scratch via
        `build_session()`, silently discarding conversation_history. This new
        method only mutates `self.backend`, `self.backend_spec`, and the
        existing `self.session.backend` — nothing else changes.

        For CLI-wrapper backends (claude-code, codex) the conversation_history
        survives in the session but isn't visible to the new backend's CLI
        process — the GUI emits `backend_swap_warning` separately so the user
        knows. For HTTP-based backends (ollama, claude API, openai, lmstudio,
        mlx, resonant) the new backend's stream() reads the preserved history
        and produces a coherent next turn.

        Returns the new backend (same shape as create_backend for compatibility).
        """
        spec = self.build_backend_spec(backend_type, model=model, project_path=self.project.project_path)

        # Preserve thinking_mode (mirrors create_backend's logic).
        prior_thinking = ""
        if self.backend_spec and self.backend_spec.backend_type == backend_type:
            prior_thinking = self.backend_spec.thinking_mode or ""
        if not prior_thinking and self.project.current_session:
            prior_thinking = getattr(self.project.current_session, "thinking_mode", "") or ""
        if prior_thinking and backend_type == "ollama":
            spec.thinking_mode = prior_thinking

        new_backend = spec.create_backend(self.settings)
        self.backend = new_backend
        self.backend_spec = spec

        if self.session is None:
            # Fall through to the full create path on the rare case where there's
            # no session yet (initial app boot before any project is open).
            return self.create_backend(
                backend_type,
                model=model,
                session_mode=session_mode,
                session_role=session_role,
            )

        # The whole point of swap_backend: just rewire .backend on the existing
        # session, leaving conversation_history + todos + everything else intact.
        self.session.set_backend(new_backend)  # set_backend defaults to reset_history=False
        return new_backend

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
            self.backend_spec.backend_type == "ollama" and
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
        # v0.4.0 — single Ollama URL resolution chain (env → settings →
        # Mac Studio default at 10.0.0.133). See `resolve_ollama_url`
        # for why localhost is NOT a silent fallback.
        # v0.4.4 (T1.4) — `api_url` (Resonant Engine remote) and
        # `lmstudio_url` (LM Studio) resolutions retired with their
        # backends.
        settings_data = self.settings.get_all()
        self.ollama_url = resolve_ollama_url(settings_data=settings_data)

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

    def get_init_data(self, refresh_only: bool = False) -> dict:
        """Get initial state for the frontend."""
        backends_info = {}
        for key, info in self.available_backends.items():
            entry = {"name": key}
            if "models" in info:
                entry["models"] = info["models"]
            if "model_labels" in info:
                entry["model_labels"] = info["model_labels"]
            if "url" in info:
                entry["url"] = info["url"]
            if "cli_path" in info:
                entry["cli_path"] = info["cli_path"]
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
            "current_session_role": self.project.current_session.session_role if self.project.current_session else "generator",
            "current_thinking_mode": (
                getattr(self.backend_spec, "thinking_mode", "") if self.backend_spec else ""
            ) or (
                getattr(self.project.current_session, "thinking_mode", "") if self.project.current_session else ""
            ),
            "recent_projects": self.project.get_recent_projects(),
            "playground_project": self.project.get_playground_project(),
            "settings": self.settings.get_masked(),
            "resonant_md": get_instruction_info(self.project.project_path),
            "rag": self.codebase_index.get_stats() if self.codebase_index else {"total_files": 0, "is_indexed": False},
            "harness": self.get_harness_summary(self.project.project_path),
            "harness_cycles": self.harness_orchestrator.list_runs(),
            "harness_enabled": self.harness_enabled(),
            "harness_migration_notice": getattr(self, "_last_migration_notice", "") or "",
            # v0.5.3a2 — Resume orphaned autonomous missions. Sessions
            # in `autonomous_running` phase whose daemon isn't currently
            # registered (server restart / crash / sleep). Frontend
            # surfaces a "Resume" affordance per orphan.
            "autonomous_orphans": _find_orphaned_autonomous_missions(self),
            # v0.5.5a2 — every autonomous mission for the project,
            # not just orphans. Powers the sidebar mission browser.
            # Includes running + complete + paused + failed, sorted
            # newest-first by autonomous_started_at.
            "autonomous_missions": _list_autonomous_missions(self),
        }


state = AppState()


# ── Autonomous-event forwarding helper (v0.5.6a3) ─────────────────────


# Set of WS event kinds that signal the daemon hit a terminal state.
# The forwarder intercepts these to update session.mission_state.phase
# in lock-step with the GUI badge transition. Without this update the
# session record stays in `autonomous_running` after the daemon stops,
# which makes the orphan-detection scanner falsely offer to resume a
# satisfied/stuck mission on the next app launch (linux-bridge
# field-observation #6).
_AUTONOMOUS_TERMINAL_EVENTS: frozenset[str] = frozenset({
    "autonomous_mission_complete",
    "autonomous_mission_paused",
    "autonomous_mission_failed",
})


def _make_autonomous_event_forwarder(
    target_state: Any, ws: "WebSocket", loop,
) -> Callable[[dict], None]:
    """Build a callback that forwards every daemon event to the WS
    AND, on terminal events, updates session.mission_state.phase +
    emits sessions_updated so the sidebar / orphan-list / inspector
    all converge on the new state atomically.

    Daemon-side roadmap.md status update happens FIRST inside
    `AutonomousMissionDaemon._emit_stop` / failed path (v0.5.6a3); this
    forwarder closes the loop on the session-record side so:
      1. roadmap.md `**Status:**` ← reflects terminal state (daemon)
      2. session.mission_state.phase ← reflects terminal state (here)
      3. GUI badge ← reflects terminal state (frontend on event)
    All three converge before the next user action can observe drift.
    """

    def _forward(payload: dict) -> None:
        # 1. Forward to WS first so the GUI gets the event ASAP.
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
        except Exception:
            logger.debug("autonomous emit raised", exc_info=True)

        kind = payload.get("event", "")
        intent_id = payload.get("intent_id", "")

        # v0.5.9a2 — open / close / route per-iter cost buckets.
        # iter_started → open the bucket + mark this intent as the
        # active one for status routing. iter_complete / _failed →
        # close the bucket + emit autonomous_iteration_cost with the
        # accumulated breakdown.
        try:
            tracker = getattr(target_state, "iter_cost_tracker", None)
            if tracker is not None and intent_id:
                if kind == "autonomous_iteration_started":
                    iter_count = int(payload.get("iter_count", 0) or 0)
                    tracker.on_iteration_started(
                        intent_id, iter_count,
                        started_at=time.time(),
                    )
                    target_state._active_autonomous_intent_id = intent_id
                elif kind in (
                    "autonomous_iteration_complete",
                    "autonomous_iteration_failed",
                ):
                    iter_count = int(payload.get("iter_count", 0) or 0)
                    snap = tracker.on_iteration_finalized(
                        intent_id, iter_count,
                        finalized_at=time.time(),
                    )
                    if (target_state._active_autonomous_intent_id
                            == intent_id):
                        target_state._active_autonomous_intent_id = ""
                    if snap is not None:
                        # Emit a separate event so the GUI can
                        # attach cost data to the iter card without
                        # the daemon needing to know about cost
                        # tracking. event-name distinct from the
                        # iter event itself so old GUIs ignore it.
                        cost_payload = {
                            "event": "autonomous_iteration_cost",
                            "intent_id": intent_id,
                            **snap.to_payload(),
                        }
                        try:
                            asyncio.run_coroutine_threadsafe(
                                ws.send_json(cost_payload), loop,
                            )
                        except Exception:
                            logger.debug(
                                "autonomous_iteration_cost emit raised",
                                exc_info=True,
                            )
        except Exception:
            logger.exception(
                "iter_cost_tracker update raised for kind=%s", kind,
            )

        # 2. On terminal events, update session-record phase. The
        # daemon already supplied the target phase as `new_phase` in
        # the payload (v0.5.6a3 contract).
        if kind not in _AUTONOMOUS_TERMINAL_EVENTS:
            return
        new_phase = payload.get("new_phase", "")
        if not new_phase:
            return  # daemon didn't supply — defensive no-op
        # v0.5.9a2 — also reset the cost tracker for this intent so
        # buckets from this terminal mission don't haunt a future
        # mission with the same intent_id (extremely unlikely but
        # cheap to guard against).
        try:
            tracker = getattr(target_state, "iter_cost_tracker", None)
            if tracker is not None and intent_id:
                tracker.reset_intent(intent_id)
        except Exception:
            logger.debug("iter_cost_tracker reset raised", exc_info=True)
        try:
            sess = getattr(target_state, "project", None)
            cur = getattr(sess, "current_session", None) if sess else None
            ms = getattr(cur, "mission_state", None) if cur else None
            # Only update if this terminal event is for the current
            # session's mission — otherwise we'd corrupt a different
            # session's state.
            if not ms or ms.get("intent_id") != intent_id:
                return
            cur.advance_mission_phase(new_phase)
            cur.save()
        except Exception:
            logger.exception(
                "autonomous terminal-event session-phase update failed "
                "(intent=%s, new_phase=%s)",
                intent_id, new_phase,
            )
            return

        # 3. Emit sessions_updated so sidebar + orphan list + browser
        # all reconcile against the new phase.
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({
                    "event": "sessions_updated",
                    "sessions": target_state.project.list_sessions(),
                    "all_sessions": target_state.project.list_all_sessions(),
                    "current_session_id": (
                        target_state.project.current_session.id
                        if target_state.project.current_session else ""
                    ),
                }),
                loop,
            )
        except Exception:
            logger.debug(
                "sessions_updated emit after terminal event raised",
                exc_info=True,
            )

    return _forward


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

    if not state.backend and state.available_backends:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: state.ensure_default_runtime_session(),
            )
        except Exception:
            logger.exception("default runtime session startup failed")

    # Initialize codebase index if not already set
    if not state.codebase_index and state.project:
        state.codebase_index = CodebaseIndex(state.project.project_path, engram=state.engram)

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            command = msg.get("command", "")

            if command == "init":
                if not state.backend and state.available_backends:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: state.ensure_default_runtime_session(),
                        )
                    except Exception:
                        logger.exception("default runtime session init failed")
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
                # v0.4.5 (T1.5) — pre-cut, this had a resonant-engine
                # branch that delegated to the remote backend's
                # `start_harness_cycle`. With ResonantBackend gone,
                # the local `state.harness_orchestrator` is the only
                # path.
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
                # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
                # to the remote backend's `set_harness_sprint`. Local
                # `state.harness.set_active_sprint` is the only path now.
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
                # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
                # to the remote backend's `set_harness_contract_status`.
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
                # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
                # to the remote backend's `set_evaluator_verdict`.
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
                    state.project.create_session(
                        backend_type=backend_type,
                        model=model or "",
                        session_role=session_role,
                    )
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.create_backend(
                            backend_type,
                            model or None,
                            session_mode=session_mode,
                            session_role=session_role,
                        ),
                    )
                    state._first_message_sent = False
                    await ws.send_json(state.get_init_data())
                    await ws.send_json({"event": "status_msg", "message": f"Connected to {backend_type}"})

                    # Pre-warm the model so the user's first message doesn't sit
                    # at "thinking" for 60-90s while Ollama cold-loads. Fire and
                    # forget — we don't want to block the connect response.
                    backend_for_warm = state.backend
                    if backend_for_warm and hasattr(backend_for_warm, "warm_up"):
                        async def _emit_warm_event(payload: dict):
                            try:
                                await ws.send_json(payload)
                            except Exception:
                                pass

                        loop = asyncio.get_running_loop()
                        warm_started = time.time()
                        await ws.send_json({
                            "event": "model_warmup_started",
                            "backend": backend_type,
                            "model": getattr(backend_for_warm, "model", model),
                        })

                        def _warm_in_bg(be=backend_for_warm, started=warm_started):
                            try:
                                be.warm_up()
                            except Exception as exc:
                                logger.debug("warm_up raised: %s", exc)
                            elapsed = time.time() - started
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    _emit_warm_event({
                                        "event": "model_warmup_complete",
                                        "elapsed_s": round(elapsed, 1),
                                    }),
                                    loop,
                                )
                            except Exception:
                                pass

                        threading.Thread(target=_warm_in_bg, name="model-warmup", daemon=True).start()
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

                # session_mode was removed from SessionRecord in the April-2026 refocus.
                # Always "code" now (the only mode).
                session_mode = "code"
                session_role = (
                    state.project.current_session.session_role
                    if state.project.current_session else "generator"
                )
                if not state.project.current_session:
                    state.ensure_persisted_current_session(session_role=session_role)

                # Auto-title session from first message
                if not state._first_message_sent:
                    state.project.update_session_title(text)

                # Sprint workflow is opt-in. The wrap only runs when (a) the master
                # setting is on AND (b) an active sprint contract exists. Otherwise
                # the message goes through unmodified — same as Claude Code / Codex.
                text_for_session = text
                if not state._first_message_sent and state.harness_enabled():
                    harness_summary = state.get_harness_summary(state.project.project_path)
                    has_active_sprint = bool(
                        harness_summary.get("active_sprint_id")
                        and harness_summary.get("contract_status") in {"approved", "needs_revision"}
                    )
                    if has_active_sprint:
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

            elif command == "mission_start":
                # Toggle-driven Mission entry. Always creates a fresh chat
                # session so the grill phase has a clean slate (mixing it
                # with prior chat would pollute the interview). The new
                # session is flagged with mission_state, which gates the
                # spec-extraction scan and drives the header badge.
                # See docs/long-running-agents.md (Phase 1).
                feature = (msg.get("feature") or "").strip()
                if not feature:
                    await ws.send_json({"event": "error",
                                        "message": "Mission feature description required"})
                    continue
                if not state.backend:
                    await ws.send_json({"event": "error",
                                        "message": "Connect a backend before starting a mission"})
                    continue
                if state.active_thread and state.active_thread.is_alive():
                    await ws.send_json({"event": "error",
                                        "message": "A session is already running"})
                    continue

                # v0.3.3 — accept an explicit project path from the
                # composer so the mission writes files where the user
                # expects, not wherever os.getcwd() happened to land
                # (Bug #25: bundled exe cwd was C:\Program Files\...).
                # We swap the project context BEFORE creating the
                # session so all specialist hand-offs see the right
                # path.
                requested_path = (msg.get("project_path") or "").strip()
                if requested_path:
                    try:
                        norm_requested = os.path.normpath(requested_path)
                        if not os.path.isdir(norm_requested):
                            try:
                                os.makedirs(norm_requested, exist_ok=True)
                            except OSError as exc:
                                await ws.send_json({"event": "error",
                                                    "message": f"Could not create project folder: {exc}"})
                                continue
                        state.apply_project_context(norm_requested, refresh_index=True)
                    except Exception as exc:
                        logger.exception("mission_start: apply_project_context failed")
                        await ws.send_json({"event": "error",
                                            "message": f"Failed to switch project: {exc}"})
                        continue

                # Create the fresh session — mirrors `clear` flow.
                session_role = msg.get("session_role", "generator")
                backend_type = getattr(state.backend, "name", "")
                model = getattr(state.backend, "model", "")
                record = state.project.create_session(
                    backend_type=backend_type,
                    model=model,
                    session_role=session_role,
                )
                # Flag it as a mission BEFORE the first message so the
                # spec-extraction gate trips correctly on the upcoming
                # text.done events.
                record.start_mission(feature)
                # v0.5.0a7 — opt-in to the rigorous-grill flow when the
                # user toggled "∞ Run autonomously" in the composer. The
                # flag is captured in mission_state so the spec card
                # later knows whether to render "Build this roadmap" or
                # "∞ Build autonomously".
                autonomous_flag = bool(msg.get("autonomous"))
                if autonomous_flag and record.mission_state is not None:
                    record.mission_state["autonomous"] = True
                # Title from the feature, not the long grill prompt.
                title = feature if len(feature) <= 60 else feature[:57] + "..."
                record.title = title
                record.save()

                state.session = state.build_session(
                    backend=state.backend,
                    backend_spec=state.backend_spec,
                    project_path=state.project.project_path,
                    session_mode="code",
                    session_role=session_role,
                )
                state._first_message_sent = True  # title is already set
                state.costs.reset_session()

                # Tell the frontend to switch to the new session before
                # streaming starts so the chat panel renders into it.
                # v0.3.4 — include cwd so the chat-header path display
                # updates immediately when a mission picks a different
                # project (Bug A from v0.3.3 E2E testing). Without this,
                # apply_project_context above silently changed the
                # backend's project_path but the frontend's currentCwd
                # stayed stale until the next init.
                await ws.send_json({
                    "event": "session_cleared",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id,
                    "session_mode": "code",
                    "session_role": session_role,
                    "mission_started": True,
                    "cwd": state.project.project_path,
                })

                from ..orchestration.grill_me import format_grill_first_message
                fed_text = format_grill_first_message(
                    feature,
                    project_path=state.project.project_path,
                    autonomous=autonomous_flag,
                    # v0.5.0a7 — pessimistic default. We don't probe
                    # Ollama for the configured vision model here (the
                    # /api/tags hit would slow down mission start);
                    # leaves the rigorous-grill prompt to assume vision
                    # IS available, which is the optimistic case. If
                    # the user's vision model isn't pulled, REFLECT's
                    # graceful-degradation handles it (errored
                    # CheckResult per [vision] criterion). Future work:
                    # cache backend probe results for fast lookup.
                    vision_available=True,
                )
                display_text = feature  # what shows in chat as the user msg

                state.cancel_requested.clear()
                state.session.reset_cancel()
                display_events = await _run_session_streaming(
                    ws,
                    state.session,
                    fed_text,
                    display_user_msg=display_text,
                    session_mode="code",
                    session_role=session_role,
                )
                state.project.save_current_session(state.session, display_events=display_events)
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "mission_resume":
                # B4 — Resume an exited / completed mission. Flips the
                # phase back to whatever phase it was in before exit; if
                # it was already past completion, drop back to drafting
                # so the user can keep iterating. Also switches to that
                # session if it's not already current.
                target_id = (msg.get("session_id") or "").strip()
                if not target_id:
                    await ws.send_json({"event": "error",
                                        "message": "session_id required"})
                    continue
                # Switch to that session first if needed.
                if (not state.project.current_session) or (state.project.current_session.id != target_id):
                    state.project.load_session(target_id)
                    if state.project.current_session and state.backend:
                        state.session = state.build_session(
                            backend=state.backend,
                            backend_spec=state.backend_spec,
                            project_path=state.project.project_path,
                            session_mode="code",
                            session_role=state.project.current_session.session_role,
                        )
                cur = state.project.current_session
                if not cur or not cur.is_mission:
                    await ws.send_json({"event": "error",
                                        "message": "Not a mission session"})
                    continue
                # If we have a captured spec already, return to planning.
                # Otherwise drop back to drafting so the user can keep
                # grilling.
                ms = cur.mission_state or {}
                if ms.get("intent_id"):
                    cur.advance_mission_phase("planning_dispatched")
                else:
                    cur.advance_mission_phase("drafting")
                if "exited_at" in cur.mission_state:
                    cur.mission_state.pop("exited_at", None)
                cur.save()
                await ws.send_json({
                    "event": "mission_phase_changed",
                    "session_id": cur.id,
                    "phase": cur.mission_state["phase"],
                })
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": cur.id,
                })

            elif command == "mission_exit":
                # User hit "Exit Mission" on the header badge. Cancels any
                # in-flight turn, marks the mission as exited, leaves the
                # session selectable in the sidebar (under Missions /
                # exited) for review.
                if state.active_thread and state.active_thread.is_alive():
                    state.cancel_requested.set()
                    if state.session:
                        state.session.cancel()
                if state.project.current_session:
                    state.project.current_session.exit_mission()
                    state.project.current_session.save()
                await ws.send_json({
                    "event": "mission_exited",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "mission_dispatch_roadmap":
                # User clicked "Build this roadmap" on the spec card. We
                # advance the mission phase, dispatch the FULL spec to
                # intent_service (not just the refined-intent paragraph —
                # that was a Tier-1 bug from the first iteration), and
                # let the existing intent flow take over.
                if not state.project.current_session:
                    await ws.send_json({"event": "error",
                                        "message": "No active mission to dispatch"})
                    continue
                ms = state.project.current_session.mission_state or {}
                if ms.get("phase") != "drafting":
                    await ws.send_json({"event": "error",
                                        "message": f"Mission phase is {ms.get('phase','?')}, expected drafting"})
                    continue

                spec_md = (msg.get("spec_markdown") or "").strip()
                refined = (msg.get("refined_intent") or "").strip()
                if not spec_md and not refined:
                    await ws.send_json({"event": "error",
                                        "message": "No spec to dispatch"})
                    continue

                # Tier-1 fix #1: pass the full spec block as the intent
                # text so the planner sees assumptions / scope / acceptance
                # criteria, not just one paragraph. Refined intent stays
                # in mission_state for display.
                intent_text = spec_md or refined

                def _emit_intent(payload: dict, _ws=ws, _loop=asyncio.get_running_loop()):
                    try:
                        asyncio.run_coroutine_threadsafe(_ws.send_json(payload), _loop)
                    except Exception:
                        logger.debug("intent emit raised", exc_info=True)

                intent_service = state.get_intent_service(on_event=_emit_intent)
                try:
                    intent_id = intent_service.start_intent(intent_text)
                except Exception as exc:
                    logger.exception("mission_dispatch_roadmap failed")
                    await ws.send_json({"event": "error",
                                        "message": f"Roadmap dispatch failed: {exc}"})
                    continue

                state.project.current_session.advance_mission_phase(
                    "planning_dispatched",
                    spec_markdown=spec_md or "",
                    refined_intent=refined or "",
                    intent_id=intent_id,
                )
                state.project.current_session.save()
                await ws.send_json({
                    "event": "mission_phase_changed",
                    "session_id": state.project.current_session.id,
                    "phase": "planning_dispatched",
                    "intent_id": intent_id,
                })
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id,
                })

            elif command == "mission_dispatch_autonomous":
                # v0.5.0a6 — User clicked "∞ Build autonomously" on the
                # spec card. We expect a rigorous-grill spec (typed
                # acceptance criteria + time budget) — the helper
                # validates and raises ValueError on misconfiguration.
                #
                # Flow:
                #   1. Build roadmap from spec, persist to <project>/.resonant/
                #   2. Spawn AutonomousMissionDaemon with production hooks
                #   3. Advance mission phase to "autonomous_running"
                #   4. Daemon emits autonomous_* events asynchronously;
                #      we forward them to the WS here.
                #
                # See docs/long-running-agents-phase-2-implementation.md
                # for the full architecture.
                if not state.project.current_session:
                    await ws.send_json({"event": "error",
                                        "message": "No active mission to dispatch"})
                    continue
                ms = state.project.current_session.mission_state or {}
                if ms.get("phase") != "drafting":
                    await ws.send_json({"event": "error",
                                        "message": f"Mission phase is {ms.get('phase','?')}, expected drafting"})
                    continue
                spec_md = (msg.get("spec_markdown") or "").strip()
                if not spec_md:
                    await ws.send_json({"event": "error",
                                        "message": "spec_markdown required for autonomous dispatch"})
                    continue
                feature = (
                    state.project.current_session.title
                    or ms.get("seed_feature", "")
                    or "autonomous mission"
                )

                # Drop any finished daemons from prior missions so the
                # registry doesn't grow unbounded over a long session.
                _cleanup_autonomous_daemons(state)

                # Forward daemon events into the WS chat stream so the
                # frontend can render iteration cards / reflection
                # summaries / mission complete banners.
                # v0.5.6a3 — also intercept terminal events to update
                # session.mission_state.phase atomically with the badge
                # transition (linux-bridge field-observation #6: GUI /
                # session / roadmap diverged when daemon hit `stuck`).
                _emit_autonomous = _make_autonomous_event_forwarder(
                    state, ws, asyncio.get_running_loop(),
                )

                # The intent_id for the umbrella autonomous mission —
                # NOT the per-iteration sub-intent ids. We use a fresh
                # uuid; the rigorous-grill spec didn't provide one.
                autonomous_intent_id = str(uuid.uuid4())
                started_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
                )

                try:
                    daemon = _start_autonomous_mission(
                        state=state,
                        intent_id=autonomous_intent_id,
                        feature=feature,
                        spec_markdown=spec_md,
                        on_event=_emit_autonomous,
                        started_iso=started_iso,
                    )
                except ValueError as exc:
                    # Misconfigured spec (no typed criteria / no Final
                    # spec block). Surface to the user.
                    await ws.send_json({"event": "error",
                                        "message": f"Autonomous dispatch failed: {exc}"})
                    continue
                except Exception as exc:
                    logger.exception("mission_dispatch_autonomous failed")
                    await ws.send_json({"event": "error",
                                        "message": f"Autonomous dispatch failed: {exc}"})
                    continue

                state.project.current_session.advance_mission_phase(
                    "autonomous_running",
                    spec_markdown=spec_md,
                    intent_id=autonomous_intent_id,
                    autonomous_started_at=time.time(),
                )
                state.project.current_session.save()
                await ws.send_json({
                    "event": "mission_phase_changed",
                    "session_id": state.project.current_session.id,
                    "phase": "autonomous_running",
                    "intent_id": autonomous_intent_id,
                })
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id,
                })

            elif command == "autonomous_mission_stop":
                # v0.5.0a6 — User clicked Stop in the chat-header
                # autonomous badge. Find the daemon by intent_id and
                # signal it; the daemon emits autonomous_mission_paused
                # asynchronously as it winds down. The mission_state
                # phase transition to autonomous_paused happens when
                # we receive that event (kept in one place to avoid
                # races).
                target_intent = (msg.get("intent_id") or "").strip()
                if not target_intent and state.project.current_session:
                    ms = state.project.current_session.mission_state or {}
                    target_intent = ms.get("intent_id", "")
                if not target_intent:
                    await ws.send_json({"event": "error",
                                        "message": "intent_id required (or active mission)"})
                    continue

                stopped = _stop_autonomous_mission(
                    state, target_intent,
                    reason="user_stop",
                    message="user clicked Stop",
                )
                if not stopped:
                    await ws.send_json({"event": "error",
                                        "message": f"No active autonomous daemon for intent {target_intent}"})
                    continue
                # Daemon will emit `autonomous_mission_paused` itself;
                # nothing else to do here.

            elif command == "autonomous_mission_pause":
                # v0.5.9a4 — pause-after-current-iter. Distinct from
                # autonomous_mission_stop which cancels in-flight.
                # Daemon completes the current iter + reflection,
                # then exits with stop_reason="user_pause". UX: lets
                # the user "stop after this completes" without losing
                # the iter's work.
                target_intent = (msg.get("intent_id") or "").strip()
                if not target_intent and state.project.current_session:
                    ms = state.project.current_session.mission_state or {}
                    target_intent = ms.get("intent_id", "")
                if not target_intent:
                    await ws.send_json({
                        "event": "error",
                        "message": "intent_id required (or active mission)",
                    })
                    continue
                daemon = _get_autonomous_daemon(state, target_intent)
                if daemon is None:
                    await ws.send_json({
                        "event": "error",
                        "message": (
                            f"No active autonomous daemon for intent "
                            f"{target_intent}"
                        ),
                    })
                    continue
                try:
                    daemon.pause_after_iter("user clicked Pause")
                except Exception as exc:
                    logger.exception("pause_after_iter raised")
                    await ws.send_json({
                        "event": "error",
                        "message": f"Failed to schedule pause: {exc}",
                    })
                    continue
                # Acknowledge so the GUI can flip the badge state to
                # "pausing — finishing current iter…". Daemon emits
                # autonomous_mission_paused once the current iter
                # completes.
                await ws.send_json({
                    "event": "autonomous_pause_scheduled",
                    "intent_id": target_intent,
                })

            elif command == "autonomous_mission_decision":
                # v0.5.8a2 — User picked an option on the
                # human-decision-required card. Look up the daemon by
                # intent_id and call provide_decision() to unblock the
                # parked REFLECT pass. The daemon will retry REFLECT
                # with the user's choice folded into the prompt.
                target_intent = (msg.get("intent_id") or "").strip()
                option_id = (msg.get("option_id") or "").strip()
                response_text = (msg.get("response_text") or "").strip()
                if not target_intent and state.project.current_session:
                    ms = state.project.current_session.mission_state or {}
                    target_intent = ms.get("intent_id", "")
                if not target_intent:
                    await ws.send_json({
                        "event": "error",
                        "message": "intent_id required (or active mission)",
                    })
                    continue
                if not option_id:
                    await ws.send_json({
                        "event": "error",
                        "message": "option_id is required",
                    })
                    continue
                daemon = _get_autonomous_daemon(state, target_intent)
                if daemon is None:
                    await ws.send_json({
                        "event": "error",
                        "message": (
                            f"No active autonomous daemon for intent "
                            f"{target_intent}"
                        ),
                    })
                    continue
                try:
                    accepted = daemon.provide_decision(
                        option_id, response_text,
                    )
                except Exception as exc:
                    logger.exception("provide_decision raised")
                    await ws.send_json({
                        "event": "error",
                        "message": f"Failed to deliver decision: {exc}",
                    })
                    continue
                # Daemon will emit `autonomous_human_decision_received`
                # asynchronously when it picks up the choice. The
                # `accepted` boolean tells us whether the daemon was
                # actually parked (race-window guard); if False, the
                # daemon may have already unparked itself or been
                # stopped, but the response is still recorded for the
                # NEXT park if one happens. Echo the routing decision
                # back so the GUI can clear the card promptly.
                await ws.send_json({
                    "event": "autonomous_decision_dispatched",
                    "intent_id": target_intent,
                    "option_id": option_id,
                    "was_parked": accepted,
                })

            elif command == "autonomous_mission_roadmap":
                # v0.5.3a3 — Sidebar roadmap inspector. Frontend asks
                # for the parsed roadmap of a specific mission so it
                # can render acceptance-criteria progress, the next
                # unchecked item, and the latest reflection summary at
                # a glance — without having to open the file directly.
                #
                # We re-parse the on-disk roadmap on every request
                # rather than caching: REFLECT mutates the file
                # asynchronously (advisory file lock around its
                # writes), so a stale in-memory copy would lie. The
                # parser is fast enough that this is a non-issue.
                target_intent = (msg.get("intent_id") or "").strip()
                if not target_intent and state.project.current_session:
                    ms = state.project.current_session.mission_state or {}
                    target_intent = ms.get("intent_id", "")
                if not target_intent:
                    await ws.send_json({"event": "error",
                                        "message": "intent_id required (or active mission)"})
                    continue

                from .roadmap import default_path as _rm_default_path, load as _rm_load
                roadmap_path = _rm_default_path(
                    state.project.project_path, target_intent,
                )
                if not roadmap_path.exists():
                    # Not an error — early in a mission the daemon
                    # may not have persisted the roadmap yet, or this
                    # could be a stale request from a closed mission.
                    # Send an empty payload so the frontend can clear
                    # its inspector cleanly.
                    await ws.send_json({
                        "event": "autonomous_mission_roadmap",
                        "intent_id": target_intent,
                        "roadmap_exists": False,
                        "roadmap_path": str(roadmap_path),
                    })
                    continue

                try:
                    rm = _rm_load(roadmap_path)
                except Exception as exc:
                    logger.exception("autonomous_mission_roadmap parse failed")
                    await ws.send_json({"event": "error",
                                        "message": f"Could not parse roadmap: {exc}"})
                    continue

                payload = _build_roadmap_inspector_payload(
                    intent_id=target_intent,
                    roadmap=rm,
                    roadmap_path=roadmap_path,
                )
                payload["event"] = "autonomous_mission_roadmap"
                await ws.send_json(payload)

            elif command == "autonomous_orphans_list":
                # v0.5.3a2 — Frontend asks for a fresh orphan list
                # (e.g. after dismissing one or after a long idle).
                # `init` already includes the same field on connect /
                # session-switch refresh; this command exists so the
                # frontend doesn't have to round-trip the full init
                # payload to refresh the banner.
                await ws.send_json({
                    "event": "autonomous_orphans",
                    "orphans": _find_orphaned_autonomous_missions(state),
                })

            elif command == "autonomous_missions_list":
                # v0.5.5a2 — Frontend refreshes the sidebar mission
                # browser. `init` includes the same field on connect;
                # this command lets the frontend pull a fresh snapshot
                # without a full init round-trip.
                await ws.send_json({
                    "event": "autonomous_missions",
                    "missions": _list_autonomous_missions(state),
                })

            elif command == "autonomous_mission_resume":
                # v0.5.3a2 — User clicked "Resume" on an orphaned
                # autonomous mission. The daemon for this intent_id
                # was interrupted (server restart / crash / sleep);
                # the roadmap is still on disk. We:
                #   1. Switch to the orphan's session if a session_id
                #      is provided (so the chat header / sidebar /
                #      composer all key to the right session).
                #   2. Call `_resume_autonomous_mission` which loads
                #      the persisted roadmap and spawns a fresh daemon
                #      preserving any progress already made.
                #   3. Re-emit `mission_phase_changed` to confirm the
                #      session is now back in `autonomous_running`.
                #   4. Refresh sessions + orphans so the banner clears.
                target_intent = (msg.get("intent_id") or "").strip()
                if not target_intent:
                    await ws.send_json({"event": "error",
                                        "message": "intent_id required for resume"})
                    continue

                # Optional: switch to the originating session first so
                # the daemon's events land in the right chat. The
                # frontend SHOULD pass session_id but we tolerate its
                # absence (resume by intent_id alone — events will go
                # to the current session, which is wrong-but-recoverable).
                session_id = (msg.get("session_id") or "").strip()
                if session_id and (
                    not state.project.current_session
                    or state.project.current_session.id != session_id
                ):
                    record = state.project.load_session(session_id)
                    if record is None:
                        await ws.send_json({"event": "error",
                                            "message": f"Session {session_id} not found for resume"})
                        continue
                    # Recreate the backend so this session has a model
                    # to dispatch to. Mirrors the switch_session flow
                    # above (kept narrow — no display-event replay
                    # since the chat will be re-emitting live events).
                    try:
                        backend_type = record.backend_type
                        model = record.model
                        if backend_type:
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: state.create_backend(
                                    backend_type,
                                    model or None,
                                    session_mode="code",
                                    session_role=record.session_role or "generator",
                                ),
                            )
                        if state.session and record.conversation_history:
                            state.session.conversation_history = record.conversation_history
                        state._first_message_sent = record.message_count > 0
                    except Exception as exc:
                        logger.exception("autonomous_mission_resume backend recreate failed")
                        await ws.send_json({"event": "error",
                                            "message": f"Resume failed (backend recreate): {exc}"})
                        continue

                # Drop any finished daemons so the registry doesn't
                # stall the resume on a dead-but-registered entry.
                _cleanup_autonomous_daemons(state)

                # Wire the daemon's events into THIS websocket — same
                # forwarder pattern as mission_dispatch_autonomous.
                # v0.5.6a3 — resume uses the same terminal-event
                # interception as the fresh-dispatch path so the
                # session-phase update happens regardless of how the
                # daemon was launched.
                _emit_resume = _make_autonomous_event_forwarder(
                    state, ws, asyncio.get_running_loop(),
                )

                try:
                    _resume_autonomous_mission(
                        state=state,
                        intent_id=target_intent,
                        on_event=_emit_resume,
                    )
                except ValueError as exc:
                    # No roadmap on disk OR roadmap has no criteria.
                    # User-facing: explain so they know whether to
                    # re-dispatch or accept the loss.
                    await ws.send_json({"event": "error",
                                        "message": f"Resume failed: {exc}"})
                    continue
                except RuntimeError as exc:
                    # Already running — defensively, this means the
                    # frontend's orphan list was stale. Refresh it.
                    await ws.send_json({"event": "error",
                                        "message": f"Resume failed: {exc}"})
                    await ws.send_json({
                        "event": "autonomous_orphans",
                        "orphans": _find_orphaned_autonomous_missions(state),
                    })
                    continue
                except Exception as exc:
                    logger.exception("autonomous_mission_resume failed")
                    await ws.send_json({"event": "error",
                                        "message": f"Resume failed: {exc}"})
                    continue

                # Phase is already `autonomous_running` on the session
                # (that's why it appeared as an orphan). Re-emit so the
                # frontend's mission badge wakes up and the orphan
                # banner clears for this entry.
                if state.project.current_session:
                    await ws.send_json({
                        "event": "mission_phase_changed",
                        "session_id": state.project.current_session.id,
                        "phase": "autonomous_running",
                        "intent_id": target_intent,
                        "resumed": True,
                    })
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })
                await ws.send_json({
                    "event": "autonomous_orphans",
                    "orphans": _find_orphaned_autonomous_missions(state),
                })

            elif command == "list_project_files":
                # Pi-style `@file` autocomplete: front-end caches a file list
                # for the current project and filters it client-side. We walk
                # the tree once on demand, skipping the usual bloat dirs and
                # capping at a sane upper bound so giant monorepos don't
                # ship multi-megabyte JSON over the websocket.
                request_id = msg.get("request_id", "")
                project_path_str = (
                    state.project.project_path
                    if state.project and state.project.project_path
                    else os.getcwd()
                )
                _SKIP_DIRS = {
                    ".git", "node_modules", "__pycache__", ".pytest_cache",
                    "dist", "build", ".venv", "venv", ".tox", ".idea",
                    ".vscode", ".cache", ".next", ".turbo", "target",
                    "out", "coverage", ".mypy_cache", ".ruff_cache",
                    ".terraform", ".gradle",
                }
                _MAX_FILES = 5000
                _files: list[str] = []
                _root = Path(project_path_str)
                try:
                    for dirpath, dirnames, filenames in os.walk(project_path_str):
                        # Prune in-place so os.walk doesn't descend into them.
                        dirnames[:] = [
                            d for d in dirnames
                            if d not in _SKIP_DIRS and not d.startswith(".")
                        ]
                        for fname in filenames:
                            if fname.startswith("."):
                                continue
                            full = Path(dirpath) / fname
                            try:
                                rel = full.relative_to(_root)
                            except ValueError:
                                continue
                            _files.append(str(rel).replace("\\", "/"))
                            if len(_files) >= _MAX_FILES:
                                break
                        if len(_files) >= _MAX_FILES:
                            break
                except (OSError, PermissionError) as _e:
                    logger.debug("list_project_files walk failed: %s", _e)

                _files.sort()
                await ws.send_json({
                    "event": "project_files",
                    "request_id": request_id,
                    "files": _files,
                    "total": len(_files),
                    "truncated": len(_files) >= _MAX_FILES,
                    "project_path": project_path_str,
                })

            elif command == "shell_exec":
                # Pi-style `!cmd` / `!!cmd` shortcut from the input box. Runs a
                # subprocess in the project directory and either feeds the
                # output back to the model (`!cmd`, feed_to_llm=True) or
                # displays it inline without involving the model (`!!cmd`,
                # feed_to_llm=False — useful for "let me check git status real
                # quick" without burning a model turn).
                cmd_str = (msg.get("cmd") or "").strip()
                request_id = msg.get("request_id", "")
                feed_to_llm = bool(msg.get("feed_to_llm", False))

                if not cmd_str:
                    await ws.send_json({
                        "event": "shell_exec_result",
                        "request_id": request_id,
                        "ok": False,
                        "output": "(empty command)",
                        "exit_code": -1,
                        "elapsed": 0.0,
                        "feed_to_llm": False,
                        "command": "",
                    })
                    continue

                # Refuse `!cmd` (LLM-feeding) if a session is already streaming;
                # `!!cmd` is just a subprocess and is safe to run anytime.
                if feed_to_llm and state.active_thread and state.active_thread.is_alive():
                    await ws.send_json({
                        "event": "shell_exec_result",
                        "request_id": request_id,
                        "ok": False,
                        "output": "Cannot feed shell output to model while a session is running.",
                        "exit_code": -1,
                        "elapsed": 0.0,
                        "feed_to_llm": True,
                        "command": cmd_str,
                    })
                    continue

                project_path = (
                    state.project.project_path
                    if state.project and state.project.project_path
                    else os.getcwd()
                )

                # Reuse the bash tool's executor so timeout, cancellation, and
                # truncation are consistent with what the model's bash tool sees.
                from ..engine.tools import _exec_bash as _bash_executor
                import time as _time
                _start = _time.time()
                _result = _bash_executor(
                    {"command": cmd_str, "timeout": 30, "cwd": project_path},
                    _start,
                )
                _output = _result.output
                _exit = (_result.metadata or {}).get("exit_code", -1)

                await ws.send_json({
                    "event": "shell_exec_result",
                    "request_id": request_id,
                    "ok": not _result.is_error,
                    "output": _output,
                    "exit_code": _exit,
                    "elapsed": _result.elapsed,
                    "feed_to_llm": feed_to_llm,
                    "command": cmd_str,
                })

                if feed_to_llm and state.session:
                    # Synthesize a user message that gives the model the
                    # command + its output, while keeping the *displayed*
                    # user message short ("!git status") so chat history
                    # reads cleanly.
                    fed_text = f"Output of `{cmd_str}`:\n\n```\n{_output}\n```"
                    display_text = f"!{cmd_str}"

                    if not state._first_message_sent:
                        state.project.update_session_title(display_text)
                    state._first_message_sent = True

                    state.cancel_requested.clear()
                    state.session.reset_cancel()
                    _session_role = (
                        state.project.current_session.session_role
                        if state.project.current_session else "generator"
                    )
                    display_events = await _run_session_streaming(
                        ws,
                        state.session,
                        fed_text,
                        display_user_msg=display_text,
                        session_mode="code",
                        session_role=_session_role,
                    )
                    state.project.save_current_session(state.session, display_events=display_events)
                    await ws.send_json({
                        "event": "sessions_updated",
                        "sessions": state.project.list_sessions(),
                        "current_session_id": state.project.current_session.id if state.project.current_session else "",
                    })

            elif command == "clear":
                # Create a new session (don't destroy old one)
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                if state.backend:
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    state.project.create_session(
                        backend_type=backend_type,
                        model=model,
                        session_role=session_role,
                    )
                    state.session = state.build_session(
                        backend=state.backend,
                        backend_spec=state.backend_spec,
                        project_path=state.project.project_path,
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
                    "cwd": state.project.project_path,
                })

            elif command == "switch_model":
                model = msg.get("model", "")
                backend_type = msg.get("backend", "")
                if not backend_type and state.backend and hasattr(state.backend, "name"):
                    backend_type = getattr(state.backend, "name", "")
                if backend_type:
                    try:
                        session_role = (
                            state.project.current_session.session_role
                            if state.project.current_session else "generator"
                        )
                        # Bug #9+#10 fix: swap_backend (not create_backend)
                        # preserves the existing session's conversation_history.
                        # Previously we rebuilt the session from scratch, silently
                        # discarding all prior turns.
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: state.swap_backend(
                                backend_type,
                                model,
                                session_mode="code",
                                session_role=session_role,
                            ),
                        )
                        # Heads-up for CLI-wrapper backends: their underlying
                        # CLI sessions ignore our conversation_history list, so
                        # even though the session-level history survived the
                        # swap, the new backend can't see it. Emit a one-time
                        # warning so the user knows.
                        if backend_type in state.CLI_WRAPPED_BACKENDS:
                            await ws.send_json({
                                "event": "backend_swap_warning",
                                "backend": backend_type,
                                "message": (
                                    f"Switched to {backend_type}. This backend uses its own CLI "
                                    f"session and won't see prior conversation turns. "
                                    f"Switch back to your previous backend to resume the "
                                    f"original thread with full context."
                                ),
                            })
                        await ws.send_json(state.get_init_data())
                    except Exception as e:
                        await ws.send_json({"event": "error", "message": str(e)})

            elif command == "get_model_telemetry":
                # Best-effort runtime info about the loaded Ollama model
                # (context_length, memory, supports_thinking).
                if state.backend and getattr(state.backend, "name", "") == "ollama" \
                        and hasattr(state.backend, "get_runtime_telemetry"):
                    data = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.backend.get_runtime_telemetry(timeout=4.0),
                    )
                    await ws.send_json({"event": "model_telemetry", "data": data})
                else:
                    await ws.send_json({"event": "model_telemetry", "data": {"error": "no Ollama backend"}})

            elif command == "get_prompt_inspector":
                session = state.session
                model_name = (
                    getattr(state.backend, "model", "")
                    or getattr(state.backend_spec, "model", "")
                    or state.settings.get("general", "default_model", "")
                )
                data = inspect_system_instructions(
                    plan_mode=bool(getattr(session, "plan_mode", False)),
                    project_instructions=getattr(session, "project_instructions", None),
                    working_directory=state.project.project_path,
                    model_name=model_name,
                    prompt_role=getattr(session, "prompt_role", "primary"),
                    role_instructions=getattr(session, "role_instructions", None),
                )
                await ws.send_json({"event": "prompt_inspector", "data": data})

            elif command == "get_context_state":
                data = state.session.context_snapshot() if state.session else {
                    "model": "",
                    "context_window": 0,
                    "estimated_total_tokens": 0,
                    "utilization": 0,
                    "history": {"entries": 0, "estimated_tokens": 0},
                    "system_prompt": {"estimated_tokens": 0, "layers": []},
                    "sources": {},
                    "largest_tool_payloads": [],
                    "todos": [],
                    "compression_count": 0,
                }
                await ws.send_json({"event": "context.state", **data})

            elif command == "checkpoint_list":
                try:
                    from ..orchestration.checkpoints import IterationCheckpointStore
                    store = IterationCheckpointStore(state.project.project_path)
                    await ws.send_json({
                        "event": "checkpoint_list",
                        "checkpoints": store.list(),
                    })
                except Exception as exc:
                    await ws.send_json({
                        "event": "checkpoint_list",
                        "checkpoints": [],
                        "error": str(exc),
                    })

            elif command == "checkpoint_compare":
                try:
                    from ..orchestration.checkpoints import IterationCheckpointStore
                    store = IterationCheckpointStore(state.project.project_path)
                    data = await asyncio.get_event_loop().run_in_executor(
                        None, store.compare, str(msg.get("ref") or "")
                    )
                    await ws.send_json({"event": "checkpoint_comparison", "data": data})
                except Exception as exc:
                    await ws.send_json({"event": "error", "message": str(exc)})

            elif command == "checkpoint_restore":
                try:
                    if state.active_thread and state.active_thread.is_alive():
                        raise RuntimeError("Stop the active agent before restoring a checkpoint")
                    from ..orchestration.checkpoints import IterationCheckpointStore
                    store = IterationCheckpointStore(state.project.project_path)
                    data = await asyncio.get_event_loop().run_in_executor(
                        None, store.restore, str(msg.get("ref") or "")
                    )
                    await ws.send_json({"event": "checkpoint_restored", "data": data})
                except Exception as exc:
                    await ws.send_json({"event": "error", "message": str(exc)})

            elif command == "evaluation_list":
                await ws.send_json({
                    "event": "evaluation_dashboard",
                    "data": state.evaluations.snapshot(),
                })

            elif command == "evaluation_start":
                try:
                    record = state.evaluations.start(
                        model_label=str(msg.get("model") or "glm"),
                        spec_name=str(msg.get("spec") or "minimal"),
                        n=int(msg.get("n") or 1),
                        timeout_minutes=int(msg.get("timeout_minutes") or 25),
                        project_path=state.project.project_path,
                    )
                    await ws.send_json({
                        "event": "evaluation_started",
                        "record": record,
                    })
                except (TypeError, ValueError, RuntimeError) as exc:
                    await ws.send_json({"event": "error", "message": str(exc)})

            elif command == "set_thinking_mode":
                # Per-session thinking-mode toggle (deepseek-v* etc.).
                # Forces a backend rebuild because Ollama options must be stable
                # for the lifetime of an OllamaBackend instance.
                mode = (msg.get("mode") or "").strip().lower()
                if mode not in {"", "off", "low", "med", "medium", "high", "max"}:
                    await ws.send_json({"event": "error", "message": f"invalid thinking mode: {mode!r}"})
                else:
                    try:
                        # v0.6.5 — explicit "off" is stored as the truthy
                        # token "off" (not "") so it survives the
                        # thinking_mode preservation in create_backend /
                        # swap_backend; otherwise a falsy "" would let the
                        # per-model default (e.g. GLM→high) clobber the
                        # user's choice on the next backend rebuild.
                        normalized = "off" if mode in {"", "off"} else ("med" if mode == "medium" else mode)
                        if state.project.current_session:
                            state.project.current_session.thinking_mode = normalized
                            state.project.current_session.save()
                        # Rebuild the backend with the new thinking_mode in spec
                        if state.backend_spec:
                            state.backend_spec.thinking_mode = normalized
                            session_role = (
                                state.project.current_session.session_role
                                if state.project.current_session else "generator"
                            )
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: state.create_backend(
                                    state.backend_spec.backend_type,
                                    state.backend_spec.model,
                                    session_mode="code",
                                    session_role=session_role,
                                ),
                            )
                        await ws.send_json({
                            "event": "thinking_mode_set",
                            "mode": normalized,
                        })
                        await ws.send_json(state.get_init_data())
                    except Exception as e:
                        await ws.send_json({"event": "error", "message": str(e)})

            elif command == "set_permission_mode":
                mode = msg.get("mode", "bypass")
                state.apply_permission_mode(mode)

            elif command == "connect_browser":
                # v0.6.5 — one-click "open Chrome in debug mode for the
                # agent". Attaches to an already-debug Chrome, else launches
                # the real Chrome with the debug port. `force_relaunch` closes
                # the user's running Chrome and reopens it in debug mode (the
                # single-instance lock case). Runs in a thread so the
                # launch/close can't block the event loop; result flows back
                # as a browser_status event.
                force = bool(msg.get("force_relaunch"))
                profile = msg.get("profile") or None
                await ws.send_json({"event": "browser_status", "status": "connecting"})
                from ..engine.browser import get_browser
                mgr = get_browser()

                def _connect():
                    if force:
                        return mgr.relaunch_in_debug(profile=profile)
                    return mgr.connect_or_launch_chrome(profile=profile)

                try:
                    result = await asyncio.get_event_loop().run_in_executor(None, _connect)
                except Exception as e:
                    result = f"Error: {e}"
                if mgr.is_connected:
                    status = "connected"
                elif "never came up" in (result or ""):
                    status = "needs_relaunch"
                else:
                    status = "error"
                await ws.send_json({
                    "event": "browser_status",
                    "status": status,
                    "detail": (result or "")[:300],
                })

            elif command == "get_session_replay_events":
                # Fetch full display_events for any session without switching the active one.
                target_id = msg.get("session_id", "")
                project_path = msg.get("project_path") or state.project.project_path
                # Try the requested project path, then any recent project that has it
                from .sessions import _sessions_dir
                fp = _sessions_dir(project_path) / f"{target_id}.json"
                if not fp.exists():
                    for proj in state.project.get_recent_projects():
                        path = proj.get("path", "") if isinstance(proj, dict) else str(proj)
                        if not path:
                            continue
                        candidate = _sessions_dir(path) / f"{target_id}.json"
                        if candidate.exists():
                            fp = candidate
                            break
                if not fp.exists():
                    await ws.send_json({"event": "session_replay_events", "session_id": target_id, "error": "not found", "events": []})
                else:
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        events = data.get("display_events") or []
                        title = data.get("title") or ""
                        await ws.send_json({
                            "event": "session_replay_events",
                            "session_id": target_id,
                            "title": title,
                            "events": events,
                        })
                    except Exception as exc:
                        await ws.send_json({"event": "session_replay_events", "session_id": target_id, "error": str(exc), "events": []})

            elif command == "fork_session":
                source_id = msg.get("session_id", "")
                idx = int(msg.get("user_message_index", 0))
                forked = state.project.fork_session(source_id, idx)
                if forked is None:
                    await ws.send_json({"event": "error", "message": f"Cannot fork: session {source_id} not found"})
                else:
                    # Rebuild a session bound to the forked record so subsequent messages append correctly.
                    if state.backend_spec:
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: state.create_backend(
                                    state.backend_spec.backend_type,
                                    state.backend_spec.model,
                                    session_mode="code",
                                    session_role=forked.session_role,
                                ),
                            )
                            # Restore the forked conversation onto the new Session
                            state.session.conversation_history = list(forked.conversation_history)
                            state._first_message_sent = bool(forked.conversation_history)
                        except Exception as exc:
                            logger.warning("fork_session backend rebuild failed: %s", exc)

                    await ws.send_json({
                        "event": "session_forked",
                        "session_id": forked.id,
                        "title": forked.title,
                        "user_messages_kept": forked.message_count,
                    })
                    await ws.send_json({
                        "event": "session_loaded",
                        "current_session_id": forked.id,
                        "session_role": forked.session_role,
                        "display_events": forked.display_events,
                        "sessions": state.project.list_sessions(),
                    })

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
                                    session_mode="code",
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
                            "session_mode": "code",
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

            elif command == "pin_session":
                session_id = msg.get("session_id", "")
                # Use current session when no explicit ID supplied (/pin command)
                if session_id:
                    record = state.project.load_session(session_id)
                    # Restore current session pointer after the load
                    if record and state.project.current_session and state.project.current_session.id != session_id:
                        state.project.load_session(state.project.current_session.id)
                else:
                    record = state.project.current_session
                if record:
                    record.pinned = not record.pinned
                    record.save()
                    verb = "Pinned" if record.pinned else "Unpinned"
                    await ws.send_json({"event": "status_msg", "message": f"{verb} session."})
                await ws.send_json({
                    "event": "sessions_updated",
                    "sessions": state.project.list_sessions(),
                    "all_sessions": state.project.list_all_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            # ── Intent / organic-orchestration commands ─────────────
            elif command in ("intent_start", "intent_cancel", "intent_pause",
                             "intent_resume", "intent_list_snapshots",
                             "intent_restore_snapshot"):
                # Bridge the intent worker thread back to this WebSocket. The
                # service runs on a thread, so we hand it a thread-safe emitter
                # that schedules `ws.send_json` on the asyncio loop.
                loop = asyncio.get_running_loop()

                def _emit_intent(payload: dict, _ws=ws, _loop=loop):
                    try:
                        asyncio.run_coroutine_threadsafe(_ws.send_json(payload), _loop)
                    except Exception:
                        logger.debug("intent emit raised", exc_info=True)

                if state.backend is None:
                    await ws.send_json({"event": "error",
                                        "message": "Connect a backend before starting an intent."})
                else:
                    intent_service = state.get_intent_service(on_event=_emit_intent)

                    if command == "intent_start":
                        text = (msg.get("text") or "").strip()
                        if not text:
                            await ws.send_json({"event": "error",
                                                "message": "intent text is required"})
                        else:
                            try:
                                intent_id = intent_service.start_intent(text)
                                await ws.send_json({
                                    "event": "intent.accepted",
                                    "intent_id": intent_id,
                                    "text": text,
                                })
                            except Exception as exc:
                                logger.exception("intent_start failed")
                                await ws.send_json({"event": "error",
                                                    "message": f"intent_start failed: {exc}"})
                    elif command == "intent_cancel":
                        ok = intent_service.cancel(msg.get("intent_id", ""))
                        await ws.send_json({"event": "intent.cancel_ack",
                                            "intent_id": msg.get("intent_id", ""),
                                            "ok": ok})
                    elif command == "intent_pause":
                        ok = intent_service.pause(msg.get("intent_id", ""))
                        await ws.send_json({"event": "intent.pause_ack",
                                            "intent_id": msg.get("intent_id", ""),
                                            "ok": ok})
                    elif command == "intent_resume":
                        ok = intent_service.resume(msg.get("intent_id", ""))
                        await ws.send_json({"event": "intent.resume_ack",
                                            "intent_id": msg.get("intent_id", ""),
                                            "ok": ok})
                    elif command == "intent_list_snapshots":
                        snaps = intent_service.list_snapshots(msg.get("intent_id", ""))
                        await ws.send_json({"event": "plan.snapshot_list",
                                            "intent_id": msg.get("intent_id", ""),
                                            "snapshots": snaps})
                    elif command == "intent_restore_snapshot":
                        ok = intent_service.restore_snapshot(
                            msg.get("intent_id", ""),
                            int(msg.get("ts_ms") or 0),
                        )
                        await ws.send_json({"event": "intent.restore_ack",
                                            "intent_id": msg.get("intent_id", ""),
                                            "ok": ok})

            elif command == "redetect_backends":
                # v0.4.0 — fired by the welcome-screen Ollama wizard
                # after the user updates the URL. Re-probes Ollama and
                # ships a fresh init payload so the wizard either
                # shows the model picker (success) or stays put with
                # a fresh diagnostic (still unreachable).
                #
                # v0.4.3 (T1.3) — emit a structured `ollama_probe_result`
                # event BEFORE the init payload so the wizard can render
                # success/failure feedback without waiting for the full
                # init round-trip (which the wizard wouldn't see on
                # success since it gets re-rendered into the model
                # picker). The wizard listens for this event and
                # updates its hint area in real time.
                state.refresh_network_defaults()
                await asyncio.get_event_loop().run_in_executor(None, state.detect_backends)
                ollama_info = state.available_backends.get("ollama") or {}
                if ollama_info and not state.backend:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: state.ensure_default_runtime_session(),
                        )
                    except Exception:
                        logger.exception("default runtime session after backend redetect failed")
                await ws.send_json({
                    "event": "ollama_probe_result",
                    "ok": bool(ollama_info),
                    "url": state.ollama_url,
                    "models_count": len(ollama_info.get("models") or []),
                })
                await ws.send_json(state.get_init_data())

            elif command == "register_project":
                project_path = msg.get("path", "").strip()
                if not project_path:
                    await ws.send_json({"event": "error", "message": "Project path is required."})
                    continue
                try:
                    resolved_project_path = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.ensure_project_path(project_path),
                    )
                    registered_project_path = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.project.register_project(resolved_project_path),
                    )
                    await ws.send_json({
                        "event": "project_registered",
                        "path": registered_project_path,
                        "recent_projects": state.project.get_recent_projects(),
                        "playground_project": state.project.get_playground_project(),
                        "all_sessions": state.project.list_all_sessions(),
                    })
                    await ws.send_json({
                        "event": "ui_notice",
                        "message": f"Project added: {registered_project_path}",
                    })
                except Exception as exc:
                    logger.warning("register_project failed for %r: %s", project_path, exc)
                    await ws.send_json({
                        "event": "error",
                        "message": f"Couldn't add project folder: {exc}",
                    })

            elif command == "set_project":
                project_path = msg.get("path", "").strip()
                if not project_path:
                    await ws.send_json({"event": "error", "message": "Project path is required."})
                    continue
                try:
                    resolved_project_path = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.ensure_project_path(project_path),
                    )
                    state.apply_project_context(resolved_project_path, refresh_index=True)
                    # Reset backend + session
                    state.backend = None
                    state.backend_spec = None
                    state.session = None
                    state._first_message_sent = False
                    state.costs.reset_session()
                    # Re-detect backends
                    await asyncio.get_event_loop().run_in_executor(None, state.detect_backends)
                    if state.available_backends:
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: state.ensure_default_runtime_session(),
                            )
                        except Exception:
                            logger.exception("default runtime session after project switch failed")
                    await ws.send_json(state.get_init_data())
                    await ws.send_json({
                        "event": "ui_notice",
                        "message": f"Project ready: {resolved_project_path}",
                    })
                except Exception as exc:
                    logger.warning("set_project failed for %r: %s", project_path, exc)
                    await ws.send_json({
                        "event": "error",
                        "message": f"Couldn't open project folder: {exc}",
                    })

            elif command == "check_updates":
                try:
                    from resonant_client.updater import check_for_updates_now

                    started = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: check_for_updates_now(silent=False),
                    )
                    await ws.send_json({
                        "event": "status_msg",
                        "message": (
                            "Update checker opened."
                            if started
                            else "Update checker is unavailable in this build."
                        ),
                    })
                except Exception as exc:
                    logger.exception("check_updates failed")
                    await ws.send_json({
                        "event": "error",
                        "message": f"Failed to check for updates: {exc}",
                    })

            elif command == "save_diagnostics":
                # v0.3.4 — Help → Save diagnostics. Bundles redacted logs
                # + intent audits + settings into a ZIP under ~/Downloads
                # so the user can attach to a GitHub issue. No data ever
                # leaves the machine without an explicit user action.
                try:
                    from . import diagnostics
                    from pathlib import Path as _P
                    from .. import __version__ as _ver
                    resonant_dir = _P.home() / ".resonant"
                    output_dir = diagnostics.default_output_dir()
                    zip_path = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: diagnostics.build_diagnostics_zip(
                            resonant_dir, output_dir, version=_ver
                        ),
                    )
                    size_bytes = zip_path.stat().st_size if zip_path.exists() else 0
                    await ws.send_json({
                        "event": "diagnostics_saved",
                        "path": str(zip_path),
                        "size_bytes": size_bytes,
                    })
                except Exception as exc:
                    logger.exception("save_diagnostics failed")
                    await ws.send_json({
                        "event": "error",
                        "message": f"Failed to save diagnostics: {exc}",
                    })

            elif command == "folder_dialog":
                # Open native folder picker via pywebview (or tkinter fallback). Always
                # acknowledge the click — silent failure was a real UX issue surfaced
                # in the dogfood test (the user clicks "Open another project..." and
                # has no idea whether it worked).
                start_dir = (msg.get("directory") or "").strip()
                if not start_dir or not os.path.isdir(start_dir):
                    start_dir = state.project.project_path if state.project else ""

                # v0.5.6a4 fast-path, restored (regressed in v0.6.8): in
                # browser mode there is no pywebview window, and a tkinter
                # dialog opens on the SERVER's display — invisible to a
                # remote user — while the awaited executor call blocks this
                # socket's message loop until someone dismisses it on the
                # host. Route browser users to the in-page path modal.
                if _webview_window is None:
                    await ws.send_json({
                        "event": "folder_picker_unavailable",
                        "message": (
                            "Native folder picker isn't available in "
                            "browser mode — type the project path "
                            "directly."
                        ),
                    })
                    continue

                await ws.send_json({"event": "ui_notice", "message": "Opening project picker..."})

                def _pick_folder():
                    global _webview_window
                    if _webview_window:
                        try:
                            import webview
                            result = _webview_window.create_file_dialog(
                                webview.FOLDER_DIALOG,
                                directory=start_dir,
                            )
                            if result and len(result) > 0:
                                return {"path": result[0], "opened": True}
                            return {"path": "", "opened": True}
                        except Exception as e:
                            logger.warning(f"pywebview folder dialog failed: {e}")

                    root = None
                    try:
                        import tkinter as tk
                        from tkinter import filedialog
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes('-topmost', True)
                        try:
                            root.lift()
                            root.focus_force()
                            root.update()
                        except Exception:
                            pass
                        folder = filedialog.askdirectory(
                            parent=root,
                            title="Open project",
                            initialdir=start_dir or None,
                        )
                        try:
                            root.destroy()
                        except Exception:
                            pass
                        return {"path": folder or "", "opened": True}
                    except Exception as e:
                        logger.warning(f"tkinter folder dialog failed: {e}")
                        try:
                            if root is not None:
                                root.destroy()
                        except Exception:
                            pass
                        return {"path": "", "opened": False}

                picked = await asyncio.get_event_loop().run_in_executor(None, _pick_folder)
                picked_path = picked.get("path", "") if isinstance(picked, dict) else ""
                picker_opened = bool(picked.get("opened")) if isinstance(picked, dict) else False
                if picked_path:
                    await ws.send_json({"event": "folder_picked", "path": picked_path})
                elif picker_opened:
                    await ws.send_json({"event": "ui_notice", "message": "Project picker closed."})
                else:
                    # No pick — tell the user what to do next so the click isn't a dead end.
                    await ws.send_json({
                        "event": "folder_picker_unavailable",
                        "message": (
                            "Couldn't open the native folder picker. "
                            "Type the project path in the workspace folder field instead, "
                            "or pick from the Recent list."
                        ),
                    })

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

            elif command == "user_input":
                # v0.3.5 — reply path for the await_user tool. The agent
                # is blocked inside `on_user_input` waiting for this
                # event. The empty-string sentinel is "user closed the
                # modal without answering" — agent receives empty and
                # decides what to do with it.
                state.user_input_result[0] = msg.get("response", "")
                state.user_input_response.set()

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

            elif command == "lsp_list":
                await ws.send_json(_lsp_list_payload(
                    project_path=state.project.project_path,
                    settings=state.settings,
                ))

            elif command == "plugin_list":
                await ws.send_json(_plugin_list_payload(settings=state.settings))

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

            # v0.6.2a3 — Skills GUI surface. Thin wrappers around the
            # existing skills.py + skill_curator.py public API; same
            # commands the resonant-skill CLI uses, just over the WS.
            elif command == "skill_list":
                await ws.send_json(_skill_list_payload(
                    project_path=state.project.project_path if state.project else "",
                    include_deprecated=bool(msg.get("include_deprecated", False)),
                ))

            elif command == "skill_view":
                skill_id = (msg.get("skill_id") or "").strip()
                project_path = state.project.project_path if state.project else ""
                await ws.send_json(_skill_view_payload(skill_id, project_path=project_path))

            elif command == "skill_pin_toggle":
                from ..orchestration.skills import load_skill, set_pinned
                skill_id = (msg.get("skill_id") or "").strip()
                project_path = state.project.project_path if state.project else ""
                try:
                    s = load_skill(skill_id, project_path=project_path)
                    if s is None:
                        await ws.send_json({"event": "skill_error", "message": f"skill {skill_id!r} not found"})
                    else:
                        new_pinned = not bool(s.pinned)
                        set_pinned(skill_id, new_pinned, project_path=project_path)
                        await ws.send_json({"event": "skill_pin_changed", "skill_id": skill_id, "pinned": new_pinned})
                        await ws.send_json(_skill_list_payload(project_path=project_path))
                except Exception as exc:
                    await ws.send_json({"event": "skill_error", "message": f"pin toggle failed: {exc}"})

            elif command == "skill_archive":
                from ..orchestration.skills import archive_skill, load_skill
                skill_id = (msg.get("skill_id") or "").strip()
                reason = (msg.get("reason") or "manual archive via GUI").strip()
                project_path = state.project.project_path if state.project else ""
                try:
                    s = load_skill(skill_id, project_path=project_path)
                    if s is None:
                        await ws.send_json({"event": "skill_error", "message": f"skill {skill_id!r} not found"})
                    elif s.created_by == "bundled":
                        await ws.send_json({"event": "skill_error", "message": "Refused: bundled skills cannot be archived"})
                    elif s.created_by == "user":
                        await ws.send_json({"event": "skill_error", "message": "Refused: user-provenance skills cannot be archived (unpin first)"})
                    elif s.pinned:
                        await ws.send_json({"event": "skill_error", "message": "Refused: pinned skills cannot be archived (unpin first)"})
                    else:
                        scope_kw = project_path if s.scope == "project" else None
                        archive_skill(s, project_path=scope_kw, reason=reason)
                        await ws.send_json({"event": "skill_archived", "skill_id": skill_id})
                        await ws.send_json(_skill_list_payload(project_path=project_path))
                except Exception as exc:
                    await ws.send_json({"event": "skill_error", "message": f"archive failed: {exc}"})

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

    def on_user_input(question, options):
        """v0.3.5 — push await_user request, block until reply.
        Mirrors on_permission's polling loop so cancellation still
        bumps us out (otherwise the agent would hang the UI when the
        user closes a mission with an open prompt).
        """
        # Reset before signalling so a stale value from a prior
        # await_user can't leak into this one.
        state.user_input_response.clear()
        state.user_input_result[0] = ""
        event_queue.put({
            "event": "await_user",
            "question": question or "",
            "options": list(options or []),
        })
        while True:
            if session.cancel_requested or state.cancel_requested.is_set():
                return ""
            if state.user_input_response.wait(timeout=0.1):
                state.user_input_response.clear()
                return state.user_input_result[0]

    def _engine_thread():
        try:
            for event in session.run(
                user_msg,
                on_permission=on_permission if not session.auto_approve else None,
                on_choice=on_choice,
                on_user_input=on_user_input,
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

                # Mission spec detection — only fires for sessions that are
                # in Mission mode AND in the drafting phase, so a regular
                # chat where someone happens to use the `## Final spec`
                # heading (e.g. reviewing a doc) doesn't accidentally
                # surface a Build Roadmap button. Tier-1 fix #2.
                try:
                    cur_session = state.project.current_session if state.project else None
                    in_drafting = bool(
                        cur_session
                        and cur_session.is_mission
                        and cur_session.mission_phase == "drafting"
                    )
                    if in_drafting:
                        from ..orchestration.grill_me import extract_spec as _extract_spec
                        _spec = _extract_spec(cleaned_text if cleaned_text else raw_text)
                        if _spec is not None:
                            await ws.send_json({
                                "event": "mission.spec_ready",
                                "session_id": cur_session.id,
                                "spec_markdown": _spec.raw,
                                "refined_intent": _spec.refined_intent,
                            })
                except Exception as _e:
                    logger.debug("mission spec extraction failed: %s", _e)

            if event_type == "status":
                stats = event.get("stats", {})
                model = event.get("model", "")
                in_tok = stats.get("input_tokens", 0)
                out_tok = stats.get("output_tokens", 0)
                if (in_tok or out_tok) and state.settings.get("cost_tracking", "enabled", True):
                    cost = state.costs.record_usage(model, in_tok, out_tok)
                    stats["cost_usd"] = round(cost, 6)
                    stats["session_cost_usd"] = state.costs.get_session_cost()["cost_usd"]
                    # v0.5.9a2 — route this status event into the
                    # active autonomous mission's per-iter bucket
                    # if one is open. Sub-mission status events
                    # carry the SUB intent_id; we route by the
                    # active autonomous intent (set by the
                    # autonomous-event forwarder on iter_started).
                    try:
                        active_intent = getattr(
                            state, "_active_autonomous_intent_id", "",
                        )
                        if active_intent and state.iter_cost_tracker is not None:
                            state.iter_cost_tracker.record_status(
                                active_intent, model, in_tok, out_tok, cost,
                            )
                    except Exception:
                        logger.debug(
                            "iter_cost_tracker.record_status raised",
                            exc_info=True,
                        )
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
                # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
                # to the remote backend's `apply_harness_update`.
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


# ── Skill list/view payload helpers (v0.6.2a3) ───────────────────────


def _workspace_language_hints(project_path: str, *, max_files: int = 1600) -> set[str]:
    ext_map = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".cs": "csharp",
        ".java": "java",
        ".lua": "lua",
        ".rb": "ruby",
        ".php": "php",
    }
    skip_dirs = {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
        "node_modules", "dist", "build", "target", ".next", ".turbo",
    }
    found: set[str] = set()
    root = Path(project_path or "")
    if not root.exists():
        return found

    scanned = 0
    try:
        for _dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".cache")]
            for filename in filenames:
                scanned += 1
                lang = ext_map.get(Path(filename).suffix.lower())
                if lang:
                    found.add(lang)
                if scanned >= max_files or len(found) >= 8:
                    return found
    except OSError:
        return found
    return found


def _lsp_list_payload(*, project_path: str = "", settings: SettingsManager | None = None) -> dict:
    """Build the {event: "lsp_list", servers: [...]} status payload.

    Resonant does not yet own a full LSP client, so this is an inventory:
    explicitly configured servers plus common language-server binaries found
    on PATH for languages present in the current workspace.
    """
    configured = settings.get("lsp_servers") if settings else {}
    if not isinstance(configured, dict):
        configured = {}

    servers: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name, raw in configured.items():
        data = raw if isinstance(raw, dict) else {"command": str(raw)}
        command = str(data.get("command", "") or "").strip()
        enabled = bool(data.get("enabled", True))
        try:
            # posix=False on Windows: POSIX rules eat the backslashes in
            # "C:\tools\...\server.exe" and which() never finds it.
            parts = shlex.split(command, posix=(os.name != "nt")) if command else []
            command_head = parts[0].strip('"') if parts else ""
        except ValueError:
            command_head = command.split(" ", 1)[0] if command else ""
        available = bool(command_head and shutil.which(command_head))
        status = "disabled" if not enabled else ("available" if available else "missing")
        servers.append({
            "id": f"configured:{name}",
            "name": str(data.get("name") or name),
            "command": command,
            "enabled": enabled,
            "available": available,
            "status": status,
            "source": "configured",
        })
        seen_names.add(str(name).lower())

    workspace_langs = _workspace_language_hints(project_path)
    common_specs = [
        {
            "id": "typescript",
            "name": "TypeScript/JavaScript",
            "languages": ["typescript", "javascript"],
            "executables": ["typescript-language-server"],
            "command": "typescript-language-server --stdio",
        },
        {
            "id": "python-pyright",
            "name": "Python (Pyright)",
            "languages": ["python"],
            "executables": ["pyright-langserver"],
            "command": "pyright-langserver --stdio",
        },
        {
            "id": "python-pylsp",
            "name": "Python (pylsp)",
            "languages": ["python"],
            "executables": ["pylsp"],
            "command": "pylsp",
        },
        {
            "id": "rust-analyzer",
            "name": "Rust Analyzer",
            "languages": ["rust"],
            "executables": ["rust-analyzer"],
            "command": "rust-analyzer",
        },
        {
            "id": "gopls",
            "name": "Go",
            "languages": ["go"],
            "executables": ["gopls"],
            "command": "gopls",
        },
        {
            "id": "csharp",
            "name": "C#",
            "languages": ["csharp"],
            "executables": ["csharp-ls", "omnisharp"],
            "command": "csharp-ls",
        },
        {
            "id": "java",
            "name": "Java",
            "languages": ["java"],
            "executables": ["jdtls"],
            "command": "jdtls",
        },
        {
            "id": "lua",
            "name": "Lua",
            "languages": ["lua"],
            "executables": ["lua-language-server"],
            "command": "lua-language-server",
        },
    ]
    for spec in common_specs:
        if spec["id"] in seen_names:
            continue
        relevant = bool(workspace_langs.intersection(spec["languages"]))
        executable = next((exe for exe in spec["executables"] if shutil.which(exe)), "")
        if not relevant and not executable:
            continue
        servers.append({
            "id": spec["id"],
            "name": spec["name"],
            "command": spec["command"],
            "enabled": False,
            "available": bool(executable),
            "status": "available" if executable else "missing",
            "source": "detected",
            "languages": spec["languages"],
            "detail": (f"Installed: {executable}" if executable else "Not installed on PATH"),
        })

    servers.sort(key=lambda item: (
        0 if item.get("source") == "configured" else 1,
        0 if item.get("available") else 1,
        str(item.get("name", "")).lower(),
    ))
    return {
        "event": "lsp_list",
        "servers": servers,
        "workspace_languages": sorted(workspace_langs),
    }


def _plugin_list_payload(*, settings: SettingsManager | None = None) -> dict:
    """Build the {event: "plugin_list", plugins: [...]} status payload.

    Skills are a prompt/runtime capability and remain in the sidebar. This
    payload is reserved for Resonant plugin packages so pinned skills do not
    appear as plugins in the OpenCode-style status popover.
    """
    configured = settings.get("plugins") if settings else {}
    if isinstance(configured, dict):
        raw_items = configured.items()
    elif isinstance(configured, list):
        raw_items = ((str(idx), item) for idx, item in enumerate(configured))
    else:
        raw_items = []

    plugins: list[dict[str, Any]] = []
    for key, raw in raw_items:
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            data = {"path": raw}
        else:
            data = {"enabled": bool(raw)}

        name = str(data.get("name") or data.get("id") or key or "").strip()
        if not name:
            continue

        plugin_path = str(data.get("path") or data.get("directory") or "").strip()
        enabled = bool(data.get("enabled", True))
        available = True
        if plugin_path:
            try:
                available = Path(plugin_path).expanduser().exists()
            except OSError:
                available = False

        status = str(data.get("status") or (
            "disabled" if not enabled else "missing" if not available else "available"
        ))
        plugins.append({
            "id": str(data.get("id") or key),
            "name": name,
            "description": str(data.get("description") or data.get("detail") or ""),
            "path": plugin_path,
            "source": str(data.get("source") or "configured"),
            "version": str(data.get("version") or ""),
            "enabled": enabled,
            "available": available,
            "status": status,
        })

    plugins.sort(key=lambda item: (
        0 if item.get("enabled") else 1,
        0 if item.get("available") else 1,
        str(item.get("name", "")).lower(),
    ))
    return {
        "event": "plugin_list",
        "plugins": plugins,
        "summary": {
            "configured": len(plugins),
            "enabled": sum(1 for item in plugins if item.get("enabled")),
        },
    }


def _skill_list_payload(*, project_path: str = "", include_deprecated: bool = False) -> dict:
    """Build the {event: "skill_list", skills: [...]} message body.

    Pulls every visible skill via `list_skills_filtered`, projects to a
    JSON-safe shape, and sorts pinned-first then most-recently-used.
    Used by the Skills sidebar panel.
    """
    from ..orchestration.skills import list_skills_filtered
    skills = list_skills_filtered(
        project_path=project_path or None,
        include_deprecated=include_deprecated,
    )
    rows: list[dict] = []
    for s in skills:
        rows.append({
            "id": s.id,
            "name": s.name,
            "description": s.description or "",
            "scope": s.scope,
            "created_by": s.created_by,
            "pinned": bool(s.pinned),
            "deprecated": bool(s.is_deprecated()),
            "success_count": int(s.success_count),
            "fail_count": int(s.fail_count),
            "last_used_at": float(s.last_used_at or 0),
            "version": s.version or "1.0.0",
        })
    rows.sort(key=lambda r: (
        # Pinned first
        0 if r["pinned"] else 1,
        # Then most-recently-used
        -(r["last_used_at"] or 0),
        # Then alphabetical for stable ordering
        r["id"],
    ))
    return {"event": "skill_list", "skills": rows}


def _skill_view_payload(skill_id: str, *, project_path: str = "") -> dict:
    """Build the {event: "skill_view_data", skill: {...}} body.

    Includes the full procedure_md body so the detail modal can render
    it without a second round-trip.

    Resolves across scopes (project → global → stack) the same way the
    `resonant-skill` CLI does, so the GUI can view a project-scoped
    skill without the caller having to pre-figure-out which scope it
    lives in.
    """
    from ..orchestration.skills import load_skill, skill_dir
    s: Optional[Any] = None
    resolved_scope = "global"
    candidates = []
    if project_path:
        candidates.append(("project", {"project_path": project_path}))
    candidates.append(("global", {}))
    # stack scope needs a stack_sig — skip for v0.6.2.
    for scope, kw in candidates:
        s = load_skill(skill_id, scope=scope, **kw)
        if s is not None:
            resolved_scope = scope
            break
    if s is None:
        return {"event": "skill_view_data", "skill": None, "error": f"skill {skill_id!r} not found"}
    # Find the procedure.md sidecar in the resolved scope.
    procedure_md = ""
    try:
        d = skill_dir(skill_id, scope=resolved_scope,
                      project_path=project_path or None if resolved_scope == "project" else None)
        md = d / "procedure.md"
        if md.exists():
            procedure_md = md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        procedure_md = ""
    return {
        "event": "skill_view_data",
        "skill": {
            "id": s.id,
            "name": s.name,
            "description": s.description or "",
            "scope": s.scope,
            "created_by": s.created_by,
            "pinned": bool(s.pinned),
            "deprecated": bool(s.is_deprecated()),
            "success_count": int(s.success_count),
            "fail_count": int(s.fail_count),
            "last_used_at": float(s.last_used_at or 0),
            "version": s.version or "1.0.0",
            "triggers": list(s.triggers or []),
            "procedure_md": procedure_md,
        },
    }


# ── Project conventions file helpers ─────────────────────────────────

def _save_resonant_md(project_path: str, content: str):
    """Persist project conventions.

    Writes back to the existing instructions file if one is present (so a
    project already using RESONANT.md or CLAUDE.md keeps that filename).
    For brand-new projects, writes `AGENTS.md` — the cross-tool standard
    adopted by Codex, OpenCode, Cursor, and OpenHands.
    """
    existing = find_instruction_file(project_path)
    target = existing if existing else (Path(project_path) / "AGENTS.md")
    target.write_text(content, encoding="utf-8")


# ── HTTP Routes ───────────────────────────────────────────────────────

def _asset_version() -> str:
    """Compute a cache-buster string for the static assets the template loads.

    Uses the max mtime across `app.js` + `styles.css` + the template itself
    so any edit to those files generates a fresh value — dev iteration on
    the GUI no longer requires manual cache-busting (a real bug we hit
    during Phase-1 mission UI testing).

    For frozen / packaged builds the files don't change at runtime, so this
    just returns a stable value tied to install time, which is fine — the
    bundled exe ships with a single self-consistent set of assets.
    """
    try:
        static = Path(__file__).parent / "static"
        templates_dir = Path(__file__).parent / "templates"
        candidates = [
            static / "app.js",
            static / "styles.css",
            static / "plan_graph_view.js",
            templates_dir / "index.html",
        ]
        mtimes = [int(p.stat().st_mtime) for p in candidates if p.is_file()]
        if not mtimes:
            return "0"
        return str(max(mtimes))
    except Exception:
        return "0"


async def homepage(request):
    # Bug #23 fix — Starlette 0.29+ changed TemplateResponse signature:
    #   OLD: templates.TemplateResponse(name, {"request": request, ...})
    #   NEW: templates.TemplateResponse(request, name, {...})
    # Use the new (request-first) signature explicitly so it works with
    # both old and new Starlette.
    return templates.TemplateResponse(
        request,
        "index.html",
        {"asset_version": _asset_version()},
    )


# ── Starlette App ─────────────────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/", homepage),
        WebSocketRoute("/ws", websocket_endpoint),
        Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
)
