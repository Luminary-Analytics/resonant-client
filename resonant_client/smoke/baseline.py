"""
Smoke-run baselines and diffing.

A variance report tells you the absolute convergence rate + timing of
N runs. What you actually want before tagging a release is the
DELTA: "after this change, did wordcount-pro converge faster, slower,
or about the same?" That's the question baselines answer.

Workflow:
1. Run a variance against a known-good build:
       resonant-smoke variance --spec wordcount --model pro --n 3
2. Promote that JSON to the baseline:
       resonant-smoke baseline set --spec wordcount --model pro \\
         --from smoke-runs/variance-wordcount-pro-n3-<ts>.json
3. After a code change, run variance with `--diff-baseline`:
       resonant-smoke variance --spec wordcount --model pro --n 3 \\
         --diff-baseline
   The markdown report adds a "Diff vs baseline" section:
   convergence-rate delta, timing-median delta, regressions called out.

Baselines live under `<project>/.resonant/smoke-baselines/<spec>-<model>.json`
by default. Per-project so different projects can have their own
acceptable convergence profile (some users may want flash on simple
specs and pro on complex ones; baselines respect that).

Pure-data module — no I/O outside `load_baseline` / `save_baseline`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .variance import VarianceReport

logger = logging.getLogger(__name__)


# ── Disk layout ─────────────────────────────────────────────────────────


def baseline_dir(project_path: str | Path) -> Path:
    """Where this project's baselines live. Per-project so different
    repos / specs can have different "acceptable" convergence profiles
    without trampling each other."""
    return Path(project_path) / ".resonant" / "smoke-baselines"


def baseline_path(project_path: str | Path, spec: str, model: str) -> Path:
    """Canonical path for a (spec, model) baseline."""
    return baseline_dir(project_path) / f"{spec}-{model}.json"


def save_baseline(
    report: VarianceReport,
    *,
    project_path: str | Path,
) -> Path:
    """Persist a variance report as the baseline for its (spec, model).
    Overwrites silently — the old baseline is gone. Caller's job to
    confirm if there's an existing one."""
    target = baseline_path(project_path, report.spec_name, report.model_label)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), indent=2),
        encoding="utf-8",
    )
    return target


def load_baseline(
    *,
    project_path: str | Path,
    spec: str,
    model: str,
) -> Optional[dict]:
    """Read the persisted baseline. Returns the raw dict (matches
    `VarianceReport.to_dict()`) or None if no baseline exists.

    Returns the dict rather than reconstructing a `VarianceReport`
    because the dict carries exactly the rolled-up stats we need;
    rebuilding the full `runs` list of `SmokeResult` objects would
    require a constructor that handles missing fields gracefully,
    and we're only using these for diffs."""
    p = baseline_path(project_path, spec, model)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load baseline at %s: %s", p, exc)
        return None


def list_baselines(project_path: str | Path) -> list[dict]:
    """Enumerate the project's baselines. Returns dicts with the
    (spec, model, n, convergence_rate, total_elapsed_seconds_median,
    path) summary fields — enough for the CLI's `baseline list` to
    print a table. Skips files we can't parse rather than crashing
    (forward-compat with future schema versions)."""
    out: list[dict] = []
    base = baseline_dir(project_path)
    if not base.exists():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "spec": data.get("spec_name", ""),
            "model": data.get("model_label", ""),
            "n": data.get("n", 0),
            "convergence_rate": data.get("convergence_rate", 0.0),
            "total_elapsed_seconds_median": (
                (data.get("total_elapsed_seconds") or {}).get("median")
            ),
            "path": str(p),
        })
    return out


# ── Diff containers ─────────────────────────────────────────────────────


@dataclass
class BaselineDiff:
    """The shape of a current-vs-baseline comparison.

    Field semantics:
    - `delta_*` fields are `current - baseline`. Positive on convergence_rate
      means improvement; positive on a duration means regression.
    - `regressions` is a list of free-form strings describing concerning
      changes (rate dropped, timing grew >X%) — caller-friendly for
      paste-into-PR review.
    - `improvements` is the corresponding "got better" list.
    """
    spec_name: str
    model_label: str
    baseline_n: int
    current_n: int

    baseline_convergence_rate: float = 0.0
    current_convergence_rate: float = 0.0
    delta_convergence_rate: float = 0.0

    baseline_total_elapsed_median: Optional[float] = None
    current_total_elapsed_median: Optional[float] = None
    delta_total_elapsed_median: Optional[float] = None

    baseline_iter_duration_median: Optional[float] = None
    current_iter_duration_median: Optional[float] = None
    delta_iter_duration_median: Optional[float] = None

    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        return bool(self.regressions)

    def to_dict(self) -> dict:
        return {
            "spec_name": self.spec_name,
            "model_label": self.model_label,
            "baseline_n": self.baseline_n,
            "current_n": self.current_n,
            "baseline_convergence_rate": self.baseline_convergence_rate,
            "current_convergence_rate": self.current_convergence_rate,
            "delta_convergence_rate": self.delta_convergence_rate,
            "baseline_total_elapsed_median": self.baseline_total_elapsed_median,
            "current_total_elapsed_median": self.current_total_elapsed_median,
            "delta_total_elapsed_median": self.delta_total_elapsed_median,
            "baseline_iter_duration_median": self.baseline_iter_duration_median,
            "current_iter_duration_median": self.current_iter_duration_median,
            "delta_iter_duration_median": self.delta_iter_duration_median,
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "has_regressions": self.has_regressions,
        }


# ── Diff computation ────────────────────────────────────────────────────


