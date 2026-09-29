"""Where Lumi's own programs come from (lumi/executables.py), against real files and folders."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lumi import executables
from lumi.executables import configured_program, find_program, person_environment, project_command

WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""


def _program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """A working folder that is neither the project nor above anything the tests install."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


def test_path_entries_that_mean_the_working_folder_are_skipped(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _program(work, "tool" + EXE)
    _program(work / "rel", "tool" + EXE)
    installed = _program(tmp_path / "installed", "tool" + EXE)
    monkeypatch.chdir(work)
    entries = ["", ".", "rel", os.path.join(".", "rel"), f'"{work / "rel"}"']
    if WINDOWS:
        drive, rest = os.path.splitdrive(str(work / "rel"))
        entries += [drive + "rel", rest]  # relative to the drive's working folder, and to its root
    monkeypatch.setenv("PATH", os.pathsep.join([*entries, str(installed.parent)]))
    assert find_program("tool") == str(installed)


def test_folders_inside_the_project_or_the_working_folder_are_skipped(tmp_path, monkeypatch, elsewhere):
    project = tmp_path / "project"
    in_project = _program(project / "node_modules" / ".bin", "tool" + EXE)
    _program(project / ".venv" / ("Scripts" if WINDOWS else "bin"), "tool" + EXE)
    installed = _program(tmp_path / "installed", "tool" + EXE)
    monkeypatch.setenv("PATH", os.pathsep.join([str(in_project.parent), str(project / ".venv" / "Scripts"),
                                                str(project / ".venv" / "bin"), str(installed.parent)]))
    assert find_program("tool", exclude=[project]) == str(installed)
    assert find_program("tool", exclude=[None, ""]) == str(in_project)  # no project named: allowed
    monkeypatch.chdir(project)  # the terminal UI and `lumi run` work in the project
    assert find_program("tool") == str(installed)


def test_a_link_into_the_project_does_not_count(tmp_path, monkeypatch, elsewhere):
    project = tmp_path / "project"
    _program(project / "bin", "tool" + EXE)
    installed = _program(tmp_path / "installed", "tool" + EXE)
    link = tmp_path / "linked"
    try:
        os.symlink(project / "bin", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account can't create symbolic links")
    monkeypatch.setenv("PATH", os.pathsep.join([str(link), str(installed.parent)]))
    assert find_program("tool", exclude=[project]) == str(installed)


def test_a_home_folder_project_keeps_the_persons_own_installs(tmp_path, monkeypatch, elsewhere):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    folder = home / "AppData" / "Local" / "Programs" / "Git" / "cmd"
    git = _program(folder, "git" + EXE)
    monkeypatch.setenv("PATH", str(folder))
    # The home folder holds per-user installs, not a repository's files.
    assert find_program("git", exclude=[home]) == str(git)
    assert find_program("git", exclude=[tmp_path]) == str(git)  # nor does a folder above it count
    assert find_program("git", exclude=[home / "AppData" / "Local" / "Programs"]) is None


@pytest.mark.skipif(not WINDOWS, reason="Windows file names")
def test_windows_names_are_what_create_process_starts(tmp_path, monkeypatch, elsewhere):
    folder = tmp_path / "bin"
    for name in ("tool.cmd", "tool.bat", "other.exe", "script.vbs", "script.py"):
        _program(folder, name)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.PY")
    assert find_program("tool") is None  # like CreateProcess: .exe only, unless batch files are wanted
    assert find_program("tool", scripts=True) == str(folder / "tool.bat")  # PATHEXT's order
    assert find_program("tool.cmd", scripts=True) == str(folder / "tool.cmd")
    assert find_program("tool.cmd") is None
    assert find_program("other") == str(folder / "other.exe")
    assert find_program("script", scripts=True) is None  # needs another program to run it


def test_names_and_paths(tmp_path, elsewhere):
    tool = _program(tmp_path / "bin", "tool" + EXE)
    assert find_program(str(tool)) == str(tool)
    assert find_program(str(tmp_path / ("missing" + EXE))) is None
    for refused in ("", "a\0b", os.path.join("bin", "tool" + EXE), os.path.join(".", "tool" + EXE)):
        assert find_program(refused) is None
    with pytest.raises(executables.ProgramNotFound):
        executables.program("lumi-no-such-program")
    assert issubclass(executables.ProgramNotFound, FileNotFoundError)


def test_the_resolver_remembers_only_what_still_exists(tmp_path, monkeypatch, elsewhere):
    first = _program(tmp_path / "first", "tool" + EXE)
    second = _program(tmp_path / "second", "tool" + EXE)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first.parent), str(second.parent)]))
    assert find_program("tool") == str(first)
    first.unlink()
    assert find_program("tool") == str(second)


