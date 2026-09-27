"""Real owned child processes, private transport and restart identity evidence."""

import json
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil
import pytest

from lumi.engine.swarming.integration import CheckSpec
from lumi.engine.swarming.process_worker import ManagedWorkerProcess, read_frame
from lumi.engine.swarming.processes import ProcessObservations
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from lumi.processes import background_process_kwargs
from tests.test_swarm_writers import assign, fixture as writer_fixture, git, snapshot
from tests.test_swarm_workers import fixture as reader_fixture, assign as assign_reader, snapshot as reader_snapshot
from tests.test_swarm_coordinator_runner import setup as coordinator_fixture, snapshot as coordinator_snapshot

fixture = writer_fixture
read_setup = reader_fixture
coordinator_setup = coordinator_fixture


def until(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    assert predicate(), "Owned process fixture did not reach expected state"


def child_script(tmp_path, body):
    path = tmp_path / "fixture_child.py"
    path.write_text("import os, sys\nsys.path.insert(0, os.getcwd())\n" + body, encoding="utf-8")
    return [sys.executable, str(path)]


def runtime(fixture, command):
    supervisor, authority, integration, project, _ = fixture
    observations = ProcessObservations(supervisor.store, host_id="fixture-host")
    result = SwarmWorkerRunner(supervisor, authority, project, integration=integration,
        backend_factory=lambda spec: pytest.fail("Writer must construct its backend inside the owned process"),
        process_observations=observations,
        writer_process_factory=lambda: ManagedWorkerProcess(command=command, cancel_grace=.1))
    return result, observations


def test_native_child_writes_finalizes_and_retains_process_identity_without_shared_backend(fixture, tmp_path):
    context, writer = assign(fixture)
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
def factory(spec):
    return StreamingBackend(name=spec.backend_type, model=spec.model, scripts=[
        [tool_call("file_write", {"path":"src/fact.txt", "content":"child wrote fixture\\n"}),
         tool_call("file_edit", {"path":"src/fact.txt", "old_text":"child", "new_text":"managed child"}, "edit"), done()],
        [text_delta("Scoped child work is ready for host verification. fixture-private-key"), done()]])
raise SystemExit(main(backend_factory=factory))
''')
    worker, observations = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen", api_key="fixture-private-key"), writer_id=writer["id"])
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        status = worker.inspect(context.attempt_id)
        assert status["state"] == "submitted", worker.poll()
        assert not status["process_alive"] and status["termination_recorded"]
        state = snapshot(fixture)
        process = state["process_observations"][0]
        assert process["state"] == "stopped" and process["exit_code"] == 0
        assert process["pid"] == status["pid"] and process["host_id"] == "fixture-host"
        assert observations.inspect(context.scope, context.run_id, context.attempt_id)["observation"] == "stopped"
        assert state["writer_worktrees"][0]["state"] == "ready"
        assert state["submissions"][0]["candidate_revision"] == state["writer_worktrees"][0]["result_revision"]
        assert (Path(writer["path"]) / "src/fact.txt").read_text() == "managed child wrote fixture\n"
        assert (fixture[3] / "src/fact.txt").read_text() == "base fact\n"
        assert git(fixture[3], "rev-parse", "HEAD") == fixture[4]
        assert "fixture-private-key" not in json.dumps(worker.poll())
        assert "fixture-private-key" not in json.dumps(state)
        assert state["submissions"][0]["handoff"].startswith("[Public report redacted")
        assert len(state["action_receipts"]) == 2
        check = CheckSpec("fixture-check", (sys.executable, "-c",
            "from pathlib import Path; assert Path('src/fact.txt').read_text() == 'managed child wrote fixture\\n'"), 10)
        candidate = fixture[2].prepare_candidate(fixture[1], writer_ids=(writer["id"],), required_checks=(check,),
            criterion_checks={"writer": {"fixture-check": "fixture-check"}})
        assert fixture[2].run_check(fixture[1], candidate["id"], "fixture-check")["state"] == "passed"
    finally:
        worker.close()


def test_stop_kills_uncooperative_child_and_grandchild_without_refunding_uncertain_request(fixture, tmp_path):
    context, writer = assign(fixture)
    descendant_file = tmp_path / "descendant.txt"
    command = child_script(tmp_path, f'''
import subprocess, time
from pathlib import Path
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, text_delta
class Blocked(StreamingBackend):
    def stream(self, **kwargs):
        descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path({str(descendant_file)!r}).write_text(str(descendant.pid))
        yield text_delta("Owned provider is blocked")
        while True: time.sleep(.1)
raise SystemExit(main(backend_factory=lambda spec: Blocked(name=spec.backend_type, model=spec.model)))
''')
    worker, observations = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        until(descendant_file.exists)
        descendant = int(descendant_file.read_text())
        assert psutil.pid_exists(descendant)
        process = snapshot(fixture)["process_observations"][0]
        assert observations.inspect(context.scope, context.run_id, context.attempt_id)["observation"] == "running"
        assert "api_key" not in " ".join(psutil.Process(process["pid"]).cmdline())
        started = time.monotonic()
        assert worker.stop()["state"] == "stopping"
        assert time.monotonic() - started < 1
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        until(lambda: not psutil.pid_exists(descendant) or psutil.Process(descendant).status() == psutil.STATUS_ZOMBIE)
        state = snapshot(fixture)
        assert state["process_observations"][0]["state"] == "stopped"
        assert state["attempts"][0]["process_state"] == "stopped"
        assert state["attempts"][0]["state"] == "uncertain"
        assert state["model_requests"][0]["state"] == "uncertain"
        assert state["model_requests"][0]["used"] is None
        assert state["reservations"][0]["state"] == "uncertain", worker.poll()
        assert not state["submissions"]
    finally:
        worker.close()


@pytest.mark.parametrize("malformation", ["unknown-operation", "oversized-frame", "early-eof", "forged-completion"])
def test_malformed_child_protocol_cannot_invoke_arbitrary_host_methods(fixture, tmp_path, malformation):
    context, writer = assign(fixture)
    payload = {
        "unknown-operation": 'print(json.dumps({"version":1,"kind":"rpc","id":1,"operation":"supervisor.handle","args":[],"kwargs":{}}),flush=True)',
        "oversized-frame": 'sys.stdout.write("x" * (4*1024*1024+1));sys.stdout.flush()',
        "early-eof": 'os.close(1)',
        "forged-completion": 'print(json.dumps({"version":1,"kind":"event","event":{"event":"text.done","text":"claimed success"}}),flush=True);'
            'print(json.dumps({"version":1,"kind":"event","event":{"event":"session.end"}}),flush=True);'
            'print(json.dumps({"version":1,"kind":"closed"}),flush=True);sys.exit(0)',
    }[malformation]
    command = child_script(tmp_path, f'''
import json, time
print(json.dumps({{"version":1,"kind":"ready"}}),flush=True)
sys.stdin.readline()
{payload}
time.sleep(30)
''')
    worker, _ = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        until(lambda: not worker.inspect(context.attempt_id)["alive"], timeout=10)
        state = snapshot(fixture)
        assert state["process_observations"][0]["state"] == "stopped"
        assert state["attempts"][0]["state"] == "failed"
        assert not state["model_requests"] and not state["action_receipts"] and not state["submissions"]
    finally:
        worker.close()


def test_foreign_host_cannot_claim_process_reconciliation(fixture, tmp_path):
    context, writer = assign(fixture)
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    events=[text_delta("Read fixture."), done()])))
''')
    worker, observations = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        foreign = ProcessObservations(fixture[0].store, host_id="foreign-host")
        assert foreign.inspect(context.scope, context.run_id, context.attempt_id)["observation"] == "unknown"
        with pytest.raises(Exception, match="host"):
            foreign.reconcile(fixture[1], context.attempt_id)
        assert observations.reconcile(fixture[1], context.attempt_id)["observation"] == "stopped"
    finally:
        worker.close()


