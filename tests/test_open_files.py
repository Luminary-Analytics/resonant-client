"""Opening files and applications never runs a file by accident (lumi/executables.py).

Clicking a file the agent changed opens a document with its own program; a
file that opening would run (a program, script, shortcut or installer) isn't
opened: the page names it and its type and offers to show it in its folder.
The model's open_application takes installed applications and a short list
of address kinds. The scripts here only append their name to a marker file
outside the project.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import executables

WINDOWS = sys.platform == "win32"


def _page(project: Path, command: str, msg: dict) -> list[dict]:
    from lumi.gui import ws_commands

    sent: list[dict] = []

    async def send_json(payload):
        sent.append(payload)

    state = SimpleNamespace(project=SimpleNamespace(project_path=str(project), current_session=None))
    context = ws_commands.CommandContext(ws=SimpleNamespace(send_json=send_json), state=state, msg=msg, runs=None)
    asyncio.run(ws_commands.HANDLERS[command](context))
    return sent


def _ran(marker: Path, seconds: float) -> str:
    """What ran, waiting as long as a started script would take to say so."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    return marker.read_text(encoding="utf-8", errors="replace") if marker.exists() else ""


@pytest.mark.parametrize("name", ["readme-notes.bat", "setup.cmd"] if WINDOWS else ["build.sh"])
def test_clicking_a_changed_script_asks_to_show_it_instead_of_running_it(tmp_path, name):
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "ran.txt"
    script = project / name
    if WINDOWS:
        script.write_text(f'@echo off\r\necho {name}>>"{marker}"\r\nexit\r\n', encoding="ascii")
    else:
        script.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\n', encoding="ascii")
        script.chmod(0o755)
    sent = _page(project, "open_workspace_path", {"path": name})
    assert sent[-1] == {"event": "workspace_path_runs", "path": name, "name": name,
                        "kind": executables.opens_as_program(script)}
    assert "batch file" in sent[-1]["kind"] if WINDOWS else sent[-1]["kind"]
    assert _ran(marker, 3 if WINDOWS else 0.5) == ""


def test_showing_a_file_in_its_folder_after_the_person_confirms(tmp_path, monkeypatch):
    from lumi.gui import ws_commands

    project = tmp_path / "project"
    (project / "tools").mkdir(parents=True)
    (project / "tools" / "setup.cmd").write_text("@echo off\r\n", encoding="ascii")
    shown = []
    monkeypatch.setattr(ws_commands, "show_in_folder", shown.append)  # Explorer itself is the system's
    sent = _page(project, "reveal_workspace_path", {"path": "tools/setup.cmd"})
    assert shown == [(project / "tools" / "setup.cmd").resolve()]
    assert sent[-1]["message"] == "Showing setup.cmd in its folder"
    outside = tmp_path / "elsewhere.cmd"
    outside.write_text("x", encoding="ascii")
    sent = _page(project, "reveal_workspace_path", {"path": str(outside)})
    assert shown == [(project / "tools" / "setup.cmd").resolve()] and "outside the active project" in sent[-1]["message"]


def test_documents_still_open_with_their_program(tmp_path, monkeypatch):
    from lumi.gui import ws_commands

    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("# notes\n", encoding="utf-8")
    opened = []
    monkeypatch.setattr(ws_commands, "open_path", opened.append)
    sent = _page(project, "open_workspace_path", {"path": "notes.md"})
    assert opened == [(project / "notes.md").resolve()] and sent[-1]["message"] == "Opened notes.md"


# ── open_application ────────────────────────────────────────────────────────


@pytest.mark.skipif(not WINDOWS, reason="ShellExecute")
def test_open_application_takes_applications_and_a_few_kinds_of_address(tmp_path):
    from lumi.engine.computer_use import _windows_application

    notepad = _windows_application("notepad")
    assert executables.is_absolute(notepad) and os.path.basename(notepad).lower() == "notepad.exe"
    for address in ("https://example.com/", "mailto:someone@example.com", "ms-settings:display",
                    "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"):
        assert _windows_application(address) == address
    document = tmp_path / "notes.txt"
    document.write_text("x", encoding="utf-8")
    assert _windows_application(str(document)) == str(document)


@pytest.mark.skipif(not WINDOWS, reason="ShellExecute")
@pytest.mark.parametrize("name", [
    "file:///C:/Temp/x.bat", "search-ms:query=x", "ms-msdt:/id x", "shell:startup", "javascript:alert(1)",
    "\\\\server\\share\\x.exe", "//server/share/notes.txt", "..\\x.exe", "bin/tool",
])
def test_open_application_refuses_other_addresses_and_shares(name):
    from lumi.engine.computer_use import _windows_application

    with pytest.raises(ValueError):
        _windows_application(name)


@pytest.mark.skipif(not WINDOWS, reason="ShellExecute")
def test_open_application_never_runs_a_file(tmp_path):
    from lumi.engine.computer_use import exec_open_application

    marker = tmp_path / "ran.txt"
    for name in ("setup.cmd", "run.bat"):
        script = tmp_path / name
        script.write_text(f'@echo off\r\necho {name}>>"{marker}"\r\nexit\r\n', encoding="ascii")
        result = exec_open_application({"name": str(script)}, start=time.time())
        assert result.is_error and "would run it" in result.output
    assert _ran(marker, 3) == ""


@pytest.mark.skipif(WINDOWS, reason="macOS and Linux")
def test_open_application_takes_names_only_elsewhere(tmp_path):
    from lumi.engine.computer_use import exec_open_application

    script = tmp_path / "tool.sh"
    script.write_text("#!/bin/sh\n", encoding="ascii")
    script.chmod(0o755)
    result = exec_open_application({"name": str(script)}, start=time.time())
    assert result.is_error and "doesn't open paths" in result.output
