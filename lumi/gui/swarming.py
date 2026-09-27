"""Capture desktop identities before forwarding commands to the swarm engine."""

from __future__ import annotations

import asyncio
import copy
import getpass
import hashlib
import os
from pathlib import Path

from ..engine.swarming import Scope
from ..engine.swarming.models import Conflict, ScopeDenied, SwarmError
from ..engine.swarming.service import CapturedSession, SwarmRuntime
from .runtime import BackendSpec
from .sessions import _sessions_dir, is_valid_session_id


def _path_key(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def blocking_team(state) -> dict | None:
    """The team that keeps the current conversation from starting new work, or None.

    Only the conversation that owns an unfinished team waits for it
    (SwarmRuntime.blocking says why that is enough); a team starting right
    now holds every conversation for the moment it takes.
    """
    if getattr(state, "_swarm_starting", False):
        return {"reason": "starting"}
    manager = getattr(state, "_swarm_desktop", None)
    if manager is None:
        return None
    project = getattr(state, "project", None)
    workspace = getattr(project, "project_path", "") or ""
    record = getattr(project, "current_session", None)
    project_id = hashlib.sha256(_path_key(workspace).encode()).hexdigest() if workspace else ""
    return manager.blocking(project_id, getattr(record, "id", "") or "")


def busy(state) -> bool:
    """Cheap foreground gate for ordinary chat, model changes and missions in this conversation."""
    return blocking_team(state) is not None


def busy_refusal(state, doing: str = "starting an ordinary chat turn") -> dict:
    """The error event for work the current conversation's team keeps from starting.

    It names the team and says how to end it. ``team`` carries what the page
    needs to offer that directly (open this conversation's Team panel).
    """
    found = blocking_team(state) or {}
    reason = found.get("reason")
    if reason == "starting":
        message = f"A team is starting. Wait until it has started before {doing}."
    elif reason == "storage":
        message = (f"Lumi can't read its saved team records right now, so it isn't {doing} until it can "
                   "tell which conversation they belong to. Restart Lumi if this persists.")
    elif reason == "discovery":
        message = (f"Lumi couldn't read this project's saved teams at startup, so it isn't {doing} here. "
                   "Restart Lumi to try again.")
    else:
        objective = " ".join(str(found.get("objective") or "").split())
        name = f"The team “{objective[:80]}{'…' if len(objective) > 80 else ''}”" if objective else "This conversation's team"
        if found.get("owned"):
            message = (f"{name} is still working in this conversation. Let it finish, or open Team above and "
                       f"choose Stop team, before {doing}.")
        elif found.get("recovering"):
            message = (f"{name} in this conversation was interrupted. In Team above, finish Review interrupted "
                       f"work (check each worker's process and record what happened), then choose Finish stopped "
                       f"team, before {doing}.")
        else:
            message = (f"{name} in this conversation was left unfinished, for example when Lumi closed while it "
                       f"ran. Open Team above, choose Take over expired team and review its interrupted work, "
                       f"then stop it, before {doing}.")
        message += " Other conversations aren't affected."
    event = {"event": "error", "message": message, "code": "team_active"}
    if reason == "team":
        event["team"] = {"run_id": found.get("run_id", ""), "objective": found.get("objective", ""),
                         "state": found.get("state", ""), "session_id": found.get("session_id", ""),
                         "owned": bool(found.get("owned")), "recovering": bool(found.get("recovering"))}
    return event


def navigation_busy(state) -> bool:
    """Permit navigation to orphaned evidence while new execution stays closed."""
    manager = getattr(state, "_swarm_desktop", None)
    return bool(getattr(state, "_swarm_starting", False) or (manager and manager.navigation_busy))


def discover(state) -> None:
    """Register saved-project ownership before serving new execution commands."""
    manager = getattr(state, "_swarm_desktop", None)
    if manager is None:
        manager = state._swarm_desktop = SwarmRuntime(state.settings, managed_desktop=getattr(state, "_swarm_managed", None))
    paths = {state.project.project_path}
    paths.update(entry["path"] for entry in state.project.get_recent_projects(limit=10000))
    for workspace in paths:
        project_id = hashlib.sha256(_path_key(workspace).encode()).hexdigest()
        manager.watch_project(workspace, Scope.personal("local:" + getpass.getuser(), project_id, "discovery"))


def chat_context(state, workspace: str, session_id: str, run_id: str) -> dict[str, str]:
    """``@team:<run>`` in a saved conversation: one of its own personal teams (engine/context_broker.py)."""
    if not is_valid_session_id(session_id):
        raise ScopeDenied("Save this conversation before attaching one of its teams")
    manager = getattr(state, "_swarm_desktop", None)
    if manager is None:
        manager = state._swarm_desktop = SwarmRuntime(state.settings, managed_desktop=getattr(state, "_swarm_managed", None))
    project_id = hashlib.sha256(_path_key(workspace).encode()).hexdigest()
    return manager.chat_context(str(Path(workspace).resolve()),
                                Scope.personal("local:" + getpass.getuser(), project_id, session_id), run_id)


def _capture(state, message, manager) -> CapturedSession:
    project = str(message.get("project") or "")
    session_id = str(message.get("session_id") or "")
    if not project or not is_valid_session_id(session_id):
        raise ScopeDenied("Open a saved conversation before managing its team")
    if message.get("run_id") and message.get("action", "view") not in {"start", "configure"}:
        captured = manager.captured_run(message["run_id"], project, session_id)
        if captured:
            return captured
    record = state.project.current_session
    workspace = state.project.project_path
    if (record is None or record.id != session_id or _path_key(workspace) != _path_key(project)
            or not (_sessions_dir(workspace) / f"{session_id}.json").is_file()):
        raise ScopeDenied("The selected saved conversation changed; reopen its team panel")
    spec = getattr(state, "backend_spec", None)
    if not isinstance(spec, BackendSpec):
        # Say why there is no model: a runtime that failed to start ("Git isn't
        # installed", an unreachable server) isn't fixed by choosing one again.
        reason = str(getattr(state, "runtime_error", "") or "")
        raise Conflict(f"This conversation's model isn't running: {reason}" if reason
                       else "Choose a configured provider and model for this conversation")
    project_id = hashlib.sha256(_path_key(workspace).encode()).hexdigest()
    capture = CapturedSession(Scope.personal("local:" + getpass.getuser(), project_id, session_id),
        str(Path(workspace).resolve()), copy.deepcopy(spec), str(getattr(state.session, "project_instructions", "") or ""))
    return manager.execution_capture(capture, message.get("execution_mode", "personal"))


async def command(state, send, message, *, chat_busy=False):
    """Capture ownership before yielding; all SQLite/provider setup runs off-loop."""
    reply = {"event": "swarm_state", "request_id": message.get("request_id"),
             "project": message.get("project"), "session_id": message.get("session_id")}
    starting = False
    try:
        if message.get("action") in {"start", "request_plan", "collaboration_prepare", "collaboration_accept_work", "managed_sharing_prepare", "managed_sharing_accept_work"} and (chat_busy or getattr(state, "_swarm_starting", False)):
            raise Conflict("Finish or stop the current operation before starting a team")
        if getattr(state, "_swarm_discovery_failed", False) and message.get("action") not in {"view", "events"}:
            # Retained runs weren't registered at startup, so starting or changing
            # team work could overlap work nobody can see (app._app_lifespan).
            raise Conflict("Lumi couldn't check this computer's retained team work at startup. "
                           "Restart Lumi before starting or changing team work.")
        manager = getattr(state, "_swarm_desktop", None)
        if manager is None:
            manager = state._swarm_desktop = SwarmRuntime(state.settings, managed_desktop=getattr(state, "_swarm_managed", None))
        capture = _capture(state, message, manager)
        if message.get("action") in {"start", "request_plan", "collaboration_prepare", "collaboration_accept_work", "managed_sharing_prepare", "managed_sharing_accept_work"}:
            state._swarm_starting = starting = True
        reply.update(await asyncio.to_thread(manager.operate, capture, copy.deepcopy(message)))
    except (ValueError, SwarmError) as exc:
        reply["error"] = str(exc)
    except Exception:
        reply["error"] = "Team storage or runtime is unavailable. Retained work requires inspection before retrying."
    finally:
        if starting:
            state._swarm_starting = False
    await send(reply)
