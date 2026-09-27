"""Bounded fake-provider proofs of worker ownership, dispatch and controls."""

import copy
from dataclasses import replace
import json
import threading
import time
import uuid

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.models import Conflict, RevisionConflict, ScopeDenied
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.tools import SWARM_TOOL_NAMES
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import StreamingBackend, done, error, text_delta, tool_call


def command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                    authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Actual isolated fact", encoding="utf-8")
    store = SwarmStore(tmp_path / "runtime" / "state.sqlite")
    supervisor = SwarmSupervisor(store)
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect isolated facts", request_limit=20,
        policy=PolicyProfile(1, tools, frozenset({"ollama"})), lease_seconds=300)
    command(supervisor, authority, "plan", {"work_items": [
        {"id": name, "objective": f"Inspect {name} facts", "read_roots": ["."],
         "write_roots": [], "tools": sorted(tools), "criteria": ["fact"]}
        for name in ("first", "second")]})
    return supervisor, authority, workspace


def assign(fixture, item="first", requests=3):
    supervisor, authority, _ = fixture
    result = command(supervisor, authority, "assign", {"work_item_id": item,
        "worker_id": item, "requests": requests,
        "model": {"provider": "ollama", "model": "chosen"}}).result
    return AttemptContext(authority.scope, authority.run_id, result["attempt_id"], item, authority.epoch)


def snapshot(fixture):
    supervisor, authority, _ = fixture
    return supervisor.store.snapshot(authority.scope, authority.run_id)


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    assert predicate(), "Fixture condition did not become true before timeout"


class Backend(StreamingBackend):
    def __init__(self, **kwargs):
        super().__init__(model="chosen", **kwargs)
        self.closed = False
        self.captured = []

    def stream(self, **kwargs):
        self.captured.append(copy.deepcopy({key: value for key, value in kwargs.items()
                                           if key != "cancel_event"}))
        yield from super().stream(**kwargs)

    def close(self):
        self.closed = True


class GatedBackend(Backend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.generator_closed = False

    def stream(self, **kwargs):
        try:
            self.entered.set()
            assert self.release.wait(5), "Fixture provider was not released"
            yield from super().stream(**kwargs)
        finally:
            self.generator_closed = True


def runner(fixture, factory, **kwargs):
    supervisor, authority, workspace = fixture
    return SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=factory, **kwargs)


def finished(runtime, context):
    return until(lambda: not runtime.inspect(context.attempt_id)["alive"])


def test_dispatch_is_nonblocking_and_peers_finish_independently(fixture):
    slow = GatedBackend(events=[text_delta("Slow findings."), done()])
    fast = Backend(events=[text_delta("Fast findings."), done()])
    providers = iter([slow, fast])
    runtime = runner(fixture, lambda spec: next(providers))
    first, second = assign(fixture), assign(fixture, "second")
    try:
        started = time.monotonic()
        runtime.start(first, BackendSpec("ollama", "chosen"))
        assert time.monotonic() - started < 1
        assert slow.entered.wait(2)
        runtime.start(second, BackendSpec("ollama", "chosen"))
        finished(runtime, second)
        assert runtime.inspect(second.attempt_id)["state"] == "submitted"
        assert runtime.inspect(first.attempt_id)["alive"]
        events = runtime.poll()["events"]
        assert any(event["event"] == "text.delta" and event["attempt_id"] == second.attempt_id for event in events)
        assert not any(event["event"] == "worker.stopped" and event["attempt_id"] == first.attempt_id for event in events)
        slow.release.set()
        finished(runtime, first)
        assert slow.generator_closed and slow.closed and fast.closed
        assert all(item["state"] == "submitted" for item in snapshot(fixture)["attempts"])
        assert snapshot(fixture)["run"]["state"] == "running"  # Submission is not acceptance.
    finally:
        slow.release.set()
        runtime.close()


