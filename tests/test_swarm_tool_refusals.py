"""A worker's out-of-scope call is refused and reported to its model; the worker continues.

Live models explore: Kimi K3 on NVIDIA NIM opened a narrow assignment with
glob("*"), which searches the project root. Refusing that call keeps the
boundary (nothing runs, no receipt); ending the whole worker did not.
"""

import json
import uuid

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.processes import ProcessObservations
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.tools import SWARM_TOOL_NAMES
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_process_workers import child_script, until

SCRIPTS = [[tool_call("glob", {"pattern": "*"}, "glob:0"), done()],
           [tool_call("file_read", {"path": "fact.txt"}, "file_read:1"), done()],
           [text_delta("Read the assigned fact."), done()]]


def _command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                     authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def narrow(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Actual isolated fact", encoding="utf-8")
    (workspace / "private.txt").write_text("Outside the assignment", encoding="utf-8")
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "runtime" / "state.sqlite"))
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect one fact", request_limit=20,
        policy=PolicyProfile(1, tools, frozenset({"ollama"})), lease_seconds=300)
    _command(supervisor, authority, "plan", {"work_items": [
        {"id": "first", "objective": "Inspect the fact", "read_roots": ["fact.txt"], "write_roots": [],
         "tools": sorted(tools), "criteria": ["fact"]}]})
    result = _command(supervisor, authority, "assign", {"work_item_id": "first", "worker_id": "first",
        "requests": 5, "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(authority.scope, authority.run_id, result["attempt_id"], "first", authority.epoch)
    return supervisor, authority, workspace, context


def _check(supervisor, authority, runner, context):
    until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=30)
    events = runner.poll()["events"]
    results = [event for event in events if event.get("event") == "tool.result"]
    assert results[0]["call_id"] == "glob:0" and results[0]["is_error"]
    assert results[0]["output"].startswith("Refused before running:")
    assert "assignment roots" in results[0]["output"]
    assert results[1]["call_id"] == "file_read:1" and "Actual isolated fact" in results[1]["output"]
    text = json.dumps(events)
    assert "Read the assigned fact." in text and "Outside the assignment" not in text
    assert not any(event.get("event") == "error" for event in events)
    snapshot = supervisor.store.snapshot(authority.scope, authority.run_id)
    # Only the narrowed call has a receipt; the refusal is kept as a run event.
    assert [row["tool_name"] for row in snapshot["action_receipts"]] == ["file_read"]
    refused = [event for event in supervisor.store.events(authority.scope, authority.run_id)
               if event.kind == "tool_refused"]
    assert len(refused) == 1 and refused[0].payload["tool"] == "glob"
    assert [row["state"] for row in snapshot["model_requests"]] == ["completed"] * 3


def test_an_in_process_worker_hears_the_refusal_and_narrows_its_call(narrow):
    supervisor, authority, workspace, context = narrow
    backend = StreamingBackend(name="ollama", model="chosen", scripts=[list(script) for script in SCRIPTS])
    runner = SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda _: backend)
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        _check(supervisor, authority, runner, context)
    finally:
        runner.close()


def test_a_child_worker_receives_the_refusal_across_its_process_boundary(narrow, tmp_path):
    supervisor, authority, workspace, context = narrow
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("glob", {"pattern": "*"}, "glob:0"), done()],
             [tool_call("file_read", {"path": "fact.txt"}, "file_read:1"), done()],
             [text_delta("Read the assigned fact."), done()]])))
''')
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        _check(supervisor, authority, runner, context)
    finally:
        runner.close()
