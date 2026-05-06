# Resonant Client — Roadmap

Post-refocus, the client is a single-purpose agentic coder: Ollama + `deepseek-v4-flash:cloud` on the Mac Studio (`10.0.0.133`), with a clean Agent + Settings UI. The original 8 clusters below were the foundation (shipped pre-v0.2.0); see the "Post-refocus state" section further down for the v0.3.x–v0.5.9 evolution.

## Foundation status (pre-refocus, 2026-04-28)

| # | Cluster | Plan | Status | Tasks shipped | Notes |
|---|---------|------|--------|---------------|-------|
| 1 | Computer-Use upgrades | [PLAN-COMPUTER-USE.md](PLAN-COMPUTER-USE.md) | ✅ Shipped | 8 / 8 | All tools registered in `engine/tools.py`; 25 tests in `tests/test_computer_use_upgrades.py` pass |
| 2 | Session ergonomics | [PLAN-SESSION-ERGONOMICS.md](PLAN-SESSION-ERGONOMICS.md) | ✅ Shipped | 4 / 4 | Fork + inline-diff + replay-scrubber + voice all wired in `gui/static/app.js` |
| 3 | deepseek-v4-flash specific | [PLAN-DEEPSEEK.md](PLAN-DEEPSEEK.md) | ✅ Shipped | 3 / 3 | Thinking-mode toggle + big-context profile + `get_runtime_telemetry()` |
| 4 | Codebase intelligence | [PLAN-CODEBASE-INTELLIGENCE.md](PLAN-CODEBASE-INTELLIGENCE.md) | ✅ Shipped | 4 / 4 | Auto-lint + auto-test + 5 git tools + 6 REPL tools, all gated by settings |
| 5 | Harness migration | [PLAN-HARNESS-MIGRATION.md](PLAN-HARNESS-MIGRATION.md) | ✅ Shipped | 5 / 5 | Sprint workflow now opt-in (default off), state moved to `~/.resonant/projects/<hash>/harness/`, AGENTS.md adopted as primary project-conventions file |
| 6 | Organic orchestration | [PLAN-ORGANIC-ORCHESTRATION.md](PLAN-ORGANIC-ORCHESTRATION.md) | ✅ Shipped | 5 / 5 | Five primitives (Intent · Plan-graph · Specialist · Reflection · Skill library); live plan-graph viz in preview panel; full autonomy with irreversibility-floor checkpoints + per-intent audit log |
| 7 | Intent wiring (live flow) | [PLAN-INTENT-WIRING.md](PLAN-INTENT-WIRING.md) | ✅ Shipped | 5 / 5 | `LocalSpecialistRunner` + `IntentService` connect orchestration to user input; `/plan` slash-prefix and "Plan this" button kick off real intents; floor enforcement + audit log fire from inside live tool dispatch; e2e stub-backend test passes |
| 8 | **Distribution: Windows installer + auto-update** | [RELEASING.md](RELEASING.md) + [docs/release-pipeline.md](docs/release-pipeline.md) | ✅ Shipped (v0.2.0) | 4 / 4 | PyInstaller bundle + Inno Setup installer (~26 MB) + WinSparkle auto-update + EdDSA signing + GitHub Pages appcast + tag-push CI workflow |

**Total: 38 / 38 atomic tasks shipped** across the 8 clusters.

**Released artifacts:**

- v0.2.0 installer: <https://github.com/Luminary-Analytics/resonant-client/releases/tag/v0.2.0>
- Auto-update channel: <https://luminary-analytics.github.io/resonant-client/appcast.xml>

End-to-end verification:

```bash
cd D:/Repos/resonant-client

# All cluster tests in one run (123 tests)
python -m pytest tests/test_computer_use_upgrades.py \
                  tests/test_deepseek_specific.py \
                  tests/test_session_ergonomics.py \
                  tests/test_git_tools.py \
                  tests/test_repl.py \
                  tests/test_auto_lint.py \
                  tests/test_auto_test.py -q

# Tool registry sanity check (52 tools)
python -c "from resonant_client.engine import tools; \
  names = sorted({t['function']['name'] for t in tools.AGENT_TOOLS}); \
  print(len(names), 'tools'); print('\n'.join(names))"
```

## Post-refocus state (v0.3.x → v0.5.9)

After the v0.4.0 hard cut to Ollama-only (Anthropic / OpenAI / Claude-Code / Codex / MLX / LM-Studio backends removed), development moved from cluster-batch planning to release-by-release iteration informed by smoke runs and field observations. Eight capability tracks emerged from accumulated work; the table below maps each to its anchoring releases.

### Capability tracks (as of 2026-05-04)