def test_actual_file_result_is_durable_and_assignment_is_generated(fixture):
    backend = Backend(scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()],
                               [text_delta("Fact observed."), done()]])
    runtime = runner(fixture, lambda spec: backend, project_instructions="Captured fixture rules")
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        finished(runtime, context)
        state = snapshot(fixture)
        assert state["action_receipts"][0]["state"] == "completed"
        assert state["action_receipts"][0]["request_id"] == state["model_requests"][0]["id"]
        assert "Actual isolated fact" in json.dumps(runtime.poll()["events"])
        assert backend.captured[0]["conversation_history"][0]["input_origin"] == "generated"
        assert "Captured fixture rules" in backend.captured[0]["instructions"]
        assert runtime.inspect(context.attempt_id)["termination_recorded"] and backend.closed
    finally:
        runtime.close()


def test_stop_remains_live_until_uncooperative_provider_is_closed(fixture):
    backend = GatedBackend(events=[text_delta("Late result."), done()])
    runtime = runner(fixture, lambda spec: backend)
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert backend.entered.wait(2)
        started = time.monotonic()
        result = runtime.stop()
        assert time.monotonic() - started < 1
        assert result["state"] == "stopping"
        status = runtime.inspect(context.attempt_id)
        assert status["alive"] and not status["termination_recorded"] and status["state"] == "stopping"
        backend.release.set()
        finished(runtime, context)
        state = snapshot(fixture)
        assert backend.closed and backend.generator_closed
        assert state["attempts"][0]["process_state"] == "stopped"
        assert state["attempts"][0]["state"] == "uncertain"
        assert not state["submissions"]
    finally:
        backend.release.set()
        runtime.close()


def test_pause_allows_admitted_slow_tool_to_settle_then_resume(fixture, monkeypatch):
    import lumi.engine.session as session_module

    entered, release = threading.Event(), threading.Event()
    original = session_module.execute_tool

    def slow_tool(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Fixture tool was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(session_module, "execute_tool", slow_tool)
    backend = Backend(scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()],
                               [text_delta("Verified fixture."), done()]])
    runtime = runner(fixture, lambda spec: backend)
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert entered.wait(2)
        assert runtime.pause()["state"] == "pausing"
        release.set()
        until(lambda: snapshot(fixture)["run"]["state"] == "paused")
        assert snapshot(fixture)["action_receipts"][0]["state"] == "completed"
        assert backend.stream_count == 1
        assert runtime.inspect(context.attempt_id)["alive"]
        runtime.resume()
        finished(runtime, context)
        assert backend.stream_count == 2
        assert runtime.inspect(context.attempt_id)["state"] == "submitted"
    finally:
        release.set()
        runtime.close()


def test_stop_is_not_blocked_by_scope_preflight_and_denies_the_effect(fixture, monkeypatch):
    from lumi.engine.swarming.execution import SwarmExecutionGuard

    entered, release = threading.Event(), threading.Event()
    original = SwarmExecutionGuard._tool_scope
    effects = []

    def slow_scope(guard, name, arguments):
        entered.set()
        assert release.wait(5), "Fixture scope traversal was not released"
        return original(guard, name, arguments)

    monkeypatch.setattr(SwarmExecutionGuard, "_tool_scope", slow_scope)
    monkeypatch.setattr("lumi.engine.session.execute_tool", lambda *args, **kwargs: effects.append(args))
    backend = Backend(events=[tool_call("glob", {"path": ".", "pattern": "*.txt"}), done()])
    runtime = runner(fixture, lambda spec: backend)
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert entered.wait(2)
        started = time.monotonic()
        assert runtime.stop()["state"] == "stopping"
        assert time.monotonic() - started < 1
        assert not snapshot(fixture)["action_receipts"]
        release.set()
        finished(runtime, context)
        assert not effects
        assert runtime.inspect(context.attempt_id)["state"] == "cancelled"
    finally:
        release.set()
        runtime.close()


