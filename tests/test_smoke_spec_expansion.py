"""Tests for v0.5.8a4 — smoke spec library expansion.

New specs and the seed_files mechanism that enables refactor-style
specs (pre-existing project state, not greenfield).

Coverage:
  - jsonlines spec parses + has typed criteria + is registered
  - refactor-py spec parses + has typed criteria + has seed_files
  - SmokeSpec.validated default and override
  - SmokeSpec.seed_files default empty
  - make_fresh_project() with seed_files writes + commits the seeds
  - make_fresh_project() with no seeds matches pre-v0.5.8a4 behavior
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from resonant_client.orchestration.grill_me import extract_spec
from resonant_client.smoke.specs import (
    SPECS,
    SmokeSpec,
    get_spec,
    list_spec_names,
)
from resonant_client.smoke.runner import make_fresh_project


# ── Spec registration ──────────────────────────────────────────────────


class TestNewSpecsRegistered:
    def test_jsonlines_in_registry(self):
        assert "jsonlines" in list_spec_names()
        spec = get_spec("jsonlines")
        assert spec.name == "jsonlines"
        assert "JSONL" in spec.spec_markdown or "jsonl" in spec.spec_markdown.lower()

    def test_refactor_py_in_registry(self):
        assert "refactor-py" in list_spec_names()
        spec = get_spec("refactor-py")
        assert spec.name == "refactor-py"

    def test_all_three_v0_5_x_staples_still_present(self):
        # Regression guard — adding new specs must not displace the
        # old ones.
        for required in ("minimal", "wordcount", "roguelite"):
            assert required in list_spec_names()


class TestNewSpecsParse:
    """Both new specs must parse cleanly via extract_spec and emit
    at least 4 typed criteria (the rigorous-grill addendum's minimum)."""

    def test_jsonlines_parses_with_5_criteria(self):
        spec = get_spec("jsonlines")
        parsed = extract_spec(spec.spec_markdown)
        assert parsed is not None
        # Spec advertises 5 criteria.
        assert len(parsed.acceptance_criteria) >= 4
        # All bash-typed (no chrome/vision/manual).
        for c in parsed.acceptance_criteria:
            assert c.type == "bash", f"expected bash, got {c.type}"

    def test_refactor_py_parses_with_5_criteria(self):
        spec = get_spec("refactor-py")
        parsed = extract_spec(spec.spec_markdown)
        assert parsed is not None
        assert len(parsed.acceptance_criteria) >= 4
        for c in parsed.acceptance_criteria:
            assert c.type == "bash"


class TestUnvalidatedFlag:
    def test_jsonlines_marked_unvalidated(self):
        # New specs ship with validated=False until they've been
        # smoke-run against a live model. The CLI surfaces a warning
        # so users know the convergence numbers aren't pinned yet.
        assert get_spec("jsonlines").validated is False

    def test_refactor_py_marked_unvalidated(self):
        assert get_spec("refactor-py").validated is False

    def test_existing_specs_remain_validated(self):
        # The three v0.5.x staples were validated in v0.5.0/v0.5.2
        # smokes; the validated default (True) preserves that.
        for name in ("minimal", "wordcount", "roguelite"):
            assert get_spec(name).validated is True


class TestSeedFilesField:
    def test_default_empty_dict(self):
        # Greenfield specs have no seed_files by default.
        for name in ("minimal", "wordcount", "roguelite", "jsonlines"):
            assert dict(get_spec(name).seed_files) == {}

    def test_refactor_py_has_two_seed_files(self):
        spec = get_spec("refactor-py")
        seeds = dict(spec.seed_files)
        assert "fizzbuzz.py" in seeds
        assert "tests/test_fizzbuzz.py" in seeds

    def test_seed_files_are_immutable_friendly(self):
        # SmokeSpec is frozen; the seed_files field shouldn't break
        # that. Reading is fine; calling dict() copies. The dataclass
        # frozen=True just prevents reassignment — the underlying dict
        # is still mutable Python-side, but consumers shouldn't rely
        # on mutating it.
        spec = get_spec("refactor-py")
        seeds = dict(spec.seed_files)
        # Callers can mutate their copy without affecting the
        # registry's spec.
        seeds["new_file.py"] = "x"
        assert "new_file.py" not in dict(get_spec("refactor-py").seed_files)


# ── Seed-file mechanism in make_fresh_project ──────────────────────────


class TestMakeFreshProjectSeedFiles:
    def test_no_seeds_matches_legacy_behavior(self, tmp_path, monkeypatch):
        # Without seed_files, the project has 1 commit (the empty
        # initial commit). Same behavior as pre-v0.5.8a4.
        monkeypatch.setattr(
            "tempfile.mkdtemp",
            lambda **kwargs: str(tmp_path / "project"),
        )
        (tmp_path / "project").mkdir()

        project = make_fresh_project(prefix="test-")
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=project,
            capture_output=True, text=True,
        )
        # 1 commit (the empty initial).
        assert len(log.stdout.strip().splitlines()) == 1

    def test_seeds_written_and_committed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "tempfile.mkdtemp",
            lambda **kwargs: str(tmp_path / "project"),
        )
        (tmp_path / "project").mkdir()

        project = make_fresh_project(
            prefix="test-",
            seed_files={
                "src/foo.py": "print('hi')\n",
                "tests/test_foo.py": "def test_x(): pass\n",
            },
        )
        # Files exist on disk.
        assert (project / "src/foo.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert (project / "tests/test_foo.py").read_text(encoding="utf-8") == "def test_x(): pass\n"
        # 2 commits (initial + seed).
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=project,
            capture_output=True, text=True,
        )
        commits = log.stdout.strip().splitlines()
        assert len(commits) == 2
        # Top commit is the seed.
        assert "smoke seed" in commits[0]

    def test_seeds_handle_nested_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "tempfile.mkdtemp",
            lambda **kwargs: str(tmp_path / "project"),
        )
        (tmp_path / "project").mkdir()

        project = make_fresh_project(
            prefix="test-",
            seed_files={
                "a/b/c/d.txt": "deep",
            },
        )
        assert (project / "a/b/c/d.txt").read_text(encoding="utf-8") == "deep"

    def test_empty_seed_dict_skips_commit(self, tmp_path, monkeypatch):
        # `seed_files={}` is the same as None — no second commit.
        monkeypatch.setattr(
            "tempfile.mkdtemp",
            lambda **kwargs: str(tmp_path / "project"),
        )
        (tmp_path / "project").mkdir()

        project = make_fresh_project(prefix="test-", seed_files={})
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=project,
            capture_output=True, text=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1


