"""Owner configured writes join native execution to reviewed Git integration."""

import sys
import threading
import time

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.models import Conflict, RevisionConflict, ScopeDenied, SwarmError
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
    """One owner action at the revision just seen, as the panel sends it.

    The runner's lease renewal (every 5 s) also advances the revision, so on a
    busy runner one can land between the view and the action, which is then
    refused before anything commits. The owner refreshes and sends it again.
    """
    service, capture, *_ = desktop
    for attempt in range(3):
        revision = view(desktop, run_id)["run"]["revision"]
        try:
            return service.operate(capture, {"action": action, "request_id": request_id, "run_id": run_id,
                                            "expected_revision": revision, **payload})["run"]
        except RevisionConflict:
            if attempt == 2:
                raise


# Creating a writer's worktree, running its worker and committing its result
# go through owned Git processes: seconds each on a loaded runner.
PIPELINE_SECONDS = 90


def progress(desktop, run_id):
    """Where the run got to, for a wait that timed out: its state, each
    attempt, writer and worker (with its error) and each operation."""
    service = desktop[0]
    current = view(desktop, run_id)
    runner = service._runners.get(run_id, (None, None))[1]
    return {"run": current["run"]["state"],
            "attempts": [(row["state"], row["process_state"]) for row in current["attempts"]],
            "writers": [row["state"] for row in current["writer_worktrees"]],
            "workers": [(row["state"], row["error"]) for row in (runner.inspect_all() if runner else [])],
            "operations": [(row["kind"], row["state"], row["error"]) for row in current["integration_operations"]]}


def submitted(desktop, run_id):
    until(lambda: len(view(desktop, run_id)["submissions"]) == 1, timeout=PIPELINE_SECONDS,
          describe=lambda: progress(desktop, run_id))


def settled(desktop, run_id):
    until(lambda: bool(view(desktop, run_id)["integration_operations"]) and all(
        row["state"] not in {"queued", "running"} for row in view(desktop, run_id)["integration_operations"]),
        timeout=PIPELINE_SECONDS, describe=lambda: progress(desktop, run_id))
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
    submitted(desktop, run_id)
    writer = view(desktop, run_id)["writer_worktrees"][0]
    operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer["id"]])
    current = settled(desktop, run_id)
    assert current["integration_candidates"][0]["state"] == "ready", current["integration_operations"]


def test_writer_reaches_exact_reviewed_application_and_preserves_dirty_checkout(desktop):
    service, capture, project, instances = desktop
    original = git(project, "rev-parse", "HEAD")
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    service.operate(capture, request())  # Exact setup retry never dispatches again.
    submitted(desktop, run_id)
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
    # The commit that lands in the person's branch says what the team did, and where it came from.
    applied = git(project, "log", "-1", "--format=%B").strip().splitlines()
    assert applied[0] == "Lumi team: Change the scoped value"
    assert applied[2:] == [f"Team run: {run_id}", f"Combined change: {candidate['id']}",
                           f"Writers: {writer['attempt_id']}"]
    # A writer's own commit says its task.
    written = git(project, "log", "-1", "--format=%B", current["writer_worktrees"][0]["result_revision"])
    assert written.strip().splitlines() == ["Lumi team: Update src/value.txt", "",
                                             f"Team run: {run_id}", f"Writer: {writer['attempt_id']}"]
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
    with pytest.raises(Conflict, match="Commit or stash your changes, then start the team again") as refused:
        service.operate(capture, request())
    # The refusal names what isn't committed, so the person knows what to commit or stash.
    assert "aren't committed: personal.txt." in str(refused.value)
    assert not service.busy and not instances
    assert (project / "personal.txt").read_text() == "my unfinished work\n"


def test_a_team_commit_is_named_by_its_objective_on_one_line():
    from lumi.engine.swarming.integration import SUBJECT_OBJECTIVE, commit_subject

    assert commit_subject("Fix the login page") == "Lumi team: Fix the login page"
    assert commit_subject("First line\n\n  second\tline") == "Lumi team: First line second line"
    shortened = commit_subject("word " * 40)
    assert len(shortened) <= len("Lumi team: ") + SUBJECT_OBJECTIVE and shortened.endswith("word…")
    assert commit_subject("") == "Lumi team: changes"
    assert commit_subject("bell\x07 and \x1b[31mred") == "Lumi team: bell and [31mred"


def test_the_dirty_checkout_refusal_names_up_to_ten_files():
    from lumi.engine.swarming.integration import status_paths, uncommitted_message

    # git status --porcelain=v1 -z: a rename carries its old name in the next field, which isn't listed.
    output = "\0".join([" M src/app.py", "R  new name.txt", "old name.txt", "?? notes/todo.md", "A  added.py", ""])
    assert status_paths(output) == ["src/app.py", "new name.txt", "notes/todo.md", "added.py"]
    assert status_paths("") == []
    message = uncommitted_message([f"file{index}.txt" for index in range(12)])
    assert "file0.txt, file1.txt" in message and "file9.txt and 2 more." in message and "file10.txt" not in message
    assert message.endswith("Commit or stash your changes, then start the team again. Nothing in the project was changed.")


def test_recovered_writer_git_discovery_cannot_block_stop(desktop, monkeypatch):
    from lumi.engine.swarming import service as service_module
    service, capture, _, _ = desktop
    now = [time.time()]
    service._store(capture).clock = lambda: now[0]
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    submitted(desktop, run_id)
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
        assert release.wait(60)
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
        assert entered.wait(60)  # the resumed run reaches its Git discovery
        started = time.monotonic()
        stopped = operate(captured, run_id, "stop", "stop-during-git")
        assert time.monotonic() - started < 1
        assert stopped["run"]["stop_requested"] == 1
        assert stopped["run"]["state"] == "recovery_required"  # Stop does not bypass explicit recovery settlement.
        release.set()
        thread.join(timeout=60)
        assert not thread.is_alive() and outcome == ["RevisionConflict"]
        assert not reopened._runners
    finally:
        release.set()
        if thread:
            thread.join(timeout=60)
        reopened.close()