def test_addressed_peer_message_is_generated_input_with_durable_receipts(fixture, monkeypatch):
    from lumi.engine.swarming.mailbox import SwarmMailbox

    receiver = GatedBackend(scripts=[
        [tool_call("file_read", {"path": "fact.txt"}), done()],
        [text_delta("Incorporated peer finding."), done()],
    ])
    first, second = assign(fixture), assign(fixture, "second")
    sender = Backend(scripts=[[
        tool_call("swarm_send", {"recipient_attempt_id": first.attempt_id, "kind": "finding",
                                "body": "Peer isolated fact", "command_id": "peer-finding"}), done()],
        [text_delta("Reported peer finding."), done()],
    ])
    backends = iter([receiver, sender])
    runtime = runner(fixture, lambda spec: next(backends))
    try:
        runtime.start(first, BackendSpec("ollama", "chosen"))
        assert receiver.entered.wait(2)
        runtime.start(second, BackendSpec("ollama", "chosen"))
        finished(runtime, second)
        assert len(snapshot(fixture)["messages"]) == 1
        worker = runtime._workers[first.attempt_id]
        mailbox = SwarmMailbox(fixture[0].store, first)
        real_steer = worker.session.steer
        monkeypatch.setattr(worker.session, "steer", lambda *args, **kwargs: False)
        runtime._collect_messages(worker, mailbox)
        assert not worker.queued_messages  # Rejected input remains eligible for redelivery.
        monkeypatch.setattr(worker.session, "steer", real_steer)
        runtime._collect_messages(worker, mailbox)
        assert len(worker.queued_messages) == 1
        receiver.release.set()
        finished(runtime, first)
        state = snapshot(fixture)
        receipts = state["receipts"]
        assert {receipt["stage"] for receipt in receipts} == {"runtime", "context"}
        message_id = state["messages"][0]["id"]
        context_receipt = next(receipt for receipt in receipts if receipt["stage"] == "context")
        delivered = [entry for entry in receiver.captured[-1]["conversation_history"]
                     if entry.get("message_id") == message_id]
        assert len(delivered) == 1 and delivered[0]["input_origin"] == "generated"
        assert "Peer isolated fact" in delivered[0]["content"]
        assert context_receipt["model_request_id"] in {item["id"] for item in state["model_requests"]}
        assert runtime.inspect(first.attempt_id)["state"] == "submitted"
    finally:
        receiver.release.set()
        runtime.close()


def test_lease_is_renewed_while_a_provider_blocks(fixture):
    backend = GatedBackend(events=[text_delta("Findings."), done()])
    runtime = runner(fixture, lambda spec: backend)
    runtime._lease_interval = 0.02
    context = assign(fixture)
    before = snapshot(fixture)["run"]["lease_until"]
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert backend.entered.wait(2)
        until(lambda: snapshot(fixture)["run"]["lease_until"] > before)
        assert runtime.inspect(context.attempt_id)["alive"]
        assert not runtime.inspect(context.attempt_id)["termination_recorded"]
    finally:
        backend.release.set()
        runtime.close()


@pytest.mark.parametrize("events,requests", [
    ([error("provider failed")], 3),
    ([text_delta("Incomplete stream")], 3),
    ([text_delta("Need more work"), tool_call("file_read", {"path": "fact.txt"}), done()], 1),
])
def test_errors_missing_done_and_allowance_limit_never_submit(fixture, events, requests):
    backend = Backend(events=events)
    runtime = runner(fixture, lambda spec: backend)
    context = assign(fixture, requests=requests)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        finished(runtime, context)
        assert not snapshot(fixture)["submissions"]
        assert runtime.inspect(context.attempt_id)["state"] in {"failed", "uncertain"}
        assert backend.closed
    finally:
        runtime.close()


