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
import shutil
import subprocess
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

PERMISSION_MODES = frozenset({"ask", "auto-edit", "plan", "bypass"})


def _is_connection_closed(exc: BaseException) -> bool:
    """Whether this exception means "the client is gone", not "we have a bug".

    Starlette does not raise a dedicated type for a send after close — it
    surfaces the raw ASGI protocol complaint — so the text is what identifies
    it. Matched narrowly on purpose: swallowing every send failure would hide
    real serialisation errors behind a silent no-op.
    """
    from starlette.websockets import WebSocketDisconnect

    if isinstance(exc, (WebSocketDisconnect, ConnectionResetError, BrokenPipeError)):
        return True
    text = str(exc).lower()
    return (
        "websocket.close" in text
        or "after sending 'websocket.close'" in text
        or "connection is closed" in text
        or "websocket is disconnected" in text
        or "unexpected asgi message" in text
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

    # Set once this connection is known to be gone, so the remaining sends in a
    # multi-message handler are skipped instead of each raising in turn.
    _closed: bool = False

    async def send(self, payload: dict[str, Any]) -> None:
        """Send to the client, tolerating a connection that has already gone.

        Writing to a closed WebSocket raises out of the handler, unwinds into
        the endpoint's receive loop, and ends the connection for good. That is
        how a single recoverable failure became "clicking sessions does
        nothing": a project switch hit an unreachable provider, the handler
        tried to *report* it, the report hit a socket the client had already
        dropped, and the endpoint died. The frontend reconnected and repeated.

        Failing to deliver a message is not worth losing the connection over —
        the client is gone either way. Anything that is not a closed connection
        still propagates, so a serialisation bug is not hidden behind this.
        """
        if self._closed:
            return
        try:
            await self.ws.send_json(payload)
        except Exception as exc:
            if not _is_connection_closed(exc):
                raise
            self._closed = True
            logger.debug("Dropping WS payload; client already disconnected", exc_info=True)

    async def send_error(self, message: str) -> None:
        await self.send({"event": "error", "message": message})

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


async def _block_active_navigation(ctx: CommandContext) -> bool:
    """The native chat loop owns one active workspace until its turn ends."""
    if ctx.runs is None or not ctx.runs.busy:
        return False
    await ctx.send({"event": "ui_notice", "message":
                    "Finish or stop the current run before changing projects or sessions. Your work is retained."})
    payload = ctx.state.get_init_data(refresh_only=True)
    payload["project_switch_id"] = str(ctx.msg.get("project_switch_id", ""))
    await ctx.send(payload)
    return True


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
        record = state.project.current_session
        if record:
            snapshot = record.history_snapshot()
            history_page = snapshot["page"]
            display_events = history_page["events"]
            projections = snapshot["projections"]
        else:
            display_events = list(data.get("display_events") or [])[-240:]
            history_page = {
                "events": display_events,
                "start_seq": None,
                "end_seq": None,
                "has_more": len(data.get("display_events") or []) > len(display_events),
                "total_events": len(data.get("display_events") or []),
                "as_of_seq": -1,
            }
            projections = {}
        public_data = {
            key: value for key, value in data.items()
            if key not in {"conversation_history", "display_events"}
        }
        await ctx.send({
            "event": "session.timeline_restored",
            "data": public_data,
            "display_events": display_events,
            "history_page": history_page,
            "projections": projections,
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
    # The open project's packs, approved or not, so Settings can show what
    # each would run before the user decides.
    try:
        payload = await _in_executor(ctx.state.capability_pack_payload)
    except Exception as exc:
        logger.warning("capability_pack_list failed", exc_info=True)
        await ctx.send_error(f"Couldn't list capability packs: {exc}")
        return
    await ctx.send(payload)


async def _set_capability_pack_approval(ctx: CommandContext, *, approve: bool) -> None:
    from ..engine.capability_packs import CapabilityPackError

    msg = ctx.msg
    try:
        payload = await _in_executor(
            lambda: ctx.state.set_capability_pack_approval(
                str(msg.get("pack_id") or ""),
                str(msg.get("path") or ""),
                digest=str(msg.get("digest") or ""),
                approve=approve,
            )
        )
    except CapabilityPackError as exc:
        # Redraw what is on disk now (a changed pack has a new digest to
        # review) and say why nothing was approved.
        payload = await _in_executor(ctx.state.capability_pack_payload)
        payload["error"] = str(exc)
        await ctx.send(payload)
        return
    await ctx.send(payload)
    await ctx.send({
        "event": "ui_notice",
        "message": (
            "Capability pack approved. Its hooks, skills, agents, MCP servers and model providers are active."
            if approve else
            "Capability pack approval revoked. Its hooks and MCP servers are off."
        ),
    })


@command("capability_pack_approve")
async def _capability_pack_approve(ctx: CommandContext) -> None:
    await _set_capability_pack_approval(ctx, approve=True)


@command("capability_pack_revoke")
async def _capability_pack_revoke(ctx: CommandContext) -> None:
    await _set_capability_pack_approval(ctx, approve=False)


@command("capability_pack_install")
async def _capability_pack_install(ctx: CommandContext) -> None:
    """Install a pack from a git repository pinned to one commit; approval stays separate."""
    from ..engine.pack_install import PackInstallError

    msg = ctx.msg
    try:
        payload, installed = await _in_executor(lambda: ctx.state.install_capability_pack(
            str(msg.get("url") or ""), str(msg.get("ref") or ""), str(msg.get("subdir") or "")))
    except PackInstallError as exc:
        payload = await _in_executor(ctx.state.capability_pack_payload)
        payload["error"] = str(exc)
        await ctx.send(payload)
        return
    await ctx.send(payload)
    await ctx.send({"event": "ui_notice", "message": (
        f"Installed {installed['name']} at {installed['commit'][:12]}. It's off until you review "
        "what it would run and approve it.")})


@command("capability_pack_remove")
async def _capability_pack_remove(ctx: CommandContext) -> None:
    from ..engine.pack_install import PackInstallError

    try:
        payload = await _in_executor(lambda: ctx.state.remove_capability_pack(str(ctx.msg.get("pack_id") or "")))
    except PackInstallError as exc:
        payload = await _in_executor(ctx.state.capability_pack_payload)
        payload["error"] = str(exc)
        await ctx.send(payload)
        return
    await ctx.send(payload)
    await ctx.send({"event": "ui_notice", "message": "Capability pack removed."})


@command("audit_status")
async def _audit_status(ctx: CommandContext) -> None:
    # Where the audit log is, whether its hash chain verifies, and export health.
    await ctx.send(await _in_executor(ctx.state.audit_status))


def _code_editors_payload(settings: Any = None, **extra: Any) -> dict:
    """Settings > Code editors: the bridge, and the editors found on this computer."""
    from ..code_editors import jetbrains_config_dirs, lumi_command, vscode_editors
    from .editor_bridge import bridge, enabled

    program, prefix = lumi_command()
    start = " ".join(part for part in (f'"{program}"' if " " in program else program, prefix) if part)
    return {"event": "code_editors", "data": {
        "enabled": enabled(settings),
        "running": bridge.published,
        "vscode": [{"command": item["command"], "name": item["name"]} for item in vscode_editors()],
        "jetbrains": [path.name for path in jetbrains_config_dirs()],
        "commands": {"vscode": f"{start} editor vscode --install", "jetbrains": f"{start} editor jetbrains --install"},
        **extra,
    }}


@command("code_editors_list")
async def _code_editors_list(ctx: CommandContext) -> None:
    settings = getattr(ctx.state, "settings", None)
    await ctx.send(await _in_executor(lambda: _code_editors_payload(settings)))


@command("code_editor_install")
async def _code_editor_install(ctx: CommandContext) -> None:
    """Install the VS Code extension, or JetBrains External Tools, from Settings > Code editors."""
    from ..code_editors import VSCODE_FAMILY, install_jetbrains, install_vscode
    from .editor_bridge import enabled

    settings = getattr(ctx.state, "settings", None)
    target = str(ctx.msg.get("target") or "")
    editor = str(ctx.msg.get("editor") or "code")

    def install() -> dict:
        if not enabled(settings):
            return {"ok": False, "message": "Turn on Code editors in Settings > Privacy & security first."}
        if target == "vscode":
            install_vscode(editor)
            return {"ok": True, "message": f"Installed in {VSCODE_FAMILY[editor]}. If it is open, run "
                                           "\"Developer: Reload Window\" there to start the extension."}
        if target == "jetbrains":
            written = install_jetbrains()
            if not written:
                return {"ok": False, "message": "No JetBrains IDE settings were found on this computer."}
            names = ", ".join(path.parent.parent.name for path in written)
            return {"ok": True, "message": f"Added to {names}. Restart an IDE that is open to see the tools."}
        return {"ok": False, "message": "Choose VS Code or JetBrains."}

    try:
        result = await _in_executor(install)
    except (RuntimeError, ValueError, OSError) as exc:
        result = {"ok": False, "message": str(exc)}
    await ctx.send(await _in_executor(lambda: _code_editors_payload(settings, result=result)))


def _schedules_payload(settings: Any = None, **extra: Any) -> dict:
    from .. import schedules

    return {"event": "schedules", "data": {**schedules.overview(settings), **extra}}


# "Run now" processes being watched, so a finished run refreshes the page.
_SCHEDULE_RUNS: set[asyncio.Task] = set()


@command("schedules_list")
async def _schedules_list(ctx: CommandContext) -> None:
    settings = getattr(ctx.state, "settings", None)
    await ctx.send(await _in_executor(lambda: _schedules_payload(settings)))


@command("schedule_save")
async def _schedule_save(ctx: CommandContext) -> None:
    # Registering with the operating system's scheduler runs schtasks,
    # launchctl or crontab: off the event loop.
    from .. import schedules

    raw = ctx.msg.get("schedule")
    if not isinstance(raw, dict):
        await ctx.send_error("Send the schedule's fields.")
        return
    settings = getattr(ctx.state, "settings", None)
    try:
        saved = await _in_executor(lambda: schedules.save(raw, str(ctx.msg.get("id") or ""), settings=settings))
    except schedules.ScheduleError as exc:
        await ctx.send({"event": "schedule_error", "message": str(exc)})
        return
    await ctx.send(await _in_executor(lambda: _schedules_payload(settings, saved=saved.id)))


@command("schedule_change")
async def _schedule_change(ctx: CommandContext) -> None:
    """Run now, pause, resume or remove a schedule."""
    from .. import schedules

    schedule_id, action = str(ctx.msg.get("id") or ""), str(ctx.msg.get("action") or "")
    settings = getattr(ctx.state, "settings", None)
    try:
        if action == "remove":
            await _in_executor(lambda: schedules.remove(schedule_id))
        elif action in ("pause", "resume"):
            await _in_executor(lambda: schedules.set_enabled(schedule_id, action == "resume", settings=settings))
        elif action == "run":
            # Its own process, like a scheduled run, so a long task never
            # holds this connection and keeps going if the app closes.
            process = await _in_executor(lambda: schedules.start(schedule_id, settings=settings))

            async def refresh_when_done() -> None:
                await asyncio.to_thread(process.wait)
                await ctx.send(await _in_executor(lambda: _schedules_payload(settings)))

            task = asyncio.create_task(refresh_when_done())
            _SCHEDULE_RUNS.add(task)
            task.add_done_callback(_SCHEDULE_RUNS.discard)
        else:
            raise schedules.ScheduleError("Choose run, pause, resume or remove.")
    except schedules.ScheduleError as exc:
        await ctx.send({"event": "schedule_error", "message": str(exc)})
        return
    await ctx.send(await _in_executor(lambda: _schedules_payload(settings)))


def _model_evals_payload(**extra: Any) -> dict:
    from .. import model_evals

    return {"event": "model_evals", "data": {**model_evals.overview(), **extra}}


@command("model_evals_list")
async def _model_evals_list(ctx: CommandContext) -> None:
    await ctx.send(await _in_executor(_model_evals_payload))


@command("model_eval_save")
async def _model_eval_save(ctx: CommandContext) -> None:
    # Validation runs git in the project: off the event loop.
    from .. import model_evals

    raw = ctx.msg.get("comparison")
    if not isinstance(raw, dict):
        await ctx.send_error("Send the comparison's fields.")
        return
    try:
        saved = await _in_executor(lambda: model_evals.create(raw))
    except model_evals.EvalError as exc:
        await ctx.send({"event": "model_eval_error", "message": str(exc)})
        return
    await ctx.send(await _in_executor(lambda: _model_evals_payload(saved=saved.id)))


@command("model_eval_change")
async def _model_eval_change(ctx: CommandContext) -> None:
    """Run, stop or remove a comparison. Runs report progress as they finish."""
    from .. import model_evals

    comparison_id, action = str(ctx.msg.get("id") or ""), str(ctx.msg.get("action") or "")
    loop = asyncio.get_running_loop()

    def on_update() -> None:  # from the comparison's thread
        asyncio.run_coroutine_threadsafe(ctx.send(_model_evals_payload()), loop)

    try:
        if action == "run":
            await _in_executor(lambda: model_evals.runner.start(comparison_id, on_update))
        elif action == "stop":
            await _in_executor(model_evals.runner.stop)
        elif action == "remove":
            await _in_executor(lambda: model_evals.remove(comparison_id))
        else:
            raise model_evals.EvalError("Choose run, stop or remove.")
    except model_evals.EvalError as exc:
        await ctx.send({"event": "model_eval_error", "message": str(exc)})
        return
    await ctx.send(await _in_executor(_model_evals_payload))


@command("project_trust_list")
async def _project_trust_list(ctx: CommandContext) -> None:
    # The open project's status plus every remembered decision, for Settings.
    await ctx.send(await _in_executor(ctx.state.project_trust_payload))


@command("project_trust_set")
async def _project_trust_set(ctx: CommandContext) -> None:
    # Trust, restrict or forget a project. Only the user's own click reaches
    # this: repository content cannot trust itself.
    decision = str(ctx.msg.get("decision") or "")
    if decision not in {"trusted", "restricted", "forget"}:
        await ctx.send_error("Choose to trust or restrict the project.")
        return
    if ctx.runs.busy:
        await ctx.send_error("Finish or stop the current run before changing project trust.")
        return
    path = str(ctx.msg.get("project_path") or "")
    try:
        payload = await _in_executor(lambda: ctx.state.set_project_trust(decision, path))
    except ValueError as exc:
        await ctx.send_error(str(exc))
        return
    await ctx.send(payload)


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

    if getattr(ctx.state, "mcp_manager", None) is not None:
        await _editor_list(ctx)


@command("get_costs")
async def _get_costs(ctx: CommandContext) -> None:
    def gather() -> dict:
        from datetime import datetime, timezone

        from .. import activity, pricing, usage

        # This month's calls from the usage records, and the prices in effect.
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        rows = usage.ledger().records(since=month)
        from .. import budgets

        totals = usage.totals(rows)
        done = activity.month_summary()
        return {
            **ctx.state.costs.get_all_costs(),
            "budgets": budgets.status(getattr(getattr(ctx.state, "project", None), "project_path", "") or ""),
            "month": {"period": month, **totals, "by_model": usage.breakdown(rows, "model"),
                      "by_purpose": usage.breakdown(rows, "purpose"), "activity": done,
                      # Turns with a passing check_run: what a verified task costs.
                      "cost_per_verified_task": (round(float(totals.get("cost_usd") or 0) / done["verified"], 4)
                                                 if done["verified"] else None)},
            "pricing": pricing.describe_catalog(),
        }

    await ctx.send({"event": "costs", "data": await _in_executor(gather)})


@command("set_permission_mode")
async def _set_permission_mode(ctx: CommandContext) -> None:
    # No default: the backends treat an unrecognised mode as Full-auto, so a
    # missing or misspelled mode must not reach apply_permission_mode.
    mode = ctx.msg.get("mode")
    if not isinstance(mode, str) or mode not in PERMISSION_MODES:
        await ctx.send_error("Choose a permission mode: ask, auto-edit, plan or bypass.")
        return
    from ..policy import current as current_policy

    policy = current_policy()
    if policy and not policy.mode_allowed(mode):
        await ctx.send_error(f"{policy.organization}'s policy doesn't allow this permission mode.")
        return
    ctx.state.apply_permission_mode(mode)


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


@command("editor_list")
async def _editor_list(ctx: CommandContext) -> None:
    from ..engine.editor_integrations import catalog
    await ctx.send({"event": "editor_integrations", "editors": catalog(ctx.state.settings, ctx.state.mcp_manager)})


@command("editor_connect")
async def _editor_connect(ctx: CommandContext) -> None:
    """Configure one known bridge and discover its actual tool surface."""
    from ..engine.editor_integrations import build_config, catalog, configured_editors, server_name
    editor = str(ctx.msg.get("editor") or "")
    action = str(ctx.msg.get("action") or "connect")
    message = ""
    try:
        if ctx.runs is not None and ctx.runs.busy:
            raise ValueError("Stop the active run before changing editor connections.")
        name = server_name(editor)
        existing = (ctx.state.settings.get("mcp_servers") or {}).get(name)
        if existing and name not in configured_editors(ctx.state.settings):
            raise ValueError("That reserved server name is already in use. Rename it in MCP settings first.")
        if action == "disconnect":
            await asyncio.to_thread(ctx.state.mcp_manager.disconnect, name)
            if existing:
                ctx.state.settings.set("mcp_servers", name, {**existing, "enabled": False})
            message = "Disconnected. Editor tools are disabled for subsequent turns."
        elif action == "connect":
            config = await asyncio.to_thread(build_config, editor, str(ctx.msg.get("value") or ""))
            ctx.state.settings.set("mcp_servers", name, config)
            success = await asyncio.to_thread(ctx.state.mcp_manager.connect, name)
            message = ("Bridge connected. Use Check editor to verify the open scene."
                       if success else "Could not connect. Check the setup steps and start the editor bridge.")
        else:
            raise ValueError("Unknown editor connection action")
        if ctx.state.session:
            ctx.state.session.mcp_tools = ctx.state.mcp_manager.get_all_tools()
        ctx.state._intent_service = None
    except (ValueError, OSError) as exc:
        message = str(exc)
    await ctx.send({"event": "editor_integrations", "editors": catalog(ctx.state.settings, ctx.state.mcp_manager),
                    "editor": editor, "message": message})
    await ctx.send({"event": "mcp_list", "servers": ctx.state.mcp_manager.list_servers()})
    await ctx.send({"event": "settings", "data": ctx.state.settings.get_masked()})


@command("editor_check")
async def _editor_check(ctx: CommandContext) -> None:
    """Run only the catalog's read-only scene probe, never arbitrary UI-supplied code."""
    from ..engine.editor_integrations import EDITORS, server_name
    from ..engine.mcp import normalize_tool_result
    editor = str(ctx.msg.get("editor") or "")
    try:
        if ctx.runs is not None and ctx.runs.busy:
            raise ValueError("Wait for the active run to finish before checking the editor.")
        name = server_name(editor)
        tool, args = EDITORS[editor]["probe"]
        result = await asyncio.to_thread(ctx.state.mcp_manager.call_tool, f"mcp_{name}_{tool}", args)
        output, _ = normalize_tool_result(result)
        # The raw reply is evidence, not a permanent 'healthy' badge. Some
        # bridges return domain errors in successful MCP text envelopes.
        await ctx.send({"event": "editor_check", "editor": editor, "output": output[:12000],
                        "is_error": bool(result.get("error") or result.get("isError"))})
    except (ValueError, OSError) as exc:
        await ctx.send({"event": "editor_check", "editor": editor, "output": str(exc), "is_error": True})


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


def _saved_session(ctx: CommandContext, target_id: str, project_path: str):
    """A saved conversation by id, from ``project_path`` or any recent project; None when there's none."""
    from .sessions import SessionRecord, _sessions_dir, is_valid_session_id

    if not is_valid_session_id(target_id):
        return None
    record_project_path = project_path
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
                record_project_path = candidate_root
                break
    if not path.exists():
        return None
    record = SessionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    record.project_path = record_project_path
    if record.ledger.path.exists():
        record.load_ledger()
    return record


@command("get_session_replay_events")
async def _get_session_replay_events(ctx: CommandContext) -> None:
    """Fetch a session's display events without switching the active one."""
    target_id = ctx.msg.get("session_id", "")
    project_path = ctx.msg.get("project_path") or ctx.project_path
    try:
        record = _saved_session(ctx, target_id, project_path)
        if record is None:
            await ctx.send({
                "event": "session_replay_events", "session_id": target_id,
                "error": "not found", "events": [],
            })
            return
        await ctx.send({
            "event": "session_replay_events",
            "session_id": target_id,
            "title": record.title or "",
            "events": record.display_events,
        })
    except Exception as exc:
        await ctx.send({
            "event": "session_replay_events", "session_id": target_id,
            "error": str(exc), "events": [],
        })


async def _share_state(ctx: CommandContext, session_id: str, **extra: Any) -> None:
    """The Share dialog: this conversation's link, and where it can be shared."""
    from .. import share

    status = await asyncio.to_thread(ctx.state.cloud.status)
    account = status.get("account") or {}
    await ctx.send({"event": "session_share", "session_id": session_id,
                    "share": share.remembered().get(session_id), "signed_in": bool(status.get("signed_in")),
                    "organizations": [{"id": org.get("id"), "name": org.get("name")}
                                      for org in account.get("organizations") or []], **extra})


@command("session_share_status")
async def _session_share_status(ctx: CommandContext) -> None:
    await _share_state(ctx, str(ctx.msg.get("session_id") or ""))


@command("session_share")
async def _session_share(ctx: CommandContext) -> None:
    """Share a read-only copy of a saved conversation in Lumi Cloud (lumi/share.py)."""
    from .. import share
    from ..cloud import CloudError

    session_id = str(ctx.msg.get("session_id") or "")
    project_path = str(ctx.msg.get("project_path") or ctx.project_path or "")

    def work() -> None:
        record = _saved_session(ctx, session_id, project_path)
        if record is None:
            raise CloudError("That conversation isn't saved yet.")
        copy = share.export(record.display_events, title=record.title, project_path=record.project_path,
                            model=str(record.model or ""))
        if not copy["entries"]:
            raise CloudError("There's nothing in this conversation to share yet.")
        answer = share.share(ctx.state.cloud, copy, organization_id=str(ctx.msg.get("organization_id") or ""),
                             visibility=str(ctx.msg.get("visibility") or "organization"))
        share.remember(session_id, answer)

    try:
        await asyncio.to_thread(work)
        await _share_state(ctx, session_id)
    except CloudError as exc:
        await _share_state(ctx, session_id, error=str(exc))


# ── Hand-offs (lumi/handoff.py) ──────────────────────────────────────────


async def _handoff_state(ctx: CommandContext, session_id: str, **extra: Any) -> None:
    """The Hand off dialog: where the work is, and who in the person's organizations it can go to."""
    from .. import handoff

    project_path = str(ctx.msg.get("project_path") or ctx.project_path or "")

    def gather() -> dict:
        status = ctx.state.cloud.status()
        record = _saved_session(ctx, session_id, project_path)
        repo = handoff.repo_state(record.project_path) if record else {}
        organizations, error = [], ""
        if status.get("signed_in"):
            for org in (status.get("account") or {}).get("organizations") or []:
                try:
                    people = handoff.recipients(ctx.state.cloud, str(org.get("id") or ""))
                except handoff.HandoffError as exc:
                    people, error = [], str(exc)
                organizations.append({"id": org.get("id"), "name": org.get("name"), "recipients": people})
        return {"signed_in": bool(status.get("signed_in")), "organizations": organizations,
                "saved": record is not None, "repo": repo, "repo_text": handoff.describe_repo(repo),
                **({"error": error} if error else {})}

    await ctx.send({"event": "session_handoff", "session_id": session_id, **await asyncio.to_thread(gather),
                    **extra})


@command("session_handoff_status")
async def _session_handoff_status(ctx: CommandContext) -> None:
    await _handoff_state(ctx, str(ctx.msg.get("session_id") or ""))


@command("session_handoff")
async def _session_handoff(ctx: CommandContext) -> None:
    """Hand a saved conversation to a teammate through Lumi Cloud, or save it in the project for a CI run."""
    from .. import handoff

    session_id = str(ctx.msg.get("session_id") or "")
    project_path = str(ctx.msg.get("project_path") or ctx.project_path or "")
    target = "ci" if ctx.msg.get("target") == "ci" else "teammate"

    def work() -> dict:
        record = _saved_session(ctx, session_id, project_path)
        if record is None:
            raise handoff.HandoffError("That conversation isn't saved yet.")
        account = ctx.state.cloud.status().get("account") or {}
        # A file for CI is committed to the repository: it names the sender, without an email address.
        name = str(account.get("name") or ctx.state.settings.get("general", "display_name", "") or "")
        data = handoff.package(record.display_events, title=record.title, project_path=record.project_path,
                               model=str(record.model or ""), note=str(ctx.msg.get("note") or ""),
                               sender={"name": name} if name else {})
        if target == "ci":
            path = handoff.save_for_ci(record.project_path, data)
            relative = path.relative_to(record.project_path).as_posix()
            return {"kind": "ci", "path": relative, "command": f"lumi run --handoff {relative}"}
        answer = handoff.send(ctx.state.cloud, data, organization_id=str(ctx.msg.get("organization_id") or ""),
                              to=str(ctx.msg.get("to") or ""))
        return {"kind": "teammate", "id": answer.get("id"), "to": answer.get("to") or {}}

    try:
        result = await asyncio.to_thread(work)
    except (handoff.HandoffError, OSError) as exc:
        await _handoff_state(ctx, session_id, error=str(exc))
        return
    await ctx.send({"event": "session_handoff_done", "session_id": session_id, **result})


async def _handoffs_inbox(ctx: CommandContext, **extra: Any) -> None:
    """Hand-offs waiting for this person, each with the recent project that holds its repository."""
    from .. import handoff

    def gather() -> dict:
        if not ctx.state.cloud.status().get("signed_in"):
            return {"signed_in": False, "to_me": [], "from_me": []}
        try:
            box = handoff.inbox(ctx.state.cloud)
        except handoff.HandoffError as exc:
            return {"signed_in": True, "to_me": [], "from_me": [], "error": str(exc)}
        projects = [str(p.get("path") if isinstance(p, dict) else p) for p in ctx.state.project.get_recent_projects()]
        for item in box["to_me"]:
            item["suggested_project"] = handoff.suggest_project(item.get("repo") or {}, projects)
        return {"signed_in": True, **box}

    await ctx.send({"event": "handoffs", **await asyncio.to_thread(gather), **extra})


@command("handoffs_inbox")
async def _cmd_handoffs_inbox(ctx: CommandContext) -> None:
    await _handoffs_inbox(ctx)


@command("handoff_check")
async def _cmd_handoff_check(ctx: CommandContext) -> None:
    """Whether a folder holds a hand-off's work (its repository, branch and commit)."""
    from .. import handoff

    repo = ctx.msg.get("repo") if isinstance(ctx.msg.get("repo"), dict) else {}
    project_path = str(ctx.msg.get("project_path") or "")
    result = await asyncio.to_thread(handoff.check_folder, repo, project_path) if os.path.isdir(project_path) \
        else {"ok": False, "message": "Choose a folder that exists."}
    await ctx.send({"event": "handoff_check", "id": str(ctx.msg.get("id") or ""), "project_path": project_path,
                    **result})


@command("handoff_pick_up")
async def _cmd_handoff_pick_up(ctx: CommandContext) -> None:
    """Take a hand-off, keep it locally, and give the app the first message of a new conversation."""
    from .. import handoff

    handoff_id = str(ctx.msg.get("id") or "")
    project_path = str(ctx.msg.get("project_path") or "")
    if not os.path.isdir(project_path):
        await _handoffs_inbox(ctx, error="Choose a folder that exists.")
        return
    try:
        data = await asyncio.to_thread(handoff.pick_up, ctx.state.cloud, handoff_id)
    except handoff.HandoffError as exc:
        await _handoffs_inbox(ctx, error=str(exc))
        return
    sender = data["from"]["name"] or data["from"]["email"] or "your teammate"
    await ctx.send({"event": "handoff_picked_up", "id": handoff_id, "project_path": project_path,
                    "title": data["title"],
                    "draft": f"@handoff:{handoff_id} Continue the work {sender} handed off: {data['title']}."})


@command("handoff_close")
async def _cmd_handoff_close(ctx: CommandContext) -> None:
    """Dismiss a hand-off sent to you, or withdraw one you sent, before anyone picks it up."""
    from .. import handoff

    try:
        await asyncio.to_thread(handoff.close, ctx.state.cloud, str(ctx.msg.get("id") or ""))
    except handoff.HandoffError as exc:
        await _handoffs_inbox(ctx, error=str(exc))
        return
    await _handoffs_inbox(ctx)


@command("session_share_stop")
async def _session_share_stop(ctx: CommandContext) -> None:
    from .. import share
    from ..cloud import CloudError

    session_id = str(ctx.msg.get("session_id") or "")
    shared = share.remembered().get(session_id) or {}
    try:
        if shared.get("id"):
            await asyncio.to_thread(share.stop, ctx.state.cloud, str(shared["id"]))
        share.remember(session_id, None)
        await _share_state(ctx, session_id)
    except CloudError as exc:
        await _share_state(ctx, session_id, error=str(exc))


@command("get_session_history_page")
async def _get_session_history_page(ctx: CommandContext) -> None:
    """Load one older display-event page without changing active runtime state."""
    target_id = str(ctx.msg.get("session_id") or "")
    if not target_id:
        await ctx.send({
            "event": "session_history_page",
            "session_id": "",
            "error": "session_id is required",
            "page": {"events": [], "has_more": False},
        })
        return
    try:
        before_raw = ctx.msg.get("before_seq")
        before_seq = int(before_raw) if before_raw is not None else None
        limit = int(ctx.msg.get("limit") or 240)
    except (TypeError, ValueError):
        await ctx.send({
            "event": "session_history_page",
            "session_id": target_id,
            "error": "invalid paging cursor",
            "page": {"events": [], "has_more": False},
        })
        return
    record = ctx.state.project.load_session(
        target_id, activate=False, hydrate=False
    )
    if record is None:
        await ctx.send({
            "event": "session_history_page",
            "session_id": target_id,
            "error": "not found",
            "page": {"events": [], "has_more": False},
        })
        return
    snapshot = record.history_snapshot(before_seq=before_seq, limit=limit)
    await ctx.send({
        "event": "session_history_page",
        "session_id": target_id,
        "page": snapshot["page"],
        "projections": snapshot["projections"],
    })


@command("open_workspace_path")
async def _open_workspace_path(ctx: CommandContext) -> None:
    """Open an existing project file explicitly selected by the user."""
    raw = str(ctx.msg.get("path") or "").strip()
    if not raw:
        await ctx.send({"event": "status_msg", "message": "No file path was provided."})
        return
    try:
        root = Path(ctx.project_path).resolve(strict=True)
        requested = Path(raw)
        target = (requested if requested.is_absolute() else root / requested).resolve(
            strict=True
        )
        target.relative_to(root)
    except (OSError, ValueError):
        await ctx.send({
            "event": "status_msg",
            "message": "That file is unavailable or outside the active project.",
        })
        return

    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], **background_process_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(target)], **background_process_kwargs())
    except OSError as exc:
        await ctx.send({"event": "status_msg", "message": f"Could not open file: {exc}"})
        return
    await ctx.send({"event": "status_msg", "message": f"Opened {target.name}"})


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
    discovering = bool(getattr(ctx.state, "_discovery_pending", False))
    if not discovering and not ctx.state.backend and ctx.state.available_backends:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ctx.state.ensure_default_runtime_session(),
            )
        except Exception:
            logger.exception("default runtime session init failed")
    payload = ctx.state.get_init_data()
    payload["run_active"] = bool(ctx.runs and ctx.runs.busy)
    payload["run_started_at"] = (
        float(ctx.runs.started_at)
        if ctx.runs and ctx.runs.started_at is not None
        else None
    )
    payload["queued_messages"] = [
        {
            "message_id": str(item.get("message_id") or ""),
            "text": str(item.get("text") or ""),
            "position": index,
            "steering": False,
        }
        for index, item in enumerate((ctx.runs.pending if ctx.runs else []), start=1)
        if str(item.get("message_id") or "")
    ]
    record = getattr(getattr(ctx.state, "project", None), "current_session", None)
    if record is not None:
        try:
            # Reconnecting should restore the visible conversation immediately
            # without rebuilding an already-live model runtime. A switch_session
            # round trip did both extra work and, for the selected row, was never
            # sent by the frontend at all.
            snapshot = record.history_snapshot(hydrate=True)
            page = snapshot["page"]
            payload["current_display_events"] = page["events"]
            payload["current_history_page"] = page
            payload["current_projections"] = snapshot["projections"]
        except Exception:
            logger.warning("Unable to hydrate current session during init", exc_info=True)
    await ctx.send(payload)



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
        await ctx.send({
            "event": "error",
            "message": ctx.state.runtime_unavailable_reason(),
        })
        return
    await ctx.runs.enqueue(ctx.msg)
    return


