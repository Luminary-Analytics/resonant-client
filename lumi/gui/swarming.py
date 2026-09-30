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

def busy(state) -> bool:
    """Cheap foreground gate for ordinary chat, navigation and backend changes."""
    manager = getattr(state, "_swarm_desktop", None)
    return bool(getattr(state, "_swarm_starting", False) or (manager and manager.busy))


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
        raise Conflict("Choose a configured provider and model for this conversation")
    project_id = hashlib.sha256(_path_key(workspace).encode()).hexdigest()
    capture = CapturedSession(Scope.personal("local:" + getpass.getuser(), project_id, session_id),
        str(Path(workspace).resolve()), copy.deepcopy(spec), str(getattr(state.session, "project_instructions", "") or ""))
    return manager.execution_capture(capture, message.get("execution_mode", "personal"))


def _full_auto_needed(state, manager, capture, message):
    """Why a team the orchestrator runs can't start from this conversation's mode, or None.

    The orchestrator approves plans and accepts results for the owner, so
    starting one, or continuing one after a recovery, needs Full-auto
    (AppState.full_auto_needed): the conversation's, or the owner's grant
    for this one run (``full_auto: true``, the panel's "Run this team in
    Full-auto"), which leaves the conversation's mode as it is. A team the
    owner reviews needs nothing more. Where the organization doesn't allow
    Full-auto, the team's own rules refuse an orchestrated team
    (organization.mode_refusal), grant or not.
    """
    action = message.get("action")
    if action == "start" and message.get("autonomy"):
        work = "team"
    elif action == "continue_recovered" and message.get("run_id") and manager.orchestrated(capture, message["run_id"]):
        work = "team_continue"
    else:
        return None
    check = getattr(state, "full_auto_needed", None)
    if not callable(check):
        return None
    return check(work, granted=message.get("full_auto") is True, session_id=str(message.get("session_id") or ""))


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
        needed = await asyncio.to_thread(_full_auto_needed, state, manager, capture, message)
        if needed:
            # The panel offers to run (or continue) this one team in Full-auto.
            reply.update(error=needed["message"], code=needed["code"], can_grant=needed["can_grant"],
                         work=needed.get("work", ""))
            await send(reply)
            return
        if message.get("action") in {"start", "request_plan", "collaboration_prepare", "collaboration_accept_work", "managed_sharing_prepare", "managed_sharing_accept_work"}:
            state._swarm_starting = starting = True
        # The grant was the desktop's to check; the team's own command fields don't carry it.
        forwarded = {key: value for key, value in message.items() if key != "full_auto"}
        reply.update(await asyncio.to_thread(manager.operate, capture, copy.deepcopy(forwarded)))
    except (ValueError, SwarmError) as exc:
        reply["error"] = str(exc)
    except Exception:
        reply["error"] = "Team storage or runtime is unavailable. Retained work requires inspection before retrying."
    finally:
        if starting:
            state._swarm_starting = False
    await send(reply)
