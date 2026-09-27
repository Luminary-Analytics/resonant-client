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
from lumi.engine.swarming.models import Conflict
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


def start_readers(service, capture, *, tasks=1, request_limit=4):
    return service.operate(capture, {"request_id": "readers", "action": "start", "objective": OBJECTIVE,
        "tasks": [{"objective": f"Read the notes ({index})", "read_roots": ["."]} for index in range(tasks)],
        "request_limit": request_limit, "max_workers": tasks})["run"]["run"]["id"]


def start_orchestrated(service, capture, *, rounds=2, autonomy=True):
    return service.operate(capture, {"request_id": "orchestrated", "action": "start", "objective": OBJECTIVE,
        "plan_mode": "coordinator", "tasks": None, "request_limit": 20, "max_workers": 2,
        "coordinator_requests": 3, "worker_requests": 3,
        **({"autonomy": {"rounds": rounds}} if autonomy else {})})["run"]["run"]["id"]


def view(service, capture, run_id):
    return service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})


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


def test_an_organization_can_keep_the_preview_off(team):
    # A locked setting wins over the person's switch (SettingsManager.get).
    service, capture, outputs, backends = team
    install(settings={"swarming.enabled": False})
    assert service.operate(capture, {"request_id": "look"})["enabled"] is False
    with pytest.raises(Conflict, match="Enable the swarming preview"):
        start_readers(service, capture)
    assert not backends


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


# ── Modes ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("modes, reader, writers, applier", [
    (["plan"], "", "neither Auto-edit nor Full-auto", "doesn't allow Full-auto"),
    (["ask"], "", "neither Auto-edit nor Full-auto", "doesn't allow Full-auto"),
    (["ask", "auto-edit"], "", "", "doesn't allow Full-auto"),
    (["bypass"], "", "", ""),
    (None, "", "", ""),
])
def test_team_features_map_to_permission_modes(modes, reader, writers, applier):
    install(**({"permissions": {"allowed_modes": modes}} if modes else {}))
    for refusal, expected in ((mode_refusal(writers=False, applies=False), reader),
                              (mode_refusal(writers=True, applies=False), writers),
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
    common = {"run_id": "run", "project": str(tmp_path), "provider": "ollama", "model": "chosen"}
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
                                provider="ollama", model="chosen")
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


def test_a_check_refused_by_a_policy_that_arrives_mid_run(tmp_path):
    # Writers ran with no policy; then one arrives that needs a second person
    # for the team's check. The owner can't run it any more.
    from tests.test_swarm_desktop_writers import operate, request, settled, view as writers_view
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
        until(lambda: len(writers_view(desktop, run_id)["submissions"]) == 1, timeout=15)
        writer_id = writers_view(desktop, run_id)["writer_worktrees"][0]["id"]
        operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer_id])
        candidate = settled(desktop, run_id)["integration_candidates"][0]
        install(approvals={"commands": [f"{sys.executable}*"]})
        with pytest.raises(Conflict, match="second person's approval"):
            operate(desktop, run_id, "run_check", "verify", candidate_id=candidate["id"], check_key="value-check")
        assert writers_view(desktop, run_id)["integration_checks"] == []
    finally:
        service.close()


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
