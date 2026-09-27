"""A team its owner let the orchestrator run keeps that loop when the owner continues it after a lost host."""

import json
import time

from lumi.engine.swarming import Scope
from lumi.engine.swarming.autopilot import CLOSING_EVIDENCE, PLAN_EVIDENCE, TeamAutopilot
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.test_swarm_autopilot_answers import FINAL, Team
from tests.test_swarm_workers import until


def view(runtime, capture, run_id):
    return runtime.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})


def test_a_continued_team_resumes_its_orchestrator_loop(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    participants = Team()
    now = [time.time()]
    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=participants.factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"),
                              str(workspace), BackendSpec("ollama", "chosen"), "Project-specific fixture instruction")
    service._store(capture).clock = lambda: now[0]
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    reopened = None
    try:
        run_id = service.operate(capture, {"request_id": "setup-request", "action": "start",
            "objective": "Check how input is handled", "plan_mode": "coordinator", "tasks": None,
            "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3,
            "autonomy": {"rounds": 1}})["run"]["run"]["id"]
        # The app loses its host before the orchestrator loop decides the plan.
        service._autopilots[run_id].close()
        store = service._store(capture)
        until(lambda: store.snapshot(capture.scope, run_id)["coordinator_proposals"] and all(
            row["process_state"] == "stopped" for row in store.snapshot(capture.scope, run_id)["attempts"]), timeout=20)
        old = service._runners[run_id][1]
        old._maintenance_stop.set()
        old._maintenance.join(timeout=1)
        now[0] += 61
        reopened = SwarmRuntime(service.settings, backend_factory=participants.factory, state_root=service._state_root)
        reopened._store(capture).clock = lambda: now[0]
        retained = view(reopened, capture, run_id)
        assert retained["autonomy"]["active"] is False and "isn't running on this host" in retained["autonomy"]["detail"]
        owned = reopened.operate(capture, {"action": "recover", "request_id": "takeover", "run_id": run_id,
                                          "expected_revision": retained["run"]["run"]["revision"]})["run"]
        reopened.operate(capture, {"action": "continue_recovered", "request_id": "continue", "run_id": run_id,
                                   "expected_revision": owned["run"]["revision"], "retry_work_items": [],
                                   "worker_requests": 3})
        finished = until(lambda: (lambda current: current if current["run"]["run"]["state"] == "completed"
                                  and not current["autonomy"]["active"] else None)(view(reopened, capture, run_id)),
                         timeout=60)
        run = finished["run"]
        # The resumed loop decided the retained plan, ran the round and wrote the closing report.
        assert finished["autonomy"]["final_report"] == FINAL["summary"]
        assert [row["state"] for row in run["coordinator_proposals"]] == ["accepted", "accepted"]
        assert {row["decision_evidence"] for row in run["coordinator_proposals"]} == {PLAN_EVIDENCE}
        assert all(row["state"] == "accepted" for row in run["work_items"]) and len(run["work_items"]) == 2
        assert {row["executor_id"] for row in run["check_receipts"]} == {"autonomy:fixture-owner"}
        assert {row["epoch"] for row in run["attempts"] if row["kind"] == "worker"} == {2}
    finally:
        if reopened is not None:
            reopened.close()
        service.close()


def proposal(state, work_items, evidence=PLAN_EVIDENCE, attempt="planner"):
    plan = {"summary": "Report" if not work_items else "Plan", "use_team": bool(work_items), "work_items": work_items}
    return {"state": state, "attempt_id": attempt, "decision_evidence": evidence,
            "payload_json": json.dumps({"plan": plan})}


def test_resumed_state_comes_from_the_retained_plans():
    work = [{"id": "api"}]
    attempts = [{"id": "planner", "worker_id": "coordinator"}]
    def resumed(*proposals, rounds=2):
        return TeamAutopilot.resumed(None, "run", {"rounds": rounds},
                                     {"coordinator_proposals": list(proposals), "attempts": attempts})
    first = resumed(proposal("pending", work))
    assert (first.round, first.closing, first.final_report) == (1, False, None)
    second = resumed(proposal("accepted", work), proposal("accepted", work))
    assert (second.round, second.closing, second.final_report) == (2, False, None)
    closing = resumed(proposal("accepted", work), proposal("accepted", work), proposal("pending", work))
    assert closing.closing is True and closing.final_report is None
    reported = resumed(proposal("accepted", work), proposal("rejected", work, CLOSING_EVIDENCE), rounds=1)
    assert reported.closing is True and reported.final_report == "Plan"
    early = resumed(proposal("accepted", work), proposal("accepted", []))
    assert early.final_report == "Report" and early.round == 1