def test_pause_observes_already_admitted_child_write_before_next_request(fixture, tmp_path):
    context, writer = assign(fixture)
    entered, release = tmp_path / "entered", tmp_path / "release"
    command = child_script(tmp_path, f'''
import time
from pathlib import Path
import lumi.engine.session as session_module
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
original = session_module.execute_tool
def slow_tool(*args, **kwargs):
    Path({str(entered)!r}).write_text("admitted")
    while not Path({str(release)!r}).exists(): time.sleep(.01)
    return original(*args, **kwargs)
session_module.execute_tool = slow_tool
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("file_write", {{"path":"src/fact.txt", "content":"paused observation"}}), done()],
             [text_delta("Fixture change ready."), done()]])))
''')
    worker, _ = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        until(entered.exists)
        assert snapshot(fixture)["action_receipts"][0]["state"] == "admitted"
        assert worker.pause()["state"] == "pausing"
        release.write_text("release")
        until(lambda: snapshot(fixture)["action_receipts"][0]["state"] == "completed")
        until(lambda: snapshot(fixture)["run"]["state"] == "paused")
        state = snapshot(fixture)
        assert state["run"]["state"] == "paused"
        assert len(state["model_requests"]) == 1
        assert worker.inspect(context.attempt_id)["alive"]
        worker.resume()
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        assert worker.inspect(context.attempt_id)["state"] == "submitted", worker.poll()
    finally:
        release.write_text("release")
        worker.close()