# A duration delta is "concerning" if it's both:
#  - >= 20% worse than the baseline (relative), AND
#  - >= 10s in absolute terms.
# Both bars matter: 20% of a 5s baseline is noise; 10s of absolute is
# below human-perceptible-impact for most workflows.
_REGRESSION_RELATIVE_THRESHOLD = 0.20
_REGRESSION_ABSOLUTE_THRESHOLD_SECONDS = 10.0

# Same threshold pair, applied to "got better" detection. Symmetric so
# the report calls out big speedups too.
_IMPROVEMENT_RELATIVE_THRESHOLD = 0.20
_IMPROVEMENT_ABSOLUTE_THRESHOLD_SECONDS = 10.0


def _safe_subtract(
    current: Optional[float], baseline: Optional[float],
) -> Optional[float]:
    """Subtract with None-tolerance. Returns None if either side is None."""
    if current is None or baseline is None:
        return None
    return current - baseline


def _is_regression(delta: float, baseline: float) -> bool:
    """True if `delta` (current - baseline) crosses both regression bars.
    `delta > 0` means current is slower than baseline."""
    if delta is None or baseline is None or baseline <= 0:
        return False
    if delta < _REGRESSION_ABSOLUTE_THRESHOLD_SECONDS:
        return False
    return (delta / baseline) >= _REGRESSION_RELATIVE_THRESHOLD


def _is_improvement(delta: float, baseline: float) -> bool:
    """True if `delta` crosses both improvement bars (current is faster).
    `delta < 0` means current is faster than baseline."""
    if delta is None or baseline is None or baseline <= 0:
        return False
    speedup = -delta  # positive when current is faster
    if speedup < _IMPROVEMENT_ABSOLUTE_THRESHOLD_SECONDS:
        return False
    return (speedup / baseline) >= _IMPROVEMENT_RELATIVE_THRESHOLD


def diff_against_baseline(
    *, current: VarianceReport, baseline: dict,
) -> BaselineDiff:
    """Compute `current - baseline`. Both parameters refer to the SAME
    (spec, model) — the caller is responsible for picking the right
    baseline. Returns a `BaselineDiff` with deltas + the regression /
    improvement narrative."""
    if (current.spec_name != baseline.get("spec_name")
            or current.model_label != baseline.get("model_label")):
        raise ValueError(
            f"Baseline mismatch: current=({current.spec_name}, "
            f"{current.model_label}) vs baseline=({baseline.get('spec_name')}, "
            f"{baseline.get('model_label')})"
        )

    baseline_total = (baseline.get("total_elapsed_seconds") or {}).get("median")
    current_total = current.total_elapsed_seconds_median
    baseline_iter = (baseline.get("iter_duration_seconds") or {}).get("median")
    current_iter = current.iter_duration_seconds_median
    baseline_rate = float(baseline.get("convergence_rate", 0.0))

    diff = BaselineDiff(
        spec_name=current.spec_name,
        model_label=current.model_label,
        baseline_n=int(baseline.get("n", 0)),
        current_n=current.n,
        baseline_convergence_rate=baseline_rate,
        current_convergence_rate=current.convergence_rate,
        delta_convergence_rate=current.convergence_rate - baseline_rate,
        baseline_total_elapsed_median=baseline_total,
        current_total_elapsed_median=current_total,
        delta_total_elapsed_median=_safe_subtract(current_total, baseline_total),
        baseline_iter_duration_median=baseline_iter,
        current_iter_duration_median=current_iter,
        delta_iter_duration_median=_safe_subtract(current_iter, baseline_iter),
    )

    # ── Narrative: regressions ────────────────────────────────────
    if diff.delta_convergence_rate < -1e-9:
        # ANY drop in convergence rate is concerning — no relative
        # threshold, this is the headline metric.
        diff.regressions.append(
            f"Convergence rate dropped from "
            f"{baseline_rate * 100:.0f}% to "
            f"{current.convergence_rate * 100:.0f}%"
        )
    if (diff.delta_total_elapsed_median is not None
            and _is_regression(diff.delta_total_elapsed_median, baseline_total or 0)):
        diff.regressions.append(
            f"Total-elapsed median grew by {diff.delta_total_elapsed_median:.1f}s "
            f"({(diff.delta_total_elapsed_median / (baseline_total or 1)) * 100:+.0f}% "
            f"vs baseline {baseline_total:.1f}s)"
        )
    if (diff.delta_iter_duration_median is not None
            and _is_regression(diff.delta_iter_duration_median, baseline_iter or 0)):
        diff.regressions.append(
            f"Iter-duration median grew by {diff.delta_iter_duration_median:.1f}s "
            f"({(diff.delta_iter_duration_median / (baseline_iter or 1)) * 100:+.0f}% "
            f"vs baseline {baseline_iter:.1f}s)"
        )

    # ── Narrative: improvements ───────────────────────────────────
    if diff.delta_convergence_rate > 1e-9:
        diff.improvements.append(
            f"Convergence rate rose from "
            f"{baseline_rate * 100:.0f}% to "
            f"{current.convergence_rate * 100:.0f}%"
        )
    if (diff.delta_total_elapsed_median is not None
            and _is_improvement(diff.delta_total_elapsed_median, baseline_total or 0)):
        diff.improvements.append(
            f"Total-elapsed median dropped by {-diff.delta_total_elapsed_median:.1f}s "
            f"({(diff.delta_total_elapsed_median / (baseline_total or 1)) * 100:+.0f}% "
            f"vs baseline {baseline_total:.1f}s)"
        )
    if (diff.delta_iter_duration_median is not None
            and _is_improvement(diff.delta_iter_duration_median, baseline_iter or 0)):
        diff.improvements.append(
            f"Iter-duration median dropped by {-diff.delta_iter_duration_median:.1f}s "
            f"({(diff.delta_iter_duration_median / (baseline_iter or 1)) * 100:+.0f}% "
            f"vs baseline {baseline_iter:.1f}s)"
        )

    return diff
