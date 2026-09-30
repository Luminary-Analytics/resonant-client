"""Teams on a new computer: no Git, a conversation's own team, and what an ended team leaves behind."""
# ruff: noqa: F811 -- the imported `setup` and `desktop` fixtures are intentionally used by name.

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from lumi.engine.swarming import service as service_module
from lumi.engine.swarming import cleanup
from lumi.engine.swarming.cleanup import (DISCARDED_EVENT, REMOVED_EVENT, clean_finished_teams, kept_across,
                                          remove_left_worktree)
from lumi.engine.swarming.git_boundary import (REPOSITORY_LOCK_NAME, git_error_line, open_repository_lock,
                                               release_repository_lock, trusted_git_executable, try_repository_lock)
from lumi.engine.swarming.models import Conflict
from lumi.gui import swarming
from tests.test_gui_swarming import enable, setup, start_request, wait_stopped  # noqa: F401 (fixture)
from tests.test_swarm_desktop_writers import (desktop, operate, request, settled, submitted,  # noqa: F401 (fixture)
                                              view)
from tests.test_swarm_workers import until


# ── Without Git ────────────────────────────────────────────────────────────


def test_writer_teams_without_git_say_so(monkeypatch, tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(Conflict, match="Writer teams need Git") as refused:
        trusted_git_executable(tmp_path / "project", tmp_path / "runtime")
    assert "Read-only teams work without it" in str(refused.value)


def test_a_git_inside_the_project_is_still_refused_as_before(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shadow = project / ("git.exe" if os.name == "nt" else "git")
    shadow.write_bytes(b"not git")
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", str(project))
    with pytest.raises(Conflict, match="outside the project"):
        trusted_git_executable(project, tmp_path / "runtime")


@pytest.mark.skipif(os.name != "nt", reason="git.cmd launchers are Windows'")
def test_a_git_cmd_launcher_counts_as_git_and_its_git_exe_runs(monkeypatch, tmp_path):
    """git_support.git_available() finds git.cmd on PATH; writer teams must not then say Git is missing."""
    install = tmp_path / "Git"
    (install / "cmd").mkdir(parents=True)
    (install / "bin").mkdir()
    (install / "cmd" / "git.cmd").write_text('@"%~dp0..\\bin\\git.exe" %*\r\n', encoding="ascii")
    (install / "bin" / "git.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PATH", str(install / "cmd"))
    # Run as the git.exe it launches: a script would pass every argument through cmd.exe's parser.
    assert trusted_git_executable(tmp_path / "project", tmp_path / "runtime") == str((install / "bin" / "git.exe").resolve())
    (install / "bin" / "git.exe").unlink()
    with pytest.raises(Conflict, match="command script") as refused:
        trusted_git_executable(tmp_path / "project", tmp_path / "runtime")
    assert "isn't installed" not in str(refused.value) and str(install / "cmd" / "git.cmd") in str(refused.value)


def test_the_team_panel_names_the_models_real_failure(monkeypatch, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    saved = tmp_path / "sessions"
    saved.mkdir()
    session_id = "20260927_120000_abcd"
    (saved / f"{session_id}.json").write_text("{}")
    monkeypatch.setattr(swarming, "is_valid_session_id", lambda value: value == session_id)
    monkeypatch.setattr(swarming, "_sessions_dir", lambda _: saved)
    state = SimpleNamespace(project=SimpleNamespace(project_path=str(workspace), current_session=SimpleNamespace(id=session_id)),
                            backend_spec=None, runtime_error="ollama (stub) failed to start: Git isn't installed")
    with pytest.raises(Conflict, match="model isn't running: ollama \\(stub\\) failed to start: Git isn't installed"):
        swarming._capture(state, {"project": str(workspace), "session_id": session_id}, SimpleNamespace())
    state.runtime_error = ""
    with pytest.raises(Conflict, match="Choose a configured provider"):
        swarming._capture(state, {"project": str(workspace), "session_id": session_id}, SimpleNamespace())


def test_git_failures_keep_gits_own_reason():
    stderr = "Preparing worktree (new branch 'lumi/team-1')\nfatal: '$GIT_DIR' too big\n"
    assert git_error_line(stderr) == "fatal: '$GIT_DIR' too big"
    assert git_error_line("error: pathspec 'x' did not match\nhint: more") == "error: pathspec 'x' did not match"
    assert git_error_line("something happened\n") == "something happened"
    assert git_error_line("") == ""


# ── Only the team's own conversation waits ─────────────────────────────────


def test_an_unfinished_team_blocks_only_its_own_conversation(setup):
    service, capture, _ = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    found = service.blocking("project", "session")
    assert found["run_id"] == run_id and found["reason"] == "team" and found["owned"] == "yes"
    assert found["objective"] == "Investigate two independent questions"
    assert service.blocking("project", "another-session") is None
    assert service.blocking("another-project", "session") is None


def test_a_team_blocks_its_conversation_the_moment_it_starts(setup):
    """Not only after the observer's next refresh (every 0.2 s) records its owner."""
    service, capture, _ = setup
    enable(service, capture)
    service._observer_stop.set()  # the observer records nothing new from here on
    if service._observer is not None:
        service._observer.join(timeout=2)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    found = service.blocking("project", "session")
    assert found and found["reason"] == "team" and found["run_id"] == run_id and found["owned"] == "yes"
    assert service.blocking("project", "another-session") is None
    wait_stopped(service, run_id)


def test_an_active_run_without_a_known_owner_holds_every_conversation(setup):
    service, capture, _ = setup
    service._observer_stop.set()
    service._active.add("swarm_" + "0" * 64)  # active, owner not recorded yet
    for session in ("session", "another-session"):
        assert service.blocking("project", session)["reason"] == "unknown"
    state = SimpleNamespace(project=SimpleNamespace(project_path="C:/p", current_session=SimpleNamespace(id="s")),
                            _swarm_desktop=service)
    assert swarming.busy(state)
    assert swarming.busy_refusal(state)["message"].startswith("A team is starting")


def test_a_chat_message_right_after_a_team_starts_is_refused(tmp_path, monkeypatch):
    """The owner's conversation (or a second window on it) in the first fraction of a second."""
    from lumi.engine.swarming.service import SwarmRuntime
    from lumi.gui.runtime import BackendSpec
    from lumi.gui.settings import SettingsManager
    from lumi.gui.ws_commands import CommandContext, HANDLERS
    from tests.streaming_stub import StreamingBackend, done, text_delta

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    saved = tmp_path / "sessions"
    saved.mkdir()
    session_id = "20260927_123456_abcd"
    (saved / f"{session_id}.json").write_text("{}")
    monkeypatch.setattr(swarming, "is_valid_session_id", lambda value: value == session_id)
    monkeypatch.setattr(swarming, "_sessions_dir", lambda _: saved)
    settings = SettingsManager(tmp_path / "settings.json")

    def factory(spec):
        backend = StreamingBackend(events=[text_delta("Inspected; owner review required."), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        return backend

    service = SwarmRuntime(settings, backend_factory=factory, state_root=lambda _: tmp_path / "state")
    state = SimpleNamespace(
        project=SimpleNamespace(project_path=str(workspace), current_session=SimpleNamespace(id=session_id)),
        backend_spec=BackendSpec("ollama", "chosen"), settings=settings, runtime_error="",
        session=SimpleNamespace(project_instructions=""), _swarm_desktop=service,
        get_init_data=lambda **kw: {"event": "init"})
    replies, events, queued = [], [], []

    async def send(payload):
        replies.append(payload)

    class Socket:
        async def send_json(self, payload):
            events.append(payload)

    class Runs:
        busy = False

        async def enqueue(self, msg):
            queued.append(msg)

    base = {"command": "swarm", "project": str(workspace), "session_id": session_id}
    try:
        asyncio.run(swarming.command(state, send, {**base, "request_id": "enable", "action": "configure", "enabled": True}))
        service._observer_stop.set()  # as if the observer hadn't refreshed yet
        asyncio.run(swarming.command(state, send, {
            **base, "request_id": "start-1", "action": "start", "objective": "Investigate two questions",
            "tasks": [{"objective": "Inspect A", "read_roots": ["."]}, {"objective": "Inspect B", "read_roots": ["."]}],
            "request_limit": 4, "max_workers": 2}))
        assert "error" not in replies[-1], replies[-1]
        asyncio.run(HANDLERS["message"](CommandContext(Socket(), state, {"command": "message", "text": "hello"}, Runs())))
        assert not queued, "a chat turn was admitted into the team's own conversation while the team runs"
        assert events[-1]["code"] == "team_active" and "Investigate two questions" in events[-1]["message"]
    finally:
        service.close()


def test_continuing_a_recovered_team_waits_for_the_chat_turn(setup):
    service, capture, instances = setup
    state = SimpleNamespace(_swarm_desktop=service, _swarm_starting=False)
    replies = []

    async def send(value):
        replies.append(value)

    asyncio.run(swarming.command(state, send, {"action": "continue_recovered", "request_id": "continue",
                                               "run_id": "swarm_" + "1" * 64}, chat_busy=True))
    assert "Finish or stop the current operation" in replies[0]["error"]
    assert not state._swarm_starting and not instances


def test_a_team_from_before_a_restart_blocks_only_its_conversation(setup):
    service, capture, _ = setup
    enable(service, capture)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    from lumi.engine.swarming.service import SwarmRuntime

    reopened = SwarmRuntime(service.settings, state_root=service._state_root)
    try:
        reopened.watch_project(capture.workspace, replace(capture.scope, session_id="discovery"))
        assert reopened.busy  # the retained work still closes new team work in that project
        found = reopened.blocking("project", "session")
        assert found and found["run_id"] == run_id and not found["owned"] and not found["recovering"]
        assert reopened.blocking("project", "another-session") is None
    finally:
        reopened.close()


def test_a_new_team_is_refused_with_the_unfinished_team_and_its_conversation(setup, tmp_path):
    service, capture, _ = setup
    enable(service, capture)
    from lumi.gui.sessions import _sessions_dir

    saved = _sessions_dir(capture.workspace)
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "session.json").write_text(json.dumps({"id": "session", "title": "Refactor the parser"}), encoding="utf-8")
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    other = replace(capture, scope=replace(capture.scope, session_id="another-session"))
    with pytest.raises(Conflict) as refused:
        service.operate(other, start_request(request_id="second-team"))
    message = str(refused.value)
    assert "“Investigate two independent questions”" in message and "“Refactor the parser”" in message
    assert "Stop team" in message and Path(capture.workspace).name in message


def _state(blocking):
    return SimpleNamespace(project=SimpleNamespace(project_path="C:/p", current_session=SimpleNamespace(id="s")),
                           _swarm_desktop=SimpleNamespace(blocking=lambda project_id, session_id: blocking))


def test_the_refusal_names_the_team_and_how_to_stop_it():
    live = {"reason": "team", "run_id": "swarm_1", "objective": "Add a team note file", "state": "running",
            "session_id": "s", "owned": "yes"}
    event = swarming.busy_refusal(_state(live))
    assert event["code"] == "team_active" and event["team"]["run_id"] == "swarm_1"
    assert "“Add a team note file”" in event["message"] and "choose Stop team" in event["message"]
    assert "Other conversations aren't affected" in event["message"]
    orphan = swarming.busy_refusal(_state({**live, "owned": ""}), "changing this conversation's model")
    assert "Take over expired team and review its interrupted work" in orphan["message"]
    assert orphan["message"].rstrip(".").endswith("aren't affected")
    assert "before changing this conversation's model" in orphan["message"]
    recovering = swarming.busy_refusal(_state({**live, "owned": "", "recovering": "yes"}))
    assert "Review interrupted work" in recovering["message"] and "Finish stopped team" in recovering["message"]
    assert recovering["team"]["recovering"] is True
    assert swarming.busy(_state(None)) is False and swarming.busy(_state(live)) is True
    assert "can't read its saved team records" in swarming.busy_refusal(_state({"reason": "storage"}))["message"]


def test_the_ownership_observer_survives_a_cleanup_it_cannot_start(setup, monkeypatch):
    """No thread for a cleanup (out of threads or memory) must not end the gate's refresh."""
    service, capture, _ = setup
    enable(service, capture)
    real_thread = threading.Thread
    refused = []

    class NoCleanupThread(real_thread):
        def start(self):
            if self.name == "swarm-cleanup" and not refused:
                refused.append(self.name)
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(service_module.threading, "Thread", NoCleanupThread)
    run_id = service.operate(capture, start_request())["run"]["run"]["id"]
    wait_stopped(service, run_id)
    service.operate(capture, {"action": "stop", "request_id": "stop", "run_id": run_id,
                              "expected_revision": view_run(service, capture, run_id)["revision"]})
    until(lambda: refused and not service._pending_cleanups, timeout=30)  # refused, kept, then retried
    assert service._observer.is_alive() and not service._storage_uncertain
    assert service.blocking("project", "session") is None


def view_run(service, capture, run_id):
    return service.operate(capture, {"request_id": "view", "run_id": run_id})["run"]["run"]


# ── What an ended team leaves in the repository ────────────────────────────


def git(path, *args, check=True):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def link_folder(link: Path, target: Path) -> None:
    """A directory junction on Windows (npm links a file: dependency that way), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def own_library(tmp_path: Path) -> Path:
    """The person's own code outside the repository, which a team's worktree links to."""
    library = tmp_path / "shared-lib"
    library.mkdir(exist_ok=True)
    (library / "index.js").write_text("the person's own code\n", encoding="utf-8")
    return library


def branches(project: Path) -> list[str]:
    return git(project, "branch", "--list", "--format=%(refname:short)", "lumi/team-*", "codex/swarm-writer-*").split()


def worktrees(project: Path) -> list[str]:
    return [line[len("worktree "):] for line in git(project, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


@pytest.fixture
def writer_repo(tmp_path):
    """The real-Git writer fixture of test_swarm_integration.py, under another name."""
    if os.name != "nt":
        pytest.skip("Supervised integration requires Windows named-job process containment")
    from lumi.engine.swarming import Scope, SwarmStore, SwarmSupervisor
    from lumi.engine.swarming.integration import SwarmIntegration
    from lumi.engine.swarming.policy import PolicyProfile

    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    (project / "a.txt").write_text("base-a\n")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    base = git(project, "rev-parse", "HEAD")
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: 1000)
    supervisor = SwarmSupervisor(store)
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read", "file_write"}),
                           allowed_providers=frozenset({"ollama"}), read_roots=(".",), write_roots=(".",), max_workers=4)
    authority = supervisor.create(Scope.personal("owner", "project", "session"), supervisor_id="supervisor",
                                  objective="Implement fixture", request_limit=12, policy=policy)
    integration = SwarmIntegration(store, project, root=tmp_path / "runtime" / "worktrees")
    return store, supervisor, authority, integration, project, base


def _end(store, run_id, state="cancelled"):
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))


def _ready(store, writer_id, result):
    """Record a writer as finalized at ``result`` (its committed change, or its base for none)."""
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE writer_worktrees SET state='ready', result_revision=? WHERE id=?", (result, writer_id))


def _legacy_writer(store, branch, *, state="ready", suffix="-legacy"):
    """A writer recorded by the same run under the pre-rebrand branch prefix, at its base."""
    with sqlite3.connect(store.path) as connection:
        manifest = json.loads(connection.execute("SELECT manifest_json FROM writer_worktrees").fetchone()[0])
        connection.execute("INSERT INTO writer_worktrees(id,run_id,attempt_id,epoch,repo_key,path,base_revision,"
                           "result_revision,state,manifest_json,process_protocol) SELECT 'legacy-writer',run_id,"
                           "'legacy-attempt',epoch,repo_key,path || ?,base_revision,base_revision,?,?,1 "
                           "FROM writer_worktrees LIMIT 1", (suffix, state, json.dumps({**manifest, "branch": branch})))


def _events(store, run_id, kind):
    with sqlite3.connect(store.path) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM events WHERE run_id=? AND kind=? ORDER BY sequence", (run_id, kind))]


def test_what_an_ended_team_keeps_follows_what_was_applied():
    from lumi.engine.swarming.cleanup import classify

    def writer(identity, state, result, base="b" * 40):
        return {"id": identity, "state": state, "base_revision": base, "result_revision": result,
                "manifest_json": json.dumps({"branch": f"lumi/team-{identity}"})}

    def candidate(identity, state, sources=None):
        manifest = {} if sources is None else {"writers": [{"id": source} for source in sources]}
        return {"id": identity, "state": state, "manifest_json": json.dumps(manifest)}

    plan = classify(
        [writer("applied", "ready", "1" * 40), writer("nothing", "ready", "b" * 40),
         writer("stopped", "active", ""), writer("unapplied", "ready", "2" * 40), writer("sent-back", "rejected", "")],
        [candidate("first-try", "failed", ["applied"]), candidate("applied-one", "verified", ["applied"]),
         candidate("with-unapplied", "verified", ["applied", "unapplied"]), candidate("unknown", "uncertain")],
        [{"candidate_id": "applied-one", "state": "applied"}])
    ids = {key: [row["id"] for row in rows] for key, rows in plan.items()}
    assert ids == {"unused": ["applied", "nothing"], "kept": ["stopped", "unapplied", "sent-back"],
                   "unused_candidates": ["first-try"], "kept_candidates": ["with-unapplied", "unknown"],
                   "applied_candidates": ["applied-one"]}


def test_an_ended_teams_unused_worktrees_and_branches_go_both_prefixes(writer_repo, tmp_path):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    assert json.loads(lease["manifest_json"])["branch"].startswith("lumi/team-")
    _ready(store, lease["id"], base)  # finished without a change
    legacy = "codex/swarm-writer-0123456789abcdef"
    git(project, "branch", legacy, base)
    _legacy_writer(store, legacy)
    library = own_library(tmp_path)
    link_folder(Path(lease["path"]) / "node_modules", library)
    # Still running: nothing is touched.
    assert clean_finished_teams(store, project, root=integration.root)["worktrees"] == 0
    assert len(branches(project)) == 2
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert (report["worktrees"], report["branches"], report["left"], report["failed"]) == (1, 2, [], []), report
    assert not Path(lease["path"]).exists() and branches(project) == [] and len(worktrees(project)) == 1
    assert sorted(path.name for path in library.iterdir()) == ["index.js"], "a junction's target lost its files"
    assert (library / "index.js").read_text(encoding="utf-8") == "the person's own code\n"
    assert sorted(_events(store, authority.run_id, REMOVED_EVENT)[0]["writers"]) == sorted([lease["id"], "legacy-writer"])
    # Recorded as done: the next start doesn't even run Git.
    assert clean_finished_teams(store, project, root=integration.root)["worktrees"] == 0


def test_unapplied_work_stays_until_discarded(writer_repo, tmp_path):
    from tests.test_swarm_integration import finish, writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (context, lease), = writers(writer_repo, names=("a",))
    (Path(lease["path"]) / "a.txt").write_text("the writer's change\n")
    manifest = finish(writer_repo, context, lease)
    assert manifest["result_revision"] != base
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert (report["worktrees"], report["branches"]) == (0, 0)
    assert Path(lease["path"]).exists() and len(branches(project)) == 1
    report = clean_finished_teams(store, project, run_ids=(authority.run_id,), root=integration.root, discard=True)
    assert (report["worktrees"], report["branches"]) == (1, 1), report
    assert not Path(lease["path"]).exists() and branches(project) == []
    assert _events(store, authority.run_id, DISCARDED_EVENT)[0]["writers"] == [lease["id"]]


def test_a_branch_someone_committed_to_is_never_deleted(writer_repo, tmp_path):
    """Neither by the automatic cleanup nor by Discard: the commits aren't only the team's."""
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    branch = json.loads(lease["manifest_json"])["branch"]
    # The person salvages the team's branch: commits their own work on it,
    # in the team's worktree (the branch is checked out there).
    (Path(lease["path"]) / "mine.txt").write_text("my own work\n")
    git(lease["path"], "add", "mine.txt")
    git(lease["path"], "commit", "-m", "My own work on the team's branch")
    mine = git(lease["path"], "rev-parse", "HEAD")
    legacy = "codex/swarm-writer-fedcba9876543210"
    git(project, "branch", legacy, base)
    git(project, "checkout", legacy)  # another one, checked out in the person's checkout
    _legacy_writer(store, legacy)
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert sorted((item["branch"], item["reason"]) for item in report["left"]) == [
        (legacy, "in_use"), (branch, "moved")], report
    # The moved branch's worktree stays with it: someone works there.
    assert Path(lease["path"]).exists() and git(project, "rev-parse", branch) == mine
    assert git(project, "rev-parse", "--abbrev-ref", "HEAD") == legacy
    moved = _events(store, authority.run_id, REMOVED_EVENT)[0]["left"]
    assert [(item["branch"], item["worktree"]) for item in moved] == [(branch, lease["path"])]
    # Discard doesn't take them either; the checked-out one waits until it is free.
    report = clean_finished_teams(store, project, run_ids=(authority.run_id,), root=integration.root, discard=True)
    assert [(item["branch"], item["reason"]) for item in report["left"]] == [(legacy, "in_use")]
    assert git(project, "rev-parse", branch) == mine and Path(lease["path"]).exists()
    assert legacy in branches(project) and git(project, "rev-parse", "--abbrev-ref", "HEAD") == legacy
    git(project, "checkout", "main")
    report = clean_finished_teams(store, project, root=integration.root)
    assert report["branches"] == 1 and legacy not in branches(project) and branch in branches(project)


def test_cleanup_never_forgets_the_persons_own_worktree(writer_repo, tmp_path):
    """No repository-wide `git worktree prune`: only the team's own records go."""
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    mine = tmp_path / "my-worktree"
    git(project, "worktree", "add", "-b", "feature", str(mine), base)
    (mine / "uncommitted.txt").write_text("work in progress\n")
    away = tmp_path / "my-worktree-away"
    mine.rename(away)  # an unplugged drive, a folder being moved
    _end(store, authority.run_id)
    assert clean_finished_teams(store, project, root=integration.root)["worktrees"] == 1
    away.rename(mine)
    assert git(mine, "status", "--short") == "?? uncommitted.txt"
    assert len(worktrees(project)) == 2


def test_cleanup_runs_git_without_hooks_and_under_the_repository_lock(writer_repo, tmp_path):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    marker = tmp_path / "hook-ran"
    hook = project / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker.as_posix()}'\n", encoding="ascii", newline="\n")
    hook.chmod(0o755)
    _end(store, authority.run_id)
    # Another team step holds the repository: cleanup waits, then leaves everything for later.
    handle = open_repository_lock(project / ".git" / REPOSITORY_LOCK_NAME)
    try_repository_lock(handle)
    try:
        report = clean_finished_teams(store, project, root=integration.root, lock_seconds=0.5)
    finally:
        release_repository_lock(handle)
        handle.close()
    assert "held this repository" in report["skipped"] and Path(lease["path"]).exists() and branches(project)
    report = clean_finished_teams(store, project, root=integration.root)
    assert report["branches"] == 1 and not marker.exists(), "a hook ran during cleanup"


def test_cleanup_without_git_leaves_everything_for_the_next_start(writer_repo, monkeypatch, tmp_path):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    _end(store, authority.run_id)
    from tests.test_clean_machine import _without_git

    _without_git(monkeypatch, tmp_path)
    report = clean_finished_teams(store, project, root=integration.root)
    assert "isn't installed on this computer" in report["skipped"] and Path(lease["path"]).exists()
    monkeypatch.undo()
    assert clean_finished_teams(store, project, root=integration.root)["branches"] == 1


def test_team_branches_in_packed_refs_go_and_nothing_else(writer_repo):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    legacy = "codex/swarm-writer-0123456789abcdef"
    git(project, "branch", legacy, base)
    _legacy_writer(store, legacy)
    git(project, "branch", "keep-me", base)
    git(project, "pack-refs", "--all")
    assert legacy in (project / ".git" / "packed-refs").read_text()
    _end(store, authority.run_id)
    assert clean_finished_teams(store, project, root=integration.root)["branches"] == 2 and branches(project) == []
    assert git(project, "rev-parse", "keep-me") == base and git(project, "rev-parse", "main") == base
    assert legacy not in (project / ".git" / "packed-refs").read_text()
    git(project, "fsck", "--no-progress")


def _after_each_removal(monkeypatch, happen):
    """Something happens in the repository right after a team worktree goes, before its branch does."""
    real = cleanup.remove_worktree

    def remove(path, *, common_dir):
        result = real(path, common_dir=common_dir)
        happen()
        return result

    monkeypatch.setattr(cleanup, "remove_worktree", remove)


def test_a_branch_checked_out_while_its_worktree_goes_stays(writer_repo, monkeypatch):
    """update-ref -d deletes a checked-out branch; what is checked out is read again right before it."""
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    branch = json.loads(lease["manifest_json"])["branch"]
    _after_each_removal(monkeypatch, lambda: git(project, "checkout", branch))  # the person looks at it meanwhile
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert [(item["branch"], item["reason"]) for item in report["left"]] == [(branch, "in_use")], report
    assert git(project, "symbolic-ref", "HEAD") == f"refs/heads/{branch}" and git(project, "rev-parse", "HEAD") == base
    assert "No commits yet" not in git(project, "status", "--short", "--branch")
    monkeypatch.undo()
    git(project, "checkout", "main")
    assert clean_finished_teams(store, project, root=integration.root)["branches"] == 1 and branches(project) == []


def test_a_commit_landing_while_its_worktree_goes_keeps_the_branch(writer_repo, monkeypatch):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    branch = json.loads(lease["manifest_json"])["branch"]
    landed = {}

    def commit():
        tree = git(project, "rev-parse", f"{base}^{{tree}}")
        landed["commit"] = git(project, "commit-tree", tree, "-p", base, "-m", "Landed meanwhile")
        git(project, "update-ref", f"refs/heads/{branch}", landed["commit"])

    _after_each_removal(monkeypatch, commit)
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert git(project, "rev-parse", branch) == landed["commit"], "the commit that landed meanwhile was lost"
    assert [(item["branch"], item["reason"]) for item in report["left"]] == [(branch, "moved")] and not report["failed"]
    assert [item["branch"] for item in _events(store, authority.run_id, REMOVED_EVENT)[0]["left"]] == [branch]


def test_discard_removes_applied_candidates_worktrees_too(writer_repo):
    """Kept so Inspect candidate works, one checkout each, until Discard frees them."""
    from tests.test_swarm_integration import finish, writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (context, lease), = writers(writer_repo, names=("a",))
    (Path(lease["path"]) / "a.txt").write_text("the writer's change\n")
    finish(writer_repo, context, lease)
    candidate = integration.root / "candidate-0123456789abcdef"
    git(project, "worktree", "add", "--detach", str(candidate), base)
    with sqlite3.connect(store.path) as connection:
        repo_key = connection.execute("SELECT repo_key FROM writer_worktrees").fetchone()[0]
        connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,state,"
                           "manifest_json,process_protocol) VALUES('applied-candidate',?,1,?,?,?,'applied',?,1)",
                           (authority.run_id, repo_key, str(candidate), base,
                            json.dumps({"writers": [{"id": lease["id"]}]})))
    _end(store, authority.run_id, "completed")
    clean_finished_teams(store, project, root=integration.root)
    assert candidate.exists() and not Path(lease["path"]).exists()  # the applied writer went, the candidate stays
    report = clean_finished_teams(store, project, run_ids=(authority.run_id,), root=integration.root, discard=True)
    assert report["worktrees"] == 1 and not candidate.exists() and len(worktrees(project)) == 1
    assert _events(store, authority.run_id, DISCARDED_EVENT)[0]["candidates"] == ["applied-candidate"]


def test_the_person_can_remove_a_folder_left_with_a_moved_branch(writer_repo, tmp_path):
    """Remove: the folder goes like a team worktree (links unlinked, never followed); the branch stays."""
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    _ready(store, lease["id"], base)
    branch = json.loads(lease["manifest_json"])["branch"]
    git(lease["path"], "commit", "--allow-empty", "-m", "The person's own work on the team's branch")
    mine = git(project, "rev-parse", branch)
    library = own_library(tmp_path)
    link_folder(Path(lease["path"]) / "node_modules", library)
    _end(store, authority.run_id)
    report = clean_finished_teams(store, project, root=integration.root)
    assert [(item["branch"], item["reason"], item["worktree"]) for item in report["left"]] == [
        (branch, "moved", lease["path"])]
    removed = remove_left_worktree(store, project, authority.run_id, lease["id"], root=integration.root)
    assert removed == {"writer_id": lease["id"], "branch": branch, "worktree": lease["path"]}
    assert not os.path.lexists(lease["path"]) and git(project, "rev-parse", branch) == mine
    assert sorted(path.name for path in library.iterdir()) == ["index.js"], "a junction's target lost its files"
    assert len(worktrees(project)) == 1  # its own Git record went with it
    with store._connection() as connection:
        left, = cleanup._recorded(connection, authority.run_id)["left"].values()
    assert "worktree" not in left and left["folder_removed"] is True
    with pytest.raises(Conflict, match="no folder"):
        remove_left_worktree(store, project, authority.run_id, lease["id"], root=integration.root)
    # Only a folder under Lumi's own: a record naming another is refused, and it stays.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("the person's\n")
    with store._connection(write=True) as connection:
        store._event(connection, authority.run_id, REMOVED_EVENT, {
            "writers": ["elsewhere"], "candidates": [], "branches": [], "failed": [],
            "left": [{"run_id": authority.run_id, "writer_id": "elsewhere", "branch": "lumi/team-elsewhere",
                      "reason": "moved", "worktree": str(outside)}]})
    with pytest.raises(Conflict, match="isn't in Lumi's folder"):
        remove_left_worktree(store, project, authority.run_id, "elsewhere", root=integration.root)
    assert (outside / "keep.txt").exists()


def test_kept_folders_are_measured_in_the_background_without_following_links(tmp_path):
    from lumi.engine.swarming.cleanup import FolderSizes
    from lumi.worktree_removal import folder_size

    folder = tmp_path / "kept"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.bin").write_bytes(b"x" * 1000)
    (folder / "sub" / "b.bin").write_bytes(b"y" * 500)
    library = own_library(tmp_path)
    (library / "big.bin").write_bytes(b"z" * 100_000)
    link_folder(folder / "node_modules", library)
    release = threading.Event()

    def measure(path):
        release.wait(10)
        return folder_size(path)

    sizes = FolderSizes(measure)
    assert sizes.get(str(folder)) is None  # the panel never waits for a walk
    release.set()
    until(lambda: sizes.get(str(folder)) == 1500, timeout=10)


def test_discard_all_counts_only_this_conversations_ended_teams(writer_repo):
    from tests.test_swarm_integration import finish, writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (context, lease), = writers(writer_repo, names=("a",))
    (Path(lease["path"]) / "a.txt").write_text("the writer's change\n")
    finish(writer_repo, context, lease)
    assert kept_across(store, authority.scope)["teams"] == 0  # still running: nothing to discard yet
    _end(store, authority.run_id)
    across = kept_across(store, authority.scope)
    assert (across["teams"], across["run_ids"], across["items"], across["applied"]) == (1, [authority.run_id], 1, 0)
    assert kept_across(store, replace(authority.scope, session_id="another-conversation"))["teams"] == 0


# ── Through the Team panel's runtime (SwarmRuntime), with real writers ──────


def act(desktop, run_id, action, request_id, **payload):
    """One owner action at the revision just seen; the whole reply (its message too)."""
    service, capture, *_ = desktop
    revision = view(desktop, run_id)["run"]["revision"]
    return service.operate(capture, {"action": action, "request_id": request_id, "run_id": run_id,
                                     "expected_revision": revision, **payload})


def ended(desktop, run_id):
    service = desktop[0]
    until(lambda: view(desktop, run_id)["run"]["state"] in ("cancelled", "completed", "failed") and not service.busy,
          timeout=90)
    return view(desktop, run_id)


def test_stop_keeps_unapplied_work_and_discard_removes_it_through_links(desktop, tmp_path):
    """Stop says what it keeps; Discard removes it, never a junction's target or the person's worktree."""
    service, capture, project, _ = desktop
    # A declared check leaves a junction in the combined candidate, as npm
    # install does for a file: dependency outside the repository.
    library = own_library(tmp_path)
    make_link = f"import _winapi, os; _winapi.CreateJunction(r'{library}', os.path.abspath('node_modules_link'))"
    checks = [*request()["checks"], {"key": "install", "argv": [sys.executable, "-c", make_link], "timeout_seconds": 30}]
    run_id = service.operate(capture, request(checks=checks))["run"]["run"]["id"]
    submitted(desktop, run_id)
    writer = view(desktop, run_id)["writer_worktrees"][0]
    branch = json.loads(writer["manifest_json"])["branch"]
    operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer["id"]])
    candidate = settled(desktop, run_id)["integration_candidates"][0]
    assert candidate["state"] == "ready"
    operate(desktop, run_id, "run_check", "install", candidate_id=candidate["id"], check_key="install")
    assert settled(desktop, run_id)["integration_checks"][-1]["state"] == "passed"
    assert os.path.lexists(Path(candidate["path"]) / "node_modules_link")
    # Something in the writer's worktree links out too.
    link_folder(Path(writer["path"]) / "node_modules", library)
    mine = tmp_path / "my-worktree"
    git(project, "worktree", "add", "-b", "feature", str(mine))
    (mine / "work.txt").write_text("in progress\n")
    away = tmp_path / "my-worktree-away"
    mine.rename(away)

    stopped = act(desktop, run_id, "stop", "stop")
    assert "stays in your repository until you discard it" in stopped["message"], stopped["message"]
    assert branch in stopped["message"] and "1 combined candidate" in stopped["message"]
    current = ended(desktop, run_id)
    kept = current["kept_work"]
    assert kept["ended"] and [(item["kind"], item.get("branch")) for item in kept["items"]] == [
        ("writer", branch), ("candidate", None)], kept
    assert kept["items"][0]["changed"] is True
    assert branch in branches(project) and Path(writer["path"]).exists() and Path(candidate["path"]).exists()

    discarded = act(desktop, run_id, "discard_kept_work", "discard")
    assert discarded["message"] == "Discarded 2 worktrees and 1 branch.", discarded["message"]
    assert branches(project) == [] and not Path(writer["path"]).exists() and not Path(candidate["path"]).exists()
    assert sorted(path.name for path in library.iterdir()) == ["index.js"]
    assert (library / "index.js").read_text(encoding="utf-8") == "the person's own code\n"
    assert discarded["run"]["kept_work"]["items"] == []
    store = service._stores[service_module._path_key(capture.workspace)]
    recorded, = _events(store, run_id, DISCARDED_EVENT)
    assert recorded["writers"] == [writer["id"]] and recorded["candidates"] == [candidate["id"]]
    away.rename(mine)
    assert git(mine, "status", "--short") == "?? work.txt" and len(worktrees(project)) == 2


