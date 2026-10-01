"""Separate real PostgreSQL databases and credentials qualify archival boundaries."""
# ruff: noqa: F811 -- imported isolated database fixture is intentionally reused.

import hashlib
import json
import secrets
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from sonn_governance.archive import AuditArchive, AuditRelay, configure_relay, initialize_archive
from sonn_governance.content import ContentKeys, ContentStore
from sonn_governance.models import Conflict, InvalidRequest
from sonn_governance.store import GovernanceStore, migrate
from test_migrations import isolated_database  # noqa: F401
from test_store import Tenant, uid


class ArchiveFixture:
    """Default repr never includes connection secrets."""


@pytest.fixture
def archive_fixture(isolated_database):
    owner, app, app_role = isolated_database
    migrate(owner, application_role=app_role)
    suffix = uuid4().hex
    database = "sonn_archive_fixture_" + suffix
    writer, relay = "sonn_archive_writer_" + suffix, "sonn_archive_relay_" + suffix
    passwords = {writer: secrets.token_urlsafe(32), relay: secrets.token_urlsafe(32)}
    with psycopg.connect(owner, autocommit=True) as connection:
        for name in (writer, relay):
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD {}").format(sql.Identifier(name), sql.Literal(passwords[name])))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    fixture = ArchiveFixture()
    fixture.owner, fixture.app, fixture.app_role = owner, app, app_role
    fixture.relay_dsn = make_conninfo(owner, user=relay, password=passwords[relay])
    fixture.archive_owner = make_conninfo(owner, dbname=database)
    fixture.writer_dsn = make_conninfo(fixture.archive_owner, user=writer, password=passwords[writer])
    fixture.archive_app = make_conninfo(app, dbname=database)
    fixture.tenant = Tenant({"owner_dsn": owner, "application_dsn": app, "application_role": app_role})
    fixture.archive_id, fixture.source_id = uid(), uid()
    fixture.relay_role = relay
    try:
        initialize_archive(fixture.archive_owner, writer_role=writer, archive_id=fixture.archive_id)
        configure_relay(owner, relay_role=relay, tenant_id=fixture.tenant.id, archive_id=fixture.archive_id, source_id=fixture.source_id, max_delay_seconds=30)
        fixture.archive = AuditArchive(fixture.writer_dsn, archive_id=fixture.archive_id, source_id=fixture.source_id)
        fixture.relay = AuditRelay(fixture.relay_dsn, fixture.archive)
        yield fixture
    finally:
        # Only exact fixture databases/roles generated above are removed. The
        # shared test cluster, primary fixture and application role are untouched.
        assert database == "sonn_archive_fixture_" + suffix and len(suffix) == 32
        with psycopg.connect(owner, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            connection.execute(sql.SQL("REVOKE ALL ON sonn_governance.audit_outbox,sonn_governance.archive_deliveries,sonn_governance.archive_requirements,sonn_governance.tenant_grants FROM {}").format(sql.Identifier(relay)))
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA sonn_governance FROM {}").format(sql.Identifier(relay)))
            for name in (writer, relay):
                assert name.endswith(suffix)
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def source_rows(fixture, table):
    with psycopg.connect(fixture.owner) as connection:
        return connection.execute(sql.SQL("SELECT * FROM sonn_governance.{} WHERE tenant_id=%s ORDER BY audit_id").format(sql.Identifier(table)), (fixture.tenant.id,)).fetchall()


def test_runtime_decision_and_outbox_commit_together_then_exact_delivery_replays(archive_fixture):
    fixture = archive_fixture
    tenant = fixture.tenant
    tenant.store.command(tenant.admin, tenant.command())
    assert len(source_rows(fixture, "audit")) == len(source_rows(fixture, "audit_outbox")) == 2
    first = fixture.relay.drain(tenant.id, limit=1)
    assert first["delivered"] == 1
    assert fixture.relay.drain(tenant.id)["delivered"] == 1
    assert fixture.relay.drain(tenant.id)["delivered"] == 0
    assert len(source_rows(fixture, "archive_deliveries")) == 2
    with psycopg.connect(fixture.archive_owner) as connection:
        records = connection.execute("SELECT payload,sha256 FROM sonn_archive.records ORDER BY audit_id").fetchall()
    assert len(records) == 2
    for payload, fingerprint in records:
        assert hashlib.sha256(payload).hexdigest() == fingerprint
        value = json.loads(payload)
        assert set(value) == {"version", "tenant_id", "audit_id", "actor_id", "operation", "project_id", "resource_revision", "decision_id", "semantics_sha256", "occurred_at"}


def test_archived_retention_records_identify_exact_deleted_object_without_content(archive_fixture):
    fixture, tenant = archive_fixture, archive_fixture.tenant
    tenant.member(tenant.reader, projects={tenant.project: ["content_write", "retention_admin"]})
    tenant.store.command(tenant.admin, tenant.command(content_mode="explicit"))
    content = ContentStore(tenant.store, ContentKeys({"fixture": b"k" * 32}, "fixture"))
    identity = uid()
    content.publish(tenant.reader, tenant.id, tenant.project, identity, command_id=uid(),
        content=b"private evidence must not be archived", media_type="text/plain", retention_seconds=60)
    content.retain(tenant.reader, tenant.id, tenant.project, identity, command_id=uid(), expected_revision=1,
        operation="delete_content")
    fixture.relay.drain(tenant.id)
    with psycopg.connect(fixture.archive_owner) as connection:
        rows = connection.execute("SELECT payload FROM sonn_archive.records WHERE tenant_id=%s ORDER BY audit_id", (tenant.id,)).fetchall()
    records = [json.loads(bytes(row[0])) for row in rows]
    retained = [record for record in records if record["operation"] in {"publish_content", "delete_content"}]
    assert [(record["decision_id"], record["resource_revision"]) for record in retained] == [(identity, 1), (identity, 2)]
    assert all(record["project_id"] == tenant.project for record in retained)
    assert b"private evidence" not in b"".join(bytes(row[0]) for row in rows)


def test_outbox_failure_rolls_back_policy_and_audit(archive_fixture):
    fixture = archive_fixture
    tenant = fixture.tenant
    with psycopg.connect(fixture.owner) as connection:
        connection.execute("ALTER TABLE sonn_governance.audit_outbox ADD CONSTRAINT fixture_reject_new CHECK(audit_id<0) NOT VALID")
    with pytest.raises(psycopg.errors.CheckViolation):
        tenant.store.command(tenant.admin, tenant.command())
    with psycopg.connect(fixture.owner) as connection:
        assert connection.execute("SELECT count(*) FROM sonn_governance.policies").fetchone()[0] == 0
    assert len(source_rows(fixture, "audit")) == len(source_rows(fixture, "audit_outbox")) == 1


def test_lost_destination_ack_is_reconciled_without_overwrite_or_duplicate(archive_fixture, monkeypatch):
    fixture = archive_fixture
    original = fixture.archive.append

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fixture dropped archive acknowledgement")

    monkeypatch.setattr(fixture.archive, "append", lose_ack)
    with pytest.raises(RuntimeError):
        fixture.relay.drain(fixture.tenant.id)
    assert source_rows(fixture, "archive_deliveries") == []
    monkeypatch.undo()
    assert fixture.relay.drain(fixture.tenant.id)["delivered"] == 1
    with psycopg.connect(fixture.archive_owner) as connection:
        assert connection.execute("SELECT count(*) FROM sonn_archive.records").fetchone()[0] == 1


def test_changed_history_cannot_replace_existing_archive_identity(archive_fixture):
    fixture = archive_fixture
    fixture.relay.drain(fixture.tenant.id)
    with psycopg.connect(fixture.archive_owner) as connection:
        audit_id, original = connection.execute("SELECT audit_id,payload FROM sonn_archive.records").fetchone()
    value = json.loads(original)
    value["operation"] = "set_policy"
    modified = json.dumps(value).encode()
    with pytest.raises(Conflict):
        fixture.archive.append(fixture.tenant.id, audit_id, modified, hashlib.sha256(modified).hexdigest())


def test_distinct_roles_cannot_rewrite_archive_or_manufacture_primary_receipts(archive_fixture):
    fixture = archive_fixture
    fixture.relay.drain(fixture.tenant.id)
    checks = [
        (fixture.writer_dsn, "DELETE FROM sonn_archive.records"),
        (fixture.writer_dsn, "UPDATE sonn_archive.records SET payload=payload"),
        (fixture.writer_dsn, "TRUNCATE sonn_archive.records"),
        (fixture.archive_app, "SELECT * FROM sonn_archive.records"),
        (fixture.relay_dsn, "DELETE FROM sonn_governance.audit_outbox"),
        (fixture.relay_dsn, "UPDATE sonn_governance.memberships SET active=false"),
        (fixture.app, "INSERT INTO sonn_governance.archive_deliveries SELECT * FROM sonn_governance.archive_deliveries"),
        (fixture.app, "ALTER TABLE sonn_governance.audit DISABLE TRIGGER audit_capture"),
    ]
    for dsn, statement in checks:
        with psycopg.connect(dsn) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
            connection.rollback()
    with pytest.raises(InvalidRequest):
        AuditArchive(fixture.archive_owner, archive_id=fixture.archive_id, source_id=fixture.source_id)
    with pytest.raises(InvalidRequest):
        AuditRelay(fixture.app, fixture.archive)


def test_required_archive_lag_blocks_plaintext_disclosure_then_recovers_after_relay(archive_fixture):
    fixture, tenant = archive_fixture, archive_fixture.tenant
    tenant.member(tenant.reader, projects={tenant.project: ["content_write", "content_read", "metadata_read"]})
    tenant.store.command(tenant.admin, tenant.command(content_mode="explicit"))
    content = ContentStore(tenant.store, ContentKeys({"fixture": b"f" * 32}, "fixture"))
    args = (tenant.reader, tenant.id, tenant.project, uid())
    content.publish(*args, command_id=uid(), content=b"private fixture evidence", media_type="text/plain", retention_seconds=3600)
    with psycopg.connect(fixture.owner) as connection:
        connection.execute("UPDATE sonn_governance.audit_outbox SET queued_at=clock_timestamp()-interval '60 seconds' WHERE tenant_id=%s", (tenant.id,))
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        content.read(*args)
    # Operational metadata is still available to diagnose the closed gate.
    assert content.inspect(*args)["revision"] == 1
    fixture.relay.drain(tenant.id)
    assert content.read(*args)["data"] == b"private fixture evidence"


def test_owner_effect_archive_failure_retains_cleanup_but_denies_new_dispatch(archive_fixture):
    from sonn_governance.hosts import HostGovernance
    from sonn_governance.managed_resources import ManagedResources
    from sonn_governance.monitoring import RunMonitoring
    from test_managed_resources import participant

    fixture, tenant = archive_fixture, archive_fixture.tenant
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(policy_version=2, allowed_effects=["candidate_check"]))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    resources = ManagedResources(hosts)
    principal, _, lease, binding = participant((tenant, hosts, resources, RunMonitoring(hosts)))
    values = {"lease_id": lease["lease_id"], "binding_id": binding["binding_id"], "effect_id": uid(),
        "kind": "candidate_check", "semantics_sha256": "a" * 64}
    resources.authorize_effect(principal, **values)
    with psycopg.connect(fixture.owner) as connection:
        connection.execute("UPDATE sonn_governance.audit_outbox SET queued_at=clock_timestamp()-interval '60 seconds' WHERE tenant_id=%s", (tenant.id,))
    next_effect = {**values, "effect_id": uid()}
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        resources.authorize_effect(principal, **next_effect)
    assert resources.observe_effect(principal, effect_id=values["effect_id"], outcome="failed")["state"] == "failed"
    fixture.relay.drain(tenant.id)
    assert resources.authorize_effect(principal, **next_effect)["dispatch_permitted"]


