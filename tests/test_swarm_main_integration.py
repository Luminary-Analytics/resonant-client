"""The Team preview on main: organization policy gate and project file exclusions."""

import json
import time
import uuid
from pathlib import Path

import pytest

from lumi import policy as lumi_policy
from lumi.engine.exclusions import ExclusionRules
from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.models import Conflict
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime, policy_refusal
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.tools import SWARM_TOOL_NAMES
from lumi.engine.swarming.worker_child import _validate_initial
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from lumi.policy import parse
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

SECRET = "fixture-secret-value"


@pytest.fixture
def acme():
    policy = parse({"schema": "lumi.policy/v1", "organization": "Acme",
                    "permissions": {"allowed_modes": ["ask", "auto-edit", "bypass"]}}, source="test policy")
    lumi_policy.set_for_tests(policy)
    return policy


def test_new_team_work_is_refused_while_an_organization_policy_applies(acme):
    # Outside a personal team (organization-managed teams, and callers that
    # don't say): nothing new starts. Personal teams: test_swarm_organization_policy.py.
    for action in ("start", "request_plan", "resume", "retry_work", "run_check", "apply_candidate",
                   "collaboration_send", "managed_sharing_prepare", "configure"):
        assert "Acme's policy applies on this computer" in policy_refusal(action), action


def test_reading_stopping_and_recovery_stay_available_under_policy(acme):
    for action in ("view", "events", "history", "read_artifact", "stop", "pause", "cancel_worker",
                   "reconcile_request", "collaboration_revoke", "managed_recovery_inspect"):
        assert policy_refusal(action) == "", action


def test_an_invalid_policy_refuses_new_team_work():
    lumi_policy.set_for_tests(None, error="The organization policy at X is invalid: bad JSON")
    assert "administrator" in policy_refusal("start")
    assert policy_refusal("stop") == ""


def test_no_policy_refuses_nothing():
    lumi_policy.set_for_tests(None)
    assert policy_refusal("start") == ""


def test_the_runtime_refuses_before_touching_team_state(tmp_path):
    # A policy that refuses the conversation's model refuses its team before
    # any team state exists, and sharing stays refused under any policy.
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                     "models": {"allowed": ["anthropic:*"]}}, source="test policy"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = SwarmRuntime({}, backend_factory=lambda spec: pytest.fail("Nothing may start under policy"),
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("owner", "project", "session"), str(workspace),
                              BackendSpec("ollama", "chosen"))
    with pytest.raises(Conflict, match="Acme's policy doesn't allow chosen on ollama"):
        runtime.operate(capture, {"command": "swarm", "action": "start", "project": str(workspace),
                                  "session_id": "session", "request_id": uuid.uuid4().hex})
    with pytest.raises(Conflict, match="Acme's policy applies on this computer"):
        runtime.operate(capture, {"command": "swarm", "action": "collaboration_prepare", "project": str(workspace),
                                  "session_id": "session", "request_id": uuid.uuid4().hex,
                                  "objective": "Share findings", "request_limit": 4})
    assert not (tmp_path / "state").exists()


# ── File exclusions ────────────────────────────────────────────────────────


