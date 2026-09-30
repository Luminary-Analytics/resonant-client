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
import sys
from types import SimpleNamespace

import pytest

from lumi import git_support, toolchain
from lumi.engine.checkpoint_timeline import SessionCheckpointStore
from lumi.engine.tools import execute_tool
from lumi.engine.worktrees import WorktreeError, WorktreeManager
from lumi.orchestration.checkpoints import CheckpointError, IterationCheckpointStore
from lumi.processes import OutputDecoder, decode_output, utf8_env
from lumi.startup_log import TaggedLog, process_role


def _without_git(monkeypatch, tmp_path):
    """No Git on this computer: PATH without Git's folders, as on a new Windows PC.

    Nothing is patched: every lookup and every ``git`` Lumi starts meets
    the PATH a person without Git has.
    """
    if os.name == "nt":
        kept = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry and not any(
            os.path.isfile(os.path.join(entry, name)) for name in ("git.exe", "git.cmd", "git.bat", "git.com"))]
        monkeypatch.setenv("PATH", os.pathsep.join(kept))
    else:
        # /usr/bin holds Git with everything else: an empty folder instead.
        empty = tmp_path / "bin-without-git"
        empty.mkdir(exist_ok=True)
        monkeypatch.setenv("PATH", str(empty))
    toolchain.clear_cache()
    assert shutil.which("git") is None and not git_support.git_available()


# ── Git is optional ────────────────────────────────────────────────────────


def test_the_page_learns_git_is_missing_and_what_needs_it(monkeypatch, tmp_path):
    _without_git(monkeypatch, tmp_path)
    status = git_support.status()
    assert status["available"] is False
    assert status["unavailable"] == ["writer_teams", "worktrees", "mission_checkpoints", "model_comparisons"]
    assert status["download_url"].startswith("https://git-scm.com/")
    assert "Writer teams need" in git_support.missing_message("Writer teams need")


def test_worktree_manager_without_git_is_unavailable_not_an_error(monkeypatch, tmp_path):
    _without_git(monkeypatch, tmp_path)
    manager = WorktreeManager(tmp_path / "project", root=tmp_path / "worktrees")
    assert manager.available is False
    with pytest.raises(WorktreeError, match="Git"):
        manager.create("agent")


