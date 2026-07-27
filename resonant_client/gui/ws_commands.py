"""Registry of self-contained WebSocket command handlers.

`websocket_endpoint` in `gui/app.py` grew into a ~2,600-line function whose
dispatch was a single ~80-branch `elif command == "..."` chain. Every branch —
including the many that just read some state and send one JSON reply — shared
the endpoint's entire local scope, so none of them could be read, tested, or
changed in isolation.

This module holds the handlers that genuinely need nothing from that scope: a
request comes in, state is read, a reply goes out. They receive an explicit
`CommandContext` instead of closing over the endpoint's locals, which makes
them directly unit-testable with a stub socket.

Handlers still in `app.py` are the ones entangled with the run loop — they
start/stop the chat runner, rebuild the backend, swap the active session, or
drive the autonomous daemon. Those need real untangling, not relocation, and
moving them mechanically would only hide the coupling behind an indirection.

To add a handler here it must:

  * read only `ctx` (socket, app state, message, chat runner),
  * not mutate loop state in `websocket_endpoint`, and
  * finish in one request/response exchange.

Anything else belongs in the endpoint until the coupling is designed away.

A note on what "self-contained" turned out to mean: the first pass here moved
22 handlers and left the rest, described as run-loop-coupled. Re-measuring
later showed that assessment was too conservative — most of the remainder only
touched `state`, `msg`, and `ws`. The honest test is whether a handler needs
the endpoint's *locals* (chat_runner, the session/backend rebuild dance), not
whether it happens to mutate application state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..engine.session import inspect_system_instructions
from ..processes import background_process_kwargs
from .autonomous_session import (
    build_roadmap_inspector_payload as _build_roadmap_inspector_payload,
    find_orphaned_autonomous_missions as _find_orphaned_autonomous_missions,
    get_autonomous_daemon as _get_autonomous_daemon,
    list_autonomous_missions as _list_autonomous_missions,
    stop_autonomous_mission as _stop_autonomous_mission,
)
from .project_instructions import find_instruction_file
from .settings import SettingsManager

logger = logging.getLogger(__name__)

# Steering text for a mid-run status request. Lives here rather than in app.py
# because the handler that sends it does; app.py re-exports it.
STATUS_UPDATE_STEER = (
    "At the next safe agent boundary, give the user a concise progress update "
    "covering: what is complete, what you are doing now, what remains, and any "
    "real blocker. Then continue the original task without stopping or asking "
    "for confirmation unless you are genuinely blocked. Do not restart work "
    "or repeat completed steps."
)


@dataclass(slots=True)
class CommandContext:
    """Everything a self-contained command handler is allowed to touch."""

    ws: Any
    state: Any
    msg: dict[str, Any]
    # The connection's chat-turn state (gui/chat_loop.ChatRunLoop). Previously
    # a bare asyncio.Task copied out of the endpoint's scope, which meant a
    # handler could inspect the in-flight turn but never act on it. Handlers
    # may ask whether one is running and queue work; starting a turn outside
    # the queue stays with the endpoint.
    runs: Any = None

    async def send(self, payload: dict[str, Any]) -> None:
        await self.ws.send_json(payload)

    async def send_error(self, message: str) -> None:
        await self.ws.send_json({"event": "error", "message": message})

    @property
    def project_path(self) -> str:
        return self.state.project.project_path

    def session_attr(self, name: str, default: Any = None) -> Any:
        """Fetch an attribute off the live session, tolerating no session."""
        session = getattr(self.state, "session", None)
        if session is None:
            return default
        return getattr(session, name, default)


Handler = Callable[[CommandContext], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        if name in HANDLERS:
            raise RuntimeError(f"Duplicate WebSocket command handler: {name}")
        HANDLERS[name] = func
        return func

    return register


async def _in_executor(func, *args):
    return await asyncio.get_event_loop().run_in_executor(None, func, *args)


# ---------------------------------------------------------------------------
# Model / prompt / context inspection
# ---------------------------------------------------------------------------


@command("get_model_telemetry")
async def _get_model_telemetry(ctx: CommandContext) -> None:
    # Best-effort runtime info about the loaded Ollama model
    # (context_length, memory, supports_thinking).
    backend = ctx.state.backend
    if backend and getattr(backend, "name", "") == "ollama" and hasattr(backend, "get_runtime_telemetry"):
        data = await _in_executor(lambda: backend.get_runtime_telemetry(timeout=4.0))
        await ctx.send({"event": "model_telemetry", "data": data})
    else:
        await ctx.send({"event": "model_telemetry", "data": {"error": "no Ollama backend"}})


@command("get_prompt_inspector")
async def _get_prompt_inspector(ctx: CommandContext) -> None:
    state = ctx.state
    session = state.session
    model_name = (
        getattr(state.backend, "model", "")
        or getattr(state.backend_spec, "model", "")
        or state.settings.get("general", "default_model", "")
    )
    data = inspect_system_instructions(
        plan_mode=bool(getattr(session, "plan_mode", False)),
        project_instructions=getattr(session, "project_instructions", None),
        working_directory=ctx.project_path,
        model_name=model_name,
        prompt_role=getattr(session, "prompt_role", "primary"),
        role_instructions=getattr(session, "role_instructions", None),
    )
    await ctx.send({"event": "prompt_inspector", "data": data})


_EMPTY_CONTEXT_STATE = {
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


@command("get_context_state")
async def _get_context_state(ctx: CommandContext) -> None:
    session = ctx.state.session
    data = session.context_snapshot() if session else dict(_EMPTY_CONTEXT_STATE)
    await ctx.send({"event": "context.state", **data})


# ---------------------------------------------------------------------------
# Agent runtime
# ---------------------------------------------------------------------------


@command("agent_runtime_list")
async def _agent_runtime_list(ctx: CommandContext) -> None:
    registry = ctx.session_attr("agent_registry")
    agents = [record.to_dict() for record in registry.list()] if registry else []
    await ctx.send({"event": "agent.runtime_list", "agents": agents})


@command("agent_runtime_detail")
async def _agent_runtime_detail(ctx: CommandContext) -> None:
    registry = ctx.session_attr("agent_registry")
    agent_id = str(ctx.msg.get("agent_id") or "")
    record = registry.get(agent_id) if registry else None
    await ctx.send({
        "event": "agent.runtime_detail",
        "agent": record.to_dict() if record else None,
        "transcript": registry.transcript(agent_id) if record else [],
    })


@command("agent_runtime_control")
async def _agent_runtime_control(ctx: CommandContext) -> None:
    registry = ctx.session_attr("agent_registry")
    agent_id = str(ctx.msg.get("agent_id") or "")
    action = str(ctx.msg.get("action") or "")
    if not registry or not registry.get(agent_id):
        await ctx.send_error("Agent is no longer available")
        return
    try:
        if action == "pause":
            record = registry.request_pause(agent_id)
        elif action == "resume":
            # Rejects terminal agents — see AgentRegistry.resume. A restart-
            # orphaned worker has no thread to un-pause.
            record = registry.resume(agent_id)
        elif action == "cancel":
            record = registry.request_cancel(agent_id)
        elif action == "steer":
            record = registry.steer(agent_id, str(ctx.msg.get("text") or ""))
        else:
            raise ValueError(f"Unknown agent action: {action}")
        await ctx.send({"event": "agent.control_ack", "agent": record.to_dict(), "action": action})
        await ctx.send({"event": "agent.runtime_list", "agents": [item.to_dict() for item in registry.list()]})
    except (KeyError, ValueError) as exc:
        await ctx.send_error(str(exc))


@command("director_status")
async def _director_status(ctx: CommandContext) -> None:
    session = ctx.state.session
    run = getattr(session, "director_run", None) if session else None
    benchmark_store = getattr(session, "benchmark_store", None) if session else None
    await ctx.send({
        "event": "director.status",
        "run": run.to_dict() if run else None,
        "benchmark": benchmark_store.comparison() if benchmark_store else None,
    })


# ---------------------------------------------------------------------------
# Session checkpoint timeline
# ---------------------------------------------------------------------------


@command("session_timeline_list")
async def _session_timeline_list(ctx: CommandContext) -> None:
    store = ctx.session_attr("checkpoint_store")
    values = [item.to_dict() for item in store.list()] if store else []
    await ctx.send({"event": "session.timeline_list", "checkpoints": values})


@command("session_timeline_compare")
async def _session_timeline_compare(ctx: CommandContext) -> None:
    try:
        store = ctx.session_attr("checkpoint_store")
        data = await _in_executor(store.compare, str(ctx.msg.get("checkpoint_id") or ""))
        await ctx.send({"event": "session.timeline_comparison", "data": data})
    except Exception as exc:
        await ctx.send_error(str(exc))


@command("session_timeline_restore")
async def _session_timeline_restore(ctx: CommandContext) -> None:
    state = ctx.state
    try:
        if ctx.runs is not None and ctx.runs.busy:
            raise RuntimeError("Stop the active run before restoring a checkpoint")
        store = ctx.session_attr("checkpoint_store")
        checkpoint_id = str(ctx.msg.get("checkpoint_id") or "")
        mode = str(ctx.msg.get("mode") or "both")
        data = await _in_executor(store.restore, checkpoint_id, mode)
        if mode in {"conversation", "both"}:
            state.session.conversation_history = data.get("conversation_history") or []
            if state.project.current_session:
                state.project.current_session.conversation_history = list(state.session.conversation_history)
                state.project.current_session.display_events = list(data.get("display_events") or [])
                state.project.current_session.save()
        if state.session.hook_runner:
            from ..engine.hooks import HookType
            state.session.hook_runner.emit(
                HookType.CHECKPOINT_RESTORED,
                {"checkpoint_id": checkpoint_id, "mode": mode, "project_path": ctx.project_path},
            )
        await ctx.send({
            "event": "session.timeline_restored",
            "data": data,
            "display_events": data.get("display_events") or [],
        })
    except Exception as exc:
        await ctx.send_error(str(exc))


# ---------------------------------------------------------------------------
# Flight recorder
# ---------------------------------------------------------------------------


@command("flight_recorder_list")
async def _flight_recorder_list(ctx: CommandContext) -> None:
    from ..engine.flight_recorder import FlightRecorder
    await ctx.send({
        "event": "flight.recorder_list",
        "runs": FlightRecorder.list_runs(ctx.project_path),
    })


@command("flight_recorder_detail")
async def _flight_recorder_detail(ctx: CommandContext) -> None:
    try:
        from ..engine.flight_recorder import FlightRecorder
        recorder = FlightRecorder.open_run(ctx.project_path, str(ctx.msg.get("run_id") or ""))
        await ctx.send({
            "event": "flight.recorder_detail",
            "manifest": recorder.manifest.to_dict(),
            "events": recorder.events(),
        })
    except Exception as exc:
        await ctx.send_error(str(exc))


@command("flight_recorder_compare")
async def _flight_recorder_compare(ctx: CommandContext) -> None:
    try:
        from ..engine.flight_recorder import FlightRecorder
        left = FlightRecorder.open_run(ctx.project_path, str(ctx.msg.get("left") or ""))
        right = FlightRecorder.open_run(ctx.project_path, str(ctx.msg.get("right") or ""))
        await ctx.send({"event": "flight.recorder_comparison", "data": FlightRecorder.compare(left, right)})
    except Exception as exc:
        await ctx.send_error(str(exc))


@command("flight_recorder_export")
async def _flight_recorder_export(ctx: CommandContext) -> None:
    try:
        from ..engine.flight_recorder import FlightRecorder
        recorder = FlightRecorder.open_run(ctx.project_path, str(ctx.msg.get("run_id") or ""))
        artifact = ctx.state.session.artifact_store.put_text(
            json.dumps(recorder.export_otel(), indent=2),
            kind="trace", label=f"{recorder.run_id} OTLP export", source=recorder.run_id,
            media_type="application/json",
        )
        await ctx.send({"event": "artifact.created", "artifact": artifact.to_dict()})
    except Exception as exc:
        await ctx.send_error(str(exc))


# ---------------------------------------------------------------------------
# Artifacts, capability packs, context providers
# ---------------------------------------------------------------------------


@command("artifact_list")
async def _artifact_list(ctx: CommandContext) -> None:
    store = ctx.session_attr("artifact_store")
    await ctx.send({
        "event": "artifact.list",
        "artifacts": [item.to_dict() for item in reversed(store.list())] if store else [],
    })


@command("capability_pack_list")
async def _capability_pack_list(ctx: CommandContext) -> None:
    manager = ctx.session_attr("capability_packs")
    await ctx.send({
        "event": "capability.pack_list",
        "packs": [item.to_dict() for item in manager.discover()] if manager else [],
        "catalog": manager.context_catalog() if manager else {},
    })


@command("context_catalog")
async def _context_catalog(ctx: CommandContext) -> None:
    broker = ctx.session_attr("context_broker")
    await ctx.send({
        "event": "context.catalog",
        "providers": broker.catalog() if broker else [],
    })


# ---------------------------------------------------------------------------
# Iteration checkpoints
# ---------------------------------------------------------------------------


@command("checkpoint_list")
async def _checkpoint_list(ctx: CommandContext) -> None:
    try:
        from ..orchestration.checkpoints import IterationCheckpointStore
        store = IterationCheckpointStore(ctx.project_path)
        await ctx.send({"event": "checkpoint_list", "checkpoints": store.list()})
    except Exception as exc:
        await ctx.send({"event": "checkpoint_list", "checkpoints": [], "error": str(exc)})


@command("checkpoint_compare")
async def _checkpoint_compare(ctx: CommandContext) -> None:
    try:
        from ..orchestration.checkpoints import IterationCheckpointStore
        store = IterationCheckpointStore(ctx.project_path)
        data = await _in_executor(store.compare, str(ctx.msg.get("ref") or ""))
        await ctx.send({"event": "checkpoint_comparison", "data": data})
    except Exception as exc:
        await ctx.send_error(str(exc))


@command("checkpoint_restore")
async def _checkpoint_restore(ctx: CommandContext) -> None:
    try:
        if ctx.state.active_thread and ctx.state.active_thread.is_alive():
            raise RuntimeError("Stop the active agent before restoring a checkpoint")
        from ..orchestration.checkpoints import IterationCheckpointStore
        store = IterationCheckpointStore(ctx.project_path)
        data = await _in_executor(store.restore, str(ctx.msg.get("ref") or ""))
        await ctx.send({"event": "checkpoint_restored", "data": data})
    except Exception as exc:
        await ctx.send_error(str(exc))


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


@command("evaluation_list")
async def _evaluation_list(ctx: CommandContext) -> None:
    await ctx.send({"event": "evaluation_dashboard", "data": ctx.state.evaluations.snapshot()})


@command("evaluation_start")
async def _evaluation_start(ctx: CommandContext) -> None:
    msg = ctx.msg
    try:
        record = ctx.state.evaluations.start(
            model_label=str(msg.get("model") or "glm"),
            spec_name=str(msg.get("spec") or "minimal"),
            n=int(msg.get("n") or 1),
            timeout_minutes=int(msg.get("timeout_minutes") or 25),
            project_path=ctx.project_path,
        )
        await ctx.send({"event": "evaluation_started", "record": record})
    except (TypeError, ValueError, RuntimeError) as exc2:
        await ctx.send_error(str(exc2))


# ---------------------------------------------------------------------------
# Settings, costs, permissions
# ---------------------------------------------------------------------------


@command("get_settings")
async def _get_settings(ctx: CommandContext) -> None:
    await ctx.send({"event": "settings", "data": ctx.state.settings.get_masked()})


@command("get_costs")
async def _get_costs(ctx: CommandContext) -> None:
    await ctx.send({"event": "costs", "data": ctx.state.costs.get_all_costs()})


@command("set_permission_mode")
async def _set_permission_mode(ctx: CommandContext) -> None:
    ctx.state.apply_permission_mode(ctx.msg.get("mode", "bypass"))


@command("get_harness_state")
async def _get_harness_state(ctx: CommandContext) -> None:
    await ctx.send({"event": "harness_state", "data": ctx.state.get_harness_summary()})


@command("harness_cycle_list")
async def _harness_cycle_list(ctx: CommandContext) -> None:
    await ctx.send({
        "event": "harness_cycle_list",
        "runs": ctx.state.harness_orchestrator.list_runs(),
    })


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


@command("git_status")
async def _git_status_command(ctx: CommandContext) -> None:
    result = await _in_executor(_git_status, ctx.project_path)
    await ctx.send({"event": "git_status", "data": result})


@command("git_quick")
async def _git_quick_command(ctx: CommandContext) -> None:
    action = ctx.msg.get("action", "")
    result = await _in_executor(_git_quick, action, ctx.msg, ctx.project_path)
    await ctx.send({"event": "git_result", "action": action, "data": result})


# ---------------------------------------------------------------------------
# Project instructions (RESONANT.md / AGENTS.md)
# ---------------------------------------------------------------------------


@command("get_resonant_md")
async def _get_resonant_md(ctx: CommandContext) -> None:
    from .project_instructions import get_instruction_info, load_project_instructions

    await ctx.send({
        "event": "resonant_md",
        "info": get_instruction_info(ctx.project_path),
        "content": load_project_instructions(ctx.project_path) or "",
    })


@command("save_resonant_md")
async def _save_resonant_md_command(ctx: CommandContext) -> None:
    from .project_instructions import get_instruction_info

    content = ctx.msg.get("content", "")
    _save_resonant_md(ctx.project_path, content)
    ctx.state._project_instructions = content if content.strip() else None
    if ctx.state.session:
        ctx.state.session.project_instructions = ctx.state._project_instructions
    await ctx.send({
        "event": "resonant_md",
        "info": get_instruction_info(ctx.project_path),
        "content": content,
    })


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@command("mcp_list")
async def _mcp_list(ctx: CommandContext) -> None:
    await ctx.send({
        "event": "mcp_list",
        "servers": ctx.state.mcp_manager.list_servers(),
        "health": ctx.state.mcp_manager.health_check(),
    })


@command("mcp_connect")
async def _mcp_connect(ctx: CommandContext) -> None:
    server_name = ctx.msg.get("name", "")
    if not server_name:
        return
    success = await _in_executor(ctx.state.mcp_manager.connect, server_name)
    # New tools change the session's tool surface and invalidate the cached
    # intent service, which captured the old one.
    if ctx.state.session:
        ctx.state.session.mcp_tools = ctx.state.mcp_manager.get_all_tools()
    ctx.state._intent_service = None
    await ctx.send({
        "event": "mcp_list",
        "servers": ctx.state.mcp_manager.list_servers(),
        "connected": success,
    })


@command("mcp_disconnect")
async def _mcp_disconnect(ctx: CommandContext) -> None:
    server_name = ctx.msg.get("name", "")
    if not server_name:
        return
    ctx.state.mcp_manager.disconnect(server_name)
    if ctx.state.session:
        ctx.state.session.mcp_tools = ctx.state.mcp_manager.get_all_tools()
    ctx.state._intent_service = None
    await ctx.send({"event": "mcp_list", "servers": ctx.state.mcp_manager.list_servers()})


# ---------------------------------------------------------------------------
# LSP / plugin inventory
# ---------------------------------------------------------------------------


@command("lsp_list")
async def _lsp_list(ctx: CommandContext) -> None:
    await ctx.send(_lsp_list_payload(
        project_path=ctx.project_path,
        settings=ctx.state.settings,
    ))


@command("plugin_list")
async def _plugin_list(ctx: CommandContext) -> None:
    await ctx.send(_plugin_list_payload(settings=ctx.state.settings))


# ---------------------------------------------------------------------------
# Engram memory
# ---------------------------------------------------------------------------


@command("engram_status")
async def _engram_status(ctx: CommandContext) -> None:
    engram = ctx.state.engram
    await ctx.send({
        "event": "engram_status",
        "enabled": engram.enabled,
        "server_url": engram._server_url,
        "namespace": engram._namespace,
        "has_mcp": engram._mcp_manager is not None,
    })


@command("engram_recall")
async def _engram_recall(ctx: CommandContext) -> None:
    query = ctx.msg.get("query", "")
    engram = ctx.state.engram
    if query and engram.enabled:
        memories = await _in_executor(engram.recall, query)
        await ctx.send({"event": "engram_recall", "memories": memories})
    else:
        await ctx.send({"event": "engram_recall", "memories": [], "enabled": engram.enabled})


@command("engram_remember")
async def _engram_remember(ctx: CommandContext) -> None:
    text = ctx.msg.get("text", "")
    engram = ctx.state.engram
    if text and engram.enabled:
        await _in_executor(engram.remember, text)
        await ctx.send({"event": "engram_remembered", "ok": True})


# ---------------------------------------------------------------------------
# RAG / codebase index
# ---------------------------------------------------------------------------


@command("rag_stats")
async def _rag_stats(ctx: CommandContext) -> None:
    index = ctx.state.codebase_index
    if index:
        await ctx.send({"event": "rag_stats", **index.get_stats()})
    else:
        await ctx.send({"event": "rag_stats", "total_files": 0, "is_indexed": False})


@command("rag_index")
async def _rag_index(ctx: CommandContext) -> None:
    from ..engine.rag import CodebaseIndex

    if not ctx.state.codebase_index:
        project_path = ctx.project_path if ctx.state.project else os.getcwd()
        ctx.state.codebase_index = CodebaseIndex(project_path, engram=ctx.state.engram)
    stats = await _in_executor(ctx.state.codebase_index.index, ctx.msg.get("force", False))
    await ctx.send({"event": "rag_indexed", **stats})


@command("rag_search")
async def _rag_search(ctx: CommandContext) -> None:
    query = ctx.msg.get("query", "")
    index = ctx.state.codebase_index
    if query and index:
        await ctx.send({
            "event": "rag_results",
            "results": [item.to_dict() for item in index.search(query)],
        })
    else:
        await ctx.send({"event": "rag_results", "results": []})


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@command("skill_list")
async def _skill_list(ctx: CommandContext) -> None:
    await ctx.send(_skill_list_payload(
        project_path=ctx.project_path if ctx.state.project else "",
        include_deprecated=bool(ctx.msg.get("include_deprecated", False)),
    ))


@command("skill_view")
async def _skill_view(ctx: CommandContext) -> None:
    skill_id = (ctx.msg.get("skill_id") or "").strip()
    project_path = ctx.project_path if ctx.state.project else ""
    await ctx.send(_skill_view_payload(skill_id, project_path=project_path))


@command("skill_pin_toggle")
async def _skill_pin_toggle(ctx: CommandContext) -> None:
    from ..orchestration.skills import load_skill, set_pinned

    skill_id = (ctx.msg.get("skill_id") or "").strip()
    project_path = ctx.project_path if ctx.state.project else ""
    try:
        skill = load_skill(skill_id, project_path=project_path)
        if skill is None:
            await ctx.send({"event": "skill_error", "message": f"skill {skill_id!r} not found"})
            return
        new_pinned = not bool(skill.pinned)
        set_pinned(skill_id, new_pinned, project_path=project_path)
        await ctx.send({
            "event": "skill_pin_changed", "skill_id": skill_id, "pinned": new_pinned,
        })
        await ctx.send(_skill_list_payload(project_path=project_path))
    except Exception as exc:
        await ctx.send({"event": "skill_error", "message": f"pin toggle failed: {exc}"})


# ---------------------------------------------------------------------------
# Session replay
# ---------------------------------------------------------------------------


@command("get_session_replay_events")
async def _get_session_replay_events(ctx: CommandContext) -> None:
    """Fetch a session's display events without switching the active one."""
    from .sessions import _sessions_dir

    target_id = ctx.msg.get("session_id", "")
    project_path = ctx.msg.get("project_path") or ctx.project_path

    path = _sessions_dir(project_path) / f"{target_id}.json"
    if not path.exists():
        # The session may belong to a different recent project.
        for project in ctx.state.project.get_recent_projects():
            candidate_root = project.get("path", "") if isinstance(project, dict) else str(project)
            if not candidate_root:
                continue
            candidate = _sessions_dir(candidate_root) / f"{target_id}.json"
            if candidate.exists():
                path = candidate
                break

    if not path.exists():
        await ctx.send({
            "event": "session_replay_events", "session_id": target_id,
            "error": "not found", "events": [],
        })
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        await ctx.send({
            "event": "session_replay_events",
            "session_id": target_id,
            "title": data.get("title") or "",
            "events": data.get("display_events") or [],
        })
    except Exception as exc:
        await ctx.send({
            "event": "session_replay_events", "session_id": target_id,
            "error": str(exc), "events": [],
        })


