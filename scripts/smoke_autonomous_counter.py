"""
Pre-GA Step 2 (option C, tight smoke) — wordcount CLI mission against
deepseek-v4-flash:cloud and deepseek-v4-pro:cloud, side-by-side.

Same harness as `smoke_autonomous_minimal.py` but:
- Spec has 4 criteria (vs 2) — exercises REFLECT add-items + multi-iter
- Pure-bash criteria — no browser/vision infra needed
- Parameterized by --model so flash and pro use the same script
- Records per-iteration timing + final scoring fields per docs/v0.5.0-smoke-plan.md

Run:
    python scripts/smoke_autonomous_counter.py --model flash
    python scripts/smoke_autonomous_counter.py --model pro

Each run takes 15-60 minutes. The two runs together inform the
flash-vs-pro tier guidance for v0.5.0.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resonant_client.backends import create_backend  # noqa: E402
from resonant_client.engine.tools import AGENT_TOOLS  # noqa: E402
from resonant_client.gui.autonomous_session import (  # noqa: E402
    start_autonomous_mission,
)
from resonant_client.network_defaults import resolve_ollama_url  # noqa: E402
from resonant_client.orchestration.intent_service import IntentService  # noqa: E402


# ── The wordcount spec ──────────────────────────────────────────────────


# Four criteria covering: end-to-end CLI behavior, syntax validity,
# non-trivial implementation, no third-party deps. The 4th uses the
# `!` negation form so the parser is exercised. Pure bash so no
# browser orchestration or vision-model dependency.
SPEC_MD = """\
## Final spec

**Refined intent:** Build a Python CLI utility `wordcount.py` at the
project root. It takes a single file path argument and prints
space-separated `<lines> <words> <chars>` to stdout, mirroring
`wc -l -w -c <file>` output. Stdlib only — no third-party deps.

**Key assumptions:**
- Python 3.11+
- Reads UTF-8 text files
- Files small enough to read into memory

**In scope:**
- The CLI script
- Basic input handling (empty / missing file → reasonable error)

**Out of scope:**
- Streaming for huge files
- Multi-file support
- Tests (covered by the criteria, not a separate ask)

**Time budget:** 1h

**Technical constraints:**
- Stdlib only
- Single file (`wordcount.py`)
- Must be runnable with `python wordcount.py <path>`

**Acceptance criteria:**
- `[bash]` `python wordcount.py example.txt` exits 0
- `[bash]` `python -m py_compile wordcount.py` exits 0
- `[bash]` `wc -l < wordcount.py` output > 5
- `[bash]` `! grep -nE 'import (numpy|pandas|requests|httpx)' wordcount.py` exits 0

