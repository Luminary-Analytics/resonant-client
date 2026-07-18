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
    FlakyPlannerBackend,
    SmokeResult,
    VarianceReport,
    get_spec,
    list_spec_names,
    render_run_markdown,
    render_variance_markdown,
    resolve_model_id,
    summarize_runs,
)
from resonant_client.smoke.cli import _print_run_result, build_parser
from resonant_client.smoke.flaky import (
    _MALFORMED_PLANNER_RESPONSE,
    _PLANNER_PROMPT_SIGNATURES,
)
from resonant_client.smoke.report import _fmt_duration
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

    def test_edit_apply_success_rate(self):
        r = _make_result()
        r.edit_attempts = 4
        r.edit_successes = 3
        assert r.edit_apply_success_rate() == 0.75

    def test_glm_flagship_is_a_first_class_smoke_model(self):
        assert MODELS["glm"] == "glm-5.2:cloud"

    def test_arbitrary_model_ids_do_not_require_registry_changes(self):
        assert resolve_model_id("registry.example/new-model:latest") == "registry.example/new-model:latest"

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
    def test_failure_result_is_safe_for_ascii_windows_console(self, capsys):
        result = _make_result(verdict="failed", stop_reason="acceptance_failed")

        _print_run_result(result)

        output = capsys.readouterr().out
        output.encode("ascii")
        assert "FAIL" in output

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

    def test_run_subcommand_accepts_arbitrary_model_id(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--spec", "minimal", "--model", "registry/new-model:latest"]
        )
        assert args.model == "registry/new-model:latest"

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

    def test_run_inject_planner_failure_flag_default_false(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--spec", "minimal", "--model", "flash"]
        )
        assert args.inject_planner_failure is False

    def test_run_inject_planner_failure_flag_set(self):
        parser = build_parser()
        args = parser.parse_args([
            "run", "--spec", "minimal", "--model", "flash",
            "--inject-planner-failure",
        ])
        assert args.inject_planner_failure is True


# ── v0.5.4a2: FlakyPlannerBackend wrapper ───────────────────────────────


class _StubBackend:
    """Minimal backend stub. Records every `.stream(...)` call so we
    can assert which ones the wrapper forwarded vs. intercepted."""

    def __init__(self, name="stub", model="stub-model", responses=None):
        self.name = name
        self.model = model
        self.handles_tools = True
        self.calls: list[dict] = []
        # Default response: a parseable JSON envelope so non-intercepted
        # planner calls "succeed" from the parser's POV.
        self._responses = responses or [(
            "text", {"content": '{"subgoals":[{"goal":"do","specialization":"implement"}]}'}
        )]

    def stream(self, *, user_msg, conversation_history, instructions,
               tools, max_tokens=4096, cancel_event=None):
        self.calls.append({
            "user_msg": user_msg,
            "instructions": instructions,
            "tools_count": len(tools or []),
        })
        for ev in self._responses:
            yield ev
        yield ("done", {"finish_reason": "stop"})


_PLAN_PROMPT = (
    "You are a PLANNER. Your ONLY output is a JSON plan.\n"
    "..."
)
_DEEP_PLAN_PROMPT = (
    "You are a DEEP PLANNER. Your job has TWO PHASES:\n"
    "..."
)
_NON_PLANNER_PROMPT = (
    "You are an IMPLEMENT specialist. Make the change."
)


