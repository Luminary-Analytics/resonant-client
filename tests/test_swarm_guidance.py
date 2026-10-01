"""Durable participant controls and exact owner-input attribution."""

from dataclasses import replace
import hashlib
import json

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.guidance import OwnerGuidance
from lumi.engine.swarming.models import (
    AdmissionClosed, Conflict, IdempotencyConflict, RevisionConflict, ScopeDenied, StaleAuthority, WorkerPaused,
)
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.store import canonical_json


@pytest.fixture
def harness(tmp_path):
    now = [1000.0]
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read"}),
                           allowed_providers=frozenset({"ollama"}), read_roots=(".",), write_roots=())
    authority = supervisor.create(scope, supervisor_id="supervisor", objective="Fixture", request_limit=20, policy=policy)

    def send(kind, payload=None, *, key=None, revision=None, owner=None):
        selected = owner or authority
        if revision is None:
            revision = store.snapshot(scope, authority.run_id)["run"]["revision"]
        return supervisor.handle(Command(key or f"command-{revision}", selected.run_id, revision,
                                         selected.epoch, kind, payload or {}), selected)

    items = [{"id": key, "objective": "Inspect", "role": "explore", "tools": ["file_read"],
              "read_roots": ["."], "write_roots": [], "criteria": ["owner_review"], "dependencies": []}
             for key in ("a", "b")]
    send("plan", {"work_items": items})

    def participant(key="a", *, coordinator=False):
        payload = {"worker_id": f"worker-{key}", "requests": 6, "model": {"provider": "ollama", "model": "chosen"}}
        if coordinator:
            payload.update(read_roots=["."], tools=["file_read"])
        else:
            payload["work_item_id"] = key
        result = send("start_coordinator" if coordinator else "assign", payload).result
        context = AttemptContext(scope, authority.run_id, result["attempt_id"], payload["worker_id"], result["epoch"])
        send("worker_started", target(context))
        return context

    return store, supervisor, authority, now, send, participant


def target(context, **extra):
    return {"attempt_id": context.attempt_id, "attempt_epoch": context.epoch, **extra}


def record_input(store, send, guidance, inputs, request_id, *, purpose="primary", fail=False):
    context = guidance.context
    send("reserve_request", target(context, request_id=request_id, purpose="main" if purpose == "primary" else "auxiliary"))
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO request_inputs(request_id,input_sha256,purpose) VALUES(?,?,?)",
                           (request_id, hashlib.sha256(canonical_json(inputs).encode()).hexdigest(), purpose))
        guidance.record_input(connection, context, inputs, request_id)
        if fail:
            raise RuntimeError("fixture transaction abort")


def entry(text, **changes):
    return {"role": "user", "input_origin": "generated", "content": f"<runtime_message>\n{text}\n</runtime_message>", **changes}


@pytest.mark.parametrize("coordinator", [False, True])
def test_controls_are_current_participant_scoped_and_leave_grants_unchanged(harness, coordinator):
    store, supervisor, authority, now, send, participant = harness
    context = participant(coordinator=coordinator)
    other = participant("b")
    before = store.snapshot(authority.scope, authority.run_id)
    revision = before["run"]["revision"]
    receipt = send("pause_worker", target(context), key="pause-one", revision=revision)
    assert receipt.result["pause_requested"] == 1
    assert send("pause_worker", target(context), key="pause-one", revision=revision) == receipt
    with pytest.raises(IdempotencyConflict):
        send("pause_worker", target(other), key="pause-one", revision=revision)
    with pytest.raises(WorkerPaused):
        send("reserve_request", target(context, request_id="paused", purpose="main"))
    send("reserve_request", target(other, request_id="other", purpose="main"))
    send("resume_worker", target(context))
    send("reserve_request", target(context, request_id="own", purpose="main"))
    after = store.snapshot(authority.scope, authority.run_id)
    assert after["run"]["policy_json"] == before["run"]["policy_json"]
    assert [row["grant_json"] for row in after["attempts"]] == [row["grant_json"] for row in before["attempts"]]
    assert after["attempts"][0]["kind"] == ("coordinator" if coordinator else "worker")
    assert len(after["model_requests"]) == 2


