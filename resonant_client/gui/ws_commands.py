"""Registry of self-contained WebSocket command handlers.

`websocket_endpoint` in `gui/app.py` grew into a ~2,600-line function whose
dispatch is a single ~50-branch `elif command == "..."` chain. Every branch —
including the two dozen that just read some state and send one JSON reply —
shares the endpoint's entire local scope, so none of them can be read, tested,
or changed in isolation.

This module holds the handlers that genuinely need nothing from that scope: a
request comes in, state is read, a reply goes out. They receive an explicit
`CommandContext` instead of closing over the endpoint's locals, which makes
them directly unit-testable with a stub socket.

Handlers still in `app.py` are the ones entangled with the run loop — they
start/stop the chat runner, rebuild the backend, mutate the session, or drive
the autonomous daemon. Those need real untangling, not relocation, and moving
them mechanically would only hide the coupling behind an indirection.

To add a handler here it must:

  * read only `ctx` (socket, app state, message, chat runner),
  * not mutate loop state in `websocket_endpoint`, and
  * finish in one request/response exchange.

Anything else belongs in the endpoint until the coupling is designed away.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..engine.session import inspect_system_instructions


@dataclass(slots=True)
class CommandContext:
    """Everything a self-contained command handler is allowed to touch."""

    ws: Any
    state: Any
    msg: dict[str, Any]
    # Read-only view of the in-flight chat task. Present because a couple of
    # handlers must refuse to run while a turn is streaming; a handler must
    # never start or replace it.
    chat_runner: Any = None

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
        if ctx.chat_runner is not None and not ctx.chat_runner.done():
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
    except (TypeError, ValueError, RuntimeError) as exc:
        await ctx.send_error(str(exc))