**Open risks:**
- Model may add unnecessary third-party deps despite the constraint
- Word/char counting semantics may diverge from `wc` (whitespace
  handling — but the criteria don't pin exact output, just "runs
  without error")
"""


_MODEL_MAP = {
    "flash": "deepseek-v4-flash:cloud",
    "pro": "deepseek-v4-pro:cloud",
}


# ── Stub AppState ──────────────────────────────────────────────────────


class _StubProject:
    def __init__(self, path):
        self.project_path = str(path)


class _StubState:
    def __init__(self, project_path, backend, settings):
        self.project = _StubProject(project_path)
        self.backend = backend
        self.settings = settings
        self._project_instructions = ""
        self._intent_service = None
        self._autonomous_daemons = {}

    def get_intent_service(self, *, on_event=None):
        if self._intent_service is None:
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


def make_fresh_project(model_label: str) -> Path:
    """Create a fresh temp project + git repo + the example.txt file
    that the wordcount CLI needs as test input. Returns the project
    path."""
    project = Path(tempfile.mkdtemp(prefix=f"resonant-counter-{model_label}-"))
    print(f"  → project dir: {project}")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project, check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "initial"],
        cwd=project, check=True, capture_output=True, env=env,
    )
    # Pre-create the test input that the [bash] criterion runs the
    # CLI against. If the implementer doesn't make this file
    # themselves, the criterion would always fail.
    (project / "example.txt").write_text(
        "hello world\nfoo bar baz biz\n", encoding="utf-8",
    )
    return project


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--model", choices=list(_MODEL_MAP.keys()), default="flash",
        help="Which deepseek tier to use",
    )
    ap.add_argument(
        "--budget-minutes", type=int, default=60,
        help="Time budget for the autonomous run (mission-level cap)",
    )
    ap.add_argument(
        "--smoke-timeout-minutes", type=int, default=70,
        help="Smoke harness deadline (must exceed budget)",
    )
    args = ap.parse_args()

    model_label = args.model
    model_id = _MODEL_MAP[model_label]

    print("=" * 70)
    print(f"AUTONOMOUS MISSION COUNTER SMOKE — model={model_label}")
    print(f"  spec: 4 [bash] criteria, wordcount.py CLI utility")
    print("=" * 70)
    print()

    project = make_fresh_project(model_label)

    url = resolve_ollama_url()
    print(f"  → Ollama URL: {url}")
    backend = create_backend(
        backend_type="ollama", model=model_id, url=url,
    )
    print(f"  → backend: {type(backend).__name__} model={backend.model}")
    print()

    state = _StubState(project_path=project, backend=backend, settings=None)

    events: list[dict] = []
    summary = {
        "iter_started": 0,
        "iter_complete": 0,
        "iter_failed": 0,
        "reflections": 0,
        "started": False,
        "ended": False,
        "end_reason": "",
        "iter_durations": [],
    }

    def on_event(payload: dict) -> None:
        events.append(payload)
        kind = payload.get("event", "")
        if kind == "autonomous_mission_started":
            summary["started"] = True
            print(f"  ∞ STARTED — budget={payload.get('time_budget_seconds')}s")
        elif kind == "autonomous_iteration_started":
            summary["iter_started"] += 1
            print(
                f"  ∞ ITER {payload.get('iter_count')} START — "
                f"{payload.get('item_id')}: {payload.get('item_title','')[:60]}"
            )
        elif kind == "autonomous_iteration_complete":
            summary["iter_complete"] += 1
            sha = (payload.get("commit_sha") or "")[:7]
            dur = payload.get("duration_seconds", 0)
            summary["iter_durations"].append(dur)
            print(
                f"  ∞ ITER {payload.get('iter_count')} DONE — "
                f"sha={sha or '<empty>'} ({dur:.1f}s)"
            )
        elif kind == "autonomous_iteration_failed":
            summary["iter_failed"] += 1
            print(
                f"  ✗ ITER {payload.get('iter_count')} FAILED — "
                f"{payload.get('error', '')}"
            )
        elif kind == "autonomous_reflection":
            summary["reflections"] += 1
            tally = payload.get("pass_tally", {})
            verdict = payload.get("verdict")
            ac = payload.get("acceptance_summary", {})
            added_n = len(payload.get("added", []))
            print(
                f"  ∞ REFLECT verdict={verdict} accept={ac.get('passed',0)}/"
                f"{ac.get('total',0)} bash={tally.get('bash_passed',0)}p/"
                f"{tally.get('bash_failed',0)}f added={added_n}"
            )
        elif kind in {
            "autonomous_mission_complete",
            "autonomous_mission_paused",
            "autonomous_mission_failed",
        }:
            summary["ended"] = True
            summary["end_reason"] = payload.get("stop_reason") or kind
            print(
                f"  ∞ {kind.upper().replace('AUTONOMOUS_MISSION_','')} — "
                f"reason={payload.get('stop_reason')} "
                f"elapsed={payload.get('elapsed_seconds', 0):.0f}s"
            )

    print(f"-" * 70)
    print(f"Dispatching mission with {model_label} ({model_id})…")
    print(f"-" * 70)

    started_at = time.time()

    try:
        daemon = start_autonomous_mission(
            state=state,
            intent_id=f"counter-{model_label}",
            feature="wordcount.py CLI smoke",
            spec_markdown=SPEC_MD,
            on_event=on_event,
        )
    except Exception as exc:
        print(f"\n✗ start_autonomous_mission RAISED: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 2

    deadline = time.time() + args.smoke_timeout_minutes * 60.0
    last_print = time.time()
    while daemon.is_running() and time.time() < deadline:
        time.sleep(5.0)
        if time.time() - last_print > 60:
            snap = daemon.state_snapshot()
            print(
                f"  · still running — iter={snap['iter_count']} "
                f"elapsed={snap['elapsed_seconds']:.0f}s "
                f"verdict={snap['verdict']}"
            )
            last_print = time.time()

    if daemon.is_running():
        print(f"\n⚠ Daemon still running at {args.smoke_timeout_minutes}m. Stopping.")
        daemon.stop("smoke_timeout", "smoke harness deadline")
        daemon.join(timeout=30)

    total_elapsed = time.time() - started_at
    snap = daemon.state_snapshot()

    print()
    print("=" * 70)
    print(f"RESULT — {model_label}")
    print("=" * 70)
    print(f"  is_running:                 {snap['is_running']}")
    print(f"  iter_count:                 {snap['iter_count']}")
    print(f"  daemon_elapsed:             {snap['elapsed_seconds']:.1f}s")
    print(f"  smoke_total_elapsed:        {total_elapsed:.1f}s")
    print(f"  verdict:                    {snap['verdict']}")
    print(f"  stop_reason:                {snap.get('stop_reason','')}")
    print(f"  iter started/done/failed:   "
          f"{summary['iter_started']}/{summary['iter_complete']}/{summary['iter_failed']}")
    print(f"  reflections:                {summary['reflections']}")
    if summary["iter_durations"]:
        avg = sum(summary["iter_durations"]) / len(summary["iter_durations"])
        print(f"  avg iter duration:          {avg:.1f}s")
        print(f"  min/max iter duration:      "
              f"{min(summary['iter_durations']):.1f}s / "
              f"{max(summary['iter_durations']):.1f}s")
    print(f"  events captured:            {len(events)}")

    print()
    print("Project file listing (excluding .git, .resonant):")
    for p in sorted(project.iterdir()):
        if p.name in {".git", ".resonant"}:
            continue
        print(f"  {p.name} ({p.stat().st_size}b)")

    print()
    wordcount_py = project / "wordcount.py"
    if wordcount_py.exists():
        body = wordcount_py.read_text(encoding="utf-8", errors="replace")
        print(f"wordcount.py ({len(body)} chars, {body.count(chr(10))} lines):")
        print("-" * 60)
        print(body[:1500])
        if len(body) > 1500:
            print(f"... ({len(body) - 1500} more chars)")
        print("-" * 60)
    else:
        print("wordcount.py does NOT exist.")

    # Final acceptance-criteria check (re-run to record exact final state)
    print()
    print("Acceptance-criteria final state (from roadmap):")
    rm_path = project / ".resonant" / f"roadmap-counter-{model_label}.md"
    if rm_path.exists():
        rm_text = rm_path.read_text(encoding="utf-8", errors="replace")
        # Extract the criteria section
        for line in rm_text.splitlines():
            if line.startswith("- [") and "[bash]" in line:
                print(f"  {line}")

    # Save the run record as JSON for later side-by-side analysis.
    record = {
        "model_label": model_label,
        "model_id": model_id,
        "spec_markdown": SPEC_MD,
        "smoke_total_elapsed_seconds": total_elapsed,
        "daemon_state": snap,
        "summary": summary,
        "wordcount_py_exists": wordcount_py.exists(),
        "wordcount_py_size": (
            wordcount_py.stat().st_size if wordcount_py.exists() else 0
        ),
        "wordcount_py_lines": (
            wordcount_py.read_text(encoding="utf-8", errors="replace").count("\n")
            if wordcount_py.exists() else 0
        ),
        "project_path": str(project),
    }
    record_path = ROOT / f"smoke-counter-{model_label}.json"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print()
    print(f"Run record saved: {record_path}")

    print()
    if snap["verdict"] == "satisfied":
        print(f"✅ {model_label.upper()} CONVERGED — verdict=satisfied")
        return 0
    if summary["iter_complete"] >= 1:
        print(
            f"⚠ {model_label.upper()} PARTIAL — "
            f"{summary['iter_complete']} iter(s), "
            f"stop_reason={snap.get('stop_reason')}"
        )
        return 0
    print(f"✗ {model_label.upper()} FAILED — no iterations completed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
