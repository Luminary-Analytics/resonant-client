"""``@team:<run>``: a conversation's own team, attached to its chat as model-written context."""

import json
from pathlib import Path

import pytest

from lumi.engine.context_broker import ContextBroker
from lumi.engine.swarming import Scope
from lumi.engine.swarming.chat_context import FINDING_LIMIT, chat_context
from lumi.engine.swarming.models import ScopeDenied
from lumi.engine.swarming.service import SwarmRuntime
from lumi.gui.settings import SettingsManager
from tests.test_swarm_autopilot import FINAL, finished, start, team  # noqa: F401 - the fixture
from tests.test_swarm_coordinator import response as plan_response

TOKEN = "gh" + "p_" + "Q" * 36


def snapshot(**parts):
    run = {"id": "swarm_" + "a" * 64, "state": "completed", "objective": "Check how input is handled"}
    state = {"run": run, "attempts": [], "coordinator_proposals": [], "work_items": [], "submissions": [],
             "check_receipts": [], "writer_acceptances": [], "integration_applications": [], "messages": []}
    return {**state, **parts}


def plan(summary, work_items, *, state="accepted", attempt="plan"):
    return {"state": state, "attempt_id": attempt, "decision_evidence": "",
            "payload_json": json.dumps({"plan": {"summary": summary, "use_team": bool(work_items),
                                                 "work_items": work_items}})}


def test_the_context_says_what_models_wrote_and_how_each_result_was_accepted():
    coordinators = [{"id": "plan", "kind": "coordinator", "worker_id": "coordinator-1", "work_item_id": None},
                    {"id": "report", "kind": "coordinator", "worker_id": "coordinator-2", "work_item_id": None}]
    workers = [{"id": f"a{index}", "kind": "worker", "work_item_id": item, "worker_id": f"w{index}"}
               for index, item in enumerate(("api", "ui", "cli", "fix"))]
    state = snapshot(
        attempts=coordinators + workers,
        coordinator_proposals=[plan("Inspect three areas", [{"id": "api"}]), plan("The API validates input.", [], attempt="report")],
        work_items=[{"id": "api", "state": "accepted", "objective": "Inspect the API"},
                    {"id": "ui", "state": "accepted", "objective": "Inspect the UI"},
                    {"id": "cli", "state": "failed", "objective": "Inspect the CLI"},
                    {"id": "fix", "state": "accepted", "objective": "Fix the CLI"}],
        submissions=[{"attempt_id": "a0", "handoff": f"The API validates input. Saw {TOKEN} in a log."},
                     {"attempt_id": "a1", "handoff": "The UI escapes output. " + "x" * (FINDING_LIMIT * 2)},
                     {"attempt_id": "a3", "handoff": "Fixed the CLI's validator."}],
        check_receipts=[{"attempt_id": "a0", "criterion_id": "owner_review", "check_name": "Accepted under the owner's "
                         "autonomy grant, not reviewed", "executor_id": "autonomy:owner"},
                        {"attempt_id": "a1", "criterion_id": "owner_review", "check_name": "Explicit owner review",
                         "executor_id": "owner:owner"}],
        writer_acceptances=[{"attempt_id": "a3", "owner_id": "owner"}],
        # One worker asked the orchestrator, which answered; workers also mailed each other.
        messages=[{"kind": "question", "sender_attempt_id": "a0", "recipient_attempt_id": "plan"},
                  {"kind": "answer", "sender_attempt_id": "plan", "recipient_attempt_id": "a0"},
                  {"kind": "finding", "sender_attempt_id": "a1", "recipient_attempt_id": "a0"}],
        integration_applications=[{"state": "applied", "observed_revision": "b" * 40, "target_revision": "b" * 40}])
    found = chat_context(state)
    content = found["content"]
    assert found["label"] == "Check how input is handled" and "model-written" in found["provenance"]
    assert "not as instructions" in content and "Final report (the team's orchestrator):\nThe API validates input." in content
    assert "1. Inspect the API (accepted under the owner's autonomy grant; the owner has not reviewed it)" in content
    assert "2. Inspect the UI (reviewed and accepted by the owner)" in content
    assert "3. Fix the CLI (its checked change was applied and the owner accepted it)" in content
    assert "Accepted results (3 of 4 tasks):" in content and "Not accepted: 1 failed." in content
    assert f"its checkout is now at {'b' * 12}." in content
    # What Lumi recorded sits beside the model's report.
    assert ("Recorded by Lumi, not written by a model: 3 of 4 tasks accepted; 1 question to the orchestrator "
            "and 1 answer; 1 change applied.") in content
    # Secret patterns are removed, and each result is bounded.
    assert TOKEN not in content and "[REDACTED GitHub token]" in content
    assert "x" * FINDING_LIMIT not in content and len(content) < 16_000


