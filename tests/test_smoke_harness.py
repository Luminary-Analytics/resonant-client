"""Tests for `resonant_client.smoke` — the autonomous-mission smoke
harness.

These cover the testable parts (specs, statistics, data structures, CLI
parser) WITHOUT exercising live Ollama runs. Live runs are gated on the
explicit CLI invocation; the harness's exec path is too I/O-bound to
unit-test sensibly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from resonant_client.smoke import (
    MODELS,
    SPECS,
    SmokeResult,
    VarianceReport,
    get_spec,
    list_spec_names,
    summarize_runs,
)
from resonant_client.smoke.cli import build_parser
from resonant_client.smoke.variance import _median, _stddev


# ── specs.py ────────────────────────────────────────────────────────────


class TestSpecRegistry:
    def test_minimal_spec_registered(self):
        spec = get_spec("minimal")
        assert spec.name == "minimal"
        assert "Final spec" in spec.spec_markdown
        assert "Acceptance criteria" in spec.spec_markdown

    def test_all_bundled_specs_have_typed_criteria(self):
        # The whole point of the harness is that bundled specs are
        # `build_roadmap_from_spec`-ready. Ensure each spec parses
        # cleanly + emits at least one typed criterion.
        from resonant_client.orchestration.grill_me import extract_spec
        for name in list_spec_names():
            spec = get_spec(name)
            parsed = extract_spec(spec.spec_markdown)
            assert parsed is not None, f"{name} spec doesn't parse"
            assert parsed.acceptance_criteria, (
                f"{name} spec has no typed criteria"
            )

    def test_unknown_spec_raises_with_valid_list(self):
        with pytest.raises(ValueError, match="Unknown spec") as exc_info:
            get_spec("totally-not-real")
        # The error message lists valid specs to help the user recover.
        for name in list_spec_names():
            assert name in str(exc_info.value)

    def test_list_spec_names_sorted(self):
        names = list_spec_names()
        assert names == sorted(names)
        # Sanity: the three v0.5.x staples are all present.
        for required in {"minimal", "wordcount", "roguelite"}:
            assert required in names


# ── runner.SmokeResult ──────────────────────────────────────────────────


def _make_result(
    *, verdict="satisfied", iter_durations=None,
    stop_reason="", timed_out=False, total_elapsed=120.0,
    spec="wordcount", model="pro",
) -> SmokeResult:
    # NOTE: explicit None check — `iter_durations or [60.0]` would
    # short-circuit on an empty list, masking the "no iters" case.
    durations = list(iter_durations) if iter_durations is not None else [60.0]
    iter_n = len(durations) or 1
    return SmokeResult(
        spec_name=spec,
        model_label=model,
        model_id=MODELS[model],
        started_at_epoch=1714600000.0,
        total_elapsed_seconds=total_elapsed,
        daemon_elapsed_seconds=total_elapsed - 1.0,
        verdict=verdict,
        stop_reason=stop_reason,
        iter_count=iter_n,
        iter_started=iter_n,
        iter_complete=iter_n,
        iter_durations_seconds=durations,
        timed_out=timed_out,
    )


class TestSmokeResult:
    def test_is_converged_when_satisfied(self):
        assert _make_result(verdict="satisfied").is_converged() is True

    def test_not_converged_when_paused(self):
        assert _make_result(verdict="paused").is_converged() is False

    def test_avg_iter_duration_returns_none_when_no_iters(self):
        r = _make_result(iter_durations=[])
        assert r.avg_iter_duration_seconds() is None

    def test_avg_iter_duration_arithmetic(self):
        r = _make_result(iter_durations=[60.0, 90.0, 120.0])
        assert r.avg_iter_duration_seconds() == 90.0

    def test_to_dict_includes_derived_fields(self):
        r = _make_result(iter_durations=[100.0, 200.0])
        d = r.to_dict()
        assert d["spec_name"] == "wordcount"
        assert d["model_label"] == "pro"
        assert d["is_converged"] is True
        assert d["avg_iter_duration_seconds"] == 150.0
        # Round-trips via JSON.
        json.dumps(d)


# ── variance.py: statistics ─────────────────────────────────────────────


class TestStatistics:
    def test_median_empty_returns_none(self):
        assert _median([]) is None

    def test_median_single(self):
        assert _median([42.0]) == 42.0

    def test_median_odd_count(self):
        assert _median([3.0, 1.0, 2.0]) == 2.0

    def test_median_even_count_averages_middle(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_stddev_empty_returns_none(self):
        assert _stddev([]) is None

    def test_stddev_single_returns_zero(self):
        # Population stddev with n=1 — defined as 0 here (no spread).
        assert _stddev([42.0]) == 0.0

    def test_stddev_known_values(self):
        # Population stddev of [2, 4, 4, 4, 5, 5, 7, 9] = 2.0
        assert _stddev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.0)


# ── variance.VarianceReport ─────────────────────────────────────────────


class TestVarianceReport:
    def test_n_runs_aggregation(self):
        runs = [
            _make_result(verdict="satisfied", total_elapsed=120.0,
                         iter_durations=[60.0, 60.0]),
            _make_result(verdict="satisfied", total_elapsed=140.0,
                         iter_durations=[70.0, 70.0]),
            _make_result(verdict="satisfied", total_elapsed=130.0,
                         iter_durations=[65.0, 65.0]),
        ]
        report = summarize_runs(runs)
        assert report.n == 3
        assert report.converged_count == 3
        assert report.convergence_rate == 1.0
        assert report.failed_count == 0
        assert report.timed_out_count == 0
        assert report.total_elapsed_seconds_median == 130.0
        assert report.total_elapsed_seconds_min == 120.0
        assert report.total_elapsed_seconds_max == 140.0
        assert report.iter_duration_seconds_median is not None

    def test_partial_convergence_recorded(self):
        runs = [
            _make_result(verdict="satisfied", total_elapsed=120.0),
            _make_result(verdict="paused", stop_reason="stuck",
                         total_elapsed=600.0),
            _make_result(verdict="satisfied", total_elapsed=130.0),
        ]
        report = summarize_runs(runs)
        assert report.converged_count == 2
        assert report.failed_count == 1
        assert report.convergence_rate == pytest.approx(2 / 3)
        # Stop-reason breakdown picks up the non-converging case.
        assert report.stop_reason_counts.get("stuck") == 1

    def test_timed_out_runs_counted_separately_from_failed(self):
        runs = [
            _make_result(verdict="satisfied", total_elapsed=120.0),
            _make_result(verdict="paused", stop_reason="smoke_timeout",
                         total_elapsed=1500.0, timed_out=True),
        ]
        report = summarize_runs(runs)
        assert report.converged_count == 1
        assert report.failed_count == 0
        assert report.timed_out_count == 1

    def test_summarize_empty_runs_raises(self):
        with pytest.raises(ValueError, match="empty"):
            summarize_runs([])

    def test_summarize_mixed_specs_raises(self):
        runs = [
            _make_result(spec="wordcount", model="pro"),
            _make_result(spec="roguelite", model="pro"),
        ]
        with pytest.raises(ValueError, match="share spec"):
            summarize_runs(runs)

    def test_summarize_mixed_models_raises(self):
        runs = [
            _make_result(spec="wordcount", model="pro"),
            _make_result(spec="wordcount", model="flash"),
        ]
        with pytest.raises(ValueError, match="share spec"):
            summarize_runs(runs)

    def test_to_dict_round_trips_via_json(self):
        runs = [
            _make_result(iter_durations=[100.0, 110.0]),
            _make_result(iter_durations=[120.0, 130.0]),
        ]
        report = summarize_runs(runs)
        d = report.to_dict()
        # No exception means it's JSON-serializable end-to-end.
        s = json.dumps(d)
        loaded = json.loads(s)
        assert loaded["spec_name"] == runs[0].spec_name
        assert loaded["n"] == 2
        assert loaded["convergence_rate"] == 1.0
        assert isinstance(loaded["runs"], list) and len(loaded["runs"]) == 2

    def test_pooled_iter_duration_pulls_from_all_runs(self):
        # Three runs with iter durations [10], [20, 30], [40] should
        # produce a pooled list of 4 values for median + stddev.
        runs = [
            _make_result(iter_durations=[10.0]),
            _make_result(iter_durations=[20.0, 30.0]),
            _make_result(iter_durations=[40.0]),
        ]
        report = summarize_runs(runs)
        # median of [10, 20, 30, 40] = 25
        assert report.iter_duration_seconds_median == 25.0


# ── CLI parser ──────────────────────────────────────────────────────────


class TestCLIParser:
    def test_list_specs_subcommand_parses(self):
        parser = build_parser()
        args = parser.parse_args(["list-specs"])
        assert args.cmd == "list-specs"
        assert callable(args.func)

    def test_run_subcommand_requires_spec_and_model(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run"])  # missing required args
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--spec", "minimal"])  # missing --model

    def test_run_subcommand_full(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--spec", "wordcount", "--model", "pro"]
        )
        assert args.cmd == "run"
        assert args.spec == "wordcount"
        assert args.model == "pro"
        assert args.timeout_minutes == 25
        assert args.out is None

    def test_run_subcommand_rejects_unknown_spec(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["run", "--spec", "totally-fake", "--model", "pro"]
            )

    def test_run_subcommand_rejects_unknown_model(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["run", "--spec", "minimal", "--model", "claude-haiku"]
            )

    def test_variance_subcommand_default_n_is_three(self):
        parser = build_parser()
        args = parser.parse_args(
            ["variance", "--spec", "minimal", "--model", "flash"]
        )
        assert args.n == 3

    def test_variance_subcommand_custom_n(self):
        parser = build_parser()
        args = parser.parse_args(
            ["variance", "--spec", "wordcount", "--model", "pro", "--n", "5"]
        )
        assert args.n == 5

    def test_variance_subcommand_out_path(self, tmp_path):
        parser = build_parser()
        target = tmp_path / "report.json"
        args = parser.parse_args([
            "variance", "--spec", "wordcount", "--model", "pro",
            "--out", str(target),
        ])
        assert args.out == str(target)
