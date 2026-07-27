# Resonant Client — Roadmap

The current roadmap is governed by the
[agentic harness north star](docs/agentic-harness-north-star.md). The highest
priority is a capability-driven, multimodal-ready, durable runtime for
long-running open-model coding tasks. Correct completion, verification,
maintainability, and wall-clock performance rank above token or compute
efficiency. The dated release and cluster histories below remain useful records
but do not override that direction.

Post-refocus, the client is a single-purpose agentic coder with a clean Agent +
Settings UI. It is no longer tied to one model or one provider: the harness is
capability-driven and model-neutral (see the north star above), with Ollama on
the Mac Studio (`10.0.0.133`) as the primary local path, `glm-5.2` the flagship
model, and Kimi, EXO distributed inference, and the Codex CLI as additional
backends. The original 8 clusters below were the foundation (shipped
pre-v0.2.0); see the "Post-refocus state" section further down for the
v0.3.x–v0.6.x evolution.

## Project direction (2026-05, set at v0.6.3)

**Goal: an open-source flagship for local-first, self-improving autonomous coding.** Relicensed MIT at v0.6.3. The project is used personally today; the intent is a public flagship.

- **The moat is the self-improvement loop** — an agent that gets measurably better at a codebase every mission. No other open-source coding agent has the full provenance-gated extractor/curator/loader loop. See [`docs/self-improvement-loop.md`](docs/self-improvement-loop.md).
- **Comparables are Aider / OpenHands / Cline / Goose** — not closed business-automation SaaS. The ownable gap: *local-first* + *self-improving*.
- **One repo, singular focus.** A 2026-05 evaluation of getviktor.com (an "AI coworker" SaaS) concluded **do not fork** into a Viktor-style business-automation product: flagships win on depth, and a second repo would halve attention and dilute the narrative. Take Viktor's *validation* (persistent memory + autonomous execution are the right bets), not its scope.
- **Near-term priorities:** harden the loop until the headline feature is bulletproof and demoable; then v0.7 polish (README / CONTRIBUTING / architecture docs / a reproducible demo) for the flagship launch.
- **Harness gap analysis (2026-07-01):** [`docs/harness-competitive-analysis-2026-07-01.md`](docs/harness-competitive-analysis-2026-07-01.md) benchmarks the harness against Claude Code / OpenCode / Codex / Gemini CLI / Aider / Cline / Goose and ranks 16 improvements. Its six quick wins have since landed and are no longer the open workstream: per-model `num_ctx` is capability-derived, `file_edit` runs a multi-strategy match cascade, the KV-cache prefix is byte-stable (measured at 30,203 of 30,266 prompt tokens reused — see [`docs/glm-5.2-exo-qa-2026-07-23.md`](docs/glm-5.2-exo-qa-2026-07-23.md)), `reasoning_content` is replayed across tool turns with vendor sampling pins, and Ollama structured outputs ship behind a capability check. The ranked medium/large items in that report — tool-output eviction, shadow-git checkpointing, phase-scoped tool tiering, tree-sitter repo mapping, architect/editor split — remain open.

### Open engineering workstream (2026-07-25)

Ordered by leverage, from a repo-health pass at v0.11.6:

1. ~~**No correctness CI.**~~ Fixed: `.github/workflows/tests.yml` now gates every PR and push on ruff + the full suite, and `release.yml` gates the signed installer on the same. Previously the only CI was the PyInstaller smoke build, so a 2,700-test suite ran only when someone remembered to.
2. ~~**Divergent provider wire-repair.**~~ Fixed: control-token stripping is one streaming-safe filter shared by the Ollama and Kimi/EXO paths.
3. ~~**`findstr`-based `grep`.**~~ Fixed, and now bundled: `rg.exe` is fetched and SHA-256 verified at build time by [`packaging/fetch_ripgrep.ps1`](packaging/fetch_ripgrep.ps1), shipped with its MIT/Unlicense text, and preferred over PATH at runtime so every install searches identically. `bundle-policy.json` both requires the file and *runs* it — presence is not the same as working, and a wrong-architecture or truncated binary would otherwise pass a glob check and fail silently for users. `findstr`/`grep` remain the fallback for source checkouts without it.
4. ~~**Restart-resume.**~~ Done: `AgentRegistry` already marked interrupted workers `stuck` on load and persisted their assignment; `Session.restart_agent` now re-dispatches from it through `_execute_task` — the same path a parent's `task` tool call takes, so agent-type resolution, tool filtering, backend routing, worktree isolation, and handoff construction stay in one implementation. The retry gets a fresh record and transcript, is told what its predecessor completed, and links back through `metadata.resumed_from`; the interrupted run stays readable as evidence. Reachable as a Restart control on any `stuck`/`failed` agent. **Still open:** an autonomous *mission* interrupted mid-flight resumes via `resume_autonomous_mission`, and a plain chat turn is still lost — this covers delegated workers.
5. **The monoliths.** *Two slices landed.* 46 self-contained WebSocket commands now live in [`gui/ws_commands.py`](resonant_client/gui/ws_commands.py) as handlers taking an explicit `CommandContext` instead of closing over the endpoint's locals, so each is unit-testable with a stub socket (`tests/test_ws_command_registry.py`). `websocket_endpoint` is down from ~2,580 to ~2,190 lines and from 82 commands to 58; `app.py` from 9,287 to 8,649.

   The second slice also corrected the first one's judgement: 22 handlers were moved initially and the rest written off as run-loop-coupled, but re-measuring showed most of the remainder only touched `state`/`msg`/`ws`. The real test is whether a handler needs the endpoint's *locals* (`chat_runner`, the session/backend rebuild dance), not whether it mutates application state. It also surfaced that the git helpers read a module-level `AppState` singleton, so which repository they inspected was global state — they now take an explicit `project_path`.

   *Third slice landed:* the harness cluster left `AppState` for [`harness/prompts.py`](resonant_client/harness/prompts.py) — 95 methods and 4,439 lines, roughly four fifths of the class. Prompt construction for the generator and evaluator roles is domain logic; living on the GUI server's state object meant it could only be exercised by constructing the whole application. `HarnessPrompts` reaches its host through a stated 13-name surface, and [`tests/test_harness_prompts_seam.py`](tests/test_harness_prompts_seam.py) fails if any method reaches for something outside it, so the coupling cannot quietly grow back.

   | | before | after |
   |---|---:|---:|
   | `gui/app.py` | 9,287 | 4,241 |
   | `AppState` | 5,700 lines / 130 methods | 1,296 / 39 |
   | `websocket_endpoint` | 2,398 / 82 commands | 2,186 / 58 |

   *Fourth slice landed:* `app.js` began splitting. The 52 autonomous-session methods (mission lifecycle, roadmap inspector, decision cards, health signals, run banners) moved to [`static/autonomous_view.js`](resonant_client/gui/static/autonomous_view.js), mixed onto `ResonantApp.prototype` at class-definition time. `app.js` is down from 14,500 to 12,427 lines.

   Mixin rather than ES modules: the page loads classic scripts and converting the whole app is a separate change with its own risk. Two details worth keeping in mind when adding the next one — class methods are non-enumerable, so `Object.assign` silently copies **nothing** and `applyMixin` uses property descriptors instead; and `applyMixin` throws on a name collision or a missing mixin rather than letting either fail quietly at call time. New static files must be added to `resonant.spec` **and** `bundle-policy.json`, which now also lists `plan_graph_view.js` (previously shipped but ungated). `_asset_version` globs the static directory so a new file cannot silently miss cache-busting.

   *Fifth slice landed — the run-loop coupling itself.* The endpoint's private chat state (four locals and two closures: the pending queue, the in-flight task, the cancel id, the clear cache) is now [`gui/chat_loop.ChatRunLoop`](resonant_client/gui/chat_loop.py), and `CommandContext` carries it as `ctx.runs`. Measuring the coupling rather than assuming it produced a third correction to the same over-estimate: `daemon` comes from `_get_autonomous_daemon(state, ...)` and `state.session`/`state.backend` are AppState mutations, so the autonomous and session-switching commands were never endpoint-coupled at all. The real coupling was only ever the chat state, and it was scope, not design — those handlers needed a variable that only code textually inside one 2,200-line function could reach.

   The queue ordering, cancel acknowledgement, and busy check are now testable without a socket ([`tests/test_chat_run_loop.py`](tests/test_chat_run_loop.py)); as closures they could not be tested at all.

   *Sixth slice landed — the relocation.* 53 of the 58 dispatched commands moved to `ws_commands.py` (99 handlers registered). `websocket_endpoint` is **2,186 → 606 lines with 5 commands**, and `gui/app.py` **4,241 → 2,655**.

   The five that stayed genuinely belong to the endpoint: `mission_start`, `shell_exec`, and `agent_restart` start a streaming run, and `autonomous_mission_resume` and `mission_dispatch_autonomous` build the autonomous event forwarder bound to this socket.

   Structure the mechanical pass had to learn the hard way: the dispatch chain sits at exactly 12 spaces, and `intent_*` is a single `elif command in (...)` branch containing its own nested `if command ==` chain at 20 spaces — matching those as top-level branches split one handler into six, each referencing an `intent_service` the parent had built. The endpoint's `except`/`finally` also had to bound the last branch, or it absorbed the disconnect handling.

   | | start | now |
   |---|---:|---:|
   | `gui/app.py` | 9,287 | 2,655 |
   | `AppState` | 5,700 | 1,296 |
   | `websocket_endpoint` | 2,398 / 82 cmds | 606 / 5 |
   | `app.js` | 14,500 | 12,427 |

   Still open: `app.js` at 12.4k lines — the settings/modal views and run-card renderers are the next natural cuts.