def test_runtime_rejects_accidental_archive_write_grants(archive_fixture):
    fixture = archive_fixture
    GovernanceStore(fixture.app)
    with psycopg.connect(fixture.owner) as connection:
        connection.execute(sql.SQL("GRANT UPDATE(sha256) ON sonn_governance.audit_outbox TO {}").format(sql.Identifier(fixture.app_role)))
    with pytest.raises(InvalidRequest):
        GovernanceStore(fixture.app)


def test_operator_relay_cli_uses_separate_credentials_without_printing_them(archive_fixture, tmp_path):
    fixture = archive_fixture
    config = {"source_dsn": fixture.relay_dsn, "archive_dsn": fixture.writer_dsn, "source_id": fixture.source_id,
              "archive_id": fixture.archive_id, "tenant_ids": [fixture.tenant.id], "profile": "test", "interval_seconds": 1}
    path = tmp_path / "private-relay.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "sonn_governance.archive_main", "--config-file", str(path), "--once"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"delivered": 1, "tenants_checked": 1}
    assert not result.stderr
    assert fixture.relay_dsn not in result.stdout and fixture.writer_dsn not in result.stdout
    config["profile"] = "production"
    path.write_text(json.dumps(config), encoding="utf-8")
    refused = subprocess.run([sys.executable, "-m", "sonn_governance.archive_main", "--config-file", str(path), "--once"],
                             capture_output=True, text=True, timeout=20)
    assert refused.returncode == 2
    assert fixture.writer_dsn not in refused.stderr and fixture.relay_dsn not in refused.stderr


