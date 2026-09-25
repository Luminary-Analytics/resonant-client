"""
Multi-run variance comparison.

Single smoke runs tell you "did it converge once". Variance reports
tell you "does it converge consistently" — the more useful question
for ship-readiness. v0.5.2 GA caught two parser bugs precisely because
the second wordcount run hit a different code path than the first.

`run_variance(spec, model, n)` runs the same smoke `n` times against
fresh project dirs each time, then summarizes:
- Convergence rate (`k of n satisfied`)
- Per-run total elapsed (median, min, max, stddev)
- Per-iter duration distribution across all runs
- Stop-reason breakdown for non-converging runs

Use this BEFORE tagging a release: 3 runs is the minimum for variance
catching, 5 is the comfortable floor.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from .runner import SmokeResult, run_smoke

logger = logging.getLogger(__name__)


# ── Statistics helpers (pure functions, easily testable) ───────────────


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _stddev(values: list[float]) -> Optional[float]:
    """Population stddev; we don't pretend to be inferential statistics
    here — the sample size is small (typical n=3-5) and we only use this
    to flag "this run was way off the median"."""
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ── Report container ───────────────────────────────────────────────────


@dataclass
class VarianceReport:
    """Aggregated outcome of N runs of the same (spec, model)."""
    spec_name: str
    model_label: str
    model_id: str
    runs: list[SmokeResult] = field(default_factory=list)

    # Derived (filled by `_recompute`).
    converged_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    total_elapsed_seconds_median: Optional[float] = None
    total_elapsed_seconds_min: Optional[float] = None
    total_elapsed_seconds_max: Optional[float] = None
    total_elapsed_seconds_stddev: Optional[float] = None
    iter_duration_seconds_median: Optional[float] = None
    iter_duration_seconds_stddev: Optional[float] = None
    stop_reason_counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_runs(
        cls, *, spec_name: str, model_label: str, model_id: str,
        runs: list[SmokeResult],
    ) -> "VarianceReport":
        report = cls(
            spec_name=spec_name,
            model_label=model_label,
            model_id=model_id,
            runs=list(runs),
        )
        report._recompute()
        return report

    def _recompute(self) -> None:
        self.converged_count = sum(1 for r in self.runs if r.is_converged())
        # `failed` here is the strict daemon-side failure verdict; the
        # `error` field captures dispatch-time exceptions which also
        # surface as verdict="failed" in our SmokeResult (the runner
        # constructs the result that way). Keep the count specifically
        # for non-converged-and-not-timed-out runs.
        self.failed_count = sum(
            1 for r in self.runs
            if not r.is_converged() and not r.timed_out
        )
        self.timed_out_count = sum(1 for r in self.runs if r.timed_out)

        totals = [r.total_elapsed_seconds for r in self.runs]
        if totals:
            self.total_elapsed_seconds_median = _median(totals)
            self.total_elapsed_seconds_min = min(totals)
            self.total_elapsed_seconds_max = max(totals)
            self.total_elapsed_seconds_stddev = _stddev(totals)

        # Pool iter durations across ALL runs for a population-level
        # view of how long an iteration takes for this (spec, model).
        all_iter_durations: list[float] = []
        for r in self.runs:
            all_iter_durations.extend(r.iter_durations_seconds)
        if all_iter_durations:
            self.iter_duration_seconds_median = _median(all_iter_durations)
            self.iter_duration_seconds_stddev = _stddev(all_iter_durations)

        counts: dict[str, int] = {}
        for r in self.runs:
            key = r.stop_reason or r.verdict or "unknown"
            counts[key] = counts.get(key, 0) + 1
        self.stop_reason_counts = counts

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def convergence_rate(self) -> float:
        """0.0 to 1.0. Aim for 1.0 (every run converged) before
        cutting a release."""
        if not self.runs:
            return 0.0
        return self.converged_count / len(self.runs)

    def to_dict(self) -> dict:
        return {
            "spec_name": self.spec_name,
            "model_label": self.model_label,
            "model_id": self.model_id,
            "n": self.n,
            "converged_count": self.converged_count,
            "failed_count": self.failed_count,
            "timed_out_count": self.timed_out_count,
            "convergence_rate": self.convergence_rate,
            "total_elapsed_seconds": {
                "median": self.total_elapsed_seconds_median,
                "min": self.total_elapsed_seconds_min,
                "max": self.total_elapsed_seconds_max,
                "stddev": self.total_elapsed_seconds_stddev,
            },
            "iter_duration_seconds": {
                "median": self.iter_duration_seconds_median,
                "stddev": self.iter_duration_seconds_stddev,
                "sample_size": sum(
                    len(r.iter_durations_seconds) for r in self.runs
                ),
            },
            "stop_reason_counts": dict(self.stop_reason_counts),
            "runs": [r.to_dict() for r in self.runs],
        }


# ── Public functions ───────────────────────────────────────────────────


def summarize_runs(runs: list[SmokeResult]) -> VarianceReport:
    """Build a VarianceReport from a list of runs you've already done.
    Useful when you want to combine smokes from multiple sessions or
    when scripting the runs externally."""
    if not runs:
        raise ValueError("Cannot summarize an empty run list.")
    spec = runs[0].spec_name
    model_label = runs[0].model_label
    model_id = runs[0].model_id
    # All runs should share spec + model; if not, the caller composed
    # something pathological. Fail loud.
    for r in runs[1:]:
        if r.spec_name != spec or r.model_label != model_label:
            raise ValueError(
                f"Runs must share spec + model. Got spec={r.spec_name!r}/"
                f"{spec!r} and model={r.model_label!r}/{model_label!r}."
            )
    return VarianceReport.from_runs(
        spec_name=spec, model_label=model_label, model_id=model_id, runs=runs,
    )


def run_variance(
    *,
    spec_name: str,
    model_label: str,
    n: int,
    smoke_timeout_minutes: int = 25,
    on_run_complete=None,
) -> VarianceReport:
    """Run the same (spec, model) `n` times and return a
    VarianceReport. Each run gets a fresh project dir + intent_id
    suffix so they don't collide.

    `on_run_complete(idx, result)` fires after each run if provided,
    so the CLI can stream progress while a long variance batch runs.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    runs: list[SmokeResult] = []
    for i in range(n):
        # Suffix the intent_id so the harness distinguishes runs in
        # logs / persisted roadmaps. The tempdir bootstrap already
        # gives each run its own project directory.
        from .specs import get_spec
        spec = get_spec(spec_name)
        intent_id = f"{spec.intent_id_prefix}-{model_label}-r{i + 1}"
        result = run_smoke(
            spec_name=spec_name,
            model_label=model_label,
            smoke_timeout_minutes=smoke_timeout_minutes,
            intent_id=intent_id,
        )
        runs.append(result)
        if on_run_complete is not None:
            try:
                on_run_complete(i + 1, result)
            except Exception:
                logger.debug("on_run_complete raised", exc_info=True)
    # All runs share spec + model by construction here, so we can build
    # the report directly without going through summarize_runs's
    # validation (which would just re-do the same checks).
    from .runner import MODELS
    return VarianceReport.from_runs(
        spec_name=spec_name,
        model_label=model_label,
        model_id=MODELS[model_label],
        runs=runs,
    )
