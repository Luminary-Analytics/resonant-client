"""Real mTLS/PostgreSQL admission around actual owned native check processes."""
# ruff: noqa: F811 -- shared disposable native/PKI fixtures.

import sys

import psycopg
import pytest

from lumi.engine.swarming.integration import SwarmIntegration
from lumi.engine.swarming.managed_effects import ManagedEffects
from lumi.engine.swarming.managed_runtime import ManagedAdmissionError, ManagedRuntime
from sonn_governance.models import CommandEnvelope
from test_managed_native_runtime import certificates, native  # noqa: F401
from test_store import uid
from tests.test_swarm_integration import git


@pytest.fixture
def owner_effect(native, tmp_path):
    env = native
    env.tenant.store.command(env.tenant.admin, env.tenant.command(expected=1, policy_version=2,
        allowed_effects=["candidate_check"], allowed_models=[{"provider": "ollama", "model": "chosen"}]))
    env.managed = ManagedRuntime(env.journal, env.client, policy_revision=2)
    git(env.workspace, "init", "-b", "main")
    git(env.workspace, "add", ".")
    git(env.workspace, "commit", "-m", "Fixture")
    base = git(env.workspace, "rev-parse", "HEAD")
    env.effects = ManagedEffects(env.store, env.managed)
    env.integration = SwarmIntegration(env.store, env.workspace, root=tmp_path / "worktrees", managed_effects=env.effects)
    with env.store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,result_revision,state,manifest_json,process_protocol) "
            "VALUES('candidate',?,?,?,?,?,?,'ready','{}',1)", (env.authority.run_id, env.authority.epoch,
            env.integration.repo_key, str(env.workspace), base, base))
        connection.execute("INSERT INTO integration_checks(id,candidate_id,check_key,candidate_revision,argv_json,state,process_protocol) "
                           "VALUES('check','candidate','fixture',?,'[]','running',1)", (base,))
    return env


def execute(env, script="print('actual check observation')"):
    return env.integration._execute(env.authority, "check", "check", [sys.executable, "-c", script],
        env.workspace, timeout_seconds=5)


def rows(env):
    with psycopg.connect(env.database_config["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
        return connection.execute("SELECT * FROM sonn_governance.owner_effects WHERE tenant_id=%s", (env.tenant.id,)).fetchall()


def revoke(env):
    env.hosts.owner_command(env.tenant.admin, CommandEnvelope(1, uid(), env.tenant.id, env.tenant.project,
        env.active["revision"], "revoke_host", {"host_id": env.active["host_id"]}))


def test_actual_managed_check_and_cleanup_after_revocation(owner_effect):
    env = owner_effect
    result = execute(env)
    assert result.exit_code == 0 and b"actual check observation" in result.stdout
    assert [row["state"] for row in rows(env)] == ["admitted"]
    revoke(env)
    assert env.effects.flush()["delivered"] == 1
    assert [row["state"] for row in rows(env)] == ["completed"]
    assert env.effects.inspect()["resume_ready"]
    with pytest.raises(ManagedAdmissionError):
        execute(env)
    assert len(rows(env)) == 1


def test_lost_real_admission_reply_keeps_single_receipt_and_does_not_execute(owner_effect, monkeypatch, tmp_path):
    env, marker = owner_effect, tmp_path / "must-not-exist"
    original = env.resources.authorize_effect
    def lost(principal, **payload):
        original(principal, **payload)
        raise RuntimeError("Synthetic lost committed reply")
    monkeypatch.setattr(env.resources, "authorize_effect", lost)
    with pytest.raises(ManagedAdmissionError):
        execute(env, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    assert not marker.exists() and len(rows(env)) == 1
    assert env.effects.inspect()["effects"][0]["claimed"] == 0
    assert env.effects.flush()["delivered"] == 1
    assert rows(env)[0]["state"] == "never_started"


def test_server_deny_keeps_native_process_uninvoked_and_has_no_fake_delivery(owner_effect, tmp_path):
    env, marker = owner_effect, tmp_path / "must-not-exist"
    env.tenant.member(env.tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read"])
    with pytest.raises(ManagedAdmissionError):
        execute(env, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    assert not marker.exists() and rows(env) == []
    state = env.effects.inspect()
    assert state["total"] == 1 and state["pending_observations"] == 0 and state["resume_ready"]


def test_uncommitted_lost_effect_gets_server_absence_fence_before_recovery_release(owner_effect, monkeypatch, tmp_path):
    from lumi.engine.swarming.managed_client import HostChannelError
    env, marker = owner_effect, tmp_path / "must-not-exist"
    def missing(principal, **payload):
        raise RuntimeError("Synthetic transport failure before admission transaction")
    monkeypatch.setattr(env.resources, "authorize_effect", missing)
    with pytest.raises(ManagedAdmissionError):
        execute(env, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    assert not marker.exists() and rows(env) == []
    history = ManagedEffects.open_history(env.store, env.journal, env.client)
    history.fence_restart()
    assert not history.inspect()["resume_ready"]
    process_id = history.inspect()["effects"][0]["process_id"]
    intent = history.absence_intent(process_id)
    receipt = env.client.fence_absent(**intent)
    history.record_absence(process_id, receipt)
    assert history.inspect()["resume_ready"] and history.inspect()["pending_observations"] == 0
    assert history.flush()["delivered"] == 0  # A tombstone is not a command-completion acknowledgement.
    monkeypatch.undo()
    with pytest.raises(HostChannelError):
        env.client.authorize_effect(lease_id=intent["lease_id"], binding_id=env.managed.register(),
            effect_id=intent["resource_id"], kind="candidate_check", semantics_sha256="a" * 64)
    assert not marker.exists() and rows(env) == []
