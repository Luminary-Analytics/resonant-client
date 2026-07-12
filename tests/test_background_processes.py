import subprocess
from pathlib import Path

from resonant_client import processes


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


def test_main_tool_runner_combines_hidden_window_and_process_group_policy():
    source = (Path(__file__).parents[1] / "resonant_client" / "engine" / "tools.py").read_text(
        encoding="utf-8"
    )
    assert "background_process_kwargs(new_process_group=True)" in source
