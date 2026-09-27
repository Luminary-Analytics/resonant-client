"""Owner configured writes join native execution to reviewed Git integration."""

import sys
import threading
import time

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.models import Conflict, ScopeDenied, SwarmError
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_workers import until
from tests.test_swarm_writers import git


@pytest.fixture
def desktop(tmp_path, request):
    project = tmp_path / getattr(request, "param", "project")
    (project / "src").mkdir(parents=True)
    (project / "src" / "value.txt").write_text("original\n")
    (project / "personal.txt").write_text("committed personal\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    instances = []
    def factory(spec):
        backend = StreamingBackend(model=spec.model, scripts=[
            [tool_call("file_write", {"path": "src/value.txt", "content": "verified change\n"}, "write-1"), done()],
            [text_delta("Changed the isolated file; owner checks must verify it."), done()]])
        backend.name = spec.backend_type
        instances.append(backend)
        return backend
    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(project),
                              BackendSpec("ollama", "chosen"))
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    yield service, capture, project, instances
    service.close()


def request(**extra):
    return {"action": "start", "request_id": "start", "objective": "Change the scoped value",
            "write_roots": ["src"], "request_limit": 6, "worker_requests": 4, "max_workers": 2,
            "checks": [{"key": "value-check", "argv": [sys.executable, "-c",
                "from pathlib import Path; assert Path('src/value.txt').read_text() == 'verified change\\n'"],
                "timeout_seconds": 10}],
            "tasks": [{"objective": "Update src/value.txt", "role": "implement", "read_roots": ["src"],
                       "write_roots": ["src"], "criteria": ["value-check"]}], **extra}


def view(desktop, run_id):
    service, capture, *_ = desktop
    return service.operate(capture, {"request_id": "view", "run_id": run_id})["run"]


def operate(desktop, run_id, action, request_id, **payload):
    service, capture, *_ = desktop
    revision = view(desktop, run_id)["run"]["revision"]
    return service.operate(capture, {"action": action, "request_id": request_id, "run_id": run_id,
                                    "expected_revision": revision, **payload})["run"]


def settled(desktop, run_id):
    until(lambda: bool(view(desktop, run_id)["integration_operations"]) and all(
        row["state"] not in {"queued", "running"} for row in view(desktop, run_id)["integration_operations"]), timeout=15)
    return view(desktop, run_id)


def test_writer_requires_checks_and_cannot_expand_configured_write_scope(desktop):
    service, capture, _, instances = desktop
    with pytest.raises(ValueError, match="checks"):
        service.operate(capture, request(checks=[]))
    with pytest.raises(ScopeDenied, match="exceed"):
        service.operate(capture, request(tasks=[{"objective": "Bad scope", "role": "implement",
            "read_roots": ["."], "write_roots": ["."], "criteria": ["value-check"]}]))
    assert not instances and not service.busy


def test_unsupported_writer_host_is_rejected_before_reservation(desktop, monkeypatch):
    from lumi.engine.swarming.integration import SwarmIntegration
    service, capture, _, instances = desktop
    monkeypatch.setattr(SwarmIntegration, "writer_support", staticmethod(lambda: {
        "supported": False, "reason": "Fixture host lacks process containment"}))
    with pytest.raises(Conflict, match="lacks process containment"):
        service.operate(capture, request())
    assert not instances and not service.busy
    assert service.operate(capture, {"request_id": "inspect"})["run"] is None


@pytest.mark.parametrize("desktop", ["long-project-" + "p" * 48], indirect=True)
def test_writer_changes_combine_under_a_long_project_path(desktop):
    # A live team's changes couldn't be combined: Git names a worktree's admin
    # folder (.git/worktrees/<name>) after its folder, and the long candidate
    # name took it past Windows' path limit ("'$GIT_DIR' too big"). Shorter
    # paths pass everywhere, and this one passes where PATH_MAX is large.
    service, capture, project, _ = desktop
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    until(lambda: len(view(desktop, run_id)["submissions"]) == 1, timeout=15)
    writer = view(desktop, run_id)["writer_worktrees"][0]
    operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer["id"]])
    current = settled(desktop, run_id)
    assert current["integration_candidates"][0]["state"] == "ready", current["integration_operations"]


