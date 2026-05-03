"""
Single-mission smoke runner.

`run_smoke()` boots a fresh git-initialized scratch project, dispatches
the autonomous mission for the named spec against a real Ollama
backend, waits for convergence (or timeout), and returns a
`SmokeResult` summarizing what happened.

This is the consolidation of the per-spec scripts under `scripts/`.
The scripts still work as direct invocation entry points; they now
just call into here.

Network-dependent: requires Ollama reachable at the URL returned by
`network_defaults.resolve_ollama_url()`. Tests for this module mock
the daemon construction; live runs are gated on `--allow-network`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .specs import SmokeSpec, get_spec

logger = logging.getLogger(__name__)


# ── Model registry ─────────────────────────────────────────────────────


# Short labels → Ollama model ids. Add entries here as new tiers ship.
# The label is what the CLI accepts (`--model pro`); the id is what
# `create_backend` wants. v0.5.4a1: PLANNER_BY_TIER routing was
# removed, so adding a new model here is sufficient — the autonomous
# stack uses PLAN_DEEP for all of them.
MODELS: dict[str, str] = {
    "flash": "deepseek-v4-flash:cloud",
    "pro": "deepseek-v4-pro:cloud",
}


# ── Result container ───────────────────────────────────────────────────


@dataclass
class SmokeResult:
    """Outcome of a single smoke run.

    `verdict` is the daemon's terminal verdict (`satisfied` / `paused` /
    `failed` / `stuck`). Use `.is_converged()` for the boolean form
    that maps to "ship-readiness".

    Iteration durations are captured per-iter so variance analysis
    can compute median + stddev across multiple runs.
    """
    spec_name: str
    model_label: str
    model_id: str
    started_at_epoch: float
    total_elapsed_seconds: float
    daemon_elapsed_seconds: float
    verdict: str
    stop_reason: str
    iter_count: int
    iter_started: int = 0
    iter_complete: int = 0
    iter_failed: int = 0
    reflection_count: int = 0
    iter_durations_seconds: list[float] = field(default_factory=list)
    project_path: str = ""
    roadmap_path: str = ""
    error: str = ""               # set if start_autonomous_mission raised
    timed_out: bool = False       # smoke harness deadline hit before terminal

    def is_converged(self) -> bool:
        """True iff the daemon reached a satisfied terminal state."""
        return self.verdict == "satisfied"

    def avg_iter_duration_seconds(self) -> Optional[float]:
        """Average iter duration if any iters completed; None otherwise."""
        if not self.iter_durations_seconds:
            return None
        return sum(self.iter_durations_seconds) / len(self.iter_durations_seconds)

    def to_dict(self) -> dict:
        """Serializable form — JSON-friendly. The variance report uses
        this to persist per-run records to disk."""
        return {
            "spec_name": self.spec_name,
            "model_label": self.model_label,
            "model_id": self.model_id,
            "started_at_epoch": self.started_at_epoch,
            "total_elapsed_seconds": self.total_elapsed_seconds,
            "daemon_elapsed_seconds": self.daemon_elapsed_seconds,
            "verdict": self.verdict,
            "stop_reason": self.stop_reason,
            "iter_count": self.iter_count,
            "iter_started": self.iter_started,
            "iter_complete": self.iter_complete,
            "iter_failed": self.iter_failed,
            "reflection_count": self.reflection_count,
            "iter_durations_seconds": list(self.iter_durations_seconds),
            "avg_iter_duration_seconds": self.avg_iter_duration_seconds(),
            "project_path": self.project_path,
            "roadmap_path": self.roadmap_path,
            "error": self.error,
            "timed_out": self.timed_out,
            "is_converged": self.is_converged(),
        }


# ── Project-bootstrap helpers ──────────────────────────────────────────


def make_fresh_project(prefix: str) -> Path:
    """Create a new tempdir, git init, single empty commit. Returns the
    path. The autonomous mission runs against this clean project so
    repeated smoke runs don't see each other's artifacts."""
    project = Path(tempfile.mkdtemp(prefix=prefix))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=project, check=True,
                   capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "initial"],
        cwd=project, check=True, capture_output=True, env=env,
    )
    return project


# ── Stub AppState for the runner ───────────────────────────────────────


class _StubProject:
    def __init__(self, path: Path):
        self.project_path = str(path)


class _StubState:
    """Duck-typed AppState minimal enough for start_autonomous_mission.
    See `scripts/smoke_autonomous_minimal.py` for the original — this
    is the consolidated version."""

    def __init__(self, project_path: Path, backend: Any, settings: Any):
        self.project = _StubProject(project_path)
        self.backend = backend
        self.settings = settings
        self._project_instructions = ""
        self._intent_service: Any = None
        self._autonomous_daemons: dict = {}

    def get_intent_service(self, *, on_event=None):
        # Lazily construct on first call so the smoke harness can tear
        # down + recreate state cleanly between runs.
        if self._intent_service is None:
            from ..engine.tools import AGENT_TOOLS
            from ..orchestration.intent_service import IntentService
            self._intent_service = IntentService(
                project_path=self.project.project_path,
                backend=self.backend,
                all_tools=list(AGENT_TOOLS),
                project_instructions="",
                settings=self.settings,
                on_event=on_event,
            )
        elif on_event is not None:
            self._intent_service.on_event = on_event
        return self._intent_service


# ── The main entrypoint ────────────────────────────────────────────────


def run_smoke(
    *,
    spec_name: str,
    model_label: str,
    smoke_timeout_minutes: int = 25,
    on_event: Optional[Callable[[dict], None]] = None,
    project_path: Optional[Path] = None,
    backend: Any = None,
    intent_id: Optional[str] = None,
    inject_planner_failure: bool = False,
) -> SmokeResult:
    """Run one smoke against a real Ollama backend. Returns a SmokeResult.

    Parameters:
    - `spec_name`: must be in SPECS (use `list_spec_names()` to enumerate)
    - `model_label`: must be in MODELS
    - `smoke_timeout_minutes`: outer deadline so a stuck mission doesn't
      pin the harness forever. Independent of the spec's own time
      budget (which the daemon enforces internally).
    - `on_event`: optional secondary event sink. The harness logs to
      stdout regardless; pass this if you also want to capture for
      analysis.
    - `project_path` / `backend` / `intent_id`: explicitly override
      the auto-bootstrap. Tests use these to inject stubs without
      hitting the network.
    """
    if model_label not in MODELS:
        raise ValueError(
            f"Unknown model label {model_label!r}. "
            f"Valid: {', '.join(sorted(MODELS))}"
        )
    spec: SmokeSpec = get_spec(spec_name)
    model_id = MODELS[model_label]

    if project_path is None:
        project_path = make_fresh_project(
            prefix=f"resonant-{spec.name}-{model_label}-",
        )

    if backend is None:
        # Imported here to avoid pulling Ollama deps when the harness is
        # only used for unit tests.
        from ..backends import create_backend
        from ..network_defaults import resolve_ollama_url
        backend = create_backend(
            backend_type="ollama",
            model=model_id,
            url=resolve_ollama_url(),
        )

    # v0.5.4a2 — wrap the backend in FlakyPlannerBackend if requested.
    # First planner call gets corrupted; walker should spawn a retry
    # sibling and the second (uninterrupted) call recovers. A satisfied
    # mission with this flag set proves the live retry path works.
    if inject_planner_failure:
        from .flaky import FlakyPlannerBackend
        backend = FlakyPlannerBackend(backend, fail_first_n_planner_calls=1)
        logger.info(
            "FlakyPlannerBackend enabled — first planner call will be "
            "corrupted to exercise walker retry (v0.5.1a3) end-to-end."
        )

    if intent_id is None:
        intent_id = f"{spec.intent_id_prefix}-{model_label}"

    state = _StubState(project_path=project_path, backend=backend, settings=None)

    summary = {
        "iter_started": 0,
        "iter_complete": 0,
        "iter_failed": 0,
        "reflection_count": 0,
        "iter_durations": [],
    }

    def _on_event(payload: dict) -> None:
        kind = payload.get("event", "")
        if kind == "autonomous_iteration_started":
            summary["iter_started"] += 1
        elif kind == "autonomous_iteration_complete":
            summary["iter_complete"] += 1
            dur = payload.get("duration_seconds", 0)
            if isinstance(dur, (int, float)):
                summary["iter_durations"].append(float(dur))
        elif kind == "autonomous_iteration_failed":
            summary["iter_failed"] += 1
        elif kind == "autonomous_reflection":
            summary["reflection_count"] += 1
        if on_event is not None:
            try:
                on_event(payload)
            except Exception:
                logger.debug("user on_event raised", exc_info=True)

    started_at = time.time()
    error = ""

    # Local import avoids a circular import (gui.autonomous_session
    # imports a few `gui.*` modules; the smoke package is otherwise
    # gui-free).
    from ..gui.autonomous_session import start_autonomous_mission

    try:
        daemon = start_autonomous_mission(
            state=state,
            intent_id=intent_id,
            feature=f"{spec.name} smoke",
            spec_markdown=spec.spec_markdown,
            on_event=_on_event,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("start_autonomous_mission raised in smoke harness")
        return SmokeResult(
            spec_name=spec.name,
            model_label=model_label,
            model_id=model_id,
            started_at_epoch=started_at,
            total_elapsed_seconds=time.time() - started_at,
            daemon_elapsed_seconds=0.0,
            verdict="failed",
            stop_reason="dispatch_error",
            iter_count=0,
            project_path=str(project_path),
            error=error,
        )

    deadline = time.time() + smoke_timeout_minutes * 60.0
    timed_out = False
    while daemon.is_running() and time.time() < deadline:
        time.sleep(1.0)
    if daemon.is_running():
        timed_out = True
        daemon.stop("smoke_timeout", "smoke harness deadline")
        daemon.join(timeout=30)

    snap = daemon.state_snapshot()
    total_elapsed = time.time() - started_at
    roadmap_path = ""
    try:
        from ..gui.roadmap import default_path as _rm_default_path
        roadmap_path = str(_rm_default_path(str(project_path), intent_id))
    except Exception:
        pass

    return SmokeResult(
        spec_name=spec.name,
        model_label=model_label,
        model_id=model_id,
        started_at_epoch=started_at,
        total_elapsed_seconds=total_elapsed,
        daemon_elapsed_seconds=float(snap.get("elapsed_seconds", 0.0)),
        verdict=str(snap.get("verdict", "")),
        stop_reason=str(snap.get("stop_reason", "")),
        iter_count=int(snap.get("iter_count", 0)),
        iter_started=summary["iter_started"],
        iter_complete=summary["iter_complete"],
        iter_failed=summary["iter_failed"],
        reflection_count=summary["reflection_count"],
        iter_durations_seconds=list(summary["iter_durations"]),
        project_path=str(project_path),
        roadmap_path=roadmap_path,
        error=error,
        timed_out=timed_out,
    )
