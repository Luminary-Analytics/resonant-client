"""Actual pg_dump/pg_restore proof in three fresh disposable databases."""
# ruff: noqa: F811 -- imported fixtures create isolated source and archive DBs.

from copy import deepcopy
import json
import os
import secrets
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import pytest

from sonn_governance.content import ContentKeys, ContentStore
from sonn_governance.models import AccessDenied, Conflict, InvalidRequest
from sonn_governance.restore import database_identity, inspect_seal, prepare_target, reconcile, seal_source
from sonn_governance.store import GovernanceStore, migrate
from test_archive import archive_fixture  # noqa: F401
from test_migrations import isolated_database  # noqa: F401
from test_store import uid


def pg_tool(name, dsn, *args):
    executable = shutil.which(name)
    if not executable:
        pytest.skip(f"actual {name} executable is required")
    values = conninfo_to_dict(dsn)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for key in ("host", "port", "user", "password", "dbname", "sslmode"):
        if key in values:
            environment[{"dbname": "PGDATABASE"}.get(key, "PG" + key.upper())] = values[key]
    command = [executable, *args]
    if name == "pg_restore":
        command += ["--dbname", values["dbname"]]
    result = subprocess.run(command, env=environment, capture_output=True, timeout=30)
    errors = [line for line in result.stderr.decode(errors="replace").splitlines() if line.startswith("pg_restore: error:") or line.startswith("pg_dump: error:")]
    diagnostic = "\n".join(errors)[:1200].replace(values.get("password", "not-a-real-secret"), "[redacted]")
    assert result.returncode == 0, f"{name} failed: {diagnostic}"


@pytest.fixture
def lane(archive_fixture, tmp_path):
    f = archive_fixture
    t = f.tenant
    t.member(t.reader, projects={t.project: ["content_read", "content_write", "retention_admin"]})
    t.store.command(t.admin, t.command(content_mode="explicit"))
    content = ContentStore(t.store, ContentKeys({"fixture": b"k" * 32}, "fixture"))
    ids = {key: uid() for key in ("deleted", "held", "released_deleted", "kept", "absent")}

    def publish(key):
        content.publish(t.reader, t.id, t.project, ids[key], command_id=uid(), content=("private " + key).encode(),
                        media_type="text/plain", retention_seconds=3600)

    def retain(key, revision, operation, reason=None):
        content.retain(t.reader, t.id, t.project, ids[key], command_id=uid(), expected_revision=revision,
                       operation=operation, reason=reason)

    for key in ("deleted", "held", "released_deleted", "kept"):
        publish(key)
    retain("released_deleted", 1, "hold_content", "owner_request")
    dump = tmp_path / "encrypted-backup.dump"
    pg_tool("pg_dump", f.owner, "--format=custom", "--schema=sonn_governance", "--file", str(dump))
    retain("deleted", 1, "delete_content")
    retain("held", 1, "hold_content", "legal_review")
    retain("released_deleted", 2, "release_content_hold")
    retain("released_deleted", 3, "delete_content")
    publish("absent")
    retain("absent", 1, "delete_content")
    while f.relay.drain(t.id)["delivered"]:
        pass
    suffix = uuid4().hex
    reader = "sonn_restore_reader_" + suffix
    target_name = "sonn_restore_fixture_" + suffix
    password = secrets.token_urlsafe(32)
    with psycopg.connect(f.owner, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD {}").format(sql.Identifier(reader), sql.Literal(password)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
    with psycopg.connect(f.archive_owner) as connection:
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA sonn_archive TO {}").format(sql.Identifier(reader)))
        connection.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA sonn_archive TO {}").format(sql.Identifier(reader)))
    target = make_conninfo(f.owner, dbname=target_name)
    target_app = make_conninfo(f.app, dbname=target_name)
    archive_reader = make_conninfo(f.archive_owner, user=reader, password=password)
    values = SimpleNamespace(f=f, t=t, ids=ids, content=content, target=target, target_app=target_app,
        archive_reader=archive_reader, source_identity=database_identity(f.owner), target_identity=database_identity(target),
        restore_id=uid(), seal_id=uid(), dump=dump)
    try:
        yield values
    finally:
        assert target_name == "sonn_restore_fixture_" + suffix and len(suffix) == 32
        with psycopg.connect(f.owner, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_name)))
        with psycopg.connect(f.archive_owner) as connection:
            connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA sonn_archive FROM {}").format(sql.Identifier(reader)))
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA sonn_archive FROM {}").format(sql.Identifier(reader)))
        with psycopg.connect(f.owner, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(reader)))