class TestFlakyPlannerBackend:
    """Wrapper for live walker-retry validation. The wrapper itself is
    pure plumbing — these tests pin the intercept logic so a refactor
    can't accidentally break the live retry test scenario."""

    def test_passthrough_for_non_planner_call(self):
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner)
        events = list(wrapped.stream(
            user_msg="anything",
            conversation_history=[],
            instructions=_NON_PLANNER_PROMPT,
            tools=[],
        ))
        # Inner backend was called once with the original instructions.
        assert len(inner.calls) == 1
        assert inner.calls[0]["instructions"] == _NON_PLANNER_PROMPT
        # Counts: not a planner call → no increment.
        assert wrapped.planner_call_count == 0
        assert wrapped.intercepted_count == 0
        # Stream surfaced the inner's events.
        text_events = [e for e in events if e[0] == "text"]
        assert text_events  # something came through

    def test_intercepts_first_planner_call(self):
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner, fail_first_n_planner_calls=1)
        events = list(wrapped.stream(
            user_msg="goal",
            conversation_history=[],
            instructions=_PLAN_PROMPT,
            tools=[],
        ))
        # Inner backend was NOT called — the intercept short-circuits.
        assert inner.calls == []
        assert wrapped.planner_call_count == 1
        assert wrapped.intercepted_count == 1
        # The malformed response was yielded.
        text_events = [e for e in events if e[0] == "text"]
        assert len(text_events) == 1
        assert text_events[0][1]["content"] == _MALFORMED_PLANNER_RESPONSE
        # Done event still emitted so the consumer doesn't hang.
        done_events = [e for e in events if e[0] == "done"]
        assert len(done_events) == 1

    def test_second_planner_call_passes_through(self):
        # fail_first_n=1 means call #1 corrupted, call #2+ forwarded.
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner, fail_first_n_planner_calls=1)
        # First call: intercepted.
        list(wrapped.stream(
            user_msg="g1", conversation_history=[],
            instructions=_PLAN_PROMPT, tools=[],
        ))
        # Second call: forwarded.
        list(wrapped.stream(
            user_msg="g2", conversation_history=[],
            instructions=_PLAN_PROMPT, tools=[],
        ))
        assert wrapped.planner_call_count == 2
        assert wrapped.intercepted_count == 1
        # Inner saw exactly one call (the second one).
        assert len(inner.calls) == 1
        assert inner.calls[0]["user_msg"] == "g2"

    def test_intercepts_plan_deep_too(self):
        # The wrapper recognizes both PLAN and PLAN_DEEP signatures.
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner, fail_first_n_planner_calls=1)
        list(wrapped.stream(
            user_msg="g", conversation_history=[],
            instructions=_DEEP_PLAN_PROMPT, tools=[],
        ))
        assert wrapped.intercepted_count == 1
        assert inner.calls == []

    def test_zero_failures_is_passthrough(self):
        # fail_first_n=0 means the wrapper never intercepts — useful
        # for sanity-checking the wrapper has no side effect when off.
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner, fail_first_n_planner_calls=0)
        list(wrapped.stream(
            user_msg="g", conversation_history=[],
            instructions=_PLAN_PROMPT, tools=[],
        ))
        assert wrapped.intercepted_count == 0
        assert len(inner.calls) == 1

    def test_negative_fail_n_raises(self):
        inner = _StubBackend()
        with pytest.raises(ValueError, match=">= 0"):
            FlakyPlannerBackend(inner, fail_first_n_planner_calls=-1)

    def test_fail_n_greater_than_one_intercepts_multiple(self):
        inner = _StubBackend()
        wrapped = FlakyPlannerBackend(inner, fail_first_n_planner_calls=2)
        # First two planner calls intercepted; third forwarded.
        for i in range(3):
            list(wrapped.stream(
                user_msg=f"g{i}", conversation_history=[],
                instructions=_PLAN_PROMPT, tools=[],
            ))
        assert wrapped.intercepted_count == 2
        assert wrapped.planner_call_count == 3
        assert len(inner.calls) == 1  # only the 3rd one made it through

    def test_passthrough_attributes(self):
        # Wrapper exposes inner backend's identity for telemetry.
        inner = _StubBackend(name="ollama", model="deepseek-v4-pro:cloud")
        wrapped = FlakyPlannerBackend(inner)
        assert wrapped.name == "ollama"
        assert wrapped.model == "deepseek-v4-pro:cloud"
        assert wrapped.handles_tools is True

    def test_unknown_attribute_forwards_to_inner(self):
        # __getattr__ delegation — unknown attrs (e.g. telemetry methods)
        # transparently reach the inner backend. Pin the behavior so a
        # refactor can't silently break this.
        class _BackendWithExtra(_StubBackend):
            def get_runtime_telemetry(self):
                return {"loaded_model": "test"}

        wrapped = FlakyPlannerBackend(_BackendWithExtra())
        assert wrapped.get_runtime_telemetry() == {"loaded_model": "test"}

    def test_planner_signatures_match_specialist_prompts(self):
        # Defensive: the signatures we sniff for must actually appear in
        # the real PLAN / PLAN_DEEP prompts. If specialists.py changes
        # the role-identifier wording, this fails loud rather than
        # silently disabling the intercept.
        from resonant_client.orchestration.specialists import SPECIALISTS
        from resonant_client.orchestration.plan_graph import NodeSpecialization
        plan = SPECIALISTS[NodeSpecialization.PLAN].system_block
        plan_deep = SPECIALISTS[NodeSpecialization.PLAN_DEEP].system_block
        # PLAN: "You are a PLANNER" should match.
        assert any(sig in plan for sig in _PLANNER_PROMPT_SIGNATURES), (
            f"None of {_PLANNER_PROMPT_SIGNATURES} matched PLAN prompt; "
            "FlakyPlannerBackend would no-op on PLAN calls."
        )
        # PLAN_DEEP: "You are a DEEP PLANNER" should match.
        assert any(sig in plan_deep for sig in _PLANNER_PROMPT_SIGNATURES), (
            f"None of {_PLANNER_PROMPT_SIGNATURES} matched PLAN_DEEP prompt; "
            "FlakyPlannerBackend would no-op on PLAN_DEEP calls."
        )


# ── v0.5.4a3: markdown reports ──────────────────────────────────────────


