"""The code editor extensions (lumi/code_editors): the .vsix, JetBrains tools and ``lumi editor``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from lumi import code_editors
from lumi.code_editors import cli
from lumi.paths import state_home

VSIX = "{http://schemas.microsoft.com/developer/vsx-schema/2011}"


def test_the_vsix_holds_the_extension(tmp_path):
    target = code_editors.build_vsix(tmp_path / "out")
    manifest = code_editors.vscode_manifest()
    assert target.name == f"lumi-vscode-{manifest['version']}.vsix"
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert names == {"[Content_Types].xml", "extension.vsixmanifest",
                         *(f"extension/{name}" for name in code_editors.VSCODE_FILES)}
        assert json.loads(archive.read("extension/package.json")) == manifest
        package = ET.fromstring(archive.read("extension.vsixmanifest"))
        types = ET.fromstring(archive.read("[Content_Types].xml"))
    identity = package.find(f"{VSIX}Metadata/{VSIX}Identity")
    assert (identity.get("Id"), identity.get("Version"), identity.get("Publisher")) == (
        "lumi-vscode", manifest["version"], "luminary-analytics")
    engine = {item.get("Id"): item.get("Value") for item in package.iter(f"{VSIX}Property")}
    assert engine["Microsoft.VisualStudio.Code.Engine"] == manifest["engines"]["vscode"]
    assert package.find(f"{VSIX}Assets/{VSIX}Asset").get("Path") == "extension/package.json"
    assert {item.get("Extension") for item in types} >= {".json", ".js", ".md", ".txt", ".vsixmanifest"}


def test_the_extension_ships_with_every_kind_of_install():
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    assert "code_editors/vscode/*" in pyproject["tool"]["setuptools"]["package-data"]["lumi"]
    spec = (repo / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    assert all(f'"{name}"' in spec for name in code_editors.VSCODE_FILES)
    assert all((code_editors.VSCODE_DIR / name).is_file() for name in code_editors.VSCODE_FILES)
    policy = json.loads((repo / "packaging" / "bundle-policy.json").read_text(encoding="utf-8"))
    assert "_internal/lumi/code_editors/vscode/extension.js" in policy["required_globs"]


def test_installing_in_vs_code_uses_its_command_line(monkeypatch):
    calls = []

    def run(args, **kwargs):
        vsix = Path(args[2])
        with zipfile.ZipFile(vsix) as archive:  # the package exists while the editor reads it
            assert "extension/extension.js" in archive.namelist()
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "Extension 'lumi-vscode' was successfully installed.\n", "")

    monkeypatch.setattr(code_editors.shutil, "which", lambda name: f"/bin/{name}" if name != "codium" else None)
    monkeypatch.setattr(code_editors.subprocess, "run", run)
    assert "successfully installed" in code_editors.install_vscode("cursor")
    assert calls[0][0] == "/bin/cursor" and calls[0][1] == "--install-extension" and calls[0][3] == "--force"
    assert not Path(calls[0][2]).exists()  # a temporary package
    assert [item["command"] for item in code_editors.vscode_editors()] == ["code", "code-insiders", "cursor",
                                                                          "windsurf"]

    monkeypatch.setattr(code_editors.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "Failed: corrupt"))
    with pytest.raises(RuntimeError, match="corrupt"):
        code_editors.install_vscode()
    with pytest.raises(RuntimeError, match="isn't on PATH"):
        code_editors.install_vscode("codium")
    with pytest.raises(ValueError):
        code_editors.install_vscode("notepad")


def test_jetbrains_tools_run_lumi_editor(monkeypatch):
    tools = ET.fromstring(code_editors.jetbrains_tools_xml())
    assert tools.get("name") == "Lumi"
    options = [{option.get("name"): option.get("value") for option in tool.iter("option")} for tool in tools]
    assert [tool.get("name") for tool in tools] == ["Send File to Lumi", "Send Selection to Lumi",
                                                   "Ask Lumi About Selection", "Show Lumi's Changes"]
    assert all(item["COMMAND"] == sys.executable for item in options)
    assert options[1]["PARAMETERS"] == ('-m lumi editor send "$FilePath$" --lines '
                                        "$SelectionStartLine$-$SelectionEndLine$ --source JetBrains")
    assert options[3]["PARAMETERS"] == "-m lumi editor changes --diff"
    assert [tool.get("showConsoleOnStdOut") for tool in tools] == ["false", "false", "false", "true"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Lumi\lumi.exe")
    frozen = ET.fromstring(code_editors.jetbrains_tools_xml())
    first = {option.get("name"): option.get("value") for option in frozen[0].iter("option")}
    assert first == {"COMMAND": r"C:\Program Files\Lumi\lumi.exe",
                     "PARAMETERS": 'editor send "$FilePath$" --source JetBrains',
                     "WORKING_DIRECTORY": "$ProjectFileDir$"}


def test_jetbrains_settings_folders_are_found_and_given_the_tools(tmp_path):
    roaming = tmp_path / "Roaming"
    for name in ("PyCharm2025.2", "IntelliJIdea2026.1", "consentOptions", "Toolbox"):
        (roaming / "JetBrains" / name).mkdir(parents=True)
    (roaming / "JetBrains" / "stray2025.1.txt").write_text("", encoding="utf-8")
    found = code_editors.jetbrains_config_dirs({"APPDATA": str(roaming)}, tmp_path, "win32")
    assert [path.name for path in found] == ["IntelliJIdea2026.1", "PyCharm2025.2"]
    linux = tmp_path / "home"
    (linux / ".config" / "JetBrains" / "GoLand2025.3").mkdir(parents=True)
    assert [p.name for p in code_editors.jetbrains_config_dirs({}, linux, "linux")] == ["GoLand2025.3"]
    assert code_editors.jetbrains_config_dirs({}, tmp_path / "nobody", "darwin") == []

    written = code_editors.install_jetbrains(found)
    assert written == [path / "tools" / "Lumi.xml" for path in found]
    assert ET.fromstring(written[0].read_text(encoding="utf-8")).get("name") == "Lumi"


def test_line_ranges_from_jetbrains():
    assert cli.parse_lines("10-24") == (10, 24)
    assert cli.parse_lines("24-10") == (10, 24)
    assert cli.parse_lines("7") == (7, 7)
    assert cli.parse_lines("5-") == (5, 5)
    for empty in (None, "", "-", "$SelectionStartLine$-$SelectionEndLine$", "0-0"):
        assert cli.parse_lines(empty) is None


class _Lumi(BaseHTTPRequestHandler):
    """A stand-in for the app's /api/editor endpoints."""

    project = ""
    token = "bridge-token"
    received: list = []

    def log_message(self, *args):
        pass

    def _send(self, status: int, body, content_type: str = "application/json"):
        payload = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self):
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        type(self).received.append((self.command, parts.path, parse_qs(parts.query), body))
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(403, {"error": "Forbidden"})
        if parts.path == "/api/editor/status":
            return self._send(200, {"ok": True, "project": self.project, "version": "9.9", "window": False})
        if parts.path == "/api/editor/context":
            return self._send(200, {"ok": True, "attached": ["src/app.py lines 1-3"],
                                    "skipped": [{"path": "/elsewhere/x.py", "reason": "It isn't in the project."}]})
        if parts.path == "/api/editor/changes":
            return self._send(200, {"project": self.project, "more": 0, "turn": {
                "before": "cp_00001_aaaaaaaa", "prompt": "Rename things", "finished": False,
                "compared_with": "the snapshot taken before the turn's first change"}, "files": [
                {"path": "src/app.py", "status": "modified", "before_path": "src/app.py"},
                {"path": "src/new.py", "status": "added", "before_path": ""},
                {"path": "logo.png", "status": "modified", "before_path": "logo.png"}]})
        if parts.path == "/api/editor/before":
            name = parse_qs(parts.query)["path"][0]
            content = b"\x89PNG\0\0" if name == "logo.png" else b"one\ntwo\n"
            return self._send(200, content, "application/octet-stream")
        return self._send(409, {"error": "Lumi's window isn't open."})

    do_GET = do_POST = _handle


