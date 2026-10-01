"""Personal collaboration contract tests; real SQLite, no providers or process execution."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from lumi.engine.swarming.models import (AdmissionClosed, AllowanceExceeded, AttemptContext, Command,
    Conflict, IdempotencyConflict, RevisionConflict, SchemaVersionError, Scope, ScopeDenied, StaleAuthority)
from lumi.engine.swarming.policy import PolicyProfile, PolicyDenied
from lumi.engine.swarming.store import SwarmStore
from lumi.engine.swarming.supervisor import SwarmSupervisor

from lumi.engine.swarming import collaboration as module
from lumi.engine.swarming.collaboration import CollaborationTerms, SwarmCollaboration


def uid():
    return uuid4().hex


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: now[0])
    supervisor = SwarmSupervisor(store)
    policy = PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama", "sonn"}), max_workers=4)
    actors = {key: supervisor.create(Scope.personal("owner", "project", key), supervisor_id=f"sup-{key}",
              run_id=key, objective=f"Private {key} conversation", request_limit=10, policy=policy, lease_seconds=300)
              for key in ("a", "b", "c", "d")}
    return store, supervisor, SwarmCollaboration(supervisor), actors, now


def invoke(collaboration, authority, operation, **kwargs):
    revision = collaboration.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return getattr(collaboration, operation)(authority, command_id=kwargs.pop("command_id", uid()),
        expected_revision=kwargs.pop("expected_revision", revision), **kwargs)


def grant(setup, origin="a", receiver="b", **values):
    _, _, collaboration, actors, now = setup
    terms = CollaborationTerms("Explicit code question only", module.KINDS, module.DATA_CLASSES,
        now[0] + 150, frozenset({origin, receiver}), **values)
    offer = invoke(collaboration, actors[origin], "offer", receiver_scope=actors[receiver].scope,
                   receiver_run_id=receiver, terms=terms).result
    invoke(collaboration, actors[receiver], "accept_grant", **{key: offer[key] for key in ("grant_id", "terms_sha256")})
    return offer["grant_id"]


def send(setup, grant_id, sender="a", recipient="b", *, kind="work_request", parent_id=None, body="Please inspect only this explicit question"):
    _, _, collaboration, actors, _ = setup
    message = invoke(collaboration, actors[sender], "send", grant_id=grant_id, kind=kind,
                     data_class="summary", body=body, parent_id=parent_id).result["message_id"]
    invoke(collaboration, actors[recipient], "deliver", message_id=message)
    return message


def work(item_id=None, **values):
    return {"id": item_id or uid(), "objective": "Receiver's independently chosen review", "role": "explore",
            "tools": ["file_read"], "read_roots": ["."], "write_roots": [], "criteria": ["owner_review"], **values}


def accept(setup, message, actor="b", **values):
    _, _, collaboration, actors, _ = setup
    arguments = dict(message_id=message, work_item=work(), model={"provider": "sonn", "model": "receiver-choice"},
                     requests=2, worker_id=uid(), evidence="Receiver explicitly accepted this bounded review")
    arguments.update(values)
    return invoke(collaboration, actors[actor], "accept_work", **arguments)


def native(setup, actor, kind, payload=None):
    store, supervisor, _, actors, _ = setup
    authority = actors[actor]
    revision = store.snapshot(authority.scope, actor)["run"]["revision"]
    return supervisor.handle(Command(uid(), actor, revision, authority.epoch, kind, payload or {}), authority)


def test_bilateral_grant_retains_exact_terms_without_conversation_or_implicit_delivery(setup):
    _, _, collaboration, actors, now = setup
    terms = CollaborationTerms("Only selected question", frozenset({"question"}), frozenset({"summary"}),
                               now[0] + 20, frozenset({"a"}))
    offer = invoke(collaboration, actors["a"], "offer", receiver_scope=actors["b"].scope, receiver_run_id="b", terms=terms).result
    with pytest.raises(AdmissionClosed):
        invoke(collaboration, actors["a"], "send", grant_id=offer["grant_id"], kind="question", data_class="summary", body="Chosen text")
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["a"], "accept_grant", grant_id=offer["grant_id"], terms_sha256=offer["terms_sha256"])
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["b"], "accept_grant", grant_id=offer["grant_id"], terms_sha256="0" * 64)
    invoke(collaboration, actors["b"], "accept_grant", grant_id=offer["grant_id"], terms_sha256=offer["terms_sha256"])
    message = invoke(collaboration, actors["a"], "send", grant_id=offer["grant_id"], kind="question", data_class="summary", body="Chosen text").result
    before = collaboration.inspect(actors["b"], offer["grant_id"])
    assert "Chosen text" not in str(before) and "Private a conversation" not in str(before)
    receipt = invoke(collaboration, actors["b"], "deliver", message_id=message["message_id"])
    assert receipt.result["body"] == "Chosen text"
    assert collaboration.inspect(actors["b"], offer["grant_id"])["messages"][0]["body"] == "Chosen text"


def test_receiver_acceptance_owns_model_policy_budget_and_pending_assignment(setup):
    store, _, _, actors, _ = setup
    message = send(setup, grant(setup))
    receipt = accept(setup, message)
    claim = receipt.result["claim"]
    assert receipt.result["payer_run_id"] == "b" and receipt.result["state"] == "accepted_pending_dispatch"
    assert claim["grant"]["model"] == {"provider": "sonn", "model": "receiver-choice"}
    source = store.snapshot(actors["a"].scope, "a")
    destination = store.snapshot(actors["b"].scope, "b")
    assert not source["attempts"] and not source["reservations"]
    assert destination["reservations"][0]["amount"] == 2
    assert destination["dispatches"][0]["state"] == "pending"
    assert destination["attempts"][0]["process_state"] == "pending"
    assert "supervisor_id" not in json.dumps(receipt.result) and "api_key" not in json.dumps(receipt.result)


@pytest.mark.parametrize("change", ({"owner_id": "other"}, {"tenant_id": "other"}, {"project_id": "other"}, {"session_id": "absent"}))
def test_offer_does_not_discover_or_cross_owner_project_tenant_boundaries(setup, change):
    _, _, collaboration, actors, now = setup
    terms = CollaborationTerms("Purpose", frozenset({"question"}), frozenset({"summary"}), now[0] + 20, frozenset({"a"}))
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["a"], "offer", receiver_scope=replace(actors["b"].scope, **change), receiver_run_id="b", terms=terms)


def test_unaddressed_run_cannot_inspect_deliver_accept_or_revoke(setup):
    _, _, collaboration, actors, _ = setup
    grant_id = grant(setup)
    message = send(setup, grant_id)
    for operation in (lambda: collaboration.inspect(actors["c"], grant_id),
        lambda: invoke(collaboration, actors["c"], "deliver", message_id=message),
        lambda: accept(setup, message, actor="c"),
        lambda: invoke(collaboration, actors["c"], "revoke", grant_id=grant_id, evidence="Not authorized")):
        with pytest.raises(ScopeDenied):
            operation()


@pytest.mark.parametrize("failure", ("policy", "budget", "authority_field", "revision"))
def test_failed_acceptance_rolls_back_own_assignment_and_allocation(setup, failure):
    store, _, _, actors, _ = setup
    message = send(setup, grant(setup, max_requests=20))
    values = {"model": {"provider": "openrouter", "model": "not-granted"}} if failure == "policy" else (
        {"requests": 11} if failure == "budget" else {"work_item": work(supervisor_id="forged")} if failure == "authority_field" else {"expected_revision": 0})
    with pytest.raises((PolicyDenied, AllowanceExceeded, ValueError, RevisionConflict)):
        accept(setup, message, **values)
    snapshot = store.snapshot(actors["b"].scope, "b")
    assert not snapshot["work_items"] and not snapshot["reservations"] and not snapshot["attempts"]


def test_independent_connection_duplicate_acceptances_allocate_only_once(setup):
    store, _, collaboration, actors, now = setup
    message = send(setup, grant(setup))
    other = SwarmCollaboration(SwarmSupervisor(SwarmStore(store.path, clock=lambda: now[0])))
    revision = store.snapshot(actors["b"].scope, "b")["run"]["revision"]
    kwargs = dict(command_id=uid(), expected_revision=revision, message_id=message, work_item=work(),
        model={"provider": "ollama", "model": "receiver"}, requests=2, worker_id="receiver-worker", evidence="Exact receiver choice")
    barrier = threading.Barrier(2)

    def call(instance):
        barrier.wait(30)
        return instance.accept_work(actors["b"], **kwargs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(call, (collaboration, other)))
    assert first == second
    assert len(store.snapshot(actors["b"].scope, "b")["reservations"]) == 1
    with pytest.raises(IdempotencyConflict):
        other.accept_work(actors["b"], **{**kwargs, "requests": 3})


@pytest.mark.parametrize("close", ("revoke", "origin_stop", "expiry", "epoch", "policy"))
def test_grant_closure_fences_future_deliveries_without_killing_accepted_peer_work(setup, close):
    store, _, collaboration, actors, now = setup
    grant_id = grant(setup)
    accepted_message = send(setup, grant_id)
    accepted = accept(setup, accepted_message)
    pending = invoke(collaboration, actors["a"], "send", grant_id=grant_id, kind="question", data_class="summary", body="Future message").result
    if close == "revoke":
        invoke(collaboration, actors["a"], "revoke", grant_id=grant_id, evidence="Owner closes disclosure")
    elif close == "origin_stop":
        native(setup, "a", "stop")
    elif close == "expiry":
        now[0] += 151
    else:
        with store._connection(write=True) as connection:
            if close == "epoch":
                connection.execute("UPDATE runs SET epoch=epoch+1 WHERE id='a'")
            else:
                policy = json.loads(connection.execute("SELECT policy_json FROM runs WHERE id='a'").fetchone()[0])
                policy["version"] += 1
                connection.execute("UPDATE runs SET policy_json=? WHERE id='a'", (json.dumps(policy),))
    with pytest.raises(AdmissionClosed):
        invoke(collaboration, actors["b"], "deliver", message_id=pending["message_id"])
    receiver = store.snapshot(actors["b"].scope, "b")
    assert receiver["run"]["state"] == "running" and receiver["attempts"][0]["state"] == "leased"
    assert receiver["attempts"][0]["id"] == accepted.result["claim"]["attempt_id"]
    native(setup, "b", "worker_started", {"attempt_id": accepted.result["claim"]["attempt_id"], "attempt_epoch": 1})


def test_hop_fanout_dedupe_and_undelivered_parent_limits(setup):
    _, _, collaboration, actors, _ = setup
    ab = grant(setup, max_hops=2, max_fanout=1)
    bc = grant(setup, "b", "c", max_hops=8, max_fanout=8)
    bd = grant(setup, "b", "d", max_hops=8, max_fanout=8)
    cd = grant(setup, "c", "d", max_hops=8, max_fanout=8)
    pending = invoke(collaboration, actors["a"], "send", grant_id=ab, kind="question", data_class="summary", body="Root").result["message_id"]
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["b"], "send", grant_id=bc, kind="question", data_class="summary", body="Forward", parent_id=pending)
    invoke(collaboration, actors["b"], "deliver", message_id=pending)
    forwarded = send(setup, bc, "b", "c", kind="question", parent_id=pending)
    with pytest.raises(Conflict):
        invoke(collaboration, actors["b"], "send", grant_id=bd, kind="question", data_class="summary", body="Fanout", parent_id=pending)
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["c"], "send", grant_id=cd, kind="question", data_class="summary", body="Third hop", parent_id=forwarded)


def test_cross_run_work_cycle_is_rejected_without_partial_assignment(setup):
    store, _, _, actors, _ = setup
    ab = grant(setup, max_hops=3, max_requests=10)
    bc = grant(setup, "b", "c", max_hops=3, max_requests=10)
    ca = grant(setup, "c", "a", max_hops=3, max_requests=10)
    first = send(setup, ab)
    accept(setup, first, requests=1)
    second = send(setup, bc, "b", "c", parent_id=first)
    accept(setup, second, actor="c", requests=1)
    third = send(setup, ca, "c", "a", parent_id=second)
    with pytest.raises(Conflict, match="cycle"):
        accept(setup, third, actor="a", requests=1)
    assert not store.snapshot(actors["a"].scope, "a")["work_items"]


def test_ancestor_request_cap_cannot_be_broadened_by_next_grant(setup):
    _, _, _, _, _ = setup
    ab = grant(setup, max_hops=3, max_requests=2)
    bc = grant(setup, "b", "c", max_hops=3, max_requests=20)
    first = send(setup, ab)
    accept(setup, first, requests=2)
    second = send(setup, bc, "b", "c", parent_id=first)
    with pytest.raises(Conflict, match="ceiling"):
        accept(setup, second, actor="c", requests=1)


def test_message_content_corruption_is_not_disclosed_as_original(setup):
    store, _, collaboration, actors, _ = setup
    grant_id = grant(setup)
    message = send(setup, grant_id)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE collaboration_messages SET body='substituted' WHERE id=?", (message,))
    with pytest.raises(Conflict, match="content changed"):
        collaboration.inspect(actors["b"], grant_id)


def test_terms_reject_unknown_credentials_and_unsupported_spend_transfer(setup):
    _, _, _, _, now = setup
    terms = CollaborationTerms("Purpose", frozenset({"question"}), frozenset({"summary"}), now[0] + 10, frozenset({"a"}))
    assert CollaborationTerms.from_dict(terms.to_dict()) == terms
    for change in ({"api_key": "private"}, {"cost_limit_usd": 1}, {"payer": "origin_run"},
                   {"max_requests": True}, {"expires_at": float("nan")}, {"kinds": ["stop_worker"]}):
        with pytest.raises(ValueError):
            CollaborationTerms.from_dict({**terms.to_dict(), **change})


def test_stale_supervisor_cannot_replay_even_an_original_grant_command(setup):
    store, _, collaboration, actors, now = setup
    command_id = uid()
    terms = CollaborationTerms("Purpose", frozenset({"question"}), frozenset({"summary"}), now[0] + 20, frozenset({"a"}))
    invoke(collaboration, actors["a"], "offer", command_id=command_id, receiver_scope=actors["b"].scope, receiver_run_id="b", terms=terms)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE runs SET epoch=2,supervisor_id='replacement' WHERE id='a'")
    with pytest.raises(StaleAuthority):
        invoke(collaboration, actors["a"], "offer", command_id=command_id, expected_revision=0,
               receiver_scope=actors["b"].scope, receiver_run_id="b", terms=terms)


def test_expiry_during_acceptance_rolls_back_all_new_work(setup, monkeypatch):
    store, supervisor, _, actors, now = setup
    message = send(setup, grant(setup))
    original = supervisor._assign

    def expire_after_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 151
        return result

    monkeypatch.setattr(supervisor, "_assign", expire_after_validation)
    with pytest.raises(AdmissionClosed):
        accept(setup, message)
    snapshot = store.snapshot(actors["b"].scope, "b")
    assert not snapshot["work_items"] and not snapshot["attempts"] and not snapshot["reservations"]


def test_explicit_artifact_offer_contains_no_filename_and_grants_no_blob_read(setup):
    from lumi.engine.swarming.artifacts import SwarmArtifacts
    from lumi.engine.swarming.models import AttemptContext
    store, _, collaboration, actors, _ = setup
    grant_id = grant(setup)
    message = send(setup, grant_id)
    claim = accept(setup, message, actor="b").result["claim"]
    # A retained source reference is enough to offer immutable metadata. The
    # reader receives no worker artifact grant or filesystem locator.
    context = AttemptContext(actors["b"].scope, "b", claim["attempt_id"], claim["worker_id"], 1)
    artifact = SwarmArtifacts(store).publish_text(context, "Private body", label="private-filename.txt")
    offered = invoke(collaboration, actors["b"], "send", grant_id=grant_id, kind="artifact_offer", data_class="artifact_reference", artifact_ids=(artifact.id,)).result
    delivered = invoke(collaboration, actors["a"], "deliver", message_id=offered["message_id"]).result
    assert delivered["artifacts"] == [{"id": artifact.id, "sha256": artifact.sha256, "size": 12}]
    assert "private-filename" not in json.dumps(delivered) and "Private body" not in json.dumps(delivered)
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_grants WHERE recipient_attempt_id!=?", (claim["attempt_id"],)).fetchone()[0] == 0
    with pytest.raises(ScopeDenied):
        invoke(collaboration, actors["a"], "send", grant_id=grant_id, kind="artifact_offer", data_class="artifact_reference", artifact_ids=(artifact.id,))


@pytest.mark.parametrize("values,bodies", (({"max_messages": 1}, ["First", "Second"]),
    ({"max_total_bytes": 20, "max_message_bytes": 20}, ["123456789", "123456789"])))
def test_disclosure_message_and_total_byte_ceilings_are_durable(setup, values, bodies):
    _, _, collaboration, actors, _ = setup
    grant_id = grant(setup, **values)
    invoke(collaboration, actors["a"], "send", grant_id=grant_id, kind="question", data_class="summary", body=bodies[0])
    with pytest.raises(Conflict):
        invoke(collaboration, actors["a"], "send", grant_id=grant_id, kind="question", data_class="summary", body=bodies[1])


@pytest.mark.parametrize("limit", ("bytes", "messages"))
def test_forwarding_cannot_broaden_cumulative_origin_disclosure(setup, limit):
    _, _, collaboration, actors, _ = setup
    values = {"max_total_bytes": 100, "max_message_bytes": 100} if limit == "bytes" else {"max_messages": 1}
    ab = grant(setup, max_hops=3, **values)
    bc = grant(setup, "b", "c", max_hops=3, max_messages=100)
    first = send(setup, ab, kind="question", body="x" * 50)
    with pytest.raises(Conflict, match="Inherited causal"):
        invoke(collaboration, actors["b"], "send", grant_id=bc, kind="question", data_class="summary", body="y" * 50, parent_id=first)
    assert collaboration.inspect(actors["b"], bc)["messages"] == []


def test_ancestor_expiry_during_send_rolls_back_publication(setup, monkeypatch):
    _, _, collaboration, actors, now = setup
    ab = grant(setup, max_hops=3)
    first = send(setup, ab, kind="question")
    now[0] += 10
    bc = grant(setup, "b", "c", max_hops=3)
    original = collaboration._finish

    def expire_ancestor(*args, **kwargs):
        now[0] = 1151  # A->B expired; B->C is still active until 1160.
        return original(*args, **kwargs)

    monkeypatch.setattr(collaboration, "_finish", expire_ancestor)
    with pytest.raises(AdmissionClosed):
        invoke(collaboration, actors["b"], "send", grant_id=bc, kind="question", data_class="summary", body="Too late", parent_id=first)
    assert collaboration.inspect(actors["b"], bc)["messages"] == []


def test_organization_scope_needs_separate_managed_collaboration_policy(setup):
    store, _, collaboration, actors, now = setup
    with store._connection(write=True) as connection:
        connection.execute("UPDATE runs SET tenant_id='organization' WHERE id IN ('a','b')")
    first = replace(actors["a"], scope=replace(actors["a"].scope, tenant_id="organization"))
    second = replace(actors["b"], scope=replace(actors["b"].scope, tenant_id="organization"))
    terms = CollaborationTerms("Not managed-authorized", frozenset({"question"}), frozenset({"summary"}), now[0] + 50, frozenset({"a"}))
    with pytest.raises(ScopeDenied):
        invoke(collaboration, first, "offer", receiver_scope=second.scope, receiver_run_id="b", terms=terms)


def test_native_retry_cannot_reallocate_outside_accepted_collaboration_budget(setup):
    store, _, _, actors, _ = setup
    accepted = accept(setup, send(setup, grant(setup)))
    claim = accepted.result["claim"]
    native(setup, "b", "worker_stopped", {"attempt_id": claim["attempt_id"], "attempt_epoch": 1,
                                           "outcome": "failed", "evidence": "Fixture observed no dispatch or model requests"})
    with pytest.raises(Conflict, match="fresh request"):
        native(setup, "b", "retry", {"work_item_id": accepted.result["work_item_id"], "evidence": "Explicit retry request"})
    assert store.snapshot(actors["b"].scope, "b")["work_items"][0]["state"] == "failed"
    # Even a future readiness producer cannot accidentally bypass allocation fencing.
    with store._connection(write=True) as connection:
        connection.execute("UPDATE work_items SET state='ready' WHERE id=?", (accepted.result["work_item_id"],))
    with pytest.raises(Conflict, match="fresh request"):
        native(setup, "b", "assign", {"work_item_id": accepted.result["work_item_id"], "worker_id": "retry",
            "requests": 3, "model": {"provider": "sonn", "model": "receiver-choice"}})
    assert len(store.snapshot(actors["b"].scope, "b")["reservations"]) == 1


def v2_database(tmp_path):
    path = tmp_path / "v2.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/swarming_store_v2.sql").read_text())
        connection.execute("INSERT INTO runs(id,tenant_id,owner_id,project_id,session_id,supervisor_id,epoch,protocol_version,objective,state,request_limit) "
            "VALUES('retained','personal:owner','owner','project','historical','old',1,1,'Retained private objective','stopping',5)")
        connection.execute("INSERT INTO work_items(id,run_id,objective,state) VALUES('work','retained','Retained work','uncertain')")
        connection.execute("INSERT INTO attempts(id,run_id,work_item_id,worker_id,epoch,state,process_state) "
            "VALUES('attempt','retained','work','worker',1,'uncertain','unknown')")
        connection.execute("INSERT INTO reservations VALUES('reservation','attempt',3,NULL,'uncertain')")
        connection.execute("INSERT INTO events(run_id,sequence,epoch,kind,payload) VALUES('retained',1,1,'historical','{}')")
    return path


def test_v2_upgrade_backs_up_and_preserves_unknown_state_without_replay(tmp_path):
    path = v2_database(tmp_path)
    with pytest.raises(SchemaVersionError):
        SwarmStore(path, read_only=True)
    assert not list(tmp_path.glob("*.backup"))
    upgraded = SwarmStore(path)
    snapshot = upgraded.snapshot(Scope.personal("owner", "project", "historical"), "retained")
    assert snapshot["run"]["state"] == "stopping"
    assert snapshot["attempts"][0]["process_state"] == "unknown"
    assert snapshot["reservations"][0]["state"] == "uncertain" and snapshot["reservations"][0]["used"] is None
    backup, = tmp_path.glob("v2.sqlite.v2-*.backup")
    assert SwarmStore.inspect_database(backup)["schema_version"] == 2
    assert SwarmStore.inspect_database(path)["schema_version"] == 3
    with upgraded._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM collaboration_grants").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v2_upgrade_rolls_back_schema_and_retains_recovery_backup(tmp_path, monkeypatch):
    path = v2_database(tmp_path)
    original = SwarmStore._migrate_v2

    def fail(connection):
        original(connection)
        raise sqlite3.OperationalError("Injected schema-three interruption")

    monkeypatch.setattr(SwarmStore, "_migrate_v2", staticmethod(fail))
    with pytest.raises(sqlite3.OperationalError, match="schema-three"):
        SwarmStore(path)
    assert SwarmStore.inspect_database(path)["schema_version"] == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations WHERE state='uncertain'").fetchone()[0] == 1
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='collaboration_grants'").fetchone() is None
    assert len(list(tmp_path.glob("v2.sqlite.v2-*.backup"))) == 1


def test_two_native_sessions_keep_captured_ownership_and_receiver_work_after_origin_stop(setup, tmp_path):
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

    store, supervisor, collaboration, actors, _ = setup
    (tmp_path / "source.txt").write_text("Source-only evidence")
    (tmp_path / "receiver.txt").write_text("Receiver's selected evidence")
    entered = {key: threading.Event() for key in ("a", "b")}
    release = {key: threading.Event() for key in ("a", "b")}
    captured = {key: [] for key in ("a", "b")}

    class Provider(StreamingBackend):
        def __init__(self, key, spec):
            self.key = key
            path = "source.txt" if key == "a" else "receiver.txt"
            super().__init__(name=spec.backend_type, model=spec.model,
                scripts=[[tool_call("file_read", {"path": path}), done(model=spec.model)],
                         [text_delta("Actual file observation submitted for separate owner review"), done(model=spec.model)]])

        def stream(self, **kwargs):
            captured[self.key].append(json.dumps(kwargs["conversation_history"]))
            entered[self.key].set()
            while not release[self.key].wait(.01):
                if kwargs["cancel_event"].is_set():
                    return
            yield from super().stream(**kwargs)

    native(setup, "a", "plan", {"work_items": [work("source-work", objective="Source-only private context", read_roots=["source.txt"])]})
    source_claim = native(setup, "a", "assign", {"work_item_id": "source-work", "worker_id": "source-worker", "requests": 4,
        "model": {"provider": "ollama", "model": "source-choice"}}).result
    grant_id = grant(setup)
    message = send(setup, grant_id, body="Selected question only")
    accepted = accept(setup, message, work_item=work(objective="Answer only the selected question", read_roots=["receiver.txt"]), requests=4)
    target_claim = accepted.result["claim"]
    runtimes = {key: SwarmWorkerRunner(SwarmSupervisor(SwarmStore(store.path, clock=store.clock)), actors[key], tmp_path,
                backend_factory=lambda spec, key=key: Provider(key, spec)) for key in ("a", "b")}
    try:
        for key, claim, provider, model in (("a", source_claim, "ollama", "source-choice"), ("b", target_claim, "sonn", "receiver-choice")):
            context = AttemptContext(actors[key].scope, key, claim["attempt_id"], claim["worker_id"], 1)
            runtimes[key].start(context, BackendSpec(provider, model, api_key=f"private-{key}-fixture-key"))
        assert entered["a"].wait(30) and entered["b"].wait(30)
        runtimes["a"].stop()
        assert runtimes["b"].inspect(target_claim["attempt_id"])["alive"]
        assert store.snapshot(actors["b"].scope, "b")["run"]["state"] == "running"
        with pytest.raises(AdmissionClosed):
            invoke(collaboration, actors["b"], "send", grant_id=grant_id, kind="finding", data_class="summary", body="Late delivery denied")
        release["b"].set()
        deadline = time.monotonic() + 8
        while runtimes["b"].inspect(target_claim["attempt_id"])["alive"] and time.monotonic() < deadline:
            time.sleep(.01)
        result = store.snapshot(actors["b"].scope, "b")
        assert len(result["submissions"]) == 1 and result["work_items"][0]["state"] == "submitted"
        assert result["reservations"][0]["used"] == 2 and result["attempts"][0]["process_state"] == "stopped"
        assert "Source-only private context" not in "".join(captured["b"])
        assert "private-a-fixture-key" not in str(captured) and "private-b-fixture-key" not in str(captured)
    finally:
        for event in release.values():
            event.set()
        for runtime in runtimes.values():
            assert all(not row["alive"] for row in runtime.close(timeout=5))
