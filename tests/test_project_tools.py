"""A project's own tools (.venv, venv, node_modules/.bin) run only once it's trusted.

Automatic lint and tests, language servers and sprint mode's Python look in
a trusted project's own environments first (lumi/executables.py
``project_tool``) and say where each tool came from; an untrusted project's
copies never run, and the result says to trust the project. Real planted
programs that only append their name to a marker file outside the project.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_project_programs import plant

WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""
SCRIPTS = "Scripts" if WINDOWS else "bin"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Python project with its own ruff and pytest in .venv, and none on PATH."""
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    for name in ("ruff", "pytest", "python", "pylsp"):
        plant(root / ".venv" / SCRIPTS, name + EXE, marker)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    def ran() -> list[str]:
        return marker.read_text(encoding="utf-8").split() if marker.exists() else []

    return SimpleNamespace(root=root, ran=ran)


def test_automatic_lint_uses_a_trusted_projects_own_linter(project):
    from lumi.engine.lint import lint_file

    untrusted = lint_file(project.root, project.root / "app.py", trusted=False)
    assert project.ran() == []
    assert untrusted["skipped_reason"] == "ruff not installed"
    assert untrusted["notice"] == "The project's .venv has its own ruff; trust the project to use it."
    trusted = lint_file(project.root, project.root / "app.py", trusted=True, timeout=60)
    assert project.ran() == ["ruff" + EXE] and trusted["source"] == "from the project's .venv"


def test_automatic_tests_use_a_trusted_projects_own_runner(project):
    from lumi.engine.auto_test import run_tests_for_edit

    untrusted = run_tests_for_edit(project.root, project.root / "app.py", command="pytest -x", trusted=False)
    assert project.ran() == [] and untrusted["notice"].startswith("The project's .venv has its own pytest")
    trusted = run_tests_for_edit(project.root, project.root / "app.py", command="pytest -x", trusted=True)
    assert project.ran() == ["pytest" + EXE] and trusted["source"] == "from the project's .venv"


def test_language_servers_say_where_their_program_comes_from(project):
    from lumi.engine import lsp
    from lumi.gui.ws_commands import _lsp_list_payload

    class NoConfiguredServers:
        def get(self, section, key=None, default=None):
            return default

    found, source, notice = lsp.program_source("pylsp", str(project.root), trusted=True)
    assert Path(found).parent == project.root / ".venv" / SCRIPTS and source == "from the project's .venv"
    assert lsp.program_source("pylsp", str(project.root), trusted=False) == (
        None, "", "The project's .venv has its own pylsp; trust the project to use it.")
    _, argv = lsp.choose("app.py", NoConfiguredServers(), str(project.root), trusted=True)
    assert Path(argv[0]).parent == project.root / ".venv" / SCRIPTS
    with pytest.raises(lsp.LspError):
        lsp.choose("app.py", NoConfiguredServers(), str(project.root), trusted=False)
    listed = {server["id"]: server for server in _lsp_list_payload(
        project_path=str(project.root), settings=NoConfiguredServers(), trusted=False)["servers"]}
    assert "trust the project to use it" in listed["python-pylsp"]["detail"]
    listed = {server["id"]: server for server in _lsp_list_payload(
        project_path=str(project.root), settings=NoConfiguredServers(), trusted=True)["servers"]}
    assert listed["python-pylsp"]["program_source"] == "from the project's .venv"
    assert "Installed (from the project's .venv)" in listed["python-pylsp"]["detail"]
    assert project.ran() == []  # listing and choosing start nothing


def test_sprint_mode_uses_the_projects_python_only_when_trusted(project):
    from lumi.harness.prompts import HarnessPrompts

    for trusted, expected in ((False, Path(sys.executable).resolve()),
                              (True, project.root / ".venv" / SCRIPTS / ("python" + EXE))):
        app = SimpleNamespace(project=SimpleNamespace(project_path=str(project.root)),
                              project_trust=lambda path, trusted=trusted: SimpleNamespace(trusted=trusted))
        assert Path(HarnessPrompts(app)._preferred_harness_python(str(project.root))) == expected
    assert project.ran() == []
