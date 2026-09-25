"""The shell sandbox (lumi/engine/os_sandbox.py)."""

from __future__ import annotations

import os
import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from lumi.engine import os_sandbox, tools
from lumi.engine.os_sandbox import Availability

# Checked once, without the module's cache: a real sandbox on macOS or Linux.
HERE = os_sandbox._probe()
live = pytest.mark.skipif(not HERE.available, reason=f"no sandbox on this computer: {HERE.reason}")


class _Settings:
    def __init__(self, value):
        self.value = value

    def get(self, section, key, default=None):
        return self.value if (section, key) == ("security", "shell_sandbox") else default


@pytest.fixture(autouse=True)
def _reset():
    yield
    os_sandbox.set_for_tests("off", None)


def _refuse_to_run(monkeypatch):
    monkeypatch.setattr(tools, "_run_subprocess_with_cancel", lambda *a, **k: pytest.fail("the command ran"))


def test_the_setting_picks_the_mode(monkeypatch):
    monkeypatch.setattr(os_sandbox, "_probe_soon", lambda: None)
    assert os_sandbox.configure(_Settings("project")) == "project"
    assert os_sandbox.configure(_Settings("everywhere")) == "off"  # unknown values turn it off
    assert os_sandbox.configure(None) == "off"


def test_settings_never_wait_for_the_check(monkeypatch):
    started = []
    monkeypatch.setattr(os_sandbox, "_probe_soon", lambda: started.append(True))
    os_sandbox.set_for_tests("project", None)
    assert os_sandbox.status() == {"mode": "project", "available": None, "kind": "", "reason": ""}
    assert started  # and the check was started
    os_sandbox.set_for_tests("project", Availability(False, reason="Not here."))
    assert os_sandbox.status()["reason"] == "Not here."


def test_what_the_check_finds_without_a_sandbox(monkeypatch):
    monkeypatch.setattr(os_sandbox.sys, "platform", "win32")
    assert "Windows" in os_sandbox._probe().reason
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: None)
    info = os_sandbox._probe()
    assert not info.available and "bubblewrap" in info.reason


def test_off_runs_commands_as_before(tmp_path):
    result = tools.execute_tool("bash", {"command": "echo hi", "cwd": str(tmp_path)}, project_path=str(tmp_path))
    assert result.output == "hi" and result.metadata["sandboxed"] is False


def test_on_without_a_sandbox_nothing_runs(tmp_path, monkeypatch):
    os_sandbox.set_for_tests("project", Availability(False, reason="Not here."))
    _refuse_to_run(monkeypatch)
    for name, args in [("bash", {"command": "echo hi", "cwd": str(tmp_path)}),
                       ("check_run", {"command": "echo hi", "requirement": "r"})]:
        result = tools.execute_tool(name, args, project_path=str(tmp_path))
        assert result.is_error and "Not here." in result.output, name
        assert result.metadata["not_executed"] is True

    started = []
    monkeypatch.setattr(os_sandbox.subprocess, "Popen", lambda *a, **k: started.append(a) or pytest.fail("started"))
    result = tools.execute_tool("job_start", {"command": ["python", "-V"]}, project_path=str(tmp_path))
    assert result.is_error and "Not here." in result.output
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = tools.execute_tool("preview_start", {"command": ["python", "-m", "http.server", str(port)],
                                                  "url": f"http://127.0.0.1:{port}/"}, project_path=str(tmp_path))
    assert result.is_error and "Not here." in result.output
    assert started == []


def test_settings_offer_the_two_modes(tmp_path, monkeypatch):
    from lumi.gui.settings import DEFAULTS
    from lumi.gui.ws_commands import _SOCKET_SETTING_KEYS, _socket_setting_value

    assert DEFAULTS["security"]["shell_sandbox"] == "off" and "shell_sandbox" in _SOCKET_SETTING_KEYS["security"]
    assert _socket_setting_value("security", "shell_sandbox", "project") == "project"
    for bad in ("strict", True, None):
        with pytest.raises(ValueError, match="off or project"):
            _socket_setting_value("security", "shell_sandbox", bad)
    with pytest.raises(ValueError, match="on or off"):  # the other switches are still switches
        _socket_setting_value("security", "computer_use", "project")


