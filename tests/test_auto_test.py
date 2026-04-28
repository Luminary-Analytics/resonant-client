"""
Tests for resonant_client/engine/auto_test.py
  - find_test_target heuristics
  - run_tests_for_edit (uses pytest as the runner)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from resonant_client.engine import auto_test


# ── find_test_target ───────────────────────────────────────────────────


class TestFindTestTarget:
    def test_no_target(self, tmp_path):
        f = tmp_path / "lonely.py"
        f.write_text("x = 1\n", encoding="utf-8")
        assert auto_test.find_test_target(tmp_path, f) is None

    def test_tests_dir_test_prefix(self, tmp_path):
        src = tmp_path / "foo.py"
        src.write_text("def add(a, b): return a + b\n", encoding="utf-8")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        target = tests_dir / "test_foo.py"
        target.write_text("def test_x(): pass\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, src)
        assert found is not None
        assert found.resolve() == target.resolve()

    def test_adjacent_test(self, tmp_path):
        src = tmp_path / "foo.py"
        src.write_text("x = 1\n", encoding="utf-8")
        adj = tmp_path / "test_foo.py"
        adj.write_text("def test_x(): pass\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, src)
        assert found.resolve() == adj.resolve()

    def test_adjacent_underscore_test(self, tmp_path):
        src = tmp_path / "foo.py"
        src.write_text("x = 1\n", encoding="utf-8")
        adj = tmp_path / "foo_test.py"
        adj.write_text("def test_x(): pass\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, src)
        assert found.resolve() == adj.resolve()

    def test_test_file_returns_itself(self, tmp_path):
        # If user edits the test file, run that file
        f = tmp_path / "test_foo.py"
        f.write_text("def test_x(): pass\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, f)
        assert found.resolve() == f.resolve()

    def test_js_test_ts(self, tmp_path):
        src = tmp_path / "foo.ts"
        src.write_text("export const a = 1;\n", encoding="utf-8")
        adj = tmp_path / "foo.test.ts"
        adj.write_text("test('x', () => {});\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, src)
        assert found.resolve() == adj.resolve()

    def test_js_spec_jsx(self, tmp_path):
        src = tmp_path / "Btn.jsx"
        src.write_text("export default () => null;\n", encoding="utf-8")
        adj = tmp_path / "Btn.spec.jsx"
        adj.write_text("test('x', () => {});\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, src)
        assert found.resolve() == adj.resolve()

    def test_mirrored_layout(self, tmp_path):
        # pkg/mod.py → tests/pkg/test_mod.py
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
        tests_pkg = tmp_path / "tests" / "pkg"
        tests_pkg.mkdir(parents=True)
        target = tests_pkg / "test_mod.py"
        target.write_text("def test_a(): pass\n", encoding="utf-8")
        found = auto_test.find_test_target(tmp_path, pkg / "mod.py")
        assert found.resolve() == target.resolve()


# ── run_tests_for_edit ─────────────────────────────────────────────────


_PYTEST_AVAILABLE = shutil.which("pytest") is not None or True  # always true: this very file is using pytest


@pytest.fixture
def project(tmp_path):
    """A small project that has a passing test by default."""
    src = tmp_path / "calc.py"
    src.write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test = tests_dir / "test_calc.py"
    test.write_text(
        "from calc import add\n"
        "def test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    # conftest.py to put tmp_path on sys.path
    (tmp_path / "conftest.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        encoding="utf-8",
    )
    return tmp_path


class TestRunTestsForEdit:
    def test_no_target_skipped(self, tmp_path):
        f = tmp_path / "lonely.py"
        f.write_text("x = 1\n", encoding="utf-8")
        result = auto_test.run_tests_for_edit(tmp_path, f, command="pytest -x")
        assert result["ok"] is True
        assert "no test target found" in result["skipped_reason"]

    def test_passing(self, project):
        result = auto_test.run_tests_for_edit(
            project, project / "calc.py", command=f"{sys.executable} -m pytest -x",
        )
        assert result["ok"] is True
        assert result["target"]
        assert result["output"] == ""

    def test_failing(self, project):
        # Break the source so the test fails
        (project / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",  # subtract!
            encoding="utf-8",
        )
        result = auto_test.run_tests_for_edit(
            project, project / "calc.py", command=f"{sys.executable} -m pytest -x",
        )
        assert result["ok"] is False
        assert "FAILED" in result["output"] or "assert" in result["output"]

    def test_runner_not_installed(self, project):
        result = auto_test.run_tests_for_edit(
            project, project / "calc.py", command="definitely-not-a-real-runner",
        )
        # Treated as ok (don't block the agent on missing tooling)
        assert result["ok"] is True
        assert "not installed" in result["skipped_reason"]
