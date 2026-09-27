"""Owner-triggered planning consumes partial native results without stopping peers."""

import asyncio
import copy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lumi.engine.swarming.models import (AdmissionClosed, AllowanceExceeded, Conflict, IdempotencyConflict,
                                         RevisionConflict)
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.gui import swarming
from tests.streaming_stub import StreamingBackend, done, text_delta
from tests.test_gui_swarming import setup as desktop_fixture, enable
from tests.test_swarm_process_workers import child_script
from tests.test_swarm_workers import until


setup = desktop_fixture
FINDING = "partial-finding-42: independent evidence is available"


def plan(*, follow_up=False):
    def item(label, objective):
        return {"id": label, "objective": objective, "role": "explore", "dependencies": [],
                "read_roots": ["."], "write_roots": [], "criteria": ["owner_review"]}
    return {"summary": "Use the partial evidence" if follow_up else "Two independent investigations", "use_team": True,
            "work_items": [item("inspect", "Follow-up investigation from the partial finding")] if follow_up else
            [item("inspect", "Fast independent investigation"), item("slow", "Hold independent peer")]}


def planning_data(inputs):
    for entry in inputs.get("conversation_history", []):
        value = entry.get("content", "")
        if entry.get("role") == "user" and entry.get("input_origin") == "generated" and "Captured planning data:\n" in value:
            return json.loads(value.split("Captured planning data:\n", 1)[1].split("\n</runtime_message>", 1)[0])
    return None


@pytest.fixture
def team(setup):
    service, capture, _ = setup
    entered, release = threading.Event(), threading.Event()
    instances = []
    class Backend(StreamingBackend):
        def __init__(self, spec):
            super().__init__(name=spec.backend_type, model=spec.model)
            self.captured = []
        def stream(self, **kwargs):
            self.captured.append(copy.deepcopy({key: value for key, value in kwargs.items() if key != "cancel_event"}))
            data = planning_data(kwargs)
            if data is not None:
                output = json.dumps(plan(follow_up=bool(data["existing_work"])))
            elif "Hold independent peer" in str(kwargs):
                entered.set()
                # Held while the test plans a follow-up around it: a generous
                # bound, since each step waits on workers on a loaded runner.
                deadline = time.monotonic() + 180
                while not release.wait(.01):
                    if kwargs.get("cancel_event") and kwargs["cancel_event"].is_set():
                        return
                    assert time.monotonic() < deadline, "held fixture peer exceeded its deadline"
                output = "Late independent result"
            else:
                output = FINDING
            yield text_delta(output)
            yield done(model=self.model)
    def factory(spec):
        backend = Backend(spec)
        instances.append(backend)
        return backend
    service._factory = factory
    enable(service, capture)
    started = service.operate(capture, {"action": "start", "request_id": "initial", "objective": "Coordinate independent findings",
        "plan_mode": "coordinator", "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3})
    f = SimpleNamespace(service=service, capture=capture, run_id=started["run"]["run"]["id"],
                        entered=entered, release=release, backends=instances)
    f.runner = service._runners[f.run_id][1]
    until(lambda: len(view(f)["run"]["coordinator_proposals"]) == 1 and not f.runner.inspect_all()[0]["alive"])
    try:
        yield f
    finally:
        release.set()


def view(f):
    return f.service.operate(f.capture, {"action": "view", "request_id": "view", "run_id": f.run_id})


def command(f, action, **payload):
    return {"action": action, "request_id": uuid4().hex, "run_id": f.run_id,
            "expected_revision": view(f)["run"]["run"]["revision"], **payload}


def decide(f, proposal=None):
    proposal = proposal or view(f)["run"]["coordinator_proposals"][-1]
    return f.service.operate(f.capture, command(f, "decide_proposal", proposal_id=proposal["id"],
        sha256=proposal["sha256"], accept=True, evidence="Owner reviewed this exact scoped proposal"))


def peers(f):
    """The run's attempts and the runner's workers, for a wait that timed out."""
    return {"attempts": [(row["kind"], row["state"]) for row in view(f)["run"]["attempts"]],
            "workers": [(row["state"], row["error"]) for row in f.runner.inspect_all()]}


def start_peers(f):
    decide(f)
    # Two workers start after the owner's decision; allow for a loaded runner.
    assert f.entered.wait(60), f"The held peer never started its request: {peers(f)}"
    until(lambda: len(view(f)["run"]["submissions"]) == 1)
    until(lambda: sum(row["alive"] for row in f.runner.inspect_all()) == 1)


def follow(f):
    message = command(f, "request_plan", coordinator_requests=2, read_roots=[])
    result = f.service.operate(f.capture, message)
    until(lambda: len(view(f)["run"]["coordinator_proposals"]) == 2)
    until(lambda: sum(row["alive"] for row in f.runner.inspect_all()) == 1)
    return message, result


def test_partial_findings_are_attested_while_peer_runs_and_reused_labels_add_fresh_work(team):
    f = team
    start_peers(f)
    before = view(f)["run"]
    old_ids = {row["id"] for row in before["work_items"]}
    old_specifications = {row["id"]: row["specification"] for row in before["work_items"]}
    message, _ = follow(f)
    state = view(f)
    data = planning_data(f.backends[-1].captured[0])
    assert data["coordinator_read_roots"] == [] and data["read_roots"] == ["."]
    assert FINDING in str(data["recent_untrusted_findings"])
    assert len(data["existing_work"]) == 2 and data["proposed_work_namespace"]
    assert not {"file_read", "glob", "grep"} & {tool["function"]["name"] for tool in f.backends[-1].captured[0]["tools"]}
    assert sum(row["alive"] for row in f.runner.inspect_all()) == 1
    proposal = state["run"]["coordinator_proposals"][-1]
    receipt = next(row for row in state["run"]["coordinator_inputs"] if row["request_id"] == proposal["request_id"])
    assert receipt["input_sha256"] == json.loads(proposal["payload_json"])["input_sha256"]
    assert len(state["run"]["work_items"]) == 2 and not state["run"]["check_receipts"]
    count = len(f.backends)
    f.service.operate(f.capture, message)
    assert len(f.backends) == count  # Exact replay never launches again.
    decide(f, proposal)
    until(lambda: len(view(f)["run"]["submissions"]) == 2)
    after = view(f)["run"]
    assert len(after["work_items"]) == 3 and old_ids < {row["id"] for row in after["work_items"]}
    assert all(old_specifications[row["id"]] == row["specification"] for row in after["work_items"] if row["id"] in old_ids)
    assert after["scheduling"]["requests_per_worker"] == 3 and f.entered.is_set() and not f.release.is_set()
    assert len([row for row in after["attempts"] if row["kind"] == "coordinator"]) == 2


def test_followup_stale_revision_allowance_scope_and_changed_replay_do_not_dispatch(team):
    f = team
    start_peers(f)
    before = len(f.backends)
    stale = command(f, "request_plan", coordinator_requests=2, read_roots=[])
    stale["expected_revision"] = 0
    with pytest.raises(RevisionConflict):
        f.service.operate(f.capture, stale)
    with pytest.raises(AllowanceExceeded):
        f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=1000, read_roots=[]))
    with pytest.raises(ValueError):
        f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=2, read_roots=["../escape"]))
    with pytest.raises(ValueError, match="captured worker contract"):
        f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=2, read_roots=[], worker_requests=999))
    assert len(f.backends) == before
    message, _ = follow(f)
    with pytest.raises(IdempotencyConflict):
        f.service.operate(f.capture, {**message, "read_roots": ["."]})


