"""Single steps of the orchestrator loop against retained team states it must not misread."""

import json
import threading
from types import SimpleNamespace

import pytest

from lumi.engine.swarming.autopilot import PLAN_EVIDENCE, STALE_EVIDENCE, TeamAutopilot
from lumi.engine.swarming.coordinator import ANSWER_WORKER_PREFIX
from lumi.engine.swarming.models import Conflict


def snapshot(**parts):
    state = {"run": {"state": "running", "revision": 7, "epoch": 1},
             "attempts": [], "coordinator_proposals": [], "work_items": [], "messages": [], "receipts": [],
             "submissions": [], "integration_operations": [], "integration_candidates": [],
             "integration_checks": [], "reservations": [], "writer_worktrees": []}
    return {**state, **parts}


def attempt(identity, kind="worker", *, state="running", process_state="running", work_item_id=None,
            worker_id=None, cancel_requested=0, epoch=1):
    return {"id": identity, "kind": kind, "state": state, "process_state": process_state, "work_item_id": work_item_id,
            "worker_id": worker_id or f"worker-{identity}", "cancel_requested": cancel_requested, "epoch": epoch}


def stopped(identity, kind="worker", state="submitted", **fields):
    return attempt(identity, kind, state=state, process_state="stopped", **fields)


def item(identity, state, *, writes=False, criteria=None):
    specification = {"write_roots": ["src"] if writes else [],
                     "criteria": criteria or (["tests"] if writes else ["owner_review"])}
    return {"id": identity, "state": state, "specification": json.dumps(specification)}


class Scheduler:
    requests_per_worker = 3

    def __init__(self, blocked=None):
        self.blocked, self.started = blocked or {}, 0

    def inspect(self):
        return {"error": "", "blocked": self.blocked}

    def start(self):
        self.started += 1


class Runtime:
    """The runtime surface the loop steps through, recording what it asks for."""

    def __init__(self, state, *, scheduler=None, remaining=20, failures=()):
        self.state = state
        self.runner = SimpleNamespace(store=SimpleNamespace(snapshot=lambda scope, run_id: self.state),
                                      supervisor="supervisor", authority="authority",
                                      inspect=lambda attempt_id: {"error": ""})
        self.capture = SimpleNamespace(scope="scope", workspace="project")
        self._runners = {"run": (self.capture, self.runner)}
        self._schedulers = {"run": scheduler or Scheduler()}
        self._lock = threading.Lock()
        self.remaining, self.failures = remaining, list(failures)
        self.commands, self.answers, self.operations, self.plans = [], [], [], []

    def _command(self, supervisor, authority, kind, payload):
        self.commands.append((kind, payload))
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure

    def _planning_view(self, store, run_id, snapshot):
        return {"available": True, "reason": None, "remaining_requests": self.remaining,
                "default_requests": 3, "read_roots": ["."]}

    # The organization's rules and budgets (organization.py): none refuse here.
    def team_dispatch_refusal(self, run_id):
        return ""

    def team_policy_refusal(self, run_id, action):
        return ""

    def team_governance(self, run_id):
        return None

    def _answer_workers(self, capture, **turn):
        self.answers.append(turn)

    def operate(self, capture, message):
        self.operations.append(message)

    def _request_plan(self, capture, message, *, closing=False, retry_reason=""):
        self.plans.append((message, closing, retry_reason))


def test_an_orchestrator_turn_without_a_known_outcome_hands_the_team_back():
    runtime = Runtime(snapshot(attempts=[stopped("plan", "coordinator", "uncertain", worker_id="coordinator-1")]))
    loop = TeamAutopilot(runtime, "run", rounds=2)
    assert loop.step() is True
    assert loop.phase == "needs_owner" and "without a known outcome" in loop.detail
    assert runtime.commands == runtime.plans == []


def test_rules_that_refuse_the_team_hand_it_back_before_any_step():
    # A policy or budget refusing the team (organization.py) stops the loop's
    # steps, even with retryable work and a question waiting; it resumes when
    # the rules allow the team again.
    runtime = Runtime(snapshot(work_items=[item("api", "failed")],
                               attempts=[stopped("a1", "worker", "failed", work_item_id="api")]))
    refusal = ["Acme's policy doesn't allow chosen on ollama, so the team can't use it."]
    runtime.team_dispatch_refusal = lambda run_id: refusal[0]
    loop = TeamAutopilot(runtime, "run", rounds=2)
    assert loop.step() is True
    assert loop.phase == "needs_owner" and loop.detail == refusal[0]
    assert runtime.commands == runtime.plans == runtime.answers == []
    refusal[0] = ""
    loop.step()
    assert loop.phase == "working" and "retrying it shortly" in loop.detail