def _command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                     authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def reader(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Actual isolated fact", encoding="utf-8")
    (workspace / ".env").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
    store = SwarmStore(tmp_path / "runtime" / "state.sqlite")
    supervisor = SwarmSupervisor(store)
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect isolated facts", request_limit=20,
        policy=PolicyProfile(1, tools, frozenset({"ollama"})), lease_seconds=300)
    _command(supervisor, authority, "plan", {"work_items": [
        {"id": "first", "objective": "Inspect facts", "read_roots": ["."], "write_roots": [],
         "tools": sorted(tools), "criteria": ["fact"]}]})
    result = _command(supervisor, authority, "assign", {"work_item_id": "first", "worker_id": "first",
        "requests": 3, "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(authority.scope, authority.run_id, result["attempt_id"], "first", authority.epoch)
    return supervisor, authority, workspace, context


class _Recording(StreamingBackend):
    def __init__(self, **kwargs):
        super().__init__(model="chosen", **kwargs)
        self.requests = []

    def stream(self, **kwargs):
        self.requests.append(json.dumps({k: v for k, v in kwargs.items() if k != "cancel_event"}, default=str))
        yield from super().stream(**kwargs)

    def close(self):
        pass


def _run(reader, backend, exclusions):
    supervisor, authority, workspace, context = reader
    runner = SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda spec: backend,
                               exclusions=exclusions)
    try:
        runner.start(context, BackendSpec("ollama", "chosen"))
        deadline = time.monotonic() + 8
        while runner.inspect(context.attempt_id)["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not runner.inspect(context.attempt_id)["alive"]
        return json.dumps(runner.poll()["events"], default=str)
    finally:
        runner.close()


def test_a_worker_cannot_read_a_file_the_project_excludes(reader):
    backend = _Recording(scripts=[[tool_call("file_read", {"path": ".env"}), done()],
                                  [text_delta("Could not read it."), done()]])
    events = _run(reader, backend, ExclusionRules.for_project(str(reader[2]), settings_patterns=[".env"]))
    assert SECRET not in events
    assert all(SECRET not in request for request in backend.requests)
    # The read is refused before it runs and the model hears why, then carries
    # on without the file (the refusal no longer ends the worker).
    refusals = [event for event in json.loads(events) if event.get("event") == "tool.result"
                and str(event.get("output", "")).startswith("Refused before running:")]
    assert refusals and "exclu" in refusals[0]["output"].lower()
    assert len(backend.requests) == 2 and "Could not read it." in events


def test_a_worker_search_leaves_out_excluded_files(reader):
    backend = _Recording(scripts=[[tool_call("grep", {"path": ".", "pattern": "TOKEN|Actual"}), done()],
                                  [text_delta("Searched."), done()]])
    events = _run(reader, backend, ExclusionRules.for_project(str(reader[2]), settings_patterns=[".env"]))
    assert SECRET not in events and all(SECRET not in request for request in backend.requests)


def test_without_rules_the_same_read_succeeds(reader):
    # Control: the fixture really can read .env when nothing excludes it.
    backend = _Recording(scripts=[[tool_call("file_read", {"path": ".env"}), done()],
                                  [text_delta("Read it."), done()]])
    events = _run(reader, backend, None)
    assert SECRET in events


def test_the_child_contract_carries_bounded_rule_pairs(tmp_path):
    base = {"backend": BackendSpec("ollama", "chosen").to_dict(include_sensitive=True),
            "workspace": str(tmp_path), "conversation_key": "swarm:run:attempt", "prompt": "Inspect",
            "instructions": "", "role": "", "request_limit": 3, "tools": ["file_read"],
            "write_tools": [], "exclusions": [[".env", "Settings"], ["*.pem", ".lumiignore"]], "connection": None,
            "secret_scan": True}
    assert _validate_initial(dict(base))["exclusions"] == [[".env", "Settings"], ["*.pem", ".lumiignore"]]
    for bad in ([".env"], [[".env"]], [[".env", ""]], [[".env", 3]], "*.pem", [["x" * 5000, "Settings"]]):
        with pytest.raises(ValueError, match="exclusions"):
            _validate_initial({**base, "exclusions": bad})
    for bad in ("true", 1, None):
        with pytest.raises(ValueError, match="secret scan"):
            _validate_initial({**base, "secret_scan": bad})
    for field in ("exclusions", "secret_scan"):
        missing = dict(base)
        del missing[field]
        with pytest.raises(ValueError):
            _validate_initial(missing)


def test_the_runtime_builds_rules_from_settings_and_the_ignore_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".lumiignore").write_text("secrets/\n", encoding="utf-8")

    class _Settings:
        def get(self, section, key, default=None):
            return [".env"] if (section, key) == ("privacy", "excluded_paths") else default

    runtime = SwarmRuntime(_Settings(), backend_factory=lambda spec: None, state_root=lambda _: tmp_path / "state")
    rules = runtime.exclusions_for(str(project))
    assert rules.match(str(project / ".env")) is not None
    assert rules.match(str(project / "secrets" / "key.txt")) is not None
    assert rules.match(str(project / "fact.txt")) is None
    assert Path(project).exists()


# ── A real child worker process ────────────────────────────────────────────

from lumi.engine.swarming.process_worker import ManagedWorkerProcess  # noqa: E402
from lumi.engine.swarming.processes import ProcessObservations  # noqa: E402
from tests.test_swarm_process_workers import child_script, until  # noqa: E402
from tests.test_swarm_workers import assign as assign_reader, fixture as reader_fixture  # noqa: E402

read_setup = reader_fixture


@pytest.mark.parametrize("excluded", [True, False], ids=["excluded", "control"])
def test_a_child_worker_receives_the_rules_and_its_search_leaves_out_excluded_files(
        read_setup, tmp_path, excluded):
    supervisor, authority, workspace = read_setup
    (workspace / ".env").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
    context = assign_reader(read_setup)
    command = child_script(tmp_path, '''
from lumi.engine.swarming.worker_child import main
from tests.streaming_stub import StreamingBackend, tool_call, text_delta, done
raise SystemExit(main(backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
    scripts=[[tool_call("grep", {"path":".", "pattern":"TOKEN|Actual"}), done()],
             [text_delta("Searched the fixture."), done()]])))
''')
    worker = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"),
        exclusions=ExclusionRules.for_project(str(workspace), settings_patterns=[".env"] if excluded else []))
    try:
        worker.start(context, BackendSpec("ollama", "chosen"))
        until(lambda: not worker.inspect(context.attempt_id)["alive"])
        events = json.dumps(worker.poll()["events"], default=str)
        receipts = json.dumps(supervisor.store.snapshot(authority.scope, authority.run_id)["action_receipts"],
                              default=str)
        assert "Actual isolated fact" in events
        if excluded:
            assert SECRET not in events and SECRET not in receipts
        else:
            # Control: the same child search does reach .env without the rules.
            assert SECRET in events
    finally:
        worker.close()
