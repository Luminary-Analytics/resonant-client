"""
Tests for resonant_client/engine/lint.py
  - detect_linter
  - lint_file (skipped when ruff/eslint not on PATH)
"""

from __future__ import annotations

import json
import shutil

import pytest

from resonant_client.engine import lint


# ── detect_linter ───────────────────────────────────────────────────────


class TestDetectLinter:
    def test_no_config(self, tmp_path):
        assert lint.detect_linter(tmp_path) is None

    def test_ruff_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ruff]\nline-length = 100\n', encoding="utf-8"
        )
        result = lint.detect_linter(tmp_path)
        assert result is not None
        assert result[0] == "ruff"
        assert result[1][0] == "ruff"

    def test_ruff_subsection(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nselect = ["E"]\n', encoding="utf-8"
        )
        result = lint.detect_linter(tmp_path)
        assert result is not None
        assert result[0] == "ruff"

    def test_ruff_toml_file(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("line-length = 88\n", encoding="utf-8")
        result = lint.detect_linter(tmp_path)
        assert result[0] == "ruff"

    def test_flake8_dotfile(self, tmp_path):
        (tmp_path / ".flake8").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
        result = lint.detect_linter(tmp_path)
        assert result[0] == "flake8"

    def test_eslint_dotfile(self, tmp_path):
        (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
        result = lint.detect_linter(tmp_path)
        assert result[0] == "eslint"

    def test_eslint_in_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "eslintConfig": {"rules": {}}}), encoding="utf-8"
        )
        result = lint.detect_linter(tmp_path)
        assert result[0] == "eslint"

    def test_ruff_takes_precedence_over_flake8(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\n[tool.flake8]\n", encoding="utf-8"
        )
        result = lint.detect_linter(tmp_path)
        assert result[0] == "ruff"


# ── lint_file ───────────────────────────────────────────────────────────


_RUFF_AVAILABLE = shutil.which("ruff") is not None


class TestLintFile:
    def test_no_linter_returns_ok(self, tmp_path):
        result = lint.lint_file(tmp_path, tmp_path / "missing.py")
        assert result["ok"] is True
        assert "no linter detected" in result["skipped_reason"]

    def test_skipped_for_wrong_filetype(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
        f = tmp_path / "x.ts"
        f.write_text("var a = 1;\n", encoding="utf-8")
        result = lint.lint_file(tmp_path, f)
        assert result["ok"] is True
        assert "doesn't apply" in result["skipped_reason"]

    @pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff not installed")
    def test_ruff_clean_file(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
        f = tmp_path / "good.py"
        f.write_text("x = 1\n", encoding="utf-8")
        result = lint.lint_file(tmp_path, f)
        assert result["ok"] is True
        assert result["errors"] == ""

    @pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff not installed")
    def test_ruff_catches_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\n[tool.ruff.lint]\nselect = [\"F\"]\n", encoding="utf-8"
        )
        f = tmp_path / "bad.py"
        # Unused import — F401
        f.write_text("import os\n", encoding="utf-8")
        result = lint.lint_file(tmp_path, f)
        assert result["ok"] is False
        assert "F401" in result["errors"] or "unused" in result["errors"].lower() or result["errors"]