def _triples(argv):
    return list(zip(argv, argv[1:], argv[2:]))


def test_bubblewrap_makes_everything_read_only_but_the_allowed_folders(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    os_sandbox.set_for_tests("project", Availability(True, "bubblewrap", "/usr/bin/bwrap"))
    argv = os_sandbox.prepare_shell("make test", roots=[str(project)], cwd=str(project))
    assert argv[0] == "/usr/bin/bwrap" and argv[-3:] == ["/bin/sh", "-c", "make test"]
    real, git = os.path.realpath(project), os.path.realpath(project / ".git")
    triples = _triples(argv)
    assert ("--ro-bind", "/", "/") in triples
    assert ("--bind", real, real) in triples
    temp = os.path.realpath(tempfile.gettempdir())
    assert ("--bind", temp, temp) in triples
    # .git is mounted read-only after the project is mounted writable.
    assert triples.index(("--ro-bind", git, git)) > triples.index(("--bind", real, real))
    assert ("--chdir", str(project)) in list(zip(argv, argv[1:]))

    jobs = os_sandbox.prepare_argv(["blender", "-b"], roots=[str(project)], cwd=str(project))
    assert jobs[0] == "/usr/bin/bwrap" and jobs[-2:] == ["blender", "-b"]


def test_seatbelt_allows_writing_only_to_the_allowed_folders(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    os_sandbox.set_for_tests("project", Availability(True, "seatbelt", "/usr/bin/sandbox-exec"))
    argv = os_sandbox.prepare_shell("make test", roots=[str(project)], cwd=str(project))
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and argv[-3:] == ["/bin/sh", "-c", "make test"]
    profile = argv[2]
    assert "(allow default)\n(deny file-write*)\n(allow file-write*" in profile
    quoted = os_sandbox._seatbelt_string
    assert f"(subpath {quoted(os.path.realpath(project))})" in profile
    # .git is excluded from every allowed folder, the temporary one included.
    assert f"(require-not (subpath {quoted(os.path.realpath(project / '.git'))}))" in profile
    assert quoted('a"b\\c') == '"a\\"b\\\\c"'


def test_the_session_passes_its_writable_folders(tmp_path):
    from lumi.engine.sandbox import PathSandbox
    from lumi.engine.session import Session

    session = Session.__new__(Session)
    session.sandbox = PathSandbox(str(tmp_path), allowed_dirs=[str(tmp_path / "shared")], enabled=True)
    session.project_path = str(tmp_path / "sub")
    assert session._sandbox_roots() == [session.sandbox.project_path, *session.sandbox.allowed_dirs]
    session.sandbox = None
    assert session._sandbox_roots() == [str(tmp_path / "sub")]


@live
def test_a_sandboxed_command_writes_in_the_project_and_nowhere_else(tmp_path):
    import pwd  # the live test runs only on macOS and Linux

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    # The account's real home: the suite points HOME at a folder under the
    # temporary folder, which the sandbox lets commands write to.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    outside = real_home / f".lumi-sandbox-test-{uuid.uuid4().hex}"
    in_temp = Path(tempfile.gettempdir()) / f"lumi-sandbox-test-{uuid.uuid4().hex}"
    os_sandbox.set_for_tests("project", HERE)

    def run(command):
        return tools.execute_tool("bash", {"command": command, "cwd": str(project)}, project_path=str(project),
                                  sandbox_roots=[str(project)])

    try:
        result = run("echo in > inside.txt")
        assert not result.is_error, result.output
        assert result.metadata["sandboxed"] is True
        assert (project / "inside.txt").read_text().strip() == "in"
        assert not run(f"echo t > '{in_temp}'").is_error and in_temp.exists()
        assert run(f"echo out > '{outside}'").is_error and not outside.exists()
        assert run("echo x > .git/hook-test").is_error and not (project / ".git" / "hook-test").exists()
        # Reading outside the project still works.
        assert not run(f"ls '{real_home}'").is_error
    finally:
        for path in (outside, in_temp):
            if path.exists():
                path.unlink()
