"""Upgrade proof uses a separately named database inside the disposable cluster."""

from importlib.resources import files
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from sonn_governance.models import InvalidRequest, Principal, UnsupportedVersion, digest
from sonn_governance.store import GovernanceStore, SCHEMA_VERSION, migrate
from test_store import policy, uid


@pytest.fixture
def isolated_database(database_config):
    name = "sonn_migration_fixture_" + uuid4().hex
    with psycopg.connect(database_config["owner_dsn"], autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    owner = make_conninfo(database_config["owner_dsn"], dbname=name)
    app = make_conninfo(database_config["application_dsn"], dbname=name)
    try:
        yield owner, app, database_config["application_role"]
    finally:
        # Exact generated fixture database only; never the shared configured DB.
        assert name.startswith("sonn_migration_fixture_") and len(name) == 55
        with psycopg.connect(database_config["owner_dsn"], autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_v3_upgrade_preserves_policy_receipt_and_backfills_only_proven_host_owner(isolated_database):
    owner, app, role = isolated_database
    with psycopg.connect(owner) as connection:
        for migration in ("001_initial.sql", "002_hosts.sql", "003_content.sql"):
            connection.execute(files("sonn_governance").joinpath("migrations", migration).read_text("utf-8"))
    tenant_id, project_id = uid(), uid()
    administrator = Principal("https://migration.example", uid(), time.time() + 300)
    # Seed the actual v3 bootstrap shape, before membership_approve existed;
    # using today's bootstrap here would fabricate historical schema behavior.
    with psycopg.connect(owner) as connection:
        connection.execute("INSERT INTO sonn_governance.tenants(tenant_id) VALUES(%s)", (tenant_id,))
        connection.execute("INSERT INTO sonn_governance.memberships VALUES(%s,%s,true,0)", (tenant_id, administrator.actor_id))
        connection.execute("INSERT INTO sonn_governance.projects(tenant_id,project_id) VALUES(%s,%s)", (tenant_id, project_id))
        connection.execute("INSERT INTO sonn_governance.tenant_grants VALUES(%s,%s,'membership_admin'),(%s,%s,'policy_admin')",
                           (tenant_id, administrator.actor_id, tenant_id, administrator.actor_id))
    known_host, unknown_host = uid(), uid()
    with psycopg.connect(owner) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        connection.execute("INSERT INTO sonn_governance.policies VALUES(%s,%s,1,%s,%s)",
                           (tenant_id, project_id, Jsonb(policy()), digest(policy())))
        connection.execute("UPDATE sonn_governance.projects SET policy_revision=1 WHERE tenant_id=%s", (tenant_id,))
        for index, host_id in enumerate((known_host, unknown_host)):
            connection.execute(
                "INSERT INTO sonn_governance.hosts VALUES(%s,%s,%s,%s,'pending',1,1,%s,clock_timestamp()+interval '5 minutes',NULL)",
                (tenant_id, project_id, host_id, digest(index), digest("challenge")),
            )
        audit = connection.execute(
            "INSERT INTO sonn_governance.audit(tenant_id,actor_id,operation,project_id) "
            "VALUES(%s,%s,'enroll_host',%s) RETURNING audit_id", (tenant_id, administrator.actor_id, project_id),
        ).fetchone()[0]
        retained = {"host_id": known_host, "state": "pending", "revision": 1, "challenge": "fixture"}
        connection.execute("INSERT INTO sonn_governance.command_receipts VALUES(%s,%s,'governance.v1',%s,%s,%s,%s)",
                           (tenant_id, administrator.actor_id, uid(), digest(retained), Jsonb(retained), audit))
    migrate(owner, application_role=role)
    GovernanceStore(app)
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT version FROM sonn_governance.schema_version").fetchone()[0] == SCHEMA_VERSION
        rows = dict(connection.execute("SELECT host_id::text,enrolled_by FROM sonn_governance.hosts").fetchall())
        assert rows == {known_host: administrator.actor_id, unknown_host: None}
        assert connection.execute("SELECT result FROM sonn_governance.command_receipts").fetchone()[0] == retained
        assert connection.execute("SELECT document FROM sonn_governance.policies").fetchone()[0] == policy()
        forced = connection.execute("SELECT relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                    "WHERE n.nspname='sonn_governance' AND c.relname IN ('hosts','command_receipts')").fetchall()
        assert forced == [(True,), (True,)]


def test_unknown_schema_is_rejected_without_mutation(isolated_database):
    owner, app, role = isolated_database
    migrate(owner, application_role=role)
    with psycopg.connect(owner) as connection:
        connection.execute("UPDATE sonn_governance.schema_version SET version=999")
    with pytest.raises(UnsupportedVersion):
        migrate(owner, application_role=role)
    with pytest.raises(UnsupportedVersion):
        GovernanceStore(app)
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT version FROM sonn_governance.schema_version").fetchone()[0] == 999


@pytest.mark.parametrize("table,column", [("command_receipts", "result"), ("scim_credentials", "active")])
def test_column_only_mutation_privilege_is_rejected(isolated_database, table, column):
    owner, app, role = isolated_database
    migrate(owner, application_role=role)
    GovernanceStore(app)
    with psycopg.connect(owner) as connection:
        connection.execute(sql.SQL("GRANT UPDATE({}) ON sonn_governance.{} TO {}").format(
            sql.Identifier(column), sql.Identifier(table), sql.Identifier(role)))
    with pytest.raises(InvalidRequest, match="unsafe immutable-record privileges"):
        GovernanceStore(app)


@pytest.mark.parametrize("table,column", [("owner_effects", "semantics_sha256"), ("worker_slots", "host_id"), ("tool_admissions", "request_id")])
def test_mutable_observation_cannot_grant_identity_rewrite(isolated_database, table, column):
    owner, app, role = isolated_database
    migrate(owner, application_role=role)
    GovernanceStore(app)
    with psycopg.connect(owner) as connection:
        connection.execute(sql.SQL("GRANT UPDATE({}) ON sonn_governance.{} TO {}").format(
            sql.Identifier(column), sql.Identifier(table), sql.Identifier(role)))
    with pytest.raises(InvalidRequest, match="unsafe resource identity privileges"):
        GovernanceStore(app)
