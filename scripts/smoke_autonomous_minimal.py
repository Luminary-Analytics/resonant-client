"""
Pre-GA integration smoke for Autonomous Mission.

Exercises the full daemon stack against a real Ollama backend with a
hand-crafted minimal spec — bypasses the slow rigorous-grill phase
so we can find wiring bugs in 30 minutes instead of 6 hours.

Goals:
1. Verify mission_dispatch_autonomous → start_autonomous_mission →
   AutonomousMissionDaemon construction wires correctly with a real
   IntentService, real backend, real git subprocess.
2. Verify the daemon's first iteration actually dispatches a Phase-1
   sub-mission and waits for it to finish.
3. Verify run_reflect_pass actually executes [bash] criteria against
   a real subprocess.
4. Verify the daemon reaches a terminal state (satisfied / stuck /
   blocked) within a reasonable time and emits the right events.

Run from project root:
    python scripts/smoke_autonomous_minimal.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resonant_client.backends import create_backend  # noqa: E402
from resonant_client.engine.tools import AGENT_TOOLS  # noqa: E402
from resonant_client.gui.autonomous_session import (  # noqa: E402
    start_autonomous_mission,
)
from resonant_client.network_defaults import resolve_ollama_url  # noqa: E402
from resonant_client.orchestration.intent_service import IntentService  # noqa: E402


# ── Minimal spec: 1 simple task, 2 bash criteria ───────────────────────


SPEC_MD = """\
## Final spec

**Refined intent:** Create a file called `hello.txt` at the project
root containing exactly the text `hello world` followed by a newline.

**Key assumptions:**
- Plain UTF-8 text file
- Project root is the working directory

**In scope:**
- Single file creation

**Out of scope:**
- Anything else

**Time budget:** 1h

**Technical constraints:**
- POSIX-compatible commands

**Acceptance criteria:**
- `[bash]` `test -f hello.txt` exits 0
- `[bash]` `cat hello.txt` output == hello world