| # | Track | Anchored in | Notes |
|---|-------|-------------|-------|
| 9  | Engine + agentic loop | v0.4.x | Ollama-native session runner. Tool palette: file ops, bash, glob/grep, browser (Playwright), MCP, RAG codebase index, hooks, diff review, sandbox. v0.4.0 cut all non-Ollama backends. |
| 10 | Discovery → Mission seam (rigorous grill) | v0.5.x | Structured interview → typed acceptance criteria + time budget. 5-beat exemplar (acknowledge → bridge → options → recommend → invite override) codified in v0.5.7a5. |
| 11 | Autonomous Mission daemon | v0.5.x | Roadmap-driven outer loop. Picks unchecked items → dispatches Phase-1 sub-missions → REFLECT every K iters → 7 priority-ordered stop rules. Resume + orphan detection. Atomic terminal-state transitions (v0.5.6a3). Pause-after-iter (v0.5.9a4). |
| 12 | REFLECT specialist | v0.5.x | Deterministic `[bash]`/`[vision]` pre-pass + model-driven `[chrome]` validation + structured JSON verdict. Failure-annotation pattern (v0.5.7a5). Decision-request schema for human-in-the-loop forks (v0.5.8a2). Verdict-override provenance (v0.5.9a3). |
| 13 | Per-specialist routing | v0.5.8a1 | Pin different Ollama models per `NodeSpecialization` (settings.json or env-var). Pro for REFLECT/PLAN_DEEP, flash for IMPLEMENT/EXPLORE. Defaults still flash-only (P2 carry-over below). |
| 14 | Smoke harness | v0.5.0–v0.5.8 | `resonant-smoke run/variance/baseline/ci`. 5 specs (3 validated, 2 unvalidated). Seed-files mechanism for refactor-style specs (v0.5.8a4). Per-project baselines under `~/.resonant/projects/<hash>/.resonant/smoke-baselines/`. |
| 15 | GUI evolution | v0.4.x–v0.5.x | Chat + plan-graph + inspector + harness UI + diff review + image attachments + cost tracker + permission modes + mission browser (v0.5.5a2) + orphan banner + decision card (v0.5.8a2) + iter folding (v0.5.8a3) + activity panel (v0.5.9a1) + Pause/Stop affordances (v0.5.9a4). |
| 16 | Diagnostics + cost tracking | v0.5.9 | Full-fidelity ZIP bundle with redaction. Per-iter cost attribution + per-model split (v0.5.9a2). Daily cost tracking with budget alerts. Mission-summary index (v0.5.9a5). |

### Release timeline

| Version | Date | Theme | Notes |
|---------|------|-------|-------|
| v0.5.0–v0.5.2 | 2026-05-02 | Autonomous mission daemon foundation + first smoke runs | Convergence proven on 4-of-5 pro runs |
| v0.5.3 | 2026-05-03 | Resume after interrupt + sidebar inspector + smoke CLI | [release notes](docs/v0.5.3-release-notes.md) |
| v0.5.4 | 2026-05-03 | PLAN cleanup + flaky-planner harness + markdown reports | [release notes](docs/v0.5.4-release-notes.md) |
| v0.5.5 | 2026-05-03 | Variance baselines + mission browser + smoke CI | [release notes](docs/v0.5.5-release-notes.md) |
| v0.5.6 | 2026-05-03 | Linux-bridge field findings → guard rails (a1–a4) | [release notes](docs/v0.5.6-release-notes.md) |
| v0.5.7 | 2026-05-04 | Field-finding backlog burn-down (a1–a5) | [release notes](docs/v0.5.7-release-notes.md) |
| v0.5.8 | 2026-05-04 | Per-specialist routing + decision-request + iter folding + smoke specs | [release notes](docs/v0.5.8-release-notes.md) |
| v0.5.9 | 2026-05-04 | QoL + observability (activity / cost / override / pause / diagnostics) | [release notes](docs/v0.5.9-release-notes.md) |
| v0.5.10 | 2026-05-04 | Ground truth — ROADMAP refresh + cross-feature tests + smoke hardening | [release notes](docs/v0.5.10-release-notes.md) |
| v0.5.11 | 2026-05-04 | Coverage + clarity — under-tested modules deepened, docs freshened | [release notes](docs/v0.5.11-release-notes.md) |
| v0.5.12 | 2026-05-05 | Findings + legacy + harness — Geist CDN drop, engine/server.py triage, harness/state.py lifecycle | [release notes](docs/v0.5.12-release-notes.md) |
| v0.5.13 | 2026-05-05 | session.py coverage — small methods + run() branches + module parsers (62%→73%) | [release notes](docs/v0.5.13-release-notes.md) |
| v0.5.14 | 2026-05-05 | Harness coverage — service.py (15%→69%) + orchestrator.py static helpers (28%→45%) | [release notes](docs/v0.5.14-release-notes.md) |
| v0.5.15 | 2026-05-05 | build_resume_prompt — harness/service.py finished (69%→100%) | [release notes](docs/v0.5.15-release-notes.md) |
| v0.5.16 | 2026-05-06 | Remove `resonant serve` — symmetric with the v0.4.4 `connect` removal | [release notes](docs/v0.5.16-release-notes.md) |

Test count growth: ~1300 (v0.5.2) → 1652 (v0.5.6) → 1691 (v0.5.7) → 1751 (v0.5.8) → 1790 (v0.5.9) → 1823 (v0.5.10) → 1884 (v0.5.11) → 1931 (v0.5.12) → 1983 (v0.5.13) → 2083 (v0.5.14) → 2102 (v0.5.15) → 2091/2 (v0.5.16; -11 from removing test_engine_server.py).

