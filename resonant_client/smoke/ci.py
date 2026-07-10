"""
v0.5.5a3 — `resonant-smoke ci` subcommand.

A curated suite shaped for CI / cron environments. Runs a fixed set of
specs (default: `minimal` + `wordcount`) against a single model.
Optionally diffs each spec's variance against a baseline and gates the
exit code on the combined result.

Design goals:
- **Predictable runtime**: defaults skip `roguelite` since it's
  ~3-7 minutes per run. CI budgets care.
- **Fail-fast on convergence drops**: if ANY spec fails to converge,
  exit code is non-zero. Catches regressions before they ship.
- **Baseline-aware**: with `--diff-baseline`, also fails on any
  REGRESSION even if the spec converged. Subtle slowdowns matter.
- **Machine-readable artifacts**: JSON record + markdown report are
  always emitted. CI runners can pipeline them.

Usage:
    resonant-smoke ci --model pro
    resonant-smoke ci --model pro --specs minimal,wordcount
    resonant-smoke ci --model pro --diff-baseline
    resonant-smoke ci --model pro --n 1                  # single-shot per spec

Default behavior (no flags): n=1 per spec (fast smoke), no baseline.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .baseline import BaselineDiff, diff_against_baseline, load_baseline
from .runner import MODELS, SmokeResult, run_smoke
from .specs import get_spec, list_spec_names
from .variance import VarianceReport, summarize_runs

logger = logging.getLogger(__name__)


# ── Default suite ──────────────────────────────────────────────────────


# The default suite is intentionally narrow. CI should run cheap; users
# wanting comprehensive validation can pass `--specs minimal,wordcount,roguelite`.
# `minimal` proves the harness works at all; `wordcount` is the v0.5.x
# convergence canary.
DEFAULT_CI_SPECS: tuple[str, ...] = ("minimal", "wordcount")


# ── Per-spec result ────────────────────────────────────────────────────


@dataclass
class CISpecResult:
    """One spec's outcome inside a CI suite run."""
    spec_name: str
    variance: VarianceReport
    baseline_diff: Optional[BaselineDiff] = None
    skipped: bool = False
    skipped_reason: str = ""

    @property
    def converged(self) -> bool:
        return self.variance.convergence_rate >= 1.0

    @property
    def has_regressions(self) -> bool:
        return self.baseline_diff is not None and self.baseline_diff.has_regressions

    @property
    def passed(self) -> bool:
        """Spec passes iff converged AND no baseline regressions."""
        if self.skipped:
            return True  # skipped specs don't fail the suite
        return self.converged and not self.has_regressions

    def to_dict(self) -> dict:
        return {
            "spec_name": self.spec_name,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
            "passed": self.passed,
            "converged": self.converged,
            "has_regressions": self.has_regressions,
            "variance": self.variance.to_dict() if not self.skipped else None,
            "baseline_diff": (
                self.baseline_diff.to_dict() if self.baseline_diff else None
            ),
        }


# ── Suite-level result ─────────────────────────────────────────────────


@dataclass
class CISuiteResult:
    """The shape of one `ci` invocation."""
    model_label: str
    model_id: str
    started_at_epoch: float
    total_elapsed_seconds: float
    spec_results: list[CISpecResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.spec_results)

    @property
    def passing_count(self) -> int:
        return sum(1 for r in self.spec_results if r.passed)

    @property
    def has_any_regression(self) -> bool:
        return any(r.has_regressions for r in self.spec_results)

    def exit_code(self) -> int:
        """0 if every (non-skipped) spec converged AND no regressions
        surfaced; 1 otherwise. Maps cleanly to CI runner conventions."""
        return 0 if self.all_passed else 1

    def to_dict(self) -> dict:
        return {
            "model_label": self.model_label,
            "model_id": self.model_id,
            "started_at_epoch": self.started_at_epoch,
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "spec_count": len(self.spec_results),
            "passing_count": self.passing_count,
            "all_passed": self.all_passed,
            "has_any_regression": self.has_any_regression,
            "exit_code": self.exit_code(),
            "spec_results": [r.to_dict() for r in self.spec_results],
        }


# ── Runner ─────────────────────────────────────────────────────────────


def parse_specs_arg(specs_csv: str) -> list[str]:
    """Parse a `--specs` CSV value into a validated list of spec names.
    Raises ValueError on unknown specs (CI should fail loud, not run a
    surprise empty suite)."""
    requested = [s.strip() for s in specs_csv.split(",") if s.strip()]
    if not requested:
        raise ValueError(
            "--specs must list at least one spec name (comma-separated)"
        )
    valid = set(list_spec_names())
    unknown = [s for s in requested if s not in valid]
    if unknown:
        raise ValueError(
            f"Unknown spec(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(valid))}"
        )
    return requested


