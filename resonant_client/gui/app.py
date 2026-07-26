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
import sys
import threading
import time
import difflib
from collections import deque
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
    ExoBackend,
    KimiBackend,
    OllamaBackend,
    codex_cli_model_labels,
    resolve_codex_cli_path,
)
from ..engine import Session
from ..network_defaults import default_thinking_for_model, resolve_exo_url, resolve_ollama_url
from . import ws_commands
# Payload builders moved to ws_commands.py with the handlers that use them.
# Re-exported because `skill_archive` still lives in the endpoint and because
# these are the module's established public surface for tests. Safe from
# circularity: ws_commands imports nothing from app.
from .ws_commands import (  # noqa: F401  (re-exported public surface)
    _save_resonant_md,
    _skill_list_payload,
    _skill_view_payload,
)
from .sessions import ProjectManager
from .settings import SettingsManager
from .costs import CostTracker
from .project_instructions import (
    get_instruction_info,
    load_project_instructions,
)
from .runtime import BackendSpec
from .evaluation_dashboard import EvaluationManager
from ..harness import HarnessWorkspace, HarnessOrchestrator, HarnessService
from ..harness.prompts import HarnessPrompts
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

STATUS_UPDATE_STEER = (
    "At the next safe agent boundary, give the user a concise progress update "
    "covering: what is complete, what you are doing now, what remains, and any "
    "real blocker. Then continue the original task without stopping or asking "
    "for confirmation unless you are genuinely blocked. Do not restart work "
    "or repeat completed steps."
)

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

    # Ordinary model turns do not set a generation-token limit. The provider's
    # natural stop and native context window are the only boundaries.
    SESSION_MAX_TOKENS = None
    HARNESS_ROLE_MAX_TOKENS = {
        "planner": None,
        "generator": None,
        "evaluator": None,
    }
    CODE_SESSION_ROLES = {"planner", "generator", "evaluator"}

    # Class-level so the lazy property works on instances built with
    # `AppState.__new__(AppState)` — several provider tests do exactly that to
    # skip __init__'s side effects (recents files, settings migration).
    _harness_prompts: Optional[HarnessPrompts] = None

    @property
    def harness_prompts(self) -> HarnessPrompts:
        """Prompt construction and payload inference for the harness roles.

        These 95 methods used to live directly on AppState, where they were
        roughly four fifths of the class. They are domain logic, not GUI
        runtime state; see `harness/prompts.py` for the narrow contract they
        depend on. Built lazily so constructing an AppState stays cheap.
        """
        if self._harness_prompts is None:
            self._harness_prompts = HarnessPrompts(self)
        return self._harness_prompts

    # ── Compatibility surface ────────────────────────────────────────────
    # The three moved methods with callers outside this module. Delegating
    # rather than making every caller learn the new location: `harness_enabled`
    # gates GUI features from settings.py, `get_harness_summary` is read by the
    # WS command registry, and `select_harness_backend` is pinned by the
    # provider tests.

    def harness_enabled(self) -> bool:
        return self.harness_prompts.harness_enabled()

    def get_harness_summary(self, project_path: Optional[str] = None) -> dict[str, Any]:
        return self.harness_prompts.get_harness_summary(project_path)

    def select_harness_backend(self, *args, **kwargs):
        return self.harness_prompts.select_harness_backend(*args, **kwargs)

    def __init__(self):
        self._harness_prompts = None
        self.available_backends: dict = {}
        self.backend = None
        self.backend_spec: Optional[BackendSpec] = None
        self.session: Optional[Session] = None
        # v0.4.4 (T1.4) — `api_url` (Resonant Engine remote) and
        # `lmstudio_url` (LM Studio probe) were retired. Both backends
        # were cut in v0.4.0 but the AppState fields lingered as dead
        # state. Provider URLs are set by `refresh_network_defaults`.
        self.ollama_url = ""
        self.exo_url = ""
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
            summary_getter=lambda project_path: self.harness_prompts.get_harness_summary(project_path),
            prompt_builder=lambda session_role, project_path, objective="": self.harness_prompts._build_harness_cycle_prompt(
                session_role=session_role,
                project_path=project_path,
                objective=objective,
            ),
            backend_selector=lambda session_role, project_path=None: self.harness_prompts.select_harness_backend(
                session_role=session_role,
                project_path=project_path,
            ),
            retry_backend_selector=lambda session_role, failed_backend="", project_path=None: self.harness_prompts.select_harness_retry_backend(
                session_role=session_role,
                failed_backend=failed_backend,
                project_path=project_path,
            ),
            role_timeout_getter=lambda session_role: self.harness_prompts.get_harness_role_timeout_seconds(session_role),
            retry_timeout_getter=lambda session_role: self.harness_prompts.get_harness_role_retry_timeout_seconds(session_role),
            role_runner=lambda **kwargs: self.harness_prompts.run_harness_role_once(**kwargs),
            teacher_escalator=lambda **kwargs: self.harness_prompts.run_harness_teacher_escalation(**kwargs),
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
        except Exception:
            pass

    def _apply_big_context_preset(self) -> None:
        """
        If `general.big_context_profile` is true and the user has NOT manually
        set RESONANT_OLLAMA_NUM_CTX/NUM_BATCH via env, bump them to the
        large-repository profile (131072 ctx, 2048 batch).

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
                all_tools=list(AGENT_TOOLS) + self.mcp_manager.get_all_tools(),
                project_instructions=(self._project_instructions or ""),
                settings=self.settings,
                on_event=on_event or (lambda ev: None),
                # v0.5.8a1 — wire the per-specialist Ollama model
                # resolver. None override → default backend.
                specialist_backend_resolver=self._build_specialist_backend,
                mcp_manager=self.mcp_manager,
            )
            self._intent_service_signature = signature
        elif on_event is not None:
            self._intent_service.on_event = on_event
        return self._intent_service

    def _module_name_from_target_file(raw_path: str) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        if not path.endswith(".py"):
            return ""
        module = path[:-3].strip("/").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return module

    def rewrite_last_assistant_message(session: Session, original_text: str, cleaned_text: str) -> None:
        if not original_text or original_text == cleaned_text:
            return
        for item in reversed(session.conversation_history):
            if item.get("role") == "assistant" and item.get("content") == original_text:
                item["content"] = cleaned_text
                return

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
        if self.harness_prompts.harness_enabled():
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
        from ..engine.agent_runtime import AgentRegistry
        from ..engine.artifacts import ArtifactStore
        from ..engine.checkpoint_timeline import SessionCheckpointStore
        from ..engine.capability_packs import CapabilityPackManager
        from ..engine.context_broker import ContextBroker
        from ..engine.flight_recorder import FlightRecorder
        from ..engine.director import DirectorBenchmarkStore, DirectorConfig, DirectorRun
        from ..engine.model_roles import ModelRoleRouter
        from ..engine.worktrees import WorktreeManager

        artifact_store = ArtifactStore(target_path)
        agent_registry = AgentRegistry(
            target_path,
            on_event=self._push_ws_event,
            artifact_store=artifact_store,
        )
        runtime_session_id = str(
            getattr(session.event_logger, "session_id", "") or uuid.uuid4().hex[:12]
        )
        checkpoint_store = SessionCheckpointStore(
            target_path,
            session_id=runtime_session_id,
        )
        flight_recorder = FlightRecorder(
            target_path,
            run_id=f"run_{runtime_session_id}",
            metadata={"session_mode": session.prompt_role},
        )
        worktree_manager = WorktreeManager(target_path)

        def _role_backend_factory(profile):
            active_spec = self.backend_spec
            backend_type = profile.backend_type or (
                active_spec.backend_type if active_spec else getattr(session.backend, "name", "ollama")
            )
            spec = self.build_backend_spec(
                backend_type,
                model=profile.model or getattr(session.backend, "model", ""),
                project_path=target_path,
            )
            if profile.thinking_mode:
                spec.thinking_mode = profile.thinking_mode
            return spec.create_backend(self.settings)

        record = self.project.current_session
        director_config = DirectorConfig.from_dict(
            getattr(record, "director_config", {}) if record else {}
        )
        role_router = ModelRoleRouter(
            self.settings.get("model_roles") or {},
            workers=[worker.to_dict() for worker in director_config.workers],
            backend_factory=_role_backend_factory,
        )
        context_broker = ContextBroker(
            target_path,
            agent_registry=agent_registry,
            checkpoint_store=checkpoint_store,
            artifact_store=artifact_store,
            codebase_index=session._codebase_index,
        )
        capability_packs = CapabilityPackManager(
            target_path,
            configured=self.settings.get("plugins") or {},
        )
        capability_packs.discover()
        self.hook_runner.add_hooks(capability_packs.hook_definitions())
        # Trusted packs may contribute MCP servers. They are connected with an
        # explicit config object, so no untrusted manifest reaches execution.
        from ..engine.mcp import MCPServerConfig
        for server_name, server_data in capability_packs.mcp_servers().items():
            try:
                self.mcp_manager.connect(
                    server_name,
                    MCPServerConfig.from_dict(server_name, server_data),
                )
            except Exception:
                logger.warning("Capability-pack MCP connection failed: %s", server_name, exc_info=True)
        session.mcp_tools = self.mcp_manager.get_all_tools()
        session.agent_registry = agent_registry
        session.artifact_store = artifact_store
        session.checkpoint_store = checkpoint_store
        session.checkpoint_display_provider = lambda: list(
            getattr(self.project.current_session, "display_events", []) or []
        )
        session.flight_recorder = flight_recorder
        session.context_broker = context_broker
        session.model_role_router = role_router
        session.benchmark_store = DirectorBenchmarkStore(target_path)
        if director_config.enabled:
            run_id = str(getattr(record, "director_run_id", "") or "") if record else ""
            try:
                director_run = (
                    DirectorRun.load(
                        target_path, run_id, on_event=self._push_ws_event,
                    )
                    if run_id else None
                )
            except (OSError, ValueError, KeyError):
                logger.warning("Director run %s could not be restored; starting a new run", run_id)
                director_run = None
            if director_run is None:
                director_run = DirectorRun(
                    target_path,
                    config=director_config,
                    on_event=self._push_ws_event,
                )
            session.director_run = director_run
            director_prompt = director_run.system_prompt()
            session.role_instructions = "\n\n".join(
                value for value in (session.role_instructions, director_prompt) if value
            )
            if record:
                record.orchestration_mode = "director"
                record.director_config = director_config.to_dict()
                record.director_run_id = director_run.id
                record.save()
        session.worktree_manager = worktree_manager
        session.capability_packs = capability_packs
        session._settings_ref = self.settings
        from ..orchestration.skill_loader import build_skill_context
        def _combined_skill_context(query):
            built_in = build_skill_context(query, project_path=target_path, max_skills=6)
            return (getattr(built_in, "block", "") or "") + capability_packs.skill_context(query)

        session._skill_context_provider = _combined_skill_context
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
            if models:
                available["ollama"] = {"url": ollama_url, "models": models}
        except Exception:
            # Non-fatal — empty available means "show the Ollama wizard."
            pass

        exo_catalog = ExoBackend.discover_models(base_url=self.exo_url, timeout=4.0)
        exo_models = exo_catalog["models"]
        if exo_models:
            downloaded = set(exo_catalog["downloaded_models"])
            running = set(exo_catalog["running_models"])
            available["exo"] = {
                "url": self.exo_url,
                "models": exo_models,
                "running_models": list(exo_catalog["running_models"]),
                "downloaded_models": list(exo_catalog["downloaded_models"]),
                "model_labels": {
                    model: (
                        f"{model} (running)" if model in running
                        else f"{model} (downloaded)" if model in downloaded
                        else model
                    )
                    for model in exo_models
                },
            }

        codex_cli = resolve_codex_cli_path()
        if codex_cli:
            available["codex"] = {
                "models": CodexCliBackend.list_available_models(),
                "model_labels": codex_cli_model_labels(),
                "cli_path": codex_cli,
            }

        kimi_key, kimi_source, kimi_env, kimi_setting = self._api_key_details(
            "kimi", "MOONSHOT_API_KEY"
        )
        if kimi_key:
            available["kimi"] = {
                "url": os.environ.get("MOONSHOT_BASE_URL", KimiBackend.DEFAULT_BASE_URL),
                "models": list(KimiBackend.MODELS),
                "model_labels": {KimiBackend.DEFAULT_MODEL: "Kimi K3"},
                "api_key_source": kimi_source,
                "api_key_env": kimi_env,
                "api_key_setting": kimi_setting,
            }

        self.available_backends = available
        return available

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

        if backend_type == "codex":
            info = self.available_backends.get("codex") or {}
            models = info.get("models") or CodexCliBackend.list_available_models()
            selected_model = model or self._resolve_default_model(models)
            spec = BackendSpec(backend_type="codex", model=selected_model)
            spec.cwd = project_path
            spec.permission_mode = self.permission_mode
            return spec

        if backend_type == "kimi":
            info = self.available_backends.get("kimi") or {}
            api_key, source, env_var, setting = self._api_key_details(
                "kimi", "MOONSHOT_API_KEY"
            )
            if not api_key:
                raise ValueError(
                    "Kimi API key required. Add it in Settings -> Kimi API or set "
                    "MOONSHOT_API_KEY."
                )
            models = info.get("models") or list(KimiBackend.MODELS)
            spec = BackendSpec(
                backend_type="kimi",
                model=model or self._resolve_default_model(models) or KimiBackend.DEFAULT_MODEL,
                base_url=info.get("url") or KimiBackend.DEFAULT_BASE_URL,
                api_key_source=source,
                api_key_env=env_var,
                api_key_setting=setting,
                thinking_mode="max",
            )
            return spec

        if backend_type == "exo":
            info = self.available_backends.get("exo") or {}
            models = info.get("models") or []
            if not models:
                raise ValueError(
                    "EXO is not reachable or has no models. Check the EXO URL in "
                    "Settings -> Network."
                )
            api_key, source, env_var, _ = self._api_key_details("exo", "EXO_API_KEY")
            return BackendSpec(
                backend_type="exo",
                model=model or self._resolve_default_model(models),
                base_url=info.get("url") or self.exo_url or ExoBackend.DEFAULT_BASE_URL,
                api_key_source=source,
                api_key_env=env_var,
                api_key=api_key if source == "literal" else "",
            )

        if backend_type != "ollama":
            raise ValueError(
                f"Backend '{backend_type}' is not supported. Resonant Client "
                f"supports Ollama, EXO, Kimi, and Codex."
            )

        info = self.available_backends.get("ollama")
        if not info:
            raise ValueError(
                "Ollama is not reachable. Check the URL in Settings → Network "
                "(default: http://127.0.0.1:11434) and that "
                "`ollama serve` is running."
            )

        models = info.get("models") or []
        selected_model = model or self._resolve_default_model(models)
        spec = BackendSpec(backend_type="ollama", model=selected_model)
        spec.url = info.get("url", "")
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
        # Configured model isn't currently available. Fall back to first detected so the
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
        if not override:
            return None
        try:
            if isinstance(self.backend, OllamaBackend):
                return OllamaBackend(
                    base_url=self.backend.base_url,
                    model=override,
                    thinking=getattr(self.backend, "thinking_mode", None),
                )
            active_spec = getattr(self, "backend_spec", None)
            if active_spec:
                spec = self.build_backend_spec(
                    active_spec.backend_type,
                    model=override,
                    project_path=self.project.project_path,
                )
                spec.thinking_mode = active_spec.thinking_mode
                return spec.create_backend(self.settings)
            logger.warning(
                "specialist override requested for %s without a reproducible backend spec",
                specialization,
            )
            return None
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
        director_config: Optional[dict[str, Any]] = None,
        director_run_id: str = "",
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

        # Director Mode is session-local. Explicit values are persisted before
        # wiring; otherwise a resumed record restores its own configuration.
        record = self.project.current_session
        if record and director_config is not None:
            from ..engine.director import DirectorConfig
            normalized = DirectorConfig.from_dict(director_config)
            record.director_config = normalized.to_dict()
            record.orchestration_mode = "director" if normalized.enabled else "single"
            record.director_run_id = str(director_run_id or "")
            record.save()

        if self._normalize_path(effective_root) == self._normalize_path(self.project.project_path):
            project_instructions = self._project_instructions or load_project_instructions(effective_root)
            engram = self.engram
            codebase_index = self.codebase_index
        else:
            project_instructions = load_project_instructions(effective_root)
            engram = self.base_engram.clone(namespace=self._project_namespace(effective_root))
            engram.set_mcp_manager(self.mcp_manager)
            codebase_index = CodebaseIndex(effective_root, engram=engram)

        harness_instructions = self.harness_prompts.build_harness_instructions(
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
            max_tokens=max_tokens,
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
        backend_order.extend(k for k in ("ollama", "exo", "kimi", "codex") if k not in backend_order)

        for backend_type in backend_order:
            info = self.available_backends.get(backend_type) or {}
            models = list(info.get("models") or [])
            if models:
                return backend_type, self._resolve_default_model(models)
        return "", ""

    def project_chat_backend_choice(self) -> tuple[str, str]:
        """Prefer the model this project most recently used.

        Provider discovery is transient: a distributed EXO model may disappear
        from ``/models`` while its runner restarts.  Falling through to the
        provider's first remaining model silently changes user intent.  Keep
        the recorded project model instead and let the provider report a clear
        availability error if it has not recovered yet.
        """
        record = self.project.current_session
        if record and record.backend_type and record.model:
            return record.backend_type, record.model
        for summary in self.project.list_sessions():
            backend_type = str(summary.get("backend_type") or "").strip()
            model = str(summary.get("model") or "").strip()
            if backend_type and model:
                return backend_type, model
        return self.default_chat_backend_choice()

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
            backend_type, model = self.project_chat_backend_choice()
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
        # Keep the persisted/sidebar identity in lockstep with the live backend.
        # Without this, the composer showed the newly selected EXO model while
        # the session list kept advertising the model that created the session.
        if self.project.current_session:
            self.project.current_session.backend_type = spec.backend_type
            self.project.current_session.model = spec.model
            self.project.current_session.thinking_mode = spec.thinking_mode or ""
            self.project.save_current_session(engine_session=self.session)
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

        if self.backend_spec and self.backend_spec.backend_type == "kimi":
            kimi_key, _, _, _ = self._api_key_details("kimi", "MOONSHOT_API_KEY")
            if not kimi_key:
                fallback_type, fallback_model = self.default_chat_backend_choice()
                if fallback_type and fallback_model:
                    self.swap_backend(fallback_type, fallback_model)
                else:
                    self.backend = None
                    self.backend_spec = None
                    self.session = None

        if (
            self.backend_spec and
            self.backend_spec.backend_type in {"ollama", "exo", "kimi"} and
            section in {"api_keys", "engram", "general", "network"}
        ):
            try:
                if section == "network" and self.backend_spec.backend_type == "ollama":
                    self.backend_spec.url = self.ollama_url
                if section == "network" and self.backend_spec.backend_type == "exo":
                    self.backend_spec.base_url = self.exo_url
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
        self.exo_url = resolve_exo_url(settings_data=settings_data)

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
        model_capabilities = {}
        if self.backend:
            current_backend = getattr(self.backend, "name", "")
            current_model = getattr(self.backend, "model", "")
            handles_tools = getattr(self.backend, "handles_tools", False)
            profile = getattr(self.backend, "capability_profile", None)
            if profile is not None and hasattr(profile, "to_dict"):
                model_capabilities = profile.to_dict()

        return {
            "event": "init",
            "refresh_only": refresh_only,
            "backends": backends_info,
            "current_backend": current_backend,
            "current_model": current_model,
            "handles_tools": handles_tools,
            "model_capabilities": model_capabilities,
            "permission_mode": self.permission_mode,
            "cwd": self.project.project_path.replace("\\", "/"),
            "sessions": self.project.list_sessions(),
            "all_sessions": self.project.list_all_sessions(),
            "current_session_id": self.project.current_session.id if self.project.current_session else "",
            "current_session_role": self.project.current_session.session_role if self.project.current_session else "generator",
            "orchestration_mode": (
                self.project.current_session.orchestration_mode
                if self.project.current_session else "single"
            ),
            "director_config": (
                self.project.current_session.director_config
                if self.project.current_session else {}
            ),
            "director_run": (
                self.session.director_run.to_dict()
                if self.session and getattr(self.session, "director_run", None) else None
            ),
            "director_benchmark": (
                self.session.benchmark_store.comparison()
                if self.session and getattr(self.session, "benchmark_store", None) else None
            ),
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
            "harness": self.harness_prompts.get_harness_summary(self.project.project_path),
            "harness_cycles": self.harness_orchestrator.list_runs(),
            "harness_enabled": self.harness_prompts.harness_enabled(),
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

async def _process_chat_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    """Run one serialized chat turn without blocking the WS receive loop."""
    text = str(msg.get("text") or "").strip()
    if not text:
        return
    if not state.session:
        await ws.send_json({"event": "error", "message": "No backend selected"})
        return

    images = None
    raw_images = msg.get("images", [])
    if raw_images:
        import base64 as _b64
        images = []
        for image in raw_images:
            data = image.get("data", "")
            media_type = image.get("media_type", "image/png")
            try:
                images.append((_b64.b64decode(data), media_type))
            except Exception:
                pass

    session_mode = "code"
    session_role = (
        state.project.current_session.session_role
        if state.project.current_session else "generator"
    )
    if not state.project.current_session:
        state.ensure_persisted_current_session(session_role=session_role)

    if not state._first_message_sent:
        state.project.update_session_title(text)

    text_for_session = text
    if not state._first_message_sent and state.harness_prompts.harness_enabled():
        harness_summary = state.harness_prompts.get_harness_summary(state.project.project_path)
        has_active_sprint = bool(
            harness_summary.get("active_sprint_id")
            and harness_summary.get("contract_status") in {"approved", "needs_revision"}
        )
        if has_active_sprint:
            text_for_session = state.harness_prompts.wrap_user_message_for_harness(
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
    # Close the run's steering window before the next serialized chat turn.
    # This catches the tiny race where direction arrives after the engine's
    # final safe boundary but before the websocket runner marks itself idle.
    state.session.discard_steering()

    state.project.save_current_session(state.session, display_events=display_events)
    await ws.send_json({
        "event": "sessions_updated",
        "sessions": state.project.list_sessions(),
        "current_session_id": (
            state.project.current_session.id if state.project.current_session else ""
        ),
    })


async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket handler — bidirectional communication with frontend."""
    await ws.accept()

    pending_chat_messages: list[dict[str, Any]] = []
    chat_runner: asyncio.Task | None = None
    cancel_request_id: str | None = None
    clear_request_cache: dict[str, dict[str, Any]] = {}

    async def _drain_chat_queue() -> None:
        nonlocal chat_runner, cancel_request_id
        try:
            while pending_chat_messages:
                queued = pending_chat_messages.pop(0)
                try:
                    if queued.get("_was_queued"):
                        await ws.send_json({
                            "event": "message.started",
                            "message_id": queued.get("message_id", ""),
                            "text": queued.get("text", ""),
                        })
                    await _process_chat_message(ws, queued)
                except WebSocketDisconnect:
                    raise
                except Exception as exc:
                    logger.exception("queued chat turn failed")
                    try:
                        await ws.send_json({"event": "error", "message": str(exc)})
                    except Exception:
                        pass
        except WebSocketDisconnect:
            raise
        finally:
            chat_runner = None
            if cancel_request_id:
                completed_id = cancel_request_id
                cancel_request_id = None
                try:
                    await ws.send_json({
                        "event": "cancel.completed",
                        "cancel_id": completed_id,
                    })
                except Exception:
                    pass

    async def _enqueue_chat_message(msg: dict[str, Any]) -> None:
        nonlocal chat_runner
        message_id = str(msg.get("message_id") or uuid.uuid4())
        running = chat_runner is not None and not chat_runner.done()
        queued = dict(msg, message_id=message_id, _was_queued=running)
        pending_chat_messages.append(queued)
        if running:
            await ws.send_json({
                "event": "message.queued",
                "message_id": message_id,
                "text": queued.get("text", ""),
                "position": len(pending_chat_messages),
                "steering": False,
            })
        else:
            chat_runner = asyncio.create_task(_drain_chat_queue())

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

            # Self-contained commands (read some state, send one reply) live in
            # gui/ws_commands.py with an explicit context instead of closing
            # over this function's ~2,600-line local scope. See that module for
            # what qualifies. Everything below is still entangled with the run
            # loop — the chat runner, backend rebuilds, the autonomous daemon —
            # and needs untangling rather than relocation.
            standalone = ws_commands.HANDLERS.get(command)
            if standalone is not None:
                await standalone(ws_commands.CommandContext(
                    ws=ws, state=state, msg=msg, chat_runner=chat_runner,
                ))
                continue

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

            elif command == "get_harness_resume_prompt":
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                prompt = state.harness_prompts.build_harness_resume_prompt(
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
                    (msg.get("failed_role") or state.harness_prompts.get_harness_summary().get("active_role") or "generator"),
                )
                objective = (msg.get("objective") or "").strip()
                try:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: state.harness_prompts.run_harness_teacher_escalation(
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
                    await ws.send_json({"event": "harness_state", "data": state.harness_prompts.get_harness_summary()})
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
                await ws.send_json({"event": "harness_state", "data": state.harness_prompts.get_harness_summary()})
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
                await ws.send_json({"event": "harness_state", "data": state.harness_prompts.get_harness_summary()})
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
                await ws.send_json({"event": "harness_state", "data": state.harness_prompts.get_harness_summary()})
                await ws.send_json({"event": "status_msg", "message": f"Evaluator marked sprint {sprint_id} as {verdict}"})

            elif command == "select_backend":
                backend_type = msg.get("backend", "")
                model = msg.get("model", "")
                session_mode = msg.get("session_mode", "code")
                session_role = msg.get("session_role", "generator")
                previous_record = state.project.current_session
                try:
                    # Backend setup starts a fresh, lazily-persisted session.
                    # Detach the prior record so the first message cannot
                    # overwrite an existing conversation. Restore it if the
                    # provider connection itself fails.
                    state.project.current_session = None
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
                    state.project.current_session = previous_record
                    await ws.send_json({"event": "error", "message": str(e)})

            elif command == "message":
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if not state.session:
                    await ws.send_json({"event": "error", "message": "No backend selected"})
                    continue
                await _enqueue_chat_message(msg)
                continue

            elif command == "agent_restart":
                # Re-dispatch a worker whose thread died with the process.
                #
                # Deliberately NOT in ws_commands.py: this starts a real run
                # that streams for minutes and owns the chat runner, which is
                # exactly the coupling that module exists to stay out of.
                agent_id = str(msg.get("agent_id") or "")
                if not state.session:
                    await ws.send_json({"event": "error", "message": "No backend selected"})
                    continue
                if chat_runner is not None and not chat_runner.done():
                    await ws.send_json({
                        "event": "error",
                        "message": "Finish or stop the active run before restarting an agent.",
                    })
                    continue
                try:
                    # Resolve the assignment before spawning anything, so an
                    # unknown or already-completed agent fails as a clean error
                    # instead of a half-started run.
                    registry = getattr(state.session, "agent_registry", None)
                    if registry is None:
                        raise RuntimeError("This session has no agent registry.")
                    assignment = registry.restart_assignment(agent_id)
                except KeyError:
                    # str(KeyError) is the repr of its argument, quotes and all.
                    await ws.send_json({
                        "event": "error",
                        "message": f"Unknown agent: {agent_id}",
                    })
                    continue
                except (ValueError, RuntimeError) as exc:
                    await ws.send_json({"event": "error", "message": str(exc)})
                    continue

                state.cancel_requested.clear()
                session_for_restart = state.session

                def _restart_source(_agent_id=agent_id, _session=session_for_restart):
                    return _session.restart_agent(_agent_id)

                await ws.send_json({
                    "event": "agent.restarted",
                    "source_agent_id": agent_id,
                    "agent_type": assignment["agent_type"],
                    "completed_steps": assignment["completed_steps"],
                })
                chat_runner = asyncio.ensure_future(_run_session_streaming(
                    ws,
                    state.session,
                    assignment["prompt"],
                    display_user_msg=(
                        f"Restarting {assignment['agent_type']} agent "
                        f"(interrupted after {assignment['completed_steps']} step"
                        f"{'' if assignment['completed_steps'] == 1 else 's'})"
                    ),
                    event_source=_restart_source,
                ))
                continue

            elif command == "status_update":
                # A status request is steering, never a replacement turn. Do
                # not enqueue it if the active run ends during this command;
                # that would unexpectedly start a new task after the race.
                message_id = str(msg.get("message_id") or uuid.uuid4())
                running = chat_runner is not None and not chat_runner.done()
                accepted = bool(
                    running
                    and state.session
                    and state.session.steer(
                        STATUS_UPDATE_STEER,
                        message_id=message_id,
                    )
                )
                await ws.send_json({
                    "event": (
                        "status.update_queued"
                        if accepted
                        else "status.update_rejected"
                    ),
                    "message_id": message_id,
                    "message": (
                        "Agent update queued for the next safe step."
                        if accepted
                        else "The run ended before the agent update could be queued."
                    ),
                })
                continue

            elif command == "steer":
                text = str(msg.get("text") or "").strip()
                message_id = str(msg.get("message_id") or uuid.uuid4())
                running = chat_runner is not None and not chat_runner.done()
                if not text or not state.session:
                    continue
                if running and state.session.steer(text, message_id=message_id):
                    await ws.send_json({
                        "event": "message.queued",
                        "message_id": message_id,
                        "text": text,
                        "position": 0,
                        "steering": True,
                    })
                else:
                    await _enqueue_chat_message(dict(msg, command="message"))
                continue

            elif command == "steer_queued":
                message_id = str(msg.get("message_id") or "")
                queued_index = next(
                    (
                        index for index, item in enumerate(pending_chat_messages)
                        if str(item.get("message_id") or "") == message_id
                    ),
                    None,
                )
                if queued_index is None:
                    await ws.send_json({
                        "event": "ui_notice",
                        "message": "That follow-up has already started or is no longer queued.",
                    })
                    continue
                queued = pending_chat_messages.pop(queued_index)
                steering_text = str(queued.get("text") or "").strip()
                accepted = bool(
                    state.session
                    and state.session.steer(steering_text, message_id=message_id)
                )
                if not accepted:
                    pending_chat_messages.insert(queued_index, queued)
                    await ws.send_json({
                        "event": "ui_notice",
                        "message": "The active run could not accept that steering message.",
                    })
                    continue
                await ws.send_json({
                    "event": "message.queued",
                    "message_id": message_id,
                    "text": steering_text,
                    "position": 0,
                    "steering": True,
                })
                continue

            elif command == "remove_queued":
                message_id = str(msg.get("message_id") or "")
                queued_index = next(
                    (
                        index for index, item in enumerate(pending_chat_messages)
                        if str(item.get("message_id") or "") == message_id
                    ),
                    None,
                )
                removed = False
                if queued_index is not None:
                    pending_chat_messages.pop(queued_index)
                    removed = True
                elif state.session and hasattr(state.session, "remove_steering"):
                    removed = bool(state.session.remove_steering(message_id))

                if removed:
                    await ws.send_json({
                        "event": "message.removed",
                        "message_id": message_id,
                    })
                else:
                    await ws.send_json({
                        "event": "message.remove_failed",
                        "message_id": message_id,
                        "message": "That follow-up has already started or was already applied.",
                    })
                continue

            elif command == "cancel":
                cancel_id = str(msg.get("cancel_id") or uuid.uuid4())
                cleared_ids = [
                    str(item.get("message_id") or "") for item in pending_chat_messages
                ]
                pending_chat_messages.clear()
                await ws.send_json({
                    "event": "cancel.requested",
                    "cancel_id": cancel_id,
                })
                state.cancel_requested.set()
                if state.session:
                    state.session.cancel()
                if cleared_ids:
                    await ws.send_json({
                        "event": "message.queue_cleared",
                        "message_ids": cleared_ids,
                    })
                if chat_runner is not None and not chat_runner.done():
                    cancel_request_id = cancel_id
                else:
                    await ws.send_json({
                        "event": "cancel.completed",
                        "cancel_id": cancel_id,
                    })

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
                        # Per-run: how long this mission may sit parked on a
                        # human decision before proceeding with REFLECT's
                        # nominated option. Empty preserves wait-forever.
                        decision_timeout_label=str(
                            msg.get("decision_timeout") or ""
                        ).strip(),
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
                request_id = str(msg.get("request_id") or "").strip()
                if request_id and request_id in clear_request_cache:
                    await ws.send_json(clear_request_cache[request_id])
                    continue
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
                response = {
                    "event": "session_cleared",
                    "sessions": state.project.list_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                    "session_mode": session_mode,
                    "session_role": session_role,
                    "cwd": state.project.project_path,
                    "request_id": request_id,
                }
                if request_id:
                    clear_request_cache[request_id] = response
                    if len(clear_request_cache) > 32:
                        clear_request_cache.pop(next(iter(clear_request_cache)))
                await ws.send_json(response)

            elif command == "director_configure":
                from ..engine.director import DirectorConfig

                config = DirectorConfig.from_dict(msg.get("config") or {})
                enabled_workers = [
                    worker for worker in config.workers
                    if worker.enabled and worker.backend_type and worker.model
                ]
                if config.enabled and (
                    not config.director_backend_type or not config.director_model
                ):
                    await ws.send_json({
                        "event": "director.configure_failed",
                        "message": "Select a Director model before enabling Director Mode.",
                    })
                    continue
                if config.enabled and not enabled_workers:
                    await ws.send_json({
                        "event": "director.configure_failed",
                        "message": "Select at least one worker model.",
                    })
                    continue

                record = state.project.current_session
                if record is None:
                    record = state.project.create_session(
                        backend_type=config.director_backend_type or getattr(state.backend, "name", ""),
                        model=config.director_model or getattr(state.backend, "model", ""),
                        session_role="generator",
                        orchestration_mode="director" if config.enabled else "single",
                        director_config=config.to_dict(),
                    )
                old_history = list(state.session.conversation_history) if state.session else []
                record.orchestration_mode = "director" if config.enabled else "single"
                record.director_config = config.to_dict()
                record.director_run_id = ""
                if config.enabled:
                    record.backend_type = config.director_backend_type
                    record.model = config.director_model
                record.save()
                try:
                    backend_type = record.backend_type or getattr(state.backend, "name", "")
                    model = record.model or getattr(state.backend, "model", "")
                    if backend_type and model:
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: state.create_backend(
                                backend_type,
                                model,
                                session_mode="code",
                                session_role=record.session_role or "generator",
                            ),
                        )
                        state.session.conversation_history = old_history
                    record.conversation_history = old_history
                    record.save()
                    await ws.send_json({
                        "event": "director.configured",
                        "mode": record.orchestration_mode,
                        "config": record.director_config,
                        "run": (
                            state.session.director_run.to_dict()
                            if state.session and state.session.director_run else None
                        ),
                    })
                    await ws.send_json(state.get_init_data(refresh_only=True))
                except Exception as exc:
                    logger.exception("Director Mode configuration failed")
                    await ws.send_json({
                        "event": "director.configure_failed",
                        "message": f"Director Mode configuration failed: {exc}",
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
                    # Replay the saved conversation immediately. Backend startup
                    # can take seconds (or fail when a provider is offline), but
                    # neither condition should prevent users from opening and
                    # reading an existing session.
                    await ws.send_json({
                        "event": "session_loaded",
                        "session_id": record.id,
                        "title": record.title,
                        "backend_type": record.backend_type,
                        "model": record.model,
                        "message_count": record.message_count,
                        "session_mode": "code",
                        "session_role": record.session_role or "generator",
                        "orchestration_mode": record.orchestration_mode,
                        "director_config": record.director_config,
                        "director_run_id": record.director_run_id,
                        "display_events": record.display_events,
                        "sessions": state.project.list_sessions(),
                        "current_session_id": record.id,
                        "runtime_pending": bool(record.backend_type),
                    })

                    # Recreate backend + session with saved conversation history.
                    # The websocket remains serialized, so a prompt submitted
                    # during this short window is processed only after runtime
                    # setup completes.
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

                    except Exception as e:
                        state.backend = None
                        state.backend_spec = None
                        state.session = None
                        await ws.send_json({
                            "event": "status_msg",
                            "message": f"Session loaded. Its runtime is unavailable: {e}",
                        })
                    # Send lightweight init refresh (skip re-detecting backends)
                    # even when runtime setup fails so navigation stays coherent.
                    await ws.send_json(state.get_init_data(refresh_only=True))
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
                project_switch_id = str(msg.get("project_switch_id", "")).strip()
                if not project_path:
                    await ws.send_json({
                        "event": "error",
                        "message": "Project path is required.",
                        "project_switch_id": project_switch_id,
                    })
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
                    init_data = state.get_init_data()
                    init_data["project_switch_id"] = project_switch_id
                    await ws.send_json(init_data)
                    await ws.send_json({
                        "event": "ui_notice",
                        "message": f"Project ready: {resolved_project_path}",
                        "project_switch_id": project_switch_id,
                    })
                except Exception as exc:
                    logger.warning("set_project failed for %r: %s", project_path, exc)
                    await ws.send_json({
                        "event": "error",
                        "message": f"Couldn't open project folder: {exc}",
                        "project_switch_id": project_switch_id,
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
                await ws.send_json({
                    "event": "user_input_received",
                    "response": state.user_input_result[0],
                })

            # ── Settings ────────────────────────────────────
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
    finally:
        pending_chat_messages.clear()
        if chat_runner is not None and not chat_runner.done():
            state.cancel_requested.set()
            if state.session:
                state.session.cancel()
            chat_runner.cancel()


_STREAM_DELTA_COALESCE_SECONDS = 0.012
_STREAM_DELTA_MAX_CHARS = 16_384
_COALESCIBLE_STREAM_EVENTS = frozenset({"text.delta", "thinking.delta"})


def _get_coalesced_stream_event(
    event_queue: queue.Queue,
    deferred_events: deque,
    *,
    timeout: float = 0.5,
) -> Any:
    """Read one event, combining adjacent token deltas into a small frame.

    Local models can emit hundreds of tiny chunks per second. Sending every
    chunk through an executor hop, JSON encoder, WebSocket frame, and DOM event
    adds substantial overhead without improving perceived latency. A 12ms
    window keeps streaming responsive while preserving strict event order.
    """
    if deferred_events:
        event = deferred_events.popleft()
    else:
        event = event_queue.get(timeout=timeout)

    if not isinstance(event, dict) or event.get("event") not in _COALESCIBLE_STREAM_EVENTS:
        return event

    event_type = event.get("event")
    combined = dict(event)
    parts = [str(event.get("delta") or "")]
    character_count = len(parts[0])
    deadline = time.monotonic() + _STREAM_DELTA_COALESCE_SECONDS

    while character_count < _STREAM_DELTA_MAX_CHARS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            next_event = event_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if not isinstance(next_event, dict) or next_event.get("event") != event_type:
            deferred_events.append(next_event)
            break
        delta = str(next_event.get("delta") or "")
        parts.append(delta)
        character_count += len(delta)

    combined["delta"] = "".join(parts)
    return combined


async def _run_session_streaming(
    ws: WebSocket,
    session: Session,
    user_msg: str,
    images=None,
    *,
    display_user_msg: str | None = None,
    session_mode: str = "code",
    session_role: str = "generator",
    event_source: Callable[[], Any] | None = None,
):
    """Run Session.run() in a thread, streaming events to WebSocket.

    Returns a list of display events for session persistence/replay.

    `event_source` substitutes a different engine generator for the usual
    `session.run(...)` — used by the agent-restart path, which re-dispatches a
    worker instead of taking a user turn. Everything downstream (streaming,
    persistence, cancellation, replay) is identical, so a restarted worker is
    observable and stoppable exactly like any other run.
    """
    event_queue: queue.Queue = queue.Queue()
    display_events: list = []
    pending_harness_payload: dict[str, Any] | None = None
    pending_harness_text: str = ""
    harness_parse_error: str | None = None

    # Record the user message as a display event
    display_events.append({"event": "user_message", "text": display_user_msg or user_msg})
    persisted_events = list(
        getattr(state.project.current_session, "display_events", []) or []
    )
    session.checkpoint_display_provider = lambda: persisted_events + list(display_events)

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
            source = event_source() if event_source is not None else session.run(
                user_msg,
                on_permission=on_permission if not session.auto_approve else None,
                on_choice=on_choice,
                on_user_input=on_user_input,
                images=images,
            )
            for event in source:
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

    deferred_events: deque = deque()

    def _get_event():
        while True:
            try:
                return _get_coalesced_stream_event(
                    event_queue,
                    deferred_events,
                    timeout=0.5,
                )
            except queue.Empty:
                continue

    loop = asyncio.get_event_loop()
    try:
        while True:
            event = await loop.run_in_executor(None, _get_event)
            if event is None:
                break
            session._log_event(event)
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
                cleaned_text, harness_payload, parse_error = state.harness_prompts.extract_harness_update(
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
                stats = event.get("stats") or {}
                model = event.get("model", "")
                in_tok = stats.get("input_tokens", 0)
                out_tok = stats.get("output_tokens", 0)
                if (in_tok or out_tok) and state.settings.get("cost_tracking", "enabled", True):
                    cost = state.costs.record_usage(
                        model,
                        in_tok,
                        out_tok,
                        stats.get("cached_tokens", 0),
                    )
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

            if event_type == EngineEvent.SESSION_END.value and event.get("telemetry"):
                try:
                    state.evaluations.record_turn_telemetry(event["telemetry"])
                except Exception:
                    logger.debug("turn telemetry persistence failed", exc_info=True)

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
                status_message = state.harness_prompts.apply_harness_update(
                    session_mode=session_mode,
                    session_role=session_role,
                    payload=pending_harness_payload,
                    project_path=state.project.project_path,
                    assistant_text=pending_harness_text,
                    user_request=display_user_msg or user_msg,
                )
                await ws.send_json({"event": "harness_state", "data": state.harness_prompts.get_harness_summary()})
                if status_message:
                    await ws.send_json({"event": "status_msg", "message": status_message})
            except Exception as exc:
                await ws.send_json({"event": "error", "message": f"Failed to apply harness update: {exc}"})
    finally:
        state.active_thread = None
        session.checkpoint_display_provider = lambda: list(
            getattr(state.project.current_session, "display_events", []) or []
        )

    return display_events


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
