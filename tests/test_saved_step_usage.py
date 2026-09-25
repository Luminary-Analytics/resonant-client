"""Each step's model and token counts are saved with the conversation.

The app saves ``step.end`` with a conversation but never ``status``, so a
replayed turn's ``▣ model · tokens · time`` footer (``handleStepEnd`` in
lumi/gui/static/app.js) can show only what ``step.end`` carries. These drive
the real engine loop, and the real ``_run_session_streaming``, with a scripted
model; no provider is called.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lumi.engine.session import Session
from lumi.gui import app as gui_app
from tests.streaming_stub import StreamingBackend, done, events_of_kind, text_delta, tool_call

# A call that reported neither its model nor any token counts.
_BARE_DONE = ("done", {"model": None, "stats": None, "cognitive_state": None})


def _numbers(events: list[dict]) -> list[dict]:
    return [{key: event.get(key) for key in ("step", "model", "input_tokens", "output_tokens")}
            for event in events_of_kind(events, "step.end")]


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


def test_a_saved_turn_keeps_each_steps_own_model_and_tokens(gui_state):
    (Path(gui_state.project.project_path) / "notes.txt").write_text("alpha\n", encoding="utf-8")
    backend = StreamingBackend(model="stub:latest", scripts=[
        # Ollama's names for the counts, and the model tag the server resolved.
        [tool_call("file_read", {"path": "notes.txt"}, call_id="r1"),
         done(model="stub:7b", stats={"prompt_eval_count": 1200, "eval_count": 80})],
        [text_delta("The notes say alpha."), _BARE_DONE],
    ])
    session = gui_state.build_session(backend=backend, project_path=gui_state.project.project_path)
    gui_state.session = session
    record = gui_state.ensure_persisted_current_session() or gui_state.project.create_session()
    socket = _Socket()

    asyncio.run(gui_app._run_session_streaming(socket, session, "what do the notes say?"))

    record.history_snapshot(hydrate=True)  # as a reload reads it from disk
    saved = record.display_events
    assert not events_of_kind(saved, "status")  # still never saved
    assert _numbers(saved) == [
        {"step": 1, "model": "stub:7b", "input_tokens": 1200, "output_tokens": 80},
        # Nothing reported: no counts (not the step before's), and the
        # connection's model, as this call's usage record names it.
        {"step": 2, "model": "stub:latest", "input_tokens": 0, "output_tokens": 0},
    ]
    # The page saw the same numbers live.
    assert _numbers(socket.sent) == _numbers(saved)


def test_a_workers_steps_carry_the_workers_own_numbers(tmp_path):
    backend = StreamingBackend(model="turn-model", scripts=[
        [tool_call("task", {"prompt": "look around", "agent_type": "build"}, call_id="t1"),
         done(model="turn-model", stats={"input_tokens": 500, "output_tokens": 50})],
        [text_delta("Nothing to change."), done(model="worker-model", stats={"input_tokens": 40, "output_tokens": 4})],
        [text_delta("The worker found nothing."), done(model="turn-model", stats={"input_tokens": 700, "output_tokens": 70})],
    ])
    session = Session(backend=backend, max_steps=4, auto_approve=True)
    session.project_path = str(tmp_path)

    events = list(session.run("delegate a look"))

    ends = [(bool(event.get("_subagent")), event["model"], event["input_tokens"], event["output_tokens"])
            for event in events_of_kind(events, "step.end")]
    assert ends == [
        (True, "worker-model", 40, 4),
        (False, "turn-model", 500, 50),
        (False, "turn-model", 700, 70),
    ]