def test_planned_tasks_dispatch_keeps_refusing_hand_the_team_back():
    reason = "Request allowance is already allocated or exhausted"
    runtime = Runtime(snapshot(work_items=[item("api", "accepted"), item("ui", "ready")]),
                      scheduler=Scheduler(blocked={"ui": reason}))
    loop = TeamAutopilot(runtime, "run", rounds=2)
    # A refusal from before the last task finished may still be showing.
    loop.step()
    loop.step()
    assert loop.phase == "working"
    loop.step()
    assert loop.phase == "needs_owner" and f"can't start 1 planned task: {reason}" in loop.detail
    # While other work runs, a refused task waits for it.
    runtime.state = snapshot(work_items=[item("api", "running"), item("ui", "ready")],
                             attempts=[attempt("a1", work_item_id="api")])
    loop.step()
    assert loop.phase == "working" and loop._stuck == 0


def writers_after_a_failed_check():
    manifest = {"writers": [{"id": "w1", "attempt_id": "a1"}, {"id": "w2", "attempt_id": "a2"}],
                "checks": [{"key": "tests"}]}
    return snapshot(
        work_items=[item("api", "submitted", writes=True), item("ui", "submitted", writes=True)],
        attempts=[stopped("a1", work_item_id="api"), stopped("a2", work_item_id="ui")],
        reservations=[{"attempt_id": "a1", "state": "settled"}, {"attempt_id": "a2", "state": "settled"}],
        writer_worktrees=[{"id": "w1", "attempt_id": "a1", "state": "ready", "epoch": 1},
                          {"id": "w2", "attempt_id": "a2", "state": "ready", "epoch": 1}],
        integration_candidates=[{"id": "c1", "state": "failed", "manifest_json": json.dumps(manifest)}],
        integration_checks=[{"candidate_id": "c1", "check_key": "tests", "state": "failed", "exit_code": 1}])


def test_a_failed_check_sends_every_writer_in_the_change_back_at_once():
    runtime = Runtime(writers_after_a_failed_check())
    loop = TeamAutopilot(runtime, "run", rounds=1, apply=True)
    loop.step()
    # Which change broke the check isn't known: both go back in one step.
    assert [(kind, payload["attempt_id"]) for kind, payload in runtime.commands] == [("reject", "a1"), ("reject", "a2")]
    assert "failed the declared check tests (failed, exit code 1)" in runtime.commands[0][1]["evidence"]
    # A failed check needs no pause before each writer's one retry.
    runtime.state = snapshot(work_items=[item("api", "failed", writes=True), item("ui", "failed", writes=True)],
                             attempts=[stopped("a1", "worker", "failed", work_item_id="api"),
                                       stopped("a2", "worker", "failed", work_item_id="ui")])
    loop.step()
    loop.step()
    assert [(kind, payload["work_item_id"]) for kind, payload in runtime.commands[2:]] == [
        ("retry", "api"), ("retry", "ui")]


def test_what_the_owner_stopped_is_not_retried():
    task = Runtime(snapshot(work_items=[item("api", "cancelled")],
                            attempts=[stopped("a1", "worker", "cancelled", work_item_id="api", cancel_requested=1)]))
    loop = TeamAutopilot(task, "run", rounds=2)
    loop.step()
    assert task.commands == [] and loop.phase == "needs_owner" and "you stopped it" in loop.detail
    turn = Runtime(snapshot(attempts=[stopped("plan", "coordinator", "cancelled", worker_id="coordinator-1",
                                              cancel_requested=1)]))
    loop = TeamAutopilot(turn, "run", rounds=2)
    loop.step()
    assert turn.plans == [] and "You stopped the orchestrator's turn" in loop.detail


def asked(*, remaining, queued=0, seen=False):
    """A running worker asked the orchestrator a question; one step of the loop."""
    attempts = [stopped("plan", "coordinator", "completed", worker_id="coordinator-1"), attempt("a1", work_item_id="api")]
    receipts = []
    if seen:  # An answer turn already had the question in its input.
        attempts.append(stopped("answer", "coordinator", "completed", worker_id=ANSWER_WORKER_PREFIX + "1"))
        receipts.append({"message_id": "m1", "stage": "context", "recipient_attempt_id": "answer"})
    runtime = Runtime(snapshot(
        work_items=[item("api", "running")] + [item(f"later-{index}", "pending") for index in range(queued)],
        attempts=attempts, receipts=receipts,
        messages=[{"id": "m1", "sequence": 4, "sender_attempt_id": "a1", "recipient_attempt_id": "plan",
                   "kind": "question"}]), remaining=remaining)
    loop = TeamAutopilot(runtime, "run", rounds=2)
    loop.step()
    return runtime, loop


