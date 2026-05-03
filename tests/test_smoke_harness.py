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
    summarize_runs,
)
from resonant_client.smoke.cli import build_parser
from resonant_client.smoke.flaky import (
    _MALFORMED_PLANNER_RESPONSE,
    _PLANNER_PROMPT_SIGNATURES,
)
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