class TestFmtDuration:
    """Pin the duration formatter so the markdown output stays stable
    across cosmetic refactors."""

    def test_seconds_for_under_60(self):
        assert _fmt_duration(45.0) == "45.0s"

    def test_minutes_for_under_3600(self):
        assert _fmt_duration(120.0) == "2.0m"

    def test_hours_for_3600_and_up(self):
        assert _fmt_duration(7200.0) == "2.00h"

    def test_none_renders_as_dash(self):
        assert _fmt_duration(None) == "-"

    def test_non_numeric_renders_as_dash(self):
        # Defensive: a JSON value that arrived as something unexpected
        # shouldn't crash the formatter.
        assert _fmt_duration("not a number") == "-"  # type: ignore[arg-type]


class TestRenderRunMarkdown:
    def test_includes_spec_and_model_in_heading(self):
        r = _make_result(spec="wordcount", model="pro")
        md = render_run_markdown(r)
        assert "wordcount" in md
        assert "pro" in md
        assert md.startswith("# Smoke run")

    def test_converged_shows_check_mark_status(self):
        r = _make_result(verdict="satisfied")
        md = render_run_markdown(r)
        assert "✅" in md
        assert "converged" in md

    def test_paused_shows_failure_status(self):
        r = _make_result(verdict="paused", stop_reason="stuck")
        md = render_run_markdown(r)
        assert "✗" in md
        assert "paused" in md

    def test_timeout_shows_warning_status(self):
        r = _make_result(verdict="paused", stop_reason="smoke_timeout",
                         timed_out=True)
        md = render_run_markdown(r)
        assert "⚠" in md
        assert "timed out" in md

    def test_renders_iter_durations_when_present(self):
        r = _make_result(iter_durations=[60.0, 90.0, 120.0])
        md = render_run_markdown(r)
        assert "Iter durations" in md
        # Each value formatted via _fmt_duration
        assert "1.0m" in md
        assert "2.0m" in md

    def test_omits_iter_durations_when_empty(self):
        r = _make_result(iter_durations=[])
        md = render_run_markdown(r)
        assert "Iter durations" not in md

    def test_includes_error_field_only_when_set(self):
        ok = _make_result()
        md_ok = render_run_markdown(ok)
        assert "| Error |" not in md_ok

        broken = _make_result(verdict="failed")
        broken.error = "RuntimeError: backend offline"
        md_broken = render_run_markdown(broken)
        assert "| Error |" in md_broken
        assert "RuntimeError" in md_broken

    def test_table_has_header_row(self):
        # Markdown table must include the header + separator rows so
        # GitHub renders it as a table.
        r = _make_result()
        md = render_run_markdown(r)
        assert "| Field | Value |" in md
        assert "|---|---|" in md


class TestRenderVarianceMarkdown:
    def _three_run_report(self, **kwargs):
        runs = [
            _make_result(verdict="satisfied", total_elapsed=120.0,
                         iter_durations=[60.0, 60.0]),
            _make_result(verdict="satisfied", total_elapsed=140.0,
                         iter_durations=[70.0, 70.0]),
            _make_result(verdict="satisfied", total_elapsed=130.0,
                         iter_durations=[65.0, 65.0]),
        ]
        for r in runs:
            for k, v in kwargs.items():
                setattr(r, k, v)
        return summarize_runs(runs)

    def test_heading_includes_spec_model_n(self):
        report = self._three_run_report()
        md = render_variance_markdown(report)
        assert "wordcount" in md
        assert "pro" in md
        assert "n=3" in md

    def test_full_convergence_shows_check_headline(self):
        report = self._three_run_report()
        md = render_variance_markdown(report)
        assert "✅" in md
        assert "3 of 3 converged" in md
        assert "100%" in md

    def test_partial_convergence_shows_warning_headline(self):
        runs = [
            _make_result(verdict="satisfied"),
            _make_result(verdict="paused", stop_reason="stuck"),
            _make_result(verdict="satisfied"),
        ]
        report = summarize_runs(runs)
        md = render_variance_markdown(report)
        assert "⚠" in md
        assert "2 of 3 converged" in md
        assert "67%" in md

    def test_timing_rollup_present(self):
        report = self._three_run_report()
        md = render_variance_markdown(report)
        assert "## Timing" in md
        assert "Total elapsed (median)" in md
        assert "Iter duration (median)" in md

    def test_stop_reason_section_when_any(self):
        runs = [
            _make_result(verdict="satisfied"),
            _make_result(verdict="paused", stop_reason="stuck"),
        ]
        report = summarize_runs(runs)
        md = render_variance_markdown(report)
        assert "## Stop reasons" in md
        assert "`stuck`" in md

    def test_per_run_table_has_one_row_per_run(self):
        report = self._three_run_report()
        md = render_variance_markdown(report)
        assert "## Per-run detail" in md
        # The header row + 3 data rows + 1 separator row → at least 4
        # `| 1 |`, `| 2 |`, `| 3 |` distinguishes data rows.
        assert "| 1 |" in md
        assert "| 2 |" in md
        assert "| 3 |" in md

    def test_partial_convergence_lists_timed_out_count(self):
        runs = [
            _make_result(verdict="satisfied"),
            _make_result(verdict="paused", stop_reason="smoke_timeout",
                         timed_out=True),
        ]
        report = summarize_runs(runs)
        md = render_variance_markdown(report)
        assert "1 timed out" in md