def seal(lane):
    return seal_source(lane.f.owner, lane.archive_reader, expected_database=lane.source_identity,
        tenant_id=lane.t.id, archive_id=lane.f.archive_id, source_id=lane.f.source_id, seal_id=lane.seal_id)


def restore(lane):
    prepare_target(lane.target, expected_database=lane.target_identity, restore_id=lane.restore_id)
    with pytest.raises(psycopg.OperationalError):
        GovernanceStore(lane.target_app)
    pg_tool("pg_restore", lane.target, "--exit-on-error", "--no-owner", str(lane.dump))


def apply(lane, saved):
    return reconcile(lane.target, lane.archive_reader, expected_database=lane.target_identity,
        restore_id=lane.restore_id, checkpoint=saved["checkpoint"], checkpoint_sha256=saved["sha256"],
        tenant_id=lane.t.id, archive_id=lane.f.archive_id, source_id=lane.f.source_id)


def snapshot(lane):
    with psycopg.connect(lane.target, row_factory=dict_row) as connection:
        rows = connection.execute("SELECT * FROM sonn_governance.content_objects ORDER BY content_id").fetchall()
        sequence = connection.execute("SELECT last_value,is_called FROM sonn_governance.audit_audit_id_seq").fetchone()
        receipts = connection.execute("SELECT count(*) AS count FROM sonn_restore.reconciliations").fetchone()
        return rows, sequence, receipts


def test_actual_restore_reconciles_deletion_preserves_unknown_hold_and_never_reopens(lane):
    saved = seal(lane)
    assert inspect_seal(lane.f.owner, expected_database=lane.source_identity) == saved
    assert "legal_review" not in json.dumps(saved) and "private" not in json.dumps(saved)
    with pytest.raises(psycopg.OperationalError):
        lane.content.read(lane.t.reader, lane.t.id, lane.t.project, lane.ids["kept"])
    with pytest.raises(psycopg.OperationalError):
        GovernanceStore(lane.f.app)
    with pytest.raises(AccessDenied, match="quarantined"):
        migrate(lane.f.owner, application_role=lane.f.app_role)
    restore(lane)
    old_sequence = snapshot(lane)[1]
    result = apply(lane, saved)
    assert result["quarantined"] and result["deleted"] == 3 and result["absent_objects"] == 1
    assert result["held_with_reason_unresolved"] == 1
    rows, sequence, _ = snapshot(lane)
    assert sequence["last_value"] == saved["checkpoint"]["audit_sequence_next"] and not sequence["is_called"]
    assert old_sequence["last_value"] < sequence["last_value"]
    by_id = {str(row["content_id"]): row for row in rows}
    for key in ("deleted", "released_deleted"):
        row = by_id[lane.ids[key]]
        assert row["ciphertext"] is None and row["content_sha256"] is None and row["deleted_at"] and not row["held"]
    held = by_id[lane.ids["held"]]
    assert held["ciphertext"] is not None and held["hold_reason"] is None and held["hold_actor"] is None
    before = snapshot(lane)
    assert apply(lane, saved) == result and snapshot(lane) == before
    with psycopg.connect(lane.target, row_factory=dict_row) as connection:
        proof = connection.execute("SELECT * FROM sonn_restore.retention WHERE content_id=%s", (lane.ids["held"],)).fetchone()
        assert proof["state"] == "held" and proof["evidence"]["operation"] == "hold_content"
        assert "reason" not in proof["evidence"]
        assert connection.execute("SELECT state FROM sonn_restore.retention WHERE content_id=%s", (lane.ids["absent"],)).fetchone()["state"] == "deleted"
    # Even a stale service binary without the Python quarantine gate cannot
    # connect with the old runtime credential. Owner SQL separately proves the
    # retained hold/absent-identity mutation guard.
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(lane.target_app)
    with psycopg.connect(lane.target) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="quarantined"):
            connection.execute("UPDATE sonn_governance.content_objects SET ciphertext=NULL WHERE content_id=%s", (lane.ids["held"],))
    with pytest.raises(psycopg.OperationalError):
        GovernanceStore(lane.target_app)
    with pytest.raises(AccessDenied, match="quarantined"):
        GovernanceStore(lane.target)


