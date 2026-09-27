"""A new Windows computer: no Git, Python or Node.js, and a user name like "Jöhn Smith".

Found by running the packaged app on such a machine: a missing Git ended every
session ("[WinError 2] The system cannot find the file specified"), cmd.exe's
output lost everything after one "ü", searches showed case-folded absolute
paths and Lumi's own index, and the prompt told models to run Python that
wasn't there. These tests pin what should happen instead.
"""

from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lumi import git_support, toolchain
from lumi.engine.checkpoint_timeline import SessionCheckpointStore
from lumi.engine.tools import execute_tool
from lumi.engine.worktrees import WorktreeError, WorktreeManager
from lumi.orchestration.checkpoints import CheckpointError, IterationCheckpointStore
from lumi.processes import decode_output
from lumi.startup_log import TaggedLog, process_role


def _without_git(monkeypatch):
    """No Git on this computer: PATH lookups miss it and starting it fails as Windows does."""
    real_which = shutil.which
    real_run = subprocess.run

    def which(name, *args, **kwargs):
        return None if os.path.splitext(os.path.basename(str(name)))[0].lower() == "git" else real_which(name, *args, **kwargs)

    def run(args, *rest, **kwargs):
        program = args[0] if isinstance(args, (list, tuple)) and args else str(args).split(" ", 1)[0]
        if os.path.splitext(os.path.basename(str(program)))[0].lower() == "git":
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    toolchain.clear_cache()


# ── Git is optional ────────────────────────────────────────────────────────


def test_the_page_learns_git_is_missing_and_what_needs_it(monkeypatch):
    _without_git(monkeypatch)
    status = git_support.status()
    assert status["available"] is False
    assert status["unavailable"] == ["writer_teams", "worktrees", "mission_checkpoints", "model_comparisons"]
    assert status["download_url"].startswith("https://git-scm.com/")
    assert "Writer teams need" in git_support.missing_message("Writer teams need")


def test_worktree_manager_without_git_is_unavailable_not_an_error(monkeypatch, tmp_path):
    _without_git(monkeypatch)
    manager = WorktreeManager(tmp_path / "project", root=tmp_path / "worktrees")
    assert manager.available is False
    with pytest.raises(WorktreeError, match="Git"):
        manager.create("agent")


def test_checkpoints_without_git_say_so_and_turn_checkpoints_use_an_archive(monkeypatch, tmp_path):
    _without_git(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("kept\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="isn't installed"):
        IterationCheckpointStore(project)
    checkpoint = SessionCheckpointStore(project, session_id="s", root=tmp_path / "cp").create(
        conversation_history=[], reason="turn")
    assert checkpoint.workspace_archive and not checkpoint.workspace_ref


def test_a_session_starts_and_the_init_data_reports_git_missing(monkeypatch, tmp_path):
    project = tmp_path / "Alpha Project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(project)
    import lumi.gui.app as app_module

    app_module = importlib.reload(app_module)
    from lumi.gui.runtime import BackendSpec

    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})
    state = app_module.AppState()
    backend = SimpleNamespace(name="ollama", model="stub", handles_tools=True)
    monkeypatch.setattr(BackendSpec, "create_backend", lambda self, settings: backend)
    monkeypatch.setattr(app_module.AppState, "build_backend_spec",
                        lambda self, backend_type, model=None, project_path=None: BackendSpec(backend_type, model or ""))
    _without_git(monkeypatch)
    state.create_backend("ollama", "stub")  # raised FileNotFoundError before
    assert state.session is not None and state.runtime_error == ""
    assert state.session.worktree_manager.available is False
    assert state.get_init_data()["git"]["available"] is False


# ── Command output ─────────────────────────────────────────────────────────


def test_output_decodes_utf8_and_else_the_oem_code_page():
    assert decode_output("Jürgen".encode("utf-8")) == "Jürgen"
    assert decode_output(b"J\x81rgen M\x81ller", oem_code_page="cp437") == "Jürgen Müller"
    assert decode_output(b"J\x94hn Smith\r\n", oem_code_page="cp850") == "Jöhn Smith\n"
    assert decode_output(None) == "" and decode_output("text") == "text"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe writes the OEM code page")
def test_the_shell_tool_returns_cmd_output_with_umlauts(monkeypatch, tmp_path):
    monkeypatch.setenv("LUMI_TEST_NAME", "C:\\Users\\Jöhn Smith")
    result = execute_tool("bash", {"command": "echo Jürgen Müller & echo %LUMI_TEST_NAME%", "cwd": str(tmp_path)},
                          project_path=str(tmp_path))
    assert not result.is_error, result.output
    assert "Jürgen Müller" in result.output
    assert "C:\\Users\\Jöhn Smith" in result.output


# ── Searches ───────────────────────────────────────────────────────────────


def _project_with_lumi_state(tmp_path) -> Path:
    project = tmp_path / "Alpha Project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("needle in the readme\n", encoding="utf-8")
    (project / "src" / "Greet.py").write_text("# needle\n", encoding="utf-8")
    (project / ".lumi").mkdir()
    (project / ".lumi" / "index.json").write_text('{"needle": 1}', encoding="utf-8")
    return project


def test_grep_shows_project_relative_paths_as_spelled_and_skips_lumi_state(tmp_path):
    project = _project_with_lumi_state(tmp_path)
    # The sandbox hands tools a case-folded absolute root.
    result = execute_tool("grep", {"pattern": "needle", "path": os.path.normcase(str(project))},
                          project_path=str(project))
    lines = sorted(result.output.splitlines())
    assert lines == ["README.md:1:needle in the readme", os.path.join("src", "Greet.py") + ":1:# needle"], result.output