def test_discard_waits_for_the_team_to_end_and_keeps_a_moved_branch(desktop):
    service, capture, project, _ = desktop
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    submitted(desktop, run_id)
    with pytest.raises(Conflict, match="Stop the team"):
        act(desktop, run_id, "discard_kept_work", "too-early")
    writer = view(desktop, run_id)["writer_worktrees"][0]
    branch = json.loads(writer["manifest_json"])["branch"]
    with pytest.raises(Conflict, match="Stop the team"):
        act(desktop, run_id, "remove_left_worktree", "too-early-remove", writer_id=writer["id"])
    act(desktop, run_id, "stop", "stop")
    ended(desktop, run_id)
    # The panel says how much disk the kept folders take, measured in the background.
    until(lambda: not view(desktop, run_id)["kept_work"]["size_pending"], timeout=30)
    kept = view(desktop, run_id)["kept_work"]
    assert kept["size"] > 0 and kept["size"] == sum(item["size"] for item in kept["items"])
    # Someone commits on the team's branch after it stopped.
    git(writer["path"], "commit", "--allow-empty", "-m", "Salvaged by the person")
    salvaged = git(project, "rev-parse", branch)
    discarded = act(desktop, run_id, "discard_kept_work", "discard")
    assert f"Kept {branch}: it has commits the team didn't make." in discarded["message"], discarded["message"]
    assert git(project, "rev-parse", branch) == salvaged
    left = discarded["run"]["kept_work"]["left"]
    assert [(item["branch"], item["reason"], item["worktree"]) for item in left] == [(branch, "moved", writer["path"])]
    # Remove: that folder goes (never by hand, which could follow a junction); the branch stays.
    removed = act(desktop, run_id, "remove_left_worktree", "remove", writer_id=writer["id"])
    assert removed["message"] == (f"Removed {writer['path']}. The branch {branch} and its commits stay in "
                                  "your repository."), removed["message"]
    assert not os.path.lexists(writer["path"]) and git(project, "rev-parse", branch) == salvaged
    left, = removed["run"]["kept_work"]["left"]
    assert left["folder_removed"] is True and "worktree" not in left
    with pytest.raises(Conflict, match="no folder"):
        act(desktop, run_id, "remove_left_worktree", "remove-again", writer_id=writer["id"])