# ── refactor-py: seed contents are sane ────────────────────────────────


class TestRefactorPySeedContents:
    """Sanity checks on the seeded fizzbuzz.py + test_fizzbuzz.py.
    The bug is intentional; the test file should catch it."""

    def test_buggy_fizzbuzz_has_off_by_one(self):
        spec = get_spec("refactor-py")
        seed = spec.seed_files["fizzbuzz.py"]
        # The intentional bug: range(1, n) instead of range(1, n + 1).
        assert "range(1, n)" in seed
        # Comment flagging the bug for human readers.
        assert "BUG" in seed

    def test_buggy_fizzbuzz_actually_fails_tests(self):
        # If the seed is wrong (e.g. the bug got fixed in the seed
        # by accident), the spec's premise breaks. Run the seed
        # in-place and confirm it returns a too-short list for n=15.
        spec = get_spec("refactor-py")
        ns = {}
        exec(spec.seed_files["fizzbuzz.py"], ns)
        result = ns["fizzbuzz"](15)
        # Bug means result has 14 items (range(1,15) = 1..14), not 15.
        assert len(result) == 14, (
            f"buggy seed returned {len(result)} items; bug may have "
            f"been accidentally fixed in the seed"
        )

    def test_test_file_imports_fizzbuzz(self):
        # The test file uses `import fizzbuzz` (project root), so the
        # seeded fizzbuzz.py must be at the project root.
        spec = get_spec("refactor-py")
        test_src = spec.seed_files["tests/test_fizzbuzz.py"]
        assert "import fizzbuzz" in test_src

    def test_test_file_pinning_signature_check(self):
        # The acceptance criterion has a `git diff --name-only -- tests/`
        # check that catches model-edits-the-tests cheating. The test
        # file must include the signature-pinning test so attempts to
        # weaken it stand out.
        spec = get_spec("refactor-py")
        test_src = spec.seed_files["tests/test_fizzbuzz.py"]
        assert "test_signature_unchanged" in test_src