# ---------------------------------------------------------------------------
# Relocated endpoint commands
#
# Moved once ChatRunLoop gave the endpoint's private chat state a name. The
# bodies are unchanged apart from mechanical renaming: ws.send_json becomes
# ctx.send, the endpoint locals become ctx.state / ctx.msg / ctx.runs, and a
# dispatch-level "continue" becomes "return". A continue or break inside a loop
# in a body kept its own meaning.
#
# Some of these mutate application state — swapping the session, rebuilding the
# backend, driving the autonomous daemon. That was never what made them
# unmovable; they were held here by needing a variable that only code inside
# websocket_endpoint could see.
# ---------------------------------------------------------------------------


@command("init")
async def _cmd_init(ctx: CommandContext) -> None:
    if not ctx.state.backend and ctx.state.available_backends:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ctx.state.ensure_default_runtime_session(),
            )
        except Exception:
            logger.exception("default runtime session init failed")
    await ctx.send(ctx.state.get_init_data())



@command("get_harness_resume_prompt")
async def _cmd_get_harness_resume_prompt(ctx: CommandContext) -> None:
    session_mode = ctx.msg.get("session_mode", "code")
    session_role = ctx.msg.get("session_role", "generator")
    prompt = ctx.state.harness_prompts.build_harness_resume_prompt(
        session_mode=session_mode,
        session_role=session_role,
        project_path=ctx.state.project.project_path,
    )
    await ctx.send({
        "event": "resume_prompt",
        "session_mode": ctx.state.normalize_session_mode(session_mode),
        "session_role": ctx.state.normalize_session_role(session_mode, session_role),
        "prompt": prompt,
    })



