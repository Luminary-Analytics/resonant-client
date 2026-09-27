"""Native isolated processes require exact one-use managed owner permits."""
# ruff: noqa: F811 -- imported real Git fixture is intentionally used by name.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lumi.engine.swarming.managed_effects import ManagedEffects
from lumi.engine.swarming.integration import ApplyApproval, CheckSpec
from lumi.engine.swarming.managed_journal import ManagedBinding
from lumi.engine.swarming.managed_runtime import ManagedAdmissionError
from lumi.engine.swarming.models import AdmissionClosed, Conflict, Scope, ScopeDenied
from tests.test_swarm_integration import command, finish, setup, writers  # noqa: F401


class Client:
    def __init__(self):
        self.admissions, self.observations = [], []
        self.permit = True
        self.before_admit = lambda: None

    def authorize_effect(self, **values):
        self.admissions.append(values)
        self.before_admit()
        return {"effect_id": values["effect_id"], "state": "admitted", "dispatch_permitted": self.permit}

    def observe_effect(self, **values):
        self.observations.append(values)
        return {"effect_id": values["effect_id"], "state": values["outcome"],
                "source": "authenticated_host_observation", "dispatch_permitted": False}


class Runtime:
    def __init__(self, path, authority):
        self.client = Client()
        self.binding = ManagedBinding("https://fixture.invalid", "a" * 64, authority.scope.tenant_id,
            str(uuid4()), str(uuid4()), 1, authority.scope.owner_id, authority.scope.project_id,
            authority.scope.session_id, authority.run_id, authority.epoch)
        self.journal = SimpleNamespace(path=path, binding=self.binding)
        self.lease = {"lease_id": str(uuid4()), "policy": {"policy_version": 2,
            "allowed_effects": ["writer_git", "candidate_git", "candidate_check", "checkout_apply"]}}
        self.deadline = time.monotonic() + 60
        self.remote_binding = str(uuid4())

    def register(self):
        return self.remote_binding

    def effect_lease(self):
        return self.lease, self.deadline

    def claim_effect_lease(self, lease, deadline):
        if lease != self.lease or deadline != self.deadline or time.monotonic() >= deadline:
            raise ManagedAdmissionError()


@pytest.fixture
def managed(setup, tmp_path):
    store, supervisor, old, integration, project, base = setup
    with store._connection() as connection:
        policy = json.loads(connection.execute("SELECT policy_json FROM runs WHERE id=?", (old.run_id,)).fetchone()[0])
    from lumi.engine.swarming.policy import PolicyProfile
    authority = supervisor.create(Scope(str(uuid4()), "b" * 64, "project", "session"),
        supervisor_id="managed-owner", objective="Managed effects fixture", request_limit=12, policy=PolicyProfile.from_dict(policy))
    runtime = Runtime(tmp_path / "managed.sqlite", authority)
    effects = ManagedEffects(store, runtime)
    integration.managed_effects = effects
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,result_revision,state,manifest_json,process_protocol) "
            "VALUES('candidate',?,?,?,?,?,?,'ready','{}',1)",
            (authority.run_id, authority.epoch, integration.repo_key, str(project), base, base))
        connection.execute("INSERT INTO integration_checks(id,candidate_id,check_key,candidate_revision,argv_json,state,process_protocol) "
                           "VALUES('check','candidate','fixture',?,'[]','running',1)", (base,))
    return store, supervisor, authority, integration, effects, runtime


def execute(managed, script="print('observed')"):
    return managed[3]._execute(managed[2], "check", "check", [sys.executable, "-c", script],
        managed[3].project, environment={"FIXTURE_PRIVATE": "not in metadata"}, timeout_seconds=5)


def intent(managed):
    _, _, authority, integration, effects, _ = managed
    identity = integration._processes.intent(authority, "check", "check", [sys.executable, "-c", "pass"], integration.project)
    effects.prepare_effect(authority, identity, environment={}, timeout_seconds=5, max_output_bytes=1000)
    with integration.store._connection(write=True) as connection:
        connection.execute("UPDATE integration_processes SET state='owned' WHERE id=?", (identity,))
    return identity


def test_actual_owned_process_requires_permit_and_retains_hash_only_observation(managed):
    result = execute(managed)
    effects, runtime = managed[4:]
    assert result.stdout == b"observed\r\n" or result.stdout == b"observed\n"
    snapshot = effects.inspect()
    assert snapshot["total"] == 1 and snapshot["pending_observations"] == 1 and not snapshot["resume_ready"]
    assert snapshot["effects"][0]["outcome"] == "completed" and snapshot["effects"][0]["cleanup_observed"] == 1
    assert effects.flush()["delivered"] == 1 and effects.inspect()["resume_ready"]
    assert runtime.client.admissions[0]["kind"] == "candidate_check"
    assert set(runtime.client.admissions[0]) == {"lease_id", "binding_id", "effect_id", "kind", "semantics_sha256"}
    raw = effects.path.read_bytes()
    assert b"not in metadata" not in raw and b"print('observed')" not in raw


