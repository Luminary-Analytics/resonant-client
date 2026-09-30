"""Individual controls preserve peer execution and exact generated guidance."""

import json
from pathlib import Path
import threading
import uuid

import pytest

from lumi.engine.swarming.execution import SwarmExecutionGuard
from lumi.engine.swarming.models import RevisionConflict
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.processes import ProcessObservations
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import done, text_delta, tool_call
from tests.test_swarm_workers import (fixture as reader_fixture, Backend, GatedBackend, assign,
                                     runner, finished, snapshot, until)
from tests.test_swarm_writers import (fixture as writer_fixture, assign as assign_writer,
                                     runner as writer_runner, finished as writer_finished, snapshot as writer_snapshot)
from tests.test_swarm_process_workers import child_script
from tests.test_swarm_coordinator_runner import setup as coordinator_fixture, start as start_coordinator
from tests.test_swarm_coordinator import response

reader = reader_fixture
writer = writer_fixture
coordinator = coordinator_fixture


# How long a participant may take to reach a fixture's gate: starting its
# thread and committing its request run SQLite writes that take seconds on a
# loaded runner. The gates themselves hold until the test releases them.
REACH_SECONDS = 20
HOLD_SECONDS = 60


def control(runtime, context, action, **kwargs):
    """One owner control at the revision just read.

    The team's own work (its workers' request accounting) also advances the
    revision, so on a busy runner a change can land in between; the control is
    then refused before anything commits, and the owner refreshes and sends it
    again.
    """
    command_id = uuid.uuid4().hex
    for attempt in range(3):
        revision = runtime.store.snapshot(context.scope, context.run_id)["run"]["revision"]
        try:
            return getattr(runtime, action)(context.attempt_id, context.epoch,
                command_id=command_id, expected_revision=revision, **kwargs)
        except RevisionConflict:
            if attempt == 2:
                raise


def test_individual_pause_preserves_peer_progress_and_global_pause_hierarchy(reader):
    blocked = GatedBackend(scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()],
                                    [text_delta("Paused participant resumed."), done()]])
    providers = iter((blocked, Backend(events=[text_delta("Peer completed."), done()])))
    runtime = runner(reader, lambda spec: next(providers))
    first, peer = assign(reader), assign(reader, "second")
    try:
        runtime.start(first, BackendSpec("ollama", "chosen"))
        assert blocked.entered.wait(REACH_SECONDS)
        paused = control(runtime, first, "pause_worker")
        assert paused["worker"]["state"] == "pausing" and paused["worker"]["alive"]
        runtime.start(peer, BackendSpec("ollama", "chosen"))
        finished(runtime, peer)
        assert runtime.inspect(peer.attempt_id)["state"] == "submitted"
        blocked.release.set()
        until(lambda: runtime.inspect(first.attempt_id)["state"] == "paused")
        assert not snapshot(reader)["action_receipts"]
        runtime.pause()
        control(runtime, first, "resume_worker")
        assert not runtime.inspect(first.attempt_id)["pause_requested"]
        assert runtime._workers[first.attempt_id].pause.is_set()
        runtime.resume()
        finished(runtime, first)
        assert runtime.inspect(first.attempt_id)["state"] == "submitted"
        assert len(snapshot(reader)["action_receipts"]) == 1
    finally:
        blocked.release.set()
        runtime.close()


def test_cancel_one_blocked_participant_does_not_cancel_peer(reader):
    blocked = GatedBackend(events=[text_delta("Too late to submit."), done()])
    providers = iter((blocked, Backend(events=[text_delta("Peer remains live."), done()])))
    runtime = runner(reader, lambda spec: next(providers))
    first, peer = assign(reader), assign(reader, "second")
    try:
        runtime.start(first, BackendSpec("ollama", "chosen"))
        assert blocked.entered.wait(REACH_SECONDS)
        control(runtime, first, "cancel_worker")
        assert runtime.inspect(first.attempt_id)["state"] == "stopping"
        assert not runtime.inspect(first.attempt_id)["termination_recorded"]
        runtime.start(peer, BackendSpec("ollama", "chosen"))
        finished(runtime, peer)
        blocked.release.set()
        finished(runtime, first)
        state = snapshot(reader)
        assert state["run"]["state"] == "running"
        assert {item["attempt_id"] for item in state["submissions"]} == {peer.attempt_id}
        assert runtime.inspect(first.attempt_id)["termination_recorded"]
    finally:
        blocked.release.set()
        runtime.close()


