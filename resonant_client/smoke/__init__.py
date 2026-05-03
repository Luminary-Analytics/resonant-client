"""
Resonant Client smoke harness — codifies the iterative dev cycle that
v0.5.0–v0.5.2 was running by hand.

The harness lets you reproduce, vary, and compare end-to-end autonomous
mission runs against a real Ollama backend without writing throwaway
scripts. Use it after any change to confirm convergence behavior hasn't
regressed; use it across model tiers to pick the right default.

Public API:
    from resonant_client.smoke import (
        SPECS, get_spec, run_smoke, run_variance,
        SmokeResult, VarianceReport,
    )

CLI:
    resonant-smoke list-specs
    resonant-smoke run --spec wordcount --model pro
    resonant-smoke variance --spec wordcount --model pro --n 3

See `docs/long-running-agents-phase-2-implementation.md` for the
broader autonomous-mission architecture this harness exercises.
"""
from .specs import SPECS, SmokeSpec, get_spec, list_spec_names
from .runner import SmokeResult, run_smoke, MODELS
from .variance import VarianceReport, run_variance, summarize_runs
from .flaky import FlakyPlannerBackend
from .report import render_run_markdown, render_variance_markdown
from .baseline import (
    BaselineDiff,
    baseline_path,
    diff_against_baseline,
    list_baselines,
    load_baseline,
    save_baseline,
)

__all__ = [
    "SPECS",
    "SmokeSpec",
    "get_spec",
    "list_spec_names",
    "SmokeResult",
    "run_smoke",
    "MODELS",
    "VarianceReport",
    "run_variance",
    "summarize_runs",
    "FlakyPlannerBackend",
    "render_run_markdown",
    "render_variance_markdown",
    "BaselineDiff",
    "baseline_path",
    "diff_against_baseline",
    "list_baselines",
    "load_baseline",
    "save_baseline",
]