@command("harness_cycle_start")
async def _cmd_harness_cycle_start(ctx: CommandContext) -> None:
    # v0.4.5 (T1.5) — pre-cut, this had a resonant-engine
    # branch that delegated to the remote backend's
    # `start_harness_cycle`. With ResonantBackend gone,
    # the local `ctx.state.harness_orchestrator` is the only
    # path.
    max_loops = int(ctx.msg.get("max_loops") or 6)
    name = (ctx.msg.get("name") or "").strip()
    objective = (ctx.msg.get("objective") or "").strip()
    run = ctx.state.harness_orchestrator.start_cycle(
        project_path=ctx.state.project.project_path,
        name=name or "Harness Cycle",
        objective=objective,
        max_loops=max_loops,
    )
    await ctx.send({"event": "harness_cycle_started", "run": run.to_dict()})
    await ctx.send({"event": "harness_cycle_list", "ctx.runs": ctx.state.harness_orchestrator.list_runs()})



@command("harness_cycle_result")
async def _cmd_harness_cycle_result(ctx: CommandContext) -> None:
    run_id = (ctx.msg.get("run_id") or "").strip()
    run = ctx.state.harness_orchestrator.get_run(run_id)
    if run:
        await ctx.send({"event": "harness_cycle_result", "run": run.to_full_dict()})
    else:
        await ctx.send({"event": "error", "message": f"Harness cycle {run_id} not found"})



@command("harness_cycle_cancel")
async def _cmd_harness_cycle_cancel(ctx: CommandContext) -> None:
    run_id = (ctx.msg.get("run_id") or "").strip()
    cancelled = ctx.state.harness_orchestrator.cancel(run_id)
    await ctx.send({"event": "harness_cycle_cancelled", "run_id": run_id, "success": cancelled})
    await ctx.send({"event": "harness_cycle_list", "ctx.runs": ctx.state.harness_orchestrator.list_runs()})