def test_pause_after_reservation_retains_same_request_until_resume_and_run_pause_wins(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    send("reserve_request", target(context, request_id="request", purpose="main"))
    send("pause_worker", target(context))
    with pytest.raises(WorkerPaused):
        send("start_request", {"request_id": "request", "attempt_epoch": context.epoch})
    send("pause")
    send("resume_worker", target(context))
    with pytest.raises(AdmissionClosed):
        send("start_request", {"request_id": "request", "attempt_epoch": context.epoch})
    assert store.snapshot(authority.scope, authority.run_id)["model_requests"][0]["state"] == "reserved"
    send("resume")
    send("start_request", {"request_id": "request", "attempt_epoch": context.epoch})
    assert len(store.snapshot(authority.scope, authority.run_id)["model_requests"]) == 1


def test_cancel_retains_started_uncertainty_and_observation_but_never_resumes(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    send("reserve_request", target(context, request_id="started", purpose="main"))
    send("start_request", {"request_id": "started", "attempt_epoch": context.epoch})
    send("reserve_request", target(context, request_id="uninvoked", purpose="main"))
    send("cancel_worker", target(context))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["attempts"][0]["process_state"] == "running"
    assert snapshot["reservations"][0]["state"] == "reserved"
    for kind, payload in (("resume_worker", target(context)), ("steer_worker", target(context, text="Continue")),
                          ("start_request", {"request_id": "uninvoked", "attempt_epoch": context.epoch})):
        with pytest.raises(AdmissionClosed):
            send(kind, payload)
    send("worker_stopped", target(context, outcome="cancelled", evidence="fixture observed join"))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert {row["id"]: (row["state"], row["used"]) for row in snapshot["model_requests"]} == {
        "started": ("uncertain", None), "uninvoked": ("not_started", 0),
    }
    assert snapshot["attempts"][0]["state"] == "uncertain"
    assert snapshot["reservations"][0]["state"] == "uncertain"


def test_pause_or_cancel_allows_already_started_request_observation_and_replay_does_not_clear_flags(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    original = send("resume_worker", target(context), key="resume", revision=revision)
    send("reserve_request", target(context, request_id="request", purpose="main"))
    send("start_request", {"request_id": "request", "attempt_epoch": context.epoch})
    send("pause_worker", target(context))
    send("cancel_worker", target(context))
    assert send("resume_worker", target(context), key="resume", revision=revision) == original
    send("settle_request", {"request_id": "request", "attempt_epoch": context.epoch, "outcome": "completed", "used": 1})
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["attempts"][0]["cancel_requested"] == 1
    assert snapshot["attempts"][0]["pause_requested"] == 1
    assert snapshot["model_requests"][0]["state"] == "completed"
    assert snapshot["reservations"][0]["state"] == "reserved"


def test_control_rejects_stale_scope_revision_stopped_and_run_stop(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    with pytest.raises(RevisionConflict):
        send("pause_worker", target(context), revision=0, key="stale")
    with pytest.raises(ScopeDenied):
        send("pause_worker", target(context), owner=replace(authority, scope=replace(authority.scope, session_id="other")))
    with pytest.raises(StaleAuthority):
        send("pause_worker", target(context, attempt_epoch=2))
    send("stop")
    with pytest.raises(AdmissionClosed):
        send("resume_worker", target(context))
    send("worker_stopped", target(context, outcome="cancelled", evidence="joined"))
    assert store.snapshot(authority.scope, authority.run_id)["attempts"][0]["pause_requested"] == 0


def test_guidance_exact_generated_primary_input_is_immutable_reopen_safe_and_scoped(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    guidance = OwnerGuidance(store, context)
    revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    receipt = send("steer_worker", target(context, text="Inspect café first"), key="steer", revision=revision)
    assert send("steer_worker", target(context, text="Inspect café first"), key="steer", revision=revision) == receipt
    with pytest.raises(IdempotencyConflict):
        send("steer_worker", target(context, text="Change scope"), key="steer", revision=revision)
    row = guidance.pending()[0]
    text = guidance.generated_text(row)
    inputs = {"conversation_history": [entry(text)]}
    record_input(store, send, guidance, inputs, "aux", purpose="title")
    assert guidance.pending() == [row]
    record_input(store, send, guidance, inputs, "primary")
    assert guidance.pending() == []
    record_input(store, send, guidance, inputs, "later-primary")
    reopened = SwarmStore(store.path, clock=lambda: now[0])
    snapshot = reopened.snapshot(authority.scope, authority.run_id)
    assert len(snapshot["owner_directives"]) == len(snapshot["owner_directive_receipts"]) == 1
    observed = snapshot["owner_directive_receipts"][0]
    assert observed["directive_id"] == receipt.result["directive_id"]
    assert observed["request_id"] == "primary"
    assert observed["input_revision"] == 1
    assert observed["input_sha256"] == hashlib.sha256(canonical_json(inputs).encode()).hexdigest()
    with pytest.raises(ScopeDenied):
        OwnerGuidance(reopened, replace(context, scope=replace(context.scope, project_id="elsewhere"))).pending()


@pytest.mark.parametrize("changes", [{"role": "assistant"}, {"role": "tool"}, {"input_origin": "human"}])
def test_quoted_or_human_directive_is_not_generated_input(harness, changes):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    send("steer_worker", target(context, text="Inspect this"))
    guidance = OwnerGuidance(store, context)
    record_input(store, send, guidance, {"conversation_history": [entry(guidance.generated_text(guidance.pending()[0]), **changes)]}, "request")
    assert len(guidance.pending()) == 1
    assert store.snapshot(authority.scope, authority.run_id)["owner_directive_receipts"] == []


@pytest.mark.parametrize("fault", ["foreign", "changed", "peer_id", "duplicate", "rollback"])
def test_guidance_input_transaction_rejects_forgery_and_rolls_back(harness, fault):
    store, supervisor, authority, now, send, participant = harness
    context, other = participant(), participant("b")
    send("steer_worker", target(context, text="Original"))
    guidance = OwnerGuidance(store, context)
    row = guidance.pending()[0]
    if fault == "foreign":
        guidance = OwnerGuidance(store, other)
    if fault == "changed":
        row = {**row, "text": "Changed"}
    item = entry(guidance.generated_text(row), **({"message_id": "fake-peer"} if fault == "peer_id" else {}))
    inputs = {"conversation_history": [item, item] if fault == "duplicate" else [item]}
    with pytest.raises((ScopeDenied, Conflict, RuntimeError)):
        record_input(store, send, guidance, inputs, "request", fail=fault == "rollback")
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["owner_directive_receipts"] == snapshot["request_inputs"] == []
    assert snapshot["model_requests"][0]["state"] == "reserved"


def test_guidance_requires_actual_digest_and_current_request_actor(harness):
    store, supervisor, authority, now, send, participant = harness
    context, other = participant(), participant("b")
    send("steer_worker", target(context, text="Inspect"))
    guidance = OwnerGuidance(store, context)
    inputs = {"conversation_history": [entry(guidance.generated_text(guidance.pending()[0]))]}
    send("reserve_request", target(context, request_id="request", purpose="main"))
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO request_inputs(request_id,input_sha256,purpose) VALUES('request','wrong','primary')")
    with store._connection(write=True) as connection, pytest.raises(Conflict, match="differs"):
        guidance.record_input(connection, context, inputs, "request")
    with store._connection(write=True) as connection, pytest.raises(ScopeDenied):
        guidance.record_input(connection, other, inputs, "request")
    assert store.snapshot(authority.scope, authority.run_id)["owner_directive_receipts"] == []


def test_guidance_pending_is_bounded_and_cancelled_or_expired_participants_cannot_collect(harness):
    store, supervisor, authority, now, send, participant = harness
    context = participant()
    for index in range(64):
        send("steer_worker", target(context, text=f"Instruction {index}"))
    with pytest.raises(Conflict, match="limit"):
        send("steer_worker", target(context, text="Too many"))
    for text in ("", " ", "é" * 4097):
        with pytest.raises(ValueError):
            send("steer_worker", target(context, text=text))
    guidance = OwnerGuidance(store, context)
    assert len(guidance.pending()) == 64
    send("pause_worker", target(context))
    assert len(guidance.pending()) == 64
    send("cancel_worker", target(context))
    with pytest.raises(AdmissionClosed):
        guidance.pending()
    now[0] += 31
    with pytest.raises(StaleAuthority):
        guidance.pending()
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert json.loads(snapshot["attempts"][0]["grant_json"])["write_roots"] == []