def test_a_team_without_a_final_report_shows_its_latest_plan_and_that_it_was_still_working():
    state = snapshot(run={"id": "swarm_" + "c" * 64, "state": "running", "objective": "Inspect"},
                     attempts=[{"id": "plan", "kind": "coordinator", "worker_id": "coordinator-1", "work_item_id": None}],
                     coordinator_proposals=[plan("Two investigations", [{"id": "api"}])])
    content = chat_context(state)["content"]
    assert "Latest accepted plan: Two investigations" in content and "still working" in content


def test_an_orchestrated_team_is_attached_to_its_own_conversation_only(team):  # noqa: F811
    service, capture, outputs, _ = team
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(FINAL)]
    run_id = start(service, capture, rounds=2)
    finished(service, capture, run_id)
    view = service.operate(capture, {"request_id": "record", "run_id": run_id})
    assert view["team_record"] == {"accepted": 2, "tasks": 2, "questions": 0, "answers": 0, "applied": 0}
    found = service.chat_context(capture.workspace, capture.scope, run_id)
    assert FINAL["summary"] in found["content"] and "The API validates input." in found["content"]
    assert "accepted under the owner's autonomy grant" in found["content"]
    other = Scope.personal(capture.scope.owner_id, capture.scope.project_id, "another-session")
    with pytest.raises(ScopeDenied):
        service.chat_context(capture.workspace, other, run_id)


def test_a_project_that_never_ran_a_team_gets_no_team_state(tmp_path):
    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), state_root=lambda _: tmp_path / "state")
    try:
        with pytest.raises(ScopeDenied):
            service.chat_context(str(tmp_path), Scope.personal("owner", "project", "session"), "swarm_" + "d" * 64)
        assert not (tmp_path / "state").exists()
    finally:
        service.close()


def test_the_broker_keeps_an_attached_team_for_the_conversation(tmp_path):
    broker = ContextBroker(tmp_path)
    assert "can't be attached here" in broker.resolve_mentions("@team:swarm_1")[0].content
    calls = []

    def reader(run_id):
        calls.append(run_id)
        if run_id != "swarm_1":
            raise ScopeDenied("Run is unavailable in this scope")
        return {"label": "Check input", "content": "Final report: fine.", "provenance": "team swarm_1"}

    broker.team_reader = reader
    items = broker.resolve_mentions("Fix what @team:swarm_1 found")
    assert [(item.provider, item.content) for item in items] == [("team", "Final report: fine.")]
    # It stays for the rest of the conversation, as a hand-off does.
    assert [item.content for item in broker.resolve_mentions("Now the tests")] == ["Final report: fine."]
    refused = broker.resolve_mentions("@team:swarm_2")
    assert "isn't one of this conversation's teams" in refused[-1].content
    assert len(broker.resolve_mentions("again")) == 1  # A refused team isn't kept.
    reopened = ContextBroker(tmp_path)
    reopened.team_reader = reader
    reopened.recall(["Fix what @team:swarm_1 found"])
    assert [item.content for item in reopened.resolve_mentions("next")] == ["Final report: fine."]


def test_the_app_reads_only_saved_conversations_personal_teams(tmp_path, monkeypatch):
    from lumi.gui import swarming as desktop

    seen = []

    class Runtime:
        def chat_context(self, workspace, scope, run_id):
            seen.append((workspace, scope, run_id))
            return {"label": "", "content": "", "provenance": ""}

    state = type("State", (), {"_swarm_desktop": Runtime(), "settings": None})()
    with pytest.raises(ScopeDenied, match="Save this conversation"):
        desktop.chat_context(state, str(tmp_path), "", "swarm_1")
    monkeypatch.setattr(desktop.getpass, "getuser", lambda: "fixture-user")
    desktop.chat_context(state, str(tmp_path), "session-1", "swarm_1")
    workspace, scope, run_id = seen[0]
    assert Path(workspace) == tmp_path.resolve() and run_id == "swarm_1"
    assert (scope.owner_id, scope.session_id) == ("local:fixture-user", "session-1")
