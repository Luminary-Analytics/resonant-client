"""The checkpoint Timeline: what it lists, what it restores, and whose it is.

Before each change it makes, Lumi saves a checkpoint of the project's files
and the conversation. The Timeline that restored them left the desktop page in
v0.14.0; these cover the commands behind the new one:

- the list names what each checkpoint was saved before, without the call's
  full arguments (a write's arguments hold the whole file);
- a worker's checkpoint holds the worker's conversation, so it restores files
  only;
- checkpoints belong to the saved conversation, so its Timeline outlives a
  rebuilt session (an app restart, a draft's first message).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi.engine.checkpoint_timeline import SessionCheckpointStore
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from lumi.gui import app as gui_app
from lumi.gui import ws_commands
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def _run(handler, ctx):
    asyncio.run(handler(ctx))
    return ctx.ws.sent


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _ctx(session, msg=None):
    state = SimpleNamespace(session=session, project=SimpleNamespace(current_session=None, project_path=""))
    return ws_commands.CommandContext(ws=_Socket(), state=state, msg=msg or {}, runs=SimpleNamespace(busy=False))


def _store(tmp_path: Path) -> tuple[Path, SessionCheckpointStore]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project, SessionCheckpointStore(project, session_id="conversation-1", root=tmp_path / "checkpoints")


def _checkpoint(store, *, tool="file_write", arguments=None, subagent=False, history=None):
    return store.create(
        conversation_history=history or [{"role": "user", "content": "before"}],
        display_events=[{"event": "user_message", "text": "before"}],
        reason=f"Before {tool}",
        tool_name=tool,
        metadata={"call_id": "c1", "arguments": arguments or {}, "subagent": subagent},
    )


def test_the_list_names_each_checkpoint_without_the_calls_contents(tmp_path):
    _, store = _store(tmp_path)
    _checkpoint(store, arguments={"path": "notes.txt", "content": "the whole file " * 500})
    _checkpoint(store, tool="bash", arguments={"command": "npm run build\nnpm test"})
    _checkpoint(store, tool="git_commit", arguments={"message": "Fix the notes\n\nLonger body"}, subagent=True)

    sent = _run(ws_commands.HANDLERS["session_timeline_list"], _ctx(SimpleNamespace(checkpoint_store=store)))

    items = sent[0]["checkpoints"]
    assert [(item["tool_name"], item["target"], item["subagent"]) for item in items] == [
        ("git_commit", "Fix the notes", True),
        ("bash", "npm run build", False),
        ("file_write", "notes.txt", False),
    ]
    assert all(item["snapshot"] in {"git", "archive"} for item in items)
    # Nothing of a write's content reaches the page.
    assert "the whole file" not in str(sent)


def test_a_workers_checkpoint_restores_files_but_never_the_conversation(tmp_path):
    project, store = _store(tmp_path)
    notes = project / "notes.txt"
    notes.write_text("before\n", encoding="utf-8")
    checkpoint = _checkpoint(store, subagent=True, history=[{"role": "user", "content": "the worker's task"}])
    notes.write_text("after\n", encoding="utf-8")
    session = SimpleNamespace(checkpoint_store=store, conversation_history=[{"role": "user", "content": "ours"}],
                              hook_runner=None)

    for mode in ("conversation", "both"):
        sent = _run(ws_commands.HANDLERS["session_timeline_restore"],
                    _ctx(session, {"checkpoint_id": checkpoint.id, "mode": mode}))
        assert sent[0]["event"] == "error"
        assert "worker's conversation" in sent[0]["message"]
    assert session.conversation_history == [{"role": "user", "content": "ours"}]
    assert notes.read_text(encoding="utf-8") == "after\n"

    sent = _run(ws_commands.HANDLERS["session_timeline_restore"],
                _ctx(session, {"checkpoint_id": checkpoint.id, "mode": "files"}))
    assert sent[0]["event"] == "session.timeline_restored"
    assert notes.read_text(encoding="utf-8") == "before\n"
    # The page gets the compact checkpoint, and where the old files went.
    assert sent[0]["data"]["checkpoint"]["target"] == ""
    assert Path(sent[0]["data"]["workspace"]["recovery_archive"]).is_file()


# ── Whose checkpoints ──────────────────────────────────────────────────


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


def _chat_session(state, backend=None):
    session = state.build_session(backend=backend or StreamingBackend(), project_path=state.project.project_path)
    state.session = session
    return session


def test_a_conversations_checkpoints_outlive_a_rebuilt_session(gui_state):
    session = _chat_session(gui_state)
    record = gui_state.ensure_persisted_current_session() or gui_state.project.create_session()
    store = gui_state.bind_conversation_checkpoints(session)
    assert store.session_id == record.id
    assert session.context_broker.checkpoint_store is store
    saved = _checkpoint(store)

    # As after an app restart: a new session, wired with a fresh store.
    rebuilt = _chat_session(gui_state)
    assert rebuilt.checkpoint_store.session_id != record.id
    sent = _run(ws_commands.HANDLERS["session_timeline_list"],
                ws_commands.CommandContext(ws=_Socket(), state=gui_state, msg={}))

    assert [item["id"] for item in sent[0]["checkpoints"]] == [saved.id]
    assert rebuilt.checkpoint_store.session_id == record.id


def test_a_restore_is_marked_in_the_chat_but_never_in_the_conversation(gui_state):
    session = _chat_session(gui_state)
    record = gui_state.ensure_persisted_current_session() or gui_state.project.create_session()
    store = gui_state.bind_conversation_checkpoints(session)
    notes = Path(gui_state.project.project_path) / "notes.txt"
    notes.write_text("before\n", encoding="utf-8")
    asked = [{"role": "user", "content": "write the notes"}]
    # Saved mid-turn, as every checkpoint is: the turn has only started.
    checkpoint = _checkpoint(store, arguments={"path": "notes.txt", "content": "after\n"}, history=asked)
    notes.write_text("after\n", encoding="utf-8")
    later = [*asked, {"role": "assistant", "content": "Wrote it."}, {"role": "user", "content": "thanks"}]
    session.conversation_history = list(later)
    record.conversation_history = list(later)
    record.display_events = [{"event": "user_message", "text": "write the notes"}, {"event": "session.end"}]
    record.save()

    def restore(mode):
        ctx = ws_commands.CommandContext(ws=_Socket(), state=gui_state,
                                         msg={"checkpoint_id": checkpoint.id, "mode": mode})
        sent = _run(ws_commands.HANDLERS["session_timeline_restore"], ctx)
        assert sent[0]["event"] == "session.timeline_restored"
        record.history_snapshot(hydrate=True)  # as saved on disk
        return sent[0]

    # Files only: the chat keeps its history and records the restore after it.
    shown = restore("files")
    assert notes.read_text(encoding="utf-8") == "before\n"
    assert [event["event"] for event in record.display_events] == ["user_message", "session.end", "timeline.restored"]
    marker = record.display_events[-1]
    assert marker["mode"] == "files"
    assert marker["checkpoint"]["id"] == checkpoint.id
    assert marker["checkpoint"]["target"] == "notes.txt"
    assert "after" not in str(marker)  # the compact checkpoint, not the write's contents
    assert shown["display_events"][-1]["event"] == "timeline.restored"
    assert record.conversation_history == later

    # Conversation: the chat goes back to the checkpoint's, then the marker.
    restore("conversation")
    assert [event["event"] for event in record.display_events] == ["user_message", "timeline.restored"]
    assert record.display_events[-1]["mode"] == "conversation"
    # The model's conversation is the checkpoint's, with nothing added.
    assert record.conversation_history == asked
    assert session.conversation_history == asked


def test_a_drafts_first_turn_saves_its_checkpoints_to_the_conversation(gui_state):
    project = Path(gui_state.project.project_path)
    backend = StreamingBackend(scripts=[
        [tool_call("file_write", {"path": "notes.txt", "content": "new\n"}, call_id="w1"), done()],
        [text_delta("Wrote it."), done()],
    ])
    session = _chat_session(gui_state, backend)
    record = gui_state.ensure_persisted_current_session() or gui_state.project.create_session()

    asyncio.run(gui_app._run_session_streaming(_Socket(), session, "write the notes"))

    assert (project / "notes.txt").read_text(encoding="utf-8") == "new\n"
    assert session.checkpoint_store.session_id == record.id
    assert [item.tool_name for item in session.checkpoint_store.list()] == ["file_write"]


def test_a_worker_marks_its_checkpoints(tmp_path):
    backend = StreamingBackend(scripts=[
        [tool_call("file_write", {"path": "parent.txt", "content": "p"}, call_id="p1"), done()],
        [tool_call("task", {"prompt": "write it", "agent_type": "build"}, call_id="t1"), done()],
        [tool_call("file_write", {"path": "worker.txt", "content": "w"}, call_id="w1"), done()],
        [text_delta("Worker finished."), done()],
        [text_delta("Parent finished."), done()],
    ])
    session = Session(backend=backend, max_steps=4, auto_approve=True)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.checkpoint_store = SessionCheckpointStore(tmp_path, session_id="c", root=tmp_path.parent / "cps")

    list(session.run("write, then delegate"))

    marks = {item.metadata["arguments"]["path"]: item.metadata["subagent"] for item in session.checkpoint_store.list()}
    assert marks == {"parent.txt": False, "worker.txt": True}