def test_new_graph_state_invalidates_followup_proposal_without_erasing_results(team):
    f = team
    start_peers(f)
    follow(f)
    snapshot = view(f)["run"]
    proposal = snapshot["coordinator_proposals"][-1]
    submission = snapshot["submissions"][0]
    attempt = next(row for row in snapshot["attempts"] if row["id"] == submission["attempt_id"])
    f.service.operate(f.capture, command(f, "review_read_result", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
        candidate_revision=submission["candidate_revision"], evidence="Owner independently reviewed partial findings"))
    with pytest.raises(Conflict, match="graph changed"):
        decide(f, proposal)
    state = view(f)["run"]
    assert len(state["work_items"]) == 2 and len(state["check_receipts"]) == 1
    assert state["coordinator_proposals"][-1]["state"] == "pending" and not f.release.is_set()


def test_ambiguous_planner_launch_is_retained_and_never_replayed(team, monkeypatch):
    f = team
    start_peers(f)
    invoked = []
    def fail(*args, **kwargs):
        invoked.append(args[0])
        raise OSError("fixture lost launcher acknowledgement")
    monkeypatch.setattr(f.runner, "start_coordinator", fail)
    message = command(f, "request_plan", coordinator_requests=2, read_roots=[])
    with pytest.raises(OSError):
        f.service.operate(f.capture, message)
    replay = f.service.operate(f.capture, message)
    assert len(invoked) == 1 and not replay["coordinator_planning"]["available"]
    retained = next(row for row in replay["run"]["attempts"] if row["id"] == invoked[0].attempt_id)
    assert retained["state"] == "leased" and retained["process_state"] == "pending"
    reservation = next(row for row in replay["run"]["reservations"] if row["attempt_id"] == retained["id"])
    assert reservation["state"] == "reserved" and reservation["used"] is None
    with pytest.raises(Conflict, match="unresolved coordinator"):
        f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=1, read_roots=[]))


def test_stop_remains_responsive_during_planner_launch_preflight(team, monkeypatch):
    f = team
    start_peers(f)
    entered, release, errors = threading.Event(), threading.Event(), []
    original = f.runner.start_coordinator
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(60)
        return original(*args, **kwargs)
    monkeypatch.setattr(f.runner, "start_coordinator", held)
    message = command(f, "request_plan", coordinator_requests=2, read_roots=[])
    def launch():
        try:
            f.service.operate(f.capture, message)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=launch)
    thread.start()
    try:
        assert entered.wait(60)
        before = len(f.backends)
        start = time.monotonic()
        f.service.operate(f.capture, command(f, "stop"))
        assert time.monotonic() - start < 1
        with pytest.raises(Conflict):
            f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=1, read_roots=[]))
    finally:
        release.set()
        thread.join(60)
    assert not thread.is_alive() and errors and len(f.backends) == before