@command("harness_teacher_recover")
async def _cmd_harness_teacher_recover(ctx: CommandContext) -> None:
    reason = (ctx.msg.get("reason") or "").strip() or "manual_recovery"
    failed_role = ctx.state.normalize_session_role(
        "code",
        (ctx.msg.get("failed_role") or ctx.state.harness_prompts.get_harness_summary().get("active_role") or "generator"),
    )
    objective = (ctx.msg.get("objective") or "").strip()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ctx.state.harness_prompts.run_harness_teacher_escalation(
                project_path=ctx.state.project.project_path,
                failed_role=failed_role,
                reason=reason,
                objective=objective,
            ),
        )
        await ctx.send(
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
        await ctx.send({"event": "harness_state", "data": ctx.state.harness_prompts.get_harness_summary()})
    except Exception as exc:
        await ctx.send({"event": "error", "message": f"Teacher recovery failed: {exc}"})



@command("set_harness_sprint")
async def _cmd_set_harness_sprint(ctx: CommandContext) -> None:
    sprint_id = (ctx.msg.get("sprint_id") or "").strip()
    feature_name = (ctx.msg.get("feature_name") or "").strip()
    objective = (ctx.msg.get("objective") or "").strip()
    if not sprint_id or not objective:
        await ctx.send({"event": "error", "message": "sprint_id and objective are required"})
        return
    # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
    # to the remote backend's `set_harness_sprint`. Local
    # `ctx.state.harness.set_active_sprint` is the only path now.
    ctx.state.harness.set_active_sprint(
        sprint_id=sprint_id,
        feature_name=feature_name,
        objective=objective,
        deliverables=list(ctx.msg.get("deliverables") or []),
        acceptance_checks=list(ctx.msg.get("acceptance_checks") or []),
        evaluator_focus=list(ctx.msg.get("evaluator_focus") or []),
        status=(ctx.msg.get("status") or "proposed"),
        role=ctx.state.normalize_session_role("code", ctx.msg.get("session_role", "planner")),
    )
    await ctx.send({"event": "harness_state", "data": ctx.state.harness_prompts.get_harness_summary()})
    await ctx.send({"event": "status_msg", "message": f"Updated sprint {sprint_id}"})



@command("set_harness_contract_status")
async def _cmd_set_harness_contract_status(ctx: CommandContext) -> None:
    status_value = (ctx.msg.get("status") or "").strip()
    if status_value not in {"proposed", "approved", "implemented", "needs_revision", "passed", "failed"}:
        await ctx.send({"event": "error", "message": "valid contract status is required"})
        return
    # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
    # to the remote backend's `set_harness_contract_status`.
    ctx.state.harness.set_contract_status(
        status=status_value,
        role=ctx.state.normalize_session_role("code", ctx.msg.get("session_role", "planner")),
    )
    await ctx.send({"event": "harness_state", "data": ctx.state.harness_prompts.get_harness_summary()})
    await ctx.send({"event": "status_msg", "message": f"Set sprint contract to {status_value}"})



@command("set_evaluator_verdict")
async def _cmd_set_evaluator_verdict(ctx: CommandContext) -> None:
    sprint_id = (ctx.msg.get("sprint_id") or "").strip()
    verdict = (ctx.msg.get("verdict") or "").strip()
    if not sprint_id or verdict not in {"pass", "revise", "blocked"}:
        await ctx.send({"event": "error", "message": "valid sprint_id and verdict are required"})
        return
    # v0.4.5 (T1.5) — pre-cut, the resonant branch delegated
    # to the remote backend's `set_evaluator_verdict`.
    ctx.state.harness.record_evaluator_verdict(
        sprint_id=sprint_id,
        verdict=verdict,
        findings=list(ctx.msg.get("findings") or []),
        required_revisions=list(ctx.msg.get("required_revisions") or []),
        passed_checks=list(ctx.msg.get("passed_checks") or []),
        failed_checks=list(ctx.msg.get("failed_checks") or []),
        score=ctx.msg.get("score"),
    )
    await ctx.send({"event": "harness_state", "data": ctx.state.harness_prompts.get_harness_summary()})
    await ctx.send({"event": "status_msg", "message": f"Evaluator marked sprint {sprint_id} as {verdict}"})



@command("select_backend")
async def _cmd_select_backend(ctx: CommandContext) -> None:
    backend_type = ctx.msg.get("backend", "")
    model = ctx.msg.get("model", "")
    session_mode = ctx.msg.get("session_mode", "code")
    session_role = ctx.msg.get("session_role", "generator")
    previous_record = ctx.state.project.current_session
    try:
        # Backend setup starts a fresh, lazily-persisted session.
        # Detach the prior record so the first message cannot
        # overwrite an existing conversation. Restore it if the
        # provider connection itself fails.
        ctx.state.project.current_session = None
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ctx.state.create_backend(
                backend_type,
                model or None,
                session_mode=session_mode,
                session_role=session_role,
            ),
        )
        ctx.state._first_message_sent = False
        await ctx.send(ctx.state.get_init_data())
        await ctx.send({"event": "status_msg", "message": f"Connected to {backend_type}"})

        # Pre-warm the model so the user's first message doesn't sit
        # at "thinking" for 60-90s while Ollama cold-loads. Fire and
        # forget — we don't want to block the connect response.
        backend_for_warm = ctx.state.backend
        if backend_for_warm and hasattr(backend_for_warm, "warm_up"):
            async def _emit_warm_event(payload: dict):
                try:
                    await ctx.send(payload)
                except Exception:
                    pass

            loop = asyncio.get_running_loop()
            warm_started = time.time()
            await ctx.send({
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
        ctx.state.project.current_session = previous_record
        await ctx.send({"event": "error", "message": str(e)})



@command("message")
async def _cmd_message(ctx: CommandContext) -> None:
    text = ctx.msg.get("text", "").strip()
    if not text:
        return
    if not ctx.state.session:
        await ctx.send({"event": "error", "message": "No backend selected"})
        return
    await ctx.runs.enqueue(ctx.msg)
    return



@command("status_update")
async def _cmd_status_update(ctx: CommandContext) -> None:
    # A status request is steering, never a replacement turn. Do
    # not enqueue it if the active run ends during this command;
    # that would unexpectedly start a new task after the race.
    message_id = str(ctx.msg.get("message_id") or uuid.uuid4())
    running = ctx.runs.busy
    accepted = bool(
        running
        and ctx.state.session
        and ctx.state.session.steer(
            STATUS_UPDATE_STEER,
            message_id=message_id,
        )
    )
    await ctx.send({
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
    return



@command("steer")
async def _cmd_steer(ctx: CommandContext) -> None:
    text = str(ctx.msg.get("text") or "").strip()
    message_id = str(ctx.msg.get("message_id") or uuid.uuid4())
    running = ctx.runs.busy
    if not text or not ctx.state.session:
        return
    if running and ctx.state.session.steer(text, message_id=message_id):
        await ctx.send({
            "event": "message.queued",
            "message_id": message_id,
            "text": text,
            "position": 0,
            "steering": True,
        })
    else:
        await ctx.runs.enqueue(dict(ctx.msg, command="message"))
    return



@command("steer_queued")
async def _cmd_steer_queued(ctx: CommandContext) -> None:
    message_id = str(ctx.msg.get("message_id") or "")
    queued_index = next(
        (
            index for index, item in enumerate(ctx.runs.pending)
            if str(item.get("message_id") or "") == message_id
        ),
        None,
    )
    if queued_index is None:
        await ctx.send({
            "event": "ui_notice",
            "message": "That follow-up has already started or is no longer queued.",
        })
        return
    queued = ctx.runs.pending.pop(queued_index)
    steering_text = str(queued.get("text") or "").strip()
    accepted = bool(
        ctx.state.session
        and ctx.state.session.steer(steering_text, message_id=message_id)
    )
    if not accepted:
        ctx.runs.pending.insert(queued_index, queued)
        await ctx.send({
            "event": "ui_notice",
            "message": "The active run could not accept that steering message.",
        })
        return
    await ctx.send({
        "event": "message.queued",
        "message_id": message_id,
        "text": steering_text,
        "position": 0,
        "steering": True,
    })
    return



@command("remove_queued")
async def _cmd_remove_queued(ctx: CommandContext) -> None:
    message_id = str(ctx.msg.get("message_id") or "")
    queued_index = next(
        (
            index for index, item in enumerate(ctx.runs.pending)
            if str(item.get("message_id") or "") == message_id
        ),
        None,
    )
    removed = False
    if queued_index is not None:
        ctx.runs.pending.pop(queued_index)
        removed = True
    elif ctx.state.session and hasattr(ctx.state.session, "remove_steering"):
        removed = bool(ctx.state.session.remove_steering(message_id))

    if removed:
        await ctx.send({
            "event": "message.removed",
            "message_id": message_id,
        })
    else:
        await ctx.send({
            "event": "message.remove_failed",
            "message_id": message_id,
            "message": "That follow-up has already started or was already applied.",
        })
    return



@command("cancel")
async def _cmd_cancel(ctx: CommandContext) -> None:
    cancel_id = str(ctx.msg.get("cancel_id") or uuid.uuid4())
    cleared_ids = [
        str(item.get("message_id") or "") for item in ctx.runs.pending
    ]
    ctx.runs.pending.clear()
    await ctx.send({
        "event": "cancel.requested",
        "cancel_id": cancel_id,
    })
    ctx.state.cancel_requested.set()
    if ctx.state.session:
        ctx.state.session.cancel()
    if cleared_ids:
        await ctx.send({
            "event": "message.queue_cleared",
            "message_ids": cleared_ids,
        })
    if ctx.runs.busy:
        ctx.runs.cancel_request_id = cancel_id
    else:
        await ctx.send({
            "event": "cancel.completed",
            "cancel_id": cancel_id,
        })



@command("mission_resume")
async def _cmd_mission_resume(ctx: CommandContext) -> None:
    # B4 — Resume an exited / completed mission. Flips the
    # phase back to whatever phase it was in before exit; if
    # it was already past completion, drop back to drafting
    # so the user can keep iterating. Also switches to that
    # session if it's not already current.
    target_id = (ctx.msg.get("session_id") or "").strip()
    if not target_id:
        await ctx.send({"event": "error",
                            "message": "session_id required"})
        return
    # Switch to that session first if needed.
    if (not ctx.state.project.current_session) or (ctx.state.project.current_session.id != target_id):
        ctx.state.project.load_session(target_id)
        if ctx.state.project.current_session and ctx.state.backend:
            ctx.state.session = ctx.state.build_session(
                backend=ctx.state.backend,
                backend_spec=ctx.state.backend_spec,
                project_path=ctx.state.project.project_path,
                session_mode="code",
                session_role=ctx.state.project.current_session.session_role,
            )
    cur = ctx.state.project.current_session
    if not cur or not cur.is_mission:
        await ctx.send({"event": "error",
                            "message": "Not a mission session"})
        return
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
    await ctx.send({
        "event": "mission_phase_changed",
        "session_id": cur.id,
        "phase": cur.mission_state["phase"],
    })
    await ctx.send({
        "event": "sessions_updated",
        "sessions": ctx.state.project.list_sessions(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        "current_session_id": cur.id,
    })



@command("mission_exit")
async def _cmd_mission_exit(ctx: CommandContext) -> None:
    # User hit "Exit Mission" on the header badge. Cancels any
    # in-flight turn, marks the mission as exited, leaves the
    # session selectable in the sidebar (under Missions /
    # exited) for review.
    if ctx.state.active_thread and ctx.state.active_thread.is_alive():
        ctx.state.cancel_requested.set()
        if ctx.state.session:
            ctx.state.session.cancel()
    if ctx.state.project.current_session:
        ctx.state.project.current_session.exit_mission()
        ctx.state.project.current_session.save()
    await ctx.send({
        "event": "mission_exited",
        "sessions": ctx.state.project.list_sessions(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        "current_session_id": ctx.state.project.current_session.id if ctx.state.project.current_session else "",
    })



@command("mission_dispatch_roadmap")
async def _cmd_mission_dispatch_roadmap(ctx: CommandContext) -> None:
    # User clicked "Build this roadmap" on the spec card. We
    # advance the mission phase, dispatch the FULL spec to
    # intent_service (not just the refined-intent paragraph —
    # that was a Tier-1 bug from the first iteration), and
    # let the existing intent flow take over.
    if not ctx.state.project.current_session:
        await ctx.send({"event": "error",
                            "message": "No active mission to dispatch"})
        return
    ms = ctx.state.project.current_session.mission_state or {}
    if ms.get("phase") != "drafting":
        await ctx.send({"event": "error",
                            "message": f"Mission phase is {ms.get('phase','?')}, expected drafting"})
        return

    spec_md = (ctx.msg.get("spec_markdown") or "").strip()
    refined = (ctx.msg.get("refined_intent") or "").strip()
    if not spec_md and not refined:
        await ctx.send({"event": "error",
                            "message": "No spec to dispatch"})
        return

    # Tier-1 fix #1: pass the full spec block as the intent
    # text so the planner sees assumptions / scope / acceptance
    # criteria, not just one paragraph. Refined intent stays
    # in mission_state for display.
    intent_text = spec_md or refined

    def _emit_intent(payload: dict, _ws=ctx.ws, _loop=asyncio.get_running_loop()):
        try:
            asyncio.run_coroutine_threadsafe(_ws.send_json(payload), _loop)
        except Exception:
            logger.debug("intent emit raised", exc_info=True)

    intent_service = ctx.state.get_intent_service(on_event=_emit_intent)
    try:
        intent_id = intent_service.start_intent(intent_text)
    except Exception as exc:
        logger.exception("mission_dispatch_roadmap failed")
        await ctx.send({"event": "error",
                            "message": f"Roadmap dispatch failed: {exc}"})
        return

    ctx.state.project.current_session.advance_mission_phase(
        "planning_dispatched",
        spec_markdown=spec_md or "",
        refined_intent=refined or "",
        intent_id=intent_id,
    )
    ctx.state.project.current_session.save()
    await ctx.send({
        "event": "mission_phase_changed",
        "session_id": ctx.state.project.current_session.id,
        "phase": "planning_dispatched",
        "intent_id": intent_id,
    })
    await ctx.send({
        "event": "sessions_updated",
        "sessions": ctx.state.project.list_sessions(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        "current_session_id": ctx.state.project.current_session.id,
    })



@command("autonomous_mission_stop")
async def _cmd_autonomous_mission_stop(ctx: CommandContext) -> None:
    # v0.5.0a6 — User clicked Stop in the chat-header
    # autonomous badge. Find the daemon by intent_id and
    # signal it; the daemon emits autonomous_mission_paused
    # asynchronously as it winds down. The mission_state
    # phase transition to autonomous_paused happens when
    # we receive that event (kept in one place to avoid
    # races).
    target_intent = (ctx.msg.get("intent_id") or "").strip()
    if not target_intent and ctx.state.project.current_session:
        ms = ctx.state.project.current_session.mission_state or {}
        target_intent = ms.get("intent_id", "")
    if not target_intent:
        await ctx.send({"event": "error",
                            "message": "intent_id required (or active mission)"})
        return

    stopped = _stop_autonomous_mission(
        ctx.state, target_intent,
        reason="user_stop",
        message="user clicked Stop",
    )
    if not stopped:
        await ctx.send({"event": "error",
                            "message": f"No active autonomous daemon for intent {target_intent}"})
        return
    # Daemon will emit `autonomous_mission_paused` itself;
    # nothing else to do here.



@command("autonomous_mission_pause")
async def _cmd_autonomous_mission_pause(ctx: CommandContext) -> None:
    # v0.5.9a4 — pause-after-current-iter. Distinct from
    # autonomous_mission_stop which cancels in-flight.
    # Daemon completes the current iter + reflection,
    # then exits with stop_reason="user_pause". UX: lets
    # the user "stop after this completes" without losing
    # the iter's work.
    target_intent = (ctx.msg.get("intent_id") or "").strip()
    if not target_intent and ctx.state.project.current_session:
        ms = ctx.state.project.current_session.mission_state or {}
        target_intent = ms.get("intent_id", "")
    if not target_intent:
        await ctx.send({
            "event": "error",
            "message": "intent_id required (or active mission)",
        })
        return
    daemon = _get_autonomous_daemon(ctx.state, target_intent)
    if daemon is None:
        await ctx.send({
            "event": "error",
            "message": (
                f"No active autonomous daemon for intent "
                f"{target_intent}"
            ),
        })
        return
    try:
        daemon.pause_after_iter("user clicked Pause")
    except Exception as exc:
        logger.exception("pause_after_iter raised")
        await ctx.send({
            "event": "error",
            "message": f"Failed to schedule pause: {exc}",
        })
        return
    # Acknowledge so the GUI can flip the badge ctx.state to
    # "pausing — finishing current iter…". Daemon emits
    # autonomous_mission_paused once the current iter
    # completes.
    await ctx.send({
        "event": "autonomous_pause_scheduled",
        "intent_id": target_intent,
    })



@command("autonomous_mission_decision")
async def _cmd_autonomous_mission_decision(ctx: CommandContext) -> None:
    # v0.5.8a2 — User picked an option on the
    # human-decision-required card. Look up the daemon by
    # intent_id and call provide_decision() to unblock the
    # parked REFLECT pass. The daemon will retry REFLECT
    # with the user's choice folded into the prompt.
    target_intent = (ctx.msg.get("intent_id") or "").strip()
    option_id = (ctx.msg.get("option_id") or "").strip()
    response_text = (ctx.msg.get("response_text") or "").strip()
    if not target_intent and ctx.state.project.current_session:
        ms = ctx.state.project.current_session.mission_state or {}
        target_intent = ms.get("intent_id", "")
    if not target_intent:
        await ctx.send({
            "event": "error",
            "message": "intent_id required (or active mission)",
        })
        return
    if not option_id:
        await ctx.send({
            "event": "error",
            "message": "option_id is required",
        })
        return
    daemon = _get_autonomous_daemon(ctx.state, target_intent)
    if daemon is None:
        await ctx.send({
            "event": "error",
            "message": (
                f"No active autonomous daemon for intent "
                f"{target_intent}"
            ),
        })
        return
    try:
        accepted = daemon.provide_decision(
            option_id, response_text,
        )
    except Exception as exc:
        logger.exception("provide_decision raised")
        await ctx.send({
            "event": "error",
            "message": f"Failed to deliver decision: {exc}",
        })
        return
    # Daemon will emit `autonomous_human_decision_received`
    # asynchronously when it picks up the choice. The
    # `accepted` boolean tells us whether the daemon was
    # actually parked (race-window guard); if False, the
    # daemon may have already unparked itself or been
    # stopped, but the response is still recorded for the
    # NEXT park if one happens. Echo the routing decision
    # back so the GUI can clear the card promptly.
    await ctx.send({
        "event": "autonomous_decision_dispatched",
        "intent_id": target_intent,
        "option_id": option_id,
        "was_parked": accepted,
    })



@command("autonomous_mission_roadmap")
async def _cmd_autonomous_mission_roadmap(ctx: CommandContext) -> None:
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
    target_intent = (ctx.msg.get("intent_id") or "").strip()
    if not target_intent and ctx.state.project.current_session:
        ms = ctx.state.project.current_session.mission_state or {}
        target_intent = ms.get("intent_id", "")
    if not target_intent:
        await ctx.send({"event": "error",
                            "message": "intent_id required (or active mission)"})
        return

    from .roadmap import default_path as _rm_default_path, load as _rm_load
    roadmap_path = _rm_default_path(
        ctx.state.project.project_path, target_intent,
    )
    if not roadmap_path.exists():
        # Not an error — early in a mission the daemon
        # may not have persisted the roadmap yet, or this
        # could be a stale request from a closed mission.
        # Send an empty payload so the frontend can clear
        # its inspector cleanly.
        await ctx.send({
            "event": "autonomous_mission_roadmap",
            "intent_id": target_intent,
            "roadmap_exists": False,
            "roadmap_path": str(roadmap_path),
        })
        return

    try:
        rm = _rm_load(roadmap_path)
    except Exception as exc:
        logger.exception("autonomous_mission_roadmap parse failed")
        await ctx.send({"event": "error",
                            "message": f"Could not parse roadmap: {exc}"})
        return

    payload = _build_roadmap_inspector_payload(
        intent_id=target_intent,
        roadmap=rm,
        roadmap_path=roadmap_path,
    )
    payload["event"] = "autonomous_mission_roadmap"
    await ctx.send(payload)



@command("autonomous_orphans_list")
async def _cmd_autonomous_orphans_list(ctx: CommandContext) -> None:
    # v0.5.3a2 — Frontend asks for a fresh orphan list
    # (e.g. after dismissing one or after a long idle).
    # `init` already includes the same field on connect /
    # session-switch refresh; this command exists so the
    # frontend doesn't have to round-trip the full init
    # payload to refresh the banner.
    await ctx.send({
        "event": "autonomous_orphans",
        "orphans": _find_orphaned_autonomous_missions(ctx.state),
    })



@command("autonomous_missions_list")
async def _cmd_autonomous_missions_list(ctx: CommandContext) -> None:
    # v0.5.5a2 — Frontend refreshes the sidebar mission
    # browser. `init` includes the same field on connect;
    # this command lets the frontend pull a fresh snapshot
    # without a full init round-trip.
    await ctx.send({
        "event": "autonomous_missions",
        "missions": _list_autonomous_missions(ctx.state),
    })



@command("list_project_files")
async def _cmd_list_project_files(ctx: CommandContext) -> None:
    # Pi-style `@file` autocomplete: front-end caches a file list
    # for the current project and filters it client-side. We walk
    # the tree once on demand, skipping the usual bloat dirs and
    # capping at a sane upper bound so giant monorepos don't
    # ship multi-megabyte JSON over the websocket.
    request_id = ctx.msg.get("request_id", "")
    project_path_str = (
        ctx.state.project.project_path
        if ctx.state.project and ctx.state.project.project_path
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
    await ctx.send({
        "event": "project_files",
        "request_id": request_id,
        "files": _files,
        "total": len(_files),
        "truncated": len(_files) >= _MAX_FILES,
        "project_path": project_path_str,
    })



@command("clear")
async def _cmd_clear(ctx: CommandContext) -> None:
    # Create a new session (don't destroy old one)
    request_id = str(ctx.msg.get("request_id") or "").strip()
    if request_id and request_id in ctx.runs.clear_cache:
        await ctx.send(ctx.runs.clear_cache[request_id])
        return
    session_mode = ctx.msg.get("session_mode", "code")
    session_role = ctx.msg.get("session_role", "generator")
    if ctx.state.backend:
        backend_type = getattr(ctx.state.backend, "name", "")
        model = getattr(ctx.state.backend, "model", "")
        ctx.state.project.create_session(
            backend_type=backend_type,
            model=model,
            session_role=session_role,
        )
        ctx.state.session = ctx.state.build_session(
            backend=ctx.state.backend,
            backend_spec=ctx.state.backend_spec,
            project_path=ctx.state.project.project_path,
            session_mode=session_mode,
            session_role=session_role,
        )
        ctx.state._first_message_sent = False
        ctx.state.costs.reset_session()
    response = {
        "event": "session_cleared",
        "sessions": ctx.state.project.list_sessions(),
        "current_session_id": ctx.state.project.current_session.id if ctx.state.project.current_session else "",
        "session_mode": session_mode,
        "session_role": session_role,
        "cwd": ctx.state.project.project_path,
        "request_id": request_id,
    }
    if request_id:
        ctx.runs.clear_cache[request_id] = response
        if len(ctx.runs.clear_cache) > 32:
            ctx.runs.clear_cache.pop(next(iter(ctx.runs.clear_cache)))
    await ctx.send(response)



@command("director_configure")
async def _cmd_director_configure(ctx: CommandContext) -> None:
    from ..engine.director import DirectorConfig

    config = DirectorConfig.from_dict(ctx.msg.get("config") or {})
    enabled_workers = [
        worker for worker in config.workers
        if worker.enabled and worker.backend_type and worker.model
    ]
    if config.enabled and (
        not config.director_backend_type or not config.director_model
    ):
        await ctx.send({
            "event": "director.configure_failed",
            "message": "Select a Director model before enabling Director Mode.",
        })
        return
    if config.enabled and not enabled_workers:
        await ctx.send({
            "event": "director.configure_failed",
            "message": "Select at least one worker model.",
        })
        return

    record = ctx.state.project.current_session
    if record is None:
        record = ctx.state.project.create_session(
            backend_type=config.director_backend_type or getattr(ctx.state.backend, "name", ""),
            model=config.director_model or getattr(ctx.state.backend, "model", ""),
            session_role="generator",
            orchestration_mode="director" if config.enabled else "single",
            director_config=config.to_dict(),
        )
    old_history = list(ctx.state.session.conversation_history) if ctx.state.session else []
    record.orchestration_mode = "director" if config.enabled else "single"
    record.director_config = config.to_dict()
    record.director_run_id = ""
    if config.enabled:
        record.backend_type = config.director_backend_type
        record.model = config.director_model
    record.save()
    try:
        backend_type = record.backend_type or getattr(ctx.state.backend, "name", "")
        model = record.model or getattr(ctx.state.backend, "model", "")
        if backend_type and model:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ctx.state.create_backend(
                    backend_type,
                    model,
                    session_mode="code",
                    session_role=record.session_role or "generator",
                ),
            )
            ctx.state.session.conversation_history = old_history
        record.conversation_history = old_history
        record.save()
        await ctx.send({
            "event": "director.configured",
            "mode": record.orchestration_mode,
            "config": record.director_config,
            "run": (
                ctx.state.session.director_run.to_dict()
                if ctx.state.session and ctx.state.session.director_run else None
            ),
        })
        await ctx.send(ctx.state.get_init_data(refresh_only=True))
    except Exception as exc:
        logger.exception("Director Mode configuration failed")
        await ctx.send({
            "event": "director.configure_failed",
            "message": f"Director Mode configuration failed: {exc}",
        })



@command("switch_model")
async def _cmd_switch_model(ctx: CommandContext) -> None:
    model = ctx.msg.get("model", "")
    backend_type = ctx.msg.get("backend", "")
    if not backend_type and ctx.state.backend and hasattr(ctx.state.backend, "name"):
        backend_type = getattr(ctx.state.backend, "name", "")
    if backend_type:
        try:
            session_role = (
                ctx.state.project.current_session.session_role
                if ctx.state.project.current_session else "generator"
            )
            # Bug #9+#10 fix: swap_backend (not create_backend)
            # preserves the existing session's conversation_history.
            # Previously we rebuilt the session from scratch, silently
            # discarding all prior turns.
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ctx.state.swap_backend(
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
            if backend_type in ctx.state.CLI_WRAPPED_BACKENDS:
                await ctx.send({
                    "event": "backend_swap_warning",
                    "backend": backend_type,
                    "message": (
                        f"Switched to {backend_type}. This backend uses its own CLI "
                        f"session and won't see prior conversation turns. "
                        f"Switch back to your previous backend to resume the "
                        f"original thread with full context."
                    ),
                })
            await ctx.send(ctx.state.get_init_data())
        except Exception as e:
            await ctx.send({"event": "error", "message": str(e)})



@command("set_thinking_mode")
async def _cmd_set_thinking_mode(ctx: CommandContext) -> None:
    # Per-session thinking-mode toggle (deepseek-v* etc.).
    # Forces a backend rebuild because Ollama options must be stable
    # for the lifetime of an OllamaBackend instance.
    mode = (ctx.msg.get("mode") or "").strip().lower()
    if mode not in {"", "off", "low", "med", "medium", "high", "max"}:
        await ctx.send({"event": "error", "message": f"invalid thinking mode: {mode!r}"})
    else:
        try:
            # v0.6.5 — explicit "off" is stored as the truthy
            # token "off" (not "") so it survives the
            # thinking_mode preservation in create_backend /
            # swap_backend; otherwise a falsy "" would let the
            # per-model default (e.g. GLM→high) clobber the
            # user's choice on the next backend rebuild.
            normalized = "off" if mode in {"", "off"} else ("med" if mode == "medium" else mode)
            if ctx.state.project.current_session:
                ctx.state.project.current_session.thinking_mode = normalized
                ctx.state.project.current_session.save()
            # Rebuild the backend with the new thinking_mode in spec
            if ctx.state.backend_spec:
                ctx.state.backend_spec.thinking_mode = normalized
                session_role = (
                    ctx.state.project.current_session.session_role
                    if ctx.state.project.current_session else "generator"
                )
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ctx.state.create_backend(
                        ctx.state.backend_spec.backend_type,
                        ctx.state.backend_spec.model,
                        session_mode="code",
                        session_role=session_role,
                    ),
                )
            await ctx.send({
                "event": "thinking_mode_set",
                "mode": normalized,
            })
            await ctx.send(ctx.state.get_init_data())
        except Exception as e:
            await ctx.send({"event": "error", "message": str(e)})



@command("fork_session")
async def _cmd_fork_session(ctx: CommandContext) -> None:
    source_id = ctx.msg.get("session_id", "")
    idx = int(ctx.msg.get("user_message_index", 0))
    forked = ctx.state.project.fork_session(source_id, idx)
    if forked is None:
        await ctx.send({"event": "error", "message": f"Cannot fork: session {source_id} not found"})
    else:
        # Rebuild a session bound to the forked record so subsequent messages append correctly.
        if ctx.state.backend_spec:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ctx.state.create_backend(
                        ctx.state.backend_spec.backend_type,
                        ctx.state.backend_spec.model,
                        session_mode="code",
                        session_role=forked.session_role,
                    ),
                )
                # Restore the forked conversation onto the new Session
                ctx.state.session.conversation_history = list(forked.conversation_history)
                ctx.state._first_message_sent = bool(forked.conversation_history)
            except Exception as exc:
                logger.warning("fork_session backend rebuild failed: %s", exc)

        await ctx.send({
            "event": "session_forked",
            "session_id": forked.id,
            "title": forked.title,
            "user_messages_kept": forked.message_count,
        })
        await ctx.send({
            "event": "session_loaded",
            "current_session_id": forked.id,
            "session_role": forked.session_role,
            "display_events": forked.display_events,
            "sessions": ctx.state.project.list_sessions(),
        })



@command("switch_session")
async def _cmd_switch_session(ctx: CommandContext) -> None:
    session_id = ctx.msg.get("session_id", "")
    # If session is from a different project, switch project first
    project_path = ctx.msg.get("project_path", "")
    if project_path and ctx.state._normalize_path(project_path) != ctx.state._normalize_path(ctx.state.project.project_path):
        if os.path.isdir(project_path):
            ctx.state.apply_project_context(project_path, refresh_index=True)
            ctx.state.backend = None
            ctx.state.backend_spec = None
            ctx.state.session = None
            ctx.state._first_message_sent = False
            await asyncio.get_event_loop().run_in_executor(None, ctx.state.detect_backends)
    record = ctx.state.project.load_session(session_id)
    if record:
        # Replay the saved conversation immediately. Backend startup
        # can take seconds (or fail when a provider is offline), but
        # neither condition should prevent users from opening and
        # reading an existing session.
        await ctx.send({
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
            "sessions": ctx.state.project.list_sessions(),
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
                    lambda: ctx.state.create_backend(
                        backend_type,
                        model or None,
                        session_mode="code",
                        session_role=record.session_role or "generator",
                    ),
                )
            else:
                ctx.state.backend = None
                ctx.state.backend_spec = None
                ctx.state.session = None
            if ctx.state.session and record.conversation_history:
                ctx.state.session.conversation_history = record.conversation_history
            ctx.state._first_message_sent = record.message_count > 0

        except Exception as e:
            ctx.state.backend = None
            ctx.state.backend_spec = None
            ctx.state.session = None
            await ctx.send({
                "event": "status_msg",
                "message": f"Session loaded. Its runtime is unavailable: {e}",
            })
        # Send lightweight init refresh (skip re-detecting backends)
        # even when runtime setup fails so navigation stays coherent.
        await ctx.send(ctx.state.get_init_data(refresh_only=True))
    else:
        await ctx.send({"event": "error", "message": f"Session {session_id} not found"})



@command("delete_session")
async def _cmd_delete_session(ctx: CommandContext) -> None:
    session_id = ctx.msg.get("session_id", "")
    ctx.state.project.delete_session(session_id)
    await ctx.send({
        "event": "sessions_updated",
        "sessions": ctx.state.project.list_sessions(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        "current_session_id": ctx.state.project.current_session.id if ctx.state.project.current_session else "",
    })



@command("rename_session")
async def _cmd_rename_session(ctx: CommandContext) -> None:
    session_id = ctx.msg.get("session_id", "")
    new_title = ctx.msg.get("title", "").strip()
    if session_id and new_title:
        record = ctx.state.project.load_session(session_id)
        if record:
            record.title = new_title
            record.save()
            # Restore current session pointer if it changed
            if ctx.state.project.current_session and ctx.state.project.current_session.id != session_id:
                ctx.state.project.load_session(ctx.state.project.current_session.id)
        await ctx.send({
            "event": "sessions_updated",
            "sessions": ctx.state.project.list_sessions(),
            "all_sessions": ctx.state.project.list_all_sessions(),
            "current_session_id": ctx.state.project.current_session.id if ctx.state.project.current_session else "",
        })



@command("pin_session")
async def _cmd_pin_session(ctx: CommandContext) -> None:
    session_id = ctx.msg.get("session_id", "")
    # Use current session when no explicit ID supplied (/pin command)
    if session_id:
        record = ctx.state.project.load_session(session_id)
        # Restore current session pointer after the load
        if record and ctx.state.project.current_session and ctx.state.project.current_session.id != session_id:
            ctx.state.project.load_session(ctx.state.project.current_session.id)
    else:
        record = ctx.state.project.current_session
    if record:
        record.pinned = not record.pinned
        record.save()
        verb = "Pinned" if record.pinned else "Unpinned"
        await ctx.send({"event": "status_msg", "message": f"{verb} session."})
    await ctx.send({
        "event": "sessions_updated",
        "sessions": ctx.state.project.list_sessions(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        "current_session_id": ctx.state.project.current_session.id if ctx.state.project.current_session else "",
    })

# ── Intent / organic-orchestration commands ─────────────



@command("intent_start")
@command("intent_cancel")
@command("intent_pause")
@command("intent_resume")
@command("intent_list_snapshots")
@command("intent_restore_snapshot")
async def _cmd_intent_start(ctx: CommandContext) -> None:
    # Bridge the intent worker thread back to this WebSocket. The
    # service ctx.runs on a thread, so we hand it a thread-safe emitter
    # that schedules `ctx.ws.send_json` on the asyncio loop.
    loop = asyncio.get_running_loop()

    def _emit_intent(payload: dict, _ws=ctx.ws, _loop=loop):
        try:
            asyncio.run_coroutine_threadsafe(_ws.send_json(payload), _loop)
        except Exception:
            logger.debug("intent emit raised", exc_info=True)

    if ctx.state.backend is None:
        await ctx.send({"event": "error",
                            "message": "Connect a backend before starting an intent."})
    else:
        intent_service = ctx.state.get_intent_service(on_event=_emit_intent)

        if command == "intent_start":
            text = (ctx.msg.get("text") or "").strip()
            if not text:
                await ctx.send({"event": "error",
                                    "message": "intent text is required"})
            else:
                try:
                    intent_id = intent_service.start_intent(text)
                    await ctx.send({
                        "event": "intent.accepted",
                        "intent_id": intent_id,
                        "text": text,
                    })
                except Exception as exc:
                    logger.exception("intent_start failed")
                    await ctx.send({"event": "error",
                                        "message": f"intent_start failed: {exc}"})
        elif command == "intent_cancel":
            ok = intent_service.cancel(ctx.msg.get("intent_id", ""))
            await ctx.send({"event": "intent.cancel_ack",
                                "intent_id": ctx.msg.get("intent_id", ""),
                                "ok": ok})
        elif command == "intent_pause":
            ok = intent_service.pause(ctx.msg.get("intent_id", ""))
            await ctx.send({"event": "intent.pause_ack",
                                "intent_id": ctx.msg.get("intent_id", ""),
                                "ok": ok})
        elif command == "intent_resume":
            ok = intent_service.resume(ctx.msg.get("intent_id", ""))
            await ctx.send({"event": "intent.resume_ack",
                                "intent_id": ctx.msg.get("intent_id", ""),
                                "ok": ok})
        elif command == "intent_list_snapshots":
            snaps = intent_service.list_snapshots(ctx.msg.get("intent_id", ""))
            await ctx.send({"event": "plan.snapshot_list",
                                "intent_id": ctx.msg.get("intent_id", ""),
                                "snapshots": snaps})
        elif command == "intent_restore_snapshot":
            ok = intent_service.restore_snapshot(
                ctx.msg.get("intent_id", ""),
                int(ctx.msg.get("ts_ms") or 0),
            )
            await ctx.send({"event": "intent.restore_ack",
                                "intent_id": ctx.msg.get("intent_id", ""),
                                "ok": ok})



@command("redetect_backends")
async def _cmd_redetect_backends(ctx: CommandContext) -> None:
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
    ctx.state.refresh_network_defaults()
    await asyncio.get_event_loop().run_in_executor(None, ctx.state.detect_backends)
    ollama_info = ctx.state.available_backends.get("ollama") or {}
    if ollama_info and not ctx.state.backend:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ctx.state.ensure_default_runtime_session(),
            )
        except Exception:
            logger.exception("default runtime session after backend redetect failed")
    await ctx.send({
        "event": "ollama_probe_result",
        "ok": bool(ollama_info),
        "url": ctx.state.ollama_url,
        "models_count": len(ollama_info.get("models") or []),
    })
    await ctx.send(ctx.state.get_init_data())



async def _send_project_list(ctx: CommandContext, **extra: Any) -> None:
    """The payload the sidebar rebuilds itself from."""
    await ctx.send({
        "event": "project_registered",
        "recent_projects": ctx.state.project.get_recent_projects(),
        "playground_project": ctx.state.project.get_playground_project(),
        "all_sessions": ctx.state.project.list_all_sessions(),
        **extra,
    })


@command("rename_project")
async def _cmd_rename_project(ctx: CommandContext) -> None:
    """Give a sidebar project a custom display name."""
    path = str(ctx.msg.get("path") or "").strip()
    name = str(ctx.msg.get("name") or "").strip()
    try:
        label = await _in_executor(ctx.state.project.rename_project, path, name)
    except Exception as exc:
        await ctx.send_error(f"Couldn't rename project: {exc}")
        return
    await _send_project_list(ctx, path=path)
    await ctx.send({"event": "ui_notice", "message": f"Project renamed to {label}."})


@command("forget_project")
async def _cmd_forget_project(ctx: CommandContext) -> None:
    """Stop tracking a project in the sidebar.

    Removes the entry only. The folder and its sessions stay on disk, so
    re-opening it restores everything — which is why this is 'forget' and not
    'delete', and why it does not ask for confirmation.
    """
    path = str(ctx.msg.get("path") or "").strip()
    if not path:
        await ctx.send_error("Project path is required.")
        return
    try:
        removed = await _in_executor(ctx.state.project.forget_project, path)
    except Exception as exc:
        await ctx.send_error(f"Couldn't remove project: {exc}")
        return
    await _send_project_list(ctx, path=path)
    await ctx.send({
        "event": "ui_notice",
        "message": (
            "Removed from the sidebar. The folder and its sessions are untouched."
            if removed else "That project was not in the sidebar."
        ),
    })


@command("register_project")
async def _cmd_register_project(ctx: CommandContext) -> None:
    project_path = ctx.msg.get("path", "").strip()
    if not project_path:
        await ctx.send({"event": "error", "message": "Project path is required."})
        return
    try:
        resolved_project_path = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ctx.state.ensure_project_path(project_path),
        )
        registered_project_path = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ctx.state.project.register_project(resolved_project_path),
        )
        await ctx.send({
            "event": "project_registered",
            "path": registered_project_path,
            "recent_projects": ctx.state.project.get_recent_projects(),
            "playground_project": ctx.state.project.get_playground_project(),
            "all_sessions": ctx.state.project.list_all_sessions(),
        })
        await ctx.send({
            "event": "ui_notice",
            "message": f"Project added: {registered_project_path}",
        })
    except Exception as exc:
        logger.warning("register_project failed for %r: %s", project_path, exc)
        await ctx.send({
            "event": "error",
            "message": f"Couldn't add project folder: {exc}",
        })



@command("set_project")
async def _cmd_set_project(ctx: CommandContext) -> None:
    project_path = ctx.msg.get("path", "").strip()
    project_switch_id = str(ctx.msg.get("project_switch_id", "")).strip()
    if not project_path:
        await ctx.send({
            "event": "error",
            "message": "Project path is required.",
            "project_switch_id": project_switch_id,
        })
        return
    try:
        resolved_project_path = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ctx.state.ensure_project_path(project_path),
        )
        ctx.state.apply_project_context(resolved_project_path, refresh_index=True)
        # Reset backend + session
        ctx.state.backend = None
        ctx.state.backend_spec = None
        ctx.state.session = None
        ctx.state._first_message_sent = False
        ctx.state.costs.reset_session()
        # Re-detect backends
        await asyncio.get_event_loop().run_in_executor(None, ctx.state.detect_backends)
        if ctx.state.available_backends:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ctx.state.ensure_default_runtime_session(),
                )
            except Exception:
                logger.exception("default runtime session after project switch failed")
        init_data = ctx.state.get_init_data()
        init_data["project_switch_id"] = project_switch_id
        await ctx.send(init_data)
        await ctx.send({
            "event": "ui_notice",
            "message": f"Project ready: {resolved_project_path}",
            "project_switch_id": project_switch_id,
        })
    except Exception as exc:
        logger.warning("set_project failed for %r: %s", project_path, exc)
        await ctx.send({
            "event": "error",
            "message": f"Couldn't open project folder: {exc}",
            "project_switch_id": project_switch_id,
        })



@command("check_updates")
async def _cmd_check_updates(ctx: CommandContext) -> None:
    try:
        from resonant_client.updater import check_for_updates_now

        started = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: check_for_updates_now(silent=False),
        )
        await ctx.send({
            "event": "status_msg",
            "message": (
                "Update checker opened."
                if started
                else "Update checker is unavailable in this build."
            ),
        })
    except Exception as exc:
        logger.exception("check_updates failed")
        await ctx.send({
            "event": "error",
            "message": f"Failed to check for updates: {exc}",
        })



@command("save_diagnostics")
async def _cmd_save_diagnostics(ctx: CommandContext) -> None:
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
        await ctx.send({
            "event": "diagnostics_saved",
            "path": str(zip_path),
            "size_bytes": size_bytes,
        })
    except Exception as exc:
        logger.exception("save_diagnostics failed")
        await ctx.send({
            "event": "error",
            "message": f"Failed to save diagnostics: {exc}",
        })



@command("folder_dialog")
async def _cmd_folder_dialog(ctx: CommandContext) -> None:
    # Open native folder picker via pywebview (or tkinter fallback). Always
    # acknowledge the click — silent failure was a real UX issue surfaced
    # in the dogfood test (the user clicks "Open another project..." and
    # has no idea whether it worked).
    start_dir = (ctx.msg.get("directory") or "").strip()
    if not start_dir or not os.path.isdir(start_dir):
        start_dir = ctx.state.project.project_path if ctx.state.project else ""

    # v0.5.6a4 fast-path, restored (regressed in v0.6.8): in
    # browser mode there is no pywebview window, and a tkinter
    # dialog opens on the SERVER's display — invisible to a
    # remote user — while the awaited executor call blocks this
    # socket's message loop until someone dismisses it on the
    # host. Route browser users to the in-page path modal.
    from . import app as _gui_app
    if getattr(_gui_app, "_webview_window", None) is None:
        await ctx.send({
            "event": "folder_picker_unavailable",
            "message": (
                "Native folder picker isn't available in "
                "browser mode — type the project path "
                "directly."
            ),
        })
        return

    await ctx.send({"event": "ui_notice", "message": "Opening project picker..."})

    def _pick_folder():
        # Read late and by attribute: server.py assigns this on the app module
        # after import (`_gui_app._webview_window = window`), so a from-import
        # would capture None forever and silently route every desktop user to
        # the browser fallback.
        from . import app as _gui_app
        window = getattr(_gui_app, "_webview_window", None)
        if window:
            try:
                import webview
                result = window.create_file_dialog(
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
        await ctx.send({"event": "folder_picked", "path": picked_path})
    elif picker_opened:
        await ctx.send({"event": "ui_notice", "message": "Project picker closed."})
    else:
        # No pick — tell the user what to do next so the click isn't a dead end.
        await ctx.send({
            "event": "folder_picker_unavailable",
            "message": (
                "Couldn't open the native folder picker. "
                "Type the project path in the workspace folder field instead, "
                "or pick from the Recent list."
            ),
        })



@command("list_dirs")
async def _cmd_list_dirs(ctx: CommandContext) -> None:
    # List subdirectories for folder browsing
    parent = ctx.msg.get("path", "").strip()
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
        await ctx.send({"event": "dir_list", "path": parent, "dirs": dirs})
    except Exception as e:
        await ctx.send({"event": "dir_list", "path": parent, "dirs": [], "error": str(e)})



@command("approve")
async def _cmd_approve(ctx: CommandContext) -> None:
    ctx.state.permission_result[0] = ctx.msg.get("approved", True)
    ctx.state.permission_response.set()



@command("choice_select")
async def _cmd_choice_select(ctx: CommandContext) -> None:
    ctx.state.choice_result[0] = ctx.msg.get("selected", "")
    ctx.state.choice_response.set()



@command("user_input")
async def _cmd_user_input(ctx: CommandContext) -> None:
    # v0.3.5 — reply path for the await_user tool. The agent
    # is blocked inside `on_user_input` waiting for this
    # event. The empty-string sentinel is "user closed the
    # modal without answering" — agent receives empty and
    # decides what to do with it.
    ctx.state.user_input_result[0] = ctx.msg.get("response", "")
    ctx.state.user_input_response.set()
    await ctx.send({
        "event": "user_input_received",
        "response": ctx.state.user_input_result[0],
    })

# ── Settings ────────────────────────────────────



@command("update_settings")
async def _cmd_update_settings(ctx: CommandContext) -> None:
    section = ctx.msg.get("section", "")
    key = ctx.msg.get("key")
    value = ctx.msg.get("value")
    clear_secret = bool(ctx.msg.get("clear_secret", False))
    data = ctx.state.update_setting_value(
        section,
        key,
        value,
        clear_secret=clear_secret,
    )
    await ctx.send({"event": "settings", "data": data})
    await ctx.send(ctx.state.get_init_data(refresh_only=True))

# ── Cost Tracking ───────────────────────────────



@command("skill_archive")
async def _cmd_skill_archive(ctx: CommandContext) -> None:
    from ..orchestration.skills import archive_skill, load_skill
    skill_id = (ctx.msg.get("skill_id") or "").strip()
    reason = (ctx.msg.get("reason") or "manual archive via GUI").strip()
    project_path = ctx.state.project.project_path if ctx.state.project else ""
    try:
        s = load_skill(skill_id, project_path=project_path)
        if s is None:
            await ctx.send({"event": "skill_error", "message": f"skill {skill_id!r} not found"})
        elif s.created_by == "bundled":
            await ctx.send({"event": "skill_error", "message": "Refused: bundled skills cannot be archived"})
        elif s.created_by == "user":
            await ctx.send({"event": "skill_error", "message": "Refused: user-provenance skills cannot be archived (unpin first)"})
        elif s.pinned:
            await ctx.send({"event": "skill_error", "message": "Refused: pinned skills cannot be archived (unpin first)"})
        else:
            scope_kw = project_path if s.scope == "project" else None
            archive_skill(s, project_path=scope_kw, reason=reason)
            await ctx.send({"event": "skill_archived", "skill_id": skill_id})
            await ctx.send(_skill_list_payload(project_path=project_path))
    except Exception as exc:
        await ctx.send({"event": "skill_error", "message": f"archive failed: {exc}"})


# ---------------------------------------------------------------------------
# Payload builders
#
# Moved here with the commands that use them. They were top-level functions in
# app.py serving only these handlers, so leaving them behind would have split
# one concern across two files for no reason.
# ---------------------------------------------------------------------------

def _git_run(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a git command and return (returncode, stdout).

    `cwd` is required in practice. It used to fall back to the module-level
    AppState singleton in app.py, which made these helpers untestable and
    silently tied "which repository" to global state — the caller always knew
    the project path and now has to say so.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=15,
            cwd=cwd or os.getcwd(),
            shell=(sys.platform == "win32"),
            **background_process_kwargs(),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _git_status(project_path: str) -> dict:
    """Get git status for the given project."""
    cwd = project_path

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


def _git_quick(action: str, msg: dict, project_path: str) -> dict:
    """Execute quick git actions."""
    cwd = project_path

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
