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
from pathlib import Path
from datetime import date
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..events import EngineEvent, make_event
from ..backends import (
    ClaudeBackend, OpenAIBackend,
    ClaudeCodeBackend, CodexBackend, _find_cli,
)
from ..engine import Session
from .sessions import ProjectManager
from .settings import SettingsManager
from .costs import CostTracker
from .project_instructions import load_project_instructions, get_instruction_info
from .task_runner import TaskRunner
from .scheduler import Scheduler
from .runtime import BackendSpec
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
        self._scheduler_started = False
        # Extension systems
        self.hook_runner = HookRunner(self.settings)
        self.mcp_manager = MCPManager(self.settings)
        self.base_engram = EngramIntegration(self.settings)
        self.base_engram.set_mcp_manager(self.mcp_manager)
        self.engram = self.base_engram.clone(namespace=self._project_namespace(self.project.project_path))
        self.codebase_index: Optional[CodebaseIndex] = None
        self.scheduler.set_backend_factory(lambda _task: self.make_background_session)
        self.apply_project_context(self.project.project_path, refresh_index=True)

    @staticmethod
    def _normalize_path(project_path: str) -> str:
        return os.path.normpath(project_path).replace("\\", "/").lower()

    def _project_namespace(self, project_path: str) -> str:
        normalized = os.path.normpath(project_path).replace("\\", "/").lower()
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"project:{digest}"

    def _session_auto_approve(self, mode: Optional[str] = None) -> bool:
        return (mode or self.permission_mode) != "ask"

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
        engram: Optional[EngramIntegration] = None,
        codebase_index: Optional[CodebaseIndex] = None,
    ) -> Session:
        target_path = project_path or self.project.project_path
        session.project_instructions = load_project_instructions(target_path)
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

        self.available_backends = available
        return available

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
    ) -> Session:
        project_path = os.path.normpath(project_path or self.project.project_path)
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

        session = Session(
            backend=backend,
            max_steps=self.SESSION_MAX_STEPS,
            max_tokens=self.SESSION_MAX_TOKENS,
            auto_approve=self._session_auto_approve() if auto_approve is None else auto_approve,
            project_instructions=project_instructions,
            cancel_event=cancel_event,
        )
        return self._wire_session(
            session,
            project_path=project_path,
            engram=engram,
            codebase_index=codebase_index,
        )

    def create_backend(self, backend_type: str, model: str = None):
        """Create a backend and session."""
        spec = self.build_backend_spec(backend_type, model=model, project_path=self.project.project_path)
        self.backend = spec.create_backend(self.settings)
        self.backend_spec = spec
        self._project_instructions = load_project_instructions(self.project.project_path)
        self.session = self.build_session(
            backend=self.backend,
            backend_spec=spec,
            project_path=self.project.project_path,
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
            self.backend_spec.backend_type in {"claude", "openai", "lmstudio"} and
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
            "recent_projects": self.project.get_recent_projects(),
            "settings": self.settings.get_masked(),
            "resonant_md": get_instruction_info(self.project.project_path),
            "rag": self.codebase_index.get_stats() if self.codebase_index else {"total_files": 0, "is_indexed": False},
            "chat_groups": self.project.list_chat_groups(),
        }


state = AppState()


# ── WebSocket Handler ─────────────────────────────────────────────────

async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket handler — bidirectional communication with frontend."""
    await ws.accept()

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

            elif command == "select_backend":
                backend_type = msg.get("backend", "")
                model = msg.get("model", "")
                session_mode = msg.get("session_mode", "code")
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, state.create_backend, backend_type, model or None
                    )
                    # Auto-create a new session when backend is selected
                    state.project.create_session(
                        backend_type=backend_type,
                        model=model or getattr(state.backend, "model", ""),
                        session_mode=session_mode,
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
                    state._first_message_sent = True

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

                state.cancel_requested.clear()
                state.session.reset_cancel()
                display_events = await _run_session_streaming(ws, state.session, text, images=images)

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
                if state.backend:
                    backend_type = getattr(state.backend, "name", "")
                    model = getattr(state.backend, "model", "")
                    state.session = state.build_session(
                        backend=state.backend,
                        backend_spec=state.backend_spec,
                        project_path=state.project.project_path,
                    )
                    state.project.create_session(backend_type=backend_type, model=model, session_mode=session_mode)
                    state._first_message_sent = False
                    state.costs.reset_session()
                await ws.send_json({
                    "event": "session_cleared",
                    "sessions": state.project.list_sessions(),
                    "current_session_id": state.project.current_session.id if state.project.current_session else "",
                })

            elif command == "switch_model":
                model = msg.get("model", "")
                backend_type = msg.get("backend", "")
                if not backend_type and state.backend and hasattr(state.backend, "name"):
                    backend_type = getattr(state.backend, "name", "")
                if backend_type:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, state.create_backend, backend_type, model
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
                                None, state.create_backend, backend_type, model or None
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

            # ── Scheduled Tasks ──────────────────────────────
            elif command == "schedule_create":
                name = msg.get("name", "").strip()
                prompt = msg.get("prompt", "").strip()
                schedule = msg.get("schedule", "").strip()
                backend_type = msg.get("backend", getattr(state.backend, "name", ""))
                model = msg.get("model", getattr(state.backend, "model", ""))
                if not prompt or not schedule:
                    await ws.send_json({"event": "error", "message": "Prompt and schedule are required"})
                    continue
                try:
                    spec = state.build_backend_spec(
                        backend_type,
                        model=model or None,
                        project_path=state.project.project_path,
                    )
                    sched = state.scheduler.add(
                        name=name,
                        prompt=prompt,
                        schedule=schedule,
                        backend_type=backend_type,
                        model=spec.model,
                        backend_spec=spec.to_dict(),
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
                for key in ("name", "prompt", "schedule", "backend_type", "model", "enabled"):
                    if key in msg:
                        updates[key] = msg[key]
                if "backend_type" in updates or "model" in updates:
                    schedules = state.scheduler.list_schedules()
                    existing = next((item for item in schedules if item["id"] == task_id), None)
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


async def _run_session_streaming(ws: WebSocket, session: Session, user_msg: str, images=None):
    """Run Session.run() in a thread, streaming events to WebSocket.

    Returns a list of display events for session persistence/replay.
    """
    event_queue: queue.Queue = queue.Queue()
    display_events: list = []

    # Record the user message as a display event
    display_events.append({"event": "user_message", "text": user_msg})

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