def test_cleanup_failure_retains_unconfirmed_termination(fixture):
    class BrokenClose(Backend):
        def close(self):
            raise OSError("fixture close failed")

    runtime = runner(fixture, lambda spec: BrokenClose(events=[text_delta("Findings."), done()]))
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        finished(runtime, context)
        status = runtime.inspect(context.attempt_id)
        assert status["state"] == "reconciliation_required" and not status["termination_recorded"]
        assert snapshot(fixture)["attempts"][0]["process_state"] == "running"
        assert not snapshot(fixture)["submissions"]
        assert not any(event["event"] == "worker.stopped" for event in runtime.poll()["events"])
    finally:
        runtime.close()


def test_foreign_context_and_model_mismatch_rejected_before_factory(fixture):
    calls = []
    runtime = runner(fixture, lambda spec: calls.append(spec))
    context = assign(fixture)
    try:
        with pytest.raises(ScopeDenied):
            runtime.start(replace(context, worker_id="forged"), BackendSpec("ollama", "chosen"))
        with pytest.raises(ScopeDenied):
            runtime.start(context, BackendSpec("ollama", "another-model"))
        assert not calls
    finally:
        runtime.close()


def test_mutable_backend_reuse_rejected_without_closing_other_worker(fixture):
    backend = GatedBackend(events=[text_delta("Findings."), done()])
    runtime = runner(fixture, lambda spec: backend)
    first, second = assign(fixture), assign(fixture, "second")
    try:
        runtime.start(first, BackendSpec("ollama", "chosen"))
        assert backend.entered.wait(2)
        runtime.start(second, BackendSpec("ollama", "chosen"))
        finished(runtime, second)
        assert runtime.inspect(second.attempt_id)["state"] == "failed"
        assert not backend.closed
        assert "reused mutable state" in runtime.inspect(second.attempt_id)["error"]
    finally:
        backend.release.set()
        runtime.close()


def test_viewer_controls_require_current_revision_and_replay_does_not_repause(fixture):
    runtime = runner(fixture, lambda spec: Backend())
    try:
        revision = snapshot(fixture)["run"]["revision"]
        with pytest.raises(RevisionConflict):
            runtime.pause(command_id="stale", expected_revision=revision - 1)
        assert snapshot(fixture)["run"]["state"] == "running"
        with pytest.raises(ValueError):
            runtime.pause(command_id="incomplete")
        first = runtime.pause(command_id="pause", expected_revision=revision)
        assert first["state"] == "paused"
        current = snapshot(fixture)["run"]["revision"]
        runtime.resume(command_id="resume", expected_revision=current)
        replay = runtime.pause(command_id="pause", expected_revision=revision)
        assert replay["revision"] == first["revision"]
        assert replay["state"] == "running"
        with pytest.raises(Conflict):
            runtime.stop(command_id="pause", expected_revision=revision)
    finally:
        runtime.close()


def test_poll_has_bounded_cursor_gap_and_credentials_are_redacted(fixture):
    secret, password, query = "fixture-api-secret", "fixture-url-pass", "fixture-query-secret"

    def broken_factory(spec):
        raise RuntimeError(f"{spec.api_key} https://user:{password}@example.test/api?key={query}")

    runtime = runner(fixture, broken_factory, event_capacity=1)
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen", api_key=secret))
        finished(runtime, context)
        page = runtime.poll()
        assert page["gap"] and len(page["events"]) == 1
        assert not runtime.poll(after=page["cursor"])["events"]
        serialized = json.dumps(page)
        assert all(value not in serialized for value in (secret, password, query))
        assert "[redacted]" in serialized
    finally:
        runtime.close()


def test_handoff_rejection_still_records_closed_worker(fixture):
    runtime = runner(fixture, lambda spec: Backend(events=[text_delta("x" * 66000), done()]))
    context = assign(fixture)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        finished(runtime, context)
        status = runtime.inspect(context.attempt_id)
        assert status["termination_recorded"] and status["state"] == "failed"
        assert not snapshot(fixture)["submissions"]
    finally:
        runtime.close()


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), -1])
def test_close_requires_bounded_timeout(fixture, timeout):
    runtime = runner(fixture, lambda spec: Backend())
    with pytest.raises(ValueError):
        runtime.close(timeout=timeout)
    runtime.close()