@pytest.fixture
def lumi(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_bytes(b"one\nTWO\n")
    (project / "src" / "new.py").write_bytes(b"print('new')")
    (project / "logo.png").write_bytes(b"\x89PNG\0\1")
    _Lumi.project, _Lumi.received = str(project), []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Lumi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = state_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "editor-bridge.json").write_text(json.dumps({
        "version": 1, "url": f"http://127.0.0.1:{server.server_port}", "token": _Lumi.token, "pid": os.getpid()}),
        encoding="utf-8")
    # A proxy in the environment must not see requests to this computer.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    yield project
    server.shutdown()
    server.server_close()


def test_the_command_line_talks_to_the_running_app(lumi, capsys):
    assert cli.main(["status"]) == 0
    assert "Lumi 9.9 is running with" in capsys.readouterr().out

    assert cli.main(["send", str(lumi / "src" / "app.py"), "--lines", "3-1", "--text", "Why?"]) == 0
    out, err = capsys.readouterr()
    assert "Added to your message in Lumi: src/app.py lines 1-3. Send it from Lumi." in out
    assert "Not added: /elsewhere/x.py" in err
    method, route, _, body = _Lumi.received[-1]
    assert (method, route) == ("POST", "/api/editor/context")
    assert body == {"items": [{"path": str((lumi / "src" / "app.py").resolve()), "start_line": 1, "end_line": 3}],
                    "text": "Why?", "source": "the command line"}

    assert cli.main(["changes", "--diff"]) == 0
    out = capsys.readouterr().out
    assert "Lumi's changes (still running): Rename things" in out
    assert "  changed  src/app.py" in out and "  new      src/new.py" in out
    assert "-two\n+TWO\n" in out
    assert "+print('new')\n\\ No newline at end of file\n" in out
    assert "Binary file logo.png changed." in out
    befores = [query for method, route, query, _ in _Lumi.received if route == "/api/editor/before"]
    assert {query["path"][0] for query in befores} == {"src/app.py", "logo.png"}  # nothing before a new file

    assert cli.main(["diff", str(lumi / "src" / "app.py")]) == 0
    assert "--- a/src/app.py" in capsys.readouterr().out
    assert cli.main(["diff", str(lumi / "unchanged.txt")]) == 0
    assert "didn't change unchanged.txt" in capsys.readouterr().out
    assert cli.main(["diff", str(lumi.parent / "elsewhere.py")]) == 1
    assert "isn't in the project" in capsys.readouterr().err


