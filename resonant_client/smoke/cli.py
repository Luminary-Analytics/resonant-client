"""
`resonant-smoke` CLI entry point.

Subcommands:
  list-specs          Show registered specs.
  run                 Run a single smoke and print the result.
  variance            Run N smokes against the same (spec, model) and
                      summarize convergence + variance.

All subcommands write a JSON record to disk (override path with
`--out`). The `variance` subcommand additionally prints a one-line
summary per run as it streams, then a final report block.

Example:
    resonant-smoke list-specs
    resonant-smoke run --spec wordcount --model pro
    resonant-smoke variance --spec wordcount --model pro --n 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .baseline import (
    baseline_path,
    diff_against_baseline,
    list_baselines,
    load_baseline,
    save_baseline,
)
from .ci import (
    DEFAULT_CI_SPECS,
    CISpecResult,
    CISuiteResult,
    parse_specs_arg,
    render_ci_markdown,
    run_ci_suite,
)
from .report import render_run_markdown, render_variance_markdown
from .runner import MODELS, SmokeResult, run_smoke
from .specs import SPECS, get_spec, list_spec_names
from .variance import VarianceReport, run_variance


def _ts_label() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _default_record_path(prefix: str) -> Path:
    """Default location for run records. Lives under the cwd in
    `smoke-runs/` so it's visible in the git status of the project
    being smoked. Caller can override via `--out`."""
    out = Path("smoke-runs")
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{prefix}-{_ts_label()}.json"


# ── list-specs ─────────────────────────────────────────────────────────


def _cmd_list_specs(_args: argparse.Namespace) -> int:
    print("Available smoke specs:")
    print()
    has_unvalidated = False
    for name in list_spec_names():
        spec = SPECS[name]
        # v0.5.8a4 — surface the validated flag so users know which
        # specs have pinned convergence numbers and which are still
        # awaiting their first live-model run.
        marker = "" if spec.validated else " [unvalidated]"
        print(f"  {name:<14}{marker}  {spec.description}")
        if not spec.validated:
            has_unvalidated = True
    print()
    if has_unvalidated:
        print(
            "Note: specs marked [unvalidated] haven't been smoke-validated "
            "against a live model yet. Convergence/timing numbers are "
            "estimates; expect to refine `expected_iter_seconds` after the "
            "first run."
        )
        print()
    print(f"Models: {', '.join(sorted(MODELS))}")
    return 0


# ── run ────────────────────────────────────────────────────────────────


def _print_run_header(spec_name: str, model_label: str) -> None:
    print("=" * 70)
    print(f"SMOKE — spec={spec_name} model={model_label}")
    print("=" * 70)


def _print_run_result(result: SmokeResult) -> None:
    converged = "✅" if result.is_converged() else (
        "[unvalidated] TIMEOUT" if result.timed_out else "✗"
    )
    print()
    print("-" * 70)
    print(f"{converged}  spec={result.spec_name} model={result.model_label}")
    print(f"  verdict:               {result.verdict}")
    print(f"  stop_reason:           {result.stop_reason}")
    print(f"  iter started/done/failed: "
          f"{result.iter_started}/{result.iter_complete}/{result.iter_failed}")
    print(f"  reflections:           {result.reflection_count}")
    print(f"  total elapsed:         {result.total_elapsed_seconds:.1f}s")
    avg = result.avg_iter_duration_seconds()
    if avg is not None:
        print(f"  avg iter duration:     {avg:.1f}s")
    print(f"  project:               {result.project_path}")
    if result.error:
        print(f"  error:                 {result.error}")


def _cmd_run(args: argparse.Namespace) -> int:
    _print_run_header(args.spec, args.model)
    if args.inject_planner_failure:
        print("  [unvalidated] --inject-planner-failure: first planner call will be")
        print("    corrupted; walker should spawn one retry to recover.")
        print()
    result = run_smoke(
        spec_name=args.spec,
        model_label=args.model,
        smoke_timeout_minutes=args.timeout_minutes,
        inject_planner_failure=args.inject_planner_failure,
    )
    _print_run_result(result)

    out_path = Path(args.out) if args.out else _default_record_path(
        f"smoke-{args.spec}-{args.model}"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    print(f"  → record: {out_path}")

    # v0.5.4a3 — markdown report. When `--report` is set, write the
    # human-readable summary to that path. When omitted, write
    # alongside the JSON with a `.md` suffix so a single CLI run
    # always produces both artifacts (cheap to emit; useful in PR
    # descriptions even when the user didn't explicitly ask for it).
    md_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_run_markdown(result), encoding="utf-8")
    print(f"  → markdown: {md_path}")
    return 0 if result.is_converged() else 1


# ── variance ───────────────────────────────────────────────────────────


def _print_variance_report(report: VarianceReport) -> None:
    print()
    print("=" * 70)
    print(f"VARIANCE REPORT — spec={report.spec_name} model={report.model_label} n={report.n}")
    print("=" * 70)
    print(f"  converged:        {report.converged_count}/{report.n} "
          f"({report.convergence_rate * 100:.0f}%)")
    print(f"  failed:           {report.failed_count}")
    print(f"  timed out:        {report.timed_out_count}")
    if report.total_elapsed_seconds_median is not None:
        print(
            f"  total elapsed:    median={report.total_elapsed_seconds_median:.1f}s "
            f"min={report.total_elapsed_seconds_min:.1f}s "
            f"max={report.total_elapsed_seconds_max:.1f}s "
            f"stddev={report.total_elapsed_seconds_stddev:.1f}s"
        )
    if report.iter_duration_seconds_median is not None:
        print(
            f"  iter duration:    median={report.iter_duration_seconds_median:.1f}s "
            f"stddev={report.iter_duration_seconds_stddev:.1f}s"
        )
    if report.stop_reason_counts:
        print("  stop reasons:")
        for reason, count in sorted(
            report.stop_reason_counts.items(), key=lambda kv: -kv[1],
        ):
            print(f"    {reason:<20} {count}")


def _cmd_variance(args: argparse.Namespace) -> int:
    _print_run_header(args.spec, args.model)
    print(f"Running {args.n} smoke(s)…")
    print()

    def _on_run_complete(idx: int, result: SmokeResult) -> None:
        verdict_label = "✅" if result.is_converged() else (
            "[unvalidated]TO" if result.timed_out else "✗"
        )
        print(
            f"  run {idx}/{args.n}: {verdict_label} verdict={result.verdict} "
            f"elapsed={result.total_elapsed_seconds:.0f}s "
            f"iters={result.iter_count}"
        )

    report = run_variance(
        spec_name=args.spec,
        model_label=args.model,
        n=args.n,
        smoke_timeout_minutes=args.timeout_minutes,
        on_run_complete=_on_run_complete,
    )
    _print_variance_report(report)

    # v0.5.5a1 — optional baseline diff. When `--diff-baseline` is set
    # and a baseline exists for this (spec, model), compute the delta;
    # the markdown report includes it as a "Diff vs baseline" section.
    # We also print the regression / improvement narratives to stdout
    # so the diff is visible without opening the .md file.
    diff = None
    if args.diff_baseline:
        project_root = Path.cwd()
        baseline_data = load_baseline(
            project_path=project_root,
            spec=args.spec,
            model=args.model,
        )
        if baseline_data is None:
            print()
            print(f"  [unvalidated] --diff-baseline: no baseline found at "
                  f"{baseline_path(project_root, args.spec, args.model)}")
            print(f"    Run `resonant-smoke baseline set --spec {args.spec} "
                  f"--model {args.model} --from <variance.json>` first.")
        else:
            diff = diff_against_baseline(current=report, baseline=baseline_data)
            print()
            print(f"  Diff vs baseline (n={diff.baseline_n}):")
            print(f"    convergence: {diff.baseline_convergence_rate * 100:.0f}% "
                  f"→ {diff.current_convergence_rate * 100:.0f}% "
                  f"({diff.delta_convergence_rate * 100:+.0f}pp)")
            for r in diff.regressions:
                print(f"    [unvalidated] {r}")
            for imp in diff.improvements:
                print(f"    ✅ {imp}")
            if not diff.regressions and not diff.improvements:
                print("    (no significant change)")

    out_path = Path(args.out) if args.out else _default_record_path(
        f"variance-{args.spec}-{args.model}-n{args.n}"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"\n  → record: {out_path}")

    # v0.5.4a3 — markdown report next to the JSON.
    md_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        render_variance_markdown(report, baseline_diff=diff), encoding="utf-8",
    )
    print(f"  → markdown: {md_path}")

    # Convergence-rate gating: variance is "passing" iff every run
    # converged AND there are no regressions vs baseline.
    converged = report.convergence_rate >= 1.0
    no_regressions = (diff is None) or not diff.has_regressions
    return 0 if (converged and no_regressions) else 1


# ── baseline ───────────────────────────────────────────────────────────


def _cmd_baseline_set(args: argparse.Namespace) -> int:
    """Promote a variance JSON to the baseline for its (spec, model).

    Loads the source JSON, reconstructs enough of the VarianceReport
    to call save_baseline (which only persists the to_dict form
    anyway). If the source spec/model don't match the flags, refuse.
    """
    src = Path(args.source)
    if not src.is_file():
        print(f"✗ Source file not found: {src}")
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗ Could not read source: {exc}")
        return 1
    src_spec = data.get("spec_name", "")
    src_model = data.get("model_label", "")
    if args.spec != src_spec or args.model != src_model:
        print(
            f"✗ Source variance is for ({src_spec}, {src_model}) but flags "
            f"specified ({args.spec}, {args.model}). Refusing to baseline "
            "across mismatched (spec, model)."
        )
        return 1

    project_root = Path.cwd()
    target = baseline_path(project_root, args.spec, args.model)
    if target.exists() and not args.force:
        print(
            f"✗ Baseline already exists at {target}. Pass --force to "
            "overwrite, or `baseline rm` first."
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    rate_pct = int(round(float(data.get("convergence_rate", 0.0)) * 100))
    n = data.get("n", 0)
    print(f"✅ Baseline set: {target}")
    print(f"   spec={args.spec} model={args.model} n={n} convergence={rate_pct}%")
    return 0


def _cmd_baseline_list(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    baselines = list_baselines(project_root)
    if not baselines:
        print(f"No baselines under {project_root / '.resonant' / 'smoke-baselines'}")
        return 0
    print(f"Baselines (under {project_root / '.resonant' / 'smoke-baselines'}):")
    print()
    print(f"  {'spec':<14} {'model':<8} {'n':>3}  {'rate':>5}  {'median':<10}  path")
    print(f"  {'-' * 14} {'-' * 8} {'-' * 3}  {'-' * 5}  {'-' * 10}  ----")
    for b in baselines:
        rate = f"{int(round(b['convergence_rate'] * 100))}%"
        median = (f"{b['total_elapsed_seconds_median']:.1f}s"
                  if b['total_elapsed_seconds_median'] is not None else "-")
        print(
            f"  {b['spec']:<14} {b['model']:<8} {b['n']:>3}  "
            f"{rate:>5}  {median:<10}  {b['path']}"
        )
    return 0


def _cmd_baseline_show(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    data = load_baseline(
        project_path=project_root, spec=args.spec, model=args.model,
    )
    if data is None:
        print(
            f"No baseline for ({args.spec}, {args.model}) under "
            f"{project_root / '.resonant' / 'smoke-baselines'}"
        )
        return 1
    # Print the headline + timing rollup, NOT the full per-run array
    # (that's what `cat` is for).
    rate = float(data.get("convergence_rate", 0.0))
    n = data.get("n", 0)
    converged = data.get("converged_count", 0)
    print(f"Baseline — {args.spec} × {args.model} × n={n}")
    print(f"  convergence: {converged}/{n} ({int(round(rate * 100))}%)")
    total = data.get("total_elapsed_seconds") or {}
    if total.get("median") is not None:
        print(
            f"  total elapsed:  median={total.get('median'):.1f}s "
            f"min={total.get('min', 0):.1f}s max={total.get('max', 0):.1f}s "
            f"stddev={total.get('stddev', 0):.1f}s"
        )
    iter_d = data.get("iter_duration_seconds") or {}
    if iter_d.get("median") is not None:
        print(
            f"  iter duration:  median={iter_d.get('median'):.1f}s "
            f"stddev={iter_d.get('stddev', 0):.1f}s"
        )
    return 0


# ── ci ──────────────────────────────────────────────────────────────────


def _cmd_ci(args: argparse.Namespace) -> int:
    """Run a curated suite of specs against a model. Designed for
    cron / GitHub Actions: stable specs, predictable runtime, machine-
    readable artifacts, exit code maps to convergence + regression
    state."""
    try:
        spec_names = parse_specs_arg(args.specs) if args.specs else list(DEFAULT_CI_SPECS)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 2

    print("=" * 70)
    print(f"CI SUITE — model={args.model} specs={','.join(spec_names)} n={args.n}")
    if args.diff_baseline:
        print("  --diff-baseline: any baseline regression fails the suite")
    print("=" * 70)
    print()

    def _on_spec_complete(spec_name: str, spec_result: CISpecResult) -> None:
        if spec_result.passed:
            sigil = "✅"
            label = "PASS"
        elif spec_result.has_regressions:
            sigil = "[unvalidated]"
            label = "REGRESSED"
        else:
            sigil = "✗"
            label = "FAIL"
        v = spec_result.variance
        print(
            f"  {sigil} {spec_name:<14} {label:<10} "
            f"converged={v.converged_count}/{v.n} "
            f"median={v.total_elapsed_seconds_median or 0:.0f}s"
        )
        if spec_result.has_regressions:
            for reg in spec_result.baseline_diff.regressions:
                print(f"      [unvalidated] {reg}")

    result = run_ci_suite(
        model_label=args.model,
        spec_names=spec_names,
        n=args.n,
        smoke_timeout_minutes=args.timeout_minutes,
        diff_baseline=args.diff_baseline,
        on_spec_complete=_on_spec_complete,
    )

    print()
    print("-" * 70)
    overall = "✅ PASS" if result.all_passed else "✗ FAIL"
    print(f"{overall} — {result.passing_count}/{len(result.spec_results)} specs passed")
    print(f"  total elapsed: {result.total_elapsed_seconds:.1f}s")
    print(f"  exit code:     {result.exit_code()}")

    out_path = Path(args.out) if args.out else _default_record_path(
        f"ci-{args.model}"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    print(f"  → record: {out_path}")

    md_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_ci_markdown(result), encoding="utf-8")
    print(f"  → markdown: {md_path}")

    return result.exit_code()


def _cmd_baseline_rm(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    target = baseline_path(project_root, args.spec, args.model)
    if not target.exists():
        print(f"No baseline at {target}")
        return 1
    target.unlink()
    print(f"✅ Removed baseline: {target}")
    return 0


# ── parser + main ──────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Exposed separately so tests can
    invoke it without going through `main()`."""
    parser = argparse.ArgumentParser(
        prog="resonant-smoke",
        description="End-to-end smoke harness for autonomous missions.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub_list = sub.add_parser(
        "list-specs", help="Show available smoke specs.",
    )
    sub_list.set_defaults(func=_cmd_list_specs)

    spec_choices = list_spec_names()
    model_choices = sorted(MODELS)

    sub_run = sub.add_parser(
        "run", help="Run a single smoke against (spec, model).",
    )
    sub_run.add_argument("--spec", choices=spec_choices, required=True)
    sub_run.add_argument("--model", choices=model_choices, required=True)
    sub_run.add_argument("--timeout-minutes", type=int, default=25,
                         help="Outer harness deadline (default: 25)")
    sub_run.add_argument("--out", default=None,
                         help="Path to write the JSON run record (default: smoke-runs/...)")
    sub_run.add_argument(
        "--report", default=None,
        help=("Path to write the markdown summary (default: same path as "
              "--out with .md suffix). Always emitted; flag overrides location."),
    )
    sub_run.add_argument(
        "--inject-planner-failure", action="store_true",
        help=("Wrap the backend so the first planner call returns malformed "
              "output. Walker should auto-retry once (v0.5.1a3) and recover. "
              "Use this to validate the retry path — convergence proves it works."),
    )
    sub_run.set_defaults(func=_cmd_run)

    sub_var = sub.add_parser(
        "variance", help="Run N smokes and report variance.",
    )
    sub_var.add_argument("--spec", choices=spec_choices, required=True)
    sub_var.add_argument("--model", choices=model_choices, required=True)
    sub_var.add_argument("--n", type=int, default=3,
                         help="How many runs (default: 3)")
    sub_var.add_argument("--timeout-minutes", type=int, default=25,
                         help="Per-run outer deadline (default: 25)")
    sub_var.add_argument("--out", default=None,
                         help="Path to write the JSON variance report (default: smoke-runs/...)")
    sub_var.add_argument(
        "--report", default=None,
        help=("Path to write the markdown summary (default: same path as "
              "--out with .md suffix). Always emitted; flag overrides location."),
    )
    sub_var.add_argument(
        "--diff-baseline", action="store_true",
        help=("Compare against the persisted baseline for this (spec, model). "
              "Exit non-zero if any regressions surface. Set baselines via "
              "`resonant-smoke baseline set`."),
    )
    sub_var.set_defaults(func=_cmd_variance)

    # ── baseline subcommand tree (v0.5.5a1) ──────────────────────
    sub_baseline = sub.add_parser(
        "baseline",
        help="Manage smoke-run baselines (set / list / show / rm).",
    )
    baseline_sub = sub_baseline.add_subparsers(dest="baseline_cmd", required=True)

    bl_set = baseline_sub.add_parser(
        "set", help="Promote a variance JSON to the baseline.",
    )
    bl_set.add_argument("--spec", choices=spec_choices, required=True)
    bl_set.add_argument("--model", choices=model_choices, required=True)
    bl_set.add_argument("--from", dest="source", required=True,
                        help="Path to the variance JSON to promote.")
    bl_set.add_argument("--force", action="store_true",
                        help="Overwrite an existing baseline.")
    bl_set.set_defaults(func=_cmd_baseline_set)

    bl_list = baseline_sub.add_parser(
        "list", help="Show all baselines under the project.",
    )
    bl_list.set_defaults(func=_cmd_baseline_list)

    bl_show = baseline_sub.add_parser(
        "show", help="Print the rolled-up stats for a (spec, model) baseline.",
    )
    bl_show.add_argument("--spec", choices=spec_choices, required=True)
    bl_show.add_argument("--model", choices=model_choices, required=True)
    bl_show.set_defaults(func=_cmd_baseline_show)

    bl_rm = baseline_sub.add_parser(
        "rm", help="Delete a baseline.",
    )
    bl_rm.add_argument("--spec", choices=spec_choices, required=True)
    bl_rm.add_argument("--model", choices=model_choices, required=True)
    bl_rm.set_defaults(func=_cmd_baseline_rm)

    # ── ci subcommand (v0.5.5a3) ─────────────────────────────────
    sub_ci = sub.add_parser(
        "ci",
        help="Run a curated suite for CI / cron environments.",
    )
    sub_ci.add_argument("--model", choices=model_choices, required=True)
    sub_ci.add_argument(
        "--specs", default=None,
        help=("Comma-separated spec names to run "
              f"(default: {','.join(DEFAULT_CI_SPECS)})"),
    )
    sub_ci.add_argument(
        "--n", type=int, default=1,
        help=("Runs per spec (default: 1 for fast smoke; bump to 3 for "
              "ship-readiness gating)"),
    )
    sub_ci.add_argument(
        "--timeout-minutes", type=int, default=25,
        help="Per-run outer deadline (default: 25)",
    )
    sub_ci.add_argument(
        "--diff-baseline", action="store_true",
        help=("For each spec, compare against its baseline. Any regression "
              "fails the suite (in addition to non-convergence)."),
    )
    sub_ci.add_argument("--out", default=None,
                        help="Path to write the JSON suite record (default: smoke-runs/...)")
    sub_ci.add_argument("--report", default=None,
                        help="Path to write the markdown summary (default: same as --out with .md)")
    sub_ci.set_defaults(func=_cmd_ci)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