class TestCLIReportFlag:
    def test_run_report_flag_default_is_none(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--spec", "minimal", "--model", "flash"]
        )
        assert args.report is None

    def test_run_report_flag_explicit(self, tmp_path):
        parser = build_parser()
        target = tmp_path / "summary.md"
        args = parser.parse_args([
            "run", "--spec", "minimal", "--model", "flash",
            "--report", str(target),
        ])
        assert args.report == str(target)

    def test_variance_report_flag_default_is_none(self):
        parser = build_parser()
        args = parser.parse_args(
            ["variance", "--spec", "minimal", "--model", "flash"]
        )
        assert args.report is None

    def test_variance_report_flag_explicit(self, tmp_path):
        parser = build_parser()
        target = tmp_path / "var.md"
        args = parser.parse_args([
            "variance", "--spec", "minimal", "--model", "flash",
            "--report", str(target),
        ])
        assert args.report == str(target)


# ── v0.5.5a1: baseline + variance diff ─────────────────────────────────


from resonant_client.smoke import (
    BaselineDiff,
    baseline_path,
    diff_against_baseline,
    list_baselines,
    load_baseline,
    save_baseline,
)
from resonant_client.smoke.baseline import (
    _is_improvement,
    _is_regression,
    _safe_subtract,
)


def _three_satisfied_runs(model="pro", total_elapsed_seq=(120.0, 140.0, 130.0),
                          iter_each=(60.0, 60.0)) -> VarianceReport:
    runs = [
        _make_result(verdict="satisfied", total_elapsed=t,
                     iter_durations=list(iter_each), model=model)
        for t in total_elapsed_seq
    ]
    return summarize_runs(runs)


class TestBaselineDiskIO:
    def test_save_and_load_round_trip(self, tmp_path):
        report = _three_satisfied_runs()
        target = save_baseline(report, project_path=tmp_path)
        # Path follows the canonical .resonant/smoke-baselines layout.
        assert target == baseline_path(tmp_path, report.spec_name, report.model_label)
        assert target.exists()
        loaded = load_baseline(
            project_path=tmp_path, spec=report.spec_name, model=report.model_label,
        )
        assert loaded is not None
        assert loaded["spec_name"] == report.spec_name
        assert loaded["model_label"] == report.model_label
        assert loaded["n"] == 3

    def test_load_returns_none_when_missing(self, tmp_path):
        loaded = load_baseline(
            project_path=tmp_path, spec="wordcount", model="pro",
        )
        assert loaded is None

    def test_save_overwrites_existing(self, tmp_path):
        # First save: 3 runs.
        save_baseline(_three_satisfied_runs(), project_path=tmp_path)
        # Second save: 5 runs — should replace.
        new = _three_satisfied_runs(total_elapsed_seq=(100.0, 110.0, 120.0, 130.0, 140.0))
        save_baseline(new, project_path=tmp_path)
        loaded = load_baseline(
            project_path=tmp_path, spec=new.spec_name, model=new.model_label,
        )
        assert loaded["n"] == 5

    def test_list_baselines_empty_directory(self, tmp_path):
        assert list_baselines(tmp_path) == []

    def test_list_baselines_returns_summaries(self, tmp_path):
        save_baseline(_three_satisfied_runs(), project_path=tmp_path)
        save_baseline(
            _three_satisfied_runs(model="flash"),
            project_path=tmp_path,
        )
        rows = list_baselines(tmp_path)
        assert len(rows) == 2
        models = {r["model"] for r in rows}
        assert models == {"pro", "flash"}
        for r in rows:
            assert r["spec"] == "wordcount"
            assert r["n"] == 3
            assert r["convergence_rate"] == 1.0
            assert r["total_elapsed_seconds_median"] == 130.0
            assert "path" in r

    def test_list_baselines_skips_unparseable_files(self, tmp_path):
        # A .json file under the baseline dir that ISN'T valid JSON
        # should be skipped, not crash. Forward-compat with future
        # schema versions / hand-edited files.
        (tmp_path / ".resonant" / "smoke-baselines").mkdir(parents=True)
        (tmp_path / ".resonant" / "smoke-baselines" / "broken.json").write_text(
            "not json", encoding="utf-8",
        )
        save_baseline(_three_satisfied_runs(), project_path=tmp_path)
        rows = list_baselines(tmp_path)
        # Only the valid baseline shows; the broken file is silently dropped.
        assert len(rows) == 1
        assert rows[0]["spec"] == "wordcount"