@pytest.mark.parametrize("damage", ["missing", "tampered", "foreign_pin", "sequence"])
def test_failed_seal_leaves_source_unmodified(lane, damage):
    if damage == "missing":
        with psycopg.connect(lane.f.archive_owner) as connection:
            connection.execute("DELETE FROM sonn_archive.records WHERE audit_id=(SELECT max(audit_id) FROM sonn_archive.records)")
    elif damage == "tampered":
        with psycopg.connect(lane.f.archive_owner) as connection:
            connection.execute("UPDATE sonn_archive.records SET payload='{}'::bytea WHERE audit_id=(SELECT max(audit_id) FROM sonn_archive.records)")
    elif damage == "foreign_pin":
        lane.f.source_id = uid()
    else:
        with psycopg.connect(lane.f.owner) as connection:
            connection.execute("ALTER SEQUENCE sonn_governance.audit_audit_id_seq RESTART WITH 1")
    with pytest.raises((Conflict, InvalidRequest)):
        seal(lane)
    with psycopg.connect(lane.f.owner) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone() is None
    GovernanceStore(lane.f.app)


@pytest.mark.parametrize("damage", ["missing", "tampered", "changed_checkpoint", "sequence", "content_revision"])
def test_failed_reconciliation_keeps_restore_and_sequence_unchanged(lane, damage):
    saved = seal(lane)
    restore(lane)
    if damage in {"missing", "tampered"}:
        with psycopg.connect(lane.f.archive_owner) as connection:
            if damage == "missing":
                connection.execute("DELETE FROM sonn_archive.records WHERE audit_id=(SELECT max(audit_id) FROM sonn_archive.records)")
            else:
                connection.execute("UPDATE sonn_archive.records SET payload='{}'::bytea WHERE audit_id=(SELECT max(audit_id) FROM sonn_archive.records)")
    elif damage == "changed_checkpoint":
        saved = deepcopy(saved)
        saved["checkpoint"]["records"] = saved["checkpoint"]["records"][:-1]
    elif damage == "sequence":
        with psycopg.connect(lane.target) as connection:
            connection.execute("ALTER SEQUENCE sonn_governance.audit_audit_id_seq RESTART WITH 1")
    else:
        with psycopg.connect(lane.target) as connection:
            connection.execute("UPDATE sonn_governance.content_objects SET revision=revision+1 WHERE content_id=%s", (lane.ids["held"],))
    before = snapshot(lane)
    with pytest.raises((Conflict, InvalidRequest)):
        apply(lane, saved)
    assert snapshot(lane) == before
    with pytest.raises(psycopg.OperationalError):
        GovernanceStore(lane.target_app)


def test_interrupted_apply_rolls_back_tombstones_receipt_and_sequence(lane, monkeypatch):
    import sonn_governance.restore as module
    saved = seal(lane)
    restore(lane)
    original = module._archive
    calls = 0
    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture connection interruption before commit")
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "_archive", interrupt)
    before = snapshot(lane)
    with pytest.raises(RuntimeError, match="interruption"):
        apply(lane, saved)
    assert snapshot(lane) == before
    monkeypatch.undo()
    assert apply(lane, saved)["deleted"] == 3


def test_archive_failure_after_staging_seal_rolls_back_quarantine(lane, monkeypatch):
    import sonn_governance.restore as module
    original = module._archive
    calls = 0
    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        rows = original(*args, **kwargs)
        return rows[:-1] if calls == 2 else rows
    monkeypatch.setattr(module, "_archive", changed)
    with pytest.raises(Conflict, match="during source sealing"):
        seal(lane)
    with psycopg.connect(lane.f.owner) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone() is None
    assert lane.content.read(lane.t.reader, lane.t.id, lane.t.project, lane.ids["kept"])["data"] == b"private kept"


def test_observed_late_old_binary_connection_refuses_seal_without_terminating_it(lane, monkeypatch):
    import sonn_governance.restore as module
    original = module._archive
    calls, late = 0, None
    def connect_late(*args, **kwargs):
        nonlocal calls, late
        calls += 1
        rows = original(*args, **kwargs)
        if calls == 2:
            # The uncommitted CONNECT revocation cannot prevent this connection.
            # Operator network isolation is still a mandatory precondition.
            late = psycopg.connect(lane.f.app)
        return rows
    monkeypatch.setattr(module, "_archive", connect_late)
    try:
        with pytest.raises(Conflict, match="clients"):
            seal(lane)
        assert late is not None and not late.closed
        assert late.execute("SELECT 1").fetchone()[0] == 1
    finally:
        if late:
            late.close()
    with psycopg.connect(lane.f.owner) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone() is None
    GovernanceStore(lane.f.app)