def test_writer_reaches_exact_reviewed_application_and_preserves_dirty_checkout(desktop):
    service, capture, project, instances = desktop
    original = git(project, "rev-parse", "HEAD")
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    service.operate(capture, request())  # Exact setup retry never dispatches again.
    until(lambda: len(view(desktop, run_id)["submissions"]) == 1, timeout=15)
    current = view(desktop, run_id)
    (project / "personal.txt").write_text("my unfinished work\n")
    assert len(instances) == 1
    assert current["writer_setup"]["base_revision"] == original
    assert current["writer_setup"]["target_branch"] == "refs/heads/main"
    assert (project / "src" / "value.txt").read_text() == "original\n"
    assert (project / "personal.txt").read_text() == "my unfinished work\n"
    with pytest.raises(Conflict, match="Completion"):
        operate(desktop, run_id, "complete", "premature")
    writer = current["writer_worktrees"][0]
    operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer["id"]])
    current = settled(desktop, run_id)
    candidate = current["integration_candidates"][0]
    assert candidate["state"] == "ready"
    operate(desktop, run_id, "run_check", "verify", candidate_id=candidate["id"], check_key="value-check")
    current = settled(desktop, run_id)
    assert current["integration_candidates"][0]["state"] == "verified", current["integration_checks"]
    apply = {"candidate_id": candidate["id"], "expected_base": candidate["base_revision"],
             "target_revision": candidate["result_revision"], "evidence": "Owner reviewed exact isolated changes and check output"}
    operate(desktop, run_id, "apply_candidate", "dirty-apply", **apply)
    current = settled(desktop, run_id)
    assert current["integration_operations"][-1]["state"] == "failed"
    assert git(project, "rev-parse", "HEAD") == original
    assert (project / "personal.txt").read_text() == "my unfinished work\n"
    (project / "personal.txt").write_text("committed personal\n")
    operate(desktop, run_id, "apply_candidate", "reviewed-apply", **apply)
    current = settled(desktop, run_id)
    assert current["integration_candidates"][0]["state"] == "applied"
    assert current["work_items"][0]["state"] == "submitted"  # Applying is not owner acceptance.
    attempt = current["attempts"][0]
    operate(desktop, run_id, "accept_writer", "accept", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
            candidate_id=candidate["id"], evidence="Owner accepted the applied exact candidate and its named check")
    completed = operate(desktop, run_id, "complete", "complete")
    assert completed["run"]["state"] == "completed" and len(completed["writer_acceptances"]) == 1
    assert (project / "src" / "value.txt").read_text() == "verified change\n"
    assert len(instances) == 1
    assert completed["integration_processes"]
    assert all("launch_token" not in row and "cwd" not in row for row in completed["integration_processes"])


def test_dirty_writer_baseline_is_reported_before_reserving_or_launching(desktop):
    service, capture, project, instances = desktop
    (project / "personal.txt").write_text("my unfinished work\n")
    with pytest.raises(Conflict, match="clean committed"):
        service.operate(capture, request())
    assert not service.busy and not instances
    assert (project / "personal.txt").read_text() == "my unfinished work\n"


def test_recovered_writer_git_discovery_cannot_block_stop(desktop, monkeypatch):
    from lumi.engine.swarming import service as service_module
    service, capture, _, _ = desktop
    now = [time.time()]
    service._store(capture).clock = lambda: now[0]
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    until(lambda: len(view(desktop, run_id)["submissions"]) == 1, timeout=15)
    old = service._runners[run_id][1]
    old._maintenance_stop.set()
    old._maintenance.join(timeout=1)
    now[0] += 61
    reopened = SwarmRuntime(service.settings, backend_factory=lambda _: pytest.fail("Stopped recovery cannot replay"),
                            state_root=service._state_root)
    reopened._store(capture).clock = lambda: now[0]
    captured = (reopened, capture, None, None)
    entered, release = threading.Event(), threading.Event()
    real_integration = service_module.SwarmIntegration
    def slow_git(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_integration(*args, **kwargs)
    outcome = []
    thread = None
    try:
        owned = operate(captured, run_id, "recover", "takeover")
        monkeypatch.setattr(service_module, "SwarmIntegration", slow_git)
        def resume():
            try:
                reopened.operate(capture, {"action": "continue_recovered", "request_id": "resume", "run_id": run_id,
                    "expected_revision": owned["run"]["revision"], "retry_work_items": [], "worker_requests": 4})
            except SwarmError as exc:
                outcome.append(type(exc).__name__)
        thread = threading.Thread(target=resume)
        thread.start()
        assert entered.wait(3)
        started = time.monotonic()
        stopped = operate(captured, run_id, "stop", "stop-during-git")
        assert time.monotonic() - started < 1
        assert stopped["run"]["stop_requested"] == 1
        assert stopped["run"]["state"] == "recovery_required"  # Stop does not bypass explicit recovery settlement.
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive() and outcome == ["RevisionConflict"]
        assert not reopened._runners
    finally:
        release.set()
        if thread:
            thread.join(timeout=5)
        reopened.close()
