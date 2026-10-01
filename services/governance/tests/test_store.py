"""Independent real PostgreSQL connections exercise authoritative decisions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import errors
import pytest

from sonn_governance.models import (
    AccessDenied, CommandEnvelope, Conflict, InvalidRequest, Principal, Query,
    UnsupportedVersion,
)
from sonn_governance.store import GovernanceStore, bootstrap, migrate


def uid():
    return str(uuid4())


def policy(**overrides):
    return {"policy_version": 1, "allowed_models": [{"provider": "ollama", "model": "fixture"}],
            "allowed_tools": ["file_read"], "max_workers": 2, "request_limit": 20,
            "content_mode": "none", **overrides}


class Tenant:
    def __init__(self, config):
        self.config = config
        self.id, self.project, self.other_project = uid(), uid(), uid()
        self.admin = Principal("https://issuer.example", uid(), time.time() + 3600)
        self.reader = Principal("https://issuer.example", uid(), time.time() + 3600)
        self.revision = 0
        bootstrap(config["owner_dsn"], tenant_id=self.id, administrator=self.admin,
                  project_ids=[self.project, self.other_project])
        self.store = GovernanceStore(config["application_dsn"])

    def member(self, principal, *, tenant=(), projects=None, active=True):
        command = CommandEnvelope(1, uid(), self.id, None, self.revision, "set_membership", {
            "actor_id": principal.actor_id, "active": active, "tenant_permissions": list(tenant),
            "project_permissions": projects or {},
        })
        result = self.store.command(self.admin, command)
        self.revision = result["revision"]
        return command

    def command(self, *, project=None, expected=0, **policy_args):
        return CommandEnvelope(1, uid(), self.id, project or self.project, expected, "set_policy",
                               {"policy": policy(**policy_args)})

    def counts(self):
        with psycopg.connect(self.config["owner_dsn"]) as connection:
            connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (self.id,))
            return tuple(connection.execute(
                f"SELECT count(*) FROM sonn_governance.{name} WHERE tenant_id=%s", (self.id,)
            ).fetchone()[0] for name in ("policies", "command_receipts", "audit"))


@pytest.fixture
def tenant(database_config):
    return Tenant(database_config)


def test_policy_commit_replay_and_metadata_audit_are_exact(tenant):
    command = tenant.command()
    result = tenant.store.command(tenant.admin, command)
    assert result["revision"] == 1
    assert tenant.store.command(tenant.admin, command) == result
    assert tenant.counts() == (1, 1, 2)
    view = tenant.store.query(tenant.admin, Query(tenant.id, tenant.project, "policy"))
    assert view["policy"] == policy()
    assert view["revision"] == 1
    assert tenant.counts() == (1, 1, 3)
    with pytest.raises(Conflict):
        tenant.store.command(tenant.admin, replace(command, payload={"policy": policy(max_workers=1)}))
    with pytest.raises(Conflict):
        tenant.store.command(tenant.admin, replace(command, expected_revision=1))
    with pytest.raises(Conflict):
        tenant.store.command(tenant.admin, replace(command, project_id=tenant.other_project))


def test_project_permissions_and_separate_admin_content_control_audit(tenant):
    for kind in ("metadata", "audit"):
        with pytest.raises(AccessDenied):
            tenant.store.query(tenant.admin, Query(tenant.id, tenant.project, kind))
    tenant.member(tenant.reader, projects={tenant.project: ["metadata_read"]})
    result = tenant.store.query(tenant.reader, Query(tenant.id, tenant.project, "metadata"))
    assert set(result) == {"tenant_id", "project_id", "active", "policy_revision"}
    for project, kind in ((tenant.other_project, "metadata"), (tenant.project, "policy"),
                          (tenant.project, "audit")):
        with pytest.raises(AccessDenied):
            tenant.store.query(tenant.reader, Query(tenant.id, project, kind))
    with pytest.raises(AccessDenied):
        tenant.store.command(tenant.reader, tenant.command())
    tenant.member(tenant.reader, projects={tenant.project: ["content_read", "control_execute"]})
    with pytest.raises(AccessDenied):
        tenant.store.query(tenant.reader, Query(tenant.id, tenant.project, "metadata"))
    with pytest.raises(InvalidRequest):
        tenant.store.query(tenant.reader, Query(tenant.id, tenant.project, "content"))
    with pytest.raises(AccessDenied):
        tenant.store.command(tenant.reader, tenant.command())


def test_replay_reauthorizes_revoked_membership_and_permissions(tenant):
    tenant.member(tenant.reader, projects={tenant.project: ["policy_admin"]})
    command = tenant.command()
    result = tenant.store.command(tenant.reader, command)
    tenant.member(tenant.reader, projects={tenant.project: ["metadata_read"]})
    with pytest.raises(AccessDenied):
        tenant.store.command(tenant.reader, command)
    tenant.member(tenant.reader, projects={tenant.project: ["policy_admin"]})
    assert tenant.store.command(tenant.reader, command) == result
    tenant.member(tenant.reader, projects={tenant.project: ["policy_admin"]}, active=False)
    with pytest.raises(AccessDenied):
        tenant.store.command(tenant.reader, command)


def test_cross_tenant_and_unknown_resources_have_same_denial(tenant, database_config):
    other = Tenant(database_config)
    foreign = other.command()
    other.store.command(other.admin, foreign)
    for query in (Query(other.id, other.project, "metadata"), Query(uid(), uid(), "metadata"),
                  Query(tenant.id, other.project, "policy")):
        with pytest.raises(AccessDenied, match="^resource is unavailable$"):
            tenant.store.query(tenant.admin, query)
    with pytest.raises(AccessDenied, match="^resource is unavailable$"):
        tenant.store.command(tenant.admin, foreign)
    before = tenant.counts()
    with pytest.raises(AccessDenied):
        tenant.member(tenant.reader, projects={other.project: ["metadata_read"]})
    assert tenant.counts() == before


def test_same_command_id_is_scoped_to_authenticated_actor(tenant):
    tenant.member(tenant.reader, tenant=["policy_admin"])
    first = tenant.command()
    result = tenant.store.command(tenant.admin, first)
    second = replace(first, expected_revision=1, payload={"policy": policy(request_limit=7)})
    next_result = tenant.store.command(tenant.reader, second)
    assert next_result["revision"] == 2
    assert next_result["decision_id"] != result["decision_id"]
    assert tenant.store.command(tenant.admin, first) == result


def test_expired_principal_and_identity_collision_do_not_replay(tenant):
    command = tenant.command()
    tenant.store.command(tenant.admin, command)
    for principal in (replace(tenant.admin, expires_at=time.time() - 1),
                      replace(tenant.admin, issuer="https://different-issuer.example"),
                      replace(tenant.admin, subject="somebody-else")):
        with pytest.raises(AccessDenied):
            tenant.store.command(principal, command)
    with pytest.raises(InvalidRequest):
        replace(tenant.admin, kind="host")


def test_atomic_rollback_if_required_audit_fails(tenant, monkeypatch):
    before = tenant.counts()
    command = tenant.command()
    original = tenant.store._audit

    def fail(*args, **kwargs):
        raise RuntimeError("fixture audit unavailable")

    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        tenant.store.command(tenant.admin, command)
    assert tenant.counts() == before
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert tenant.store.command(tenant.admin, command)["revision"] == 1


def test_parallel_expected_revision_has_one_winner(tenant):
    commands = [tenant.command(request_limit=value) for value in (10, 11)]
    barrier = threading.Barrier(2)

    def run(command):
        barrier.wait(timeout=5)
        try:
            return tenant.store.command(tenant.admin, command)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, commands))
    assert sum(result is not None for result in results) == 1
    assert tenant.counts() == (1, 1, 2)


def test_parallel_same_key_returns_one_durable_decision(tenant):
    command = tenant.command()
    barrier = threading.Barrier(4)

    def run(_):
        barrier.wait(timeout=5)
        return tenant.store.command(tenant.admin, command)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(4)))
    assert all(result == results[0] for result in results)
    assert tenant.counts() == (1, 1, 2)


def test_revocation_linearizes_with_in_flight_decision(tenant, monkeypatch):
    tenant.member(tenant.reader, projects={tenant.project: ["policy_admin"]})
    command = tenant.command()
    admitted, release = threading.Event(), threading.Event()
    original = tenant.store._audit

    def delay(connection, principal, *args, **kwargs):
        if principal == tenant.reader:
            admitted.set()
            assert release.wait(5)
        return original(connection, principal, *args, **kwargs)

    monkeypatch.setattr(tenant.store, "_audit", delay)
    with ThreadPoolExecutor(max_workers=2) as executor:
        policy_future = executor.submit(tenant.store.command, tenant.reader, command)
        assert admitted.wait(5)
        revoke_future = executor.submit(tenant.member, tenant.reader, active=False)
        # The revocation waits behind the already admitted transaction's tenant
        # share lock; release it and both transactions have a definite order.
        release.set()
        assert policy_future.result(5)["revision"] == 1
        revoke_future.result(5)
    with pytest.raises(AccessDenied):
        tenant.store.command(tenant.reader, command)


def test_audit_queries_are_scoped_bounded_metadata_only(tenant):
    first = tenant.store.command(tenant.admin, tenant.command())
    tenant.store.command(tenant.admin, tenant.command(project=tenant.other_project))
    tenant.member(tenant.reader, projects={tenant.project: ["audit_read"]})
    result = tenant.store.query(tenant.reader, Query(tenant.id, tenant.project, "audit", limit=1))
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["decision_id"] == first["decision_id"]
    assert set(event) == {"audit_id", "actor_id", "operation", "project_id", "resource_revision",
                         "decision_id", "semantics_sha256", "occurred_at"}
    with pytest.raises(AccessDenied):
        tenant.store.query(tenant.reader, Query(tenant.id, None, "audit"))
    with pytest.raises(AccessDenied):
        tenant.store.query(tenant.reader, Query(tenant.id, tenant.other_project, "audit"))


def test_runtime_role_cannot_rewrite_audit_receipts_or_policy(tenant):
    tenant.store.command(tenant.admin, tenant.command())
    for table in ("audit", "command_receipts", "policies", "host_receipts", "policy_leases", "host_directory",
                  "content_receipts"):
        for statement in (f"DELETE FROM sonn_governance.{table}",
                          f"TRUNCATE sonn_governance.{table}"):
            with psycopg.connect(tenant.config["application_dsn"]) as connection:
                connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
                with pytest.raises(errors.InsufficientPrivilege):
                    connection.execute(statement)
                connection.rollback()
    with psycopg.connect(tenant.config["application_dsn"]) as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("UPDATE sonn_governance.audit SET actor_id=actor_id")
        connection.rollback()
    with pytest.raises(InvalidRequest, match="unsafe privileges"):
        GovernanceStore(tenant.config["owner_dsn"])


def test_rls_without_context_denies_and_transaction_context_does_not_leak(tenant, database_config):
    other = Tenant(database_config)
    with psycopg.connect(tenant.config["application_dsn"], autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM sonn_governance.projects").fetchone()[0] == 0
        with connection.transaction():
            connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
            assert connection.execute("SELECT count(*) FROM sonn_governance.projects").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM sonn_governance.projects WHERE tenant_id=%s",
                                      (other.id,)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sonn_governance.projects").fetchone()[0] == 0
        with pytest.raises(errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
                connection.execute("INSERT INTO sonn_governance.memberships(tenant_id,actor_id,active,revision) VALUES(%s,%s,true,0)",
                                   (other.id, tenant.reader.actor_id))


def test_tenant_composite_foreign_keys_reject_foreign_project_grant(tenant, database_config):
    other = Tenant(database_config)
    with psycopg.connect(tenant.config["application_dsn"]) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant.id,))
        with pytest.raises(errors.ForeignKeyViolation):
            connection.execute("INSERT INTO sonn_governance.project_grants VALUES(%s,%s,%s,'metadata_read')",
                               (tenant.id, tenant.admin.actor_id, other.project))
        connection.rollback()


def test_migration_reopen_preserves_receipt_and_does_not_regrant_member(tenant):
    command = tenant.command()
    result = tenant.store.command(tenant.admin, command)
    migrate(tenant.config["owner_dsn"], application_role=tenant.config["application_role"])
    reopened = GovernanceStore(tenant.config["application_dsn"])
    assert reopened.command(tenant.admin, command) == result
    assert tenant.counts() == (1, 1, 2)


@pytest.mark.parametrize("change,error", [
    ({"protocol_version": 2}, UnsupportedVersion),
    ({"expected_revision": True}, InvalidRequest),
    ({"operation": "approve_model_prose"}, InvalidRequest),
    ({"payload": {"policy": policy(max_workers=8)}}, InvalidRequest),
    ({"payload": {"policy": policy(content_mode="all")}}, InvalidRequest),
    ({"payload": {"policy": policy(policy_version=999)}}, UnsupportedVersion),
    ({"payload": {"policy": policy(allowed_tools=["file_read", "file_read"])}}, InvalidRequest),
])
def test_malformed_or_unknown_contract_is_rejected_without_write(tenant, change, error):
    before = tenant.counts()
    with pytest.raises(error):
        tenant.store.command(tenant.admin, replace(tenant.command(), **change))
    assert tenant.counts() == before