class TestDiffStatistics:
    def test_safe_subtract_handles_none(self):
        assert _safe_subtract(None, 10.0) is None
        assert _safe_subtract(10.0, None) is None
        assert _safe_subtract(None, None) is None

    def test_safe_subtract_arithmetic(self):
        assert _safe_subtract(150.0, 100.0) == 50.0
        assert _safe_subtract(100.0, 150.0) == -50.0

    def test_is_regression_requires_both_thresholds(self):
        # 5s absolute: too small even if it's 50% relative.
        assert _is_regression(delta=5.0, baseline=10.0) is False
        # 15s absolute, 15% relative: relative bar fails.
        assert _is_regression(delta=15.0, baseline=100.0) is False
        # 25s absolute, 25% relative: both bars cross.
        assert _is_regression(delta=25.0, baseline=100.0) is True

    def test_is_regression_negative_delta_is_not_regression(self):
        # Faster than baseline — never a regression.
        assert _is_regression(delta=-50.0, baseline=100.0) is False

    def test_is_improvement_mirrors_regression(self):
        # 25s faster than 100s baseline → 25% speedup → improvement.
        assert _is_improvement(delta=-25.0, baseline=100.0) is True
        # Slower → not an improvement.
        assert _is_improvement(delta=25.0, baseline=100.0) is False


class TestDiffAgainstBaseline:
    def _baseline_dict(self, **overrides):
        # Stable shape — what `VarianceReport.to_dict` produces.
        base = {
            "spec_name": "wordcount",
            "model_label": "pro",
            "n": 3,
            "convergence_rate": 1.0,
            "total_elapsed_seconds": {"median": 130.0, "min": 120.0,
                                      "max": 140.0, "stddev": 8.0},
            "iter_duration_seconds": {"median": 60.0, "stddev": 5.0,
                                      "sample_size": 6},
        }
        base.update(overrides)
        return base

    def test_no_change_no_regressions_no_improvements(self):
        baseline = self._baseline_dict()
        # Current matches baseline exactly.
        current = _three_satisfied_runs()
        diff = diff_against_baseline(current=current, baseline=baseline)
        assert diff.delta_convergence_rate == 0.0
        assert diff.regressions == []
        assert diff.improvements == []
        assert diff.has_regressions is False

    def test_convergence_drop_is_always_a_regression(self):
        # ANY drop in convergence is a regression (no relative threshold).
        baseline = self._baseline_dict(convergence_rate=1.0)
        runs = [
            _make_result(verdict="satisfied"),
            _make_result(verdict="paused", stop_reason="stuck"),
            _make_result(verdict="satisfied"),
        ]
        current = summarize_runs(runs)
        diff = diff_against_baseline(current=current, baseline=baseline)
        assert diff.has_regressions is True
        assert any("Convergence rate" in r for r in diff.regressions)

    def test_timing_regression_only_above_thresholds(self):
        # Baseline median 100s, current 130s: 30% relative, 30s absolute → regression.
        baseline = self._baseline_dict(
            total_elapsed_seconds={"median": 100.0, "min": 100.0,
                                   "max": 100.0, "stddev": 0.0},
        )
        current = _three_satisfied_runs(total_elapsed_seq=(130.0, 130.0, 130.0))
        diff = diff_against_baseline(current=current, baseline=baseline)
        assert diff.has_regressions is True
        assert any("Total-elapsed median grew" in r for r in diff.regressions)

    def test_small_timing_change_no_regression(self):
        # Baseline 100s, current 105s: 5% relative — below threshold.
        baseline = self._baseline_dict(
            total_elapsed_seconds={"median": 100.0, "min": 100.0,
                                   "max": 100.0, "stddev": 0.0},
        )
        current = _three_satisfied_runs(total_elapsed_seq=(105.0, 105.0, 105.0))
        diff = diff_against_baseline(current=current, baseline=baseline)
        assert diff.regressions == []

    def test_speedup_recorded_as_improvement(self):
        # Baseline 200s, current 100s: 50% faster → improvement.
        baseline = self._baseline_dict(
            total_elapsed_seconds={"median": 200.0, "min": 200.0,
                                   "max": 200.0, "stddev": 0.0},
        )
        current = _three_satisfied_runs(total_elapsed_seq=(100.0, 100.0, 100.0))
        diff = diff_against_baseline(current=current, baseline=baseline)
        assert any("Total-elapsed median dropped" in i for i in diff.improvements)

    def test_mismatched_spec_raises(self):
        # Baseline (wordcount, pro) vs current (roguelite, pro): refuse.
        baseline = self._baseline_dict(spec_name="roguelite")
        current = _three_satisfied_runs()  # spec=wordcount
        with pytest.raises(ValueError, match="Baseline mismatch"):
            diff_against_baseline(current=current, baseline=baseline)

    def test_mismatched_model_raises(self):
        baseline = self._baseline_dict(model_label="flash")
        current = _three_satisfied_runs()  # model=pro
        with pytest.raises(ValueError, match="Baseline mismatch"):
            diff_against_baseline(current=current, baseline=baseline)

    def test_to_dict_round_trips_via_json(self):
        baseline = self._baseline_dict()
        current = _three_satisfied_runs(total_elapsed_seq=(150.0, 160.0, 170.0))
        diff = diff_against_baseline(current=current, baseline=baseline)
        d = diff.to_dict()
        json.dumps(d)  # serializable
        assert d["spec_name"] == "wordcount"
        assert d["delta_total_elapsed_median"] is not None


