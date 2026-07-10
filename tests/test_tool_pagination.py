"""Pagination contracts for context-efficient read/search tools."""

from __future__ import annotations

import time
from unittest.mock import patch

from resonant_client.engine.tools import _exec_file_read, _exec_glob, _exec_grep


def test_file_read_returns_requested_line_window_and_actionable_footer(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text("\n".join(f"line-{index}" for index in range(10)), encoding="utf-8")

    result = _exec_file_read(
        {"path": str(target), "offset": 3, "limit": 4},
        time.time(),
    )

    assert result.output.startswith("line-3\nline-4\nline-5\nline-6")
    assert '"offset": 7' in result.output
    assert result.metadata["next_offset"] == 7
    assert result.metadata["lines"] == 10


def test_file_read_final_page_has_no_continue_footer(tmp_path):
    target = tmp_path / "small.txt"
    target.write_text("a\nb\nc", encoding="utf-8")

    result = _exec_file_read(
        {"path": str(target), "offset": 2, "limit": 10},
        time.time(),
    )

    assert result.output == "c"
    assert result.metadata["next_offset"] is None


def test_glob_paginates_sorted_paths(tmp_path):
    for index in range(6):
        (tmp_path / f"file-{index}.py").write_text("", encoding="utf-8")

    result = _exec_glob(
        {"pattern": "*.py", "path": str(tmp_path), "offset": 2, "limit": 2},
        time.time(),
    )

    assert "file-2.py" in result.output
    assert "file-3.py" in result.output
    assert '"offset": 4' in result.output
    assert result.metadata["count"] == 6


def test_grep_paginates_matches_and_preserves_total(tmp_path):
    stdout = "\n".join(f"file.py:{index}:match" for index in range(8)).encode()
    with patch(
        "resonant_client.engine.tools._run_subprocess_with_cancel",
        return_value=(0, stdout, b"", False),
    ):
        result = _exec_grep(
            {"pattern": "match", "path": str(tmp_path), "offset": 3, "limit": 2},
            time.time(),
        )

    assert result.output.startswith("file.py:3:match\nfile.py:4:match")
    assert '"offset": 5' in result.output
    assert result.metadata["count"] == 8
    assert result.metadata["next_offset"] == 5
