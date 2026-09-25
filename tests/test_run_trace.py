"""A run's trace and saved files, as its run card opens them.

A session's flight recorder lives across its turns. Each top-level turn is its
own slice of the trace, named on the turn's session.end, so a run card can open
it after a reload:

- a worker's events belong to the turn that started it;
- an event the engine records as it yields it is recorded once, even though
  the app records every event it streams;
- the Trace dialog gets what each event was and when, never a call's contents;
- a turn's trace exports on its own, and reading or exporting it never
  rewrites the run a session may still be recording;
- the saved-file viewer reads files by their id, never by a path from the page.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from lumi.engine.artifacts import ArtifactStore
from lumi.engine.checkpoint_timeline import SessionCheckpointStore
from lumi.engine.flight_recorder import FlightRecorder
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from lumi.gui import ws_commands
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _send(command: str, project: Path, msg: dict, store: ArtifactStore | None = None) -> list[dict]:
    state = SimpleNamespace(
        session=SimpleNamespace(artifact_store=store),
        project=SimpleNamespace(current_session=None, project_path=str(project)),
    )
    ctx = ws_commands.CommandContext(ws=_Socket(), state=state, msg=msg)
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def _session(project: Path, scripts) -> Session:
    session = Session(backend=StreamingBackend(scripts=scripts), max_steps=4, auto_approve=True)
    session.project_path = str(project)
    session.sandbox = PathSandbox(str(project), enabled=True)
    session.flight_recorder = FlightRecorder(project)
    return session


def _turn(session: Session, prompt: str) -> list[dict]:
    """One turn, logged as the app logs it: every event it streams."""
    events = []
    for event in session.run(prompt):
        session._log_event(event)
        events.append(event)
    return events


def test_each_turn_records_its_own_slice_of_the_trace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    session = _session(project, [
        [tool_call("file_write", {"path": "notes.txt", "content": "one\n"}, call_id="w1"), done()],
        [text_delta("Wrote it."), done()],
        [text_delta("Hello again."), done()],
    ])
    session.checkpoint_store = SessionCheckpointStore(project, session_id="c", root=tmp_path / "cps")

    first = _turn(session, "write the notes")
    second = _turn(session, "say hello")

    ends = [event for event in first + second if event["event"] == "session.end"]
    traces = [end["trace"] for end in ends]
    assert [trace["run_id"] for trace in traces] == [session.flight_recorder.run_id] * 2
    assert traces[0]["turn_id"] != traces[1]["turn_id"]

    recorder = session.flight_recorder
    one = recorder.turn_events(traces[0]["turn_id"])
    kinds = [event["event"] for event in one]
    # The engine records session.start and the checkpoint as it yields them,
    # and the app records them again: the trace keeps each once.
    assert kinds.count("session.start") == 1
    assert kinds.count("checkpoint.created") == 1
    assert "tool.call" in kinds and kinds[-1] == "session.end"
    assert all(event["event"] != "text.done" or event["text"] == "Wrote it." for event in one)
    # The trace's own sequence wins over the checkpoint event's.
    sequences = [event["sequence"] for event in one]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)

    two = recorder.turn_events(traces[1]["turn_id"])
    assert [event.get("text") for event in two if event["event"] == "text.done"] == ["Hello again."]
    assert {event["turn_id"] for event in recorder.events()} == {trace["turn_id"] for trace in traces}


def test_a_workers_events_belong_to_the_turn_that_started_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    session = _session(project, [
        [tool_call("task", {"prompt": "write it", "agent_type": "build"}, call_id="t1"), done()],
        [tool_call("file_write", {"path": "worker.txt", "content": "w"}, call_id="w1"), done()],
        [text_delta("Worker finished."), done()],
        [text_delta("Parent finished."), done()],
    ])

    events = _turn(session, "delegate it")

    trace = next(event["trace"] for event in events
                 if event["event"] == "session.end" and not event.get("_subagent"))
    # The worker's own session.end names no trace: it has none of its own.
    assert all("trace" not in event for event in events
               if event["event"] == "session.end" and event.get("_subagent"))
    recorded = session.flight_recorder.events()
    assert {event["turn_id"] for event in recorded} == {trace["turn_id"]}
    assert any(event["event"] == "tool.call" and event.get("name") == "file_write" for event in recorded)


def _recorded_turns(project: Path) -> tuple[FlightRecorder, str, str]:
    recorder = FlightRecorder(project)
    first = recorder.begin_turn()
    recorder.record({"event": "session.start", "model": "stub:latest", "backend": "ollama"}, agent_id="agt_main")
    recorder.record({"event": "tool.call", "name": "file_write",
                     "arguments": {"path": "notes.txt", "content": "the whole secret file"}}, agent_id="agt_main")
    recorder.record({"event": "tool.result", "name": "bash", "output": "x" * 60_000, "elapsed": 0.25,
                     "metadata": {"artifact": {"id": "art_1", "label": "bash result"}}}, agent_id="agt_main")
    recorder.record({"event": "step.start", "step": 1, "_subagent": True, "_agent_type": "build"}, agent_id="agt_main")
    recorder.record({"event": "tool.call", "name": "file_edit", "arguments": {"path": "w.txt"}}, agent_id="agt_worker")
    recorder.record({"event": "status", "stats": {"prompt_eval_count": 120, "eval_count": 30}}, agent_id="agt_main")
    recorder.record({"event": "text.delta", "delta": "hidden"}, agent_id="agt_main")
    recorder.record({"event": "session.end", "outcome": "changed_unverified"}, agent_id="agt_main")
    second = recorder.begin_turn()
    recorder.record({"event": "session.start", "model": "stub:latest"}, agent_id="agt_main")
    recorder.close()
    return recorder, first, second


def test_the_trace_dialog_gets_rows_not_contents(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    recorder, first, _ = _recorded_turns(project)

    sent = _send("flight_recorder_detail", project, {"run_id": recorder.run_id, "turn_id": first})

    reply = sent[0]
    assert reply["event"] == "flight.recorder_detail" and reply["turn_id"] == first
    trace = reply["trace"]
    assert (trace["model"], trace["backend"], trace["outcome"], trace["events"]) == (
        "stub:latest", "ollama", "changed_unverified", 8)
    rows = trace["rows"]
    assert [row["event"] for row in rows] == [
        "session.start", "tool.call", "tool.result", "step.start", "tool.call", "status", "session.end"]
    assert rows[1]["target"] == "notes.txt"
    assert (rows[2]["chars"], rows[2]["artifact"], rows[2]["elapsed"]) == (60_000, "bash result", 0.25)
    # A worker's events: passed on by its parent, or recorded by the worker.
    assert (rows[3]["worker"], rows[4]["worker"]) == ("build", "worker")
    assert rows[5]["tokens"] == [120, 30]
    assert all(row["at"] >= 0 for row in rows)
    # Nothing of a call's arguments or a result's text reaches the page.
    assert "the whole secret file" not in json.dumps(sent) and "xxxx" not in json.dumps(sent)


def test_an_unknown_trace_says_so_in_its_dialog(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    recorder, _, _ = _recorded_turns(project)

    for msg in ({"run_id": recorder.run_id, "turn_id": "turn_gone"},
                {"run_id": "../../outside", "turn_id": "turn_x"}):
        sent = _send("flight_recorder_detail", project, msg)
        assert sent == [{"event": "error", "message": "This run's trace is no longer saved.",
                         "source": "trace", "turn_id": msg["turn_id"]}]


def test_a_turns_trace_exports_on_its_own_without_touching_the_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    recorder, first, _ = _recorded_turns(project)
    store = ArtifactStore(project)
    manifest = recorder.run_dir / "manifest.json"
    before = manifest.read_text(encoding="utf-8")

    _send("flight_recorder_detail", project, {"run_id": recorder.run_id, "turn_id": first})
    sent = _send("flight_recorder_export", project, {"run_id": recorder.run_id, "turn_id": first}, store)

    assert manifest.read_text(encoding="utf-8") == before
    reply = sent[0]
    assert reply["event"] == "artifact.created" and reply["turn_id"] == first
    saved = reply["artifact"]
    assert (saved["kind"], saved["media_type"], Path(saved["path"]).suffix) == ("trace", "application/json", ".json")
    payload = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 8
    resource = {item["key"]: item["value"]["stringValue"] for item in payload["resourceSpans"][0]["resource"]["attributes"]}
    assert resource["lumi.turn_id"] == first


def test_the_viewer_reads_saved_files_by_id(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = ArtifactStore(project)
    output = store.put_text("a" * 16000 + "b" * 100, kind="terminal", label="bash result")
    image = store.put_bytes(b"\x89PNG\r\n\x1a\nfake", kind="image", media_type="image/png", suffix=".png")
    binary = store.put_bytes(b"\x00\x01", kind="binary")

    first = _send("artifact_view", project, {"artifact_id": output.id}, store)[0]
    assert first["event"] == "artifact.view" and first["artifact"]["label"] == "bash result"
    assert (first["text"], first["next_offset"]) == ("a" * 16000, 16000)
    rest = _send("artifact_view", project, {"artifact_id": output.id, "offset": 16000}, store)[0]
    assert (rest["text"], rest["next_offset"]) == ("b" * 100, None)

    shown = _send("artifact_view", project, {"artifact_id": image.id}, store)[0]
    assert shown["image"].startswith("data:image/png;base64,")
    assert "note" in _send("artifact_view", project, {"artifact_id": binary.id}, store)[0]

    # Only a saved file's id opens it; a path from the page is no id.
    for artifact_id in ("art_missing", output.path, str(project / "notes.txt")):
        assert _send("artifact_view", project, {"artifact_id": artifact_id}, store) == [{
            "event": "error", "message": "This file is no longer saved.",
            "source": "artifact", "artifact_id": artifact_id,
        }]