class TestBaselineMarkdownIntegration:
    def _baseline_dict(self):
        return {
            "spec_name": "wordcount", "model_label": "pro", "n": 3,
            "convergence_rate": 1.0,
            "total_elapsed_seconds": {"median": 100.0, "min": 100.0,
                                      "max": 100.0, "stddev": 0.0},
            "iter_duration_seconds": {"median": 50.0, "stddev": 0.0,
                                      "sample_size": 6},
        }

    def test_variance_md_includes_diff_section_when_provided(self):
        current = _three_satisfied_runs()
        diff = diff_against_baseline(
            current=current, baseline=self._baseline_dict(),
        )
        md = render_variance_markdown(current, baseline_diff=diff)
        assert "## Diff vs baseline" in md

    def test_variance_md_omits_diff_when_not_provided(self):
        current = _three_satisfied_runs()
        md = render_variance_markdown(current)
        assert "## Diff vs baseline" not in md

    def test_variance_md_diff_table_has_all_three_metric_rows(self):
        current = _three_satisfied_runs()
        diff = diff_against_baseline(
            current=current, baseline=self._baseline_dict(),
        )
        md = render_variance_markdown(current, baseline_diff=diff)
        assert "Convergence rate" in md
        assert "Total elapsed (median)" in md
        assert "Iter duration (median)" in md

    def test_variance_md_lists_regression_narrative(self):
        # Force a regression: baseline 100s, current 200s.
        current = _three_satisfied_runs(total_elapsed_seq=(200.0, 200.0, 200.0))
        diff = diff_against_baseline(
            current=current, baseline=self._baseline_dict(),
        )
        md = render_variance_markdown(current, baseline_diff=diff)
        assert "Regressions:" in md
        assert "⚠" in md

    def test_variance_md_lists_improvement_narrative(self):
        # Baseline 200s, current 100s — symmetric speedup case.
        baseline = self._baseline_dict()
        baseline["total_elapsed_seconds"] = {
            "median": 200.0, "min": 200.0, "max": 200.0, "stddev": 0.0,
        }
        current = _three_satisfied_runs(total_elapsed_seq=(100.0, 100.0, 100.0))
        diff = diff_against_baseline(current=current, baseline=baseline)
        md = render_variance_markdown(current, baseline_diff=diff)
        assert "Improvements:" in md


class TestBaselineCLIParser:
    def test_baseline_set_requires_spec_model_source(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["baseline", "set"])
        with pytest.raises(SystemExit):
            parser.parse_args(["baseline", "set", "--spec", "wordcount"])
        with pytest.raises(SystemExit):
            parser.parse_args(["baseline", "set", "--spec", "wordcount",
                               "--model", "pro"])

    def test_baseline_set_full(self, tmp_path):
        parser = build_parser()
        src = tmp_path / "v.json"
        args = parser.parse_args([
            "baseline", "set", "--spec", "wordcount", "--model", "pro",
            "--from", str(src),
        ])
        assert args.spec == "wordcount"
        assert args.model == "pro"
        assert args.source == str(src)
        assert args.force is False

    def test_baseline_set_force_flag(self):
        parser = build_parser()
        args = parser.parse_args([
            "baseline", "set", "--spec", "wordcount", "--model", "pro",
            "--from", "/tmp/v.json", "--force",
        ])
        assert args.force is True

    def test_baseline_list_takes_no_args(self):
        parser = build_parser()
        args = parser.parse_args(["baseline", "list"])
        assert args.baseline_cmd == "list"

    def test_baseline_show_full(self):
        parser = build_parser()
        args = parser.parse_args(
            ["baseline", "show", "--spec", "minimal", "--model", "flash"]
        )
        assert args.spec == "minimal"
        assert args.model == "flash"

    def test_baseline_rm_full(self):
        parser = build_parser()
        args = parser.parse_args(
            ["baseline", "rm", "--spec", "minimal", "--model", "flash"]
        )
        assert args.spec == "minimal"
        assert args.model == "flash"

    def test_variance_diff_baseline_flag_default(self):
        parser = build_parser()
        args = parser.parse_args(
            ["variance", "--spec", "minimal", "--model", "flash"]
        )
        assert args.diff_baseline is False

    def test_variance_diff_baseline_flag_set(self):
        parser = build_parser()
        args = parser.parse_args([
            "variance", "--spec", "minimal", "--model", "flash",
            "--diff-baseline",
        ])
        assert args.diff_baseline is True