def test_default_source_entrypoint_handshakes_without_startup_hook_or_provider(tmp_path, monkeypatch):
    marker = tmp_path / "startup-ran"
    (tmp_path / "sitecustomize.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    process = ManagedWorkerProcess()
    events = list(process.run({}, rpc={}, cancel_event=threading.Event(), pause_event=threading.Event()))
    assert process.cleanup_confirmed and process.exit_code == 0
    assert events == [{"event": "error", "message": "Native worker failed (ValueError)"}]
    assert not marker.exists()


def test_transport_full_queue_does_not_leave_shutdown_thread_waiting():
    process = ManagedWorkerProcess()
    for _ in range(process._incoming.maxsize):
        process._incoming.put_nowait(("frame", {}))
    thread = threading.Thread(target=process._receive, args=(("frame", {}),), daemon=True)
    thread.start()
    process.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows named job crash cleanup contract")
def test_parent_crash_kills_named_job_and_restart_observer_proves_tree_stopped(fixture, tmp_path):
    context, _ = assign(fixture)
    identity, release, descendant_file = (tmp_path / name for name in ("identity.json", "admit", "descendant.json"))
    child = child_script(tmp_path, f'''
import json, subprocess, time
from pathlib import Path
print(json.dumps({{"version":1,"kind":"ready"}}), flush=True)
sys.stdin.readline()
descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(descendant_file)!r}).write_text(json.dumps(descendant.pid))
while True: time.sleep(.1)
''')
    helper = tmp_path / "fixture_host.py"
    helper.write_text(f'''
import json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
def started(process):
    path = Path({str(identity)!r})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({{"pid":process.pid,"created_at":process.created_at,"launch_token":process.launch_token}}))
    temporary.replace(path)
    while not Path({str(release)!r}).exists(): time.sleep(.01)
process = ManagedWorkerProcess(command={child!r})
for event in process.run({{}}, rpc={{}}, cancel_event=threading.Event(), pause_event=threading.Event(), on_started=started):
    pass
''', encoding="utf-8")
    host = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **background_process_kwargs(new_process_group=True))
    observations = ProcessObservations(fixture[0].store, host_id="fixture-host")
    try:
        until(identity.exists)
        captured = json.loads(identity.read_text())
        observations.started(fixture[1], context, **captured)
        release.write_text("durable identity committed")
        until(lambda: descendant_file.exists() and descendant_file.read_text())
        descendant = json.loads(descendant_file.read_text())
        assert observations.inspect(context.scope, context.run_id, context.attempt_id)["observation"] == "running"
        host.kill()
        host.wait(timeout=5)
        until(lambda: not psutil.pid_exists(captured["pid"]) and not psutil.pid_exists(descendant))
        restarted = ProcessObservations(fixture[0].store, host_id="fixture-host")
        assert restarted.reconcile(fixture[1], context.attempt_id)["observation"] == "stopped"
        assert snapshot(fixture)["process_observations"][0]["state"] == "stopped"
    finally:
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)