def test_exact_archive_set_refuses_new_record_after_seal(lane):
    saved = seal(lane)
    restore(lane)
    # Custodian/old-source divergence cannot be dismissed as a harmless later
    # watermark: this may be a deletion missing from the pinned checkpoint.
    with psycopg.connect(lane.f.archive_owner) as connection:
        original = connection.execute("SELECT payload FROM sonn_archive.records ORDER BY audit_id DESC LIMIT 1").fetchone()[0]
    value = json.loads(bytes(original))
    value["audit_id"] += 100
    encoded = json.dumps(value).encode()
    import hashlib
    lane.f.archive.append(lane.t.id, value["audit_id"], encoded, hashlib.sha256(encoded).hexdigest())
    before = snapshot(lane)
    with pytest.raises(Conflict, match="later"):
        apply(lane, saved)
    assert snapshot(lane) == before


def test_seal_refuses_existing_client_and_wrong_database_identity(lane):
    with psycopg.connect(lane.f.app):
        with pytest.raises(Conflict, match="clients"):
            seal(lane)
    lane.source_identity = {**lane.source_identity, "database": "different-explicit-database"}
    with pytest.raises(Conflict, match="identity"):
        seal(lane)


def test_cli_validation_hides_credentials_without_mutating_database(lane, tmp_path, capsys):
    from sonn_governance.restore_cli import main
    config = tmp_path / "protected.json"
    config.write_text(json.dumps({"profile": "test", "operator_dsn": lane.f.owner, "unexpected": "private-key-marker"}))
    assert main(["identity", "--config-file", str(config)]) == 2
    output = capsys.readouterr()
    assert "private-key-marker" not in str(output) and lane.f.owner not in str(output)
    with psycopg.connect(lane.f.owner) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone() is None


def test_cli_test_profile_pins_actual_libpq_address_without_connecting(tmp_path, monkeypatch):
    from sonn_governance.restore_cli import _configuration
    config = tmp_path / "protected.json"
    config.write_text(json.dumps({"profile": "test", "operator_dsn":
        "host=127.0.0.1 hostaddr=192.0.2.10 user=fixture dbname=fixture service=arbitrary"}))
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.20")
    result = _configuration(config, "identity")
    values = conninfo_to_dict(result["operator_dsn"])
    assert values["host"] == values["hostaddr"] == "127.0.0.1"


def test_explicit_cli_seal_writes_checkpoint_and_inspection_recovers_same_evidence(lane, tmp_path, capsys):
    from sonn_governance.restore_cli import main
    config = tmp_path / "operator-seal.json"
    config.write_text(json.dumps({"profile": "test", "source_dsn": lane.f.owner, "archive_dsn": lane.archive_reader,
        "expected_database": lane.source_identity, "tenant_id": lane.t.id, "archive_id": lane.f.archive_id,
        "source_id": lane.f.source_id, "seal_id": lane.seal_id}))
    output = tmp_path / "checkpoint.json"
    output.write_text("operator file must be preserved")
    assert main(["seal-source", "--config-file", str(config), "--output-file", str(output)]) == 2
    assert output.read_text() == "operator file must be preserved"
    with psycopg.connect(lane.f.owner) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='sonn_restore'").fetchone() is None
    output = tmp_path / "new-checkpoint.json"
    assert main(["seal-source", "--config-file", str(config), "--output-file", str(output)]) == 0
    saved = json.loads(output.read_text())
    inspection = tmp_path / "operator-inspect.json"
    inspection.write_text(json.dumps({"profile": "test", "source_dsn": lane.f.owner, "expected_database": lane.source_identity}))
    repeated = tmp_path / "inspected-checkpoint.json"
    assert main(["inspect-seal", "--config-file", str(inspection), "--output-file", str(repeated)]) == 0
    assert json.loads(repeated.read_text()) == saved
    displayed = capsys.readouterr()
    assert lane.f.owner not in str(displayed) and lane.archive_reader not in str(displayed)
