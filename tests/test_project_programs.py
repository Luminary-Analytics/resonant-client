"""Lumi's own programs never come from the open project (lumi/executables.py).

The project here holds harmless programs named like the tools Lumi starts
(git, rg, explorer, findstr…); each one only adds its name to a file outside
the project. Lumi's own code paths then run the way the app used to and the
terminal UI and ``lumi run`` still do: with the project as the working folder,
PATH listing the working folder, a relative folder and the project itself,
and without ``NoDefaultCurrentDirectoryInExePath``, so that each code path is
checked on its own and not only through the process-wide setting. None of the
programs may run. Real processes throughout: nothing here is mocked.
"""

from __future__ import annotations

import asyncio
import io
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import executables

WINDOWS = sys.platform == "win32"
ROOT = Path(__file__).resolve().parents[1]

# What Lumi starts by name, as each could be planted.
PLANTED = (
    ["git.exe", "git.bat", "git.cmd", "rg.exe", "rg.bat", "findstr.exe", "explorer.exe", "cmd.exe",
     "powershell.exe", "taskkill.exe", "node.exe", "bash.exe", "ffmpeg.exe", "pylsp.exe",
     "pyright-langserver.cmd", "typescript-language-server.cmd", "code.cmd", "codex.cmd", "claude.cmd",
     "az.cmd", "gcloud.cmd", "uvx.exe", "ruff.exe"]
    if WINDOWS else
    ["git", "rg", "grep", "open", "xdg-open", "osascript", "bash", "node", "ffmpeg", "pylsp", "code",
     "codex", "claude", "az", "gcloud", "uvx", "ruff", "crontab", "launchctl"]
)


def _launcher() -> bytes:
    """distlib's console launcher, which pip puts in front of every script .exe it installs."""
    try:
        import pip._vendor.distlib as distlib
    except ImportError:  # pragma: no cover - pip comes with every Python the tests run on
        pytest.fail("Planting .exe programs needs pip's distlib launcher")
    name = "t64-arm.exe" if platform.machine().lower() in ("arm64", "aarch64") else "t64.exe"
    return (Path(distlib.__file__).parent / name).read_bytes()