def test_the_command_line_explains_failures(lumi, capsys):
    bridge_file = state_home() / "editor-bridge.json"
    saved = json.loads(bridge_file.read_text(encoding="utf-8"))
    bridge_file.write_text(json.dumps({**saved, "token": "stale"}), encoding="utf-8")
    assert cli.main(["status"]) == 1
    assert "Forbidden" in capsys.readouterr().err
    bridge_file.write_text(json.dumps({**saved, "url": "http://example.com:80"}), encoding="utf-8")
    assert cli.main(["status"]) == 1
    assert "doesn't point at this computer" in capsys.readouterr().err
    bridge_file.unlink()
    assert cli.main(["send", str(lumi / "src" / "app.py")]) == 1
    assert cli.NOT_RUNNING in capsys.readouterr().err


def test_the_command_line_packs_and_installs(tmp_path, monkeypatch, capsys):
    assert cli.main(["vscode", "--out", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert Path(out.strip()).is_file() and "code --install-extension" in err
    monkeypatch.setattr(cli, "install_vscode", lambda editor: f"installed with {editor}")
    assert cli.main(["vscode", "--install", "--editor", "cursor"]) == 0
    assert "installed with cursor" in capsys.readouterr().out

    assert cli.main(["jetbrains"]) == 0
    assert ET.fromstring(capsys.readouterr().out).get("name") == "Lumi"
    target = tmp_path / "Lumi.xml"
    assert cli.main(["jetbrains", "--out", str(target)]) == 0 and target.is_file()
    monkeypatch.setattr(cli, "install_jetbrains", lambda: [])
    assert cli.main(["jetbrains", "--install"]) == 1
    assert "No JetBrains IDE settings" in capsys.readouterr().err
