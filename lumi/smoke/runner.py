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
    "glm": "glm-5.2:cloud",
    "flash": "deepseek-v4-flash:cloud",
    "pro": "deepseek-v4-pro:cloud",
}


def resolve_model_id(model_label: str) -> str:
    """Resolve a legacy shorthand or accept a provider model id directly."""
    normalized = str(model_label or "").strip()
    if not normalized:
        raise ValueError("model must not be empty")
    return MODELS.get(normalized, normalized)


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
    tool_calls_total: int = 0
    edit_attempts: int = 0
    edit_successes: int = 0
    fuzzy_edit_rescues: int = 0
    tool_argument_failures: int = 0
    backend_retry_count: int = 0
    structured_output_repairs: int = 0
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

    def edit_apply_success_rate(self) -> Optional[float]:
        if not self.edit_attempts:
            return None
        return self.edit_successes / self.edit_attempts

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
            "tool_calls_total": self.tool_calls_total,
            "edit_attempts": self.edit_attempts,
            "edit_successes": self.edit_successes,
            "edit_apply_success_rate": self.edit_apply_success_rate(),
            "fuzzy_edit_rescues": self.fuzzy_edit_rescues,
            "tool_argument_failures": self.tool_argument_failures,
            "backend_retry_count": self.backend_retry_count,
            "structured_output_repairs": self.structured_output_repairs,
            "project_path": self.project_path,
            "roadmap_path": self.roadmap_path,
            "error": self.error,
            "timed_out": self.timed_out,
            "is_converged": self.is_converged(),
        }


# ── Project-bootstrap helpers ──────────────────────────────────────────


def _check_seed_path_format(relpath) -> None:
    """Cheap pre-tempdir validation of a seed-files key.

    Rejects: empty/non-string, paths that the OS calls absolute,
    POSIX-rooted paths (`/foo`) which are NOT considered absolute
    on Windows but still resolve outside the project, and any
    `..` segment.

    Cross-platform: a POSIX-style absolute path like `/etc/x`
    isn't classified as absolute by `Path` on Windows, but it
    resolves to `C:\\etc\\x` when joined with a tempdir under
    `C:\\Users\\...` — clearly outside the project. Catch it
    here with the explicit prefix check so the error message is
    consistent across platforms.

    Raises ValueError on rejection. Returns None on success.
    """
    if not relpath or not isinstance(relpath, str):
        raise ValueError(
            f"seed_files path must be a non-empty string; got {relpath!r}"
        )
    p = Path(relpath)
    if p.is_absolute() or relpath.startswith(("/", "\\")):
        raise ValueError(
            f"seed_files path {relpath!r} is absolute; must be "
            f"relative to the project root"
        )
    if any(part == ".." for part in p.parts):
        raise ValueError(
            f"seed_files path {relpath!r} contains '..' segment; "
            f"must stay inside the project root"
        )


def _validate_seed_path(relpath, project_root: Path) -> Path:
    """Full validation: cheap checks + post-resolve containment check.

    A spec author typo like `seed_files={"../foo.py": ...}` would
    silently write outside the smoke project. Defense in depth —
    bundled specs are trusted, but the cost of this check is tiny
    and it removes a whole class of footgun.

    Returns the resolved absolute path on success. Raises ValueError
    with a clear message on rejection.
    """
    _check_seed_path_format(relpath)
    target = (project_root / Path(relpath)).resolve()
    try:
        target.relative_to(project_root.resolve())
    except ValueError:
        raise ValueError(
            f"seed_files path {relpath!r} resolves outside the "
            f"project root ({project_root}); rejecting"
        )
    return target


def make_fresh_project(
    prefix: str,
    seed_files: dict[str, str] | None = None,
) -> Path:
    """Create a new tempdir, git init, single empty commit. Returns the
    path. The autonomous mission runs against this clean project so
    repeated smoke runs don't see each other's artifacts.

    v0.5.8a4 — `seed_files` (path → contents) is for refactor-style
    specs. Files are written + git-committed BEFORE the smoke run so
    the autonomous loop sees them as pre-existing project state, not
    its own first commit. The seed commit lands AFTER the empty
    initial commit so `git log` shows a clean two-commit baseline.

    v0.5.10a3 — seed_files paths are validated to stay inside the
    project root. Absolute paths and `..` segments raise ValueError
    *before* any tempdir is created, so a typo in a spec doesn't
    leak files into the host filesystem.
    """
    # Validate paths BEFORE creating the tempdir so a bad spec fails
    # fast without leaving a dangling tempdir. The full resolve check
    # runs later against the actual project root.
    if seed_files:
        for relpath in seed_files:
            _check_seed_path_format(relpath)

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
    if seed_files:
        for relpath, content in seed_files.items():
            # Re-validate against the resolved project root — this
            # catches things like `foo/../../bar` that pass the cheap
            # check but resolve outside.
            target = _validate_seed_path(relpath, project)
            target.parent.mkdir(parents=True, exist_ok=True)
            # `errors="replace"` is paranoid; spec authors should write
            # ASCII-clean fixtures, but if they don't we want to fail
            # noisily on the diff — not corrupt the on-disk file.
            target.write_text(content, encoding="utf-8")
        # Commit the seed so the autonomous loop's first commit is
        # `seed: ...` -> seed-baseline; subsequent iterations are
        # the loop's own work.
        subprocess.run(["git", "add", "-A"], cwd=project, check=True,
                       capture_output=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "smoke seed (pre-existing project state)"],
            cwd=project, check=True, capture_output=True, env=env,
        )
    return project