def test_writer_candidate_check_and_explicit_apply_each_get_distinct_process_permits(managed):
    store, supervisor, authority, integration, effects, runtime = managed
    local = (store, supervisor, authority, integration, integration.project, integration._head(integration.project))
    context, writer = writers(local, ("a",))[0]
    (Path(writer["path"]) / "a.txt").write_text("managed change\n")
    finish(local, context, writer)
    check = CheckSpec("combined", (sys.executable, "-c",
        "from pathlib import Path; assert Path('a.txt').read_text() == 'managed change\\n'"), 5)
    candidate = integration.prepare_candidate(authority, writer_ids=(writer["id"],), required_checks=(check,),
                                              criterion_checks={"a": {"combined": "combined"}})
    assert (integration.project / "a.txt").read_text() == "base-a\n"
    assert integration.run_check(authority, candidate["id"], "combined")["state"] == "passed"
    approval = ApplyApproval("managed-approval", authority.scope, authority.run_id, candidate["id"],
                             candidate["base_revision"], candidate["result_revision"], 1100)
    assert integration.apply(authority, candidate["id"], approval=approval)["state"] == "applied"
    assert (integration.project / "a.txt").read_text() == "managed change\n"
    admissions = runtime.client.admissions
    assert {row["kind"] for row in admissions} == {"writer_git", "candidate_git", "candidate_check", "checkout_apply"}
    assert len({row["effect_id"] for row in admissions}) == len(admissions)
    assert len({row["semantics_sha256"] for row in admissions}) == len(admissions)
    assert effects.inspect()["total"] == len(admissions)
    assert effects.flush(maximum=100)["delivered"] == len(admissions)


def test_false_replay_and_lost_admission_never_execute_argv(managed, tmp_path):
    runtime = managed[5]
    runtime.client.permit = False
    marker = tmp_path / "must-not-exist"
    with pytest.raises(ManagedAdmissionError):
        execute(managed, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    assert not marker.exists()
    assert managed[4].inspect()["effects"][0]["outcome"] == "never_started"
    assert len(runtime.client.admissions) == 1


def test_stop_remains_live_during_central_http_and_fences_late_permit(managed, tmp_path):
    store, supervisor, authority, _, effects, runtime = managed
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(5)
    runtime.client.before_admit = blocked
    marker = tmp_path / "must-not-exist"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, managed, f"from pathlib import Path; Path({str(marker)!r}).touch()")
        assert entered.wait(3)
        before = time.monotonic()
        assert command(supervisor, authority, "stop").state == "stopping"
        assert time.monotonic() - before < 1
        release.set()
        with pytest.raises(AdmissionClosed):
            future.result(timeout=5)
    assert not marker.exists() and effects.inspect()["effects"][0]["claimed"] == 0
    assert store.snapshot(authority.scope, authority.run_id)["integration_processes"][0]["state"] == "not_started"


def test_independent_connections_single_use_claim_and_changed_semantics_denied(managed):
    store, _, authority, _, effects, _ = managed
    process_id = intent(managed)
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(3)
        try:
            with store._connection(write=True) as connection:
                effects.claim_effect(authority, process_id, connection)
            return True
        except ManagedAdmissionError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [False, True]
    with pytest.raises(Conflict, match="immutable"):
        effects.prepare_effect(authority, process_id, environment={"CHANGED": "yes"}, timeout_seconds=5, max_output_bytes=1000)


def test_restart_cannot_restore_permit_and_recovery_needs_native_cleanup_and_ack(managed):
    store, _, authority, _, effects, runtime = managed
    process_id = intent(managed)
    with store._connection(write=True) as connection:
        effects.claim_effect(authority, process_id, connection)
        connection.execute("UPDATE integration_processes SET state='invoked',invoked=1 WHERE id=?", (process_id,))
    reopened = ManagedEffects.open_history(store, runtime.journal, runtime.client)
    reopened.fence_restart()
    assert not reopened.inspect()["resume_ready"]
    with store._connection(write=True) as connection:
        with pytest.raises(ManagedAdmissionError):
            reopened.claim_effect(authority, process_id, connection)
        connection.execute("UPDATE runs SET epoch=epoch+1,supervisor_id='new-owner' WHERE id=?", (authority.run_id,))
    recovered = replace(authority, epoch=authority.epoch + 1, supervisor_id="new-owner")
    with pytest.raises(Conflict, match="cleanup"):
        reopened.reconcile_process(recovered, process_id)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_processes SET state='stopped',exit_code=0 WHERE id=?", (process_id,))
    reopened.reconcile_process(recovered, process_id)
    assert reopened.inspect()["uncertain"] == 1 and not reopened.inspect()["resume_ready"]
    reopened.flush()
    assert reopened.inspect()["resume_ready"] and reopened.inspect()["uncertain"] == 1


