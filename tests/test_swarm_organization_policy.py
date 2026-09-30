"""A personal team under an organization policy: models, modes, checks, budgets, usage and audit.

Every policy here is installed with ``policy.set_for_tests``; the machine's is
never read. Usage records and the audit log live in each test's folder.
"""

import json
import sys
import time

import pytest

from lumi import audit, usage
from lumi import policy as lumi_policy
from lumi.audit import AuditLog
from lumi.engine.swarming import Scope
from lumi.engine.swarming.autopilot import PLAN_EVIDENCE
from lumi.engine.swarming.models import Conflict, RevisionConflict
from lumi.engine.swarming.organization import (TeamGovernance, check_refusal, mode_refusal, model_refusal,
                                               sandbox_refusal)
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime, policy_refusal
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from lumi.policy import parse
from lumi.usage import UsageLedger
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_coordinator import response as plan_response
from tests.test_swarm_workers import assign as assign_reader, fixture as reader_fixture, until
from tests.test_swarm_writers import git

read_setup = reader_fixture

OBJECTIVE = "Check how input is handled in the payments service"
FINAL = {"summary": "Both areas were inspected: the API validates input and the UI escapes it.",
         "use_team": False, "work_items": []}
# Organization prices make the local fixture model cost money: 10 input and 20
# output tokens (the stub's done()) come to $3.00 per request.
PRICES = {"prices": {"ollama:chosen": {"input": 100000, "output": 100000}}}


def install(**sections):
    """Install Acme's policy for this test; conftest removes it afterwards."""
    policy = parse({"schema": "lumi.policy/v1", "organization": "Acme", **sections}, source="test policy")
    lumi_policy.set_for_tests(policy)
    return policy


@pytest.fixture
def records(tmp_path):
    """Usage records and the audit log (default capture level: metadata) in this test's folder."""
    ledger = UsageLedger(tmp_path / "usage")
    usage.set_for_tests(ledger)
    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    return ledger, log


def audit_records(log, kind=None):
    rows = [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if kind is None or row["type"] == kind]


class PolicyArrives(StreamingBackend):
    """Installs a policy while its first request streams, as if IT pushed one mid-run."""

    def __init__(self, sections, **kwargs):
        super().__init__(**kwargs)
        self.sections = sections

    def stream(self, **kwargs):
        for event in super().stream(**kwargs):
            if event[0] == "done" and self.stream_count == 1:
                install(**self.sections)
            yield event