def test_glob_shows_project_relative_paths_and_skips_lumi_state(tmp_path):
    project = _project_with_lumi_state(tmp_path)
    result = execute_tool("glob", {"pattern": "**/*", "path": os.path.normcase(str(project))},
                          project_path=str(project))
    shown = set(result.output.splitlines())
    assert shown == {"README.md", "src", os.path.join("src", "Greet.py")}, result.output


def test_a_project_under_lumis_own_folder_is_still_searched(tmp_path):
    project = tmp_path / ".lumi" / "workspace"  # the fallback workspace
    project.mkdir(parents=True)
    (project / "a.txt").write_text("needle\n", encoding="utf-8")
    result = execute_tool("grep", {"pattern": "needle", "path": str(project)}, project_path=str(project))
    assert result.output.splitlines() == ["a.txt:1:needle"]


# ── The prompt's environment line ──────────────────────────────────────────


def _programs(monkeypatch, found: dict[str, str]):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: found.get(name))
    toolchain.clear_cache()


def test_prompt_hints_follow_the_programs_this_computer_has(monkeypatch):
    _programs(monkeypatch, {})
    hints = toolchain.prompt_hints()
    assert "Python isn't installed here" in hints and "Use 'python'" not in hints
    assert "Not installed here: Git, Node.js" in hints
    _programs(monkeypatch, {"python": "/bin/python", "python3": "/bin/python3", "git": "/bin/git", "node": "/bin/node"})
    hints = toolchain.prompt_hints()
    assert "isn't installed" not in hints and "Not installed" not in hints


@pytest.mark.skipif(sys.platform != "win32", reason="Windows App execution aliases")
def test_the_microsoft_store_python_alias_is_not_python(monkeypatch):
    alias = os.path.join(os.environ.get("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local"), "Microsoft", "WindowsApps",
                         "python.exe")
    _programs(monkeypatch, {"python": alias})
    assert "Microsoft Store placeholder" in toolchain.prompt_hints()
    _programs(monkeypatch, {"py": "C:\\Windows\\py.exe"})
    assert "'py' launcher" in toolchain.prompt_hints()


def test_the_system_prompt_uses_the_hints(monkeypatch):
    from lumi.engine.session import get_system_instruction_layers

    _programs(monkeypatch, {})
    runtime = get_system_instruction_layers(working_directory="C:/p")[0]["content"]
    assert "Python isn't installed here" in runtime


# ── The packaged app's own folder is never a project ───────────────────────


def test_a_portable_copys_folder_and_the_folders_above_it_are_not_projects(monkeypatch, tmp_path):
    from lumi.gui import sessions

    app = tmp_path / "Downloads" / "Lumi Portable"
    (app / "_internal").mkdir(parents=True)
    sibling = tmp_path / "Downloads" / "My Project"
    sibling.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "lumi.exe"))
    for folder in (app, app / "_internal", tmp_path / "Downloads", tmp_path, Path(tmp_path.anchor)):
        assert sessions._is_unsafe_cwd(str(folder)), folder
    assert not sessions._is_unsafe_cwd(str(sibling))
    monkeypatch.setattr(sys, "frozen", False)
    # From source, sys.executable is a Python that can live in the user's project.
    assert not sessions._is_unsafe_cwd(str(tmp_path / "Downloads"))


# ── Command-line text ──────────────────────────────────────────────────────


def test_help_lists_the_commands(monkeypatch):
    import argparse

    # On Windows, importing lumi.tui wraps sys.stdout/sys.stderr; import it
    # over throwaway streams so pytest's capture files survive (as test_tui.py does).
    streams = sys.stdout, sys.stderr
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        tui_main = importlib.import_module("lumi.tui").main
    finally:
        sys.stdout, sys.stderr = streams
    printed = []
    monkeypatch.setattr(argparse.ArgumentParser, "_print_message",
                        lambda self, message, file=None: printed.append(message))
    with pytest.raises(SystemExit):
        tui_main(["--help"])
    text = "".join(printed)
    assert "Commands:" in text and "gui [--browser]" in text and "updates [verify <file>]" in text
    assert "Ollama-only" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="WinSparkle is Windows-only")
def test_a_missing_updater_says_reinstall(monkeypatch, capsys):
    from lumi import update_channels, updater
    from lumi.gui.ws_commands import _update_check_message

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "_find_dll", lambda: None)
    assert updater.component_missing()
    message = _update_check_message({"mode": "automatic", "component_missing": True}, False)
    assert "Reinstall Lumi" in message and "source" not in message
    assert update_channels.main([]) == 0
    assert "reinstall" in json.loads(capsys.readouterr().out)["updater"]


# ── The shared startup log ─────────────────────────────────────────────────


def test_every_line_in_the_startup_log_says_which_process_wrote_it():
    stream = io.StringIO()
    log = TaggedLog(stream, "gui", pid=42)
    log.write("  Lumi GUI running at http://127.0.0.1:1\n")
    log.write("Traceback (most recent call last):\n  File ")
    log.write('"x.py"\nerror\n')
    assert stream.getvalue().splitlines() == [
        "[gui 42]   Lumi GUI running at http://127.0.0.1:1",
        "[gui 42] Traceback (most recent call last):",
        '[gui 42]   File "x.py"',
        "[gui 42] error",
    ]
    assert not log.isatty() and log.getvalue() == stream.getvalue()  # the rest is the file's
    assert process_role(["lumi.exe", "gui", "--browser"]) == "gui"
    assert process_role(["lumi.exe"]) == "tui" and process_role(["lumi.exe", "--version"]) == "cli"