def test_expiry_and_candidate_mutation_close_final_invocation_gate(managed):
    store, _, authority, _, effects, runtime = managed
    process_id = intent(managed)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_candidates SET result_revision=? WHERE id='candidate'", ('d' * 40,))
        with pytest.raises(Conflict, match="immutable"):
            effects.claim_effect(authority, process_id, connection)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE integration_candidates SET result_revision=base_revision WHERE id='candidate'")
        runtime.deadline = 0
        with pytest.raises(ManagedAdmissionError):
            effects.claim_effect(authority, process_id, connection)
    assert effects.inspect()["effects"][0]["claimed"] == 0


def test_org_cannot_fall_back_to_local_effect_and_foreign_history_is_rejected(managed):
    store, _, authority, integration, effects, runtime = managed
    integration.managed_effects = None
    with pytest.raises(ScopeDenied, match="central"):
        execute(managed)
    assert effects.inspect()["total"] == 0
    runtime.journal.binding = replace(runtime.binding, session_id="other-session")
    with pytest.raises(ScopeDenied, match="another"):
        ManagedEffects.open_history(store, runtime.journal, runtime.client)


def test_history_handles_are_closed_after_inspection(managed):
    effects = managed[4]
    effects.inspect()
    moved = effects.path.with_suffix(".renamed")
    effects.path.rename(moved)
    moved.rename(effects.path)
    assert Path(effects.path).is_file()


def test_existing_file_cannot_become_a_new_authority_and_missing_coverage_is_not_repaired(managed):
    store, _, _, _, effects, runtime = managed
    with pytest.raises(Conflict, match="recovery"):
        ManagedEffects(store, runtime)
    with effects._connection(write=True) as connection:
        connection.execute("DELETE FROM binding")
    with pytest.raises(Conflict, match="coverage"):
        ManagedEffects.open_history(store, runtime.journal, runtime.client)
    with effects._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM binding").fetchone()[0] == 0


def test_remote_definite_denial_stays_not_admitted_after_recovery(managed):
    from lumi.engine.swarming.managed_client import HostChannelError
    store, _, authority, _, effects, runtime = managed
    def denied(**kwargs):
        raise HostChannelError(status=403, delivery_unknown=False)
    runtime.client.authorize_effect = denied
    with pytest.raises(ManagedAdmissionError):
        execute(managed)
    process_id = effects.inspect()["effects"][0]["process_id"]
    reopened = ManagedEffects.open_history(store, runtime.journal, runtime.client)
    reopened.fence_restart()
    with store._connection(write=True) as connection:
        connection.execute("UPDATE runs SET epoch=epoch+1,supervisor_id='new-owner' WHERE id=?", (authority.run_id,))
    reopened.reconcile_process(replace(authority, epoch=authority.epoch + 1, supervisor_id="new-owner"), process_id)
    assert reopened.inspect()["resume_ready"] and reopened.inspect()["pending_observations"] == 0
    assert reopened.inspect()["effects"][0]["phase"] == "not_admitted"


def test_scope_and_native_stop_are_checked_before_claim_replay(managed):
    store, supervisor, authority, _, effects, _ = managed
    process_id = intent(managed)
    with store._connection(write=True) as connection:
        with pytest.raises(ScopeDenied):
            effects.claim_effect(replace(authority, scope=replace(authority.scope, session_id="foreign")), process_id, connection)
        effects.claim_effect(authority, process_id, connection)
    command(supervisor, authority, "stop")
    with store._connection(write=True) as connection:
        with pytest.raises(AdmissionClosed):
            effects.claim_effect(authority, process_id, connection)


def test_observation_network_budget_is_bounded_between_sends(managed, monkeypatch):
    effects, runtime = managed[4:]
    execute(managed)
    execute(managed)
    now = [0.0]
    monkeypatch.setattr("lumi.engine.swarming.managed_effects.time.monotonic", lambda: now[0])
    original = runtime.client.observe_effect
    def delayed(**payload):
        now[0] = 10
        return original(**payload)
    runtime.client.observe_effect = delayed
    assert effects.flush(maximum=8) == {"delivered": 1, "unavailable": True}
    assert effects.inspect()["pending_observations"] == 1