@command('employee_task')
async def _employee_task(ctx: CommandContext) -> None:
    """Serialize control/advice with the current conversation's native execution."""
    from .employee_tasks import command as execute
    allowed = {'command', 'action', 'project', 'session_id', 'request_id', 'ceiling_microusd', 'descriptor', 'question', 'consultation_mode'}
    if set(ctx.msg) - allowed:
        await ctx.send_error('Unsupported employee task fields')
        return
    if ctx.msg.get('action', 'view') in ('view', 'graph'):
        if ctx.runs is not None and ctx.runs.busy:
            await ctx.send({'event': 'employee_task_state', 'project': ctx.project_path,
                'session_id': getattr(ctx.state.project.current_session, 'id', ''),
                'request_id': ctx.msg.get('request_id'), 'error': 'Refresh task details after the active operation completes.'})
            return
        await execute(ctx.state, ctx.send, ctx.msg)
        return
    if ctx.runs is None or ctx.runs.busy:
        await ctx.send({'event': 'employee_task_state', 'project': ctx.project_path,
            'session_id': getattr(ctx.state.project.current_session, 'id', ''),
            'request_id': ctx.msg.get('request_id'), 'error': 'Finish or stop the active operation before changing the task.'})
        return
    await ctx.runs.enqueue(dict(ctx.msg))



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
    active_session = getattr(ctx.state, "active_session", None) or ctx.state.session
    if active_session:
        active_session.cancel()
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
    if await _block_active_navigation(ctx):
        return
    # Create a new session (don't destroy old one)
    request_id = str(ctx.msg.get("request_id") or "").strip()
    if request_id and request_id in ctx.runs.clear_cache:
        await ctx.send(ctx.runs.clear_cache[request_id])
        return
    session_mode = ctx.msg.get("session_mode", "code")
    session_role = ctx.msg.get("session_role", "generator")
    draft_only = ctx.msg.get('draft_only') is True
    if draft_only and ctx.runs.busy:
        await ctx.send({'event': 'error', 'request_id': request_id,
                        'message': 'Let the current run finish, or stop it, before starting a new session.'})
        return
    prepared = None
    if draft_only:
        project_key = os.path.normcase(os.path.abspath(ctx.state.project.project_path))
        preference = ctx.state.settings.get("project_models", project_key, {}) or {}
        if preference.get("backend") and preference.get("model"):
            def prepare():
                spec = ctx.state.build_backend_spec(preference["backend"], preference["model"], project_path=ctx.state.project.project_path)
                backend = spec.create_backend(ctx.state.settings)
                session = ctx.state.build_session(backend=backend, backend_spec=spec,
                                                  project_path=ctx.state.project.project_path,
                                                  session_mode=session_mode, session_role=session_role)
                return backend, spec, session
            try:
                prepared = await asyncio.to_thread(prepare)
            except Exception as exc:
                await ctx.send({"event": "error", "request_id": request_id, "message": str(exc)})
                await ctx.send(ctx.state.get_init_data(refresh_only=True))
                return
    if draft_only:
        ctx.state.project.current_session = None
        ctx.state.session = None
        ctx.state._first_message_sent = False
        if prepared:
            ctx.state.backend, ctx.state.backend_spec, ctx.state.session = prepared
    if ctx.state.backend:
        backend_type = getattr(ctx.state.backend, "name", "")
        model = getattr(ctx.state.backend, "model", "")
        if not draft_only:
            ctx.state.project.create_session(
                backend_type=backend_type,
                model=model,
                session_role=session_role,
            )
        ctx.state.session = prepared[2] if prepared else ctx.state.build_session(
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
    if prepared:
        await ctx.send(ctx.state.get_init_data(refresh_only=True))




@command("switch_model")
async def _cmd_switch_model(ctx: CommandContext) -> None:
    if ctx.runs.busy:
        await ctx.send({"event": "model_switch_blocked", "message": "Finish or stop the current run before changing models."})
        await ctx.send(ctx.state.get_init_data(refresh_only=True))
        return
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
            if backend_type in ctx.state.CLI_WRAPPED_BACKENDS and backend_type != "codex":
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
            if ctx.msg.get("remember_project") is True:
                project_key = os.path.normcase(os.path.abspath(ctx.state.project.project_path))
                ctx.state.settings.set("project_models", project_key, {"backend": backend_type, "model": model})
                await ctx.send({"event": "settings", "data": ctx.state.settings.get_masked()})
            await ctx.send(ctx.state.get_init_data())
        except Exception as e:
            await ctx.send({"event": "error", "message": str(e)})
            # The selector optimistically shows the model the user picked, so
            # push authoritative state back or the UI keeps advertising a
            # backend that failed to start.
            await ctx.send(ctx.state.get_init_data(refresh_only=True))
    else:
        # No backend to switch to and none currently loaded. This used to fall
        # off the end of the handler silently: the user picked a model, the UI
        # said "connected", and the server did nothing at all.
        await ctx.send({
            "event": "error",
            "message": "Could not determine which provider to use for that model.",
        })



@command("set_thinking_mode")
async def _cmd_set_thinking_mode(ctx: CommandContext) -> None:
    # Per-session thinking-mode toggle (deepseek-v* etc.).
    # Forces a backend rebuild because Ollama options must be stable
    # for the lifetime of an OllamaBackend instance.
    mode = (ctx.msg.get("mode") or "").strip().lower()
    if mode not in {"", "default", "off", "low", "med", "medium", "high", "max"}:
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
            if ctx.state.backend_spec and ctx.state.backend_spec.backend_type == "kimi":
                normalized = "max" if mode in {"", "default"} else mode
                if normalized not in {"low", "high", "max"}:
                    raise ValueError("Kimi supports low, high, and max; thinking is always enabled.")
            if ctx.state.backend_spec and ctx.state.backend_spec.backend_type == "ollama":
                # Validate before persisting a setting that cannot be applied.
                from ..backends import OllamaBackend
                OllamaBackend("http://validation-only", ctx.state.backend_spec.model, thinking=normalized)
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



@command("preview_list")
@command("preview_stop")
async def _cmd_previews(ctx: CommandContext) -> None:
    from ..engine.previews import previews
    project = ctx.state.project.project_path
    try:
        if ctx.msg.get('command') == 'preview_stop':
            await asyncio.to_thread(previews.stop, project, ctx.msg.get('id', ''))
        await ctx.send({'event': 'previews_updated', 'project': project, 'previews': previews.list(project)})
    except (OSError, ValueError) as exc:
        await ctx.send({'event': 'error', 'message': str(exc)})


@command("memory_list")
@command("memory_save")
@command("memory_delete")
@command("memory_share")
async def _cmd_project_memory(ctx: CommandContext) -> None:
    from .. import team_library
    from ..engine.project_memory import ProjectMemory
    project = ctx.state.project.project_path
    memory = ProjectMemory(project)
    shared = ''
    try:
        if ctx.msg.get('command') == 'memory_save':
            memory.save(ctx.msg.get('text', ''), source=ctx.msg.get('source', ''),
                        kind=ctx.msg.get('kind', 'decision'), sources=ctx.msg.get('sources', []),
                        memory_id=ctx.msg.get('id', ''), author='user')
        elif ctx.msg.get('command') == 'memory_delete':
            memory.delete(ctx.msg.get('id', ''))
        elif ctx.msg.get('command') == 'memory_share':
            note = next((n for n in memory.list() if n['id'] == ctx.msg.get('id')), None)
            if note is None:
                raise ValueError('That note no longer exists.')
            answer = await asyncio.to_thread(team_library.share_note, ctx.state.cloud, project, note,
                                             str(ctx.msg.get('organization_id') or ''))
            organization = (answer.get('organization') or {}).get('name') or 'your organization'
            shared = (f'Already shared with {organization}.' if answer.get('status') == 'approved'
                      else f'Sent to {organization} for review. The team gets it once an owner or admin approves it.')
        status = await asyncio.to_thread(ctx.state.cloud.status)
        share_to = ([{'id': org.get('id'), 'name': org.get('name')}
                     for org in (status.get('account') or {}).get('organizations') or []]
                    if status.get('signed_in') and await asyncio.to_thread(team_library.repository_of, project) else [])
        await ctx.send({'event': 'project_memory_updated', 'project': project, 'memories': memory.list(),
                        'team_notes': await asyncio.to_thread(team_library.notes_for, project),
                        'share_to': share_to, **({'shared': shared} if shared else {})})
    except (OSError, ValueError, team_library.LibraryError) as exc:
        await ctx.send({'event': 'error', 'message': str(exc)})


@command("fork_session")
async def _cmd_fork_session(ctx: CommandContext) -> None:
    if await _block_active_navigation(ctx):
        return
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
        snapshot = forked.history_snapshot()
        history_page = snapshot["page"]
        await ctx.send({
            "event": "session_loaded",
            "cwd": ctx.state.project.project_path,
            "current_session_id": forked.id,
            "session_role": forked.session_role,
            "display_events": history_page["events"],
            "history_page": history_page,
            "projections": snapshot["projections"],
            "sessions": ctx.state.project.list_sessions(),
        })



@command("switch_session")
async def _cmd_switch_session(ctx: CommandContext) -> None:
    if await _block_active_navigation(ctx):
        return
    session_id = ctx.msg.get("session_id", "")
    # If session is from a different project, switch project first
    project_path = ctx.msg.get("project_path", "")
    if project_path and ctx.state._normalize_path(project_path) != ctx.state._normalize_path(ctx.state.project.project_path):
        if os.path.isdir(project_path):
            # Detach the old engine session before project context rewiring.
            # Provider discovery is global (not project-scoped), so probing it
            # here only delays the conversation the user asked to see.
            ctx.state.session = None
            ctx.state.apply_project_context(project_path, refresh_index=True)
            ctx.state._first_message_sent = False
    record = ctx.state.project.load_session(session_id, hydrate=False)
    if record:
        # Replay the saved conversation immediately. Backend startup
        # can take seconds (or fail when a provider is offline), but
        # neither condition should prevent users from opening and
        # reading an existing session.
        # One ledger parse supplies both the visible page and the engine's
        # conversation history. The previous path parsed every event twice.
        snapshot = record.history_snapshot(hydrate=True)
        history_page = snapshot["page"]
        await ctx.send({
            "event": "session_loaded",
            "session_id": record.id,
            "cwd": ctx.state.project.project_path,
            "title": record.title,
            "backend_type": record.backend_type,
            "model": record.model,
            "message_count": record.message_count,
            "session_mode": "code",
            "session_role": record.session_role or "generator",
            "display_events": history_page["events"],
            "history_page": history_page,
            "projections": snapshot["projections"],
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
                    lambda: ctx.state.restore_session_runtime(
                        backend_type,
                        model or None,
                        session_mode="code",
                        session_role=record.session_role or "generator",
                        thinking_mode=record.thinking_mode or "",
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
            fallback_type, fallback_model = ctx.state.recovery_chat_backend_choice(
                record.backend_type,
                record.model,
            )
            if fallback_type and fallback_model:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: ctx.state.create_backend(
                            fallback_type,
                            fallback_model,
                            session_mode="code",
                            session_role=record.session_role or "generator",
                        ),
                    )
                    if ctx.state.session and record.conversation_history:
                        ctx.state.session.conversation_history = record.conversation_history
                    ctx.state._first_message_sent = record.message_count > 0
                    record.backend_type = fallback_type
                    record.model = fallback_model
                    record.thinking_mode = (
                        getattr(ctx.state.backend_spec, "thinking_mode", "") or ""
                    )
                    ctx.state.project.save_current_session(ctx.state.session)
                    await ctx.send({
                        "event": "status_msg",
                        "message": (
                            f"The saved {backend_type} runtime was unavailable. "
                            f"Continuing with {fallback_type} ({fallback_model})."
                        ),
                    })
                except Exception:
                    logger.warning(
                        "saved-session fallback runtime also failed",
                        exc_info=True,
                    )
                    ctx.state.backend = None
                    ctx.state.backend_spec = None
                    ctx.state.session = None

            if not ctx.state.session:
                # Retain the reason. This used to be sent only as a status_msg,
                # which the frontend renders as a transient toast — so by the time
                # the user typed a message the explanation was gone and all that
                # was left was a stale model name in the composer and an error
                # claiming no model was selected.
                ctx.state.runtime_error = (
                    f"{backend_type or 'This session'}"
                    f"{f' ({model})' if model else ''}"
                    f" failed to start: {e}"
                )
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
        current = ctx.state.project.current_session
        record = current if current and current.id == session_id else ctx.state.project.load_session(session_id, activate=False)
        if record:
            record.title = new_title
            record.title_source = "manual"
            record.save()
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
    # Explicit user request: bypass the probe-freshness window.
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: ctx.state.detect_backends(force=True)
    )
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
            "open_after_add": bool(ctx.msg.get("open_after_add")),
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
    if await _block_active_navigation(ctx):
        return
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
        # The engine session is project-scoped, but HTTP provider clients are
        # not. Detach the old conversation before applying the new context and
        # let ensure_default_runtime_session reuse a compatible client.
        if await _block_active_navigation(ctx):
            return
        ctx.state.session = None
        ctx.state.apply_project_context(resolved_project_path, refresh_index=True)
        ctx.state._first_message_sent = False
        ctx.state.costs.reset_session()
        # Provider discovery is global and already cached from app startup.
        # Re-probing every project click made an unreachable provider's network
        # timeout part of navigation latency.
        if ctx.state.available_backends:
            # Publish the selected workspace before potentially slow runtime setup.
            # Runtime construction stays serialized to avoid attaching it to a
            # different workspace when users click projects quickly.
            navigation = ctx.state.get_init_data()
            navigation.update(project_switch_id=project_switch_id,
                              runtime_preparing=True, current_backend='', current_model='')
            await ctx.send(navigation)
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



def _update_check_message(info: dict, started: bool) -> str:
    """What Check for updates tells people, from ``updater.status()``."""
    pending = info.get("pending") or {}
    if info.get("installed_by") == "msi":
        return "This copy was installed from the MSI package, so your organization's device management updates it."
    if info.get("mode") == "off":
        if pending and pending.get("mode") != "off":
            return "Updates are off until Lumi restarts with your new update settings."
        if info.get("managed_by") and "mode" in (info.get("locked") or []):
            return f"Updates are turned off by {info['managed_by']}."
        return "Updates are turned off in Settings > Updates."
    if not started:
        return "This copy of Lumi doesn't update itself (it runs from source or outside Windows)."
    message = f"Checking {info.get('describe') or 'for updates'}."
    if pending:
        message += " Your changed update settings apply after Lumi restarts."
    return message


def _third_party_notices_path() -> str:
    """THIRD_PARTY_NOTICES.txt of an installed copy, or "" when running from source."""
    import sys
    from pathlib import Path

    base = getattr(sys, "_MEIPASS", "")
    candidate = Path(base) / "licenses" / "THIRD_PARTY_NOTICES.txt" if base else None
    return str(candidate) if candidate and candidate.is_file() else ""


async def _cloud_run(ctx: CommandContext, action) -> None:
    """Run a Lumi Cloud action off the event loop, then send the account status."""
    from ..cloud import CloudError

    client = ctx.state.cloud
    try:
        await asyncio.to_thread(action, client)
    except CloudError as exc:
        client.last_error = str(exc)
    except Exception as exc:
        logger.exception("Lumi Cloud action failed")
        client.last_error = f"Something went wrong: {exc}"
    await ctx.send({"event": "cloud_status", "data": await asyncio.to_thread(client.status)})


@command("cloud_status")
async def _cmd_cloud_status(ctx: CommandContext) -> None:
    """Settings > Lumi account: the Lumi Cloud sign-in and this computer's enrollment."""
    await ctx.send({"event": "cloud_status", "data": await asyncio.to_thread(ctx.state.cloud.status)})


@command("cloud_sign_in")
async def _cmd_cloud_sign_in(ctx: CommandContext) -> None:
    """Open the browser to sign in; the result arrives later as a cloud_status event."""
    url = str(ctx.msg.get("url") or "")
    await _cloud_run(ctx, lambda client: client.begin_sign_in(url))


@command("cloud_cancel")
async def _cmd_cloud_cancel(ctx: CommandContext) -> None:
    await _cloud_run(ctx, lambda client: client.cancel_sign_in())


@command("cloud_refresh")
async def _cmd_cloud_refresh(ctx: CommandContext) -> None:
    await _cloud_run(ctx, lambda client: client.refresh_account())


@command("cloud_sign_out")
async def _cmd_cloud_sign_out(ctx: CommandContext) -> None:
    from .. import team_library

    def sign_out(client: Any) -> None:
        client.sign_out()
        team_library.clear()  # an organization's library goes with the sign-in

    await _cloud_run(ctx, sign_out)


@command("team_library")
async def _cmd_team_library(ctx: CommandContext) -> None:
    """The organization's skills and prompts (lumi/team_library.py), synced first when asked or when old."""
    from .. import team_library

    status = await asyncio.to_thread(ctx.state.cloud.status)
    signed_in, error = bool(status.get("signed_in")), ""
    stale = time.time() - (team_library.cached().get("synced_at") or 0) >= team_library.STALE_SECONDS
    if signed_in and (ctx.msg.get("sync") or stale):
        try:
            await asyncio.to_thread(team_library.sync, ctx.state.cloud)
        except team_library.LibraryError as exc:
            error = str(exc)
    prompts = [{key: item[key] for key in ("ref", "name", "description", "body", "version", "organization",
                                           "updated_by")}
               for item in team_library.items("prompt")]
    await ctx.send({"event": "team_library", "signed_in": signed_in, **team_library.summary(), "prompts": prompts,
                    **({"error": error} if error else {})})


@command("cloud_enroll")
async def _cmd_cloud_enroll(ctx: CommandContext) -> None:
    organization = str(ctx.msg.get("organization_id") or "")
    await _cloud_run(ctx, lambda client: client.enroll(organization))


@command("cloud_unenroll")
async def _cmd_cloud_unenroll(ctx: CommandContext) -> None:
    await _cloud_run(ctx, lambda client: client.unenroll())


@command("cloud_check_in")
async def _cmd_cloud_check_in(ctx: CommandContext) -> None:
    await _cloud_run(ctx, lambda client: client.check_in())


@command("cloud_remote_tasks")
async def _cmd_cloud_remote_tasks(ctx: CommandContext) -> None:
    """Settings > Lumi account: tasks from Slack and Teams on or off, and where and how they run."""
    from ..cloud import CloudError
    from ..policy import current as current_policy
    from ..remote_tasks import MODES

    enabled = ctx.msg.get("enabled") is True
    project = os.path.abspath(os.path.expanduser(str(ctx.msg.get("project") or "").strip())) \
        if str(ctx.msg.get("project") or "").strip() else ""
    mode = str(ctx.msg.get("mode") or "ask")

    def save(client) -> None:
        policy = current_policy()
        if policy and policy.locked("cloud", "remote_tasks"):
            raise CloudError(f"{policy.organization} manages tasks from Slack and Teams on this computer.")
        if mode not in MODES:
            raise CloudError("Choose Ask, Auto-edit or Bypass.")
        if enabled and not os.path.isdir(project):
            raise CloudError("Choose the folder requests run in: it must exist on this computer.")
        client.last_error = ""
        ctx.state.settings.update_section("cloud", {"remote_tasks": enabled, "remote_tasks_project": project,
                                                    "remote_tasks_mode": mode})

    await _cloud_run(ctx, save)


@command("about_info")
async def _cmd_about_info(ctx: CommandContext) -> None:
    """Settings > About Lumi: version, license and who manages this copy."""
    from .. import __version__
    from ..policy import current as current_policy
    from ..update_channels import installed_by

    policy = current_policy()
    await ctx.send({"event": "about_info", "data": {
        "version": __version__,
        "license": "MIT",
        "notices": _third_party_notices_path(),
        "organization": policy.organization if policy else "",
        "installed_by": installed_by(),
    }})


@command("create_sample_project")
async def _cmd_create_sample_project(ctx: CommandContext) -> None:
    """Make (or find) the first-run sample project; the page then opens it."""
    from .onboarding import SAMPLE_TASK, create_sample_project

    try:
        path = await asyncio.to_thread(create_sample_project)
    except OSError as exc:
        await ctx.send({"event": "error", "message": f"Couldn't create the sample project: {exc}"})
        return
    await ctx.send({"event": "sample_project", "path": str(path), "task": SAMPLE_TASK})


@command("update_status")
async def _cmd_update_status(ctx: CommandContext) -> None:
    from lumi.updater import status as update_status

    await ctx.send({"event": "update_status", "data": await asyncio.to_thread(update_status)})


@command("check_updates")
async def _cmd_check_updates(ctx: CommandContext) -> None:
    try:
        from lumi.updater import check_for_updates_now, status as update_status

        info = await asyncio.to_thread(update_status)
        started = info.get("mode") != "off" and await asyncio.to_thread(
            lambda: check_for_updates_now(silent=False))
        await ctx.send({"event": "status_msg", "message": _update_check_message(info, bool(started))})
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
        from .. import __version__ as _ver
        from .. import secret_scan
        from ..paths import state_home
        state_dir = state_home()
        output_dir = diagnostics.default_output_dir()
        # Actual key values (including keys in the OS credential store) are
        # removed wherever they appear, whatever their format.
        known = secret_scan.secret_values(
            ctx.state.settings, min_length=diagnostics.MIN_KNOWN_SECRET_LENGTH,
        )
        zip_path = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: diagnostics.build_diagnostics_zip(
                state_dir, output_dir, version=_ver, known_secrets=known,
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
    """Answer the tool-permission prompt that is waiting, and only that one.

    The answer must name the prompt's request id; anything else (a late
    click, a stale tab) is ignored rather than applied to a later prompt.
    Only a JSON ``true`` approves. The first accepted answer is final.
    """
    state = ctx.state
    request_id = str(ctx.msg.get("request_id") or "")
    lock = getattr(state, "_permission_lock", None)
    if lock is None:
        return
    with lock:
        pending = str(getattr(state, "permission_request_id", "") or "")
        if not pending or request_id != pending:
            logger.info("Ignored a permission answer for a prompt that is no longer waiting")
            return
        state.permission_request_id = ""
        state.permission_result[0] = ctx.msg.get("approved") is True
        state.permission_response.set()



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



# What Settings may change over the socket, by section and key. Hooks, LSP
# servers, plugins, the chat gateway and stdio MCP servers start processes or
# grant remote control; Settings shows them read-only and they are edited in
# ~/.lumi/settings.json. HTTP MCP servers are checked separately below.
_SOCKET_SETTING_KEYS: dict[str, frozenset[str]] = {
    "general": frozenset({
        "display_name", "show_companion", "default_backend", "default_model",
        "default_permission_mode", "auto_lint_after_edits", "auto_test_after_edits",
        "auto_test_command", "max_model_requests", "big_context_profile",
        "harness_enabled", "autonomous_sessions", "fallback_models", "role_models",
    }),
    "appearance": frozenset({"theme", "density", "font_size"}),
    "local_backends": frozenset({"ollama_host", "ollama_num_ctx", "ollama_keep_alive"}),
    "network": frozenset({"ollama_url", "exo_url", "sonn_url", "proxy_url", "no_proxy", "system_certificates"}),
    "api_keys": frozenset({"sonn", "openrouter", "kimi", "anthropic", "openai", "otlp", "github", "gitlab", "bitbucket",
                           "azure_devops", "jira", "linear"}),
    "issue_trackers": frozenset({"jira_url", "jira_email"}),
    "review": frozenset({"agent_changes", "reviewers"}),
    "engram": frozenset({"enabled", "server_url"}),
    "cost_tracking": frozenset({"enabled", "budget_alert_usd", "daily_limit_usd", "turn_limit_usd",
                                "price_overrides"}),
    "privacy": frozenset({
        "secret_scan", "excluded_paths", "transcript_retention_days",
        "audit_log", "audit_capture", "audit_retention_days",
    }),
    "audit": frozenset({"otlp_endpoint", "otlp_auth_header"}),
    "security": frozenset({"cli_adapters", "computer_use", "chat_gateway", "shell_sandbox", "scheduled_tasks",
                           "editor_bridge"}),
    "updates": frozenset({"mode", "channel", "pin"}),
    "onboarding": frozenset({"dismissed"}),
    "model_favorites": frozenset({"models"}),
}


def _socket_mcp_server(value: Any) -> dict[str, Any]:
    """Normalise an MCP server entry from Settings, which adds HTTP servers only."""
    from urllib.parse import urlsplit

    if not isinstance(value, dict):
        raise ValueError("MCP server settings must be an object.")
    transport = str(value.get("transport", "")).strip().lower()
    if transport not in {"http", "streamable_http", "streamable-http"}:
        raise ValueError(
            "Settings adds HTTP MCP servers only. Add command-based servers "
            "in ~/.lumi/settings.json."
        )
    url = str(value.get("url", "")).strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Enter an http:// or https:// MCP server URL.")
    # Rebuilt rather than stored as sent, so command/args/env from an older
    # entry cannot ride along with a URL edit.
    entry: dict[str, Any] = {
        "transport": "http",
        "url": url,
        "enabled": value.get("enabled", True) is not False,
    }
    headers = value.get("headers")
    if isinstance(headers, dict):
        entry["headers"] = {str(k): str(v) for k, v in headers.items()}
    return entry


def _socket_setting_value(section: Any, key: Any, value: Any) -> Any:
    """Return the value to store for one Settings write, or raise ValueError."""
    if section == "mcp_servers":
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError("Enter an MCP server name.")
        return _socket_mcp_server(value)
    allowed = _SOCKET_SETTING_KEYS.get(section, ()) if isinstance(section, str) else ()
    if not isinstance(key, str) or key not in allowed:
        raise ValueError(
            f"{section}.{key} can't be changed from the app. "
            "Edit ~/.lumi/settings.json instead."
        )
    if (section, key) == ("general", "default_permission_mode") and (
        not isinstance(value, str) or value not in PERMISSION_MODES
    ):
        raise ValueError("Choose a permission mode: ask, auto-edit, plan or bypass.")
    if section == "updates":
        from ..update_channels import normalize

        return normalize(key, value)
    if (section, key) in {
        ("network", "system_certificates"), ("privacy", "secret_scan"), ("privacy", "audit_log"),
        ("onboarding", "dismissed"),
    } or (section == "security" and key != "shell_sandbox"):
        if not isinstance(value, bool):
            raise ValueError(f"{section}.{key} must be on or off.")
    elif (section, key) == ("security", "shell_sandbox"):
        from ..engine.os_sandbox import MODES

        if value not in MODES:
            raise ValueError("Choose where commands can write: off or project.")
    elif (section, key) == ("privacy", "excluded_paths"):
        items = value.splitlines() if isinstance(value, str) else value
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("Enter up to 500 patterns, one per line.")
        patterns = []
        for item in items:
            text = str(item or "").strip()
            if len(text) > 300:
                raise ValueError("A pattern can be at most 300 characters.")
            if text and not text.startswith("#") and text not in patterns:
                patterns.append(text)
        return patterns
    elif (section, key) == ("review", "agent_changes"):
        if not isinstance(value, bool):
            raise ValueError("review.agent_changes must be on or off.")
    elif (section, key) == ("review", "reviewers"):
        import re

        items = value.splitlines() if isinstance(value, str) else value
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError("Enter up to 20 reviewers, one per line.")
        names = []
        for item in items:
            name = str(item or "").strip().lstrip("@")
            if not name:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,38}(/[\w.-]{1,100})?", name):
                raise ValueError(f"{name} isn't a GitHub username or organization/team.")
            if name not in names:
                names.append(name)
        return names
    elif (section, key) == ("general", "fallback_models"):
        from ..engine.model_roles import parse_fallback_models

        return parse_fallback_models(value)
    elif (section, key) == ("general", "role_models"):
        from ..engine.model_roles import parse_role_models

        parsed = parse_role_models(value)
        return [f"{role} {entry['backend_type']}:{entry['model']}" for role, entry in parsed.items()]
    elif section == "cost_tracking" and key in {"budget_alert_usd", "daily_limit_usd", "turn_limit_usd"}:
        if value in (None, ""):
            return None  # an emptied field removes the limit
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1_000_000:
            raise ValueError("Enter an amount in USD, or leave it empty for no limit.")
        return float(value) or None
    elif (section, key) == ("cost_tracking", "price_overrides"):
        from ..pricing import parse_override_lines

        return parse_override_lines(value)
    elif (section, key) == ("privacy", "audit_capture"):
        from ..audit import CAPTURE_LEVELS

        if value not in CAPTURE_LEVELS:
            raise ValueError("Choose what the audit log captures: metadata, redacted or full.")
    elif (section, key) == ("privacy", "audit_retention_days"):
        value = 0 if value is None else value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3650:
            raise ValueError("Enter a number of days from 0 (keep) to 3650.")
        return int(value)
    elif (section, key) == ("audit", "otlp_endpoint"):
        from urllib.parse import urlsplit

        text = str(value or "").strip()
        parts = urlsplit(text)
        if text and (parts.scheme not in {"http", "https"} or not parts.hostname):
            raise ValueError("Enter the collector as http(s)://host:4318.")
        if parts.username or parts.password:
            raise ValueError("Put the collector token under API keys, not in the URL.")
        return text.rstrip("/")
    elif (section, key) == ("audit", "otlp_auth_header"):
        import re

        text = str(value or "").strip() or "Authorization"
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", text):
            raise ValueError("Enter a header name such as Authorization or x-honeycomb-team.")
        return text
    elif (section, key) == ("privacy", "transcript_retention_days"):
        value = 0 if value is None else value  # an emptied field keeps transcripts
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3650:
            raise ValueError("Enter a number of days from 0 (keep) to 3650.")
        return int(value)
    elif (section, key) == ("network", "proxy_url"):
        from .. import net
        return net.validate_proxy_url(value if isinstance(value, str) else "")
    elif (section, key) == ("network", "no_proxy"):
        hosts = [item.strip() for item in str(value or "").replace("\n", ",").split(",")]
        if any(len(item) > 253 or any(ch.isspace() for ch in item) for item in hosts):
            raise ValueError("List hosts separated by commas, such as internal.example.com, 10.1.2.3.")
        return ", ".join(item for item in hosts if item)
    return value


@command("update_settings")
async def _cmd_update_settings(ctx: CommandContext) -> None:
    section = ctx.msg.get("section", "")
    key = ctx.msg.get("key")
    clear_secret = bool(ctx.msg.get("clear_secret", False))
    # The Ollama setup wizard sends its keys together as `values`.
    values = ctx.msg.get("values")
    if key is None and isinstance(values, dict):
        writes = list(values.items())
    else:
        writes = [(key, ctx.msg.get("value"))]
    async def refuse(message: str) -> None:
        # Marked so Settings shows it on the page being edited.
        await ctx.send({"event": "error", "message": message, "source": "settings"})

    try:
        if not writes:
            raise ValueError("No setting was given.")
        writes = [(k, _socket_setting_value(section, k, v)) for k, v in writes]
    except ValueError as exc:
        await refuse(str(exc))
        return
    from .. import audit
    from ..policy import current as current_policy

    policy = current_policy()
    managed = [k for k, _ in writes if policy and isinstance(section, str) and policy.locked(section, k)]
    if managed:
        await refuse(f"{section}.{managed[0]} is managed by {policy.organization} and can't be changed here.")
        return
    sonn_change = any(
        (section == "api_keys" and k == "sonn") or (section == "network" and k == "sonn_url")
        for k, _ in writes
    )
    if ctx.runs.busy and section == "security" and any(k == "cli_adapters" for k, _ in writes):
        await refuse("Finish or stop the current run before changing which providers are allowed.")
        return
    if ctx.runs.busy and sonn_change:
        await refuse("Finish or stop the current run before changing SONN settings.")
        return
    def current_values() -> dict:
        # Off the event loop: API keys come from the OS credential store.
        return {k: ctx.state.settings.get(section, k) for k, _ in writes}

    before = await asyncio.to_thread(current_values)
    for k, v in writes:
        data = await asyncio.to_thread(ctx.state.update_setting_value, section, k, v, clear_secret=clear_secret)
    # Which settings changed, never their values. Leaving a field unchanged
    # (Settings saves on blur) isn't a change.
    after = await asyncio.to_thread(current_values)
    changed = [str(k) for k, _ in writes if after[k] != before[k]]
    if changed:
        audit.record("settings.change", section=str(section), keys=changed)
    if sonn_change:
        ctx.state.sonn_account_revision = getattr(ctx.state, "sonn_account_revision", 0) + 1
        await ctx.send({"event": "sonn_account", "data": None})
    await ctx.send({"event": "settings", "data": data})
    if section == "updates":
        # Settings > Updates says what changes after a restart.
        from lumi.updater import status as update_status

        await ctx.send({"event": "update_status", "data": await asyncio.to_thread(update_status)})
    await ctx.send(ctx.state.get_init_data(refresh_only=True))


@command("sonn_account")
async def _cmd_sonn_account(ctx: CommandContext) -> None:
    from ..network_defaults import resolve_sonn_url
    from ..sonn_account import read_account

    revision = getattr(ctx.state, "sonn_account_revision", 0)
    api_key, _, _, _ = ctx.state._api_key_details("sonn", "SONN_API_KEY")
    base_url = resolve_sonn_url(settings_data=ctx.state.settings.get_all())
    try:
        data = await asyncio.to_thread(read_account, api_key, base_url=base_url)
    except ValueError as exc:
        data = {"error": str(exc)}
    except Exception:
        data = {"error": "SONN account details are unavailable. Try refreshing later."}
    if revision == getattr(ctx.state, "sonn_account_revision", 0):
        await ctx.send({"event": "sonn_account", "data": data})


@command("provider_connection")
async def _cmd_provider_connection(ctx: CommandContext) -> None:
    from ..codex_account import codex_account
    from ..openrouter import OpenRouterBackend
    from ..sonn import SonnBackend
    from ..network_defaults import resolve_sonn_url

    provider = ctx.msg.get("provider")
    action = ctx.msg.get("action", "status")
    try:
        if provider == "codex":
            if action == "login":
                if ctx.runs.busy:
                    raise ValueError("Finish or stop the current run before changing the connected account.")
                data = await asyncio.to_thread(codex_account.login)
            elif action == "cancel":
                await asyncio.to_thread(codex_account.cancel_login)
                data = {"cancelled": True}
            elif action == "status":
                data = await asyncio.to_thread(codex_account.status)
                account = data.get("account")
                data["account"] = ({key: account.get(key) for key in ("type", "email", "planType")}
                                   if account else None)
                ctx.state.codex_connection = data
                rows = data.get("models") or []
                if rows:
                    ctx.state.available_backends.setdefault("codex", {}).update({
                        "models": [row.get("model") or row["id"] for row in rows],
                        "model_labels": {row.get("model") or row["id"]: row.get("displayName") or row["id"] for row in rows},
                    })
            else:
                raise ValueError("Unknown connection action.")
        elif provider == "openrouter" and action == "status":
            api_key, _, _, _ = ctx.state._api_key_details("openrouter", "OPENROUTER_API_KEY")
            data = await asyncio.to_thread(OpenRouterBackend(api_key, "connection-check").health)
            await asyncio.to_thread(OpenRouterBackend.catalog, force=True)
            await asyncio.to_thread(ctx.state.detect_backends, force=True)
        elif provider in {"anthropic", "openai"} and action == "status":
            from ..anthropic_api import AnthropicBackend
            from ..openai_api import OpenAIResponsesBackend

            env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
            api_key, _, _, _ = ctx.state._api_key_details(provider, env)
            if not api_key:
                raise ValueError(f"Add your {'Anthropic' if provider == 'anthropic' else 'OpenAI'} key "
                                 "in API keys below, then check the connection.")
            backend_class = AnthropicBackend if provider == "anthropic" else OpenAIResponsesBackend
            data = await asyncio.to_thread(backend_class(api_key, backend_class.DEFAULT_MODEL).health)
            data["model_count"] = len(data.get("models") or [])
            await asyncio.to_thread(ctx.state.detect_backends, force=True)
        elif provider == "sonn" and action == "status":
            api_key, _, _, _ = ctx.state._api_key_details("sonn", "SONN_API_KEY")
            base_url = resolve_sonn_url(settings_data=ctx.state.settings.get_all())
            data = await asyncio.to_thread(SonnBackend(api_key, base_url=base_url).health)
            ctx.state.available_backends["sonn"] = {
                "url": base_url, "models": data["models"], "model_labels": data["model_labels"],
            }
        else:
            raise ValueError("Unknown provider connection.")
        await ctx.send({"event": "provider_connection", "provider": provider, "data": data})
        if action == "status":
            await ctx.send(ctx.state.get_init_data(refresh_only=True))
    except Exception as exc:
        await ctx.send({"event": "provider_connection", "provider": provider, "data": {"error": str(exc)}})

# ── Model connections (gateways, Azure, Bedrock, Vertex) ───────────


def _connections_payload(state) -> dict:
    from ..connections import AUTH_METHODS, CONNECTION_TYPES, list_connections, secret_setting
    from ..engine import provider_extensions

    items = []
    for connection in list_connections(state.settings):
        key = str(state.settings.get("api_keys", secret_setting(connection["id"]), "") or "")
        items.append({**connection, "has_key": bool(key)})
    try:
        extensions = provider_extensions.available(state.settings)
    except Exception:  # noqa: BLE001 - a broken pack folder mustn't hide the connections
        logger.warning("Listing extension providers failed", exc_info=True)
        extensions = []
    return {"event": "connections", "data": {
        "items": items, "types": CONNECTION_TYPES, "auth_methods": list(AUTH_METHODS),
        "extension_providers": extensions}}


def _connection_in_use(ctx: CommandContext, connection_id: str) -> bool:
    spec = getattr(ctx.state, "backend_spec", None)
    return bool(ctx.runs.busy and spec and spec.backend_type == "conn-" + connection_id)


@command("connections_list")
async def _cmd_connections_list(ctx: CommandContext) -> None:
    await ctx.send(_connections_payload(ctx.state))


@command("connection_save")
async def _cmd_connection_save(ctx: CommandContext) -> None:
    from ..connections import MAX_CONNECTIONS, list_connections, normalize_connection, secret_setting

    original_id = str(ctx.msg.get("original_id") or "").strip()
    try:
        existing = list_connections(ctx.state.settings)
        others = {c["id"] for c in existing if c["id"] != original_id}
        connection = normalize_connection(ctx.msg.get("connection"), existing_ids=others)
        if not original_id and len(existing) >= MAX_CONNECTIONS:
            raise ValueError(f"Lumi keeps up to {MAX_CONNECTIONS} connections. Remove one first.")
        if original_id and not any(c["id"] == original_id for c in existing):
            raise ValueError("That connection no longer exists. Reload Settings and try again.")
        if _connection_in_use(ctx, original_id or connection["id"]):
            raise ValueError("Finish or stop the current run before changing the connection it uses.")
        api_key = ctx.msg.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("The key must be text.")
    except ValueError as exc:
        await ctx.send({"event": "connection_saved", "data": {"error": str(exc)}})
        return

    def _save() -> None:
        updated = [c for c in existing if c["id"] != original_id] + [connection]
        if original_id and original_id != connection["id"]:
            old_key = str(ctx.state.settings.get("api_keys", secret_setting(original_id), "") or "")
            if old_key and not api_key:
                ctx.state.settings.set("api_keys", secret_setting(connection["id"]), old_key)
            ctx.state.settings.set("api_keys", secret_setting(original_id), "")
        if ctx.msg.get("clear_key"):
            ctx.state.settings.set("api_keys", secret_setting(connection["id"]), "")
        elif api_key:
            ctx.state.settings.set("api_keys", secret_setting(connection["id"]), api_key.strip())
        ctx.state.update_setting_value("connections", None, updated)

    await asyncio.to_thread(_save)
    await ctx.send({"event": "connection_saved", "data": {"id": connection["id"]}})
    await ctx.send(_connections_payload(ctx.state))
    await ctx.send(ctx.state.get_init_data(refresh_only=True))


@command("connection_delete")
async def _cmd_connection_delete(ctx: CommandContext) -> None:
    from ..connections import list_connections, secret_setting

    connection_id = str(ctx.msg.get("id") or "").strip()
    existing = list_connections(ctx.state.settings)
    if not any(c["id"] == connection_id for c in existing):
        await ctx.send({"event": "connection_saved", "data": {"error": "That connection no longer exists."}})
        return
    if _connection_in_use(ctx, connection_id):
        await ctx.send({"event": "connection_saved", "data": {
            "error": "Finish or stop the current run before removing the connection it uses."}})
        return

    def _delete() -> None:
        ctx.state.settings.set("api_keys", secret_setting(connection_id), "")
        ctx.state.update_setting_value("connections", None, [c for c in existing if c["id"] != connection_id])

    await asyncio.to_thread(_delete)
    await ctx.send({"event": "connection_saved", "data": {"deleted": connection_id}})
    await ctx.send(_connections_payload(ctx.state))
    await ctx.send(ctx.state.get_init_data(refresh_only=True))


@command("connection_test")
async def _cmd_connection_test(ctx: CommandContext) -> None:
    """Check a draft or saved connection: credentials resolve and models are listed."""
    from ..connections import (
        create_connection_backend, discover_models, normalize_connection, secret_setting, sign_in,
    )

    try:
        connection = normalize_connection(ctx.msg.get("connection"))
        api_key = ctx.msg.get("api_key")
        if not api_key:
            # A draft of a saved connection reuses its stored key.
            api_key = str(ctx.state.settings.get(
                "api_keys", secret_setting(str(ctx.msg.get("original_id") or connection["id"])), "") or "")

        def _check() -> dict:
            # Sign in first, so a refused sign-in or an unreadable client
            # certificate says why instead of "no models".
            token_provider, _tls = sign_in(connection, api_key)
            if token_provider is not None:
                token_provider()
            models = discover_models(connection, api_key, timeout=8.0, settings=ctx.state.settings)
            if connection["type"] in {"openai-compatible"} and not models:
                raise ValueError(f"{connection['name']} answered, but listed no models. "
                                 "Enter the model ids by hand.")
            probe_model = (models or connection["models"] or [""])[0]
            if connection["type"] != "openai-compatible":
                backend = create_connection_backend(connection, probe_model, api_key, settings=ctx.state.settings)
                health = backend.health()
                # An extension's test starts its process; a failure is the result.
                if connection["type"] == "extension" and not health.get("ok"):
                    raise ValueError(health.get("error") or "The provider didn't answer.")
                models = models or health.get("models") or []
            return {"ok": True, "models": models[:200],
                    "message": f"Connected · {len(models)} model{'s' if len(models) != 1 else ''} available"}

        data = await asyncio.to_thread(_check)
    except Exception as exc:
        data = {"ok": False, "message": str(exc) or type(exc).__name__}
    await ctx.send({"event": "connection_test", "data": data})


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
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".hpp": "cpp",
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

    The servers the ``code_intel`` tool can use (engine/lsp.py): Settings'
    ``lsp_servers``, then well-known servers installed on PATH or matching the
    project's languages, with whether each is running for this project.
    """
    from ..engine import lsp

    running = lsp.servers.status(project_path) if project_path else {}

    def status_of(spec_id: str, fallback: str) -> tuple[str, str]:
        state = running.get(spec_id) or {}
        if state.get("state") in ("running", "failed"):
            return state["state"], state.get("error", "")
        return fallback, ""

    servers: list[dict[str, Any]] = []
    configured_programs: set[str] = set()
    for spec, enabled in lsp.configured(settings):
        program = spec.command[0]
        configured_programs.add(lsp._program_name(program))
        available = bool(shutil.which(program) or os.path.isfile(program))
        languages = sorted(set(spec.languages.values()))
        status, error = status_of(spec.id, "available" if available else "missing")
        if not enabled:
            status, error = "disabled", ""
        servers.append({
            "id": spec.id,
            "name": spec.name,
            "command": " ".join(spec.command),
            "enabled": enabled,
            "available": available,
            "status": status,
            "source": "configured",
            "languages": languages,
            "detail": error or (", ".join(languages) if languages
                                else "Add extensions or languages to use it"),
        })

    workspace_langs = _workspace_language_hints(project_path)
    for spec in lsp.KNOWN:
        if lsp._program_name(spec.command[0]) in configured_programs:
            continue
        languages = sorted(set(spec.languages.values()))
        executable = shutil.which(spec.command[0])
        if not executable and not workspace_langs.intersection(languages):
            continue
        status, error = status_of(spec.id, "available" if executable else "missing")
        servers.append({
            "id": spec.id,
            "name": spec.name,
            "command": " ".join(spec.command),
            "enabled": bool(executable),
            "available": bool(executable),
            "status": status,
            "source": "detected",
            "languages": languages,
            "detail": error or (f"Installed: {executable}; starts when the agent asks about this code"
                                if executable else f"Install {spec.command[0]} to use it"),
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
    payload is reserved for Lumi plugin packages so pinned skills do not
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
    `lumi-skill` CLI does, so the GUI can view a project-scoped
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