def test_cleanup_observation_failure_keeps_process_termination_unconfirmed(fixture, tmp_path):
    context, writer = assign(fixture)
    command = child_script(tmp_path, '''
import json
print(json.dumps({"version":1,"kind":"ready"}),flush=True)
sys.stdin.readline()
print(json.dumps({"version":1,"kind":"closed"}),flush=True)
''')
    worker, _ = runtime(fixture, command)
    class Unconfirmed(ManagedWorkerProcess):
        def _tree_empty(self):
            raise PermissionError("Fixture ownership observation denied")
    worker._writer_process_factory = lambda: Unconfirmed(command=command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        status = worker.inspect(context.attempt_id)
        assert status["state"] == "reconciliation_required" and not status["termination_recorded"]
        state = snapshot(fixture)
        assert state["process_observations"][0]["state"] == "started"
        assert state["attempts"][0]["process_state"] == "running"
        assert not state["submissions"]
    finally:
        worker.close()


@pytest.mark.parametrize("observation", ["usage", "tool-output", "tool-metadata"])
def test_captured_credential_in_observation_is_withheld_and_uncertain(fixture, tmp_path, observation):
    context, writer = assign(fixture)
    script = ('[text_delta("Finished"), done(stats={"diagnostic":"fixture-private-key"})]'
              if observation == "usage" else '[tool_call("file_write", {"path":"src/fact.txt", "content":"known effect"}), done()]')
    injection = ""
    if observation != "usage":
        modification = ('result.output += " fixture-private-key"' if observation == "tool-output"
                        else 'result.metadata["diagnostic"] = "fixture-private-key"')
        injection = f'''import lumi.engine.session as session_module
original = session_module.execute_tool
def observed(*args, **kwargs):
    result = original(*args, **kwargs)
    {modification}
    return result
session_module.execute_tool = observed
'''
    command = child_script(tmp_path, f'''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
{injection}
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    events={script})))
''')
    worker, _ = runtime(fixture, command)
    try:
        worker.start(context, BackendSpec("ollama", "chosen", api_key="fixture-private-key"), writer_id=writer["id"])
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        state = snapshot(fixture)
        assert not state["submissions"]
        assert state["attempts"][0]["state"] == "uncertain", worker.poll()
        if observation == "usage":
            assert state["model_requests"][0]["state"] == "uncertain"
            assert state["reservations"][0]["state"] == "uncertain"
        else:
            # The request cost is known; the already performed write still
            # requires reconciliation because its result cannot be retained.
            assert state["reservations"][0]["state"] == "settled"
            assert state["reservations"][0]["used"] == 1
            assert state["action_receipts"][0]["state"] == "uncertain"
            assert state["action_receipts"][0]["output_artifact_id"] is None
            assert (Path(writer["path"]) / "src/fact.txt").read_text() == "known effect"
        assert "fixture-private-key" not in json.dumps(state)
        assert "fixture-private-key" not in json.dumps(worker.poll())
    finally:
        worker.close()


def test_managed_reader_uses_owned_child_and_durable_primary_result(read_setup, tmp_path):
    context = assign_reader(read_setup)
    supervisor, authority, workspace = read_setup
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("grep", {"path":".", "pattern":"Actual"}), done()],
             [text_delta("Inspected fixture content in owned child."), done()]])))
