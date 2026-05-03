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
    for name in list_spec_names():
        spec = SPECS[name]
        print(f"  {name:<12}  {spec.description}")
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
        "⚠ TIMEOUT" if result.timed_out else "✗"
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
        print("  ⚠ --inject-planner-failure: first planner call will be")
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
            "⚠TO" if result.timed_out else "✗"
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

    out_path = Path(args.out) if args.out else _default_record_path(
        f"variance-{args.spec}-{args.model}-n{args.n}"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"\n  → record: {out_path}")

    # v0.5.4a3 — markdown report next to the JSON.
    md_path = Path(args.report) if args.report else out_path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_variance_markdown(report), encoding="utf-8")
    print(f"  → markdown: {md_path}")

    # Convergence-rate gating: variance is "passing" iff every run
    # converged. Anything less is a real signal — flag with non-zero.
    return 0 if report.convergence_rate >= 1.0 else 1


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
    sub_var.set_defaults(func=_cmd_variance)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
