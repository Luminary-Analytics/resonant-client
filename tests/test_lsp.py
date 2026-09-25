"""Language servers for the model (lumi/engine/lsp.py), against tests/fake_lsp_server.py."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lumi.engine import lsp
from lumi.engine.exclusions import ExclusionRules

FAKE = str(Path(__file__).with_name("fake_lsp_server.py"))


class Settings:
    def __init__(self, servers: dict):
        self.servers = servers

    def get(self, section, key=None, default=None):
        return self.servers if section == "lsp_servers" else default


def fake_settings(**extra) -> Settings:
    return Settings({"fake": {"command": [sys.executable, FAKE], "extensions": [".py"], **extra}})


@pytest.fixture(autouse=True)
def stop_servers():
    yield
    lsp.servers.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text(textwrap.dedent("""\
        class Greeter:
            def greet(self, name):
                return "hi " + name


        def main():
            greeter = Greeter()  # TODO_WARN
            print(greeter.greet("🙂"), Greeter)
        """), encoding="utf-8")
    (root / "other.py").write_text("from app import Greeter\n\nGreeter().greet('x')\n", encoding="utf-8")
    (root / "secret.py").write_text("from app import Greeter\nKEY = Greeter\n", encoding="utf-8")
    return str(root)


def ask(project, settings=None, *, trusted=True, exclusions=None, **arguments):
    return lsp.code_intel(arguments, project_path=project, settings=settings or fake_settings(),
                          exclusions=exclusions, trusted=trusted)


def test_servers_from_settings():
    settings = Settings({
        "quoted": {"command": '"C:\\tools\\my server.exe" --stdio' if os.name == "nt" else "'/opt/my server' --stdio",
                   "extensions": ["py", ".PYI"]},
        "pyright": {"command": "pyright-langserver --stdio"},
        "by-language": {"command": ["serve-go"], "languages": ["go"]},
        "unknown": {"command": "mystery-server"},
        "off": {"command": "gopls", "enabled": False},
        "empty": {"command": ""},
    })
    specs = {spec.id: (spec, enabled) for spec, enabled in lsp.configured(settings)}
    quoted = specs["configured:quoted"][0]
    assert quoted.command[0].endswith(("my server.exe", "my server")) and quoted.command[1:] == ("--stdio",)
    assert quoted.languages == {".py": "python", ".pyi": "python"}
    # A well-known program serves that program's file types.
    assert specs["configured:pyright"][0].languages[".py"] == "python"
    assert specs["configured:by-language"][0].languages == {".go": "go"}
    assert specs["configured:unknown"][0].languages == {}
    assert specs["configured:off"][1] is False and "configured:empty" not in specs


def test_choosing_a_server(monkeypatch):
    monkeypatch.setattr(lsp.shutil, "which", lambda program: f"/bin/{program}" if program == "pylsp" else None)
    spec, argv = lsp.choose("a.py", Settings({}))
    # Pyright isn't installed, so the next known Python server is used.
    assert (spec.id, argv) == ("python-pylsp", ["/bin/pylsp"])
    spec, _ = lsp.choose("a.py", fake_settings())
    assert spec.id == "configured:fake"  # Settings first
    spec, _ = lsp.choose("a.py", fake_settings(enabled=False))
    assert spec.id == "python-pylsp"
    with pytest.raises(lsp.LspError, match="Install gopls"):
        lsp.choose("main.go", Settings({}))
    with pytest.raises(lsp.LspError, match="Name a server"):
        lsp.choose("notes.xyz", Settings({}))


def test_positions_count_utf16_code_units():
    text = "x = 1\nprint('🙂', value, values)\n"
    # The emoji is two UTF-16 units, so "value" (character 11) starts at unit 12.
    assert lsp.position(text, 2, symbol="value") == {"line": 1, "character": 12}
    assert lsp.position(text, 2, symbol="values") == {"line": 1, "character": 19}
    assert lsp.position(text, 2, column=1) == {"line": 1, "character": 0}
    assert lsp.position("    return x\n", 1) == {"line": 0, "character": 4}  # first non-space
    with pytest.raises(lsp.LspError, match="isn't on line 1"):
        lsp.position(text, 1, symbol="value")
    with pytest.raises(lsp.LspError, match="from 1 to 3"):
        lsp.position(text, 9)
    assert lsp._from_utf16("print('🙂', value)", 13) == 12


def test_uris():
    if os.name == "nt":
        assert lsp.uri_to_path("file:///c%3A/Users/me/a%20b.py") == "C:\\Users\\me\\a b.py"
        assert lsp.uri_to_path("file:///C:/Users/me/a.py") == "C:\\Users\\me\\a.py"
    else:
        assert lsp.uri_to_path("file:///home/me/a%20b.py") == "/home/me/a b.py"
    path = os.path.abspath("some dir/x.py")
    assert os.path.normcase(lsp.uri_to_path(lsp.path_to_uri(path))) == os.path.normcase(path)


def test_definition_references_hover_and_symbols(project):
    found = ask(project, action="definition", path="app.py", line=7, symbol="Greeter")
    assert found[0] == "app.py:1:7  class Greeter:" and found[1]["server"] == "fake" and found[1]["count"] == 1

    # The line with the emoji: columns are characters again in the answer.
    text, meta = ask(project, action="references", path="app.py", line=8, symbol="Greeter")
    lines = text.splitlines()
    assert "app.py:8:31  print(greeter.greet(\"🙂\"), Greeter)" in lines
    assert "other.py:1:17  from app import Greeter" in lines
    assert meta["count"] == 7  # three in app.py, two each in other.py and secret.py
    exclusions = ExclusionRules.for_project(project, settings_patterns=["secret.py"])
    hidden, meta = ask(project, exclusions=exclusions, action="references", path="app.py", line=7, symbol="Greeter")
    assert "secret.py" not in hidden and hidden.endswith("(2 in excluded files not shown)")
    assert meta["count"] == 5

    hover, _ = ask(project, action="hover", path="app.py", line=2, symbol="greet")
    assert hover == "```python\ngreet\n```\nDocs for greet."
    outline, meta = ask(project, action="symbols", path="app.py")
    assert outline.splitlines() == ["class Greeter — line 1", "  method greet — line 2", "function main — line 6"]
    assert meta["count"] == 3


def test_diagnostics_follow_the_file(project):
    text, meta = ask(project, action="diagnostics", path="app.py")
    assert text.splitlines() == ["app.py: 1 warning (fake)", "7:28 warning: TODO_WARN found [fake todo_warn]"]
    assert meta["counts"] == {"warning": 1}
    # Unchanged: the same answer without waiting for the server again.
    assert ask(project, action="diagnostics", path="app.py")[0] == text
    # Changed on disk: the server gets the new text.
    path = Path(project, "app.py")
    path.write_text(path.read_text(encoding="utf-8").replace("TODO_WARN", "TODO_ERROR"), encoding="utf-8")
    assert ask(project, action="diagnostics", path="app.py")[0].splitlines()[0] == "app.py: 1 error (fake)"
    assert ask(project, action="diagnostics", path="other.py")[0] == "fake reports no problems in other.py."


def test_pull_diagnostics(project, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_PULL", "1")
    monkeypatch.setenv("FAKE_LSP_SILENT", "1")  # so only the pull can answer
    text, _ = ask(project, action="diagnostics", path="app.py")
    assert text.splitlines()[1] == "7:28 warning: TODO_WARN found [fake todo_warn]"


def test_a_server_that_says_nothing_isnt_taken_for_a_clean_file(project, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_SILENT", "1")
    monkeypatch.setattr(lsp, "DIAGNOSTIC_SECONDS", 1.0)
    text, _ = ask(project, action="diagnostics", path="app.py")
    assert "reported nothing" in text and "doesn't mean the file has no problems" in text


def test_only_trusted_projects_start_servers(project):
    with pytest.raises(lsp.LspError, match="trusted projects"):
        ask(project, trusted=False, action="symbols", path="app.py")
    assert lsp.servers.status(project) == {}


def test_a_server_that_fails_is_reported_and_not_restarted_at_once(project, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_CRASH", "1")
    with pytest.raises(lsp.LspError, match="didn't start") as failure:
        ask(project, action="symbols", path="app.py")
    assert "configuration is broken" in str(failure.value)
    assert lsp.servers.status(project)["configured:fake"]["state"] == "failed"
    monkeypatch.delenv("FAKE_LSP_CRASH")
    with pytest.raises(lsp.LspError, match="didn't start"):  # within RETRY_SECONDS
        ask(project, action="symbols", path="app.py")
    monkeypatch.setattr(lsp, "RETRY_SECONDS", 0)
    assert ask(project, action="symbols", path="app.py")[1]["count"] == 3


def test_servers_are_kept_stopped_when_idle_and_restarted(project):
    ask(project, action="symbols", path="app.py")
    server = lsp.servers.server_for(project, os.path.join(project, "app.py"), fake_settings())
    assert lsp.servers.status(project) == {"configured:fake": {"state": "running", "error": ""}}
    assert lsp.servers.stop_idle(-1) == 1
    assert server.state == "stopped" and not server.alive
    assert lsp.servers.status(project) == {}
    assert ask(project, action="symbols", path="app.py")[1]["count"] == 3


def test_bad_calls(project):
    with pytest.raises(lsp.LspError, match="Choose an action"):
        ask(project, action="rename", path="app.py")
    with pytest.raises(lsp.LspError, match="isn't a file"):
        ask(project, action="symbols", path="missing.py")
    with pytest.raises(lsp.LspError, match="needs the line"):
        ask(project, action="definition", path="app.py", symbol="Greeter")


def test_the_tool_through_the_session(project):
    from lumi.engine.sandbox import READ_ONLY_TOOLS, PathSandbox
    from lumi.engine.session import Session, ToolBoundaryViolation
    from lumi.engine.tools import execute_tool

    assert "code_intel" in READ_ONLY_TOOLS
    session = Session.__new__(Session)
    session.project_path = project
    session.sandbox = PathSandbox(project, enabled=True)
    session.exclusions = ExclusionRules.for_project(project, settings_patterns=["secret.py"])
    session.computer_use_enabled = True
    session._allowed_tools = None
    prepared = session._prepare_workspace_tool_args("code_intel", {"action": "symbols", "path": "app.py"})
    assert os.path.normcase(prepared["path"]) == os.path.normcase(os.path.join(project, "app.py"))
    with pytest.raises(ToolBoundaryViolation, match="excluded"):
        session._prepare_workspace_tool_args("code_intel", {"action": "symbols", "path": "secret.py"})
    with pytest.raises(Exception):
        session._prepare_workspace_tool_args("code_intel", {"action": "symbols", "path": "../outside.py"})

    result = execute_tool("code_intel", prepared, project_path=project, settings=fake_settings(),
                          sandbox_roots=[project], project_trusted=True)
    assert not result.is_error and result.metadata["code_intel"]["count"] == 3
    refused = execute_tool("code_intel", prepared, project_path=project, settings=fake_settings(),
                           sandbox_roots=[project])
    assert refused.is_error and "trusted projects" in refused.output


def test_stopping_ends_the_process(project):
    ask(project, action="symbols", path="app.py")
    server = lsp.servers.server_for(project, os.path.join(project, "app.py"), fake_settings())
    process = server._process
    lsp.servers.close()
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is not None and server.state == "stopped"


def test_the_inventory_uses_the_same_servers(project, monkeypatch):
    from lumi.gui.ws_commands import _lsp_list_payload

    monkeypatch.setattr(lsp.shutil, "which", lambda program: None)
    payload = _lsp_list_payload(project_path=project, settings=fake_settings())
    first = payload["servers"][0]
    assert (first["id"], first["source"], first["status"]) == ("configured:fake", "configured", "available")
    ask(project, action="symbols", path="app.py")
    payload = _lsp_list_payload(project_path=project, settings=fake_settings())
    assert payload["servers"][0]["status"] == "running"
    python = next(item for item in payload["servers"] if item["id"] == "python-pyright")
    assert python["status"] == "missing" and "pyright-langserver" in python["detail"]