def test_an_applied_change_is_cleaned_up_and_stays_inspectable_until_discarded(desktop):
    """The applied writer's worktree and branch go; the applied candidate stays for Inspect candidate until Discard."""
    service, capture, project, _ = desktop
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    submitted(desktop, run_id)
    writer = view(desktop, run_id)["writer_worktrees"][0]
    branch = json.loads(writer["manifest_json"])["branch"]
    operate(desktop, run_id, "prepare_candidate", "prepare", writer_ids=[writer["id"]])
    candidate = settled(desktop, run_id)["integration_candidates"][0]
    operate(desktop, run_id, "run_check", "verify", candidate_id=candidate["id"], check_key="value-check")
    assert settled(desktop, run_id)["integration_candidates"][0]["state"] == "verified"
    operate(desktop, run_id, "apply_candidate", "apply", candidate_id=candidate["id"],
            expected_base=candidate["base_revision"], target_revision=candidate["result_revision"],
            evidence="Owner reviewed the exact candidate and its check")
    assert settled(desktop, run_id)["integration_candidates"][0]["state"] == "applied"
    attempt = view(desktop, run_id)["attempts"][0]
    operate(desktop, run_id, "accept_writer", "accept", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
            candidate_id=candidate["id"], evidence="Owner accepted the applied candidate")
    operate(desktop, run_id, "complete", "complete")
    ended(desktop, run_id)
    until(lambda: branch not in branches(project) and not Path(writer["path"]).exists(), timeout=60)
    assert Path(candidate["path"]).exists()
    kept = view(desktop, run_id)["kept_work"]
    assert kept["items"] == [] and kept["applied_candidates"] == 1
    assert [item["id"] for item in kept["applied"]] == [candidate["id"]]
    act(desktop, run_id, "inspect_candidate", "inspect", candidate_id=candidate["id"])
    until(lambda: any(item.get("state") == "ready" for item in view(desktop, run_id)["candidate_details"]), timeout=60)
    assert (project / "src" / "value.txt").read_text() == "verified change\n"
    # One full checkout per applied candidate would pile up: Discard frees it.
    discarded = act(desktop, run_id, "discard_kept_work", "discard")
    assert discarded["message"] == "Discarded 1 worktree and 0 branches.", discarded["message"]
    assert not Path(candidate["path"]).exists() and discarded["run"]["kept_work"]["applied"] == []
    assert (project / "src" / "value.txt").read_text() == "verified change\n"
    service._candidate_details.clear()
    act(desktop, run_id, "inspect_candidate", "inspect-again", candidate_id=candidate["id"])
    details, = view(desktop, run_id)["candidate_details"]
    assert details["state"] == "failed" and "has been removed" in details["error"]


