"""Native model tools write only a leased Git worktree, then finalize as host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.execution import SwarmExecutionGuard
from lumi.engine.swarming.integration import CheckSpec, SwarmIntegration
from lumi.engine.swarming.models import ScopeDenied
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def git(path, *args):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=disabled-hooks", *args],
        cwd=path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                    authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def fixture(tmp_path):
    if os.name != "nt":
        pytest.skip("Supervised writers require Windows named-job process containment")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "private").mkdir()
    (project / "src" / "fact.txt").write_text("base fact\n")
    (project / "private" / "fact.txt").write_text("preserve private\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Isolated fixture")
    base = git(project, "rev-parse", "HEAD")
    store = SwarmStore(tmp_path / "runtime" / "state.sqlite")
    supervisor = SwarmSupervisor(store)
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Modify fixture", request_limit=10, lease_seconds=300,
        policy=PolicyProfile(1, frozenset({"file_read", "file_write", "file_edit", "glob", "grep", "swarm_submit"}),
                             frozenset({"ollama"}), read_roots=(".",), write_roots=(".",)))
    integration = SwarmIntegration(store, project, root=tmp_path / "runtime" / "worktrees")
    return supervisor, authority, integration, project, base


def assign(fixture, *, read_roots=("src",), write_roots=("src",)):
    supervisor, authority, integration, _, base = fixture
    tools = ["file_read", "file_write", "file_edit", "glob", "grep", "swarm_submit"]
    command(supervisor, authority, "plan", {"work_items": [{"id": "writer", "objective": "Change fixture files",
        "role": "implement", "tools": tools, "read_roots": list(read_roots),
        "write_roots": list(write_roots), "criteria": ["fixture-check"]}]})
    assigned = command(supervisor, authority, "assign", {"work_item_id": "writer", "worker_id": "writer",
        "requests": 6, "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], "writer", authority.epoch)
    record = integration.create_writer(authority, context, base_revision=base)
    return context, record


class Backend(StreamingBackend):
    def __init__(self, **kwargs):
        super().__init__(model="chosen", **kwargs)
        self.closed = False
        self.tool_names = []

    def stream(self, **kwargs):
        self.tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        yield from super().stream(**kwargs)

    def close(self):
        self.closed = True


def runner(fixture, backend):
    supervisor, authority, integration, project, _ = fixture
    return SwarmWorkerRunner(supervisor, authority, project, integration=integration,
                             backend_factory=lambda spec: backend, writer_process_factory=None)


def explain(runtime):
    """Each worker's state and error and the last events, as one string: a CI
    log shows only a line of any longer, non-string assertion message."""
    polled = runtime.poll(limit=1000)
    return json.dumps({"workers": [{key: row[key] for key in ("state", "error", "alive", "termination_recorded")}
                                   for row in polled["workers"]],
                       "last_events": [{key: value for key, value in event.items() if key in {"event", "message", "error", "outcome"}}
                                       for event in polled["events"]][-12:]}, default=str)


def finished(runtime, context):
    # A writer's worker runs, then its result is committed through owned Git
    # processes: seconds each on a loaded runner.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if not runtime.inspect(context.attempt_id)["alive"]:
            return
        time.sleep(.01)
    raise AssertionError(explain(runtime))


def snapshot(fixture):
    supervisor, authority, *_ = fixture
    return supervisor.store.snapshot(authority.scope, authority.run_id)


def test_a_writer_cancelled_while_its_result_waits_for_the_repository_commits_nothing(fixture):
    # A writer's result waits for the repository while another step holds it
    # (a long check, say). Cancelling that writer ends the wait at once, and
    # nothing of it is committed afterwards.
    from lumi.engine.swarming.models import RevisionConflict
    from tests.test_swarm_integration import repository_held
    context, writer = assign(fixture)
    backend = Backend(scripts=[[tool_call("file_write", {"path": "src/fact.txt", "content": "cancelled change\n"}, "write-1"), done()],
                               [text_delta("Changed the isolated file."), done()]])
    runtime = runner(fixture, backend)
    try:
        with repository_held(fixture[2]) as release:
            runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
            deadline = time.monotonic() + 90
            while runtime.inspect(context.attempt_id)["state"] != "waiting_for_repository":
                assert time.monotonic() < deadline, explain(runtime)
                time.sleep(.01)
            for attempt in range(3):  # the team's own work can refuse it first, before anything commits
                try:
                    runtime.cancel_worker(context.attempt_id, context.epoch, command_id="cancel",
                                          expected_revision=snapshot(fixture)["run"]["revision"])
                    break
                except RevisionConflict:
                    if attempt == 2:
                        raise
            finished(runtime, context)
            assert not release.is_set()  # the cancel ended the wait, not the other step
        state = snapshot(fixture)
        assert runtime.inspect(context.attempt_id)["state"] == "cancelled", explain(runtime)
        assert state["attempts"][0]["state"] == "cancelled" and not state["submissions"]
        assert state["writer_worktrees"][0]["state"] == "active"
        assert git(Path(writer["path"]), "rev-parse", "HEAD") == fixture[4]
    finally:
        runtime.close()


def test_model_file_write_and_edit_finalize_to_checked_candidate_without_touching_checkout(fixture):
    context, writer = assign(fixture)
    backend = Backend(scripts=[[
        tool_call("glob", {"path": "src", "pattern": "*.txt"}, "search-1"),
        tool_call("file_write", {"path": "src/new.txt", "content": "new isolated file\n"}, "write-1"),
        tool_call("file_edit", {"path": "src/fact.txt", "old_text": "base", "new_text": "updated"}, "edit-1"), done()],
        [text_delta("Updated both isolated files; checks require host review."), done()],
    ])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        status = runtime.inspect(context.attempt_id)
        assert status["state"] == "submitted", explain(runtime)
        assert backend.closed and backend._supervised_single_request
        assert "swarm_submit" not in backend.tool_names
        state = snapshot(fixture)
        recorded_writer = state["writer_worktrees"][0]
        assert recorded_writer["state"] == "ready"
        assert state["submissions"][0]["candidate_revision"] == recorded_writer["result_revision"]
        assert state["attempts"][0]["process_state"] == "stopped"
        assert len(state["action_receipts"]) == 3
        assert all(action["state"] == "completed" for action in state["action_receipts"])
        assert {action["call_id"] for action in state["action_receipts"]} == {"search-1", "write-1", "edit-1"}
        assert all(action["request_id"] == state["model_requests"][0]["id"] for action in state["action_receipts"])
        _, authority, integration, project, base = fixture
        check = CheckSpec("fixture-check", (sys.executable, "-c",
            "from pathlib import Path; assert Path('src/fact.txt').read_text() == 'updated fact\\n'; "
            "assert Path('src/new.txt').read_text() == 'new isolated file\\n'; print('fixture checked')"), 10)
        candidate = integration.prepare_candidate(authority, writer_ids=(writer["id"],), required_checks=(check,))
        receipt = integration.run_check(authority, candidate["id"], "fixture-check")
        assert receipt["state"] == "passed" and receipt["candidate_revision"] == candidate["result_revision"]
        assert git(project, "rev-parse", "HEAD") == base
        assert (project / "src" / "fact.txt").read_text() == "base fact\n"
        assert not (project / "src" / "new.txt").exists()
    finally:
        runtime.close()


def test_new_files_keep_their_case_in_the_writer_commit(fixture):
    """The writer's commit names new files as the model spelled them.

    On Windows the path sandbox used to hand tools a case-folded path, so a
    writer's src/NewModule.py was committed as src/newmodule.py, and applied
    to the person's repository that way (tests/test_file_name_case.py).
    """
    context, writer = assign(fixture)
    backend = Backend(scripts=[[
        tool_call("file_write", {"path": "src/NewModule.py", "content": "VALUE = 1\n"}, "write-1"),
        tool_call("file_write", {"path": "src/Nested/Helper.PY", "content": "HELPER = 2\n"}, "write-2"), done()],
        [text_delta("Added two modules; checks require host review."), done()],
    ])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        assert runtime.inspect(context.attempt_id)["state"] == "submitted", explain(runtime)
        state = snapshot(fixture)
        assert [action["state"] for action in state["action_receipts"]] == ["completed", "completed"], explain(runtime)
        recorded_writer = state["writer_worktrees"][0]
        project = fixture[3]
        names = git(project, "ls-tree", "-r", "--name-only", recorded_writer["result_revision"]).splitlines()
        # A new folder keeps the model's spelling too.
        assert {"src/NewModule.py", "src/Nested/Helper.PY"} <= set(names), names
        assert not {"src/newmodule.py", "src/nested/helper.py"} & set(names), names
    finally:
        runtime.close()


@pytest.mark.parametrize("name,args", [
    ("file_write", {"path": "private/fact.txt", "content": "escape"}),
    ("file_write", {"path": "../outside.txt", "content": "escape"}),
    ("file_write", {"path": ".git", "content": "gitdir: elsewhere"}),
    ("file_edit", {"path": ".GiT/config", "old_text": "a", "new_text": "b"}),
    ("swarm_submit", {"handoff": "Forged completion", "candidate_revision": "fake"}),
    ("bash", {"command": "echo forbidden"}),
    ("mcp_fixture_write", {"path": "src/fact.txt"}),
    ("task", {"prompt": "spawn another writer"}),
])
def test_writer_forged_escape_and_unqualified_tools_have_no_effect(fixture, name, args):
    context, writer = assign(fixture)
    backend = Backend(events=[tool_call(name, args), done()])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        state = snapshot(fixture)
        assert not state["action_receipts"] and not state["submissions"]
        assert (Path(writer["path"]) / "private/fact.txt").read_text() == "preserve private\n"
        assert (Path(writer["path"]) / ".git").read_text().startswith("gitdir: ")
        assert state["writer_worktrees"][0]["state"] == "active"
    finally:
        runtime.close()


def test_file_edit_requires_read_scope_in_addition_to_write_scope(fixture):
    context, writer = assign(fixture, read_roots=("private",), write_roots=("src",))
    backend = Backend(events=[tool_call("file_edit", {"path": "src/fact.txt", "old_text": "base", "new_text": "changed"}), done()])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        assert not snapshot(fixture)["action_receipts"]
        assert (Path(writer["path"]) / "src/fact.txt").read_text() == "base fact\n"
    finally:
        runtime.close()


def test_writer_grant_cannot_fall_back_to_original_checkout_or_forged_writer(fixture):
    context, writer = assign(fixture)
    backend = Backend(events=[done()])
    runtime = runner(fixture, backend)
    try:
        with pytest.raises(ScopeDenied):
            runtime.start(context, BackendSpec("ollama", "chosen"))
        with pytest.raises(ScopeDenied):
            runtime.start(context, BackendSpec("ollama", "chosen"), writer_id="foreign-writer")
        assert backend.stream_count == 0
        assert not runtime.inspect_all()
        assert Path(writer["path"]) != fixture[3]
    finally:
        runtime.close()


def test_hardlinked_write_target_cannot_change_outside_file(fixture, tmp_path):
    context, writer = assign(fixture)
    outside = tmp_path / "outside.txt"
    outside.write_text("preserved")
    os.link(outside, Path(writer["path"]) / "src/alias.txt")
    backend = Backend(events=[tool_call("file_write", {"path": "src/alias.txt", "content": "overwrite"}), done()])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        assert outside.read_text() == "preserved"
        assert not snapshot(fixture)["action_receipts"]
    finally:
        runtime.close()


def test_stop_during_writer_validation_commits_before_filesystem_check_finishes(fixture, monkeypatch):
    context, writer = assign(fixture)
    entered, release = threading.Event(), threading.Event()
    original = SwarmExecutionGuard._validate_writer

    def gate(guard):
        entered.set()
        assert release.wait(20)
        return original(guard)

    monkeypatch.setattr(SwarmExecutionGuard, "_validate_writer", gate)
    backend = Backend(events=[tool_call("file_write", {"path": "src/fact.txt", "content": "changed"}), done()])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        assert entered.wait(20)
        started = time.monotonic()
        runtime.stop()
        assert time.monotonic() - started < 1
        release.set()
        finished(runtime, context)
        assert backend.stream_count == 0
        assert not snapshot(fixture)["action_receipts"]
        assert (Path(writer["path"]) / "src/fact.txt").read_text() == "base fact\n"
    finally:
        release.set()
        runtime.close()


def test_finalization_failure_retains_intent_and_records_actual_closed_worker(fixture, monkeypatch):
    context, writer = assign(fixture)
    integration = fixture[2]
    original = integration._git

    def fail_commit(path, *args, **kwargs):
        if args and args[0] == "commit":
            raise OSError("fixture Git commit unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(integration, "_git", fail_commit)
    backend = Backend(scripts=[[tool_call("file_write", {"path": "src/fact.txt", "content": "changed"}), done()],
                               [text_delta("Changed isolated file."), done()]])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        state = snapshot(fixture)
        assert backend.closed
        # Commit failed before launch. Every earlier effect is observed stopped,
        # so the isolated partial tree is a known failure, not an unknown writer.
        assert state["writer_worktrees"][0]["state"] == "failed"
        assert state["writer_worktrees"][0]["result_revision"] == ""
        assert git(Path(writer["path"]), "rev-parse", "HEAD") == fixture[4]
        assert (Path(writer["path"]) / "src/fact.txt").read_text() == "changed"
        assert all(row["state"] == "stopped" for row in state["integration_processes"])
        assert state["attempts"][0]["process_state"] == "stopped"
        assert not state["submissions"]
        assert runtime.inspect(context.attempt_id)["termination_recorded"]
        assert "unavailable" in runtime.inspect(context.attempt_id)["error"]
    finally:
        runtime.close()


def test_root_search_rejects_git_metadata(fixture):
    context, writer = assign(fixture, read_roots=(".",))
    backend = Backend(events=[tool_call("glob", {"path": ".", "pattern": "**/*"}), done()])
    runtime = runner(fixture, backend)
    try:
        runtime.start(context, BackendSpec("ollama", "chosen"), writer_id=writer["id"])
        finished(runtime, context)
        assert not snapshot(fixture)["action_receipts"]
        assert "Git administration" in json.dumps(runtime.poll())
    finally:
        runtime.close()
