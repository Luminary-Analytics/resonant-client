"""The orchestrator runs on the session's model; the team's workers may run on another."""

import json
import time

import pytest

from lumi.engine.swarming.models import Conflict
from tests.test_swarm_autopilot import FINAL, team  # noqa: F401 - the shared fixture
from tests.test_swarm_coordinator import response as plan_response

WORKERS = {"provider": "ollama", "model": "small-worker"}


def start(service, capture, **extra):
    return service.operate(capture, {"request_id": "setup-request", "action": "start",
        "objective": "Check how input is handled", "plan_mode": "coordinator", "tasks": None,
        "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3,
        "autonomy": {"rounds": 1}, **extra})["run"]["run"]["id"]


def finished(service, capture, run_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})
        if view["run"]["run"]["state"] == "completed" and not view["autonomy"]["active"]:
            return view
        time.sleep(.05)
    raise AssertionError("The team did not complete")


def test_workers_run_on_their_own_model_while_the_orchestrator_uses_the_sessions(team):  # noqa: F811
    service, capture, outputs, backends = team
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(FINAL)]
    run_id = start(service, capture, worker_model=WORKERS)
    view = finished(service, capture, run_id)
    run = view["run"]
    # Orchestrator turns (the plan and the closing report) used the session's model.
    coordinators = {row["id"] for row in run["attempts"] if row["kind"] == "coordinator"}
    by_attempt = {row["id"]: json.loads(row["grant_json"])["model"] for row in run["attempts"]}
    assert {by_attempt[identity]["model"] for identity in coordinators} == {"chosen"}
    assert {grant["model"] for identity, grant in by_attempt.items() if identity not in coordinators} == {"small-worker"}
    assert [backend.model for backend in backends] == ["chosen", "small-worker", "small-worker", "chosen"]
    assert run["worker_model"] == {**WORKERS, "label": "ollama"}
    assert view["autonomy"]["final_report"] == FINAL["summary"]


def test_the_sessions_own_model_is_no_separate_choice(team):  # noqa: F811
    service, capture, outputs, backends = team
    outputs += [plan_response(), "Finding A.", "Finding B.", json.dumps(FINAL)]
    run_id = start(service, capture, worker_model={"provider": "ollama", "model": "chosen"})
    assert finished(service, capture, run_id)["run"]["worker_model"] is None


@pytest.mark.parametrize("choice, error, match", [
    ({"provider": "codex", "model": "gpt"}, Conflict, "Team workers can't run on Codex"),
    ({"provider": "claude-code", "model": "sonnet"}, Conflict, "Team workers can't run on Claude Code"),
    ({"provider": "kimi", "model": "k2"}, ValueError, "No API key for kimi"),
    ({"provider": "ollama"}, ValueError, "workers' provider and model"),
    ({"provider": "ollama", "model": " "}, ValueError, "workers' provider and model"),
    ("ollama:small", ValueError, "workers' provider and model"),
])
def test_a_workers_model_must_be_usable(team, choice, error, match, monkeypatch):  # noqa: F811
    service, capture, _, backends = team
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(error, match=match):
        start(service, capture, worker_model=choice)
    assert not backends and not service.busy