# ── v0.5.5a3: ci subcommand ────────────────────────────────────────────


from resonant_client.smoke import (
    DEFAULT_CI_SPECS,
    CISpecResult,
    CISuiteResult,
    parse_specs_arg,
    render_ci_markdown,
)


class TestParseSpecsArg:
    def test_single_spec(self):
        assert parse_specs_arg("minimal") == ["minimal"]

    def test_multiple_specs(self):
        assert parse_specs_arg("minimal,wordcount") == ["minimal", "wordcount"]

    def test_strips_whitespace(self):
        assert parse_specs_arg("  minimal , wordcount  ") == ["minimal", "wordcount"]

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_specs_arg("")

    def test_only_commas_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_specs_arg(",,")

    def test_unknown_spec_raises_with_valid_list(self):
        with pytest.raises(ValueError, match="Unknown spec") as exc_info:
            parse_specs_arg("not-a-real-spec")
        # Helpful: the error lists what IS valid.
        for name in DEFAULT_CI_SPECS:
            assert name in str(exc_info.value)


class TestCISpecResult:
    def _passing_variance(self):
        return summarize_runs([
            _make_result(verdict="satisfied"),
            _make_result(verdict="satisfied"),
        ])

    def _failing_variance(self):
        return summarize_runs([
            _make_result(verdict="paused", stop_reason="stuck"),
            _make_result(verdict="satisfied"),
        ])

    def test_passing_when_converged_no_diff(self):
        r = CISpecResult(spec_name="x", variance=self._passing_variance())
        assert r.converged is True
        assert r.has_regressions is False
        assert r.passed is True

    def test_failing_when_not_converged(self):
        r = CISpecResult(spec_name="x", variance=self._failing_variance())
        assert r.converged is False
        assert r.passed is False

    def test_failing_when_converged_but_baseline_regression(self):
        # Construct a BaselineDiff with regressions populated.
        from resonant_client.smoke import BaselineDiff
        diff = BaselineDiff(
            spec_name="x", model_label="pro",
            baseline_n=3, current_n=3,
            regressions=["something got slower"],
        )
        r = CISpecResult(
            spec_name="x", variance=self._passing_variance(), baseline_diff=diff,
        )
        assert r.converged is True
        assert r.has_regressions is True
        assert r.passed is False  # regression flips it to fail

    def test_skipped_spec_passes_trivially(self):
        # Skipped specs don't fail the suite — same as a missing entry.
        r = CISpecResult(
            spec_name="x", variance=self._passing_variance(),
            skipped=True, skipped_reason="not in --specs filter",
        )
        assert r.passed is True

    def test_to_dict_round_trips_via_json(self):
        r = CISpecResult(spec_name="x", variance=self._passing_variance())
        json.dumps(r.to_dict())  # serializable


class TestCISuiteResult:
    def _suite_with_results(self, *spec_results) -> CISuiteResult:
        return CISuiteResult(
            model_label="pro", model_id="deepseek-v4-pro:cloud",
            started_at_epoch=1714600000.0,
            total_elapsed_seconds=120.0,
            spec_results=list(spec_results),
        )

    def _passing_spec(self, name="minimal"):
        return CISpecResult(
            spec_name=name,
            variance=summarize_runs([_make_result(verdict="satisfied")]),
        )

    def _failing_spec(self, name="wordcount"):
        return CISpecResult(
            spec_name=name,
            variance=summarize_runs([_make_result(verdict="paused", stop_reason="stuck")]),
        )

    def test_all_passed_true_when_all_pass(self):
        suite = self._suite_with_results(self._passing_spec("minimal"),
                                         self._passing_spec("wordcount"))
        assert suite.all_passed is True
        assert suite.passing_count == 2
        assert suite.exit_code() == 0

    def test_all_passed_false_when_any_fail(self):
        suite = self._suite_with_results(self._passing_spec("minimal"),
                                         self._failing_spec("wordcount"))
        assert suite.all_passed is False
        assert suite.passing_count == 1
        assert suite.exit_code() == 1

    def test_has_any_regression_propagates(self):
        from resonant_client.smoke import BaselineDiff
        diff = BaselineDiff(
            spec_name="wordcount", model_label="pro",
            baseline_n=3, current_n=3,
            regressions=["slower"],
        )
        regressed = CISpecResult(
            spec_name="wordcount",
            variance=summarize_runs([_make_result(verdict="satisfied")]),
            baseline_diff=diff,
        )
        suite = self._suite_with_results(self._passing_spec("minimal"), regressed)
        assert suite.has_any_regression is True
        # Regression alone fails the suite — even with convergence.
        assert suite.exit_code() == 1

    def test_to_dict_round_trips_via_json(self):
        suite = self._suite_with_results(self._passing_spec(),
                                         self._failing_spec())
        d = suite.to_dict()
        json.dumps(d)
        assert d["spec_count"] == 2
        assert d["passing_count"] == 1
        assert d["all_passed"] is False
        assert d["exit_code"] == 1


