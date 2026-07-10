"""Regression tests for reliable, model-friendly file edits."""

from __future__ import annotations

import time

import pytest

from resonant_client.engine.editing import EditMatchError, apply_text_edit
from resonant_client.engine.tools import _exec_file_edit


def test_exact_match_requires_uniqueness():
    with pytest.raises(EditMatchError, match="matched 2 locations"):
        apply_text_edit("same\nmiddle\nsame\n", "same", "changed")


def test_replace_all_must_be_explicit():
    result = apply_text_edit(
        "same\nmiddle\nsame\n",
        "same",
        "changed",
        replace_all=True,
    )

    assert result.content == "changed\nmiddle\nchanged\n"
    assert result.replacements == 2
    assert result.strategy == "exact"


def test_whitespace_and_indentation_drift_is_repaired():
    content = "def total(items):\n    return sum(items)\n"
    result = apply_text_edit(
        content,
        "def total(items):\n  return   sum(items)",
        "def total(items):\n    return sum(items, start=0)",
    )

    assert "start=0" in result.content
    assert result.strategy == "whitespace"


def test_high_confidence_fuzzy_match_repairs_small_model_drift():
    content = (
        "def checkout(total, discount):\n"
        "    adjusted = total - discount\n"
        "    return max(adjusted, 0)\n"
    )
    result = apply_text_edit(
        content,
        (
            "def checkout(total, discount):\n"
            "    adjusted = total - discout\n"
            "    return max(adjusted, 0)\n"
        ),
        "def checkout(total, discount):\n    return max(total - discount, 0)\n",
    )

    assert result.strategy == "fuzzy"
    assert "adjusted" not in result.content


def test_failed_match_returns_actionable_closest_line_hint():
    with pytest.raises(EditMatchError, match=r"Closest candidate starts at line 2"):
        apply_text_edit(
            "header\ndef calculate(value):\n    return value * 2\nfooter\n",
            "def completely_different(value):\n    raise RuntimeError\n",
            "replacement",
        )


def test_exec_file_edit_does_not_modify_ambiguous_file(tmp_path):
    target = tmp_path / "sample.txt"
    original = "same\nmiddle\nsame\n"
    target.write_text(original, encoding="utf-8")

    result = _exec_file_edit(
        {"path": str(target), "old_text": "same", "new_text": "changed"},
        time.time(),
    )

    assert result.is_error
    assert "more surrounding context" in result.output
    assert target.read_text(encoding="utf-8") == original


def test_exec_file_edit_reports_match_strategy(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("if ready:\n    run()\n", encoding="utf-8")

    result = _exec_file_edit(
        {
            "path": str(target),
            "old_text": "if ready:\n  run()",
            "new_text": "if ready:\n    run_once()",
        },
        time.time(),
    )

    assert not result.is_error
    assert result.metadata["match_strategy"] == "whitespace"
    assert "whitespace match" in result.output
