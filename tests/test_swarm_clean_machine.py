"""Teams on a new computer: no Git, a conversation's own team, cleanup after a team ends."""
# ruff: noqa: F811 -- the imported `setup` fixture is intentionally used by name.

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from lumi.engine.swarming import git_boundary
from lumi.engine.swarming.cleanup import clean_finished_teams
from lumi.engine.swarming.git_boundary import git_error_line, trusted_git_executable
from lumi.engine.swarming.models import Conflict
from lumi.gui import swarming
from tests.test_gui_swarming import enable, setup, start_request, wait_stopped  # noqa: F401 (fixture)


# ── Without Git ────────────────────────────────────────────────────────────


def test_writer_teams_without_git_say_so(monkeypatch, tmp_path):
    monkeypatch.setattr(git_boundary.os, "get_exec_path", lambda *a: [])
    with pytest.raises(Conflict, match="Writer teams need Git") as refused:
        trusted_git_executable(tmp_path / "project", tmp_path / "runtime")
    assert "Read-only teams work without it" in str(refused.value)


def test_a_git_inside_the_project_is_still_refused_as_before(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shadow = project / ("git.exe" if os.name == "nt" else "git")
    shadow.write_bytes(b"not git")
    shadow.chmod(0o755)
    monkeypatch.setattr(git_boundary.os, "get_exec_path", lambda *a: [str(project)])
    with pytest.raises(Conflict, match="outside the project"):
        trusted_git_executable(project, tmp_path / "runtime")


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


# ── After a team ends, its writers' worktrees and branches go ──────────────


def _git(path, *args):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=path, env=environment,
                            capture_output=True, text=True, timeout=20)
    return result


@pytest.fixture
def writer_repo(tmp_path):
    """The real-Git writer fixture of test_swarm_integration.py, under another name."""
    if os.name != "nt":
        pytest.skip("Supervised integration requires Windows named-job process containment")
    from lumi.engine.swarming import Scope, SwarmStore, SwarmSupervisor
    from lumi.engine.swarming.integration import SwarmIntegration
    from lumi.engine.swarming.policy import PolicyProfile
    from tests.test_swarm_integration import git

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


def test_ended_teams_leave_no_worktrees_or_branches(writer_repo):
    from tests.test_swarm_integration import writers

    store, _supervisor, authority, integration, project, base = writer_repo
    (_context, lease), = writers(writer_repo, names=("a",))
    manifest = lease.get("manifest") or json.loads(lease["manifest_json"])
    branch = manifest["branch"]
    assert branch.startswith("lumi/team-")
    assert Path(lease["path"]).exists()
    # A legacy writer branch from before the rebrand, recorded by the same run.
    legacy = "codex/swarm-writer-0123456789abcdef"
    assert _git(project, "branch", legacy, base).returncode == 0
    with sqlite3.connect(store.path) as connection:
        manifest = json.loads(connection.execute("SELECT manifest_json FROM writer_worktrees").fetchone()[0])
        connection.execute("INSERT INTO writer_worktrees(id,run_id,attempt_id,epoch,repo_key,path,base_revision,state,"
                           "manifest_json,process_protocol) SELECT 'legacy-writer',run_id,'legacy-attempt',epoch,repo_key,"
                           "path || '-legacy',base_revision,'failed',?,1 FROM writer_worktrees",
                           (json.dumps({**manifest, "branch": legacy}),))
    # Still running: nothing is touched.
    assert clean_finished_teams(store, project, root=integration.root) == {"worktrees": 0, "branches": 0}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE runs SET state='cancelled' WHERE id=?", (authority.run_id,))
    removed = clean_finished_teams(store, project, root=integration.root)
    assert removed == {"worktrees": 1, "branches": 2}
    assert not Path(lease["path"]).exists()
    listed = _git(project, "branch", "--list", "lumi/team-*", "codex/swarm-writer-*").stdout
    assert listed.strip() == ""
    assert _git(project, "worktree", "list").stdout.count("\n") == 1  # only the user's checkout
    # Idempotent: nothing left to do.
    assert clean_finished_teams(store, project, root=integration.root) == {"worktrees": 0, "branches": 0}


def test_cleanup_without_git_does_nothing(monkeypatch, tmp_path):
    from lumi.engine.swarming import cleanup

    monkeypatch.setattr(cleanup, "leftovers", lambda store, run_ids=None: ([str(tmp_path / "x")], ["lumi/team-1"]))
    monkeypatch.setattr(git_boundary.os, "get_exec_path", lambda *a: [])
    assert clean_finished_teams(SimpleNamespace(), tmp_path, root=tmp_path / "runtime") == {"worktrees": 0, "branches": 0}