class TestRenderCIMarkdown:
    def _ok_suite(self):
        return CISuiteResult(
            model_label="pro", model_id="deepseek-v4-pro:cloud",
            started_at_epoch=1714600000.0,
            total_elapsed_seconds=120.0,
            spec_results=[
                CISpecResult(
                    spec_name="minimal",
                    variance=summarize_runs([
                        _make_result(verdict="satisfied", total_elapsed=60.0),
                    ]),
                ),
                CISpecResult(
                    spec_name="wordcount",
                    variance=summarize_runs([
                        _make_result(verdict="satisfied", total_elapsed=140.0),
                    ]),
                ),
            ],
        )

    def test_passing_headline_includes_check(self):
        md = render_ci_markdown(self._ok_suite())
        assert "✅" in md
        assert "2 of 2 specs passed" in md
        assert md.startswith("# CI suite")

    def test_failing_headline_includes_x(self):
        suite = self._ok_suite()
        suite.spec_results.append(CISpecResult(
            spec_name="roguelite",
            variance=summarize_runs([
                _make_result(verdict="paused", stop_reason="stuck"),
            ]),
        ))
        md = render_ci_markdown(suite)
        assert "✗" in md
        assert "2 of 3" in md

    def test_per_spec_table_has_one_row_per_spec(self):
        md = render_ci_markdown(self._ok_suite())
        assert "## Specs" in md
        assert "| `minimal` |" in md
        assert "| `wordcount` |" in md

    def test_diff_section_only_when_baselines_diff(self):
        # No baseline diffs → no "Baseline diffs" section.
        md = render_ci_markdown(self._ok_suite())
        assert "## Baseline diffs" not in md

        # Inject a diff with a regression.
        from resonant_client.smoke import BaselineDiff
        suite = self._ok_suite()
        suite.spec_results[0].baseline_diff = BaselineDiff(
            spec_name="minimal", model_label="pro",
            baseline_n=3, current_n=1,
            regressions=["Total-elapsed median grew by 30s"],
        )
        md = render_ci_markdown(suite)
        assert "## Baseline diffs" in md
        assert "minimal" in md
        assert "30s" in md

    def test_skipped_spec_renders_with_skip_marker(self):
        suite = self._ok_suite()
        suite.spec_results.append(CISpecResult(
            spec_name="roguelite",
            variance=summarize_runs([_make_result(verdict="satisfied")]),
            skipped=True, skipped_reason="too slow for CI",
        ))
        md = render_ci_markdown(suite)
        assert "⏭" in md or "skipped" in md.lower()


class TestCICLIParser:
    def test_ci_requires_model(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ci"])

    def test_ci_default_specs_is_none_in_args(self):
        # Default is determined inside _cmd_ci (uses DEFAULT_CI_SPECS
        # when args.specs is None). Assert the arg defaults to None
        # so that branch fires.
        parser = build_parser()
        args = parser.parse_args(["ci", "--model", "pro"])
        assert args.specs is None
        assert args.n == 1
        assert args.timeout_minutes == 25
        assert args.diff_baseline is False

    def test_ci_specs_csv(self):
        parser = build_parser()
        args = parser.parse_args(
            ["ci", "--model", "pro", "--specs", "minimal,wordcount"]
        )
        assert args.specs == "minimal,wordcount"

    def test_ci_n_override(self):
        parser = build_parser()
        args = parser.parse_args(
            ["ci", "--model", "pro", "--n", "3"]
        )
        assert args.n == 3

    def test_ci_diff_baseline_flag(self):
        parser = build_parser()
        args = parser.parse_args(
            ["ci", "--model", "pro", "--diff-baseline"]
        )
        assert args.diff_baseline is True

    def test_ci_out_and_report_paths(self, tmp_path):
        parser = build_parser()
        out = tmp_path / "ci.json"
        rep = tmp_path / "ci.md"
        args = parser.parse_args([
            "ci", "--model", "pro", "--out", str(out), "--report", str(rep),
        ])
        assert args.out == str(out)
        assert args.report == str(rep)


class TestDefaultCISpecs:
    def test_minimal_in_default_suite(self):
        assert "minimal" in DEFAULT_CI_SPECS

    def test_wordcount_in_default_suite(self):
        assert "wordcount" in DEFAULT_CI_SPECS

    def test_roguelite_NOT_in_default_suite(self):
        # Too slow for CI by design — opt in via --specs.
        assert "roguelite" not in DEFAULT_CI_SPECS

    def test_all_default_specs_are_real_specs(self):
        from resonant_client.smoke import list_spec_names
        valid = set(list_spec_names())
        for name in DEFAULT_CI_SPECS:
            assert name in valid
