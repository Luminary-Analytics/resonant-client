"""
The bootstrap-roguelite end-to-end smoke. Same harness as
smoke_autonomous_counter.py but with the canonical 8-criterion
spec from `docs/long-running-agents-phase-2.md` §11.2 — the
"design north-star" mission.

Per the v0.5.0 design doc, this is the spec we wanted v0.5.0 GA
to validate. The v0.5.0/v0.5.1 GA smokes used the simpler
wordcount spec because it converges faster (single-iter on flash
or pro). Roguelite should exercise multi-iteration behavior
because the implementer has to scaffold more files.

Pure-bash criteria only (no [chrome] / [vision]) so this stays
runnable without a dev server or vision model. The full spec
WITH chrome+vision is documented in design doc §11.2 for future
reference; this version is the bash-only subset.

Run:
    python scripts/smoke_autonomous_roguelite.py --model pro
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


# ── The roguelite spec (bash-only subset) ──────────────────────────────


SPEC_MD = """\
## Final spec

**Refined intent:** Bootstrap a TypeScript roguelite skeleton
with strict tsc, a centered Canvas rendering the player as a
single green circle on a dark navy background, dev-server-driven
via Vite. Six source files total, no `any` types.

**Key assumptions:**
- Greenfield (no existing code touched)
- Vite is acceptable as the dev server
- Player is rendered with the 2D canvas API, no third-party engine
- TypeScript strict mode is non-negotiable

**In scope:**
- Project scaffold (package.json, tsconfig.json, vite.config.ts)
- Canvas mounting + 800×600 sizing
- Player as a centered green circle on dark navy background
- Single index.html entry point

**Out of scope:**
- Movement / input
- Map generation
- Combat / enemies / items
- Tests (covered by criteria)

**Time budget:** 1h

**Technical constraints:**
- Strict TypeScript (no `any`)
- Exactly 6 source files in src/
- No third-party game engines
- Stdlib + Vite + TypeScript only

**Acceptance criteria:**
- `[bash]` `test -f package.json` exits 0
- `[bash]` `test -f tsconfig.json` exits 0
- `[bash]` `test -f index.html` exits 0
- `[bash]` `find src -type f \\( -name '*.ts' -o -name '*.tsx' \\) | wc -l` output == 6
- `[bash]` `! grep -rnE ': any[^a-zA-Z_]' src/` exits 0
- `[bash]` `cat tsconfig.json | python -c "import json,sys; c=json.load(sys.stdin); assert c['compilerOptions']['strict']==True" && echo ok` output == ok

**Open risks:**
- Model may add unnecessary third-party deps despite the constraint
- TS strict mode interpretation can vary (pinned via the JSON
  parse criterion above)
- Source-file counting depends on `find`'s behavior — Git Bash
  on Windows handles this correctly per v0.5.1a4 fix