6. **Tree-sitter has no test coverage.** `code_intelligence.py` imports it; nothing in `tests/` does. CI deliberately does not install the `code-intelligence` extra rather than pretend to cover that path.

### Waiting policy (2026-07-26)

The two ways a mission can block are now one policy (`WaitPolicy` in
`gui/autonomous_loop.py`) with two deliberately distinct members, because they
need opposite recoveries:

- **Parked on a person** (`human_seconds`) — REFLECT emitted a
  `decision_request` it could not resolve alone. On expiry the daemon
  *proceeds* with the nominated option: the work is fine, only the decision was
  missing. Set per run from the launch card.
- **A sub-mission grinding** (`dispatch_seconds`) — on expiry the dispatch is
  *cancelled* and the iteration fails, because the sub-mission is the problem.
  Now derived from the time budget instead of a fixed hour, which was wrong at
  both ends: it let one sub-task consume an entire 1h mission, and it killed
  legitimately long work on a multi-day run (each kill counts toward
  `check_failed_streak_limit`, so two stop the mission).

Both report expiry through one `autonomous_wait_expired` event carrying `kind`
and `outcome`, so the GUI answers "why did this move on?" the same way
regardless of which wait ended.

Correction to the record: the stall ceiling was previously documented as
guarding against a sub-mission calling `await_user` with no GUI and blocking
forever. That is not possible — `LocalSpecialistRunner` runs sub-missions
without an `on_user_input` callback, so `await_user` returns immediately. The
ceiling guards genuine stalls only.

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
| 17 | Agent self-improvement loop | v0.6.0 | Skill provenance + bundled reference skills (v0.6.0a1). Mission-iter skill extractor with threshold heuristic + agentskills.io output format (v0.6.0a2). Deterministic curator with archive-only / never-delete semantics + REPORT.md (v0.6.0a3). Skill discovery + prompt injection with pinned-always rule + max-skills cap (v0.6.0a4). Wired into autonomous mission daemon via `extract_skill_hook` and `queue_curation_hook` on satisfied verdicts (v0.6.0 GA). Hook factory + `resonant-skill` CLI + promote/demote + auto-install (v0.6.1). Field-tested in v0.6.2: skill name generator (word-boundary trunc + drop noise prefixes), GUI Skills sidebar + detail modal, archive list + restore CLI, field-observations ingestion as user-provenance skills. Hermes-inspired three-layer pattern: prompt nudges + agent-callable tools + background curator. See [PLAN-SELF-IMPROVEMENT.md](PLAN-SELF-IMPROVEMENT.md) for the design + [docs/skills.md](docs/skills.md) for usage. |

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
| v0.5.17 | 2026-05-06 | Stub backend.stream() harness + session.py run() coverage (73%→83%) | [release notes](docs/v0.5.17-release-notes.md) |
| v0.5.18 | 2026-05-06 | orchestrator.py public lifecycle (45%→72%) — start_cycle, list_runs, get_run, cancel | [release notes](docs/v0.5.18-release-notes.md) |
| v0.6.0 | 2026-05-06 | Self-improvement loop — provenance + bundled skills + extractor + curator + loader + daemon integration | [release notes](docs/v0.6.0-release-notes.md) |
| v0.6.1 | 2026-05-06 | Self-improvement productionization — hook factory + resonant-skill CLI + promote/demote + auto-install | [release notes](docs/v0.6.1-release-notes.md) |
| v0.6.2 | 2026-05-06 | Self-improvement field-tested — skill name generator + GUI Skills sidebar + archive restore + field-obs ingest | [release notes](docs/v0.6.2-release-notes.md) |
| v0.6.3 | 2026-05-08 | Self-improvement loop closed — F1 grill Rule 0 + skill loader wired into runtime + iter-card chip; relicensed MIT | [release notes](docs/v0.6.3-release-notes.md) |
| v0.6.4 | 2026-05-16 | Cloud-variance resilience — F2 retry-exhausted chip + F6 open-phase timeout retry (grill survives a slow cloud) | [release notes](docs/v0.6.4-release-notes.md) |

Test count growth: ~1300 (v0.5.2) → 1652 (v0.5.6) → 1691 (v0.5.7) → 1751 (v0.5.8) → 1790 (v0.5.9) → 1823 (v0.5.10) → 1884 (v0.5.11) → 1931 (v0.5.12) → 1983 (v0.5.13) → 2083 (v0.5.14) → 2102 (v0.5.15) → 2091 (v0.5.16) → 2136 (v0.5.17) → 2154 (v0.5.18) → 2291 (v0.6.0) → 2350/2 (v0.6.1) → 2445/2 (v0.6.2) → 2469/2 (v0.6.3) → 2479/2 (v0.6.4).

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