def test_guidance_replay_is_generated_exactly_once_and_bound_to_next_request(reader):
    backend = GatedBackend(scripts=[[text_delta("First response."), done()],
                                   [text_delta("Response after owner guidance."), done()]])
    runtime = runner(reader, lambda spec: backend)
    context = assign(reader)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen", api_key="fixture-private-key"))
        assert backend.entered.wait(REACH_SECONDS)
        for attempt in range(3):  # the team's own work may refuse it first, before anything commits
            revision = snapshot(reader)["run"]["revision"]
            envelope = dict(command_id="steer-once", expected_revision=revision, text="Inspect the omitted edge case.")
            try:
                runtime.steer_worker(context.attempt_id, context.epoch, **envelope)
                break
            except RevisionConflict:
                if attempt == 2:
                    raise
        runtime.steer_worker(context.attempt_id, context.epoch, **envelope)  # the exact replay
        reopened = type(runtime.store)(runtime.store.path).snapshot(context.scope, context.run_id)
        assert len(reopened["owner_directives"]) == 1 and not reopened["owner_directive_receipts"]
        with pytest.raises(ValueError, match="credential"):
            control(runtime, context, "steer_worker", text="fixture-private-key")
        with pytest.raises(RevisionConflict):
            runtime.cancel_worker(context.attempt_id, context.epoch, command_id="stale", expected_revision=revision)
        assert not runtime.inspect(context.attempt_id)["cancel_requested"]
        backend.release.set()
        finished(runtime, context)
        state = snapshot(reader)
        assert runtime.inspect(context.attempt_id)["state"] == "submitted", runtime.poll()
        assert len(backend.captured) == 2 and len(state["owner_directive_receipts"]) == 1
        receipt = state["owner_directive_receipts"][0]
        assert receipt["request_id"] == state["model_requests"][1]["id"]
        assert receipt["input_sha256"] == state["request_inputs"][1]["input_sha256"]
        messages = [entry for entry in backend.captured[1]["conversation_history"] if "Owner guidance" in str(entry)]
        assert len(messages) == 1 and messages[0]["input_origin"] == "generated" and "message_id" not in messages[0]
        assert not state["messages"] and not state["receipts"]
    finally:
        backend.release.set()
        runtime.close()


@pytest.mark.parametrize("action", ["pause_worker", "cancel_worker"])
def test_control_between_reservation_and_input_keeps_one_exact_request(reader, monkeypatch, action):
    reached, release = threading.Event(), threading.Event()
    original = SwarmExecutionGuard._command
    def blocked(self, kind, payload):
        result = original(self, kind, payload)
        if kind == "reserve_request":
            reached.set()
            assert release.wait(HOLD_SECONDS)
        return result
    monkeypatch.setattr(SwarmExecutionGuard, "_command", blocked)
    backend = Backend(events=[text_delta("Observed response."), done()])
    runtime = runner(reader, lambda spec: backend)
    context = assign(reader)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert reached.wait(REACH_SECONDS)
        control(runtime, context, action)
        release.set()
        if action == "pause_worker":
            until(lambda: runtime.inspect(context.attempt_id)["state"] == "paused")
            state = snapshot(reader)
            assert len(state["model_requests"]) == 1 and state["model_requests"][0]["state"] == "reserved"
            assert not backend.captured
            control(runtime, context, "resume_worker")
        finished(runtime, context)
        requests = snapshot(reader)["model_requests"]
        assert len(requests) == 1
        assert requests[0]["state"] == ("completed" if action == "pause_worker" else "not_started")
        assert requests[0]["used"] == (1 if action == "pause_worker" else 0)
    finally:
        release.set()
        runtime.close()


def test_cancel_after_write_admission_retains_actual_observation(writer, monkeypatch):
    import lumi.engine.session as session_module
    entered, release = threading.Event(), threading.Event()
    original = session_module.execute_tool
    def slow(name, *args, **kwargs):
        if name == "file_write":
            entered.set()
            assert release.wait(HOLD_SECONDS)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(session_module, "execute_tool", slow)
    context, lease = assign_writer(writer)
    backend = Backend(events=[tool_call("file_write", {"path": "src/fact.txt", "content": "Observed admitted write"}), done()])
    runtime = writer_runner(writer, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=lease["id"])
        assert entered.wait(REACH_SECONDS)
        assert writer_snapshot(writer)["action_receipts"][0]["state"] == "admitted"
        control(runtime, context, "cancel_worker")
        release.set()
        writer_finished(runtime, context)
        state = writer_snapshot(writer)
        assert state["action_receipts"][0]["state"] == "completed"
        assert (Path(lease["path"]) / "src/fact.txt").read_text() == "Observed admitted write"
        assert state["attempts"][0]["state"] == "cancelled" and not state["submissions"]
    finally:
        release.set()
        runtime.close()


def test_pause_during_slow_scope_preflight_waits_without_closing_guard(reader, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = SwarmExecutionGuard._tool_scope
    first = True
    def scope(self, name, arguments):
        nonlocal first
        if first:
            first = False
            reached.set()
            assert release.wait(HOLD_SECONDS)
        return original(self, name, arguments)
    monkeypatch.setattr(SwarmExecutionGuard, "_tool_scope", scope)
    backend = Backend(scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()],
                               [text_delta("Scope check resumed."), done()]])
    runtime = runner(reader, lambda spec: backend)
    context = assign(reader)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        assert reached.wait(REACH_SECONDS)
        control(runtime, context, "pause_worker")
        release.set()
        until(lambda: runtime.inspect(context.attempt_id)["state"] == "paused")
        assert not snapshot(reader)["action_receipts"]
        assert not runtime._workers[context.attempt_id].session._execution_boundary.guard.guard.closed
        control(runtime, context, "resume_worker")
        finished(runtime, context)
        assert runtime.inspect(context.attempt_id)["state"] == "submitted"
        assert len(snapshot(reader)["action_receipts"]) == 1
    finally:
        release.set()
        runtime.close()