def test_answer_turns_spend_only_requests_planned_work_does_not_need():
    runtime, _ = asked(remaining=10)
    assert [(turn["questions"], turn["requests"]) for turn in runtime.answers] == [([4], 3)]
    # One queued task keeps its three requests; two are left, enough for one answer.
    runtime, _ = asked(remaining=5, queued=1)
    assert [turn["requests"] for turn in runtime.answers] == [2]
    # One request can't answer (the last one offers no tools): the next plan reads the question.
    runtime, loop = asked(remaining=4, queued=1)
    assert runtime.answers == [] and loop._answered == {4}
    loop.step()
    assert runtime.answers == []
    runtime, _ = asked(remaining=10, seen=True)
    assert runtime.answers == []


def pending_plan():
    plan = {"summary": "Inspect the API", "use_team": True, "work_items": [{"id": "api"}]}
    return snapshot(coordinator_proposals=[{"id": "p1", "sha256": "digest", "state": "pending", "attempt_id": "plan",
                                            "payload_json": json.dumps({"plan": plan})}])


def test_a_plan_made_on_out_of_date_work_is_declined_with_the_reason():
    runtime = Runtime(pending_plan(), failures=[Conflict("Work graph changed after the proposal input; request a fresh plan")])
    loop = TeamAutopilot(runtime, "run", rounds=2)
    loop.step()
    assert [(payload["accept"], payload["evidence"]) for _, payload in runtime.commands] == [
        (True, PLAN_EVIDENCE), (False, STALE_EVIDENCE)]
    assert runtime._schedulers["run"].started == 0 and "out-of-date work" in loop.detail
    # Any other refusal is the runtime's to report, not a reason to decline.
    runtime = Runtime(pending_plan(), failures=[Conflict("Proposal policy contract does not match the current run")])
    with pytest.raises(Conflict, match="policy contract"):
        TeamAutopilot(runtime, "run", rounds=2).step()
    assert len(runtime.commands) == 1


def test_a_read_only_task_with_check_criteria_is_handed_back():
    # Plans can no longer declare them (planning.py); one retained from before can't finish on its own.
    runtime = Runtime(snapshot(work_items=[item("api", "submitted", criteria=["tests"])],
                               attempts=[stopped("a1", work_item_id="api")],
                               submissions=[{"attempt_id": "a1", "candidate_revision": "r1"}]))
    loop = TeamAutopilot(runtime, "run", rounds=2)
    loop.step()
    assert runtime.commands == [] and "declared checks that nothing runs" in loop.detail


def test_a_resumed_loop_keeps_the_owners_choices_and_the_steps_already_spent():
    manifest = {"writers": [{"id": "w1", "attempt_id": "a3"}], "checks": [{"key": "tests"}]}
    state = snapshot(
        work_items=[item("api", "accepted"), item("ui", "failed"), item("cli", "submitted", writes=True)],
        attempts=[stopped("a1", "worker", "failed", work_item_id="api"), stopped("a2", "worker", "accepted", work_item_id="api"),
                  stopped("a4", "worker", "failed", work_item_id="ui"), stopped("a3", work_item_id="cli")],
        reservations=[{"attempt_id": "a3", "state": "settled"}],
        writer_worktrees=[{"id": "w1", "attempt_id": "a3", "state": "ready", "epoch": 1}],
        integration_candidates=[{"id": "c1", "state": "verified", "base_revision": "base", "result_revision": "result",
                                 "manifest_json": json.dumps(manifest)}],
        integration_checks=[{"candidate_id": "c1", "check_key": "tests", "state": "passed", "exit_code": 0}],
        integration_operations=[{"kind": "apply", "state": "failed", "payload_json": json.dumps({"candidate_id": "c1"})}])
    runtime = Runtime(state)
    loop = TeamAutopilot.resumed(runtime, "run", {"rounds": 2, "apply": True}, state)
    # A task retried before, and one the owner left failed at Continue, aren't retried.
    assert loop._retried == {"api", "ui"}
    loop.step()
    assert not [kind for kind, _ in runtime.commands if kind == "retry"]
    # The application that failed before the restart isn't tried again: the checkout is the owner's.
    assert runtime.operations == [] and loop.phase == "needs_owner" and "couldn't be applied" in loop.detail


def test_a_pausing_team_says_so():
    runtime = Runtime(snapshot(run={"state": "pausing", "revision": 7, "epoch": 1}))
    loop = TeamAutopilot(runtime, "run", rounds=2)
    assert loop.step() is True and (loop.phase, loop.detail) == ("paused", "The team is pausing.")