# ── Event-stream accumulator (module-level for testability) ───────────


def _new_summary() -> dict:
    """Initialize the per-run summary dict used by the event sink."""
    return {
        "iter_started": 0,
        "iter_complete": 0,
        "iter_failed": 0,
        "reflection_count": 0,
        "iter_durations": [],
        "tool_calls_total": 0,
        "edit_attempts": 0,
        "edit_successes": 0,
        "fuzzy_edit_rescues": 0,
        "tool_argument_failures": 0,
        "backend_retry_count": 0,
        "structured_output_repairs": 0,
    }


def _accumulate_event(payload: dict, summary: dict) -> None:
    """Update `summary` in place from one daemon event.

    Module-level so the filter behavior (e.g. skipping missing or
    non-positive durations from `iteration_complete` events) is unit-
    testable without booting a live mission.

    v0.5.10a3 — `duration_seconds` is now required to be present AND
    positive to land in `iter_durations`. Pre-fix, a missing key
    returned 0 from `.get(default=0)` and isinstance passed, so any
    daemon-event-shape regression that dropped duration_seconds
    silently filled the durations list with 0.0 entries and biased
    the `avg_iter_duration_seconds()` rollup downward.
    """
    kind = payload.get("event", "")
    if kind == "autonomous_iteration_started":
        summary["iter_started"] += 1
    elif kind == "autonomous_iteration_complete":
        summary["iter_complete"] += 1
        dur = payload.get("duration_seconds")
        if isinstance(dur, (int, float)) and dur > 0:
            summary["iter_durations"].append(float(dur))
    elif kind == "autonomous_iteration_failed":
        summary["iter_failed"] += 1
    elif kind == "autonomous_reflection":
        summary["reflection_count"] += 1
    elif kind == "tool.call":
        summary["tool_calls_total"] += 1
        if payload.get("name") == "file_edit":
            summary["edit_attempts"] += 1
    elif kind == "tool.result":
        if str(payload.get("output") or "").startswith("Tool arguments were malformed"):
            summary["tool_argument_failures"] += 1
        if payload.get("name") == "file_edit" and not payload.get("is_error") and not payload.get("denied"):
            summary["edit_successes"] += 1
            strategy = (payload.get("metadata") or {}).get("match_strategy")
            if strategy and strategy != "exact":
                summary["fuzzy_edit_rescues"] += 1
    elif kind == "backend.status":
        if "retry" in str(payload.get("kind") or ""):
            summary["backend_retry_count"] += 1
    elif kind == "node.done":
        result = payload.get("result") or payload.get("data") or {}
        metrics = result.get("data") if isinstance(result, dict) else {}
        if isinstance(metrics, dict) and metrics.get("structured_output_repaired"):
            summary["structured_output_repairs"] += 1


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
    - `model_label`: a legacy shorthand or any Ollama model identifier
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
    spec: SmokeSpec = get_spec(spec_name)
    model_id = resolve_model_id(model_label)

    if project_path is None:
        project_path = make_fresh_project(
            prefix=f"resonant-{spec.name}-{model_label}-",
            # v0.5.8a4 — seed pre-existing files for refactor-style
            # specs (refactor-py); empty for greenfield specs.
            seed_files=dict(spec.seed_files) if spec.seed_files else None,
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

    summary = _new_summary()

    def _on_event(payload: dict) -> None:
        _accumulate_event(payload, summary)
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
        tool_calls_total=summary["tool_calls_total"],
        edit_attempts=summary["edit_attempts"],
        edit_successes=summary["edit_successes"],
        fuzzy_edit_rescues=summary["fuzzy_edit_rescues"],
        tool_argument_failures=summary["tool_argument_failures"],
        backend_retry_count=summary["backend_retry_count"],
        structured_output_repairs=summary["structured_output_repairs"],
        project_path=str(project_path),
        roadmap_path=roadmap_path,
        error=error,
        timed_out=timed_out,
    )
