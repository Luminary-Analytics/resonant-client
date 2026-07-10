"""Tests for v0.5.10a3 — smoke runner edge-case hardening.

Two defensive contracts shipped:

1. `make_fresh_project` validates seed_files paths to keep writes
   inside the tempdir. A spec author typo with `..` or an absolute
   path would have silently written outside the smoke project and
   leaked files into the host filesystem (or worse, on POSIX, an
   absolute path overwrites `Path('/foo') / '/etc/x'` semantics).

2. `_accumulate_event` (extracted from `run_smoke`) drops missing
   or non-positive `duration_seconds` from `iter_durations`. Pre-fix
   a daemon-event-shape regression that dropped the duration_seconds
   key would have silently filled durations with 0.0 entries and
   biased the variance/median rollups downward. Now the rollup
   either has a real number or nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.smoke.runner import (
    _accumulate_event,
    _new_summary,
    _validate_seed_path,
    make_fresh_project,
)


# ── Seed-files path safety ──────────────────────────────────────────────


class TestSeedFilesPathSafety:
    """`make_fresh_project` must reject seed_files paths that escape
    the project root before any I/O happens."""

    def test_rejects_absolute_posix_path(self):
        with pytest.raises(ValueError, match="absolute"):
            make_fresh_project(
                prefix="resonant-test-",
                seed_files={"/etc/passwd": "x"},
            )

    def test_rejects_dotdot_segment(self):
        with pytest.raises(ValueError, match=r"\.\.|outside"):
            make_fresh_project(
                prefix="resonant-test-",
                seed_files={"../escape.py": "x"},
            )

    def test_rejects_dotdot_in_middle_of_path(self):
        # Even a path that LOOKS innocuous can escape via embedded ..
        with pytest.raises(ValueError, match=r"\.\.|outside"):
            make_fresh_project(
                prefix="resonant-test-",
                seed_files={"foo/../../../escape.py": "x"},
            )

    def test_rejects_empty_path_string(self):
        with pytest.raises(ValueError, match="non-empty string"):
            make_fresh_project(
                prefix="resonant-test-",
                seed_files={"": "x"},
            )

    def test_rejects_non_string_key(self):
        with pytest.raises(ValueError, match="non-empty string"):
            make_fresh_project(
                prefix="resonant-test-",
                seed_files={123: "x"},  # type: ignore[dict-item]
            )

    def test_accepts_simple_relative_filename(self, tmp_path):
        # Sanity: well-formed paths still work.
        project = make_fresh_project(
            prefix="resonant-test-ok-",
            seed_files={"hello.py": "print('hi')\n"},
        )
        try:
            assert (project / "hello.py").read_text() == "print('hi')\n"
        finally:
            # Clean up the tempdir to avoid littering /tmp during
            # repeated test runs.
            import shutil
            shutil.rmtree(project, ignore_errors=True)

    def test_accepts_nested_subdir(self):
        # Subdirs are fine — the path validator only blocks escape.
        project = make_fresh_project(
            prefix="resonant-test-nested-",
            seed_files={"src/foo/bar.py": "x = 1\n"},
        )
        try:
            assert (project / "src" / "foo" / "bar.py").read_text() == "x = 1\n"
        finally:
            import shutil
            shutil.rmtree(project, ignore_errors=True)

    def test_rejection_happens_before_tempdir_created(self, monkeypatch):
        # Critical invariant: a bad spec must NOT leak a tempdir.
        # If validation passes-through to mkdtemp before raising, we'd
        # leave a stale `/tmp/resonant-test-...` per failing test run.
        import tempfile
        mkdtemp_calls = []
        original_mkdtemp = tempfile.mkdtemp

        def spy_mkdtemp(*args, **kwargs):
            mkdtemp_calls.append((args, kwargs))
            return original_mkdtemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)

        with pytest.raises(ValueError):
            make_fresh_project(
                prefix="resonant-bad-",
                seed_files={"/abs/path": "x"},
            )

        # No tempdir should have been created.
        assert mkdtemp_calls == []


class TestValidateSeedPathDirect:
    """Module-level helper unit tests."""

    def test_valid_path_returns_resolved_target(self, tmp_path):
        target = _validate_seed_path("foo/bar.py", tmp_path)
        assert target == (tmp_path / "foo/bar.py").resolve()
        # Must be inside the project root.
        target.relative_to(tmp_path.resolve())

    def test_rejects_absolute_path(self, tmp_path):
        with pytest.raises(ValueError, match="absolute"):
            _validate_seed_path("/etc/x", tmp_path)

    def test_rejects_dotdot(self, tmp_path):
        with pytest.raises(ValueError, match=r"\.\.|outside"):
            _validate_seed_path("../x", tmp_path)

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_seed_path("", tmp_path)

    def test_rejects_none(self, tmp_path):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_seed_path(None, tmp_path)  # type: ignore[arg-type]


# ── _accumulate_event filter behavior ──────────────────────────────────


class TestAccumulateEvent:
    """Module-level helper that the run_smoke event sink wraps. Each
    event kind updates a specific summary field; v0.5.10a3 hardening
    is that `iter_durations` only collects POSITIVE NUMERIC durations."""

    def _new(self):
        return _new_summary()

    def test_iteration_started_increments_counter(self):
        s = self._new()
        _accumulate_event({"event": "autonomous_iteration_started"}, s)
        assert s["iter_started"] == 1
        assert s["iter_complete"] == 0
        # Other counters untouched.
        assert s["iter_durations"] == []

    def test_iteration_complete_records_positive_duration(self):
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": 12.5},
            s,
        )
        assert s["iter_complete"] == 1
        assert s["iter_durations"] == [12.5]

    def test_iteration_complete_records_int_duration(self):
        # ints are accepted (not just floats) since daemon may emit
        # whole-second elapsed in some shapes.
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": 7},
            s,
        )
        assert s["iter_durations"] == [7.0]

    def test_iteration_complete_skips_missing_duration(self):
        # The pre-v0.5.10a3 bug: missing key → 0 default → isinstance
        # passes → iter_durations gets a 0.0. Now: missing → skip.
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete"},
            s,
        )
        assert s["iter_complete"] == 1
        assert s["iter_durations"] == []  # no 0.0 pollution

    def test_iteration_complete_skips_zero_duration(self):
        # Zero is technically numeric but means "we don't have a real
        # measurement" — drop it.
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": 0},
            s,
        )
        assert s["iter_complete"] == 1
        assert s["iter_durations"] == []

    def test_iteration_complete_skips_negative_duration(self):
        # Negative would only happen via clock drift / timer bug, but
        # certainly not a valid datapoint. Drop.
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": -3.2},
            s,
        )
        assert s["iter_durations"] == []

    def test_iteration_complete_skips_non_numeric_duration(self):
        # Defensive: a future event-shape change might make this a str.
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": "12.5"},
            s,
        )
        assert s["iter_durations"] == []

    def test_iteration_complete_skips_none_duration(self):
        s = self._new()
        _accumulate_event(
            {"event": "autonomous_iteration_complete",
             "duration_seconds": None},
            s,
        )
        assert s["iter_durations"] == []

    def test_iteration_failed_increments_counter(self):
        s = self._new()
        _accumulate_event({"event": "autonomous_iteration_failed"}, s)
        assert s["iter_failed"] == 1

    def test_reflection_increments_counter(self):
        s = self._new()
        _accumulate_event({"event": "autonomous_reflection"}, s)
        assert s["reflection_count"] == 1

    def test_agent_reliability_metrics_are_accumulated(self):
        s = self._new()
        events = [
            {"event": "tool.call", "name": "file_edit"},
            {
                "event": "tool.result",
                "name": "file_edit",
                "is_error": False,
                "metadata": {"match_strategy": "indentation"},
            },
            {"event": "tool.call", "name": "grep"},
            {
                "event": "tool.result",
                "name": "grep",
                "is_error": True,
                "output": "Tool arguments were malformed: expected object",
            },
            {"event": "backend.status", "kind": "ollama_retry"},
            {
                "event": "node.done",
                "result": {"data": {"structured_output_repaired": True}},
            },
        ]
        for event in events:
            _accumulate_event(event, s)

        assert s["tool_calls_total"] == 2
        assert s["edit_attempts"] == 1
        assert s["edit_successes"] == 1
        assert s["fuzzy_edit_rescues"] == 1
        assert s["tool_argument_failures"] == 1
        assert s["backend_retry_count"] == 1
        assert s["structured_output_repairs"] == 1

    def test_unknown_event_is_no_op(self):
        s_before = self._new()
        s = self._new()
        _accumulate_event({"event": "some_other_event"}, s)
        assert s == s_before

    def test_empty_event_is_no_op(self):
        s_before = self._new()
        s = self._new()
        _accumulate_event({}, s)
        assert s == s_before

    def test_realistic_full_iter_sequence(self):
        # Sanity: a typical iter's event stream produces a single
        # bumped iter_started, iter_complete, reflection_count, and
        # one duration entry. This is what run_smoke actually sees
        # for one happy-path iter.
        s = self._new()
        events = [
            {"event": "autonomous_iteration_started"},
            {"event": "autonomous_iteration_complete",
             "duration_seconds": 42.7},
            {"event": "autonomous_reflection"},
        ]
        for e in events:
            _accumulate_event(e, s)
        assert s["iter_started"] == 1
        assert s["iter_complete"] == 1
        assert s["iter_failed"] == 0
        assert s["reflection_count"] == 1
        assert s["iter_durations"] == [42.7]
