"""Regression: glob tool must accept absolute patterns (models pass them often)."""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.engine.tools import execute_tool


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "a.py").write_text("# a", encoding="utf-8")
    (p / "src" / "b.py").write_text("# b", encoding="utf-8")
    (p / "tests").mkdir()
    (p / "tests" / "test_a.py").write_text("# t", encoding="utf-8")
    return p


def test_relative_pattern_works(project):
    result = execute_tool("glob", {"pattern": "**/*.py", "path": str(project)},
                          project_path=str(project))
    assert not result.is_error
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "test_a.py" in result.output


def test_absolute_pattern_with_meta_chars(project):
    """Models often build absolute patterns like '<project>/**/*.py'."""
    pattern = str(project) + "/**/*.py"
    result = execute_tool("glob", {"pattern": pattern}, project_path=str(project))
    assert not result.is_error, f"absolute pattern should now work; got {result.output}"
    assert "a.py" in result.output


def test_absolute_pattern_no_meta_chars(project):
    """An absolute pattern with no glob meta should match the literal file."""
    pattern = str(project / "src" / "a.py")
    result = execute_tool("glob", {"pattern": pattern}, project_path=str(project))
    assert not result.is_error
    assert "a.py" in result.output


def test_absolute_pattern_under_subdir(project):
    """Absolute pattern targeting a subdir."""
    pattern = str(project) + "/src/*.py"
    result = execute_tool("glob", {"pattern": pattern}, project_path=str(project))
    assert not result.is_error
    assert "a.py" in result.output
    assert "b.py" in result.output
    # tests/ files should NOT match
    assert "test_a.py" not in result.output


def test_unsupported_pattern_returns_useful_error(project):
    """Garbage patterns should return a structured error, not crash."""
    # An unmatched bracket triggers a glob compile error on some Pythons
    result = execute_tool("glob", {"pattern": "[broken", "path": str(project)},
                          project_path=str(project))
    # Either it matches nothing (returns "(no matches)") or returns an error —
    # either way, no exception should bubble up.
    assert result is not None