@pytest.fixture
def team(tmp_path):
    """A personal team whose scripted model gives each new participant the next output."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("fixture notes\n", encoding="utf-8")
    outputs, backends = [], []

    def factory(spec):
        output = outputs[len(backends)]
        backend = (output if isinstance(output, StreamingBackend)
                   else StreamingBackend(scripts=output) if isinstance(output, list)
                   else StreamingBackend(events=[text_delta(output), done()]))
        backend.name, backend.model = spec.backend_type, spec.model
        backends.append(backend)
        return backend

    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "conversation-7"), str(workspace),
                              BackendSpec("ollama", "chosen"))
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    yield service, capture, outputs, backends
    service.close()


def start_readers(service, capture, *, tasks=1, request_limit=4, request_id="readers", **extra):
    return service.operate(capture, {"request_id": request_id, "action": "start", "objective": OBJECTIVE,
        "tasks": [{"objective": f"Read the notes ({index})", "read_roots": ["."]} for index in range(tasks)],
        "request_limit": request_limit, "max_workers": tasks, **extra})["run"]["run"]["id"]


def start_orchestrated(service, capture, *, rounds=2, autonomy=True, **extra):
    return service.operate(capture, {"request_id": "orchestrated", "action": "start", "objective": OBJECTIVE,
        "plan_mode": "coordinator", "tasks": None, "request_limit": 20, "max_workers": 2,
        "coordinator_requests": 3, "worker_requests": 3,
        **({"autonomy": {"rounds": rounds}} if autonomy else {}), **extra})["run"]["run"]["id"]


def view(service, capture, run_id):
    return service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})


def act(service, capture, run_id, action, **payload):
    """One owner action on the team, at its current revision.

    The team's own work can advance the revision between the view and the
    action; the action is then refused before anything commits, and the owner
    refreshes and sends it again (see test_swarm_desktop_writers.operate).
    """
    request_id = f"{action}-{time.monotonic()}"
    for attempt in range(3):
        revision = view(service, capture, run_id)["run"]["run"]["revision"]
        try:
            return service.operate(capture, {"action": action, "request_id": request_id,
                                             "run_id": run_id, "expected_revision": revision, **payload})
        except RevisionConflict:
            if attempt == 2:
                raise


def handed_back(service, capture, run_id):
    """The run once the orchestrator loop has handed it back, with its plan and turn settled."""
    until(lambda: view(service, capture, run_id)["autonomy"]["phase"] == "needs_owner")
    until(lambda: (lambda value: value["run"]["coordinator_proposals"] and all(
        row["process_state"] == "stopped" for row in value["run"]["attempts"]))(view(service, capture, run_id)))
    time.sleep(1.2)  # Several loop passes after the plan is ready: nothing new starts.
    return view(service, capture, run_id)


def settled_worker(service, capture, run_id):
    """The run once its (only) worker attempt has stopped."""
    return until(lambda: (lambda current: current if any(
        row["kind"] == "worker" and row["process_state"] == "stopped" for row in current["run"]["attempts"])
        and not any(row["alive"] for row in current["run"]["workers"]) else None)(view(service, capture, run_id)))


# ── Which actions a policy allows ──────────────────────────────────────────


def test_a_personal_team_runs_under_a_policy_and_sharing_stays_refused():
    install(permissions={"allowed_modes": ["ask", "auto-edit", "bypass"]})
    for action in ("start", "request_plan", "resume", "retry_work", "run_check", "apply_candidate",
                   "prepare_candidate", "decide_proposal", "continue_recovered", "configure", "complete",
                   "review_read_result", "accept_writer", "recover"):
        assert policy_refusal(action, personal=True, team=lambda: "") == "", action
    # The team's rules decide the actions that start work, and only those.
    assert policy_refusal("run_check", personal=True, team=lambda: "Refused by the team's rules") == (
        "Refused by the team's rules")
    assert policy_refusal("complete", personal=True, team=lambda: "Refused by the team's rules") == ""
    for action in ("collaboration_prepare", "collaboration_offer", "collaboration_send", "collaboration_accept_work",
                   "managed_sharing_prepare", "managed_sharing_offer", "managed_sharing_accept_work", "unknown"):
        refusal = policy_refusal(action, personal=True, team=lambda: "")
        assert "Acme's policy applies on this computer" in refusal and "sharing" in refusal, action
    # Organization-managed teams: unchanged, only reading, stopping and recovery.
    assert "Acme's policy applies on this computer" in policy_refusal("start", team=lambda: "")
    assert policy_refusal("stop") == policy_refusal("collaboration_revoke", personal=True) == ""
    # Taking over an expired team starts nothing, so it stays available to
    # every team, under an invalid policy too; continuing it is governed.
    lumi_policy.set_for_tests(None, error="The organization policy at X is invalid: bad JSON")
    assert policy_refusal("recover") == policy_refusal("recover", personal=True) == ""
    assert "administrator" in policy_refusal("continue_recovered", personal=True, team=lambda: "")


def test_an_organization_can_keep_the_preview_off(team):
    # A locked setting wins over the person's switch (SettingsManager.get).
    service, capture, outputs, backends = team
    install(settings={"swarming.enabled": False})
    current = service.operate(capture, {"request_id": "look"})
    assert current["enabled"] is False and current["available"] is False
    assert "turns the Team preview off" in current["message"]
    with pytest.raises(Conflict, match="turns the Team preview off"):
        start_readers(service, capture)
    assert not backends


def test_a_policy_locking_the_preview_off_mid_run_stops_the_orchestrated_team(team, records):
    # The lock arrives while the orchestrator writes its plan: the loop hands
    # the team back and nothing more starts, from the loop or the owner.
    service, capture, outputs, backends = team
    _, log = records
    outputs.append(PolicyArrives({"settings": {"swarming.enabled": False}},
                                 events=[text_delta(plan_response()), done()]))
    run_id = start_orchestrated(service, capture)
    current = handed_back(service, capture, run_id)
    assert "turns the Team preview off" in current["autonomy"]["detail"]
    assert len(backends) == 1 and [row["state"] for row in current["run"]["coordinator_proposals"]] == ["pending"]
    proposal = current["run"]["coordinator_proposals"][0]
    with pytest.raises(Conflict, match="turns the Team preview off"):
        act(service, capture, run_id, "decide_proposal", proposal_id=proposal["id"], sha256=proposal["sha256"],
            accept=True, evidence="Owner accepted the plan")
    with pytest.raises(Conflict, match="turns the Team preview off"):
        act(service, capture, run_id, "request_plan", coordinator_requests=2, read_roots=["."])
    # Rejecting the plan, reading and stopping stay available.
    act(service, capture, run_id, "decide_proposal", proposal_id=proposal["id"], sha256=proposal["sha256"],
        accept=False, evidence="Owner declined it")
    assert act(service, capture, run_id, "stop")["run"]["run"]["state"] in {"stopping", "cancelled"}
    assert len(backends) == 1
    assert any(row["data"]["decision"] == "hand_back" for row in audit_records(log, "team.decision"))


def test_the_preview_lock_refuses_the_owners_actions_that_start_work(team):
    # The lock arrives while a reader works: its next request is refused, and
    # the owner can't retry it or resume the team, only pause, look and stop.
    service, capture, outputs, backends = team
    outputs.append(PolicyArrives({"settings": {"swarming.enabled": False}}, scripts=[
        [tool_call("file_read", {"path": "notes.txt"}), done()], [text_delta("Read the notes."), done()]]))
    run_id = start_readers(service, capture)
    run = settled_worker(service, capture, run_id)["run"]
    assert [row["state"] for row in run["attempts"]] == ["failed"]
    assert "turns the Team preview off" in run["workers"][0]["error"]
    with pytest.raises(Conflict, match="turns the Team preview off"):
        act(service, capture, run_id, "retry_work", work_item_id=run["work_items"][0]["id"], evidence="Retry it")
    act(service, capture, run_id, "pause")
    with pytest.raises(Conflict, match="turns the Team preview off"):
        act(service, capture, run_id, "resume")
    act(service, capture, run_id, "stop")
    assert len(backends) == 1 and [row["kind"] for row in view(service, capture, run_id)["run"]["attempts"]] == ["worker"]


@pytest.mark.parametrize("arriving", ["model", "lock", "invalid"])
def test_taking_over_an_expired_team_stays_available_and_continuing_it_is_governed(team, arriving):
    # Taking over fences the old owner and starts nothing, whatever the policy;
    # continuing starts workers, so the rules in force decide.
    service, capture, outputs, backends = team
    now = [time.time()]
    service._store(capture).clock = lambda: now[0]
    outputs.append("The notes say hi.")
    run_id = start_readers(service, capture)
    settled_worker(service, capture, run_id)
    old = service._runners[run_id][1]
    old._maintenance_stop.set()
    old._maintenance.join(timeout=1)
    now[0] += 61  # The old owner's lease has expired.
    reopened = SwarmRuntime(service.settings, backend_factory=lambda _: pytest.fail("Nothing may start"),
                            state_root=service._state_root)
    try:
        reopened._store(capture).clock = lambda: now[0]
        if arriving == "model":
            install(models={"allowed": ["anthropic:*"]})
            refusal = "doesn't allow chosen on ollama"
        elif arriving == "lock":
            install(settings={"swarming.enabled": False})
            refusal = "turns the Team preview off"
        else:
            lumi_policy.set_for_tests(None, error="The organization policy at X is invalid: bad JSON")
            refusal = "administrator"
        owned = act(reopened, capture, run_id, "recover")["run"]
        assert owned["recovery"]["owns_lease"] is True
        with pytest.raises(Conflict, match=refusal):
            act(reopened, capture, run_id, "continue_recovered", retry_work_items=[], worker_requests=4)
        assert run_id not in reopened._runners
    finally:
        reopened.close()


def test_sharing_is_refused_by_the_runtime_under_a_policy(team):
    service, capture, outputs, backends = team
    install()
    with pytest.raises(Conflict, match="sharing with another conversation"):
        service.operate(capture, {"action": "collaboration_prepare", "request_id": "share",
                                  "objective": "Share findings", "request_limit": 4})
    with pytest.raises(Conflict, match="organization-managed teams"):
        service.operate(capture, {"action": "managed_sharing_prepare", "request_id": "managed-share",
                                  "objective": "Share findings", "request_limit": 4})
    assert not backends and not service.busy


def test_a_policy_arriving_stops_a_sharing_teams_new_requests(team):
    # A team prepared to receive shared work before the policy arrived: the
    # policy now refuses its requests and its accepting more work.
    from lumi.engine.swarming import AttemptContext

    service, capture, outputs, backends = team
    run_id = service.operate(capture, {"action": "collaboration_prepare", "request_id": "share",
                                       "objective": "Share findings", "request_limit": 4})["run"]["run"]["id"]
    governance = service.team_governance(run_id)
    assert governance.kind == "sharing" and governance.dispatch_refusal() == ""
    install()
    worker = AttemptContext(capture.scope, run_id, "attempt", "collab_worker_fixture", 1)
    assert "sharing with another conversation" in governance.request_refusal(worker, "primary", ("ollama", "chosen"))
    assert "sharing with another conversation" in governance.dispatch_refusal()
    with pytest.raises(Conflict, match="sharing with another conversation"):
        act(service, capture, run_id, "collaboration_accept_work", message_id="message", read_roots=["."],
            objective="Inspect", requests=2, evidence="Accepted")
    assert not backends


def test_accepting_shared_work_checks_the_budgets_before_its_claim(team, records):
    # With no policy, accepting shared work starts a participant: the budgets
    # (here the person's own daily limit) decide before anything is claimed.
    from lumi import budgets

    service, capture, outputs, backends = team
    ledger, _ = records
    run_id = service.operate(capture, {"action": "collaboration_prepare", "request_id": "share",
                                       "objective": "Share findings", "request_limit": 4})["run"]["run"]["id"]
    ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", stats={"input_tokens": 2_000_000})
    service.settings.set("cost_tracking", "daily_limit_usd", 1)
    budgets.configure(service.settings)  # As the app does when Settings change.
    with pytest.raises(Conflict, match="Continuing needs approval, which a team run can't ask for"):
        act(service, capture, run_id, "collaboration_accept_work", message_id="message", read_roots=["."],
            objective="Inspect", requests=2, evidence="Accepted")
    assert view(service, capture, run_id)["run"]["work_items"] == [] and not backends


# ── Models ─────────────────────────────────────────────────────────────────


def test_model_rules_include_zero_retention():
    install(models={"allowed": ["ollama:*"], "blocked": ["ollama:blocked*"], "require_zero_retention": True})
    assert model_refusal("ollama", "chosen") == ""
    assert "doesn't allow blocked-model on ollama" in model_refusal("ollama", "blocked-model")
    # Ollama's cloud models run on ollama.com, so zero retention refuses them.
    assert "doesn't allow gpt-oss:120b-cloud" in model_refusal("ollama", "gpt-oss:120b-cloud")
    lumi_policy.set_for_tests(None)
    assert model_refusal("ollama", "gpt-oss:120b-cloud") == ""


def test_a_refused_model_is_refused_at_start_and_shown(team, records):
    service, capture, outputs, backends = team
    _, log = records
    install(models={"allowed": ["anthropic:*"]})
    current = service.operate(capture, {"request_id": "look"})
    assert current["available"] is False and "Acme's policy doesn't allow chosen on ollama" in current["message"]
    with pytest.raises(Conflict, match="doesn't allow chosen on ollama"):
        start_readers(service, capture)
    assert not backends and not service.busy
    refusal, = audit_records(log, "team.refusal")
    assert refusal["data"]["action"] == "start" and "text" not in refusal["data"]["reason"]


def test_a_policy_arriving_mid_run_refuses_a_workers_next_request(team, records):
    # The reader's first request runs; the policy arrives while it streams, so
    # its second request is refused before anything is reserved or sent.
    service, capture, outputs, backends = team
    _, log = records
    outputs.append(PolicyArrives({"models": {"allowed": ["anthropic:*"]}}, scripts=[
        [tool_call("file_read", {"path": "notes.txt"}), done()], [text_delta("Read the notes."), done()]]))
    run_id = start_readers(service, capture)
    run = settled_worker(service, capture, run_id)["run"]
    attempt, = [row for row in run["attempts"] if row["kind"] == "worker"]
    assert attempt["state"] == "failed" and backends[0].stream_count == 1
    assert [row["state"] for row in run["model_requests"]] == ["completed"]  # Known, never uncertain.
    worker, = run["workers"]
    assert "Acme's policy doesn't allow chosen on ollama" in worker["error"]
    events = [event for event in view(service, capture, run_id)["events"] if event["kind"] == "request_refused"]
    assert events and "doesn't allow chosen" in events[0]["payload"]["reason"]
    refused, = audit_records(log, "team.request_refused")
    assert refused["data"]["agent"] == f"team:{run_id}:reader-1" and "text" not in refused["data"]["reason"]
    ended, = audit_records(log, "team.participant.end")
    assert (ended["data"]["kind"], ended["data"]["outcome"]) == ("reader", "failed")


def test_a_policy_arriving_mid_run_hands_the_orchestrated_team_back(team, records):
    # The policy arrives while the orchestrator writes its plan: the loop takes
    # no further step, dispatches nobody and tells the owner why.
    service, capture, outputs, backends = team
    _, log = records
    outputs.append(PolicyArrives({"models": {"allowed": ["anthropic:*"]}},
                                 events=[text_delta(plan_response()), done()]))
    run_id = start_orchestrated(service, capture)
    current = until(lambda: (lambda value: value if value["autonomy"]["phase"] == "needs_owner" else None)(
        view(service, capture, run_id)))
    assert "Acme's policy doesn't allow chosen on ollama" in current["autonomy"]["detail"]
    until(lambda: (lambda value: value["run"]["coordinator_proposals"] and all(
        row["process_state"] == "stopped" for row in value["run"]["attempts"]))(view(service, capture, run_id)))
    time.sleep(1.2)  # Several loop passes after the plan is ready: nothing new starts.
    current = view(service, capture, run_id)
    assert len(backends) == 1 and [row["state"] for row in current["run"]["coordinator_proposals"]] == ["pending"]
    assert not current["coordinator_planning"]["available"]
    assert "doesn't allow chosen" in current["coordinator_planning"]["reason"]
    proposal = current["run"]["coordinator_proposals"][0]
    revision = current["run"]["run"]["revision"]
    with pytest.raises(Conflict, match="doesn't allow chosen on ollama"):
        service.operate(capture, {"action": "decide_proposal", "request_id": "owner-accepts", "run_id": run_id,
                                  "expected_revision": revision, "proposal_id": proposal["id"],
                                  "sha256": proposal["sha256"], "accept": True, "evidence": "Owner accepted the plan"})
    # The orchestrator's own entry points refuse too, before committing a turn.
    with pytest.raises(Conflict, match="doesn't allow chosen on ollama"):
        service._request_plan(capture, {"action": "request_plan", "request_id": "follow-up", "run_id": run_id,
                                        "expected_revision": revision, "coordinator_requests": 2, "read_roots": ["."]})
    with pytest.raises(Conflict, match="doesn't allow chosen on ollama"):
        service._answer_workers(capture, run_id=run_id, questions=[1], request_id="answer",
                                expected_revision=revision, requests=1)
    assert [row["kind"] for row in view(service, capture, run_id)["run"]["attempts"]] == ["coordinator"]
    hand_back, = [row for row in audit_records(log, "team.decision") if row["data"]["decision"] == "hand_back"]
    assert hand_back["data"]["by"] == "orchestrator" and "text" not in hand_back["data"]["reason"]


WORKERS = {"provider": "ollama", "model": "small-worker"}


def test_each_model_the_team_runs_passes_the_policy(team):
    # The orchestrator runs the session's model and the workers their own:
    # each must be allowed, and a team of manual tasks runs only its workers'.
    service, capture, outputs, backends = team
    install(models={"blocked": ["ollama:small-worker"]})
    with pytest.raises(Conflict, match="doesn't allow small-worker on ollama"):
        start_orchestrated(service, capture, worker_model=WORKERS)
    install(models={"blocked": ["ollama:chosen"]})
    with pytest.raises(Conflict, match="doesn't allow chosen on ollama"):
        start_orchestrated(service, capture, worker_model=WORKERS)
    assert not backends
    outputs.append("The notes say hi.")
    run_id = start_readers(service, capture, worker_model=WORKERS)
    run = settled_worker(service, capture, run_id)["run"]
    assert [row["state"] for row in run["attempts"]] == ["submitted"]
    assert [backend.model for backend in backends] == ["small-worker"]


def test_a_policy_refusing_the_workers_model_mid_run_stops_their_dispatch(team, records):
    service, capture, outputs, backends = team
    outputs.append(PolicyArrives({"models": {"blocked": ["ollama:small-worker"]}},
                                 events=[text_delta(plan_response()), done()]))
    run_id = start_orchestrated(service, capture, worker_model=WORKERS)
    current = handed_back(service, capture, run_id)
    assert "doesn't allow small-worker on ollama" in current["autonomy"]["detail"]
    assert [backend.model for backend in backends] == ["chosen"]


def test_each_request_is_priced_and_recorded_under_its_own_model(team, records):
    # The orchestrator's and the workers' requests cost what their own models do.
    service, capture, outputs, backends = team
    ledger, log = records
    install(pricing={"prices": {"ollama:chosen": {"input": 100000, "output": 100000},
                                "ollama:small-worker": {"input": 10000, "output": 10000}}})
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(FINAL)]
    run_id = start_orchestrated(service, capture, worker_model=WORKERS)
    until(lambda: (lambda value: value if value["run"]["run"]["state"] == "completed"
                   and not value["autonomy"]["active"] else None)(view(service, capture, run_id)), timeout=30)
    costs = sorted((row["model"], round(row["cost_usd"], 6)) for row in ledger.records())
    assert costs == [("chosen", 3.0), ("chosen", 3.0), ("small-worker", 0.3), ("small-worker", 0.3)]
    started = sorted((row["data"]["kind"], row["data"]["model"]) for row in audit_records(log, "team.participant.start"))
    assert started == [("coordinator", "chosen")] * 2 + [("reader", "small-worker")] * 2
    start, = audit_records(log, "team.start")
    assert (start["data"]["model"], start["data"]["worker_model"]) == ("chosen", "small-worker")


# ── Modes ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("modes, reader, writers, orchestrated, applier", [
    (["plan"], "", "neither Auto-edit nor Full-auto", "a team the orchestrator runs", "doesn't allow Full-auto"),
    (["ask"], "", "neither Auto-edit nor Full-auto", "a team the orchestrator runs", "doesn't allow Full-auto"),
    (["ask", "auto-edit"], "", "", "a team the orchestrator runs", "doesn't allow Full-auto"),
    (["bypass"], "", "", "", ""),
    (None, "", "", "", ""),
])
def test_team_features_map_to_permission_modes(modes, reader, writers, orchestrated, applier):
    install(**({"permissions": {"allowed_modes": modes}} if modes else {}))
    for refusal, expected in ((mode_refusal(writers=False, applies=False), reader),
                              (mode_refusal(writers=True, applies=False), writers),
                              # The orchestrator approves plans and accepts results for the owner.
                              (mode_refusal(writers=False, applies=False, orchestrated=True), orchestrated),
                              (mode_refusal(writers=True, applies=True), applier)):
        if expected:
            assert expected in refusal
        else:
            assert refusal == ""


def test_a_writer_team_is_refused_before_any_team_state_where_writers_arent_allowed(team):
    service, capture, outputs, backends = team
    install(permissions={"allowed_modes": ["ask", "plan"]})
    check = {"key": "value-check", "argv": [sys.executable, "-c", "print('ok')"], "timeout_seconds": 10}
    with pytest.raises(Conflict, match="neither Auto-edit nor Full-auto"):
        service.operate(capture, {"action": "start", "request_id": "writers", "objective": OBJECTIVE,
            "write_roots": ["."], "checks": [check], "request_limit": 6, "worker_requests": 3, "max_workers": 1,
            "tasks": [{"objective": "Update notes.txt", "role": "implement", "read_roots": ["."],
                       "write_roots": ["."], "criteria": ["value-check"]}]})
    install(permissions={"allowed_modes": ["ask", "auto-edit"]})
    with pytest.raises(Conflict, match="doesn't allow Full-auto"):
        service.operate(capture, {"action": "start", "request_id": "applier", "objective": OBJECTIVE,
            "plan_mode": "coordinator", "tasks": None, "write_roots": ["."], "checks": [check],
            "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3,
            "autonomy": {"rounds": 1, "apply": True}})
    assert not backends and not service.busy
    assert service.operate(capture, {"request_id": "look"})["run"] is None


def test_a_team_the_orchestrator_runs_is_refused_where_full_auto_isnt_allowed(team):
    # Even one that only reads and reports: the orchestrator decides for the
    # owner, which needs Full-auto (the desktop's AppState.full_auto_needed
    # leaves this refusal to the team's rules). A team the owner reviews runs.
    service, capture, outputs, backends = team
    install(permissions={"allowed_modes": ["ask", "auto-edit"]})
    with pytest.raises(Conflict, match="doesn't allow Full-auto, which a team the orchestrator runs needs"):
        start_orchestrated(service, capture)
    assert not backends and not service.busy
    assert service.operate(capture, {"request_id": "look"})["run"] is None


def test_a_read_only_team_runs_under_a_read_only_policy(team, records):
    service, capture, outputs, backends = team
    ledger, _ = records
    install(permissions={"allowed_modes": ["plan"]})
    outputs.append([[tool_call("file_read", {"path": "notes.txt"}), done()], [text_delta("The notes say hi."), done()]])
    run_id = start_readers(service, capture)
    run = settled_worker(service, capture, run_id)["run"]
    assert [row["state"] for row in run["attempts"]] == ["submitted"]
    assert [row["state"] for row in run["model_requests"]] == ["completed", "completed"]
    # Each request is recorded once, by the host, as the team's: never also as a turn.
    rows = ledger.records()
    assert [(row["purpose"], row["session"], row["project"], row["agent"], row["input_tokens"]) for row in rows] == [
        ("team", "conversation-7", capture.workspace, f"team:{run_id}:reader-1", 10)] * 2


# ── Checks ─────────────────────────────────────────────────────────────────


def test_checks_pass_the_guardrails_and_the_floor_policy_or_not(tmp_path):
    lumi_policy.set_for_tests(None)
    assert "never allowed" in check_refusal([{"key": "wipe", "argv": ["rm", "-rf", "/"]}],
                                            project=str(tmp_path), settings=None)
    # A shell carries a whole command in one argument; each word is checked too.
    assert "never allowed" in check_refusal([{"key": "wipe", "argv": ["sh", "-c", "rm -rf ~"]}],
                                            project=str(tmp_path), settings=None)
    assert "protected branch" in check_refusal([{"key": "push", "argv": ["git", "push", "--force", "origin", "main"]}],
                                               project=str(tmp_path), settings=None)
    assert check_refusal([{"key": "tests", "argv": [sys.executable, "-m", "pytest", "-q"]}],
                         project=str(tmp_path), settings=None) == ""


def test_checks_follow_the_organizations_shell_rules_and_approvals(tmp_path):
    install(shell={"rules": [
        {"tool_pattern": "bash", "action": "deny", "arg_patterns": {"command": "\\bcurl\\b"},
         "reason": "No downloads from the agent's shell"},
        {"tool_pattern": "check_run", "action": "prompt", "arg_patterns": {"command": "deploy"},
         "reason": "Deployments need a person"}]},
        approvals={"commands": ["npm publish*"]})
    refuse = lambda *argv: check_refusal([{"key": "check", "argv": list(argv)}],  # noqa: E731
                                         project=str(tmp_path), settings=None)
    assert "No downloads from the agent's shell" in refuse("curl", "https://example.com/install.sh")
    assert "ask a person" in refuse("python", "deploy.py") and "Deployments need a person" in refuse("python", "deploy.py")
    assert "second person's approval" in refuse("npm", "publish", "--dry-run")
    assert refuse("npm", "test") == ""


def test_the_organizations_rules_see_a_command_a_launcher_carries(tmp_path):
    # A launcher carries a command in one argument, or starts one partway through.
    install(shell={"rules": [{"tool_pattern": "bash", "action": "deny", "arg_patterns": {"command": "^\\s*curl\\b"},
                              "reason": "No downloads from the agent's shell"}]},
            approvals={"commands": ["npm publish*"]})
    refuse = lambda *argv: check_refusal([{"key": "check", "argv": list(argv)}],  # noqa: E731
                                         project=str(tmp_path), settings=None)
    for argv in (("cmd", "/c", "npm publish"), ("sh", "-c", "npm publish"), ("cmd", "/c", "npm", "publish")):
        assert "second person's approval" in refuse(*argv), argv
    for argv in (("cmd", "/c", "curl https://example.com/x.sh"), ("cmd", "/c", "curl", "https://example.com/x.sh")):
        assert "No downloads from the agent's shell" in refuse(*argv), argv
    # The guardrails see a command that starts partway through too.
    lumi_policy.set_for_tests(None)
    assert "never allowed" in refuse("cmd", "/c", "rm", "-rf", "/")
    assert refuse("cmd", "/c", "npm", "publish") == ""  # Control: no policy, no approvals.


def test_a_rule_without_argument_patterns_decides_only_the_whole_command(tmp_path):
    # "Ask about everything but npm test": the catch-all mustn't refuse "npm"
    # alone in a command the organization allowed as a whole.
    install(shell={"rules": [{"tool_pattern": "*", "action": "allow", "arg_globs": {"command": "npm test*"}},
                             {"tool_pattern": "*", "action": "prompt", "reason": "Ask before commands"}]})
    refuse = lambda *argv: check_refusal([{"key": "check", "argv": list(argv)}],  # noqa: E731
                                         project=str(tmp_path), settings=None)
    assert refuse("npm", "test", "--", "--ci") == ""
    assert "Ask before commands" in refuse("npm", "run", "deploy")


def test_a_check_that_needs_approval_refuses_the_team_before_any_team_state(team):
    service, capture, outputs, backends = team
    install(approvals={"commands": ["npm publish*"]})
    with pytest.raises(Conflict, match="second person's approval"):
        service.operate(capture, {"action": "start", "request_id": "writers", "objective": OBJECTIVE,
            "write_roots": ["."], "request_limit": 6, "worker_requests": 3, "max_workers": 1,
            "checks": [{"key": "publish", "argv": ["npm", "publish"], "timeout_seconds": 10}],
            "tasks": [{"objective": "Update notes.txt", "role": "implement", "read_roots": ["."],
                       "write_roots": ["."], "criteria": ["publish"]}]})
    assert not backends and service.operate(capture, {"request_id": "look"})["run"] is None


def test_the_shell_sandbox_refuses_writer_teams_only(tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    assert sandbox_refusal(settings) == ""
    settings.set("security", "shell_sandbox", "project")
    assert "shell sandbox is on" in sandbox_refusal(settings)
    common = {"run_id": "run", "project": str(tmp_path), "models": [("ollama", "chosen")]}
    assert "shell sandbox is on" in TeamGovernance(settings, writers=True, **common).refusal()
    assert TeamGovernance(settings, writers=False, **common).refusal() == ""


# ── Budgets and usage ──────────────────────────────────────────────────────


def test_a_spent_budget_refuses_the_team_at_start(team, records):
    service, capture, outputs, backends = team
    ledger, log = records
    ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", stats={"input_tokens": 2_000_000})
    install(budgets=[{"scope": "user", "period": "month", "block_usd": 1}])
    with pytest.raises(Conflict, match="reaches Acme's \\$1.00 budget"):
        start_readers(service, capture)
    assert not backends and not service.busy
    assert audit_records(log, "budget.block") and audit_records(log, "team.refusal")


def test_a_limit_that_asks_stops_a_team_unless_the_person_approved_it(team, records):
    # Nobody can answer during a team run; an approval given in a chat this
    # period counts, as it does for the next chat turn.
    from lumi import budgets

    service, capture, outputs, backends = team
    ledger, log = records
    ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", stats={"input_tokens": 2_000_000})
    install(budgets=[{"scope": "user", "period": "month", "approve_usd": 1}])
    with pytest.raises(Conflict, match="past Acme's \\$1.00 limit. Continuing needs approval, which a team run"):
        start_readers(service, capture)
    approval, = audit_records(log, "budget.approval")
    assert approval["data"]["decision"] == "unavailable" and not backends
    for verdict in budgets.evaluate(capture.workspace):
        budgets.approve(verdict, "a chat turn")
    outputs.append("The notes say hi.")
    run_id = start_readers(service, capture)
    assert [row["state"] for row in settled_worker(service, capture, run_id)["run"]["attempts"]] == ["submitted"]


def test_the_budget_is_checked_before_each_request_and_counts_the_whole_team_run(team, records):
    # A turn budget counts the team run: its first request costs $3.00, so the
    # second is refused before its allowance is reserved. That outcome is known.
    service, capture, outputs, backends = team
    ledger, log = records
    install(pricing=PRICES, budgets=[{"scope": "turn", "block_usd": 2.5}])
    outputs.append([[tool_call("file_read", {"path": "notes.txt"}), done()], [text_delta("Read it."), done()]])
    run_id = start_readers(service, capture)
    run = settled_worker(service, capture, run_id)["run"]
    assert [row["state"] for row in run["attempts"]] == ["failed"] and backends[0].stream_count == 1
    assert [row["state"] for row in run["model_requests"]] == ["completed"]
    assert "This team has spent $3.00, which reaches Acme's $2.50 budget" in run["workers"][0]["error"]
    [row] = ledger.records()
    assert (row["purpose"], row["cost_usd"], row["price_source"], row["agent"]) == (
        "team", pytest.approx(3.0), "organization", f"team:{run_id}:reader-1")
    block, = audit_records(log, "budget.block")
    assert (block["data"]["scope"], block["data"]["agent"]) == ("turn", f"team:{run_id}:reader-1")
    usage_audit, = audit_records(log, "model.usage")
    assert (usage_audit["data"]["purpose"], usage_audit["data"]["cost_usd"]) == ("team", pytest.approx(3.0))


def test_a_spent_budget_stops_new_dispatch_with_its_reason(team, records):
    # The owner reviews the plan; meanwhile the month's budget is spent. The
    # accepted tasks wait with the reason, and no worker starts.
    service, capture, outputs, backends = team
    ledger, _ = records
    install(budgets=[{"scope": "user", "period": "month", "block_usd": 1}])
    outputs.append(plan_response())
    run_id = start_orchestrated(service, capture, autonomy=False)
    current = until(lambda: (lambda value: value if value["run"]["coordinator_proposals"] and all(
        row["state"] == "completed" and row["process_state"] == "stopped" for row in value["run"]["attempts"])
        else None)(view(service, capture, run_id)))
    ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", stats={"input_tokens": 2_000_000})
    proposal = current["run"]["coordinator_proposals"][0]
    service.operate(capture, {"action": "decide_proposal", "request_id": "accept", "run_id": run_id,
                              "expected_revision": current["run"]["run"]["revision"], "proposal_id": proposal["id"],
                              "sha256": proposal["sha256"], "accept": True, "evidence": "Owner accepted the plan"})
    blocked = until(lambda: view(service, capture, run_id)["run"]["scheduling"]["blocked"])
    assert len(blocked) == 2 and all("reaches Acme's $1.00 budget" in reason for reason in blocked.values())
    time.sleep(.6)
    assert len(backends) == 1  # Only the orchestrator ran.
    assert all(row["state"] == "ready" for row in view(service, capture, run_id)["run"]["work_items"])


def test_a_process_worker_is_refused_by_the_host_and_its_usage_recorded_there(read_setup, tmp_path, records):
    # The request is admitted and recorded in the host, not the worker's process.
    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    from lumi.engine.swarming.processes import ProcessObservations
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from tests.test_swarm_process_workers import child_script

    ledger, log = records
    supervisor, authority, workspace = read_setup
    context = assign_reader(read_setup)
    install(pricing=PRICES, budgets=[{"scope": "turn", "block_usd": 2.5}])
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()], [text_delta("Read the fact."), done()]])))
''')
    governance = TeamGovernance(None, run_id=authority.run_id, project=str(workspace), session="conversation-7",
                                models=[("ollama", "chosen")])
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True, governance=governance,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=60)
        worker = runner.inspect(context.attempt_id)
        assert worker["state"] == "failed", worker
        assert "This team has spent $3.00, which reaches Acme's $2.50 budget" in worker["error"]
        snapshot = supervisor.store.snapshot(authority.scope, authority.run_id)
        assert [row["state"] for row in snapshot["model_requests"]] == ["completed"]
        [row] = ledger.records()
        assert (row["purpose"], row["session"], row["agent"]) == ("team", "conversation-7", f"team:{authority.run_id}:first")
        assert audit_records(log, "team.request_refused")
    finally:
        runner.close()


def test_a_worker_process_leaves_budgets_to_the_host(read_setup, tmp_path, monkeypatch):
    # The worker's process used to run a chat turn's budget checks itself, with
    # its own policy load and none of the app's approvals, so a limit the person
    # approved in a chat still stopped it. The host's check is the only one.
    from lumi import budgets
    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    from lumi.engine.swarming.processes import ProcessObservations
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from tests.test_swarm_process_workers import child_script

    supervisor, authority, workspace = read_setup
    state = tmp_path / "shared-state"  # The app's and the worker's process's state folder.
    ledger = UsageLedger(state / "usage")
    ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", stats={"input_tokens": 2_000_000})
    usage.set_for_tests(ledger)
    host_log = AuditLog(tmp_path / "host-audit")
    audit.set_for_tests(host_log)
    document = {"schema": "lumi.policy/v1", "organization": "Acme",
                "budgets": [{"scope": "user", "period": "month", "approve_usd": 1}]}
    (tmp_path / "policy.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("LUMI_POLICY_FILE", str(tmp_path / "policy.json"))  # What the worker's process reads.
    monkeypatch.setenv("LUMI_STATE_HOME", str(state))
    lumi_policy.set_for_tests(parse(document, source="test policy"))
    governance = TeamGovernance(None, run_id=authority.run_id, project=str(workspace), session="conversation-7",
                                models=[("ollama", "chosen")])
    assert "Continuing needs approval" in governance.dispatch_refusal()
    for verdict in budgets.evaluate(str(workspace)):
        budgets.approve(verdict, "a chat turn")  # The person approved going past it this month.
    assert governance.dispatch_refusal() == ""
    context = assign_reader(read_setup)
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()], [text_delta("Read the fact."), done()]])))
''')
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True, governance=governance,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=60)
        worker = runner.inspect(context.attempt_id)
        assert worker["state"] == "submitted" and worker["error"] == "", worker
        # Nor did the worker's process write budget records of its own.
        child_log = AuditLog(state / "audit")
        assert not [row for row in audit_records(child_log) if row["type"].startswith("budget.")]
        assert [row["purpose"] for row in ledger.records()] == ["turn", "team", "team"]
    finally:
        runner.close()


class _ScanSettings:
    """Settings with the secret scan on or off, as a policy's lock sets it."""

    def __init__(self, enabled):
        self.enabled = enabled

    def get(self, section, key=None, default=None):
        return self.enabled if (section, key) == ("privacy", "secret_scan") else default