def run_ci_suite(
    *,
    model_label: str,
    spec_names: list[str],
    n: int = 1,
    smoke_timeout_minutes: int = 25,
    diff_baseline: bool = False,
    project_path: Optional[Path] = None,
    on_spec_complete=None,
) -> CISuiteResult:
    """Run the curated suite against a model. One variance per spec.

    Parameters:
    - `n`: runs per spec. Default 1 (fast smoke); for ship-readiness
      gating bump to 3 (the same threshold `variance` defaults to).
    - `diff_baseline`: when set, look up the per-(spec, model) baseline
      and compute a diff. Missing baselines are fine — just no diff.
    - `on_spec_complete(spec_name, spec_result)` fires after each spec.
    """
    if model_label not in MODELS:
        raise ValueError(
            f"Unknown model label {model_label!r}. "
            f"Valid: {', '.join(sorted(MODELS))}"
        )
    if not spec_names:
        raise ValueError("spec_names must contain at least one spec")
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    project_root = project_path or Path.cwd()
    started_at = time.time()
    spec_results: list[CISpecResult] = []

    for spec_name in spec_names:
        # Validate ahead of running (cheap; surfaces typos early).
        get_spec(spec_name)

        runs: list[SmokeResult] = []
        for i in range(n):
            intent_id = f"ci-{spec_name}-{model_label}-r{i + 1}"
            result = run_smoke(
                spec_name=spec_name,
                model_label=model_label,
                smoke_timeout_minutes=smoke_timeout_minutes,
                intent_id=intent_id,
            )
            runs.append(result)

        variance = summarize_runs(runs)

        baseline_diff: Optional[BaselineDiff] = None
        if diff_baseline:
            baseline = load_baseline(
                project_path=project_root,
                spec=spec_name,
                model=model_label,
            )
            if baseline is not None:
                baseline_diff = diff_against_baseline(
                    current=variance, baseline=baseline,
                )
            # No baseline → no diff. Not an error: the spec might be
            # newly added to the suite. Surfaced via `baseline_diff is None`.

        spec_result = CISpecResult(
            spec_name=spec_name,
            variance=variance,
            baseline_diff=baseline_diff,
        )
        spec_results.append(spec_result)
        if on_spec_complete is not None:
            try:
                on_spec_complete(spec_name, spec_result)
            except Exception:
                logger.debug("on_spec_complete raised", exc_info=True)

    total_elapsed = time.time() - started_at
    return CISuiteResult(
        model_label=model_label,
        model_id=MODELS[model_label],
        started_at_epoch=started_at,
        total_elapsed_seconds=total_elapsed,
        spec_results=spec_results,
    )


# ── Markdown rendering ─────────────────────────────────────────────────


def render_ci_markdown(result: CISuiteResult) -> str:
    """Markdown summary of a CI suite run.

    Layout:
    - Headline: `K of N specs passed` + total elapsed
    - Per-spec table: spec, status, convergence, median elapsed,
      regression count
    - Per-spec diff details (when baseline_diff is present and has
      regressions / improvements)
    """
    lines: list[str] = []
    lines.append(f"# CI suite — `{result.model_label}`")
    lines.append("")
    sigil = "✅" if result.all_passed else "✗"
    lines.append(
        f"**{sigil} {result.passing_count} of {len(result.spec_results)} specs passed** "
        f"· total {_fmt_compact_duration(result.total_elapsed_seconds)}"
    )
    if result.has_any_regression:
        regs = sum(
            1 for r in result.spec_results if r.has_regressions
        )
        lines.append(f"- ⚠ {regs} spec(s) showed regression vs baseline")
    lines.append("")

    # Per-spec table
    lines.append("## Specs")
    lines.append("")
    lines.append("| Spec | Status | Convergence | Median elapsed | Regressions |")
    lines.append("|---|---|---|---|---|")
    for r in result.spec_results:
        if r.skipped:
            lines.append(
                f"| `{r.spec_name}` | ⏭ skipped | - | - | - |"
            )
            continue
        status = "✅ pass" if r.passed else (
            "⚠ regressed" if r.has_regressions else "✗ fail"
        )
        conv = (
            f"{r.variance.converged_count}/{r.variance.n} "
            f"({int(round(r.variance.convergence_rate * 100))}%)"
        )
        median = _fmt_compact_duration(r.variance.total_elapsed_seconds_median)
        reg_count = (
            len(r.baseline_diff.regressions) if r.baseline_diff else 0
        )
        lines.append(
            f"| `{r.spec_name}` | {status} | {conv} | {median} | {reg_count} |"
        )
    lines.append("")

    # Diff details for any spec with a baseline_diff that has narrative
    diff_specs = [
        r for r in result.spec_results
        if r.baseline_diff is not None
        and (r.baseline_diff.regressions or r.baseline_diff.improvements)
    ]
    if diff_specs:
        lines.append("## Baseline diffs")
        lines.append("")
        for r in diff_specs:
            lines.append(f"### `{r.spec_name}`")
            for reg in r.baseline_diff.regressions:
                lines.append(f"- ⚠ {reg}")
            for imp in r.baseline_diff.improvements:
                lines.append(f"- ✅ {imp}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fmt_compact_duration(seconds: Optional[float]) -> str:
    """Local copy — avoid importing from report.py to keep ci.py
    self-contained (it can be invoked from CI without the full
    smoke package surface)."""
    if seconds is None or not isinstance(seconds, (int, float)):
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"
