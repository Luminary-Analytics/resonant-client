"""Native execution admission and observations, exercised without model calls."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from lumi.engine.compression import compress
from lumi.engine.execution_guard import ExecutionGuardError
from lumi.engine.hooks import HookResult, HookType
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from lumi.engine.tools import AGENT_TOOLS, ToolResult
from tests.streaming_stub import StreamingBackend, done, error, text_delta, tool_call


class RecordingGuard:
    """A trusted fixture ledger which can revoke or fail at exact boundaries."""

    def __init__(self):
        self.records = []
        self.stopped = False
        self.fail = ""
        self.requests = 0
        self.tools = 0
        self.after_tool = None
        self.artifact_reader = None

    def _record(self, kind, **data):
        if self.fail == kind:
            raise OSError("fixture persistence unavailable")
        self.records.append({"kind": kind, **copy.deepcopy(data)})

    def _admit(self):
        if self.stopped:
            raise RuntimeError("fixture authority revoked")

    def begin_request(self, *, purpose, inputs):
        self._admit()
        self.requests += 1
        request_id = f"request-{self.requests}"
        self._record("request.begin", request_id=request_id, purpose=purpose, inputs=inputs)
        return request_id

    def end_request(self, request_id, *, outcome, usage, error):
        self._record("request.end", request_id=request_id, outcome=outcome, usage=usage, error=error)

    def check_tool(self, request_id, name, arguments):
        self._admit()
        self._record("tool.check", request_id=request_id, name=name, arguments=arguments)

    def begin_tool(self, request_id, call_id, name, arguments, arguments_sha256):
        self._admit()
        self.tools += 1
        receipt_id = f"receipt-{self.tools}"
        self._record("tool.begin", request_id=request_id, call_id=call_id, name=name,
                     arguments=arguments, arguments_sha256=arguments_sha256, receipt_id=receipt_id)
        return receipt_id

    def end_tool(self, receipt_id, *, outcome, output, is_error, metadata):
        self._record("tool.end", receipt_id=receipt_id, outcome=outcome,
                     output=output, is_error=is_error, metadata=metadata)
        if self.after_tool:
            self.after_tool()


def make_session(tmp_path, backend, guard, *, allowed=None):
    session = Session(backend, execution_guard=guard, max_steps=2, allowed_tools=allowed)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path))
    return session


def entries(guard, kind):
    return [entry for entry in guard.records if entry["kind"] == kind]


@pytest.mark.parametrize("name,args", [
    ("task", {"prompt": "spawn", "agent_type": "build"}),
    ("task_batch", {"tasks": [{"prompt": "spawn"}]}),
    ("mcp_fixture_write", {"path": "outside.txt"}),
    ("await_user", {"question": "Authorize more work"}),
    ("batch", {"calls": [{"name": "file_read", "arguments": {"path": "x"}}]}),
    ("bash", {"command": "echo forbidden"}),
    ("file_write", {"path": "created.txt", "content": "forbidden"}),
])
def test_forged_special_tools_are_rejected_before_hooks(tmp_path, name, args):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call(name, args), done()])
    session = make_session(tmp_path, backend, guard)
    hook_calls = []

    class Hooks:
        def run_hooks(self, *args, **kwargs):
            hook_calls.append(kwargs)
            return HookResult()

    events = list(session.run("Inspect files"))
    assert any(event.get("code") == "execution_guard_blocked" for event in events)
    assert hook_calls == []
    assert not entries(guard, "tool.begin")
    assert not list(tmp_path.iterdir())
    assert entries(guard, "request.end")[0]["outcome"] == "completed"


def test_session_allowlist_is_narrower_than_guard_envelope(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("file_read", {"path": "data.txt"}), done()])
    allowed = [tool for tool in AGENT_TOOLS if tool["function"]["name"] == "glob"]
    session = make_session(tmp_path, backend, guard, allowed=allowed)
    assert {tool["function"]["name"] for tool in session.tools} == {"glob"}
    list(session.run("Inspect files"))
    assert not entries(guard, "tool.begin")


def test_effectful_hooks_are_rejected_before_any_invocation(tmp_path):
    (tmp_path / "inside.txt").write_text("visible", encoding="utf-8")
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("file_read", {"path": "inside.txt"}), done()])
    session = make_session(tmp_path, backend, guard)

    class Hooks:
        def run_hooks(self, hook_type, **kwargs):
            assert hook_type == HookType.PRE_TOOL_USE
            return HookResult(modified_args={"path": str(tmp_path.parent / "escape.txt")})

    session.hook_runner = Hooks()
    with pytest.raises(ExecutionGuardError, match="Lifecycle hooks"):
        list(session.run("Inspect files"))
    assert not entries(guard, "tool.begin")
    assert backend.stream_count == 0


def test_permission_callback_changes_are_rechecked(tmp_path, monkeypatch):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("file_read", {"path": "inside.txt"}), done()])
    session = make_session(tmp_path, backend, guard)
    monkeypatch.setattr(session, "_should_auto_approve", lambda name: False)

    def permission(name, arguments):
        arguments["path"] = str(tmp_path.parent / "escape.txt")
        return True

    events = list(session.run("Inspect files", on_permission=permission))
    assert any(event.get("code") == "execution_guard_blocked" for event in events)
    assert not entries(guard, "tool.begin")


def test_actual_file_observation_is_bound_before_outward_result(tmp_path):
    (tmp_path / "data.txt").write_text("attributable finding", encoding="utf-8")
    guard = RecordingGuard()

    class Backend(StreamingBackend):
        def stream(self, **kwargs):
            assert entries(guard, "request.begin")
            yield from super().stream(**kwargs)
            # A done marker is not sufficient until the iterator is drained.
            assert not entries(guard, "tool.begin") if self.stream_count == 1 else True

    backend = Backend(scripts=[
        [tool_call("file_read", {"path": "data.txt"}, "read-1"), done()],
        [text_delta("Found the requested information."), done()],
    ])
    session = make_session(tmp_path, backend, guard)
    for event in session.run("Inspect files", input_origin="generated"):
        if event["event"] == "tool.result":
            observed = entries(guard, "tool.end")
            assert observed and "attributable finding" in observed[0]["output"]
            assert event["metadata"]["model_request_id"] == "request-1"
            assert event["metadata"]["action_receipt_id"] == "receipt-1"
    admitted = entries(guard, "tool.begin")[0]
    assert admitted["call_id"] == "read-1"
    expected = hashlib.sha256(json.dumps(admitted["arguments"], sort_keys=True,
                                       separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert admitted["arguments_sha256"] == expected
    assert guard.records.index(entries(guard, "request.end")[0]) < guard.records.index(admitted)
    assert entries(guard, "request.begin")[0]["inputs"]["conversation_history"][0]["input_origin"] == "generated"


def test_scoped_artifact_result_uses_the_same_observation_boundary(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(scripts=[
        [tool_call("artifact_read", {"artifact_id": "authorized"}), done()],
        [text_delta("Found evidence."), done()],
    ])
    session = make_session(tmp_path, backend, guard)

    class Artifacts:
        def read_text_page(self, artifact_id):
            assert artifact_id == "authorized"
            assert entries(guard, "tool.begin")
            return "scoped evidence"

    session.artifact_store = Artifacts()
    guard.artifact_reader = session.artifact_store
    for event in session.run("Inspect evidence"):
        if event["event"] == "tool.result":
            assert entries(guard, "tool.end")[0]["output"] == event["output"] == "scoped evidence"


@pytest.mark.parametrize("failure", ["request.begin", "request.end", "tool.begin", "tool.end"])
def test_persistence_failure_closes_admission_without_outward_tool_success(tmp_path, failure):
    (tmp_path / "data.txt").write_text("finding", encoding="utf-8")
    guard = RecordingGuard()
    guard.fail = failure
    backend = StreamingBackend(events=[tool_call("file_read", {"path": "data.txt"}), done()])
    session = make_session(tmp_path, backend, guard)
    events = list(session.run("Inspect files"))
    assert any(event["event"] == "error" for event in events)
    assert not any(event["event"] == "tool.result" for event in events)
    assert backend.stream_count == (0 if failure == "request.begin" else 1)
    guard.fail = ""
    with pytest.raises(ExecutionGuardError, match="closed"):
        list(session.run("Continue"))


@pytest.mark.parametrize("stop_before", ["second_tool", "next_request"])
def test_stop_prevents_the_next_tool_or_model_request(tmp_path, stop_before):
    (tmp_path / "data.txt").write_text("finding", encoding="utf-8")
    guard = RecordingGuard()
    script = [tool_call("file_read", {"path": "data.txt"}, "first")]
    if stop_before == "second_tool":
        script.append(tool_call("file_read", {"path": "data.txt"}, "second"))
    script.append(done())
    backend = StreamingBackend(events=script)
    guard.after_tool = lambda: setattr(guard, "stopped", True)
    session = make_session(tmp_path, backend, guard)
    events = list(session.run("Inspect files"))
    assert len(entries(guard, "tool.begin")) == 1
    assert backend.stream_count == 1
    assert any(event["event"] == "error" for event in events)


@pytest.mark.parametrize("events", [
    [tool_call("file_read", {"path": "data.txt"})],
    [tool_call("file_read", {"path": "data.txt"}), error("provider failed")],
    [tool_call("file_read", {"path": "data.txt"}), done(), error("late failure")],
])
def test_incomplete_or_failed_stream_never_dispatches_tools(tmp_path, events):
    guard = RecordingGuard()
    backend = StreamingBackend(events=events)
    session = make_session(tmp_path, backend, guard)
    observed = list(session.run("Inspect files"))
    assert any(event["event"] == "error" for event in observed)
    assert entries(guard, "request.end")[0]["outcome"] == "uncertain"
    assert not entries(guard, "tool.begin")


def test_provider_exception_is_retained_as_uncertain(tmp_path):
    guard = RecordingGuard()
    session = make_session(tmp_path, StreamingBackend(raise_on_stream=OSError("lost response")), guard)
    list(session.run("Inspect files"))
    receipt = entries(guard, "request.end")[0]
    assert receipt["outcome"] == "uncertain"
    assert "OSError" in receipt["error"]
    assert "lost response" not in receipt["error"]  # Raw provider diagnostics may contain credentials.


def test_closing_a_live_session_stream_preserves_request_uncertainty(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[text_delta("Partial output"), done()])
    session = make_session(tmp_path, backend, guard)
    events = session.run("Inspect files")
    for event in events:
        if event["event"] == "text.delta":
            break
    assert not entries(guard, "request.end")
    events.close()
    assert entries(guard, "request.end")[0]["outcome"] == "uncertain"


def test_cli_backend_is_rejected_before_invocation(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(handles_tools=True, events=[done()])
    session = make_session(tmp_path, backend, guard)
    with pytest.raises(ExecutionGuardError, match="native backend"):
        list(session.run("Inspect files"))
    assert not guard.records and backend.stream_count == 0


def test_auxiliary_planning_is_accounted_and_failure_cannot_be_swallowed(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(classify_response="COMPLEX")
    session = make_session(tmp_path, backend, guard)
    assert session.should_plan("Inspect a large system")
    assert entries(guard, "request.begin")[0]["purpose"] == "planning"
    assert entries(guard, "request.begin")[0]["inputs"]["_model_selection"] == {
        "provider": backend.name, "model": backend.model,
    }
    assert entries(guard, "request.end")[0]["outcome"] == "completed"
    backend._classify_should_raise = True
    with pytest.raises(ExecutionGuardError):
        session.should_plan("Inspect another system")
    assert entries(guard, "request.end")[-1]["outcome"] == "uncertain"


def test_guarded_artifact_read_cannot_fall_back_to_project_wide_store(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("artifact_read", {"artifact_id": "foreign"}), done()])
    session = make_session(tmp_path, backend, guard)

    class ProjectArtifacts:
        def read_text_page(self, **kwargs):
            pytest.fail("A guarded worker read the project-wide artifact store")

    session.artifact_store = ProjectArtifacts()
    events = list(session.run("Inspect evidence"))
    assert any(event.get("code") == "execution_guard_unresolved" for event in events)
    assert entries(guard, "tool.end")[0]["outcome"] == "uncertain"


def test_request_identity_cannot_be_overridden_by_caller_inputs(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[done()])
    session = make_session(tmp_path, backend, guard)
    with pytest.raises(ExecutionGuardError, match="runtime-owned"):
        list(session._model_stream(_model_selection={"provider": "other", "model": "other"}))
    assert not guard.records and backend.stream_count == 0


def test_model_mutation_during_admission_cannot_invoke_a_different_backend_model(tmp_path):
    backend = StreamingBackend(events=[done()])

    class MutatingGuard(RecordingGuard):
        def begin_request(self, **kwargs):
            request_id = super().begin_request(**kwargs)
            backend.model = "unapproved-model"
            return request_id

    guard = MutatingGuard()
    session = make_session(tmp_path, backend, guard)
    events = list(session.run("Inspect files"))
    assert any("selection changed" in event.get("message", "") for event in events)
    assert backend.stream_count == 0
    assert entries(guard, "request.end")[0]["outcome"] == "uncertain"


def test_compression_keeps_auxiliary_purpose_and_requires_full_drain(tmp_path):
    guard = RecordingGuard()
    summary = {key: "Recorded facts" for key in
               ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")}

    class Backend(StreamingBackend):
        purpose = None

        def stream_auxiliary(self, *, purpose, **kwargs):
            self.purpose = purpose
            assert entries(guard, "request.begin")[-1]["purpose"] == "compression"
            yield text_delta(json.dumps(summary))
            yield done()
            assert not entries(guard, "request.end")

    backend = Backend()
    session = make_session(tmp_path, backend, guard)
    session.conversation_history = [{"role": "user", "content": "Prior context " * 200} for _ in range(9)]
    compress(session, max_tokens=20)
    assert backend.purpose == "compression"
    assert entries(guard, "request.end")[0]["outcome"] == "completed"


def test_unguarded_session_keeps_ordinary_tool_behavior_and_human_history(tmp_path):
    backend = StreamingBackend(scripts=[
        [tool_call("await_user", {"question": "A specific detail"}), done()],
        [text_delta("Answered."), done()],
    ])
    session = Session(backend, max_steps=2)
    events = list(session.run("Inspect files", on_user_input=lambda *args: "fixture reply"))
    assert any(event.get("output") == "fixture reply" for event in events)
    assert "input_origin" not in session.conversation_history[0]


def test_generated_messages_are_context_data_not_human_goal_revisions(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[text_delta("Recorded the finding."), done()])
    session = make_session(tmp_path, backend, guard)
    session.steer("Peer finding", message_id="message-1", input_origin="generated")
    list(session.run("Inspect files", input_origin="generated"))
    history = entries(guard, "request.begin")[0]["inputs"]["conversation_history"]
    delivered = next(entry for entry in history if entry.get("message_id") == "message-1")
    assert delivered["input_origin"] == "generated"
    assert delivered["content"] == "<runtime_message>\nPeer finding\n</runtime_message>"
    assert not any("Additional live user direction" in str(entry.get("content")) for entry in history)


def test_provider_consumes_the_admitted_snapshot_when_live_history_changes(tmp_path):
    class MutatingGuard(RecordingGuard):
        def begin_request(self, **kwargs):
            result = super().begin_request(**kwargs)
            session.conversation_history[0]["content"] = "changed after admission"
            return result

    guard = MutatingGuard()

    class Backend(StreamingBackend):
        def stream(self, **kwargs):
            assert kwargs["conversation_history"] == entries(guard, "request.begin")[0]["inputs"]["conversation_history"]
            assert kwargs["conversation_history"][0]["content"] == "original input"
            yield text_delta("Found the information.")
            yield done()

    session = make_session(tmp_path, Backend(), guard)
    list(session.run("original input"))


def test_malformed_tool_result_keeps_admitted_action_uncertain(tmp_path, monkeypatch):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("file_read", {"path": "data.txt"}), done()])
    session = make_session(tmp_path, backend, guard)
    monkeypatch.setattr("lumi.engine.session.execute_tool", lambda *args, **kwargs: object())
    events = list(session.run("Inspect files"))
    assert entries(guard, "tool.end")[0]["outcome"] == "uncertain"
    assert not any(event["event"] == "tool.result" for event in events)
    with pytest.raises(ExecutionGuardError, match="closed"):
        list(session.run("Continue"))


def test_fixed_runtime_handler_has_a_request_bound_observation(tmp_path):
    guard = RecordingGuard()
    schema = {"type": "function", "function": {"name": "swarm_status", "description": "Read scoped status",
              "parameters": {"type": "object", "properties": {}}}}
    backend = StreamingBackend(scripts=[
        [tool_call("swarm_status", {}, "status-1"), done()],
        [text_delta("Read the status."), done()],
    ])
    calls = []

    def status(arguments):
        calls.append(arguments)
        assert entries(guard, "tool.begin")[-1]["call_id"] == "status-1"
        return ToolResult("authorized status")

    session = Session(backend, max_steps=2, execution_guard=guard, allowed_tools=[schema],
                      guarded_tool_handlers={"swarm_status": status})
    events = list(session.run("Inspect status", input_origin="generated"))
    assert calls == [{}]
    assert any(event.get("output") == "authorized status" for event in events)
    assert entries(guard, "tool.end")[0]["output"] == "authorized status"


def test_unregistered_runtime_handler_cannot_be_invented(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[tool_call("swarm_status", {}), done()])
    session = make_session(tmp_path, backend, guard)
    events = list(session.run("Inspect status"))
    assert any(event.get("code") == "execution_guard_blocked" for event in events)
    assert not entries(guard, "tool.begin")


def test_unsupported_classification_does_not_fabricate_request_usage(tmp_path):
    guard = RecordingGuard()
    backend = StreamingBackend()
    backend.classify = None
    session = make_session(tmp_path, backend, guard)
    assert not session.should_plan("Inspect files")
    assert guard.records == []


@pytest.mark.parametrize("sidecar,value", [("checkpoint_store", object()), ("auto_lint_enabled", True),
                                          ("auto_test_enabled", True)])
def test_unbound_effectful_sidecars_are_rejected_before_model_admission(tmp_path, sidecar, value):
    guard = RecordingGuard()
    backend = StreamingBackend(events=[done()])
    session = make_session(tmp_path, backend, guard)
    setattr(session, sidecar, value)
    with pytest.raises(ExecutionGuardError, match="sidecars"):
        list(session.run("Perform bounded work"))
    assert not guard.records and backend.stream_count == 0