def test_relay_cannot_also_hold_authorization_grant_mutation_privilege(archive_fixture):
    fixture = archive_fixture
    with psycopg.connect(fixture.owner) as connection:
        connection.execute(sql.SQL("GRANT INSERT ON sonn_governance.tenant_grants TO {}").format(sql.Identifier(fixture.relay_role)))
    with pytest.raises(InvalidRequest):
        AuditRelay(fixture.relay_dsn, fixture.archive)


def test_valid_archive_with_wrong_source_cannot_acknowledge_required_destination(archive_fixture):
    fixture = archive_fixture
    alternate = AuditArchive(fixture.writer_dsn, archive_id=fixture.archive_id, source_id=uid())
    relay = AuditRelay(fixture.relay_dsn, alternate)
    with pytest.raises(InvalidRequest):
        relay.drain(fixture.tenant.id)
    assert source_rows(fixture, "archive_deliveries") == []
    with pytest.raises(Conflict):
        configure_relay(fixture.owner, relay_role=fixture.relay_role, tenant_id=fixture.tenant.id,
                        archive_id=uid(), source_id=fixture.source_id)
    assert fixture.relay.drain(fixture.tenant.id)["delivered"] == 1


def test_restored_receipt_with_unknown_source_fails_visibly(archive_fixture):
    fixture = archive_fixture
    fixture.relay.drain(fixture.tenant.id)
    with psycopg.connect(fixture.owner) as connection:
        connection.execute("UPDATE sonn_governance.archive_deliveries SET source_id=NULL WHERE tenant_id=%s", (fixture.tenant.id,))
    with pytest.raises(Conflict, match="custodian reconciliation"):
        fixture.relay.drain(fixture.tenant.id)