def test_checkpoints_without_git_say_so_and_turn_checkpoints_use_an_archive(monkeypatch, tmp_path):
    _without_git(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("kept\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="isn't installed"):
        IterationCheckpointStore(project)
    checkpoint = SessionCheckpointStore(project, session_id="s", root=tmp_path / "cp").create(
        conversation_history=[], reason="turn")
    assert checkpoint.workspace_archive and not checkpoint.workspace_ref


def test_every_git_caller_says_git_is_missing_without_git(monkeypatch, tmp_path):
    from lumi import model_evals
    from lumi.engine import git_tools
    from lumi.engine.context_broker import ContextBroker
    from lumi.gui import editor_bridge

    project = tmp_path / "Jöhn Smith" / "Alpha Project"
    project.mkdir(parents=True)
    _without_git(monkeypatch, tmp_path)
    assert "isn't installed" in git_tools._run_git(["status"], project)[2]
    assert b"isn't installed" in editor_bridge._git(project, "status").stderr
    assert "Model comparisons need" in model_evals._git(str(project), "status").stderr
    # @diff says why there's no diff instead of dropping the mention silently.
    diff = ContextBroker(project).resolve_mentions("What changed? @diff:working")
    assert [item.provider for item in diff] == ["diff"] and "Attaching @diff needs" in diff[0].content


class _StubBackend:
    name = "ollama"
    model = "stub"
    tool_mode = "native"
    base_url = "http://test"
    api_key = None

    def stream(self, **_kwargs):
        raise AssertionError("a refused sub-agent never reaches a model")


def test_a_sub_agent_asking_for_its_own_worktree_is_refused_without_git(monkeypatch, tmp_path):
    """Its caller wanted it kept apart from the checkout; the shared workspace isn't that."""
    from lumi.engine.session import Session

    project = tmp_path / "Jöhn Smith" / "Alpha Project"
    (project / ".git").mkdir(parents=True)  # a repository copied from another computer
    _without_git(monkeypatch, tmp_path)
    session = Session(backend=_StubBackend())
    session.project_path = str(project)
    session.worktree_manager = WorktreeManager(project, root=tmp_path / "worktrees")
    assert session.worktree_manager.available is False
    events = list(session._execute_task({"prompt": "Add a note", "agent_type": "build"}, "call-1", "{}"))
    assert [event["event"] for event in events] == ["tool.result"], events
    assert events[0]["is_error"] and "isn't installed" in events[0]["output"]
    assert 'isolation "shared"' in events[0]["output"]
    assert session.conversation_history[-1]["content"] == events[0]["output"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs Git: a folder that is gone, with Git installed")
def test_a_folder_that_is_gone_is_not_reported_as_git_missing(tmp_path):
    from lumi import model_evals
    from lumi.engine import git_tools
    from lumi.gui import editor_bridge

    gone = tmp_path / "Jöhn Smith" / "moved-away"
    # Starting Git in it fails like a missing Git does ([WinError 267] on
    # Windows, FileNotFoundError elsewhere); only the reason differs.
    with pytest.raises(CheckpointError) as refused:
        IterationCheckpointStore(gone)
    assert "isn't installed" not in str(refused.value) and "doesn't exist" in str(refused.value)
    for message in (git_tools._run_git(["status"], gone)[2],
                    editor_bridge._git(gone, "status").stderr.decode("utf-8"),
                    WorktreeManager._git_at(gone, "status", check=False).stderr):
        assert "isn't installed" not in message and "doesn't exist" in message, message
    assert "isn't installed" not in model_evals._git(str(gone), "status").stderr
    manager = WorktreeManager(gone, root=tmp_path / "worktrees")
    with pytest.raises(WorktreeError, match="requires a git repository"):
        manager.create("agent")


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
    _without_git(monkeypatch, tmp_path)
    state.create_backend("ollama", "stub")  # raised FileNotFoundError before
    assert state.session is not None and state.runtime_error == ""
    assert state.session.worktree_manager.available is False
    assert state.get_init_data()["git"]["available"] is False


# ── Command output ─────────────────────────────────────────────────────────


PAGES = {"ansi_code_page": "cp1252", "oem_code_page": "cp437"}


def test_output_decodes_utf8_and_else_the_ansi_or_oem_code_page():
    assert decode_output("Jürgen".encode("utf-8"), **PAGES) == "Jürgen"
    # cmd.exe and findstr write the OEM code page: 0x81 is "ü", undefined in cp1252.
    assert decode_output(b"J\x81rgen M\x81ller", **PAGES) == "Jürgen Müller"
    assert decode_output(b"J\x94hn Smith\r\n", ansi_code_page="cp1252", oem_code_page="cp850") == "Jöhn Smith\n"
    # Python outside UTF-8 mode writes the ANSI one: 0xFC 0xF6 are "üö" there,
    # "ⁿ÷" in cp437, which is what "Jürgen Jöhn" became.
    assert decode_output(b"J\xfcrgen J\xf6hn", **PAGES) == "Jürgen Jöhn"
    assert decode_output(None) == "" and decode_output("text") == "text"


def test_each_line_is_decoded_on_its_own():
    """One command can mix programs: cmd's echo, a Python outside UTF-8 mode, Git."""
    mixed = b"\r\n".join(("Jürgen".encode("cp437"), "Jöhn".encode("cp1252"), "Müller".encode("utf-8"), b"plain"))
    assert decode_output(mixed, **PAGES) == "Jürgen\nJöhn\nMüller\nplain"


def test_output_read_in_pieces_never_splits_a_character():
    """Jobs and previews read 1 KiB at a time; "ü" is two bytes in UTF-8."""
    data = "Jürgen Jöhn\nZweite Zeile: ü\nlast".encode("utf-8")
    decoder = OutputDecoder(**PAGES)
    pieces = [decoder.decode(data[index:index + 1]) for index in range(len(data))]
    pieces.append(decoder.decode(b"", final=True))
    assert "".join(pieces) == "Jürgen Jöhn\nZweite Zeile: ü\nlast"
    assert pieces[:12] == [""] * 12  # nothing until the line is whole
    # A line longer than the limit comes out in parts, never inside a character.
    decoder = OutputDecoder(limit=16, **PAGES)
    long = ("ü" * 40).encode("utf-8")
    parts = [decoder.decode(long[index:index + 7]) for index in range(0, len(long), 7)]
    parts.append(decoder.decode(b"", final=True))
    assert "".join(parts) == "ü" * 40 and not any("\ufffd" in part for part in parts)
    # A line in the OEM code page, split across reads, reads as one.
    decoder = OutputDecoder(**PAGES)
    assert decoder.decode(b"J\x81r") + decoder.decode(b"gen\r\n") == "Jürgen\n"


def test_a_jobs_log_keeps_every_character(monkeypatch, tmp_path):
    """A managed job's output arrives 1 KiB at a time; each piece was decoded on its own."""
    import time

    from lumi.engine.jobs import JobManager

    monkeypatch.delenv("PYTHONUTF8", raising=False)
    expected = "Jöhn Smith ü " * 600  # 15 bytes each: reads end inside characters
    manager = JobManager()
    try:
        started = manager.start(str(tmp_path), [sys.executable, "-c", "import sys; sys.stdout.write('Jöhn Smith ü ' * 600)"],
                                timeout=60)
        deadline = time.monotonic() + 60
        status = manager.status(str(tmp_path), started["id"])
        # The log's reader may still be appending the last piece when the job ends.
        while (status["state"] == "running" or len(status["logs"]) < len(expected)) and time.monotonic() < deadline:
            time.sleep(0.05)
            status = manager.status(str(tmp_path), started["id"])
    finally:
        manager.close()
    assert status["state"] == "completed", status
    assert status["logs"] == expected and "�" not in status["logs"]


def test_python_children_write_utf8_and_nothing_else_changes():
    """PYTHONIOENCODING, not PYTHONUTF8: only the pipes, never what open() reads and writes."""
    environment = utf8_env({"PATH": "x"})
    assert environment["PYTHONIOENCODING"] == "utf-8" and "PYTHONUTF8" not in environment
    # The person's own choice of either stands (decode_output reads what comes).
    assert utf8_env({"PYTHONUTF8": "0"}) == {"PYTHONUTF8": "0"}
    assert utf8_env({"PYTHONUTF8": "1"}) == {"PYTHONUTF8": "1"}
    assert utf8_env({"PYTHONIOENCODING": "cp1252"}) == {"PYTHONIOENCODING": "cp1252"}


def test_a_scripts_own_files_keep_their_encoding(monkeypatch, tmp_path):
    """A cp1252 CSV read with open()'s default still reads; a file written by default is still cp1252."""
    import subprocess

    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    # What open() uses by default in a child Python here (cp1252 on a Western Windows).
    page = subprocess.run([sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "legacy.csv").write_bytes("name;city\r\nJosé;Zürich\r\n".encode(page))
    (tmp_path / "read_it.py").write_text("print(open('legacy.csv').read().splitlines()[1])\n", encoding="utf-8")
    (tmp_path / "write_it.py").write_text("open('out.txt', 'w').write('Zürich')\n", encoding="utf-8")
    result = _shell(f'"{sys.executable}" read_it.py', tmp_path)
    assert not result.is_error and result.output == "José;Zürich", result.output
    result = _shell(f'"{sys.executable}" write_it.py', tmp_path)
    assert not result.is_error, result.output
    assert (tmp_path / "out.txt").read_bytes() == "Zürich".encode(page)


def _shell(command: str, cwd: Path):
    return execute_tool("bash", {"command": command, "cwd": str(cwd)}, project_path=str(cwd))


def _python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_the_shell_tool_reads_a_python_child_and_its_traceback(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    result = _shell(_python("print('J\\u00fcrgen J\\u00f6hn')"), tmp_path)
    assert result.output == "Jürgen Jöhn", result.output
    missing = tmp_path / "Jöhn Smith" / "missing.txt"
    result = _shell(_python(f"open(r'{missing}')"), tmp_path)
    assert result.is_error and "FileNotFoundError" in result.output
    assert repr(str(missing)) in result.output, result.output  # the error shows the name's repr
    # A Python the person set to write the ANSI code page still reads right.
    monkeypatch.setenv("PYTHONUTF8", "0")
    result = _shell(_python("print('J\\u00fcrgen J\\u00f6hn')"), tmp_path)
    assert result.output == "Jürgen Jöhn", result.output


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_the_shell_tool_reads_cmd_output_with_umlauts(monkeypatch, tmp_path):
    monkeypatch.setenv("LUMI_TEST_NAME", "C:\\Users\\Jöhn Smith")
    (tmp_path / "Jöhn Smith").mkdir()
    result = _shell("echo Jürgen Müller & echo %LUMI_TEST_NAME% & dir /b", tmp_path)
    assert not result.is_error, result.output
    assert [line.strip() for line in result.output.splitlines()] == ["Jürgen Müller", "C:\\Users\\Jöhn Smith",
                                                                     "Jöhn Smith"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_cmd_and_a_python_child_in_one_command(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    result = _shell("echo Jürgen & " + _python("print('J\\u00f6hn')") + " && exit /b 3", tmp_path)
    assert [line.strip() for line in result.output.splitlines() if line.strip()] == ["Jürgen", "Jöhn", "(exit code: 3)"]
    # The command's quotes, carets and percent signs mean what they did in cmd.exe /c.
    result = _shell('echo "a & b" ^& c %% & set /a 6*7', tmp_path)
    assert result.output.splitlines() == ['"a & b" & c %% ', "42"]


# ── Searches ───────────────────────────────────────────────────────────────


def _project_with_lumi_files(tmp_path, *, final_newline=True) -> Path:
    """A project under a "Jöhn Smith" folder, with the person's .lumi files and Lumi's old index."""
    end = "\n" if final_newline else ""
    project = tmp_path / "Jöhn Smith" / "Alpha Project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("needle in the readme" + end, encoding="utf-8")
    (project / "src" / "Greet.py").write_text("# needle" + end, encoding="utf-8")
    (project / ".lumi" / "packs" / "demo").mkdir(parents=True)
    (project / ".lumi" / "index.json").write_text('{"needle": 1}', encoding="utf-8")  # the old index
    (project / ".lumi" / "LUMI.md").write_text("Always run the linter.\n", encoding="utf-8")
    (project / ".lumi" / "packs" / "demo" / "pack.json").write_text('{"tool": "fetch_rates"}\n', encoding="utf-8")
    return project


CODE_MATCHES = ["README.md:1:needle in the readme", os.path.join("src", "Greet.py") + ":1:# needle"]


def _grep(project: Path, pattern: str, path: str | None = None, **extra):
    return execute_tool("grep", {"pattern": pattern, "path": path or os.path.normcase(str(project)), **extra},
                        project_path=str(project))


def test_grep_shows_project_relative_paths_as_spelled_and_skips_the_old_index(tmp_path):
    project = _project_with_lumi_files(tmp_path)
    # The sandbox hands tools a case-folded absolute root.
    result = _grep(project, "needle")
    assert sorted(result.output.splitlines()) == CODE_MATCHES, result.output


def test_the_persons_lumi_files_are_searched(tmp_path):
    """.lumi/LUMI.md, capability packs and mission roadmaps are the person's, not Lumi's state."""
    project = _project_with_lumi_files(tmp_path)
    assert _grep(project, "fetch_rates").output.splitlines() == [
        os.path.join(".lumi", "packs", "demo", "pack.json") + ':1:{"tool": "fetch_rates"}']
    listed = execute_tool("glob", {"pattern": "**/*.md", "path": str(project)}, project_path=str(project))
    assert sorted(listed.output.splitlines()) == [os.path.join(".lumi", "LUMI.md"), "README.md"]
    shown = set(execute_tool("glob", {"pattern": "**/*", "path": os.path.normcase(str(project))},
                             project_path=str(project)).output.splitlines())
    assert shown == {"README.md", "src", os.path.join("src", "Greet.py"), ".lumi", os.path.join(".lumi", "LUMI.md"),
                     os.path.join(".lumi", "packs"), os.path.join(".lumi", "packs", "demo"),
                     os.path.join(".lumi", "packs", "demo", "pack.json")}
    # A search that names .lumi skips nothing there, the old index included.
    inside = _grep(project, "needle", str(project / ".lumi"))
    assert inside.output.splitlines() == [os.path.join(".lumi", "index.json") + ':1:{"needle": 1}']
    listed = execute_tool("glob", {"pattern": ".lumi/*.json", "path": str(project)}, project_path=str(project))
    assert listed.output.splitlines() == [os.path.join(".lumi", "index.json")]


def test_a_search_that_only_finds_the_old_index_says_so(tmp_path):
    project = _project_with_lumi_files(tmp_path)
    result = _grep(project, "needle", glob="*.json")
    assert result.output.startswith("(no matches)")
    assert "old codebase index (.lumi/index.json) not shown" in result.output


def test_the_gitignore_note_names_what_the_agent_can_run(tmp_path, monkeypatch):
    """The shell can run rg only from PATH; Lumi's own copy isn't there."""
    from lumi.engine import tools

    if not tools._ripgrep_executable():
        pytest.skip("needs ripgrep (packaging/fetch_ripgrep.ps1)")
    project = _project_with_lumi_files(tmp_path)
    real_find = tools.find_program
    monkeypatch.setattr(tools, "find_program", lambda name, *a, **k: None if name == "rg" else real_find(name, *a, **k))
    note = _grep(project, "nowhere-to-be-found").output
    assert "files your .gitignore excludes were not searched" in note and "rg --no-ignore" not in note
    monkeypatch.setattr(tools, "find_program", lambda name, *a, **k: "/usr/bin/rg" if name == "rg" else None)
    assert "rg --no-ignore" in _grep(project, "nowhere-to-be-found").output


@pytest.mark.parametrize("final_newline", [True, False])
def test_grep_without_ripgrep_keeps_every_match_and_no_excluded_one(tmp_path, monkeypatch, final_newline):
    """findstr (grep on POSIX) instead, in a project under "Jöhn Smith".

    findstr doesn't end a match that is a file's last line without a final
    newline, so the next match followed on the same line, and it writes the
    folder's "ö" in the console's code page. Decoded as a whole, the split
    missed, and an excluded secret.txt rode along on another file's line.
    """
    from lumi.engine import tools
    from lumi.engine.exclusions import ExclusionRules

    monkeypatch.setattr(tools, "_ripgrep_executable", lambda trusted_only=False, project=None: None)
    project = _project_with_lumi_files(tmp_path, final_newline=final_newline)
    (project / "secret.txt").write_text("needle: the excluded password" + ("\n" if final_newline else ""),
                                        encoding="utf-8")
    rules = ExclusionRules.for_project(str(project), settings_patterns=["secret.txt"])
    result = execute_tool("grep", {"pattern": "needle", "path": os.path.normcase(str(project))},
                          project_path=str(project), exclusions=rules)
    assert "password" not in result.output, result.output
    lines = result.output.splitlines()
    assert sorted(lines[:-1]) == CODE_MATCHES, result.output
    assert lines[-1] == "[1 match in excluded files not shown (file exclusion rules)]"


@pytest.mark.skipif(sys.platform != "win32", reason="findstr")
def test_findstr_output_is_split_before_each_file_not_before_the_root_in_text(monkeypatch):
    from lumi import processes
    from lumi.engine.tools import _findstr_lines

    monkeypatch.setattr(processes, "_oem_code_page", lambda: "cp437")
    monkeypatch.setattr(processes, "_ansi_code_page", lambda: "cp1252")
    root = r"C:\Users\Jöhn Smith\Alpha Project"
    # The first file has no final newline and names the root in its text; the
    # second's text is the file's own UTF-8. findstr writes paths in cp437.
    data = (rf"{root}\notes.txt:1:see {root}\x for more".encode("cp437")
            + rf"{root}\a.txt:2:".encode("cp437") + "Jürgen".encode("utf-8") + b"\r\n")
    assert _findstr_lines(data, root) == [rf"{root}\notes.txt:1:see {root}\x for more", rf"{root}\a.txt:2:Jürgen"]


def test_a_project_under_lumis_own_folder_is_still_searched(tmp_path):
    project = tmp_path / ".lumi" / "workspace"  # the fallback workspace
    project.mkdir(parents=True)
    (project / "a.txt").write_text("needle\n", encoding="utf-8")
    result = execute_tool("grep", {"pattern": "needle", "path": str(project)}, project_path=str(project))
    assert result.output.splitlines() == ["a.txt:1:needle"]


# ── The prompt's environment line ──────────────────────────────────────────


def _programs(monkeypatch, found: dict[str, str]):
    monkeypatch.setattr(toolchain, "find_program", lambda name, *a, **k: found.get(name))
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


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions")
def test_a_portable_copy_reached_through_a_junction_is_not_a_project(monkeypatch, tmp_path):
    """Explorer starts the copy in the folder as the person reached it (a junction, a subst or mapped drive)."""
    import _winapi

    from lumi.gui import sessions

    real = tmp_path / "real"
    (real / "Lumi Portable" / "_internal").mkdir(parents=True)
    (real / "Lumi Portable" / "lumi.exe").write_bytes(b"MZ")
    (real / "My Project").mkdir()
    linked = tmp_path / "linked"
    _winapi.CreateJunction(str(real), str(linked))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(linked / "Lumi Portable" / "lumi.exe"))
    for folder in (linked / "Lumi Portable", linked / "Lumi Portable" / "_internal", linked,
                   real / "Lumi Portable", real):
        assert sessions._is_unsafe_cwd(str(folder)), folder
    assert not sessions._is_unsafe_cwd(str(linked / "My Project"))
    # And the other way round: the app started through its real folder.
    monkeypatch.setattr(sys, "executable", str(real / "Lumi Portable" / "lumi.exe"))
    assert sessions._is_unsafe_cwd(str(linked / "Lumi Portable"))


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
    monkeypatch.setattr(updater, "_dll", None)
    monkeypatch.setattr(updater, "_sparkle", None)
    monkeypatch.setattr(updater, "_stopped_for_offline", False)
    assert updater.component_missing()
    # Settings > Updates and Help > Check for Updates show the status's reason.
    reason = updater._unavailable_reason(update_channels.UpdatePreferences(platform="windows"))
    assert reason == updater.MISSING_COMPONENT_MESSAGE
    message = _update_check_message({"mode": "automatic", "unavailable": reason}, False)
    assert "Reinstall Lumi" in message and "source" not in message
    assert update_channels.main([]) == 0
    assert "reinstall" in json.loads(capsys.readouterr().out)["updater"]


# ── A double-clicked lumi.exe ──────────────────────────────────────────────


class TestDoubleClickingLumiExe:
    """The windowless lumi.exe opened from Explorer is the app; a command line stays a command line.

    The Windows build has no console, so Explorer, a shortcut or the Start
    menu start it without standard input, output or error. Without arguments
    it used to start the terminal UI invisibly, waiting for input that never
    came. The macOS side is tests/test_sparkle.py's TestOpeningTheApp.
    """

    EXE = [r"C:\Users\Jöhn Smith\Downloads\Lumi\lumi.exe"]
    OPENED = {"platform": "win32", "frozen": True, "streams": (None, None, None)}

    def facts(self, **changed):
        return {**self.OPENED, **changed}

    def test_a_double_click_opens_the_app(self):
        from lumi import __main__ as entry

        assert entry._started_without_streams(**self.OPENED)
        assert entry._opened_as_windows_app(self.EXE, **self.OPENED)

    def test_a_command_line_keeps_its_behavior(self):
        from lumi import __main__ as entry

        others = {
            "a command": (self.EXE + ["run", "fix the test"], self.OPENED),
            "the browser GUI": (self.EXE + ["gui", "--browser"], self.OPENED),
            "--version": (self.EXE + ["--version"], self.OPENED),
            # A script that sends output or input somewhere means the command line.
            "redirected output": (self.EXE, self.facts(streams=(None, io.StringIO(), None))),
            "redirected input": (self.EXE, self.facts(streams=(io.StringIO(), None, None))),
            "redirected errors": (self.EXE, self.facts(streams=(None, None, io.StringIO()))),
            "from source": (self.EXE, self.facts(frozen=False)),
            "macOS": (self.EXE, self.facts(platform="darwin")),
        }
        for how, (argv, facts) in others.items():
            assert not entry._opened_as_windows_app(argv, **facts), how

    def _run_main(self, monkeypatch, argv, *, double_click):
        """What main() starts for ``argv``, with the GUI, terminal UI and updater stubbed out."""
        from lumi import __main__ as entry
        from lumi import updater

        started = []
        monkeypatch.setattr(entry, "_STARTED_WITHOUT_STREAMS", double_click)
        monkeypatch.setattr(entry, "_LAUNCHED_BY_LAUNCHSERVICES", False)
        monkeypatch.setattr(updater, "init_updater", lambda *args, **kwargs: False)
        # main() leaves its working folder for the app (lumi/executables.py):
        # for real, it would move this test process into the system folder.
        monkeypatch.setattr("lumi.executables.leave_working_folder", lambda: "")
        monkeypatch.setitem(sys.modules, "lumi.gui.server",
                            SimpleNamespace(main=lambda: started.append(("gui", sys.argv[1:]))))
        monkeypatch.setitem(sys.modules, "lumi.tui", SimpleNamespace(main=lambda: started.append(("tui", sys.argv[1:]))))
        monkeypatch.setattr(sys, "argv", list(argv))
        entry.main()
        return started

    def test_main_opens_the_gui_for_a_double_click_and_the_terminal_ui_otherwise(self, monkeypatch):
        assert self._run_main(monkeypatch, self.EXE, double_click=True) == [("gui", [])]
        assert self._run_main(monkeypatch, self.EXE, double_click=False) == [("tui", [])]
        assert self._run_main(monkeypatch, self.EXE + ["gui", "--browser"], double_click=True) == [("gui", ["--browser"])]

    def test_main_runs_a_command_even_without_streams(self, monkeypatch, capsys):
        assert self._run_main(monkeypatch, self.EXE + ["--version"], double_click=True) == []
        assert capsys.readouterr().out.startswith("lumi ")


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
