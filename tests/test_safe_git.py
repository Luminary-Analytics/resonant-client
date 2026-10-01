"""Lumi's own Git never runs the programs a repository's settings name (lumi/safe_git.py).

Real Git against real repositories. Each program a repository names is a
harmless script that only appends its name to a marker file outside the
project; a test fails when a name shows up there.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import safe_git

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(not GIT, reason="needs Git")
WINDOWS = sys.platform == "win32"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd: Path, *args: str) -> str:
    """The test's own Git, to build repositories (never Lumi's)."""
    done = subprocess.run([GIT, "-c", "protocol.file.allow=always", *args], cwd=cwd, env={**os.environ, **IDENTITY},
                          check=True, capture_output=True, text=True)
    return done.stdout


def marker_script(folder: Path, name: str, marker: Path, action: str = "") -> str:
    """A program that appends ``name`` to ``marker`` and then does ``action``
    ("copy" copies input to output, "show" prints its first argument's file)."""
    folder.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        path = folder / f"{name}.bat"
        body = ["@echo off", f'echo {name}>>"{marker}"']
        body += {"copy": ["more"], "show": ["type %1"]}.get(action, [])
        path.write_text("\r\n".join(body) + "\r\n", encoding="ascii")
    else:
        path = folder / f"{name}.sh"
        body = ["#!/bin/sh", f'echo {name} >> "{marker}"']
        body += {"copy": ["cat"], "show": ['cat "$1"']}.get(action, [])
        path.write_text("\n".join(body) + "\n", encoding="ascii")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path.as_posix()


def hook(project: Path, name: str, marker: Path) -> None:
    """A repository hook (Git runs hooks with its own sh on Windows too)."""
    path = project / ".git" / "hooks" / name
    path.write_text(f'#!/bin/sh\necho {name} >> "{marker.as_posix()}"\n', encoding="ascii", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def ran(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").split() if marker.exists() else []


def make_repository(root: Path, name: str = "project") -> Path:
    project = root / name
    project.mkdir()
    (project / "notes.txt").write_text("hello\n", encoding="utf-8")
    (project / "data.bin").write_text("one\n", encoding="utf-8")
    (project / ".gitattributes").write_text("*.bin diff=review filter=review\n", encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "first")
    return project


def touch_changes(project: Path) -> None:
    """Unstaged changes, one of them "racy", so status and diff have work to do."""
    (project / "notes.txt").write_text("hello again\n", encoding="utf-8")
    (project / "data.bin").write_text("two\n", encoding="utf-8")
    os.utime(project / "data.bin", (1, 1))


@pytest.fixture(autouse=True)
def _fresh_checks():
    safe_git._checks.clear()
    yield
    safe_git._checks.clear()


@pytest.fixture
def armed(tmp_path):
    """A repository whose own settings name programs, as an archive or a copied folder can carry them."""
    project = make_repository(tmp_path)
    marker = tmp_path / "ran.txt"
    tools = project / ".git" / "tools"
    settings = {
        "core.fsmonitor": marker_script(tools, "fsmonitor", marker),
        "diff.external": marker_script(tools, "external-diff", marker),
        "diff.review.textconv": marker_script(tools, "textconv", marker, "show"),
        "filter.review.clean": marker_script(tools, "clean", marker, "copy"),
        "log.showSignature": "true",
        "gpg.program": marker_script(tools, "gpg", marker),
    }
    for key, value in settings.items():
        git(project, "config", key, value)
    hook(project, "post-commit", marker)
    hook(project, "pre-commit", marker)
    touch_changes(project)
    return SimpleNamespace(project=project, marker=marker)


def _page(project: Path, command: str, msg: dict | None = None) -> list[dict]:
    from lumi.gui import ws_commands

    sent: list[dict] = []

    async def send_json(payload):
        sent.append(payload)

    state = SimpleNamespace(project=SimpleNamespace(project_path=str(project), current_session=None))
    context = ws_commands.CommandContext(ws=SimpleNamespace(send_json=send_json), state=state, msg=msg or {},
                                         runs=None)
    asyncio.run(ws_commands.HANDLERS[command](context))
    return sent


# ── An untrusted project whose settings name programs: no Git at all ────────


def test_the_page_status_on_project_open_runs_nothing_and_says_why(armed):
    sent = _page(armed.project, "git_status")
    assert ran(armed.marker) == []
    data = sent[-1]["data"]
    assert data["is_repo"] and data["changes"] == []
    assert "trust it" in data["refused"] and "filter.review.clean" in data["refused"]


def test_the_git_popover_actions_run_nothing(armed):
    from lumi.gui import ws_commands

    for action, msg in (("diff", {}), ("diff_staged", {}), ("log", {}), ("add", {}), ("stash", {}),
                        ("commit", {"message": "x"})):
        result = ws_commands._git_quick(action, msg, str(armed.project))
        assert result.get("refused") and "trust" in result["output"], action
    assert ran(armed.marker) == []


def test_indexing_walks_the_folder_instead(armed):
    from lumi.engine.rag import CodebaseIndex

    index = CodebaseIndex(armed.project)
    stats = index.index(force=True)
    assert ran(armed.marker) == []
    assert stats["listing"] == "walk" and "trust" in stats["git_refused"]
    assert index.file_count >= 1


def test_at_diff_attaches_the_notice(armed):
    from lumi.engine.context_broker import ContextBroker

    item = ContextBroker(armed.project)._diff("working")
    assert ran(armed.marker) == []
    assert item is not None and "trust" in item.content and item.provenance == "git-refused"


def test_the_agents_git_tools_report_the_notice(armed):
    from lumi.engine import git_tools

    for tool, args in ((git_tools.exec_git_status, {}), (git_tools.exec_git_diff, {}),
                       (git_tools.exec_git_log, {}), (git_tools.exec_git_commit, {"message": "x", "paths": ["notes.txt"]}),
                       (git_tools.exec_git_branch_create, {"branch": "feature"})):
        result = tool({"cwd": str(armed.project), **args}, 0.0, trusted=False)
        assert result.is_error and "trust" in result.output and result.metadata["git_refused"], tool.__name__
    assert ran(armed.marker) == []


def test_checkpoints_hand_offs_worktrees_and_comparisons_run_nothing(armed):
    from lumi import handoff, model_evals
    from lumi.engine.worktrees import WorktreeError, WorktreeManager
    from lumi.gui.autonomous_factory import make_git_get_commit_sha, make_git_validate_sha
    from lumi.orchestration.checkpoints import CheckpointError, IterationCheckpointStore

    with pytest.raises(CheckpointError, match="trust"):
        IterationCheckpointStore(armed.project)
    assert handoff._git(str(armed.project), "rev-parse", "HEAD") == ""
    manager = WorktreeManager(armed.project, root=armed.project.parent / "worktrees")
    assert not manager.available and "trust" in manager.refused
    with pytest.raises(WorktreeError, match="trust"):
        manager.create("agent")
    with pytest.raises(model_evals.EvalError, match="trust"):
        model_evals.clean({"name": "x", "project": str(armed.project), "models": ["ollama:x"],
                           "tasks": [{"prompt": "p", "check": "exit 0"}]})
    assert make_git_get_commit_sha(str(armed.project))() is None
    assert make_git_validate_sha(str(armed.project))("0" * 40) is False
    assert ran(armed.marker) == []


def test_the_editor_bridge_and_pull_request_tools_refuse(armed):
    from lumi.engine import github_tools
    from lumi.gui import editor_bridge

    with pytest.raises(editor_bridge.BridgeError, match="trust"):
        editor_bridge._git(armed.project, "cat-file", "--filters", "HEAD:data.bin")
    with pytest.raises(github_tools.GitHubError, match="trust"):
        github_tools.repo_for(str(armed.project))
    assert ran(armed.marker) == []


def test_session_checkpoints_keep_an_archive_instead(armed, tmp_path):
    from lumi.engine.checkpoint_timeline import SessionCheckpointStore

    os.utime(armed.project / "data.bin")  # a zip archive can't hold the 1970 time the racy file has
    timeline = SessionCheckpointStore(armed.project, session_id="session", root=tmp_path / "timeline")
    checkpoint = timeline.create(conversation_history=[], reason="test")
    assert checkpoint.workspace_ref == "" and checkpoint.workspace_archive
    assert ran(armed.marker) == []


# ── Trusted: the person's own settings apply, Lumi's fixed options still hold ─


def test_a_trusted_project_uses_git_without_fsmonitor_diff_drivers_or_signatures(armed):
    from lumi.gui.workspace_trust import WorkspaceTrust

    WorkspaceTrust().trust(str(armed.project))
    sent = _page(armed.project, "git_status")
    data = sent[-1]["data"]
    assert "refused" not in data
    assert sorted(change["file"] for change in data["changes"]) == ["data.bin", "notes.txt"]
    from lumi.gui import ws_commands
    ws_commands._git_quick("diff", {}, str(armed.project))
    ws_commands._git_quick("log", {}, str(armed.project))
    # A clean filter is the repository's own way to store files; a trusted
    # project's status may run it, as the person's own `git status` would.
    assert set(ran(armed.marker)) <= {"clean"}


def test_hooks_run_only_for_a_commit_someone_asked_for_in_a_trusted_project(tmp_path, monkeypatch):
    from lumi.engine import git_tools
    from lumi.gui.workspace_trust import WorkspaceTrust

    for name, value in IDENTITY.items():  # the agent's commits use the person's identity; there is none here
        monkeypatch.setenv(name, value)

    project = make_repository(tmp_path)
    marker = tmp_path / "ran.txt"
    hook(project, "post-commit", marker)
    hook(project, "post-checkout", marker)
    (project / "notes.txt").write_text("two\n", encoding="utf-8")
    # Untrusted, but its settings name no programs: Git works, hooks don't run.
    result = git_tools.exec_git_commit({"cwd": str(project), "message": "one", "paths": ["notes.txt"]}, 0.0,
                                       trusted=False)
    assert not result.is_error, result.output
    git_tools.exec_git_branch_create({"cwd": str(project), "branch": "other"}, 0.0, trusted=False)
    assert ran(marker) == []
    WorkspaceTrust().trust(str(project))
    (project / "notes.txt").write_text("three\n", encoding="utf-8")
    result = git_tools.exec_git_commit({"cwd": str(project), "message": "two", "paths": ["notes.txt"]}, 0.0,
                                       trusted=True)
    assert not result.is_error, result.output
    assert ran(marker) == ["post-commit"]
    # Branching is Lumi's own step, not a commit: no hooks even when trusted.
    git_tools.exec_git_branch_create({"cwd": str(project), "branch": "third"}, 0.0, trusted=True)
    assert ran(marker) == ["post-commit"]


def test_lumis_own_commits_never_run_hooks(tmp_path):
    from lumi.engine.worktrees import WorktreeManager
    from lumi.gui.workspace_trust import WorkspaceTrust
    from lumi.orchestration.checkpoints import IterationCheckpointStore

    project = make_repository(tmp_path)
    marker = tmp_path / "ran.txt"
    for name in ("pre-commit", "post-commit", "post-checkout", "post-merge", "reference-transaction"):
        hook(project, name, marker)
    WorkspaceTrust().trust(str(project))
    (project / "notes.txt").write_text("two\n", encoding="utf-8")
    IterationCheckpointStore(project).create(intent_id="t", iteration=1)
    manager = WorktreeManager(project, root=tmp_path / "worktrees")
    lease = manager.create("agent")
    (Path(lease.path) / "new.txt").write_text("new\n", encoding="utf-8")
    manager.finalize(lease)
    assert lease.status == "ready"
    manager.remove(lease)
    assert ran(marker) == []


# ── What counts as naming a program ─────────────────────────────────────────


@pytest.mark.parametrize("key", [
    "filter.x.clean", "filter.x.smudge", "filter.x.process", "diff.x.textconv", "diff.x.command", "diff.external",
    "merge.x.driver", "core.sshCommand", "core.askPass", "core.editor", "core.pager", "sequence.editor",
    "gpg.program", "gpg.ssh.program", "credential.helper", "credential.https://example.com.helper",
    "remote.origin.uploadpack", "remote.origin.receivepack", "alias.status",
])
def test_each_program_setting_is_found(tmp_path, key):
    project = make_repository(tmp_path)
    git(project, "config", key, "some-program --flag")
    assert safe_git.repository_programs(project) == [key.lower()]
    assert "trust" in safe_git.refusal(project)


@pytest.mark.parametrize("key,value", [
    ("core.hooksPath", ".husky/_"),         # replaced by Lumi's empty folder for every command
    ("core.fsmonitor", "true"),             # replaced by false for every command
    ("core.fsmonitor", "some-program"),
    ("filter.lfs.required", "true"),
    ("alias.st", "!some-program"),          # Lumi never runs an alias
    ("user.name", "someone"),
    ("credential.helper", ""),              # empty: switches helpers off
])
def test_settings_that_run_nothing_lumi_does_are_fine(tmp_path, key, value):
    project = make_repository(tmp_path)
    git(project, "config", key, value)
    assert safe_git.repository_programs(project) == []
    assert safe_git.refusal(project) == ""


def test_the_persons_own_settings_dont_count(tmp_path, monkeypatch):
    """Global settings are the person's (a git-lfs filter, a credential manager)."""
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[filter \"lfs\"]\n\tclean = git-lfs clean -- %f\n[credential]\n\thelper = manager\n",
                             encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    project = make_repository(tmp_path)
    assert safe_git.repository_programs(project) == []


def test_included_files_count_even_under_a_condition_that_does_not_hold(tmp_path):
    project = make_repository(tmp_path)
    (tmp_path / "shared.cfg").write_text("[filter \"x\"]\n\tclean = some-program\n", encoding="utf-8")
    (project / ".git" / "later.cfg").write_text("[diff \"y\"]\n\ttextconv = other-program\n", encoding="utf-8")
    git(project, "config", "include.path", "../../shared.cfg")
    git(project, "config", "includeIf.onbranch:lumi/*.path", "later.cfg")
    assert safe_git.repository_programs(project) == ["filter.x.clean", "diff.y.textconv"]


def test_a_submodules_own_settings_count(tmp_path):
    library = make_repository(tmp_path, "library")
    project = make_repository(tmp_path)
    git(project, "submodule", "add", "-q", str(library), "lib")
    git(project, "commit", "-q", "-m", "submodule")
    assert safe_git.repository_programs(project) == []
    marker = tmp_path / "ran.txt"
    git(project / "lib", "config", "filter.review.clean", marker_script(tmp_path / "tools", "clean", marker, "copy"))
    touch_changes(project / "lib")
    assert safe_git.repository_programs(project) == ["filter.review.clean"]
    sent = _page(project, "git_status")
    assert "refused" in sent[-1]["data"]
    assert ran(marker) == []


def test_a_project_inside_a_repository_is_checked_there(tmp_path):
    project = make_repository(tmp_path)
    (project / "app").mkdir()
    git(project, "config", "filter.x.clean", "some-program")
    assert safe_git.repository_programs(project / "app") == ["filter.x.clean"]


def test_not_a_repository(tmp_path):
    assert safe_git.repository_programs(tmp_path) == []
    assert _page(tmp_path, "git_status")[-1]["data"] == {"is_repo": False}


# ── Fixed options and status parsing ────────────────────────────────────────


def test_every_call_gets_the_fixed_options(tmp_path):
    argv = safe_git.argv("status", "--porcelain=v1", project=tmp_path)
    assert Path(argv[0]).is_absolute()
    joined = " ".join(argv)
    for option in ("core.fsmonitor=false", "safe.bareRepository=explicit", "log.showSignature=false",
                   "protocol.ext.allow=never", "core.hooksPath="):
        assert option in joined
    assert argv[argv.index("status") - 1] == "--no-optional-locks"
    diff = safe_git.argv("-c", "core.quotepath=off", "diff", "HEAD")
    assert diff[diff.index("diff"):] == ["diff", "--no-ext-diff", "--no-textconv", "HEAD"]
    assert diff[diff.index("diff") - 2:diff.index("diff")] == ["-c", "core.quotepath=off"]
    assert "core.hooksPath=" not in " ".join(safe_git.argv("commit", hooks=True))
    hooks = Path(safe_git.empty_hooks_folder())
    assert hooks.is_dir() and not any(hooks.iterdir())


def test_status_names_are_exact(tmp_path):
    from lumi.engine import git_tools
    from lumi.gui import ws_commands

    project = tmp_path / "project"
    project.mkdir()
    for name in ("notes.txt", "staged.txt", "zeta.txt", "old name.txt"):
        (project / name).write_text("one\n", encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "first")
    (project / "notes.txt").write_text("two\n", encoding="utf-8")
    (project / "zeta.txt").write_text("two\n", encoding="utf-8")
    (project / "staged.txt").write_text("two\n", encoding="utf-8")
    git(project, "add", "staged.txt")
    git(project, "mv", "old name.txt", "new name.txt")
    (project / "with space.txt").write_text("new\n", encoding="utf-8")
    (project / " leading.txt").write_text("new\n", encoding="utf-8") if not WINDOWS else None
    (project / "café 日本.txt").write_text("new\n", encoding="utf-8")
    data = ws_commands._git_status(str(project))
    by_name = {change["file"]: change for change in data["changes"]}
    expected = {"notes.txt", "staged.txt", "zeta.txt", "new name.txt", "with space.txt", "café 日本.txt"}
    if not WINDOWS:
        expected.add(" leading.txt")
    assert set(by_name) == expected
    assert by_name["new name.txt"]["status"] == "R" and by_name["new name.txt"]["from"] == "old name.txt"
    assert by_name["notes.txt"]["status"] == "M" and by_name["with space.txt"]["status"] == "??"
    assert data["change_count"] == len(expected)
    status = git_tools.git_status(project, trusted=False)
    assert "café 日本.txt" in status["untracked"] and "with space.txt" in status["untracked"]
    assert {"path": "new name.txt", "status": "R", "from": "old name.txt"} in status["staged"]


def test_status_entries_parse_renames_and_headers():
    header, entries = safe_git.status_entries("## main...origin/main [ahead 1]\0R  new\0old\0 M a b\0?? c\0")
    assert header == "main...origin/main [ahead 1]"
    assert entries == [{"x": "R", "y": " ", "path": "new", "from": "old"},
                       {"x": " ", "y": "M", "path": "a b"}, {"x": "?", "y": "?", "path": "c"}]


def test_lumi_run_trust_project_covers_its_own_git(armed, monkeypatch):
    """``lumi run --trust-project`` trusts the folder for this process, Lumi's own Git included."""
    monkeypatch.setattr(safe_git, "_trusted_here", set())
    assert "trust" in safe_git.refusal(armed.project)
    safe_git.trust_for_this_process(armed.project.parent)
    safe_git._checks.clear()
    assert safe_git.refusal(armed.project) == ""
    assert safe_git.run(armed.project, "rev-parse", "--git-dir").returncode == 0
