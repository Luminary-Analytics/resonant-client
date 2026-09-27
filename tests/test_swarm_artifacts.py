"""Artifact disclosure is explicit, current, and checked against durable state."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from lumi.engine.swarming import Scope, SwarmStore
from lumi.engine.swarming.artifacts import SwarmArtifacts
from lumi.engine.swarming.models import AdmissionClosed, Conflict, ScopeDenied, StaleAuthority


@pytest.fixture
def fixture(tmp_path):
    store = SwarmStore(tmp_path / "state.sqlite")
    scope = Scope.personal("owner", "project", "session")
    authority = store.create_run(scope, supervisor_id="supervisor", objective="Evidence fixture", request_limit=12)
    contexts = []
    for name in ("reader-a", "reader-b", "reader-c"):
        store.add_work_item(authority, work_item_id=name, objective="Inspect")
        contexts.append(store.claim(authority, work_item_id=name, worker_id=name,
                                    requests=2, command_id=f"assign-{name}").context)
    return store, authority, SwarmArtifacts(store), contexts


def test_reference_is_not_a_disclosure_grant_and_only_producer_can_share(fixture):
    store, authority, artifacts, (producer, receiver, third) = fixture
    ref = artifacts.publish_text(producer, "Observed exact evidence")
    assert artifacts.read_text_page(producer, ref.id) == "Observed exact evidence"
    assert "path=" not in artifacts.reference(ref)
    store.send(producer, recipient_attempt_id=receiver.attempt_id, kind="handoff_reference",
               body=artifacts.reference(ref), command_id="send-ref")
    assert store.receive(receiver)[0].body == artifacts.reference(ref)
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(receiver, ref.id)
    artifacts.share(producer, ref.id, receiver)
    artifacts.share(producer, ref.id, receiver)
    assert artifacts.read_text_page(receiver, ref.id) == "Observed exact evidence"
    with pytest.raises(ScopeDenied):
        artifacts.share(receiver, ref.id, third)
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(third, ref.id)
    events = store.events(authority.scope, authority.run_id)
    assert sum(event.kind == "artifact_shared" for event in events) == 1


def test_identical_parallel_evidence_can_publish_while_winner_is_open_for_read(fixture, monkeypatch):
    _, _, artifacts, contexts = fixture
    content = b"Two workers observed the same complete immutable evidence."
    target = artifacts.root / hashlib.sha256(content).hexdigest()[:2] / hashlib.sha256(content).hexdigest()
    flushed = threading.Barrier(2)
    reading, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    counts = {"flush": 0, "read": 0, "publish": 0}
    real_sync, real_read, real_publish = os.fsync, Path.read_bytes, os.link

    def publish_after_winner_open(source, destination):
        with lock:
            counts["publish"] += 1
            number = counts["publish"]
        if number == 2:
            assert reading.wait(5)
        return real_publish(source, destination)

    def synchronized_flush(fd):
        real_sync(fd)
        with lock:
            counts["flush"] += 1
            number = counts["flush"]
        if number <= 2:
            flushed.wait(timeout=5)  # Both publishers observed the address absent.

    def held_read(path):
        if path != target:
            return real_read(path)
        with lock:
            counts["read"] += 1
            number = counts["read"]
        if number != 1:
            return real_read(path)
        with path.open("rb") as stream:
            observed = stream.read()
            reading.set()
            assert release.wait(5)
            return observed

    monkeypatch.setattr(os, "fsync", synchronized_flush)
    monkeypatch.setattr(os, "link", publish_after_winner_open)
    monkeypatch.setattr(Path, "read_bytes", held_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(artifacts.publish_bytes, context, content) for context in contexts[:2]]
        try:
            assert reading.wait(5), [(future.done(), repr(future.exception()) if future.done() else "pending") for future in futures]
            finished, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert finished, "The second publisher must not wait for the first reader to close"
            next(iter(finished)).result()  # A replacement race fails here on Windows.
        finally:
            release.set()
        references = [future.result(timeout=5) for future in futures]
    assert len({reference.id for reference in references}) == 2
    assert {reference.sha256 for reference in references} == {hashlib.sha256(content).hexdigest()}
    assert real_read(target) == content
    assert not list(target.parent.glob(".pending-*"))


@pytest.mark.parametrize("field", ["tenant_id", "owner_id", "project_id", "session_id"])
def test_foreign_scope_cannot_read_publish_share_or_inspect(fixture, field):
    store, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Private")
    foreign_scope = replace(producer.scope, **{field: "foreign"})
    foreign = replace(producer, scope=foreign_scope)
    for operation in (
        lambda: artifacts.read_text_page(foreign, ref.id),
        lambda: artifacts.publish_text(foreign, "Not authorized"),
        lambda: artifacts.share(foreign, ref.id, receiver),
        lambda: artifacts.inspect(foreign_scope, authority.run_id, ref.id),
    ):
        with pytest.raises(ScopeDenied):
            operation()


def test_other_run_same_scope_cannot_receive_content(fixture):
    store, authority, artifacts, (producer, _, _) = fixture
    ref = artifacts.publish_text(producer, "Run-private")
    other = store.create_run(authority.scope, supervisor_id="other-supervisor",
                             objective="Other", request_limit=2)
    store.add_work_item(other, work_item_id="other-item", objective="Inspect")
    context = store.claim(other, work_item_id="other-item", worker_id="other-reader",
                          requests=1, command_id="claim-other").context
    with pytest.raises(ScopeDenied):
        artifacts.share(producer, ref.id, context)
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(context, ref.id)
    with pytest.raises(ScopeDenied):
        artifacts.inspect(authority.scope, other.run_id, ref.id)


def test_stop_revokes_worker_access_but_owner_keeps_history(fixture):
    store, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Retained")
    artifacts.share(producer, ref.id, receiver)
    store.stop(authority, command_id="stop")
    for operation in (
        lambda: artifacts.read_text_page(receiver, ref.id),
        lambda: artifacts.publish_text(producer, "Late"),
        lambda: artifacts.share(producer, ref.id, receiver),
    ):
        with pytest.raises(AdmissionClosed):
            operation()
    assert artifacts.inspect(authority.scope, authority.run_id, ref.id) == ref


def test_finished_producer_does_not_erase_evidence_or_retain_worker_access(fixture):
    store, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Useful partial result")
    artifacts.share(producer, ref.id, receiver)
    store.finish_attempt(authority, producer, outcome="submitted", used_requests=1, command_id="finish")
    assert artifacts.read_text_page(receiver, ref.id) == "Useful partial result"
    with pytest.raises(Conflict):
        artifacts.read_text_page(producer, ref.id)
    with pytest.raises(Conflict):
        artifacts.publish_text(producer, "Late")


def test_epoch_recovery_fences_old_workers_without_deleting_references(fixture):
    store, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Historical")
    artifacts.share(producer, ref.id, receiver)
    store.reconcile(authority, supervisor_id="replacement")
    with pytest.raises(StaleAuthority):
        artifacts.read_text_page(receiver, ref.id)
    assert artifacts.inspect(authority.scope, authority.run_id, ref.id) == ref


def test_recipient_revocation_is_immediate_idempotent_and_preserves_other_access(fixture):
    store, authority, artifacts, (producer, receiver, third) = fixture
    ref = artifacts.publish_text(producer, "Evidence")
    artifacts.share(producer, ref.id, receiver)
    artifacts.share(producer, ref.id, third)
    artifacts.revoke(authority, ref.id, receiver)
    artifacts.revoke(authority, ref.id, receiver)
    with pytest.raises(ScopeDenied, match="revoked"):
        artifacts.share(producer, ref.id, receiver)
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(receiver, ref.id)
    assert artifacts.read_text_page(third, ref.id) == "Evidence"
    assert sum(event.kind == "artifact_access_revoked"
               for event in store.events(authority.scope, authority.run_id)) == 1


def test_blob_corruption_is_rejected_before_display(fixture):
    _, _, artifacts, (producer, _, _) = fixture
    ref = artifacts.publish_text(producer, "original")
    path = artifacts.root / ref.sha256[:2] / ref.sha256
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="content verification"):
        artifacts.read_text_page(producer, ref.id)
    with pytest.raises(ValueError, match="content digest"):
        artifacts.publish_text(producer, "original")


def test_binary_artifact_never_claims_visual_analysis(fixture):
    _, _, artifacts, (producer, _, _) = fixture
    ref = artifacts.publish_bytes(producer, b"png-fixture", kind="image", media_type="image/png")
    with pytest.raises(ValueError, match="no visual interpretation"):
        artifacts.read_text_page(producer, ref.id)


def test_unicode_pages_and_retained_content_survive_service_reopen(fixture):
    store, _, artifacts, (producer, _, _) = fixture
    text = "alpha Ω🙂 finish"
    ref = artifacts.publish_text(producer, text)
    reopened = SwarmArtifacts(SwarmStore(store.path))
    assert reopened.read_text_page(producer, ref.id, offset=6, limit=2) == "Ω🙂\n[More evidence: artifact_read offset=8]"
    assert reopened.read_text_page(producer, ref.id, offset=8) == " finish"
    assert ref.sha256 == hashlib.sha256(text.encode()).hexdigest()


def test_artifact_reference_commit_failure_never_discloses_or_acknowledges(fixture):
    store, authority, artifacts, (producer, _, _) = fixture
    before = store.snapshot(authority.scope, authority.run_id)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_artifact BEFORE INSERT ON events "
                           "WHEN NEW.kind='artifact_published' BEGIN SELECT RAISE(ABORT, 'no durable receipt'); END")
    with pytest.raises(sqlite3.IntegrityError, match="no durable receipt"):
        artifacts.publish_text(producer, "Unreferenced blob is safe to retain")
    assert store.snapshot(authority.scope, authority.run_id) == before
    # The orphan is not discoverable through an artifact ID or recipient grant.
    assert any(artifacts.root.rglob("*"))


def test_stop_during_blob_write_is_rechecked_before_reference_commit(fixture, monkeypatch):
    store, authority, artifacts, (producer, _, _) = fixture
    original = artifacts._write_blob

    def interrupt(content):
        result = original(content)
        store.stop(authority, command_id="stop-during-write")
        return result

    monkeypatch.setattr(artifacts, "_write_blob", interrupt)
    with pytest.raises(AdmissionClosed):
        artifacts.publish_text(producer, "Unacknowledged")
    assert store.snapshot(authority.scope, authority.run_id)["artifact_refs"] == []


def test_revocation_during_blob_read_is_rechecked_before_disclosure(fixture, monkeypatch):
    _, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Do not disclose")
    artifacts.share(producer, ref.id, receiver)
    original = artifacts._read_blob

    def revoke_during_read(artifact):
        result = original(artifact)
        artifacts.revoke(authority, artifact.id, receiver)
        return result

    monkeypatch.setattr(artifacts, "_read_blob", revoke_during_read)
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(receiver, ref.id)


@pytest.mark.parametrize("value", ["../../secret", "'; SELECT * FROM runs; --", "missing"])
def test_artifact_identifier_cannot_select_a_filesystem_path(fixture, value):
    _, _, artifacts, (producer, _, _) = fixture
    with pytest.raises(ScopeDenied):
        artifacts.read_text_page(producer, value)


def test_tool_provenance_requires_originating_request_and_tool_identity(fixture):
    _, _, artifacts, (producer, _, _) = fixture
    with pytest.raises(ValueError, match="model request and tool call"):
        artifacts.publish_text(producer, "Claimed output", origin="tool_result")
    with pytest.raises(ValueError, match="Only tool evidence"):
        artifacts.publish_text(producer, "Claimed handoff", tool_call_id="forged")


def test_tool_request_provenance_cannot_be_fabricated_or_borrowed(fixture):
    store, _, artifacts, (producer, other, _) = fixture
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO model_requests VALUES(?,?,?,'main','completed',1)",
                           ("other-request", other.attempt_id, other.epoch))
        connection.execute("INSERT INTO model_requests VALUES(?,?,?,'main','reserved',NULL)",
                           ("pending-request", producer.attempt_id, producer.epoch))
        connection.execute("INSERT INTO model_requests VALUES(?,?,?,'main','completed',1)",
                           ("producer-request", producer.attempt_id, producer.epoch))
    for request_id in ("nonexistent", "other-request"):
        with pytest.raises(ScopeDenied):
            artifacts.publish_text(producer, "Claimed", origin="tool_result",
                                   model_request_id=request_id, tool_call_id="call-1")
    with pytest.raises(Conflict):
        artifacts.publish_text(producer, "Premature", origin="tool_result",
                               model_request_id="pending-request", tool_call_id="call-1")
    ref = artifacts.publish_text(producer, "Observed", origin="tool_result",
                                 model_request_id="producer-request", tool_call_id="call-1")
    assert ref.model_request_id == "producer-request"
    assert ref.attempt_id == producer.attempt_id


def test_revoked_producer_cannot_reshare_or_regrant_itself(fixture):
    _, authority, artifacts, (producer, receiver, _) = fixture
    ref = artifacts.publish_text(producer, "Evidence")
    artifacts.revoke(authority, ref.id, producer)
    with pytest.raises(ScopeDenied):
        artifacts.share(producer, ref.id, producer)
    with pytest.raises(ScopeDenied):
        artifacts.share(producer, ref.id, receiver)


def test_blob_directory_junction_cannot_escape_runtime_root(fixture, tmp_path):
    _, _, artifacts, (producer, _, _) = fixture
    content = b"escape fixture"
    digest = hashlib.sha256(content).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (artifacts.root / digest[:2]).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ScopeDenied):
        artifacts.publish_bytes(producer, content)
    assert list(outside.iterdir()) == []
