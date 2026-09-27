"""Desktop swarm controls preserve saved ownership and ordinary chat admission."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.models import Conflict, IdempotencyConflict, ScopeDenied, SwarmError
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui import swarming
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from lumi.gui.ws_commands import CommandContext, HANDLERS
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_coordinator import response as plan_response
from tests.test_swarm_workers import until


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = SettingsManager(tmp_path / "settings.json")
    instances = []
    def factory(spec):
        backend = StreamingBackend(events=[text_delta("Inspected findings; independent review required."), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        instances.append(backend)
        return backend
    service = SwarmRuntime(settings, backend_factory=factory, state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"),
                              str(workspace), BackendSpec("ollama", "chosen"), "Project-specific fixture instruction")
    yield service, capture, instances
    service.close()


def start_request(**extra):
    return {"request_id": "setup-request", "action": "start", "objective": "Investigate two independent questions",
            "tasks": [{"objective": "Inspect A", "read_roots": ["."]}, {"objective": "Inspect B", "read_roots": ["."]}],
            "request_limit": 4, "max_workers": 2, **extra}


def enable(service, capture):
    return service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})


def wait_stopped(service, run_id):
    deadline = time.monotonic() + 5
    runner = service._runners[run_id][1]
    while time.monotonic() < deadline:
        if runner.inspect_all() and not any(row["alive"] for row in runner.inspect_all()):
            return
        threading.Event().wait(.01)
    raise AssertionError("fixture workers did not stop")


def test_default_off_then_explicit_model_two_independent_workers(setup):
    service, capture, instances = setup
    view = service.operate(capture, {"request_id": "view", "action": "view"})
    assert view["enabled"] is False and view["run"] is None and instances == []
    with pytest.raises(Conflict, match="Enable"):
        service.operate(capture, start_request())
    enable(service, capture)
    result = service.operate(capture, start_request())
    run_id = result["run"]["run"]["id"]
    wait_stopped(service, run_id)
    assert len(instances) == 2 and instances[0] is not instances[1]
    assert capture.backend_spec.model == "chosen"
    snapshot = service.operate(capture, {"request_id": "refresh", "run_id": run_id})["run"]
    assert len(snapshot["submissions"]) == 2
    assert all(item["state"] == "submitted" for item in snapshot["work_items"])
    assert snapshot["run"]["state"] == "running"  # No invented independent review.
    assert service.busy


def use_file_reading_fixture(service, capture):
    Path(capture.workspace, "fact.txt").write_text("Inspection fixture source fact.\n", encoding="utf-8")

    def factory(spec):
        backend = StreamingBackend(scripts=[
            [tool_call("file_read", {"path": "fact.txt"}), done()],
            [text_delta("Inspected source fact; owner review required."), done()],
        ])
        backend.name, backend.model = spec.backend_type, spec.model
        return backend

    service._factory = factory


def test_retained_history_and_content_dispatch_keep_captured_session_scope(setup):
    service, capture, _ = setup
    use_file_reading_fixture(service, capture)
    enable(service, capture)
    started = service.operate(capture, start_request())
    run_id = started["run"]["run"]["id"]
    wait_stopped(service, run_id)
    history = service.operate(capture, {"action": "history", "request_id": "history", "limit": 1})
    assert history["history"]["items"][0]["run_id"] == run_id
    artifact_id = history["run"]["artifact_refs"][0]["id"]
    request = {"action": "read_artifact", "request_id": "read-page", "run_id": run_id,
               "artifact_id": artifact_id, "limit": 12}
    page = service.operate(capture, request)["artifact_page"]
    assert page["run_id"] == run_id and len(page["text"]) == 12
    assert page["next_offset"] == 12 and page["verified_sha256"] == page["artifact"]["sha256"]
    other = replace(capture, scope=replace(capture.scope, session_id="different-session"))
    assert service.operate(other, {"action": "history", "request_id": "other-history"})["history"]["items"] == []
    with pytest.raises(ScopeDenied):
        service.operate(other, request)
    service.close()
    assert service.operate(capture, request)["artifact_page"]["text"] == page["text"]


def test_large_retained_content_verification_does_not_hold_stop_control_lock(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from lumi.engine.swarming.inspection import SwarmInspection

    service, capture, _ = setup
    use_file_reading_fixture(service, capture)
    enable(service, capture)
    started = service.operate(capture, start_request())
    run_id = started["run"]["run"]["id"]
    wait_stopped(service, run_id)
    snapshot = service.operate(capture, {"request_id": "before-read", "run_id": run_id})["run"]
    entered, release = threading.Event(), threading.Event()
    original = SwarmInspection.read_artifact

    def blocked_read(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Fixture read was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(SwarmInspection, "read_artifact", blocked_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(service.operate, capture, {"action": "read_artifact", "request_id": "slow-read",
            "run_id": run_id, "artifact_id": snapshot["artifact_refs"][0]["id"]})
        try:
            assert entered.wait(2)
            stopping = pool.submit(service.operate, capture, {"action": "stop", "request_id": "stop-during-read",
                "run_id": run_id, "expected_revision": snapshot["run"]["revision"]})
            assert stopping.result(timeout=2)["run"]["run"]["state"] == "cancelled"
        finally:
            release.set()
        finished = reading.result(timeout=2)
    assert finished["run"]["run"]["state"] == "cancelled"  # Fresh control state after slow verification.
    assert finished["artifact_page"]["format"] == "text"


def test_lost_start_response_cannot_launch_twice_or_change_setup(setup):
    service, capture, instances = setup
    enable(service, capture)
    first = service.operate(capture, start_request())
    second = service.operate(capture, start_request())
    assert first["run"]["run"]["id"] == second["run"]["run"]["id"]
    with pytest.raises(IdempotencyConflict):
        service.operate(capture, start_request(objective="A different intent"))
    wait_stopped(service, first["run"]["run"]["id"])
    assert len(instances) == 2


def test_runner_construction_failure_retains_terminal_run_without_stranding_foreground(setup, monkeypatch):
    service, capture, instances = setup
    enable(service, capture)
    def unavailable(*args, **kwargs):
        raise OSError("fixture workspace unavailable")
    monkeypatch.setattr("lumi.engine.swarming.service.SwarmWorkerRunner", unavailable)
    with pytest.raises(OSError):
        service.operate(capture, start_request())
    assert not service.busy and not instances
    result = service.operate(capture, {"request_id": "inspect"})
    assert result["run"]["run"]["state"] == "cancelled"


def test_workers_resolve_one_private_key_snapshot_without_exposing_it(setup):
    service, capture, _ = setup
    service.settings.set("api_keys", "fixture", "fixture-key-before")
    capture = replace(capture, backend_spec=BackendSpec("ollama", "chosen", api_key_source="settings", api_key_setting="fixture"))
    enable(service, capture)
    seen = []
    def factory(spec):
        seen.append(spec.api_key)
        service.settings.set("api_keys", "fixture", "fixture-key-after")
        provider = StreamingBackend(events=[text_delta("Observed fixture"), done()])
        provider.name, provider.model = "ollama", "chosen"
        return provider
    service._factory = factory
    result = service.operate(capture, start_request())
    run_id = result["run"]["run"]["id"]
    wait_stopped(service, run_id)
    assert seen == ["fixture-key-before", "fixture-key-before"]
    assert "fixture-key" not in str(service.operate(capture, {"request_id": "inspect", "run_id": run_id}))
    assert capture.backend_spec.api_key == ""


def test_disable_preserves_controls_and_history_then_stop_releases_foreground(setup):
    service, capture, _ = setup
    enable(service, capture)
    result = service.operate(capture, start_request())
    run_id = result["run"]["run"]["id"]
    wait_stopped(service, run_id)
    disabled = service.operate(capture, {"action": "configure", "request_id": "disable", "enabled": False, "run_id": run_id})
    assert disabled["enabled"] is False and disabled["run"]["submissions"]
    stopped = service.operate(capture, {"action": "stop", "request_id": "stop", "run_id": run_id,
                                      "expected_revision": disabled["run"]["run"]["revision"]})
    assert stopped["run"]["run"]["state"] == "cancelled" and not service.busy
    with pytest.raises(Conflict, match="Enable"):
        service.operate(capture, start_request(request_id="new-start"))


@pytest.mark.parametrize("changes", [
    {"request_limit": True}, {"max_workers": 9}, {"tasks": []}, {"request_limit": 1},
    {"tasks": [{"objective": "inspect", "read_roots": ["../outside"]}]},
    {"tasks": [{"objective": "inspect", "tools": ["bash"]}]}, {"owner_id": "forged"},
])
def test_invalid_setup_admits_no_worker(setup, changes):
    service, capture, instances = setup
    enable(service, capture)
    with pytest.raises((ValueError, SwarmError)):
        service.operate(capture, start_request(**changes))
    assert not instances and not service.busy


def test_foreign_scope_cannot_inspect_existing_run(setup):
    service, capture, _ = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    with pytest.raises(ScopeDenied):
        service.operate(replace(capture, scope=replace(capture.scope, owner_id="foreign")),
                        {"request_id": "foreign", "run_id": run_id})
    with pytest.raises(ScopeDenied):
        service.captured_run(run_id, capture.workspace, "another-session")
    with pytest.raises(ScopeDenied):
        service.operate(replace(capture, scope=replace(capture.scope, owner_id="foreign")),
                        {"request_id": "foreign-stop", "run_id": run_id, "action": "stop", "expected_revision": 0})


def test_saved_conversation_captured_before_async_work_and_foreign_target_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    saved = tmp_path / "sessions"
    saved.mkdir()
    session_id = "20260926_123456_abcd"
    monkeypatch.setattr(swarming, "is_valid_session_id", lambda value: value == session_id)
    monkeypatch.setattr(swarming, "_sessions_dir", lambda _: saved)
    (saved / f"{session_id}.json").write_text("{}")
    settings = SettingsManager(tmp_path / "settings.json")
    service = SwarmRuntime(settings, state_root=lambda _: tmp_path / "state")
    state = SimpleNamespace(project=SimpleNamespace(project_path=str(workspace), current_session=SimpleNamespace(id=session_id)),
        backend_spec=BackendSpec("ollama", "chosen"), settings=settings,
        session=SimpleNamespace(project_instructions="captured"), _swarm_desktop=service)
    replies = []
    async def send(payload):
        replies.append(payload)
    asyncio.run(swarming.command(state, send, {"request_id": "view", "project": str(workspace), "session_id": "foreign"}))
    assert "error" in replies[-1]
    assert not (tmp_path / "state").exists()
    asyncio.run(swarming.command(state, send, {"request_id": "view", "project": str(workspace), "session_id": session_id}))
    assert "error" not in replies[-1]
    assert replies[-1]["model"] == {"provider": "ollama", "model": "chosen", "label": "ollama"}


def test_chat_and_model_controls_do_not_replace_active_swarm():
    class Socket:
        def __init__(self):
            self.events = []
        async def send_json(self, payload):
            self.events.append(payload)
    state = SimpleNamespace(_swarm_starting=True, get_init_data=lambda **kw: {"event": "init"})
    socket = Socket()
    ctx = CommandContext(socket, state, {"command": "message", "text": "start"}, SimpleNamespace(busy=False))
    asyncio.run(HANDLERS["message"](ctx))
    assert socket.events[-1]["event"] == "error"
    ctx.msg = {"command": "switch_model", "model": "changed"}
    asyncio.run(HANDLERS["switch_model"](ctx))
    assert any(event["event"] == "model_switch_blocked" for event in socket.events)


def test_reopened_store_closes_new_work_but_allows_recovery_navigation(setup):
    service, capture, _ = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    reopened = SwarmRuntime(service.settings, state_root=service._state_root)
    try:
        # Discovery is independent of the selected conversation and creates no
        # worker, credential lookup or execution lease.
        reopened.watch_project(capture.workspace, replace(capture.scope, session_id="other-session"))
        assert reopened.busy and not reopened.navigation_busy
        snapshot = reopened.operate(capture, {"request_id": "inspect", "run_id": run_id})
        assert snapshot["run"]["recovery_needed"]
        with pytest.raises(Conflict, match="current team"):
            reopened.operate(replace(capture, scope=replace(capture.scope, session_id="other-session")),
                             start_request(request_id="new-team"))
    finally:
        reopened.close()


def test_unreadable_saved_store_blocks_work_without_breaking_inspection(setup, tmp_path):
    service, capture, _ = setup
    path = tmp_path / "state" / "swarm" / "state.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    service.watch_project(capture.workspace, capture.scope)
    assert service.busy and not service.navigation_busy
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_stop_releases_foreground_after_cleanup_without_another_view(setup):
    service, capture, _ = setup
    entered, release = threading.Event(), threading.Event()
    def factory(spec):
        backend = StreamingBackend(events=[text_delta("Finding"), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        def close():
            entered.set()
            assert release.wait(5)
        backend.close = close
        return backend
    service._factory = factory
    enable(service, capture)
    view = service.operate(capture, start_request(tasks=[{"objective": "inspect"}]))
    run_id = view["run"]["run"]["id"]
    try:
        assert entered.wait(5)
        revision = service._runners[run_id][1].store.snapshot(capture.scope, run_id)["run"]["revision"]
        stopped = service.operate(capture, {"action": "stop", "request_id": "stop", "run_id": run_id,
                                          "expected_revision": revision})
        assert stopped["run"]["run"]["state"] == "stopping" and service.busy
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while service.busy and time.monotonic() < deadline:
        threading.Event().wait(.02)
    assert not service.busy


def test_owner_review_is_bound_to_visible_submission_and_completion_is_explicit(setup):
    service, capture, _ = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    view = service.operate(capture, {"request_id": "inspect", "run_id": run_id})["run"]
    with pytest.raises(Conflict, match="all work accepted"):
        service.operate(capture, {"action": "complete", "request_id": "early-complete", "run_id": run_id,
                                 "expected_revision": view["run"]["revision"]})
    for submission in view["submissions"]:
        attempt = next(row for row in view["attempts"] if row["id"] == submission["attempt_id"])
        intent = {"action": "review_read_result", "request_id": "review-" + attempt["id"], "run_id": run_id,
                  "expected_revision": view["run"]["revision"], "attempt_id": attempt["id"],
                  "attempt_epoch": attempt["epoch"], "candidate_revision": submission["candidate_revision"],
                  "evidence": "Owner compared the finding with the scoped source evidence."}
        with pytest.raises(Conflict):
            service.operate(capture, {**intent, "candidate_revision": "replaced"})
        view = service.operate(capture, intent)["run"]
        replay = service.operate(capture, intent)["run"]
        assert len(replay["check_receipts"]) == len(view["check_receipts"])
    assert view["run"]["state"] == "running" and service.busy
    complete = service.operate(capture, {"action": "complete", "request_id": "complete", "run_id": run_id,
                                       "expected_revision": view["run"]["revision"]})
    assert complete["run"]["run"]["state"] == "completed" and not service.busy
    assert all(check["executor_id"] == "owner:" + capture.scope.owner_id for check in view["check_receipts"])


def test_coordinator_proposes_then_explicit_decision_schedules_more_work_than_slots(setup):
    service, capture, _ = setup
    instances = []
    import json
    plan = json.loads(plan_response())
    plan["work_items"].append({**plan["work_items"][0], "id": "third", "objective": "Inspect third area"})
    def factory(spec):
        output = json.dumps(plan) if not instances else "Independent source finding for owner review"
        backend = StreamingBackend(events=[text_delta(output), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        instances.append(backend)
        return backend
    service._factory = factory
    enable(service, capture)
    request = start_request(plan_mode="coordinator", tasks=None, request_limit=12,
                            coordinator_requests=3, worker_requests=3)
    run_id = service.operate(capture, request)["run"]["run"]["id"]
    wait_stopped(service, run_id)
    before = service.operate(capture, {"request_id": "plan-view", "run_id": run_id})["run"]
    assert len(instances) == 1 and not before["work_items"] and not before["submissions"]
    proposal = before["coordinator_proposals"][0]
    decision = {"action": "decide_proposal", "request_id": "plan-decision", "run_id": run_id,
                "expected_revision": before["run"]["revision"], "proposal_id": proposal["id"],
                "sha256": proposal["sha256"], "accept": True, "evidence": "Owner reviewed three bounded investigations"}
    service.operate(capture, decision)
    service.operate(capture, decision)  # Lost acknowledgement does not duplicate dispatch.
    runner = service._runners[run_id][1]
    until(lambda: len(runner.store.snapshot(capture.scope, run_id)["submissions"]) == 3)
    wait_stopped(service, run_id)
    after = service.operate(capture, {"request_id": "findings", "run_id": run_id})["run"]
    assert len(instances) == 4 and len(after["attempts"]) == 4
    assert after["run"]["state"] == "running" and not after["check_receipts"]
    assert after["coordinator_proposals"][0]["state"] == "accepted"
    assert after["scheduling"]["requests_per_worker"] == 3
    assert all(row["state"] == "submitted" for row in after["work_items"])


def test_coordinator_allowance_is_explicit_and_invalid_total_never_calls_provider(setup):
    service, capture, instances = setup
    enable(service, capture)
    with pytest.raises(ValueError):
        service.operate(capture, start_request(plan_mode="coordinator", tasks=None, request_limit=5,
                                             coordinator_requests=3, worker_requests=3))
    assert not instances and not service.busy


def expired_reader_runtime(setup):
    """Retain known stopped readers while the old host loses its lease."""
    service, capture, _ = setup
    now = [time.time()]
    store = service._store(capture)
    store.clock = lambda: now[0]
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    runner = service._runners[run_id][1]
    runner._maintenance_stop.set()
    runner._maintenance.join(timeout=1)
    now[0] += 61
    reopened = SwarmRuntime(service.settings, backend_factory=lambda _: pytest.fail("Recovery must not replay completed readers"),
                            state_root=service._state_root)
    reopened._store(capture).clock = lambda: now[0]
    return reopened, capture, run_id


def test_recovery_retains_owner_and_duplicate_continue_preserves_live_control_object(setup):
    reopened, capture, run_id = expired_reader_runtime(setup)
    try:
        view = reopened.operate(capture, {"request_id": "view-old", "run_id": run_id})["run"]
        owned = reopened.operate(capture, {"action": "recover", "request_id": "takeover", "run_id": run_id,
                                          "expected_revision": view["run"]["revision"]})["run"]
        assert owned["recovery"]["owns_lease"] and owned["run"]["state"] == "recovery_required"
        assert len(owned["attempts"]) == 2
        intent = {"action": "continue_recovered", "request_id": "continue", "run_id": run_id,
                  "expected_revision": owned["run"]["revision"], "retry_work_items": [], "worker_requests": 2}
        continued = reopened.operate(capture, intent)["run"]
        runner = reopened._runners[run_id][1]
        reopened.operate(capture, intent)
        assert reopened._runners[run_id][1] is runner
        assert continued["run"]["state"] == "running" and len(continued["attempts"]) == 2
        with pytest.raises(IdempotencyConflict):
            reopened.operate(capture, {**intent, "worker_requests": 3})
        for index, submission in enumerate(continued["submissions"]):
            attempt = next(row for row in continued["attempts"] if row["id"] == submission["attempt_id"])
            continued = reopened.operate(capture, {"action": "review_read_result", "request_id": f"review-old-{index}",
                "run_id": run_id, "expected_revision": continued["run"]["revision"], "attempt_id": attempt["id"],
                "attempt_epoch": attempt["epoch"], "candidate_revision": submission["candidate_revision"],
                "evidence": "Current owner inspected the retained exact finding after recovery"})["run"]
        completed = reopened.operate(capture, {"action": "complete", "request_id": "complete-recovered", "run_id": run_id,
                                              "expected_revision": continued["run"]["revision"]})["run"]
        assert completed["run"]["state"] == "completed"
        assert len(completed["check_receipts"]) == 2
        assert {attempt["epoch"] for attempt in completed["attempts"]} == {1}
    finally:
        reopened.close()


def test_lost_takeover_acknowledgement_keeps_exact_epoch_identity(setup, monkeypatch):
    from lumi.engine.swarming.supervisor import SwarmSupervisor
    reopened, capture, run_id = expired_reader_runtime(setup)
    original = SwarmSupervisor.acquire
    first = [True]
    def lost_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if first[0]:
            first[0] = False
            raise OSError("Fixture commit acknowledgement lost")
        return result
    monkeypatch.setattr(SwarmSupervisor, "acquire", lost_ack)
    try:
        view = reopened.operate(capture, {"request_id": "view", "run_id": run_id})["run"]
        intent = {"action": "recover", "request_id": "takeover", "run_id": run_id,
                  "expected_revision": view["run"]["revision"]}
        with pytest.raises(OSError, match="acknowledgement"):
            reopened.operate(capture, intent)
        retained = reopened.operate(capture, {"request_id": "inspect", "run_id": run_id})["run"]
        assert retained["recovery"]["requested_epoch"] == 1 and retained["recovery"]["acquired_epoch"] is None
        retry = reopened.operate(capture, {**intent, "expected_revision": retained["run"]["revision"]})["run"]
        assert retry["run"]["epoch"] == 2 and retry["recovery"]["owns_lease"]
        reopened.operate(capture, {"action": "stop", "request_id": "stop", "run_id": run_id,
                                  "expected_revision": retry["run"]["revision"]})
    finally:
        reopened.close()


def test_recovered_exact_pending_proposal_can_be_reviewed_without_replaying_coordinator(setup):
    service, capture, _ = setup
    now = [time.time()]
    service._store(capture).clock = lambda: now[0]
    def planner(spec):
        backend = StreamingBackend(model=spec.model, events=[text_delta(plan_response()), done()])
        backend.name = spec.backend_type
        return backend
    service._factory = planner
    enable(service, capture)
    run_id = service.operate(capture, start_request(plan_mode="coordinator", tasks=None, request_limit=9,
        coordinator_requests=3, worker_requests=3))["run"]["run"]["id"]
    wait_stopped(service, run_id)
    old = service._runners[run_id][1]
    old._maintenance_stop.set()
    old._maintenance.join(timeout=1)
    now[0] += 61
    providers = []
    def reader(spec):
        backend = StreamingBackend(model=spec.model, events=[text_delta("Recovered plan source finding"), done()])
        backend.name = spec.backend_type
        providers.append(backend)
        return backend
    reopened = SwarmRuntime(service.settings, backend_factory=reader, state_root=service._state_root)
    reopened._store(capture).clock = lambda: now[0]
    try:
        current = reopened.operate(capture, {"request_id": "view", "run_id": run_id})["run"]
        current = reopened.operate(capture, {"action": "recover", "request_id": "takeover", "run_id": run_id,
            "expected_revision": current["run"]["revision"]})["run"]
        current = reopened.operate(capture, {"action": "continue_recovered", "request_id": "continue", "run_id": run_id,
            "expected_revision": current["run"]["revision"], "retry_work_items": [], "worker_requests": 3})["run"]
        assert providers == [] and current["attempts"][0]["kind"] == "coordinator"
        proposal = current["coordinator_proposals"][0]
        reopened.operate(capture, {"action": "decide_proposal", "request_id": "review-retained-plan", "run_id": run_id,
            "expected_revision": current["run"]["revision"], "proposal_id": proposal["id"], "sha256": proposal["sha256"],
            "accept": True, "evidence": "Current owner reviewed the retained exact plan and unchanged contract"})
        until(lambda: len(reopened._store(capture).snapshot(capture.scope, run_id)["submissions"]) == 2)
        wait_stopped(reopened, run_id)
        current = reopened.operate(capture, {"request_id": "findings", "run_id": run_id})["run"]
        assert len(providers) == 2 and len(current["attempts"]) == 3
        assert [row["epoch"] for row in current["attempts"] if row["kind"] == "coordinator"] == [1]
        assert {row["epoch"] for row in current["attempts"] if row["kind"] == "worker"} == {2}
    finally:
        reopened.close()


@pytest.mark.parametrize("total", [6, 7])
def test_manual_reader_repair_dispatches_under_original_allowance(setup, total):
    service, capture, instances = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request(request_limit=total))["run"]["run"]["id"]
    wait_stopped(service, run_id)
    current = service.operate(capture, {"request_id": "view", "run_id": run_id})["run"]
    original = current["attempts"]
    allocations = {row["attempt_id"]: row["amount"] for row in current["reservations"]}
    for index, attempt in enumerate(original):
        current = service.operate(capture, {"action": "reject_result", "request_id": f"reject-{index}", "run_id": run_id,
            "expected_revision": current["run"]["revision"], "attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"],
            "evidence": "The retained finding omitted the requested edge case"})["run"]
        service.operate(capture, {"action": "retry_work", "request_id": f"retry-{index}", "run_id": run_id,
            "expected_revision": current["run"]["revision"], "work_item_id": attempt["work_item_id"],
            "evidence": "Investigate the missing edge case within the same original scope"})
        until(lambda: len(service._store(capture).snapshot(capture.scope, run_id)["submissions"]) == 3 + index)
        wait_stopped(service, run_id)
        current = service.operate(capture, {"request_id": "repaired", "run_id": run_id})["run"]
        replacement = next(row for row in current["attempts"] if row["work_item_id"] == attempt["work_item_id"] and row["id"] != attempt["id"])
        assert replacement["state"] == "submitted"
        allocation = next(row["amount"] for row in current["reservations"] if row["attempt_id"] == replacement["id"])
        assert allocation == allocations[attempt["id"]]
        assert next(row for row in current["attempts"] if row["id"] == attempt["id"])["state"] == "failed"
    assert len(instances) == 4 and len(current["attempts"]) == 4
    assert current["run"]["request_limit"] == total