def test_gui_planning_admission_respects_foreground_chat_guard(setup):
    service, _, instances = setup
    state = SimpleNamespace(_swarm_desktop=service, _swarm_starting=False)
    replies = []
    async def send(value):
        replies.append(value)
    asyncio.run(swarming.command(state, send, {"action": "request_plan", "request_id": "gui-plan"}, chat_busy=True))
    assert "Finish or stop" in replies[0]["error"] and not state._swarm_starting and not instances


def test_real_child_followup_uses_exact_partial_input_while_owned_peer_process_lives(team, tmp_path):
    f = team
    entered, release, captured = tmp_path / "entered", tmp_path / "release", tmp_path / "input.json"
    Path(f.capture.workspace, "fact.txt").write_text("Observed independent fact", encoding="utf-8")
    script = child_script(tmp_path, f'''
import json, time
from pathlib import Path
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
class Backend(StreamingBackend):
    def stream(self, **kwargs):
        text = str(kwargs)
        if 'Captured planning data:' in text:
            Path({str(captured)!r}).write_text(json.dumps({{k:v for k,v in kwargs.items() if k!='cancel_event'}}, default=str), encoding='utf-8')
            yield text_delta({json.dumps(plan(follow_up=True))!r})
        elif 'Hold independent peer' in text:
            Path({str(entered)!r}).touch()
            deadline=time.monotonic()+180
            while not Path({str(release)!r}).exists():
                if kwargs.get('cancel_event') and kwargs['cancel_event'].is_set(): return
                if time.monotonic()>deadline: raise RuntimeError('fixture peer deadline')
                time.sleep(.02)
            yield text_delta('Late process result')
        elif not getattr(self,'read',False):
            self.read=True
            yield tool_call('file_read',{{'path':'fact.txt'}})
        else:
            yield text_delta({FINDING!r})
        yield done(model=self.model)
raise SystemExit(main(backend_factory=lambda spec: Backend(name=spec.backend_type,model=spec.model)))
''')
    f.runner._managed_readers = True
    f.runner._writer_process_factory = lambda: ManagedWorkerProcess(command=script, cancel_grace=.2)
    try:
        decide(f)
        # Two owned child processes start (Python and the engine) and one runs.
        until(lambda: entered.exists() and len(view(f)["run"]["submissions"]) == 1, timeout=90, describe=lambda: peers(f))
        until(lambda: sum(row["alive"] for row in f.runner.inspect_all()) == 1, timeout=90, describe=lambda: peers(f))
        before = view(f)["run"]
        peer = next(row for row in before["workers"] if row["alive"])
        assert peer["process_alive"] and peer["pid"]
        message = command(f, "request_plan", coordinator_requests=2, read_roots=[])
        f.service.operate(f.capture, message)
        until(lambda: len(view(f)["run"]["coordinator_proposals"]) == 2, timeout=90, describe=lambda: peers(f))
        inputs = json.loads(captured.read_text(encoding="utf-8"))
        assert FINDING in str(planning_data(inputs)["recent_untrusted_findings"])
        assert planning_data(inputs)["coordinator_read_roots"] == []
        assert not {"file_read", "glob", "grep"} & {item["function"]["name"] for item in inputs["tools"]}
        after = view(f)["run"]
        assert f.runner.inspect(peer["attempt_id"])["process_alive"] and not release.exists()
        coordinator = next(row for row in after["attempts"] if row["id"] == after["coordinator_proposals"][-1]["attempt_id"])
        assert next(row for row in after["process_observations"] if row["attempt_id"] == coordinator["id"])["state"] == "stopped"
        assert len(after["work_items"]) == 2 and len(after["submissions"]) == 1
        f.service.operate(f.capture, message)
        assert len(view(f)["run"]["attempts"]) == 4
    finally:
        release.touch()


def test_a_planning_turn_the_runner_refused_does_not_hold_the_team(team, monkeypatch):
    # Its admission commits first. A refusal before the runner took the turn
    # (the team paused in between, say) settles it as never invoked; unlike a
    # lost launch acknowledgement (above), nothing can have run.
    f = team
    before = view(f)["coordinator_planning"]["remaining_requests"]
    def refuse(*args, **kwargs):
        raise AdmissionClosed("Run admission is closed: paused")
    monkeypatch.setattr(f.runner, "start_coordinator", refuse)
    with pytest.raises(AdmissionClosed):
        f.service.operate(f.capture, command(f, "request_plan", coordinator_requests=2, read_roots=[]))
    state = view(f)
    turn = [row for row in state["run"]["attempts"] if row["kind"] == "coordinator"][-1]
    assert (turn["state"], turn["process_state"]) == ("cancelled", "stopped")
    assert state["coordinator_planning"]["available"] is True
    assert state["coordinator_planning"]["remaining_requests"] == before
