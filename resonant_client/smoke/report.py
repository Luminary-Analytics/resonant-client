"""
Markdown renderers for `SmokeResult` and `VarianceReport`.

The JSON artifacts the harness writes are great for tooling but
unfriendly for humans skimming a PR description or pasting into
a release-notes doc. These pure functions emit a human-readable
markdown summary of the same data.

Same shape both ways: a heading, a one-line TL;DR, and the structured
detail underneath. Single-run reports are short (one card); variance
reports include a per-run table + timing distribution.

Used by:
    resonant-smoke run --spec X --model Y --report path.md
    resonant-smoke variance --spec X --model Y --report path.md

Pure functions — no I/O. Caller writes to disk.
"""
from __future__ import annotations

from typing import Optional

from .runner import SmokeResult
from .variance import VarianceReport


def _fmt_duration(seconds: Optional[float]) -> str:
    """Compact duration string. None / non-numeric → `'-'`."""
    if seconds is None or not isinstance(seconds, (int, float)):
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def _verdict_badge(result: SmokeResult) -> str:
    """Short status label with a leading sigil."""
    if result.is_converged():
        return "✅ converged"
    if result.timed_out:
        return "⚠ timed out"
    if result.error:
        return "✗ dispatch error"
    return f"✗ {result.verdict or 'unknown'}"


# ── Single-run report ──────────────────────────────────────────────────


def render_run_markdown(result: SmokeResult) -> str:
    """Render a single SmokeResult as a markdown card.

    Suitable for pasting into a PR description or release notes when
    one mission is the unit of evidence (e.g. "the inspector wired up
    cleanly — wordcount converged in 142s with this build").
    """
    lines: list[str] = []
    lines.append(f"# Smoke run — `{result.spec_name}` × `{result.model_label}`")
    lines.append("")
    lines.append(f"**{_verdict_badge(result)}** · "
                 f"{_fmt_duration(result.total_elapsed_seconds)} total · "
                 f"{result.iter_count} iter(s) · "
                 f"verdict=`{result.verdict or '-'}` · "
                 f"stop=`{result.stop_reason or '-'}`")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Spec | `{result.spec_name}` |")
    lines.append(f"| Model | `{result.model_label}` (`{result.model_id}`) |")
    lines.append(f"| Total elapsed | {_fmt_duration(result.total_elapsed_seconds)} |")
    lines.append(f"| Daemon elapsed | {_fmt_duration(result.daemon_elapsed_seconds)} |")
    lines.append(f"| Verdict | `{result.verdict or '-'}` |")
    lines.append(f"| Stop reason | `{result.stop_reason or '-'}` |")
    lines.append(f"| Iter (started/done/failed) | "
                 f"{result.iter_started} / {result.iter_complete} / {result.iter_failed} |")
    lines.append(f"| Reflections | {result.reflection_count} |")
    avg = result.avg_iter_duration_seconds()
    lines.append(f"| Avg iter duration | {_fmt_duration(avg)} |")
    if result.timed_out:
        lines.append(f"| Timed out | yes |")
    if result.error:
        # Backtick-escape the error so leading `<` etc don't break formatting.
        lines.append(f"| Error | `{result.error}` |")
    lines.append("")
    if result.iter_durations_seconds:
        joined = ", ".join(_fmt_duration(d) for d in result.iter_durations_seconds)
        lines.append(f"**Iter durations:** {joined}")
        lines.append("")
    if result.project_path:
        lines.append(f"**Project:** `{result.project_path}`")
    if result.roadmap_path:
        lines.append(f"**Roadmap:** `{result.roadmap_path}`")
    return "\n".join(lines).rstrip() + "\n"


# ── Variance report ────────────────────────────────────────────────────


def render_variance_markdown(report: VarianceReport) -> str:
    """Render a multi-run VarianceReport as a markdown summary.

    Includes:
    - Headline: convergence rate as `K of N`
    - Stat rollup: total-elapsed median/min/max/stddev, pooled iter
      duration median + stddev
    - Stop-reason histogram
    - Per-run table (one row per run)
    """
    lines: list[str] = []
    lines.append(
        f"# Variance — `{report.spec_name}` × `{report.model_label}` × n={report.n}"
    )
    lines.append("")

    # Headline
    pct = int(round(report.convergence_rate * 100))
    headline_sigil = "✅" if report.convergence_rate >= 1.0 else (
        "⚠" if report.convergence_rate >= 0.5 else "✗"
    )
    lines.append(
        f"**{headline_sigil} {report.converged_count} of {report.n} converged** "
        f"({pct}% convergence rate)"
    )
    if report.timed_out_count:
        lines.append(f"- {report.timed_out_count} timed out")
    if report.failed_count:
        lines.append(f"- {report.failed_count} non-converging non-timeout failures")
    lines.append("")

    # Timing rollup
    lines.append("## Timing")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total elapsed (median) | {_fmt_duration(report.total_elapsed_seconds_median)} |")
    lines.append(f"| Total elapsed (min) | {_fmt_duration(report.total_elapsed_seconds_min)} |")
    lines.append(f"| Total elapsed (max) | {_fmt_duration(report.total_elapsed_seconds_max)} |")
    lines.append(f"| Total elapsed (stddev) | {_fmt_duration(report.total_elapsed_seconds_stddev)} |")
    lines.append(f"| Iter duration (median) | {_fmt_duration(report.iter_duration_seconds_median)} |")
    lines.append(f"| Iter duration (stddev) | {_fmt_duration(report.iter_duration_seconds_stddev)} |")
    lines.append("")

    # Stop-reason histogram
    if report.stop_reason_counts:
        lines.append("## Stop reasons")
        lines.append("")
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for reason, count in sorted(
            report.stop_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(f"| `{reason}` | {count} |")
        lines.append("")

    # Per-run table
    if report.runs:
        lines.append("## Per-run detail")
        lines.append("")
        lines.append("| # | Status | Total | Iters | Verdict | Stop reason |")
        lines.append("|---|---|---|---|---|---|")
        for i, r in enumerate(report.runs, start=1):
            status = _verdict_badge(r)
            lines.append(
                f"| {i} | {status} | "
                f"{_fmt_duration(r.total_elapsed_seconds)} | "
                f"{r.iter_count} | "
                f"`{r.verdict or '-'}` | "
                f"`{r.stop_reason or '-'}` |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