"""


_MODEL_MAP = {
    "flash": "deepseek-v4-flash:cloud",
    "pro": "deepseek-v4-pro:cloud",
}


# ── Stub AppState (same shape as the counter smoke) ────────────────────


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
    project = Path(tempfile.mkdtemp(prefix=f"resonant-roguelite-{model_label}-"))
    print(f"  → project dir: {project}")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=project, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "initial"],
                   cwd=project, check=True, capture_output=True, env=env)
    return project


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=list(_MODEL_MAP.keys()), default="pro")
    ap.add_argument("--budget-minutes", type=int, default=60)
    ap.add_argument("--smoke-timeout-minutes", type=int, default=25)
    args = ap.parse_args()

    model_label = args.model
    model_id = _MODEL_MAP[model_label]

    print("=" * 70)
    print(f"BOOTSTRAP-ROGUELITE SMOKE — model={model_label}")
    print(f"  spec: 6 [bash] criteria, multi-file scaffold")
    print("=" * 70)
    print()

    project = make_fresh_project(model_label)
    url = resolve_ollama_url()
    print(f"  → Ollama URL: {url}")
    backend = create_backend(backend_type="ollama", model=model_id, url=url)
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
            print(f"  ∞ ITER {payload.get('iter_count')} START — "
                  f"{payload.get('item_id')}: {payload.get('item_title','')[:60]}")
        elif kind == "autonomous_iteration_complete":
            summary["iter_complete"] += 1
            sha = (payload.get("commit_sha") or "")[:7]
            dur = payload.get("duration_seconds", 0)
            summary["iter_durations"].append(dur)
            print(f"  ∞ ITER {payload.get('iter_count')} DONE — "
                  f"sha={sha or '<empty>'} ({dur:.1f}s)")
        elif kind == "autonomous_iteration_failed":
            summary["iter_failed"] += 1
            print(f"  ✗ ITER {payload.get('iter_count')} FAILED — "
                  f"{payload.get('error', '')}")
        elif kind == "autonomous_reflection":
            summary["reflections"] += 1
            tally = payload.get("pass_tally", {})
            verdict = payload.get("verdict")
            ac = payload.get("acceptance_summary", {})
            added_n = len(payload.get("added", []))
            print(f"  ∞ REFLECT verdict={verdict} accept={ac.get('passed',0)}/"
                  f"{ac.get('total',0)} bash={tally.get('bash_passed',0)}p/"
                  f"{tally.get('bash_failed',0)}f added={added_n}")
        elif kind in {
            "autonomous_mission_complete",
            "autonomous_mission_paused",
            "autonomous_mission_failed",
        }:
            summary["ended"] = True
            summary["end_reason"] = payload.get("stop_reason") or kind
            print(f"  ∞ {kind.upper().replace('AUTONOMOUS_MISSION_','')} — "
                  f"reason={payload.get('stop_reason')} "
                  f"elapsed={payload.get('elapsed_seconds', 0):.0f}s")

    print(f"-" * 70)
    print(f"Dispatching mission with {model_label} ({model_id})…")
    print(f"-" * 70)

    started_at = time.time()

    try:
        daemon = start_autonomous_mission(
            state=state,
            intent_id=f"roguelite-{model_label}",
            feature="bootstrap-roguelite smoke",
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
            print(f"  · still running — iter={snap['iter_count']} "
                  f"elapsed={snap['elapsed_seconds']:.0f}s "
                  f"verdict={snap['verdict']}")
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
    print(f"  is_running:              {snap['is_running']}")
    print(f"  iter_count:              {snap['iter_count']}")
    print(f"  daemon_elapsed:          {snap['elapsed_seconds']:.1f}s")
    print(f"  smoke_total_elapsed:     {total_elapsed:.1f}s")
    print(f"  verdict:                 {snap['verdict']}")
    print(f"  stop_reason:             {snap.get('stop_reason','')}")
    print(f"  iter started/done/failed: "
          f"{summary['iter_started']}/{summary['iter_complete']}/{summary['iter_failed']}")
    print(f"  reflections:             {summary['reflections']}")
    if summary["iter_durations"]:
        avg = sum(summary["iter_durations"]) / len(summary["iter_durations"])
        print(f"  avg iter duration:       {avg:.1f}s")
    print(f"  events captured:         {len(events)}")

    print()
    print("Project file listing:")
    for p in sorted(project.iterdir()):
        if p.name in {".git", ".resonant"}:
            continue
        if p.is_dir():
            print(f"  {p.name}/")
            for child in sorted(p.iterdir()):
                print(f"    {child.name}")
        else:
            print(f"  {p.name} ({p.stat().st_size}b)")

    print()
    print("Acceptance-criteria final state:")
    rm_path = project / ".resonant" / f"roadmap-roguelite-{model_label}.md"
    if rm_path.exists():
        rm_text = rm_path.read_text(encoding="utf-8", errors="replace")
        for line in rm_text.splitlines():
            if line.startswith("- [") and "[bash]" in line:
                print(f"  {line}")

    record = {
        "model_label": model_label,
        "model_id": model_id,
        "spec_markdown": SPEC_MD,
        "smoke_total_elapsed_seconds": total_elapsed,
        "daemon_state": snap,
        "summary": summary,
        "project_path": str(project),
    }
    record_path = ROOT / f"smoke-roguelite-{model_label}.json"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print()
    print(f"Run record saved: {record_path}")

    print()
    if snap["verdict"] == "satisfied":
        print(f"✅ {model_label.upper()} CONVERGED")
        return 0
    if summary["iter_complete"] >= 1:
        print(f"⚠ {model_label.upper()} PARTIAL — "
              f"{summary['iter_complete']} iter(s), "
              f"stop_reason={snap.get('stop_reason')}")
        return 0
    print(f"✗ {model_label.upper()} FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