''')
    worker = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("Managed reader must not call the host factory"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        worker.start(context, BackendSpec("ollama", "chosen"))
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        state = reader_snapshot(read_setup)
        assert state["attempts"][0]["state"] == "submitted", worker.poll()
        assert state["process_observations"][0]["state"] == "stopped"
        assert state["action_receipts"][0]["state"] == "completed"
        assert state["action_receipts"][0]["is_error"] == 0
        assert state["action_receipts"][0]["request_id"] == state["model_requests"][0]["id"]
        assert not state["writer_worktrees"]
    finally:
        worker.close()


def test_managed_coordinator_records_parent_attested_request_after_child_exit(coordinator_setup, tmp_path):
    supervisor, authority, context, workspace, plans = coordinator_setup
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
from tests.test_swarm_coordinator import response
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("file_read", {"path":"evidence.txt"}), done()],
             [text_delta(response(summary="Managed planner reviewed fixture")), done()]])))
''')
    worker = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("Managed coordinator must not call the host factory"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        worker.start_coordinator(context, BackendSpec("ollama", "chosen"), plans)
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        state = coordinator_snapshot(coordinator_setup)
        assert state["attempts"][0]["state"] == "completed", worker.poll()
        assert state["process_observations"][0]["state"] == "stopped"
        assert len(state["coordinator_inputs"]) == 2
        assert state["coordinator_proposals"][0]["request_id"] == state["model_requests"][-1]["id"]
        assert not state["work_items"] and not state["submissions"]
    finally:
        worker.close()


def test_thread_participant_cannot_inherit_another_app_host_identity(read_setup):
    from lumi.engine.swarming.models import Conflict
    from lumi.engine.swarming.recovery import record_run_host
    supervisor, authority, workspace = read_setup
    observations = ProcessObservations(supervisor.store, host_id="fixture-host")
    record_run_host(supervisor.store, authority, process_observations=observations)
    with supervisor.store._connection(write=True) as connection:
        connection.execute("UPDATE run_hosts SET pid=pid+1 WHERE run_id=?", (authority.run_id,))
    context = assign_reader(read_setup)
    worker = SwarmWorkerRunner(supervisor, authority, workspace, process_observations=observations,
        backend_factory=lambda _: pytest.fail("Foreign host identity must fail before backend construction"))
    try:
        with pytest.raises(Conflict, match="different captured app"):
            worker.start(context, BackendSpec("ollama", "chosen"))
        assert not reader_snapshot(read_setup)["model_requests"]
    finally:
        worker.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_transport_rejects_nonfinite_values_before_public_event_forwarding(value):
    with pytest.raises(ValueError, match="finite"):
        read_frame(io.BytesIO(('{"version":1,"kind":"event","value":' + value + '}\n').encode()))


def test_managed_search_child_stays_owned_through_cancellation(read_setup, tmp_path):
    context = assign_reader(read_setup)
    supervisor, authority, workspace = read_setup
    marker = tmp_path / "search.json"
    search_script = tmp_path / "fixture_search.py"
    search_script.write_text("import json,os,time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'group':os.getpgrp() if os.name!='nt' else None}}))\n"
        "time.sleep(60)\n", encoding="utf-8")
    command = child_script(tmp_path, f'''
import lumi.engine.tools as tools
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, done
tools._build_grep_command = lambda *args, **kwargs: [sys.executable, {str(search_script)!r}]
original = tools._run_subprocess_with_cancel
def owned(*args, **kwargs):
    assert kwargs.get("owned_process_group") is True
    return original(*args, **kwargs)
tools._run_subprocess_with_cancel = owned
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    events=[tool_call("grep", {{"path":".", "pattern":"Actual"}}), done()])))
''')
    worker = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("Managed search must use its child"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command, cancel_grace=.2),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        worker.start(context, BackendSpec("ollama", "chosen"))
        try:
            until(lambda: (marker.exists() and marker.read_text()) or not worker.inspect(context.attempt_id)["alive"])
        except AssertionError:
            process = psutil.Process(worker.inspect(context.attempt_id)["pid"])
            children = [child.cmdline() for child in process.children(recursive=True)]
            raise AssertionError(json.dumps({"status": worker.poll(), "children": children,
                "actions": reader_snapshot(read_setup)["action_receipts"]})) from None
        assert marker.exists(), worker.poll()
        search = json.loads(marker.read_text())
        if os.name != "nt":
            assert search["group"] == worker.inspect(context.attempt_id)["pid"]
        worker.stop()
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        until(lambda: not psutil.pid_exists(search["pid"]) or psutil.Process(search["pid"]).status() == psutil.STATUS_ZOMBIE)
        state = reader_snapshot(read_setup)
        assert state["process_observations"][0]["state"] == "stopped"
        assert len(state["action_receipts"]) == 1
        assert not state["submissions"]
    finally:
        worker.close()


def test_managed_search_refuses_path_resolution_without_bundled_binary(tmp_path, monkeypatch):
    import lumi.engine.tools as tools
    monkeypatch.setattr(tools, "_VENDORED_RIPGREP_DIR", tmp_path / "missing")
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(tools.shutil, "which", lambda _: pytest.fail("Managed search consulted PATH"))
    tools._ripgrep_executable.cache_clear()
    try:
        with pytest.raises(FileNotFoundError, match="PATH executables"):
            tools._build_grep_command("pattern", str(tmp_path), "", trusted_only=True)
    finally:
        tools._ripgrep_executable.cache_clear()