@pytest.mark.parametrize("scan", [True, False], ids=["scan-on", "control"])
def test_a_worker_process_follows_the_secret_scan_setting(read_setup, tmp_path, scan):
    # The app's scan setting (which a policy can lock on) reaches a worker in
    # its own process, so a file's token is removed before the model sees it.
    from lumi import secret_scan
    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    from lumi.engine.swarming.processes import ProcessObservations
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from tests.test_swarm_process_workers import child_script

    supervisor, authority, workspace = read_setup
    (workspace / "config.txt").write_text("token: ghp_" + "A1b2C3d4" * 5 + "\n", encoding="utf-8")
    secret_scan.configure(_ScanSettings(scan))
    context = assign_reader(read_setup)
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("file_read", {"path": "config.txt"}), done()], [text_delta("Read the config."), done()]])))
''')
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=60)
        events = runner.poll()["events"]
        assert runner.inspect(context.attempt_id)["state"] == "submitted", events
        redacted = [event for event in events if event.get("kind") == "secrets_redacted"]
        assert bool(redacted) is scan
        if scan:
            assert "GitHub token" in redacted[0]["message"]
    finally:
        runner.close()


def test_a_worker_process_scans_for_its_own_model_key_and_connection_headers():
    # The worker's process knows its own credentials (not the app's other saved
    # keys), and removes them from what it sends even with the pattern scan off.
    from lumi import secret_scan
    from lumi.engine.swarming.worker_child import _configure_scan

    key, header = "fixture-worker-model-key-0123456789", "Bearer fixture-connection-token-abcdef"
    _configure_scan({"backend": {"api_key": key}, "connection": {"headers": {"Authorization": header}},
                     "secret_scan": False})
    text, found = secret_scan.redact_text(f"key={key}; auth={header}; ghp_{'A1b2C3d4' * 5}")
    assert key not in text and header not in text and found["saved API key"] == 2
    assert "ghp_" in text  # The pattern scan is off.


# ── Audit ──────────────────────────────────────────────────────────────────


def test_a_team_run_is_audited_as_metadata_at_the_default_capture_level(team, records):
    service, capture, outputs, backends = team
    _, log = records
    outputs += [plan_response(), "The API validates input.", "The UI escapes output.", json.dumps(FINAL)]
    run_id = start_orchestrated(service, capture)
    until(lambda: (lambda value: value if value["run"]["run"]["state"] == "completed"
                   and not value["autonomy"]["active"] else None)(view(service, capture, run_id)), timeout=30)
    rows = audit_records(log)
    kinds = [row["type"] for row in rows]
    assert kinds[0] == "team.start" and "team.complete" in kinds
    start, = audit_records(log, "team.start")
    assert (start["session"], start["data"]["run"], start["data"]["provider"], start["data"]["model"]) == (
        "conversation-7", run_id, "ollama", "chosen")
    assert set(start["data"]["objective"]) == {"chars", "sha256"}  # Its digest, never the objective.
    started = [(row["data"]["kind"], row["data"]["model"]) for row in audit_records(log, "team.participant.start")]
    ended = [(row["data"]["kind"], row["data"]["outcome"]) for row in audit_records(log, "team.participant.end")]
    assert sorted(started) == [("coordinator", "chosen")] * 2 + [("reader", "chosen")] * 2
    assert sorted(ended) == [("coordinator", "completed")] * 2 + [("reader", "submitted")] * 2
    decisions = [(row["data"]["decision"], row["data"]["by"]) for row in audit_records(log, "team.decision")]
    assert ("decide_proposal", "orchestrator") in decisions and ("accept_under_grant", "orchestrator") in decisions
    complete, = audit_records(log, "team.complete")
    assert complete["data"]["by"] == "orchestrator"
    assert {row["data"]["purpose"] for row in audit_records(log, "model.usage")} == {"team"}
    # Metadata only: no objective, finding, report or evidence text anywhere.
    text = "\n".join(path.read_text(encoding="utf-8") for path in log._files())
    for content in (OBJECTIVE, "The API validates input.", FINAL["summary"], PLAN_EVIDENCE):
        assert content not in text
    assert all("text" not in value for row in rows for value in row["data"].values() if isinstance(value, dict))


def test_integration_steps_and_the_apply_grant_under_a_full_auto_policy(tmp_path, records):
    # A Full-auto policy allows an orchestrator that applies checked changes;
    # each integration step's outcome is audited.
    from tests.test_swarm_autopilot_writers import CHECK, FINAL as WRITERS_FINAL, finished, start, writer, writer_plan
    _, log = records
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "value.txt").write_text("original\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    outputs, backends = [writer_plan(), writer("verified change\n"), json.dumps(WRITERS_FINAL)], []

    def factory(spec):
        output = outputs[len(backends)]
        backend = (StreamingBackend(scripts=output) if isinstance(output, list)
                   else StreamingBackend(events=[text_delta(output), done()]))
        backend.name, backend.model = spec.backend_type, spec.model
        backends.append(backend)
        return backend

    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "conversation-7"), str(project),
                              BackendSpec("ollama", "chosen"))
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        install(permissions={"allowed_modes": ["bypass"]})
        run_id = start(service, capture, check=CHECK)
        current = finished(service, capture, run_id)
        assert current["run"]["run"]["state"] == "completed", current["autonomy"]
        assert (project / "src" / "value.txt").read_text() == "verified change\n"
        steps = [(row["data"]["step"], row["data"]["outcome"]) for row in audit_records(log, "team.integration")]
        assert steps == [("prepare_candidate", "completed"), ("run_check", "completed"), ("apply", "completed")]
        check, = [row["data"] for row in audit_records(log, "team.integration") if row["data"]["step"] == "run_check"]
        assert (check["check"], check["result"], check["exit_code"]) == ("value-check", "passed", 0)
        kinds = {row["data"]["kind"] for row in audit_records(log, "team.participant.start")}
        assert kinds == {"coordinator", "writer"}
    finally:
        service.close()


@pytest.fixture
def prepared_writers(tmp_path):
    """A writer team, with no policy, whose one writer's change is combined and ready for its check."""
    from tests.test_swarm_desktop_writers import operate, request, settled, submitted, view as writers_view

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "value.txt").write_text("original\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")

    def factory(spec):
        backend = StreamingBackend(model=spec.model, scripts=[
            [tool_call("file_write", {"path": "src/value.txt", "content": "verified change\n"}, "write-1"), done()],
            [text_delta("Changed the isolated file."), done()]])
        backend.name = spec.backend_type
        return backend

    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "conversation-7"), str(project),
                              BackendSpec("ollama", "chosen"))
    desktop = (service, capture, project, [])
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        run_id = service.operate(capture, request())["run"]["run"]["id"]
        submitted(desktop, run_id)  # the same generous, self-describing wait as the writer tests
        writer_id = writers_view(desktop, run_id)["writer_worktrees"][0]["id"]
        operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer_id])
        candidate = settled(desktop, run_id)["integration_candidates"][0]
        assert candidate["state"] == "ready"
        yield desktop, run_id, candidate, operate, settled, writers_view
    finally:
        service.close()


def test_a_check_refused_by_a_policy_that_arrives_mid_run(prepared_writers):
    # Then a policy arrives that needs a second person for the team's check.
    desktop, run_id, candidate, operate, _, writers_view = prepared_writers
    install(approvals={"commands": [f"{sys.executable}*"]})
    with pytest.raises(Conflict, match="second person's approval"):
        operate(desktop, run_id, "run_check", "verify", candidate_id=candidate["id"], check_key="value-check")
    assert writers_view(desktop, run_id)["integration_checks"] == []


def test_the_shell_sandbox_and_a_policy_arriving_mid_run_stop_checks_and_applying(prepared_writers):
    desktop, run_id, candidate, operate, settled, writers_view = prepared_writers
    service, _, project, _ = desktop
    base = git(project, "rev-parse", "HEAD")
    # The person turns the shell sandbox on: the team's checks can't run in it yet.
    service.settings.set("security", "shell_sandbox", "project")
    with pytest.raises(Conflict, match="shell sandbox is on"):
        operate(desktop, run_id, "run_check", "sandboxed", candidate_id=candidate["id"], check_key="value-check")
    assert "shell sandbox is on" in service.team_dispatch_refusal(run_id)
    assert writers_view(desktop, run_id)["integration_checks"] == []
    service.settings.set("security", "shell_sandbox", "off")
    operate(desktop, run_id, "run_check", "verify", candidate_id=candidate["id"], check_key="value-check")
    assert settled(desktop, run_id)["integration_candidates"][0]["state"] == "verified"
    # A policy whose modes don't allow writers arrives before the owner applies.
    install(permissions={"allowed_modes": ["ask"]})
    with pytest.raises(Conflict, match="neither Auto-edit nor Full-auto"):
        operate(desktop, run_id, "apply_candidate", "apply", candidate_id=candidate["id"],
                expected_base=candidate["base_revision"], target_revision=candidate["result_revision"],
                evidence="Owner reviewed the checked change")
    assert git(project, "rev-parse", "HEAD") == base
    assert (project / "src" / "value.txt").read_text() == "original\n"


def test_a_request_refusal_is_a_known_outcome_for_the_session(tmp_path):
    # Without the Team: the execution boundary treats a refused request as
    # nothing sent, so no uncertain request is recorded.
    from lumi.engine.execution_guard import ExecutionBoundary, RequestRefused

    class Refusing:
        artifact_reader = None
        write_tools = frozenset()

        def __init__(self):
            self.ended = []

        def begin_request(self, **_):
            raise RequestRefused("Stopped: the fixture budget is spent.")

        def end_request(self, *args, **kwargs):
            self.ended.append((args, kwargs))

    guard = Refusing()
    boundary = ExecutionBoundary(guard)
    backend = StreamingBackend(events=[text_delta("never"), done()])
    with pytest.raises(RequestRefused, match="fixture budget"):
        list(boundary.stream(backend, purpose="primary", inputs={"user_msg": "x"}, invoke=lambda: backend.stream(
            user_msg="x", conversation_history=[], instructions="", tools=[], max_tokens=10)))
    # Nothing was sent and nothing is left uncertain; the boundary admits no more.
    assert guard.ended == [] and backend.stream_count == 0 and boundary.closed
