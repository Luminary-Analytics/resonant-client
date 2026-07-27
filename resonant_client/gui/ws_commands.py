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
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..engine.session import inspect_system_instructions
from ..processes import background_process_kwargs
from .project_instructions import find_instruction_file
from .settings import SettingsManager


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
