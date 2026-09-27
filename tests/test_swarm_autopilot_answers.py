"""The team's orchestrator answers a running worker's question in the same round."""

import json
import time

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.coordinator import ANSWER_WORKER_PREFIX
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_workers import until

QUESTION = "Should I also check the CLI's input handling?"
ANSWER = "Yes: the CLI shares the API's validator, so check it too."
FINAL = {"summary": "The API and the CLI validate input; the UI escapes it.", "use_team": False, "work_items": []}


def data(text, marker):
    return json.loads(text.split(marker, 1)[1].split("\n</runtime_message>", 1)[0])


class Participant(StreamingBackend):
    """Scripts each participant from its own generated input, whatever order participants start in."""

    def __init__(self, team, **kwargs):
        super().__init__(scripts=[], **kwargs)
        self.team = team

    def stream(self, **kwargs):
        if not self.stream_count:
            self.prompt = str(kwargs.get("user_msg") or "")
            self._scripts = self.team.script(self.prompt)
        yield from super().stream(**kwargs)


class Team:
    def __init__(self):
        self.backends = []
        self.answer_prompts = []

    def factory(self, spec):
        backend = Participant(self)
        backend.name, backend.model = spec.backend_type, spec.model
        self.backends.append(backend)
        return backend

    def script(self, prompt):
        if "Captured answer data:" in prompt:
            self.answer_prompts.append(prompt)
            questions = data(prompt, "Captured answer data:\n")["untrusted_questions"]
            return [[tool_call("swarm_send", {"recipient_attempt_id": question["from_attempt_id"], "kind": "answer",
                                              "body": ANSWER, "command_id": f"answer-{question['sequence']}"},
                               f"answer-{question['sequence']}") for question in questions] + [done()],
                    [text_delta(f"Answered {len(questions)} question."), done()]]
        if "Captured planning data:" in prompt:
            if data(prompt, "Captured planning data:\n")["proposed_work_namespace"] is None:
                plan = {"summary": "Inspect the API and the UI.", "use_team": True, "work_items": [
                    {"id": name, "objective": f"Inspect the {name.upper()}", "role": "explore", "dependencies": [],
                     "read_roots": ["."], "write_roots": [], "criteria": ["owner_review"]} for name in ("api", "ui")]}
                return [[text_delta(json.dumps(plan)), done()]]
            return [[text_delta(json.dumps(FINAL)), done()]]
        if "Inspect the API" in prompt:
            # Ask the orchestrator, wait for its answer, then report.
            return [[tool_call("swarm_send", {"recipient_attempt_id": "orchestrator", "kind": "question",
                                              "body": QUESTION, "command_id": "ask"}, "ask"), done()],
                    [tool_call("swarm_receive", {"wait_seconds": 30}, "wait"), done()],
                    [text_delta("The API validates input, and the CLI shares its validator."), done()]]
        return [[text_delta("The UI escapes output."), done()]]


@pytest.fixture
def team(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    participants = Team()
    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=participants.factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"),
                              str(workspace), BackendSpec("ollama", "chosen"), "Project-specific fixture instruction")
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    yield service, capture, participants
    service.close()


def test_a_running_worker_gets_the_orchestrators_answer_in_the_same_round(team):
    service, capture, participants = team
    run_id = service.operate(capture, {"request_id": "setup-request", "action": "start",
        "objective": "Check how input is handled", "plan_mode": "coordinator", "tasks": None,
        "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3,
        "autonomy": {"rounds": 1}})["run"]["run"]["id"]
    runner = service._runners[run_id][1]
    view = until(lambda: (lambda current: current if current["run"]["run"]["state"] == "completed"
                          and not current["autonomy"]["active"] else None)(
        service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})), timeout=60)
    run = view["run"]
    assert view["autonomy"]["final_report"] == FINAL["summary"]
    # One answer turn, which completed without proposing anything.
    answers = [row for row in run["attempts"] if row["worker_id"].startswith(ANSWER_WORKER_PREFIX)]
    assert [row["state"] for row in answers] == ["completed"]
    assert [row["state"] for row in run["coordinator_proposals"]] == ["accepted", "accepted"]
    assert all(row["attempt_id"] != answers[0]["id"] for row in run["coordinator_proposals"])
    # It saw the question and answered its sender while that worker still ran.
    asker = next(row for row in run["attempts"] if row["kind"] == "worker"
                 and any(message["sender_attempt_id"] == row["id"] for message in run["messages"]))
    assert QUESTION in participants.answer_prompts[0]
    reply, = [row for row in run["messages"] if row["sender_attempt_id"] == answers[0]["id"]]
    assert (reply["recipient_attempt_id"], reply["kind"], reply["body"]) == (asker["id"], "answer", ANSWER)
    received = [event for event in runner.poll(limit=1000)["events"] if event.get("event") == "tool.result"
                and event.get("name") == "swarm_receive" and event.get("attempt_id") == asker["id"]]
    assert received and ANSWER in received[-1]["output"]
    # The answer turn spent team requests, not a worker's allowance.
    answer_requests = [row for row in run["model_requests"] if row["attempt_id"] == answers[0]["id"]]
    assert len(answer_requests) == 2
