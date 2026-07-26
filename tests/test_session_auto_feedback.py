"""
End-to-end test for Session._run_post_edit_feedback.

Verifies that after a successful file_edit / file_write, when auto_lint or
auto_test is enabled, feedback is actually injected into conversation_history
as a synthetic user turn (and that the doom-loop guard prevents re-injection
when nothing changed).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from resonant_client.engine.session import Session


_RUFF_AVAILABLE = shutil.which("ruff") is not None


def _make_minimal_session(project_path: Path) -> Session:
    """A bare-bones Session that doesn't need a backend (we only call _run_post_edit_feedback)."""
    s = Session.__new__(Session)
    s.conversation_history = []
    s.auto_lint_enabled = False
    s.auto_test_enabled = False
    s.auto_test_command = "pytest -x"
    s._lint_feedback_cache = {}
    s._test_feedback_cache = {}
    s.project_path = str(project_path)
    return s


@pytest.fixture
def lint_project(tmp_path):
    """Project with ruff configured, plus a file with an error."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\n[tool.ruff.lint]\nselect = ["F"]\n',
        encoding="utf-8",
    )
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")  # F401 unused import
    return tmp_path


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff not installed")
class TestAutoLintFeedback:
    def test_lint_feedback_injected(self, lint_project):
        s = _make_minimal_session(lint_project)
        s.auto_lint_enabled = True

        events = list(s._run_post_edit_feedback("bad.py"))

        # One STATUS event yielded
        statuses = [e for e in events if e.get("event") == "status"]
        assert any("Auto-lint" in (e.get("message") or "") for e in statuses)

        # Synthetic user turn appended
        assert len(s.conversation_history) == 1
        msg = s.conversation_history[0]
        assert msg["role"] == "user"
        assert "[auto-lint]" in msg["content"]
        assert "bad.py" in msg["content"]

    def test_doom_loop_guard_skips_duplicate(self, lint_project):
        """Same content → same fingerprint → no second injection."""
        s = _make_minimal_session(lint_project)
        s.auto_lint_enabled = True

        list(s._run_post_edit_feedback("bad.py"))
        assert len(s.conversation_history) == 1

        # Run again with no source changes — should not re-inject
        list(s._run_post_edit_feedback("bad.py"))
        assert len(s.conversation_history) == 1, "doom-loop guard failed"

    def test_changed_errors_re_inject(self, lint_project):
        s = _make_minimal_session(lint_project)
        s.auto_lint_enabled = True
        list(s._run_post_edit_feedback("bad.py"))
        assert len(s.conversation_history) == 1

        # Change the file to introduce DIFFERENT error → re-inject
        (lint_project / "bad.py").write_text(
            "import os\nimport sys\n", encoding="utf-8"
        )
        list(s._run_post_edit_feedback("bad.py"))
        assert len(s.conversation_history) == 2

    def test_disabled_no_feedback(self, lint_project):
        s = _make_minimal_session(lint_project)
        s.auto_lint_enabled = False  # explicitly off
        list(s._run_post_edit_feedback("bad.py"))
        assert s.conversation_history == []


class TestAutoTestFeedback:
    def test_no_target_no_feedback(self, tmp_path):
        s = _make_minimal_session(tmp_path)
        s.auto_test_enabled = True
        f = tmp_path / "lonely.py"
        f.write_text("x = 1\n", encoding="utf-8")
        list(s._run_post_edit_feedback("lonely.py"))
        assert s.conversation_history == []

    def test_failing_test_injects(self, tmp_path):
        import sys as _sys
        # Build a tiny project with a failing test
        (tmp_path / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",  # bug: subtract
            encoding="utf-8",
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_calc.py").write_text(
            "from calc import add\n"
            "def test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (tmp_path / "conftest.py").write_text(
            "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
            encoding="utf-8",
        )
        s = _make_minimal_session(tmp_path)
        s.auto_test_enabled = True
        s.auto_test_command = f"{_sys.executable} -m pytest -x"

        events = list(s._run_post_edit_feedback("calc.py"))
        statuses = [e for e in events if e.get("event") == "status"]
        assert any("Auto-test" in (e.get("message") or "") for e in statuses)

        assert len(s.conversation_history) == 1
        assert "[auto-test]" in s.conversation_history[0]["content"]


class TestSessionDefaults:
    """Make sure new fields are initialized on a freshly-constructed Session."""

    def test_defaults(self):
        # Use a dummy backend object — we just need __init__ to run
        class _Stub:
            name = "stub"
            model = "stub-m"
        s = Session(backend=_Stub())
        assert s.auto_lint_enabled is False
        assert s.auto_test_enabled is False
        assert s.auto_test_command == "pytest -x"
        assert s._lint_feedback_cache == {}
        assert s._test_feedback_cache == {}
