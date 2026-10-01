"""Temporary-project proofs of the native Session / durable supervisor bridge."""

from dataclasses import replace
import hashlib
import json
import os

import pytest

from lumi.engine.execution_guard import ExecutionBoundary, ExecutionGuardError
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.execution import SwarmExecutionGuard
from lumi.engine.swarming.mailbox import SwarmMailbox
from lumi.engine.swarming.models import AdmissionClosed, AllowanceExceeded, Conflict, LeaseExpired, ScopeDenied
from lumi.engine.swarming.policy import AssignmentGrant, PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.tools import AGENT_TOOLS
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(f"fixture-{revision}", authority.run_id, revision,
        authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def fixture(tmp_path):
    now = [1000.0]
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "private").mkdir()
    (workspace / "src" / "fact.txt").write_text("Observed fixture fact.", encoding="utf-8")
    (workspace / "private" / "secret.txt").write_text("out of scope", encoding="utf-8")
    store = SwarmStore(tmp_path / "runtime" / "state.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect fixture", request_limit=20,
        policy=PolicyProfile(1, frozenset({"file_read", "glob", "grep", "artifact_read"}),
                             frozenset({"ollama"}), read_roots=("src",)))
    command(supervisor, authority, "plan", {"work_items": [{
        "id": "inspect", "objective": "Inspect fixture", "read_roots": ["src"],
        "write_roots": [], "tools": ["file_read", "glob", "grep", "artifact_read"],
        "criteria": ["fact"]}]})
    assigned = command(supervisor, authority, "assign", {"work_item_id": "inspect",
        "worker_id": "worker", "requests": 15, "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], "worker", authority.epoch)
    command(supervisor, authority, "worker_started", {"attempt_id": context.attempt_id,
                                                     "attempt_epoch": context.epoch})
    guard = SwarmExecutionGuard(supervisor, authority, context, workspace,
                                AssignmentGrant.from_dict(assigned["grant"]))
    return guard, now


def inputs(**extra):
    return {"_model_selection": {"provider": "ollama", "model": "chosen"},
            "user_msg": "Inspect the fixture.", **extra}


def rows(guard, table):
    with guard.store._connection() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def completed_request(guard, *, purpose="primary"):
    request_id = guard.begin_request(purpose=purpose, inputs=inputs())
    guard.end_request(request_id, outcome="completed", usage=None, error="")
    return request_id


@pytest.mark.parametrize("control", ["pause", "stop"])
def test_admitted_tool_observation_is_retained_after_control_without_new_access(fixture, control):
    guard, _ = fixture
    request_id = completed_request(guard)
    arguments = {"path": "src/fact.txt"}
    receipt = guard.begin_tool(request_id, "file-observation", "file_read", arguments, digest(arguments))
    command(guard.supervisor, guard.authority, control)
    assert guard.store.snapshot(guard.context.scope, guard.context.run_id)["run"]["state"] == (
        "pausing" if control == "pause" else "stopping")
    guard.end_tool(receipt, outcome="completed", output="Observed before control", is_error=False, metadata={})
    action = rows(guard, "action_receipts")[0]
    assert action["state"] == "completed"
    artifact = guard.artifacts.inspect(guard.context.scope, guard.context.run_id, action["output_artifact_id"])
    assert artifact.model_request_id == request_id
    with pytest.raises(AdmissionClosed):
        guard.artifact_reader.read_text_page(artifact.id)
    assert guard.store.snapshot(guard.context.scope, guard.context.run_id)["run"]["state"] == (
        "paused" if control == "pause" else "stopping")


def test_tool_observation_requires_exact_admitted_action_and_current_epoch(fixture):
    guard, now = fixture
    request_id = completed_request(guard)
    arguments = {"path": "src/fact.txt"}
    receipt = guard.begin_tool(request_id, "observed", "file_read", arguments, digest(arguments))
    with pytest.raises(ScopeDenied):
        guard.artifacts.publish_tool_observation(guard.context, "fabricated", "text")
    with pytest.raises(ScopeDenied):
        guard.artifacts.publish_tool_observation(replace(guard.context, worker_id="another"), receipt, "text")
    now[0] += 40
    with pytest.raises(LeaseExpired):
        guard.artifacts.publish_tool_observation(guard.context, receipt, "late text")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def backend(events):
    result = StreamingBackend(events=events)
    result.name = "ollama"
    result.model = "chosen"
    return result


def test_session_real_file_read_has_durable_request_action_and_result(fixture):
    guard, _ = fixture
    provider = backend([tool_call("file_read", {"path": "src/fact.txt"}), done()])
    session = Session(provider, execution_guard=guard, max_steps=1, allowed_tools=[
        tool for tool in AGENT_TOOLS if tool["function"]["name"] == "file_read"])
    session.project_path = str(guard.workspace)
    session.sandbox = PathSandbox(str(guard.workspace))
    session.artifact_store = guard.artifact_reader
    events = list(session.run("Read src/fact.txt"))
    observed = [event for event in events if event["event"] == "tool.result"]
    assert observed, events
    assert "Observed fixture fact." in str(observed)
    actions = rows(guard, "action_receipts")
    assert len(actions) == 1 and actions[0]["state"] == "completed"
    requests = rows(guard, "model_requests")
    assert requests[0]["id"] == actions[0]["request_id"]
    assert requests[0]["state"] == "completed" and requests[0]["used"] == 1
    artifact = guard.artifacts.inspect(guard.context.scope, guard.context.run_id,
                                       actions[0]["output_artifact_id"])
    assert artifact.model_request_id == actions[0]["request_id"]
    assert artifact.tool_call_id == actions[0]["call_id"]
    assert "Observed fixture fact." in guard.artifact_reader.read_text_page(artifact.id)
    assert "supervisor_id" not in str(provider.stream_calls)


def test_main_and_auxiliary_requests_retain_unknown_usage_and_exact_input_hashes(fixture):
    guard, _ = fixture
    payload = inputs(user_msg="Unicode: café")
    request_id = guard.begin_request(purpose="primary", inputs=payload)
    assert rows(guard, "request_inputs")[0]["input_sha256"] == digest(payload)
    assert rows(guard, "model_requests")[0]["state"] == "started"
    guard.end_request(request_id, outcome="completed", usage={"input_tokens": 7, "cost": None}, error="")
    auxiliary = completed_request(guard, purpose="planning")
    completed_request(guard, purpose="compression")
    assert [row["purpose"] for row in rows(guard, "model_requests")] == ["main", "auxiliary", "auxiliary"]
    inputs_rows = rows(guard, "request_inputs")
    assert json.loads(inputs_rows[0]["usage_json"]) == {"input_tokens": 7, "cost": None}
    assert inputs_rows[1]["usage_json"] is None
    assert inputs_rows[1]["observation_outcome"] == "completed"
    with pytest.raises(Conflict, match="main request"):
        guard.check_tool(auxiliary, "file_read", {"path": "src/fact.txt"})


def test_optional_input_artifact_is_private_and_matches_hashed_inputs(fixture):
    guard, _ = fixture
    guard.retain_inputs = True
    payload = inputs()
    request_id = guard.begin_request(purpose="primary", inputs=payload)
    recorded = rows(guard, "request_inputs")[0]
    artifact_id = recorded["input_artifact_id"]
    assert artifact_id
    text = guard.artifact_reader.read_text_page(artifact_id)
    assert hashlib.sha256(text.encode()).hexdigest() == recorded["input_sha256"]
    assert json.loads(text) == payload
    assert guard.artifacts.inspect(guard.context.scope, guard.context.run_id, artifact_id).model_request_id == request_id


@pytest.mark.parametrize("selection", [{"provider": "ollama", "model": "other"},
                                       {"provider": "sonn", "model": "chosen"}])
def test_wrong_model_is_denied_before_request_reservation(fixture, selection):
    guard, _ = fixture
    with pytest.raises(ScopeDenied, match="assigned model"):
        guard.begin_request(purpose="primary", inputs=inputs(_model_selection=selection))
    assert rows(guard, "model_requests") == []
    assert guard.closed


def test_stream_interruption_retains_request_uncertainty_and_prevents_retry(fixture):
    guard, _ = fixture
    boundary = ExecutionBoundary(guard)
    provider = backend([])
    iterator = boundary.stream(provider, purpose="primary", inputs={"user_msg": "fixture"},
                                invoke=lambda: iter([text_delta("partial"), done()]))
    next(iterator)
    iterator.close()
    request = rows(guard, "model_requests")[0]
    assert request["state"] == "uncertain" and request["used"] is None
    assert rows(guard, "request_inputs")[0]["usage_json"] is None
    with pytest.raises(ExecutionGuardError):
        guard.begin_request(purpose="primary", inputs=inputs())
    assert len(rows(guard, "model_requests")) == 1


def test_unobserved_tool_intent_blocks_further_requests_after_reopen(fixture):
    guard, _ = fixture
    request_id = completed_request(guard)
    args = {"path": "src/fact.txt"}
    guard.begin_tool(request_id, "call", "file_read", args, digest(args))
    reopened = SwarmExecutionGuard(guard.supervisor, guard.authority, guard.context,
                                    guard.workspace, guard.grant)
    with pytest.raises(Conflict, match="Unresolved"):
        reopened.begin_request(purpose="primary", inputs=inputs())
    assert rows(guard, "action_receipts")[0]["state"] == "admitted"


@pytest.mark.parametrize("name,args", [
    ("file_read", {"path": "private/secret.txt"}),
    ("file_read", {"path": "src/../private/secret.txt"}),
    ("file_read", {"path": "src2/file.txt"}),
    ("file_write", {"path": "src/fact.txt", "content": "mutate"}),
    ("glob", {"path": "src", "pattern": "../private/*"}),
    ("grep", {"path": "src", "pattern": "secret", "glob": "../private/*"}),
    ("file_read", {"path": "src/fact.txt", "owner_id": "other"}),
])
def test_file_and_actor_scope_escalations_fail_before_action_admission(fixture, name, args):
    guard, _ = fixture
    request_id = completed_request(guard)
    with pytest.raises((ScopeDenied, ValueError)):
        guard.begin_tool(request_id, "call", name, args, digest(args))
    assert rows(guard, "action_receipts") == []


def test_absolute_path_inside_scope_is_allowed_but_external_path_is_denied(fixture):
    guard, _ = fixture
    request_id = completed_request(guard)
    guard.check_tool(request_id, "file_read", {"path": str(guard.workspace / "src" / "fact.txt")})
    with pytest.raises(ScopeDenied):
        guard.check_tool(request_id, "file_read", {"path": str(guard.workspace.parent / "outside.txt")})


def test_real_symlink_escape_is_denied(fixture):
    guard, _ = fixture
    link = guard.workspace / "src" / "link.txt"
    try:
        link.symlink_to(guard.workspace / "private" / "secret.txt")
    except OSError as exc:
        pytest.skip(f"Platform does not permit fixture symlink creation: {exc}")
    request_id = completed_request(guard)
    with pytest.raises(ScopeDenied, match="link resolves"):
        guard.check_tool(request_id, "file_read", {"path": "src/link.txt"})


@pytest.mark.skipif(os.name == "nt", reason="Backslashes are actual Windows path separators")
def test_posix_literal_backslash_filename_cannot_bypass_scope(fixture):
    guard, _ = fixture
    (guard.workspace / "src\\outside.txt").write_text("outside the src directory")
    request_id = completed_request(guard)
    with pytest.raises(ScopeDenied, match="POSIX"):
        guard.check_tool(request_id, "file_read", {"path": "src\\outside.txt"})


def test_stop_closes_admission_but_allows_request_observation(fixture):
    guard, _ = fixture
    request_id = guard.begin_request(purpose="primary", inputs=inputs())
    command(guard.supervisor, guard.authority, "stop")
    guard.end_request(request_id, outcome="completed", usage=None, error="")
    with pytest.raises(AdmissionClosed):
        guard.check_tool(request_id, "file_read", {"path": "src/fact.txt"})
    assert rows(guard, "model_requests")[0]["state"] == "completed"


def test_revocation_is_checked_before_any_filesystem_preflight(fixture, monkeypatch):
    guard, _ = fixture
    request_id = completed_request(guard)
    command(guard.supervisor, guard.authority, "stop")
    inspected = []
    monkeypatch.setattr(guard, "_tool_scope", lambda *args: inspected.append(True))
    with pytest.raises(AdmissionClosed):
        guard.check_tool(request_id, "glob", {"path": "src", "pattern": "**/*"})
    assert inspected == []


def test_expired_lease_and_changed_grant_fail_closed(fixture):
    guard, now = fixture
    now[0] += 31
    with pytest.raises(LeaseExpired):
        guard.begin_request(purpose="primary", inputs=inputs())
    assert rows(guard, "model_requests") == []


def test_captured_grant_must_match_persisted_attempt(fixture):
    guard, _ = fixture
    with pytest.raises(ScopeDenied, match="permissions changed"):
        SwarmExecutionGuard(guard.supervisor, guard.authority, guard.context,
                            guard.workspace, replace(guard.grant, read_roots=(".",)))


def test_tool_arguments_and_call_identity_are_immutable(fixture):
    guard, _ = fixture
    request_id = completed_request(guard)
    args = {"path": "src/fact.txt"}
    receipt = guard.begin_tool(request_id, "call", "file_read", args, digest(args))
    guard.end_tool(receipt, outcome="completed", output="Observed", is_error=False, metadata={})
    with pytest.raises(Conflict, match="twice"):
        guard.begin_tool(request_id, "call", "file_read", args, digest(args))
    assert len(rows(guard, "action_receipts")) == 1


def test_argument_digest_mismatch_is_not_admitted(fixture):
    guard, _ = fixture
    request_id = completed_request(guard)
    with pytest.raises(ValueError, match="declared identity"):
        guard.begin_tool(request_id, "call", "file_read", {"path": "src/fact.txt"}, "0" * 64)
    assert rows(guard, "action_receipts") == []


def test_only_known_revision_conflict_refreshes_a_command(fixture):
    guard, _ = fixture
    # Another admitted control command changes revision after guard construction.
    command(guard.supervisor, guard.authority, "renew")
    request_id = completed_request(guard)
    assert rows(guard, "model_requests")[0]["id"] == request_id
    assert len(rows(guard, "model_requests")) == 1


def test_ambiguous_committed_command_failure_is_never_retried(fixture, monkeypatch):
    guard, _ = fixture
    original = guard.supervisor.handle
    calls = []

    def fail_after_commit(envelope, authority):
        calls.append(envelope.kind)
        original(envelope, authority)
        raise OSError("Commit acknowledgement lost")

    monkeypatch.setattr(guard.supervisor, "handle", fail_after_commit)
    with pytest.raises(OSError, match="acknowledgement"):
        guard.begin_request(purpose="primary", inputs=inputs())
    assert calls == ["reserve_request"]
    assert rows(guard, "model_requests")[0]["state"] == "reserved"
    assert guard.closed


def test_result_persistence_failure_keeps_action_unresolved(fixture, monkeypatch):
    guard, _ = fixture
    request_id = completed_request(guard)
    args = {"path": "src/fact.txt"}
    receipt = guard.begin_tool(request_id, "call", "file_read", args, digest(args))

    def fail(*args, **kwargs):
        raise OSError("Artifact storage unavailable")

    monkeypatch.setattr(guard.artifacts, "publish_tool_observation", fail)
    with pytest.raises(OSError):
        guard.end_tool(receipt, outcome="completed", output="Observed", is_error=False, metadata={})
    assert rows(guard, "action_receipts")[0]["state"] == "admitted"
    assert guard.closed


def test_revoked_artifact_cannot_be_read_through_session_view(fixture):
    guard, _ = fixture
    artifact = guard.artifacts.publish_text(guard.context, "private")
    request_id = completed_request(guard)
    guard.artifacts.revoke(guard.authority, artifact.id, guard.context)
    with pytest.raises(ScopeDenied):
        guard.check_tool(request_id, "artifact_read", {"artifact_id": artifact.id})


def test_unknown_tool_exception_is_durable_and_not_success(fixture):
    guard, _ = fixture
    request_id = completed_request(guard)
    args = {"path": "src/fact.txt"}
    receipt = guard.begin_tool(request_id, "call", "file_read", args, digest(args))
    guard.end_tool(receipt, outcome="uncertain", output="", is_error=True,
                    metadata={"error": "injected tool interruption"})
    action = rows(guard, "action_receipts")[0]
    assert action["state"] == "uncertain"
    assert action["output_artifact_id"] is None
    assert json.loads(action["metadata_json"])["error"] == "injected tool interruption"
    assert guard.closed


def test_changed_provider_identity_cannot_spend_a_second_request(fixture):
    guard, _ = fixture
    boundary = ExecutionBoundary(guard)
    provider = backend([])
    list(boundary.stream(provider, purpose="primary", inputs={"user_msg": "first"},
                         invoke=lambda: iter([done()])))
    provider.model = "silently-switched"
    invoked = []
    with pytest.raises(ExecutionGuardError, match="assigned model"):
        list(boundary.stream(provider, purpose="primary", inputs={"user_msg": "second"},
                             invoke=lambda: invoked.append(True)))
    assert invoked == []
    assert len(rows(guard, "model_requests")) == 1


def test_request_allowance_exhaustion_does_not_silently_add_calls(fixture):
    guard, _ = fixture
    for _ in range(15):
        completed_request(guard)
    with pytest.raises(AllowanceExceeded):
        guard.begin_request(purpose="primary", inputs=inputs())
    assert len(rows(guard, "model_requests")) == 15
    assert sum(row["used"] for row in rows(guard, "model_requests")) == 15


def _message_to_guard(guard):
    original = json.loads(rows(guard, "work_items")[0]["specification"])
    command(guard.supervisor, guard.authority, "plan", {"work_items": [original, {
        **original, "id": "sender", "objective": "Send an independent finding"}]})
    assigned = command(guard.supervisor, guard.authority, "assign", {"work_item_id": "sender",
        "worker_id": "sender", "requests": 1, "model": {"provider": "ollama", "model": "chosen"}}).result
    sender = AttemptContext(guard.context.scope, guard.context.run_id, assigned["attempt_id"],
                             "sender", guard.context.epoch)
    message = SwarmMailbox(guard.store, sender).send(recipient_attempt_id=guard.context.attempt_id,
        kind="finding", body="Independent fixture finding", command_id="send-fixture")
    guard.mailbox = SwarmMailbox(guard.store, guard.context)
    assert guard.mailbox.collect() == [message]
    return message


def test_mailbox_input_receipt_is_bound_to_the_exact_prepared_request(fixture):
    guard, _ = fixture
    message = _message_to_guard(guard)
    payload = inputs(conversation_history=[SwarmMailbox.context_entry(message)])
    request_id = guard.begin_request(purpose="primary", inputs=payload)
    context_receipts = [row for row in rows(guard, "receipts") if row["stage"] == "context"]
    assert len(context_receipts) == 1
    assert context_receipts[0]["message_id"] == message.id
    assert context_receipts[0]["model_request_id"] == request_id
    assert rows(guard, "request_inputs")[0]["input_sha256"] == digest(payload)


def test_mailbox_receipt_and_input_hash_roll_back_together(fixture, monkeypatch):
    guard, _ = fixture
    message = _message_to_guard(guard)
    original = guard.mailbox.attach_context

    def failure_after_receipt(connection, payload, request_id):
        original(connection, payload, request_id)
        raise OSError("Input transaction failed")

    monkeypatch.setattr(guard.mailbox, "attach_context", failure_after_receipt)
    with pytest.raises(OSError, match="Input transaction"):
        guard.begin_request(purpose="primary", inputs=inputs(
            conversation_history=[SwarmMailbox.context_entry(message)]))
    assert rows(guard, "request_inputs") == []
    assert [row["stage"] for row in rows(guard, "receipts")] == ["runtime"]
    assert rows(guard, "model_requests")[0]["state"] == "reserved"


def test_input_preparation_does_not_claim_dispatch_if_stop_wins(fixture, monkeypatch):
    guard, _ = fixture
    message = _message_to_guard(guard)
    original = guard.supervisor.handle
    stopped = False

    def stop_before_start(envelope, authority):
        nonlocal stopped
        if envelope.kind == "start_request" and not stopped:
            stopped = True
            revision = guard.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
            original(Command("operator-stop", authority.run_id, revision, authority.epoch, "stop", {}), authority)
        return original(envelope, authority)

    monkeypatch.setattr(guard.supervisor, "handle", stop_before_start)
    with pytest.raises(AdmissionClosed):
        guard.begin_request(purpose="primary", inputs=inputs(
            conversation_history=[SwarmMailbox.context_entry(message)]))
    assert len(rows(guard, "request_inputs")) == 1
    assert [row["stage"] for row in rows(guard, "receipts")] == ["runtime", "context"]
    assert rows(guard, "model_requests")[0]["state"] == "reserved"
    assert guard.closed
