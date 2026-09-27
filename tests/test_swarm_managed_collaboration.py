"""Local managed sharing receipt fences survive retries, reopen and new epochs."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lumi.engine.swarming import AttemptContext, Scope, SwarmStore
from lumi.engine.swarming.managed_collaboration import ManagedCollaborationAdmission
from lumi.engine.swarming.managed_journal import ManagedBinding, ManagedJournal
from lumi.engine.swarming.managed_runtime import ManagedAdmissionError
from lumi.engine.swarming.models import Conflict
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from tests.test_swarm_supervisor import command


def uid():
    return str(uuid4())


@pytest.fixture
def local(tmp_path):
    clock = [1000.0]
    store = SwarmStore(tmp_path / "run.sqlite", clock=lambda: clock[0])
    supervisor = SwarmSupervisor(store)
    scope = Scope(uid(), "a" * 64, "project", "session")
    authority = supervisor.create(scope, supervisor_id="owner", objective="Receiver work", request_limit=8,
        policy=PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama"})))
    command(supervisor, authority, "plan", {"work_items": [{"id": "item", "objective": "Own selected task", "read_roots": ["fact.txt"],
        "write_roots": [], "tools": ["file_read"], "criteria": ["owner_review"]}]})
    assigned = command(supervisor, authority, "assign", {"work_item_id": "item", "worker_id": "worker", "requests": 2,
        "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(scope, authority.run_id, assigned["attempt_id"], "worker", authority.epoch)
    binding = ManagedBinding("https://fixture.example", "b" * 64, scope.tenant_id, uid(), uid(), 1, scope.owner_id, scope.project_id,
                             scope.session_id, authority.run_id, authority.epoch)
    journal = ManagedJournal(tmp_path / "managed.sqlite", binding)
    calls = []
    def accept(**values):
        calls.append(values)
        return {key: values[key] for key in ("message_id", "worker_id", "request_limit")} | {"state": "work_reserved", "dispatch_permitted": True}
    client = SimpleNamespace(sharing_accept=accept)
    adapter = ManagedCollaborationAdmission(tmp_path / "sharing.sqlite", supervisor=supervisor, authority=authority, journal=journal, client=client)
    return SimpleNamespace(store=store, supervisor=supervisor, authority=authority, context=context, adapter=adapter,
                           journal=journal, binding=binding, client=client, calls=calls, clock=clock, path=tmp_path)


def test_local_contract_once_only_and_reopen_never_restores_permit(local):
    f = local
    message = uid()
    f.adapter.bind_attempt(f.context, message)
    assert f.adapter.accepted_work_item_ids() == ["item"]
    args = {"worker_id": uid(), "lease_id": uid(), "binding_id": uid()}
    f.adapter(f.context, **args)
    assert len(f.calls) == 1 and f.calls[0]["request_limit"] == 2
    assert "Own selected task" not in str(f.calls)
    with pytest.raises(ManagedAdmissionError):
        f.adapter(f.context, **args)
    reopened = ManagedCollaborationAdmission(f.path / "sharing.sqlite", supervisor=f.supervisor, authority=f.authority, journal=f.journal, client=f.client)
    with pytest.raises(ManagedAdmissionError):
        reopened(f.context, **args)
    assert len(f.calls) == 1


def test_main_receipt_survives_new_journal_and_epoch_retry(local):
    f = local
    f.adapter.bind_attempt(f.context, uid())
    command(f.supervisor, f.authority, "worker_stopped", {"attempt_id": f.context.attempt_id, "attempt_epoch": f.context.epoch,
        "outcome": "failed", "evidence": "Observed fixture no dispatch"})
    with pytest.raises(Conflict, match="new explicit peer"):
        command(f.supervisor, f.authority, "retry", {"work_item_id": "item", "evidence": "Explicit request"})
    f.clock[0] += 31
    new = f.supervisor.acquire(f.authority.scope, f.authority.run_id, expected_epoch=1, supervisor_id="recovered", command_id=uid())
    with pytest.raises(Conflict, match="new explicit peer"):
        command(f.supervisor, new, "recover", {"retry_work_items": ["item"]})
    command(f.supervisor, new, "recover", {"retry_work_items": []})
    binding = replace(f.binding, epoch=new.epoch)
    journal = ManagedJournal(f.path / "managed-new.sqlite", binding)
    recovered = ManagedCollaborationAdmission(f.path / "sharing-new.sqlite", supervisor=f.supervisor, authority=new, journal=journal, client=f.client)
    assert recovered.accepted_work_item_ids() == ["item"]
    with pytest.raises(Conflict, match="new explicit peer"):
        command(f.supervisor, new, "retry", {"work_item_id": "item", "evidence": "Explicit recovered request"})
    # A future readiness producer still cannot escape the accepted allocation.
    with f.store._connection(write=True) as connection:
        connection.execute("UPDATE work_items SET state='ready' WHERE id='item'")
    with pytest.raises(Conflict, match="new explicit peer"):
        command(f.supervisor, new, "assign", {"work_item_id": "item", "worker_id": "retry", "requests": 3,
            "model": {"provider": "ollama", "model": "chosen"}})
    assert len(f.store.snapshot(new.scope, new.run_id)["reservations"]) == 1


def test_crash_after_main_binding_before_private_journal_fails_closed(local, monkeypatch):
    f = local
    original = f.adapter._connection
    def fail():
        raise RuntimeError("fixture journal unavailable")
    monkeypatch.setattr(f.adapter, "_connection", fail)
    with pytest.raises(RuntimeError):
        f.adapter.bind_attempt(f.context, uid())
    monkeypatch.setattr(f.adapter, "_connection", original)
    assert f.adapter.accepted_work_item_ids() == ["item"]
    with pytest.raises(ManagedAdmissionError):
        f.adapter(f.context, worker_id=uid(), lease_id=uid(), binding_id=uid())
    assert f.calls == []


def test_ready_scheduler_cannot_dispatch_a_bound_item(local):
    from lumi.engine.swarming.scheduler import SwarmScheduler
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from lumi.gui.runtime import BackendSpec
    f = local
    f.adapter.bind_attempt(f.context, uid())
    command(f.supervisor, f.authority, "worker_stopped", {"attempt_id": f.context.attempt_id, "attempt_epoch": 1,
        "outcome": "failed", "evidence": "Observed fixture no dispatch"})
    with f.store._connection(write=True) as connection:
        connection.execute("UPDATE work_items SET state='ready' WHERE id='item'")
    called = []
    runner = SwarmWorkerRunner(f.supervisor, f.authority, f.path, backend_factory=lambda spec: called.append(spec))
    scheduler = SwarmScheduler(runner, BackendSpec("ollama", "chosen"), requests_per_worker=2)
    try:
        assert scheduler.dispatch_ready() == []
        assert called == []
        assert len(f.store.snapshot(f.authority.scope, f.authority.run_id)["attempts"]) == 1
    finally:
        scheduler.close()
        runner.close(timeout=1)


def prepared_args(f):
    return {"work": {"id": "atomic", "objective": "Independent receiver objective", "role": "explore",
        "read_roots": ["fact.txt"], "write_roots": [], "tools": ["file_read"], "criteria": ["owner_review"]},
        "message_id": uid(), "model": {"provider": "ollama", "model": "chosen"}, "requests": 2,
        "worker_id": "atomic-worker", "command_id": uid(),
        "expected_revision": f.store.snapshot(f.authority.scope, f.authority.run_id)["run"]["revision"]}


def test_atomic_prepare_records_graph_reservation_and_fence_or_rolls_everything_back(local, monkeypatch):
    f = local
    args = prepared_args(f)
    before = f.store.snapshot(f.authority.scope, f.authority.run_id)
    original = f.adapter._remember_binding
    def fail(*_):
        raise RuntimeError("fixture interrupted between assignment and fence")
    monkeypatch.setattr(f.adapter, "_remember_binding", fail)
    with pytest.raises(RuntimeError):
        f.adapter.prepare_work(**args)
    assert f.store.snapshot(f.authority.scope, f.authority.run_id) == before
    monkeypatch.setattr(f.adapter, "_remember_binding", original)
    first = f.adapter.prepare_work(**args)
    assert first["dispatch_permitted"]
    second = f.adapter.prepare_work(**args)
    assert not second["dispatch_permitted"] and second["context"] == first["context"]
    assert f.adapter.accepted_work_item_ids() == ["atomic"]
    assert len(f.store.snapshot(f.authority.scope, f.authority.run_id)["attempts"]) == 2
    assert f.calls == []


def test_private_journal_failure_after_atomic_prepare_never_returns_replay_permit(local, monkeypatch):
    f = local
    args = prepared_args(f)
    def fail(*_):
        raise RuntimeError("fixture journal acknowledgement lost")
    monkeypatch.setattr(f.adapter, "_bind_private", fail)
    with pytest.raises(RuntimeError):
        f.adapter.prepare_work(**args)
    assert f.adapter.accepted_work_item_ids() == ["atomic"]
    assert not f.adapter.prepare_work(**args)["dispatch_permitted"]
    assert f.calls == []