def test_discard_all_kept_work_takes_every_ended_team_of_the_conversation(desktop):
    service, capture, project, _ = desktop
    kept_branches = []
    for number in range(2):
        run_id = service.operate(capture, request(request_id=f"start-{number}"))["run"]["run"]["id"]
        submitted(desktop, run_id)
        writer = view(desktop, run_id)["writer_worktrees"][0]
        kept_branches.append(json.loads(writer["manifest_json"])["branch"])
        act(desktop, run_id, "stop", f"stop-{number}")
        ended(desktop, run_id)
    assert sorted(branches(project)) == sorted(kept_branches)
    across = service.operate(capture, {"request_id": "view", "run_id": run_id})["kept_everywhere"]
    assert (across["teams"], across["items"]) == (2, 2), across
    until(lambda: not service.operate(capture, {"request_id": "view"})["kept_everywhere"]["size_pending"], timeout=30)
    assert service.operate(capture, {"request_id": "view"})["kept_everywhere"]["size"] > 0
    discarded = service.operate(capture, {"action": "discard_all_kept_work", "request_id": "discard-all"})
    assert discarded["message"] == "Discarded 2 worktrees and 2 branches of 2 teams.", discarded["message"]
    assert branches(project) == [] and len(worktrees(project)) == 1
    assert discarded["kept_everywhere"]["teams"] == 0
    again = service.operate(capture, {"action": "discard_all_kept_work", "request_id": "discard-all-again"})
    assert again["message"] == "No ended team in this conversation keeps anything to discard."


def test_teams_that_ended_earlier_are_cleaned_at_the_next_start(desktop):
    """The startup sweep: legacy branches nothing needs go, unapplied work stays."""
    service, capture, project, _ = desktop
    run_id = service.operate(capture, request())["run"]["run"]["id"]
    submitted(desktop, run_id)
    writer = view(desktop, run_id)["writer_worktrees"][0]
    branch = json.loads(writer["manifest_json"])["branch"]
    act(desktop, run_id, "stop", "stop")
    ended(desktop, run_id)
    base = writer["base_revision"]
    legacy = "codex/swarm-writer-0123456789abcdef"
    git(project, "branch", legacy, base)
    store = service._stores[service_module._path_key(capture.workspace)]
    _legacy_writer(store, legacy)
    reopened = service_module.SwarmRuntime(service.settings, state_root=service._state_root)
    try:
        reopened.watch_project(capture.workspace, replace(capture.scope, session_id="discovery"))
        until(lambda: legacy not in branches(project), timeout=60)
        assert branch in branches(project) and Path(writer["path"]).exists()
    finally:
        reopened.close()