def plant(folder: Path, name: str, record: Path) -> Path:
    """A program ``name`` in ``folder`` that only adds its name to ``record``."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if name.lower().endswith((".bat", ".cmd")):
        path.write_text(f'@echo off\r\necho {name}>>"{record}"\r\n', encoding="ascii")
    elif name.lower().endswith(".exe"):
        # An .exe that runs Python on the zip appended to it, as pip's script launchers do.
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("__main__.py", f"open({str(record)!r}, 'a').write({name!r} + '\\n')\n")
        path.write_bytes(_launcher() + b'#!"' + sys.executable.encode() + b'"\n' + stream.getvalue())
    else:
        path.write_text(f'#!/bin/sh\necho {name} >> "{record}"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """An open project full of planted programs, as described above."""
    git = shutil.which("git")  # the real one, found before anything changes
    project = tmp_path / "project"
    record = tmp_path / "ran.txt"
    for folder in (project, project / "bin"):
        for name in PLANTED:
            plant(folder, name, record)
    (project / "notes.txt").write_text("needle in the project\n", encoding="utf-8")
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
    if git:
        environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for arguments in (["init", "-q"], ["add", "notes.txt", "app.py"], ["commit", "-q", "-m", "first"]):
            subprocess.run([git, *arguments], cwd=project, env=environment, check=True, capture_output=True)
    path_before = os.environ.get("PATH", "")
    monkeypatch.delenv(executables.NO_CURRENT_FOLDER, raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "bin", str(project), str(project / "bin"), path_before]))
    monkeypatch.chdir(project)

    def ran() -> str:
        return record.read_text(encoding="utf-8", errors="replace") if record.exists() else ""

    return SimpleNamespace(project=project, record=record, git=git, ran=ran, path_before=path_before)


def _inside(path: str | None, folder: Path) -> bool:
    if not path:
        return False
    real = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(folder))
    return real == root or real.startswith(root + os.sep)


def test_the_planted_programs_run_when_named_in_full(planted):
    """The fixture's programs work, so nothing below passes because they couldn't run."""
    for name in (["git.exe", "git.bat"] if WINDOWS else ["git"]):
        subprocess.run([str(planted.project / name)], capture_output=True, timeout=120)
        assert name in planted.ran()


def test_git_status_when_a_project_opens(planted):
    """The page asks for Git status as soon as a project opens."""
    from lumi.gui import ws_commands

    sent = []

    async def send_json(payload):
        sent.append(payload)

    state = SimpleNamespace(project=SimpleNamespace(project_path=str(planted.project), current_session=None))
    context = ws_commands.CommandContext(ws=SimpleNamespace(send_json=send_json), state=state, msg={}, runs=None)
    asyncio.run(ws_commands.HANDLERS["git_status"](context))
    assert planted.ran() == ""
    if planted.git:
        status = sent[-1]["data"]
        assert status["is_repo"] and status["branch"] and status["commits"]


def test_the_agents_git_tools_indexing_and_context(planted):
    from lumi import handoff, model_evals
    from lumi.engine import git_tools
    from lumi.engine.context_broker import ContextBroker
    from lumi.engine.rag import CodebaseIndex
    from lumi.gui import autonomous_factory, editor_bridge

    status = git_tools.git_status(planted.project)
    stats = CodebaseIndex(planted.project).index(force=True)
    ContextBroker(planted.project)._diff("working")
    head = handoff._git(str(planted.project), "rev-parse", "HEAD")
    editor_bridge._git(planted.project, "rev-parse", "HEAD")
    model_evals._git(str(planted.project), "status", "--porcelain")
    sha = autonomous_factory.make_git_get_commit_sha(str(planted.project))()
    assert planted.ran() == ""
    if planted.git:
        assert status.get("branch") and head and head == sha and stats


@pytest.mark.parametrize("ripgrep", ["bundled or fetched", "from PATH"])
def test_search(planted, monkeypatch, ripgrep, tmp_path):
    from lumi.engine import tools

    if ripgrep == "from PATH":
        monkeypatch.setattr(tools, "_VENDORED_RIPGREP_DIR", tmp_path / "no-ripgrep")
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    tools._bundled_ripgrep.cache_clear()
    try:
        result = tools.execute_tool("grep", {"pattern": "needle", "path": str(planted.project)},
                                    project_path=str(planted.project))
    finally:
        tools._bundled_ripgrep.cache_clear()
    assert planted.ran() == ""
    assert "notes.txt" in result.output


def test_language_servers_editors_and_command_lines(planted):
    """What Settings lists (and code_intel would start) never comes from the project either."""
    from lumi.backends import resolve_claude_cli_path, resolve_codex_cli_path
    from lumi.code_editors import vscode_editors
    from lumi.engine import lsp
    from lumi.gui.ws_commands import _lsp_list_payload

    class NoConfiguredServers:
        def get(self, section, key=None, default=None):
            return default

    payload = _lsp_list_payload(project_path=str(planted.project), settings=NoConfiguredServers())
    assert all(str(planted.project) not in server["detail"] for server in payload["servers"])
    for name in ("app.py", "app.ts"):
        try:
            _, argv = lsp.choose(name, NoConfiguredServers(), str(planted.project))
        except lsp.LspError:
            continue  # none installed on this computer
        assert not _inside(argv[0], planted.project)
    found = [resolve_codex_cli_path(), resolve_claude_cli_path(),
             *(editor["path"] for editor in vscode_editors())]
    for name in ("az", "gcloud", "uvx", "node", "ffmpeg", "bash", "ruff"):
        found.append(executables.find_program(name, scripts=name in ("az", "gcloud")))
    assert not [path for path in found if _inside(path, planted.project)]
    assert planted.ran() == ""


def test_show_in_folder_and_the_systems_own_tools(planted):
    argv = executables.show_in_folder_command(planted.project / "notes.txt")
    assert not _inside(argv[0], planted.project)
    if WINDOWS:
        assert argv[0] == os.path.join(executables.windows_folder(), "explorer.exe") and os.path.isfile(argv[0])
        for tool in ("cmd", "powershell", "taskkill", "findstr", "clip", "schtasks"):
            path = executables.system_program(tool)
            assert os.path.isfile(path) and _inside(path, Path(executables.system_folder()))
    else:
        for tool in ("sh", "grep"):
            assert not _inside(executables.system_program(tool), planted.project)
    assert planted.ran() == ""


@pytest.mark.skipif(not WINDOWS, reason="POSIX shells never looked in the working folder")
def test_the_agents_shell_still_runs_what_it_is_asked_to(planted, monkeypatch):
    """By design: a command the model or the person runs finds the project's own scripts."""
    from lumi.engine import tools

    plant(planted.project, "lumi-probe.bat", planted.record)
    # Found only through the working folder, as in the person's own cmd.exe,
    # for a person whose environment doesn't set NoDefaultCurrentDirectoryInExePath.
    monkeypatch.setenv("PATH", planted.path_before)
    monkeypatch.setenv(executables.PERSON_SETTING, "unset")
    result = tools.execute_tool("bash", {"command": "lumi-probe", "cwd": str(planted.project), "timeout": 60},
                                project_path=str(planted.project))
    assert planted.ran().split() == ["lumi-probe.bat"], result.output