@pytest.mark.skipif(not WINDOWS, reason="Windows folders")
def test_system_programs_come_from_the_systems_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(executables, "_folders", {})
    # The environment names these folders too, but a person can point it anywhere.
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("windir", str(tmp_path))
    system, windows = executables.system_folder(), executables.windows_folder()
    assert not system.startswith(str(tmp_path)) and os.path.isfile(os.path.join(system, "cmd.exe"))
    assert executables.system_program("explorer") == os.path.join(windows, "explorer.exe")
    assert executables.system_program("cmd") == os.path.join(system, "cmd.exe")
    assert executables.system_program("TASKKILL.EXE") == os.path.join(system, "taskkill.exe")
    assert executables.system_program("powershell") == os.path.join(system, "WindowsPowerShell", "v1.0",
                                                                     "powershell.exe")


@pytest.mark.skipif(WINDOWS, reason="POSIX folders")
def test_posix_system_programs_come_from_the_systems_folders(tmp_path, monkeypatch):
    _program(tmp_path, "sh")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(tmp_path)]))
    assert executables.system_program("sh") in ("/bin/sh", "/usr/bin/sh")


def test_system_programs_are_names_only():
    for name in ("", "a/b", "a\\b", "..", ".hidden", "C:x", "a\0b", "a b"):
        with pytest.raises(ValueError):
            executables.system_program(name)


def test_the_persons_own_setting_goes_to_the_commands_they_run(monkeypatch):
    name, marker = executables.NO_CURRENT_FOLDER, executables.PERSON_SETTING
    base = {"PATH": "x", name: "1", name.lower(): "1", marker: "unset"}
    monkeypatch.setenv(marker, "unset")
    assert person_environment(base) == {"PATH": "x"}
    monkeypatch.setenv(marker, "set:yes")
    assert person_environment(base) == {"PATH": "x", name: "yes"}
    monkeypatch.delenv(marker)  # a process that never hardened itself changes nothing
    assert person_environment(base) == base


@pytest.mark.skipif(not WINDOWS, reason="only Windows looks in the working folder")
def test_hardening_records_the_persons_setting_once(monkeypatch):
    name, marker = executables.NO_CURRENT_FOLDER, executables.PERSON_SETTING
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(marker, raising=False)
    executables.harden_process()
    assert (os.environ[name], os.environ[marker]) == ("1", "unset")
    executables.harden_process()  # again, as a child Lumi process inheriting it does
    assert os.environ[marker] == "unset"
    monkeypatch.delenv(marker)
    monkeypatch.setenv(name, "on")
    executables.harden_process()
    assert (os.environ[name], os.environ[marker]) == ("1", "set:on")


def test_configured_programs(tmp_path, monkeypatch, elsewhere):
    project = tmp_path / "project"
    server = _program(project / "tools", "server" + EXE)
    _program(project, "pytest" + EXE)
    relative = os.path.join("tools", "server")
    assert configured_program(relative, folder=project) == str(server)  # .exe added as CreateProcess would
    assert configured_program(relative) is None
    assert configured_program(str(server)) == str(server)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    assert configured_program("pytest", folder=project) is None  # a bare name never means the project's
    if WINDOWS:
        assert configured_program("C:" + relative, folder=project) is None


def test_commands_the_model_asks_for_run_from_their_folder(tmp_path):
    project = tmp_path / "project"
    script = _program(project, "gradlew.bat" if WINDOWS else "gradlew")
    built = _program(project / "build", "app" + EXE)
    command = ["gradlew.bat" if WINDOWS else "gradlew", "bootRun"]
    elsewhere = os.path.join("build", "app" + EXE)
    if WINDOWS:
        # CreateProcess looks in Lumi's working folder, never the command's: these run from the project.
        assert project_command(command, project) == [str(script), "bootRun"]
        assert project_command([elsewhere], project) == [str(built)]
        assert project_command(["notepad"], project) == ["notepad"]  # not the project's: left to Windows
    else:
        assert project_command(command, project) == command  # execvp looks relative to cwd itself
        assert project_command([elsewhere], project) == [elsewhere]
    assert project_command([str(built), "-v"], project) == [str(built), "-v"]


def test_leaving_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(executables, "_launch_directory", None)
    assert executables.launch_directory() == str(tmp_path)
    assert executables.leave_working_folder() == str(tmp_path)
    assert os.getcwd() == (executables.system_folder() if WINDOWS else "/")
    assert executables.launch_directory() == str(tmp_path)  # where Lumi started, still


def test_only_web_addresses_open_in_the_browser():
    for address in ("file:///C:/Windows/notepad.exe", "calc", "C:\\Windows\\notepad.exe", "ms-settings:",
                    "https://example.com/ with space"):
        with pytest.raises(ValueError):
            executables.open_url(address)


def test_show_in_folder_needs_a_full_path(tmp_path):
    with pytest.raises(ValueError):
        executables.show_in_folder_command("notes.txt")
    if sys.platform.startswith("linux") and not find_program("xdg-open"):
        pytest.skip("no file manager opener here")
    argv = executables.show_in_folder_command(tmp_path / "notes.txt")
    assert os.path.isabs(argv[0]) and argv[-1] in (str(tmp_path / "notes.txt"), str(tmp_path))