### Linux-bridge field-observation backlog: COMPLETE

The first ambitious-greenfield stress test (linux-bridge, 2026-05-03) produced 12 actionable findings. All 12 shipped:

| # | Finding | Shipped in |
|---|---------|-----------|
| 1 | 503 retry invisibility | v0.5.6a1 |
| 2 | spec truncation crash | v0.5.6a2 |
| 3 | folder picker hang | v0.5.6a4 |
| 4 | default_model on switch | v0.5.7a1 |
| 5 | iter counter desync | v0.5.7a2 |
| 6 | stuck-verdict state drift | v0.5.6a3 |
| 7 | renderer pinned by long run | v0.5.8a3 |
| 8 | leading-dash filename guard | v0.5.7a3 |
| 9 | REFLECT failure annotation | v0.5.7a5 |
| 10 | path-mismatch deadlock | v0.5.8a2 |
| 11 | dispatch-card visual clutter | v0.5.7a4 |
| 12 | grill quality codification | v0.5.7a5 |

Full post-mortem at [`docs/field-observations/2026-05-03-resonant-linux-bridge.md`](docs/field-observations/2026-05-03-resonant-linux-bridge.md).

## Open work (carry-over to v0.5.10+)

| Priority | Item | Status |
|----------|------|--------|
| P0 | Field-validate v0.5.6→v0.5.9 by running mdcheck (or another greenfield prompt) | Prep doc dispatch-ready at [`docs/field-observations/2026-05-04-NEXT-RUN-PREP.md`](docs/field-observations/2026-05-04-NEXT-RUN-PREP.md). Inherently human-in-the-loop. |
| P1 | Validate the unvalidated smoke specs (`jsonlines`, `refactor-py`) on Mac Studio | Convergence numbers don't exist yet; `resonant-smoke list-specs` shows `[unvalidated]` markers. |
| P2 | Promote per-specialist routing to defaults (pro for REFLECT/PLAN_DEEP) | Wait until P0 confirms it actually helps live. |
| P3 | Parallel sub-mission dispatch | Risky daemon-iter-model change. Big payoff (2-4× throughput on independent items). Waits on P0 signal. |

The signal for the next architectural fork (model-as-bottleneck vs architecture-as-bottleneck) lives behind the next field run.

## How to read each plan

Each `PLAN-*.md` has:

- **Objective** — what and why the cluster exists
- **Context** — files and functions a future executor needs to read first
- **Prior art** — things that already exist; do NOT reinvent
- **Tasks** — atomic units with files, action, verify, done-when, plus a ✅/⚠️/⏳ status marker
- **Overall verification** — copy-paste commands that work against the repo today
- **Success criteria** — measurable, used to confirm "done"
- **Future / nice-to-haves** — ideas not yet built; pick one to extend the cluster

Status markers used inside each plan:

| Marker | Meaning |
|--------|---------|
| ✅ Shipped | Lives in `main`; tests cover it; verify command passes |
| ⚠️ Partial | Some sub-piece works; gap noted in "What's missing" line |
| ⏳ Pending | Not started; ready to execute |

## Recommended order (for re-executing from scratch)

The clusters are independent. If you needed to rebuild the whole feature surface in a new repo, the leverage-per-task ordering would be:

1. **Codebase intelligence** first — auto-lint and first-class git tools are productivity multipliers for every subsequent phase.
2. **Computer-Use upgrades** second — biggest visible capability boost. Accessibility-tree targeting (Task 1.4) is the foundation that makes later automations of real Windows apps reliable.
3. **deepseek-v4-flash specific** third — quality-of-life on the model layer. Smaller, benefits from a stable foundation.
4. **Session ergonomics** fourth — UI polish. Inline diff and session replay are nice but not load-bearing.

Within a cluster, tasks can typically be done in any order. Hard dependencies are called out per-task.

## Out of scope (deferred or rejected)

- **Mobile app** — not a target. Desktop / browser only.
- **Multi-user collaboration** — single-user IDE.
- **Plugin marketplace** — MCP servers (already wired) cover the extension story.
- **Cloud sync of sessions** — sessions stay in `~/.resonant/projects/`. If you want sync, point Dropbox/iCloud at that folder.
- **Cross-LLM tool proxying** — every tool runs on the same machine as the engine; no remote tool execution.

## Living document

**Foundation (pre-v0.2.0):** when a new task is added to a cluster (1–8), append it to its `PLAN-*.md` and bump the count in the foundation status table above.

**Post-refocus (v0.3.x+):** development is release-organized rather than cluster-batched. Each minor version gets a `docs/vX.Y.Z-release-notes.md` with a TL;DR table + per-alpha sections + validation block + carry-over. When a new capability track stabilizes (≥2 minor versions of work, with public surface area), add it as a row in the "Capability tracks" table above. When a row's notes get long, split the track into its own `PLAN-*.md` and link.
