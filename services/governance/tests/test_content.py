"""Real PostgreSQL evidence for explicit encrypted disclosure and retention."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import threading
import time

import psycopg
from psycopg.rows import dict_row
import pytest

from sonn_governance.content import ContentKeys, ContentStore, ContentUnavailable
from sonn_governance.models import AccessDenied, Conflict, InvalidRequest
from test_store import Tenant, uid


@pytest.fixture
def content_fixture(database_config):
    tenant = Tenant(database_config)
    tenant.member(tenant.reader, projects={tenant.project: [
        "content_write", "content_read", "metadata_read", "retention_admin",
    ]})
    tenant.store.command(tenant.admin, tenant.command(content_mode="explicit"))
    content = ContentStore(tenant.store, ContentKeys({"fixture-key": b"k" * 32}, "fixture-key"))
    return tenant, content


def publish(fixture, *, content_id=None, command_id=None, data=b"private retained evidence", retention_seconds=3600):
    tenant, content = fixture
    args = (tenant.reader, tenant.id, tenant.project, content_id or uid())
    kwargs = {"command_id": command_id or uid(), "content": data, "media_type": "text/plain",
              "retention_seconds": retention_seconds}
    return args, kwargs, content.publish(*args, **kwargs)


def sql(tenant, statement, parameters=()):
    with psycopg.connect(tenant.config["owner_dsn"], row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
        cursor = connection.execute(statement, parameters)
        return cursor.fetchall() if cursor.description else []


def test_explicit_content_policy_and_grants_are_separate_from_administration(database_config):
    tenant = Tenant(database_config)
    content = ContentStore(tenant.store, ContentKeys({"key": b"k" * 32}, "key"))
    args = (tenant.admin, tenant.id, tenant.project, uid())
    kwargs = {"command_id": uid(), "content": b"private", "media_type": "text/plain", "retention_seconds": 60}
    with pytest.raises(AccessDenied):
        content.publish(*args, **kwargs)
    tenant.member(tenant.reader, projects={tenant.project: ["content_write"]})
    with pytest.raises(AccessDenied):
        content.publish(tenant.reader, *args[1:], **kwargs)
    tenant.store.command(tenant.admin, tenant.command(content_mode="explicit"))
    result = content.publish(tenant.reader, *args[1:], **kwargs)
    assert result["revision"] == 1
    for principal in (tenant.reader, tenant.admin):
        with pytest.raises(AccessDenied):
            content.read(principal, *args[1:])


def test_pages_authenticate_complete_ciphertext_and_metadata_never_discloses_content(content_fixture):
    tenant, content = content_fixture
    data = ("private Ω🙂 line\n" * 4000).encode()
    args, kwargs, result = publish(content_fixture, data=data)
    row = sql(tenant, "SELECT * FROM sonn_governance.content_objects WHERE content_id=%s", (args[-1],))[0]
    assert bytes(row["ciphertext"]) != data and data[:100] not in bytes(row["ciphertext"])
    assert len(row["ciphertext"]) == len(data) + 16
    assert content.inspect(*args) == result
    assert set(result) == {"content_id", "revision", "created_at", "expires_at", "held", "deleted_at"}
    offset, observed = 0, b""
    while True:
        page = content.read(*args, offset=offset, limit=4093)
        observed += page["data"]
        assert page["sha256"] == hashlib.sha256(data).hexdigest()
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert observed == data
    assert content.read(*args, offset=len(data))["data"] == b""
    damaged = bytes(row["ciphertext"][:-1]) + bytes([row["ciphertext"][-1] ^ 1])
    sql(tenant, "UPDATE sonn_governance.content_objects SET ciphertext=%s WHERE content_id=%s", (damaged, args[-1]))
    with pytest.raises(ContentUnavailable):
        content.read(*args, limit=1)
    receipts = sql(tenant, "SELECT result FROM sonn_governance.content_receipts WHERE command_id=%s", (kwargs["command_id"],))
    assert receipts == [{"result": result}]


@pytest.mark.parametrize("change", ["hash", "media", "key", "swap"])
def test_authenticated_metadata_and_key_failures_never_return_partial_bytes(content_fixture, change):
    tenant, content = content_fixture
    args, _, _ = publish(content_fixture)
    if change == "hash":
        sql(tenant, "UPDATE sonn_governance.content_objects SET content_sha256=%s WHERE content_id=%s", ("0" * 64, args[-1]))
    elif change == "media":
        sql(tenant, "UPDATE sonn_governance.content_objects SET media_type='image/png' WHERE content_id=%s", (args[-1],))
    elif change == "key":
        content.keys = ContentKeys({"replacement": b"r" * 32}, "replacement")
    else:
        other, _, _ = publish(content_fixture)
        # Equal-length plaintext and identical key still cannot move to another identity.
        rows = sql(tenant, "SELECT content_id,nonce,ciphertext FROM sonn_governance.content_objects WHERE content_id IN (%s,%s)", (args[-1], other[-1]))
        source = next(row for row in rows if str(row["content_id"]) == other[-1])
        sql(tenant, "UPDATE sonn_governance.content_objects SET ciphertext=%s WHERE content_id=%s", (source["ciphertext"], args[-1]))
    with pytest.raises(ContentUnavailable):
        content.read(*args, limit=1)


def test_current_membership_policy_and_scope_precede_exact_publish_replay(content_fixture):
    tenant, content = content_fixture
    args, kwargs, result = publish(content_fixture)
    assert content.publish(*args, **kwargs) == result
    with pytest.raises(AccessDenied):
        content.read(tenant.reader, tenant.id, tenant.other_project, args[-1])
    with pytest.raises(AccessDenied):
        content.read(tenant.reader, uid(), tenant.project, args[-1])
    tenant.store.command(tenant.admin, tenant.command(expected=1, content_mode="none"))
    for operation in (lambda: content.publish(*args, **kwargs), lambda: content.read(*args)):
        with pytest.raises(AccessDenied):
            operation()
    tenant.store.command(tenant.admin, tenant.command(expected=2, content_mode="explicit"))
    tenant.member(tenant.reader, active=False)
    for operation in (lambda: content.publish(*args, **kwargs), lambda: content.read(*args), lambda: content.inspect(*args)):
        with pytest.raises(AccessDenied):
            operation()


def test_concurrent_publish_creates_one_receipt_and_conflicting_retries_do_not_mutate(content_fixture):
    tenant, content = content_fixture
    args = (tenant.reader, tenant.id, tenant.project, uid())
    kwargs = {"command_id": uid(), "content": b"same", "media_type": "text/plain", "retention_seconds": 3600}
    barrier = threading.Barrier(4)

    def run(_):
        barrier.wait(5)
        return content.publish(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(4)))
    assert all(result == results[0] for result in results)
    for changes in ({"content": b"different"}, {"retention_seconds": 1}, {"command_id": uid()}):
        with pytest.raises(Conflict):
            content.publish(*args, **{**kwargs, **changes})
    assert sql(tenant, "SELECT count(*) AS n FROM sonn_governance.content_objects WHERE tenant_id=%s", (tenant.id,))[0]["n"] == 1
    assert sql(tenant, "SELECT count(*) AS n FROM sonn_governance.content_receipts WHERE tenant_id=%s", (tenant.id,))[0]["n"] == 1


def test_audit_failure_rolls_back_upload_and_prevents_plaintext_disclosure(content_fixture, monkeypatch):
    tenant, content = content_fixture
    args, _, _ = publish(content_fixture)

    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit failure")

    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        content.read(*args)
    with pytest.raises(RuntimeError, match="audit failure"):
        publish(content_fixture)
    assert sql(tenant, "SELECT count(*) AS n FROM sonn_governance.content_objects WHERE tenant_id=%s", (tenant.id,))[0]["n"] == 1


def test_hold_release_delete_is_revisioned_and_cannot_resurrect_plaintext(content_fixture):
    tenant, content = content_fixture
    args, kwargs, _ = publish(content_fixture)
    hold = {"command_id": uid(), "expected_revision": 1, "operation": "hold_content", "reason": "owner_request"}
    held = content.retain(*args, **hold)
    assert held["held"] and held["revision"] == 2
    assert content.retain(*args, **hold) == held
    with pytest.raises(Conflict):
        content.retain(*args, command_id=uid(), expected_revision=2, operation="delete_content")
    with pytest.raises(Conflict):
        content.retain(*args, command_id=uid(), expected_revision=1, operation="release_content_hold")
    released = content.retain(*args, command_id=uid(), expected_revision=2, operation="release_content_hold")
    assert not released["held"] and released["revision"] == 3
    delete = {"command_id": uid(), "expected_revision": 3, "operation": "delete_content"}
    deleted = content.retain(*args, **delete)
    assert deleted["deleted_at"] and deleted["revision"] == 4
    assert content.retain(*args, **delete) == deleted
    row = sql(tenant, "SELECT * FROM sonn_governance.content_objects WHERE content_id=%s", (args[-1],))[0]
    assert all(row[name] is None for name in ("ciphertext", "content_sha256", "size_bytes", "media_type"))
    for operation in (lambda: content.publish(*args, **kwargs), lambda: content.read(*args)):
        with pytest.raises(AccessDenied):
            operation()
    with pytest.raises(Conflict):
        content.publish(*args, **{**kwargs, "command_id": uid()})
    tenant.member(tenant.reader, projects={tenant.project: ["content_read"]})
    with pytest.raises(AccessDenied):
        content.retain(*args, **delete)


def test_retention_authority_does_not_grant_read_or_upload(content_fixture):
    tenant, content = content_fixture
    args, _, _ = publish(content_fixture)
    tenant.member(tenant.reader, projects={tenant.project: ["retention_admin"]})
    assert content.retain(*args, command_id=uid(), expected_revision=1, operation="hold_content", reason="security_review")["held"]
    with pytest.raises(AccessDenied):
        content.read(*args)
    with pytest.raises(AccessDenied):
        publish(content_fixture)


@pytest.mark.parametrize("expiry", ["object", "principal"])
def test_expiry_during_audit_prevents_return_after_successful_decryption(content_fixture, monkeypatch, expiry):
    tenant, content = content_fixture
    args, _, _ = publish(content_fixture, retention_seconds=1 if expiry == "object" else 3600)
    if expiry == "principal":
        args = (replace(args[0], expires_at=time.time() + 1), *args[1:])
    original = tenant.store._audit

    def delay(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(1.1)
        return result

    monkeypatch.setattr(tenant.store, "_audit", delay)
    with pytest.raises(AccessDenied):
        content.read(*args)


def test_rotation_retains_old_read_keys_and_writes_with_new_key(content_fixture):
    tenant, content = content_fixture
    old, _, _ = publish(content_fixture, data=b"old")
    content.keys = ContentKeys({"fixture-key": b"k" * 32, "next": b"n" * 32}, "next")
    new, _, _ = publish(content_fixture, data=b"new")
    assert content.read(*old)["data"] == b"old"
    assert content.read(*new)["data"] == b"new"
    rows = sql(tenant, "SELECT key_id,nonce FROM sonn_governance.content_objects WHERE tenant_id=%s", (tenant.id,))
    assert {row["key_id"] for row in rows} == {"fixture-key", "next"}
    assert len({bytes(row["nonce"]) for row in rows}) == 2


@pytest.mark.parametrize("kwargs", [{"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 65537}, {"offset": 1000000}])
def test_invalid_content_pages_do_not_disclose(content_fixture, kwargs):
    _, content = content_fixture
    args, _, _ = publish(content_fixture)
    with pytest.raises(InvalidRequest):
        content.read(*args, **kwargs)


@pytest.mark.parametrize("data", [
    b"-----BEGIN PRIVATE KEY-----\nfixture",
    b"Authorization: Bearer fixturecredentialmaterial",
    b'{"api_key":"fixturecredentialmaterial"}',
    b"password=fixturecredentialmaterial",
    b"https://account:fixturecredentialmaterial@example.test",
    b"sk-proj-abcdefghijklmnopqrstuvw",
    b"\x00binary", b"\xff",
])
def test_recognizable_secrets_and_invalid_text_never_enter_storage(content_fixture, data):
    tenant, _ = content_fixture
    with pytest.raises(InvalidRequest):
        publish(content_fixture, data=data)
    assert sql(tenant, "SELECT count(*) AS n FROM sonn_governance.content_objects WHERE tenant_id=%s", (tenant.id,))[0]["n"] == 0
