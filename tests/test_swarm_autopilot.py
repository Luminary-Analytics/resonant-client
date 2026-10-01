"""A team whose owner let its orchestrator run it: rounds, peer mail and the final report."""

import json
import threading
import time

import pytest

from lumi.engine.swarming import AttemptContext, Scope
from lumi.engine.swarming.autopilot import CLOSING_EVIDENCE, PLAN_EVIDENCE
from lumi.engine.swarming.mailbox import SwarmMailbox
from lumi.engine.swarming.models import ScopeDenied
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.engine.swarming.tools import SwarmWorkerTools
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.streaming_stub import StreamingBackend, done, text_delta
from tests.test_swarm_coordinator import response as plan_response
from tests.test_swarm_mailbox import command as mailbox_command, setup as mailbox_fixture

mailbox_setup = mailbox_fixture

FINAL = {"summary": "Both areas were inspected: the API validates input and the UI escapes it.",
         "use_team": False, "work_items": []}


@pytest.fixture
def team(tmp_path):
    """A runtime whose scripted model answers each new participant with its next output."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    outputs, backends = [], []

    def factory(spec):
        output = outputs[len(backends)]
        # ("refused", status): the provider refuses this participant's request before any output.
        events = ([("error", {"message": "Too many requests", "status_code": output[1]})]
                  if isinstance(output, tuple) else [text_delta(output), done()])
        backend = StreamingBackend(events=events)
        backend.name, backend.model = spec.backend_type, spec.model
        backends.append(backend)
        return backend

    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"),
                              str(workspace), BackendSpec("ollama", "chosen"), "Project-specific fixture instruction")
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    yield service, capture, outputs, backends
    service.close()


def start(service, capture, *, rounds, request_limit=20):
    return service.operate(capture, {"request_id": "setup-request", "action": "start",
        "objective": "Check how input is handled", "plan_mode": "coordinator", "tasks": None,
        "request_limit": request_limit, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3,
        "autonomy": {"rounds": rounds}})["run"]["run"]["id"]


def finished(service, capture, run_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})
        if view["run"]["run"]["state"] in {"completed", "cancelled", "failed"} and not view["autonomy"]["active"]:
            return view
        time.sleep(.05)
    raise AssertionError(service.operate(capture, {"request_id": "last-view", "run_id": run_id})["autonomy"])


def test_the_orchestrator_plans_runs_its_workers_and_reports_without_owner_steps(team):
    service, capture, outputs, backends = team
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(FINAL)]
    run_id = start(service, capture, rounds=2)
    view = finished(service, capture, run_id)
    run = view["run"]
    assert run["run"]["state"] == "completed" and not service.busy
    assert view["autonomy"]["phase"] == "finished" and view["autonomy"]["final_report"] == FINAL["summary"]
    # Two orchestrator turns, both accepted under the grant; the second proposed no work.
    assert [row["state"] for row in run["coordinator_proposals"]] == ["accepted", "accepted"]
    assert {row["decision_evidence"] for row in run["coordinator_proposals"]} == {PLAN_EVIDENCE}
    # Results are accepted under the grant, never recorded as the owner's review.
    assert len(run["check_receipts"]) == 2
    assert {row["executor_id"] for row in run["check_receipts"]} == {"autonomy:fixture-owner"}
    assert all("not reviewed" in row["check_name"] for row in run["check_receipts"])
    assert all(row["state"] == "accepted" for row in run["work_items"])
    report = service.operate(capture, {"request_id": "report", "action": "export_report", "run_id": run_id})["report"]
    assert [row["kind"] for row in report["decisions"]] == ["autonomy_grant", "autonomy_grant"]
    # The orchestrator knew it was running the team, and its follow-up saw the findings.
    first, follow_up = backends[0].stream_calls[0]["user_msg"], backends[3].stream_calls[0]["user_msg"]
    assert "You are this team's orchestrator" in first and "If you can already answer the objective" in first
    assert "If the findings already meet the objective" not in first
    assert "The API validates input." in follow_up and "return work_items []" in follow_up


def test_after_its_rounds_the_orchestrator_writes_the_report_and_may_not_start_work(team):
    service, capture, outputs, backends = team
    more = json.loads(plan_response())
    more["summary"] = "Checked the API and UI; the CLI still needs a look."
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(more)]
    run_id = start(service, capture, rounds=1)
    view = finished(service, capture, run_id)
    run = view["run"]
    assert run["run"]["state"] == "completed"
    closing = run["coordinator_proposals"][-1]
    assert closing["state"] == "rejected" and closing["decision_evidence"] == CLOSING_EVIDENCE
    assert view["autonomy"]["final_report"] == more["summary"]
    assert len(run["work_items"]) == 2  # The closing turn's proposed work never ran.
    assert "This is your closing turn" in backends[3].stream_calls[0]["user_msg"]


def test_autonomy_needs_an_orchestrator_plan_and_a_bounded_number_of_rounds(team):
    service, capture, outputs, _ = team
    base = {"request_id": "bad", "action": "start", "objective": "Check input", "request_limit": 20,
            "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3}
    with pytest.raises(ValueError, match="orchestrator-planned"):
        service.operate(capture, {**base, "tasks": [{"objective": "Inspect A"}], "autonomy": {"rounds": 2}})
    for rounds in (0, 9, "2", None):
        with pytest.raises(ValueError, match="rounds"):
            service.operate(capture, {**base, "plan_mode": "coordinator", "tasks": None, "autonomy": {"rounds": rounds}})
    assert not service.busy


def test_results_are_accepted_under_the_grant_only_when_the_owner_gave_it(team):
    service, capture, outputs, _ = team
    outputs += ["Finding A", "Finding B"]
    run_id = service.operate(capture, {"request_id": "manual", "action": "start", "objective": "Check input",
        "tasks": [{"objective": "Inspect A", "read_roots": ["."]}, {"objective": "Inspect B", "read_roots": ["."]}],
        "request_limit": 4, "max_workers": 2})["run"]["run"]["id"]
    runner = service._runners[run_id][1]
    deadline = time.monotonic() + 20
    while len(runner.store.snapshot(capture.scope, run_id)["submissions"]) < 2 and time.monotonic() < deadline:
        time.sleep(.05)
    while any(row["alive"] for row in runner.inspect_all()) and time.monotonic() < deadline:
        time.sleep(.05)
    snapshot = runner.store.snapshot(capture.scope, run_id)
    attempt, submission = snapshot["attempts"][0], snapshot["submissions"][0]
    with pytest.raises(ScopeDenied, match="did not let this team's orchestrator"):
        service._command(runner.supervisor, runner.authority, "accept_under_grant", {
            "attempt_id": submission["attempt_id"], "attempt_epoch": attempt["epoch"],
            "candidate_revision": submission["candidate_revision"], "evidence": "Forged grant"})
    assert service.operate(capture, {"request_id": "view", "run_id": run_id})["autonomy"] is None


def test_workers_can_mail_the_orchestrator_between_its_turns(mailbox_setup):
    store, supervisor, authority, (sender, _) = mailbox_setup
    started = mailbox_command(supervisor, authority, "start_coordinator", worker_id="orchestrator-1", requests=2,
        model={"provider": "ollama", "model": "fixture"}, tools=["file_read"], read_roots=["."]).result
    mailbox_command(supervisor, authority, "worker_started", attempt_id=started["attempt_id"], attempt_epoch=authority.epoch)
    mailbox_command(supervisor, authority, "worker_stopped", attempt_id=started["attempt_id"],
                    attempt_epoch=authority.epoch, outcome="failed", evidence="Turn ended")
    message = SwarmMailbox(store, sender).send(recipient_attempt_id="orchestrator", kind="question",
                                               body="Should I also check the CLI?", command_id="ask")
    assert message.recipient_attempt_id == started["attempt_id"]
    with store._connection() as connection:
        rows = connection.execute("SELECT body FROM messages WHERE recipient_attempt_id=?",
                                  (started["attempt_id"],)).fetchall()
    assert [row[0] for row in rows] == ["Should I also check the CLI?"]


def test_the_orchestrator_does_not_mail_its_own_earlier_turns(mailbox_setup):
    # A live closing turn mailed its report to an earlier orchestrator turn.
    store, supervisor, authority, _ = mailbox_setup
    model = {"provider": "ollama", "model": "fixture"}
    first = mailbox_command(supervisor, authority, "start_coordinator", worker_id="orchestrator-1", requests=1,
                            model=model, tools=["file_read"], read_roots=["."]).result
    mailbox_command(supervisor, authority, "worker_started", attempt_id=first["attempt_id"], attempt_epoch=authority.epoch)
    mailbox_command(supervisor, authority, "worker_stopped", attempt_id=first["attempt_id"],
                    attempt_epoch=authority.epoch, outcome="completed", evidence="Turn ended")
    second = mailbox_command(supervisor, authority, "start_coordinator", worker_id="orchestrator-2", requests=1,
                             model=model, tools=["file_read"], read_roots=["."]).result
    mailbox_command(supervisor, authority, "worker_started", attempt_id=second["attempt_id"], attempt_epoch=authority.epoch)
    context = AttemptContext(authority.scope, authority.run_id, second["attempt_id"], "orchestrator-2", authority.epoch)
    with pytest.raises(ScopeDenied, match="own turns"):
        SwarmMailbox(store, context).send(recipient_attempt_id="orchestrator", kind="finding",
                                          body="Final report", command_id="report")


def test_a_worker_can_wait_for_an_answer_and_pause_ends_the_wait(mailbox_setup):
    store, _, _, (sender, receiver) = mailbox_setup
    stopping = threading.Event()
    tools = SwarmWorkerTools(SwarmMailbox(store, receiver), submit=lambda **_: {}, queue_message=lambda _: None,
                             stopping=stopping.is_set)
    reply = threading.Timer(.4, lambda: SwarmMailbox(store, sender).send(
        recipient_attempt_id=receiver.attempt_id, kind="answer", body="Yes, check it", command_id="answer"))
    reply.start()
    began = time.monotonic()
    result = json.loads(tools.execute("swarm_receive", {"wait_seconds": 20}).output)
    assert [row["body"] for row in result["messages"]] == ["Yes, check it"] and time.monotonic() - began < 10
    stopping.set()
    began = time.monotonic()
    result = json.loads(tools.execute("swarm_receive", {"after": result["cursor"], "wait_seconds": 20}).output)
    assert result["messages"] == [] and time.monotonic() - began < 2
    assert tools.execute("swarm_receive", {"wait_seconds": 61}).is_error


def test_a_refused_request_fails_its_task_cleanly_and_the_orchestrator_retries_it(team, monkeypatch):
    # A rate-limited provider (429) generated nothing: the request's outcome is
    # known, the worker fails, and the loop retries the task after a pause.
    monkeypatch.setattr("lumi.engine.swarming.autopilot.RETRY_DELAY_SECONDS", .3)
    service, capture, outputs, backends = team
    outputs += [plan_response(), ("refused", 429), "The UI escapes output.", "The API validates input.",
                json.dumps(FINAL)]
    run_id = start(service, capture, rounds=2)
    view = finished(service, capture, run_id)
    run = view["run"]
    assert run["run"]["state"] == "completed" and view["autonomy"]["final_report"] == FINAL["summary"]
    assert all(row["state"] == "accepted" for row in run["work_items"])
    refused = [row for row in run["model_requests"] if row["state"] != "completed"]
    assert refused == []  # Settled as known, not held as uncertain.
    assert [row["state"] for row in run["attempts"] if row["kind"] == "worker"].count("failed") == 1
    assert any("provider status 429" in (row.get("observation_error") or "") for row in run["request_inputs"])


def test_two_unusable_orchestrator_turns_in_a_row_hand_the_team_back(team):
    # A live model replied twice without a valid plan; a third turn would only
    # spend the allowance again.
    service, capture, outputs, backends = team
    outputs += ["I will look at the files first.", "Still thinking about it."]
    run_id = start(service, capture, rounds=2)
    deadline = time.monotonic() + 30
    view = None
    while time.monotonic() < deadline:
        view = service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})
        if view["autonomy"]["phase"] == "needs_owner":
            break
        time.sleep(.05)
    assert view["autonomy"]["phase"] == "needs_owner", view["autonomy"]
    assert "could not produce a usable plan twice" in view["autonomy"]["detail"]
    time.sleep(1.5)  # Several loop ticks: no third orchestrator turn starts.
    coordinators = [row for row in service.operate(capture, {"request_id": "last", "run_id": run_id})["run"]["attempts"]
                    if row["kind"] == "coordinator"]
    assert len(coordinators) == 2 and len(backends) == 2
    # The retry was told why its first plan was refused, and the first turn wasn't.
    assert "Your previous plan was refused" not in backends[0].stream_calls[0]["user_msg"]
    assert ("Your previous plan was refused: Coordinator output must be one strict JSON object"
            in backends[1].stream_calls[0]["user_msg"])
    assert view["run"]["run"]["state"] == "running" and not view["run"]["coordinator_proposals"]


def test_an_orchestrator_can_answer_directly_and_the_team_completes_without_workers(team):
    # A live Kimi K3 read the files, answered, and wrapped its JSON in prose.
    service, capture, outputs, backends = team
    outputs += ["I found both defects by reading the files, so no worker round is needed.\n\n```json\n"
                + json.dumps(FINAL) + "\n```"]
    run_id = start(service, capture, rounds=2)
    view = finished(service, capture, run_id)
    run = view["run"]
    assert run["run"]["state"] == "completed" and not service.busy
    assert view["autonomy"]["final_report"] == FINAL["summary"] and view["autonomy"]["phase"] == "finished"
    assert run["work_items"] == [] and [row["state"] for row in run["coordinator_proposals"]] == ["accepted"]
    assert len(backends) == 1  # No worker ran.
