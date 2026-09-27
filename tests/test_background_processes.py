import subprocess
import sys

import pytest

from lumi import processes


def test_background_process_kwargs_are_empty_on_non_windows(monkeypatch):
    monkeypatch.setattr(processes.sys, "platform", "linux")
    assert processes.background_process_kwargs() == {}
    assert processes.background_process_kwargs(new_process_group=True) == {
        "start_new_session": True,
    }


def test_background_process_kwargs_hide_windows_console(monkeypatch):
    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001, raising=False)
    monkeypatch.setattr(subprocess, "SW_HIDE", 0, raising=False)

    kwargs = processes.background_process_kwargs(new_process_group=True)

    assert kwargs["creationflags"] == 0x08000200
    assert kwargs["startupinfo"].dwFlags & 0x00000001
    assert kwargs["startupinfo"].wShowWindow == 0


@pytest.mark.parametrize("owned_group", [False, True])
def test_main_tool_runner_combines_hidden_window_and_process_group_policy(tmp_path, monkeypatch, owned_group):
    from lumi.engine import tools
    captured = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(tools.subprocess, "Popen", launch)
    result = tools._run_subprocess_with_cancel(
        [sys.executable, "-c", "print('Observed fixture child')"], timeout=10,
        shell=False, text=True, cwd=str(tmp_path), owned_process_group=owned_group)
    assert result == (0, "Observed fixture child\n", "", False)
    assert len(captured) == 1
    options = captured[0]
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert bool(options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP) is (not owned_group)
        assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE
    else:
        assert options.get("start_new_session", False) is (not owned_group)
    assert options["stdin"] == (subprocess.DEVNULL if owned_group else None)