GRANDCHILD = """\
import os, sys
sys.path.insert(0, {root!r})
from lumi import executables
names = [key.upper() for key in executables.person_environment()]
print(os.environ.get(executables.PERSON_SETTING), executables.NO_CURRENT_FOLDER.upper() in names)
"""

CHILD = """\
import os, shutil, subprocess, sys
sys.path.insert(0, {root!r})
import lumi
from lumi import executables
print(os.environ.get(executables.NO_CURRENT_FOLDER), os.environ.get(executables.PERSON_SETTING))
for command, shell in ((["git", "--version"], False), ("git --version", True),
                       (["rg", "--version"], False), ("rg --version", True)):
    try:
        subprocess.run(command, shell=shell, capture_output=True, timeout=120)
    except OSError:
        pass
print(shutil.which("git") if sys.version_info >= (3, 12) else "")
nested = subprocess.run([sys.executable, {grandchild!r}], capture_output=True, text=True, timeout=120)
print(nested.stdout.strip())
"""


@pytest.mark.skipif(not WINDOWS, reason="Windows looks in the working folder; POSIX systems don't")
def test_importing_lumi_hardens_every_launch_in_the_process(planted, tmp_path):
    """The process-wide layer, in a real process that starts without the setting.

    After ``import lumi`` even plain launches by bare name (CreateProcess,
    cmd.exe through ``shell=True``, ``shutil.which`` from Python 3.12) leave
    the working folder out. A Lumi process it starts (a worker, ``lumi run``)
    keeps the person's own setting for the commands they run.
    """
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(GRANDCHILD.format(root=str(ROOT)), encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(root=str(ROOT), grandchild=str(grandchild)), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in (executables.NO_CURRENT_FOLDER.upper(), executables.PERSON_SETTING.upper())}
    environment.update(PATH=planted.path_before, PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, str(child)], cwd=planted.project, env=environment,
                               capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr
    hardened, which, nested = completed.stdout.splitlines()
    assert hardened == "1 unset"
    assert not _inside(which, planted.project)
    assert nested == "unset False"  # the grandchild's commands get the person's own setting: none
    assert planted.ran() == ""


def test_every_entry_point_imports_the_package_first():
    """Each console script and the frozen app run inside ``lumi``, whose import hardens the process."""
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts and all(target.split(":")[0].split(".")[0] == "lumi" for target in scripts.values())
    spec = (ROOT / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    assert '[str(PKG_ROOT / "__main__.py")]' in spec
    main = (ROOT / "lumi" / "__main__.py").read_text(encoding="utf-8")
    imports = [line for line in main.splitlines() if line.startswith(("import ", "from "))]
    # Only the standard library's os and sys come before the package (the frozen app's script).
    assert imports[:3] == ["import os", "import sys", "from lumi import __version__"]