def test_coordinator_guidance_composes_original_prompt_attestation(coordinator):
    backend = GatedBackend(scripts=[[tool_call("file_read", {"path": "evidence.txt"}), done()],
                                   [text_delta(response()), done()]])
    runtime = start_coordinator(coordinator, backend)
    context = coordinator[2]
    try:
        assert backend.entered.wait(REACH_SECONDS)
        control(runtime, context, "steer_worker", text="Keep the plan restricted to the original questions.")
        backend.release.set()
        until(lambda: not runtime.inspect(context.attempt_id)["alive"])
        state = runtime.store.snapshot(context.scope, context.run_id)
        assert state["attempts"][0]["state"] == "completed", runtime.poll()
        receipt = state["owner_directive_receipts"][0]
        assert receipt["request_id"] == state["coordinator_inputs"][-1]["request_id"]
        assert state["coordinator_proposals"][0]["request_id"] == receipt["request_id"]
    finally:
        backend.release.set()
        runtime.close()


def test_managed_child_queues_owner_guidance_while_paused_without_peer_identity(reader, tmp_path):
    entered, release, captured = (tmp_path / name for name in ("entered", "release", "captured.json"))
    child = child_script(tmp_path, f'''
import json,time
from pathlib import Path
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend,text_delta,done
class Backend(StreamingBackend):
    def stream(self, **kwargs):
        if not self.stream_calls:
            Path({str(entered)!r}).write_text('ready')
            while not Path({str(release)!r}).exists(): time.sleep(.02)
        else:
            Path({str(captured)!r}).write_text(json.dumps(kwargs['conversation_history']))
        yield from super().stream(**kwargs)
raise SystemExit(main(backend_factory=lambda spec: Backend(name=spec.backend_type,model=spec.model,
    scripts=[[text_delta('First response'),done()],[text_delta('Guided response'),done()]])))
''')
    runtime = SwarmWorkerRunner(*reader[:2], reader[2], backend_factory=lambda _: pytest.fail("Unexpected host provider"),
        managed_readers=True, writer_process_factory=lambda: ManagedWorkerProcess(command=child),
        process_observations=ProcessObservations(reader[0].store, host_id="fixture-host"))
    context = assign(reader)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        until(entered.exists)
        control(runtime, context, "pause_worker")
        control(runtime, context, "steer_worker", text="Use the original scope for this extra review.")
        release.write_text("continue")
        until(lambda: runtime.inspect(context.attempt_id)["state"] == "paused")
        assert not snapshot(reader)["owner_directive_receipts"]
        control(runtime, context, "resume_worker")
        finished(runtime, context)
        state = snapshot(reader)
        assert runtime.inspect(context.attempt_id)["state"] == "submitted", runtime.poll()
        assert len(state["owner_directive_receipts"]) == 1 and captured.exists()
        guidance = [row for row in json.loads(captured.read_text()) if "Owner guidance" in str(row)]
        assert len(guidance) == 1 and "message_id" not in guidance[0]
        assert state["process_observations"][0]["state"] == "stopped"
    finally:
        release.write_text("continue")
        runtime.close()


def test_individual_cancel_owns_only_target_managed_process(reader, tmp_path):
    entered = tmp_path / "entered"
    child = child_script(tmp_path, f'''
import time
from pathlib import Path
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend,text_delta,done
class Backend(StreamingBackend):
    def stream(self, **kwargs):
        if 'first' in str(kwargs['conversation_history']):
            Path({str(entered)!r}).write_text('ready')
            while True: time.sleep(.02)
        yield from super().stream(**kwargs)
raise SystemExit(main(backend_factory=lambda spec: Backend(name=spec.backend_type,model=spec.model,
    events=[text_delta('Independent peer completed'),done()])))
''')
    runtime = SwarmWorkerRunner(*reader[:2], reader[2], backend_factory=lambda _: pytest.fail("Unexpected host provider"),
        managed_readers=True, writer_process_factory=lambda: ManagedWorkerProcess(command=child, cancel_grace=.1),
        process_observations=ProcessObservations(reader[0].store, host_id="fixture-host"))
    first, peer = assign(reader), assign(reader, "second")
    try:
        runtime.start(first, BackendSpec("ollama", "chosen"))
        until(entered.exists)
        runtime.start(peer, BackendSpec("ollama", "chosen"))
        control(runtime, first, "cancel_worker")
        finished(runtime, first)
        finished(runtime, peer)
        state = snapshot(reader)
        assert state["run"]["state"] == "running"
        assert {row["attempt_id"] for row in state["submissions"]} == {peer.attempt_id}
        assert all(row["state"] == "stopped" for row in state["process_observations"])
        first_request = next(row for row in state["model_requests"] if row["attempt_id"] == first.attempt_id)
        assert first_request["state"] == "uncertain" and first_request["used"] is None
        assert not runtime.inspect(first.attempt_id)["process_alive"]
    finally:
        runtime.close()
