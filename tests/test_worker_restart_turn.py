"""Restarting a worker from the app is a complete turn of its own.

The app runs a restart (`agent_restart` in lumi/gui/app.py) with the worker's
events as the run's source. What the run records is what a reload replays, and
a turn without session.end replays as interrupted, offering to resume work
that finished. These drive the real `_run_session_streaming` with a scripted
model; no provider is called.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lumi.engine.agent_runtime import AgentRegistry, AgentStatus
from lumi.gui import app as gui_app
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


@pytest.fixture
def gui_state(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(gui_app.AppState, "detect_backends", lambda self, force=False: {})
    state = gui_app.AppState()
    state.apply_project_context(str(project))
    state.project.current_session = None
    monkeypatch.setattr(gui_app, "state", state)
    return state


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _stuck_worker(session, prompt: str) -> str:
    """A worker that was running when the app closed, as the app reloads it."""
    registry = session.agent_registry
    record = registry.create(agent_type="build", prompt=prompt)
    registry.transition(record.id, AgentStatus.RUNNING)
    session.agent_registry = AgentRegistry(registry.project_path, root=registry.root)
    assert session.agent_registry.get(record.id).status == AgentStatus.STUCK.value
    return record.id


def test_a_restarted_worker_is_recorded_as_a_finished_turn(gui_state):
    project = Path(gui_state.project.project_path)
    (project / "notes.txt").write_text("old line\n", encoding="utf-8")
    backend = StreamingBackend(scripts=[
        [tool_call("file_edit", {"path": "notes.txt", "old_text": "old line", "new_text": "new line"},
                   call_id="w1"), done()],
        [text_delta("Fixed the notes."), done()],
    ])
    session = gui_state.build_session(backend=backend, project_path=str(project))
    gui_state.session = session
    agent_id = _stuck_worker(session, "Replace old line with new line in notes.txt")
    socket = _Socket()

    display = asyncio.run(gui_app._run_session_streaming(
        socket, session, "Replace old line with new line in notes.txt",
        display_user_msg="Restarting build agent (interrupted after 0 steps)",
        event_source=lambda on_permission: session.restart_agent(agent_id, on_permission=on_permission),
    ))

    assert (project / "notes.txt").read_text(encoding="utf-8") == "new line\n"
    kinds = [event.get("event") for event in display]
    assert kinds[0] == "user_message"
    assert "subagent.start" in kinds and "subagent.end" in kinds
    # The turn closes, so a reload replays it as finished, not interrupted.
    assert kinds[-1] == "session.end"
    end = display[-1]
    assert end["outcome"] == "changed_unverified"
    assert end["evidence"]["changed_files"] == ["notes.txt"]
    # The page saw the same turn live: it started and it ended.
    live = [event.get("event") for event in socket.sent]
    assert live.index("session.start") < live.index("subagent.start") < live.index("session.end")