**Open risks:**
- File encoding / line endings
"""


# ── Stub AppState (minimal duck-typed shape) ────────────────────────────


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


# ── Setup helpers ───────────────────────────────────────────────────────


def make_fresh_project() -> Path:
    """Create a fresh temp project with git initialized."""
    project = Path(tempfile.mkdtemp(prefix="resonant-smoke-"))
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
    return project


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("AUTONOMOUS MISSION INTEGRATION SMOKE — Step 1 of GA prep")
    print("=" * 70)
    print()

    project = make_fresh_project()

    # Real backend.
    url = resolve_ollama_url()
    print(f"  → Ollama URL: {url}")
    backend = create_backend(
        backend_type="ollama",
        model="deepseek-v4-flash:cloud",
        url=url,
    )
    print(f"  → backend: {type(backend).__name__} model={backend.model}")

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
    }

    def on_event(payload: dict) -> None:
        events.append(payload)
        kind = payload.get("event", "")
        # Surface key autonomous + intent lifecycle events to stdout.
        if kind == "autonomous_mission_started":
            summary["started"] = True
            print(f"  ∞ STARTED — budget={payload.get('time_budget_seconds')}s")
        elif kind == "autonomous_iteration_started":
            summary["iter_started"] += 1
            print(
                f"  ∞ ITER {payload.get('iter_count')} START — "
                f"{payload.get('item_id')}: {payload.get('item_title','')}"
            )
        elif kind == "autonomous_iteration_complete":
            summary["iter_complete"] += 1
            sha = (payload.get("commit_sha") or "")[:7]
            print(
                f"  ∞ ITER {payload.get('iter_count')} DONE — "
                f"sha={sha or '<empty>'} "
                f"({payload.get('duration_seconds', 0):.1f}s)"
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
            print(
                f"  ∞ REFLECT verdict={payload.get('verdict')} "
                f"bash={tally.get('bash_passed',0)}/"
                f"{tally.get('bash_failed',0) + tally.get('bash_passed',0)} "
                f"chrome_pending={tally.get('chrome_pending',0)} "
                f"summary={(payload.get('summary','') or '')[:80]!r}"
            )
        elif kind in {
            "autonomous_mission_complete",
            "autonomous_mission_paused",
            "autonomous_mission_failed",
        }:
            summary["ended"] = True
            summary["end_reason"] = payload.get("stop_reason") or kind
            print(
                f"  ∞ {kind.upper()} — reason={payload.get('stop_reason')} "
                f"msg={payload.get('stop_message','')!r}"
            )
        elif kind == "intent.started":
            print(f"  · sub-intent started: {payload.get('intent_id','')[:8]}")
        elif kind == "intent.complete":
            print(f"  · sub-intent complete: {payload.get('intent_id','')[:8]}")
        elif kind == "intent.failed":
            print(
                f"  ✗ sub-intent failed: "
                f"{payload.get('intent_id','')[:8]} {payload.get('error','')}"
            )

    print()
    print("-" * 70)
    print("Dispatching autonomous mission with hand-crafted minimal spec…")
    print("-" * 70)

    try:
        daemon = start_autonomous_mission(
            state=state,
            intent_id="smoke-1",
            feature="hello.txt smoke",
            spec_markdown=SPEC_MD,
            on_event=on_event,
        )
    except Exception as exc:
        print(f"\n✗ start_autonomous_mission RAISED: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 2

    # Wait up to 6 minutes for the daemon to terminate.
    deadline = time.time() + 360.0
    last_print = time.time()
    while daemon.is_running() and time.time() < deadline:
        time.sleep(2.0)
        # Print a heartbeat every 30s so the user knows we're not hung.
        if time.time() - last_print > 30:
            snap = daemon.state_snapshot()
            print(
                f"  · still running — iter={snap['iter_count']} "
                f"elapsed={snap['elapsed_seconds']:.0f}s "
                f"verdict={snap['verdict']}"
            )
            last_print = time.time()

    if daemon.is_running():
        print("\n⚠ Daemon still running at deadline (6m). Stopping.")
        daemon.stop("smoke_timeout", "6-minute smoke timeout")
        daemon.join(timeout=30)

    print()
    print("=" * 70)
    print("SMOKE RESULT")
    print("=" * 70)
    snap = daemon.state_snapshot()
    print(f"  is_running:        {snap['is_running']}")
    print(f"  iter_count:        {snap['iter_count']}")
    print(f"  elapsed:           {snap['elapsed_seconds']:.1f}s")
    print(f"  verdict:           {snap['verdict']}")
    print(f"  stop_reason:       {snap.get('stop_reason','')}")
    print(f"  events captured:   {len(events)}")
    print(f"  iter started/done/failed: "
          f"{summary['iter_started']}/{summary['iter_complete']}/{summary['iter_failed']}")
    print(f"  reflections:       {summary['reflections']}")
    print(f"  end_reason:        {summary['end_reason']}")
    print()

    # Tail of project state.
    print("Project file listing:")
    for p in sorted(project.iterdir()):
        if p.name == ".git":
            continue
        print(f"  {p.name}")
    print()

    # Quick sanity check on the file contents.
    hello = project / "hello.txt"
    if hello.exists():
        body = hello.read_text(encoding="utf-8", errors="replace")
        print(f"hello.txt contents: {body!r}")
    else:
        print("hello.txt does NOT exist.")
    print()

    # Roadmap state at end.
    resonant_dir = project / ".resonant"
    if resonant_dir.is_dir():
        for f in resonant_dir.iterdir():
            if f.suffix == ".md":
                print(f"Roadmap @ {f.name}:")
                print("-" * 60)
                print(f.read_text(encoding="utf-8", errors="replace"))
                print("-" * 60)

    # Verdict summary for the smoke.
    print()
    if snap["verdict"] == "satisfied":
        print("✅ SMOKE PASSED — daemon converged on satisfied")
        return 0
    if summary["iter_complete"] >= 1 and snap.get("stop_reason") in {
        "stuck", "satisfied", "iteration_cap"
    }:
        print(f"⚠ SMOKE PARTIAL — {summary['iter_complete']} iter(s) "
              f"completed, ended with stop_reason={snap.get('stop_reason')}")
        print("  (iteration loop works; convergence may need follow-up tuning)")
        return 0
    print(f"✗ SMOKE FAILED — stop_reason={snap.get('stop_reason')!r}, "
          f"completed iters={summary['iter_complete']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
