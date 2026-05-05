# Overnight handoff — 2026-05-05

**Run window:** ~5h autonomous; budget was 8-9h, ended early to land a thorough morning doc.
**Driving instruction:** "push v0.5.11; bundle findings into the next builds; work autonomously through the next set of things; E2E after every build; review + prep the next round."

---

## What shipped overnight

Three coherent minor versions, eight alphas, all green, all pushed to origin. Standing rule from the previous session — "MCP browser preview pass after every alpha+GA before moving on" — applied to every commit. Every preview pass was clean: zero failed network requests, zero console errors, layout intact.

| Version | Theme | Net tests | Coverage delta |
|---------|-------|----------:|----------------|
| [v0.5.12](v0.5.12-release-notes.md) | Findings + legacy + harness | +47 | engine/server.py 0%→24%, harness/state.py 61%→99% |
| [v0.5.13](v0.5.13-release-notes.md) | engine/session.py coverage | +52 | engine/session.py 62%→73% |
| [v0.5.14](v0.5.14-release-notes.md) | Harness coverage push | +100 | harness/service.py 15%→69%, harness/orchestrator.py 28%→45% |

**Test count:** 1884 (v0.5.11) → **2083** (v0.5.14). **+199 net new tests** in the overnight run. Full suite stays green throughout.

**Origin state:** every commit + tag pushed. `git log origin/main..HEAD` is empty. 11 new tags on origin.

## Per-alpha breakdown

### v0.5.12

- **a1 — Geist Mono CDN drop.** Followed up on the v0.5.11 E2E finding. The Chrome ORB block on `cdn.jsdelivr.net/.../geist-mono/style.min.css` is gone; mono falls through to system Cascadia Code (Win) / SF Mono (mac) / Consolas etc. Visual delta near-zero.
- **a2 — engine/server.py triage.** Investigation surfaced a real architectural finding: the paired client (`resonant connect`) was removed in v0.4.4, but the server (`resonant serve`) was left in place. Asymmetric legacy. Documented as `⚠ Legacy status` in the docstring + 11 minimal construction-level tests so import-level regressions catch loudly. **Open question: should `resonant serve` be removed for symmetry?** See "Decisions for you" below.
- **a3 — harness/state.py lifecycle.** 36 tests covering set_active_sprint, record_evaluator_verdict, set_contract_status, the update_* helpers, run_history JSONL append/read, teacher escalations. Had been at 61%; now 99%.

### v0.5.13

- **a1 — engine/session.py small-method coverage.** 43 tests covering parse_choices, parse_markdown_todos, strip_tool_call_tags (module-level parsers) + Session methods that were untested cross-cutting (is_subagent, tools, clear, cancel, _log_event, _should_auto_approve, _cancelled_events, should_plan, set_backend, copy_execution_context_from). 62% → 70%.
- **a2 — session.py run() branches.** 9 tests covering cleanly-isolatable run() branches (multimodal image content shape, cancellation-before-loop, _execute_task unknown-agent-type early return). 70% → 73%.

### v0.5.14

- **a1 — harness/service.py.** 50 tests. Full coverage of normalize_contract_status (16 alias + role-fallback cases), the static helpers (_truncate_text, _normalize_string_list, _normalize_string_mapping), build_output_contract, build_instructions, get_summary integration. 15% → 69%.
- **a2 — harness/orchestrator.py static helpers.** 50 tests covering the static classification helpers that drive WHICH role runs next in the planner→generator→evaluator cycle (_choose_next_role state machine, _is_generator_ready_contract, _repairable_generator_failure, _is_retryable_failure, _summary_signature, _should_auto_approve, _completion_message) + dataclass to_dict methods (HarnessCycleStep result-truncation contract). 28% → 45%.

## Findings + observations from the run

### From the v0.5.11 post-build E2E

- **Geist Mono CDN ORB-block.** Shipped fix in v0.5.12a1.

### From v0.5.12a2 investigation

- **engine/server.py asymmetry.** The `resonant serve` mode runs a JSON-over-WS server that was historically paired with `resonant connect`. v0.4.4 removed the client without removing the server. There is no bundled client today that knows how to connect to it. External tools could still drive it via the documented protocol (commands: message/clear/switch_model/cancel) but none ship with the project. Triage decision deferred — see Decisions below.

### From v0.5.13a2 testing

- **`session.py` cancel-out cleanup contract.** When `cancel()` is called BEFORE `run()`, the user message IS still recorded in conversation_history (the cancel-check happens AFTER the history.append). This is fine but worth knowing — a UI that displays conversation history right after a Stop will see the user's message even though no model response was generated. The TUI/GUI both already handle this correctly (the stop button doesn't try to "rewind" history); just lock it in via test.

### From the post-build E2E rounds (8 separate preview passes)

Every pass was clean. The GUI consistently:
- Renders the title, File/Edit/View/Help menu, project pill, model chip
- Shows the sidebar with real session data (project hashes, "X ago" timestamps)
- Backend chip shows `ollama · deepseek-v4-flash:cloud` (confirms v0.5.7a1 default-model resolver still working — would have fired if the chip was ever blank or wrong-model)
- Zero console errors / warnings

No regressions in the GUI from any of the eight overnight changes. The Geist fix held across all subsequent E2E passes.

## Decisions for you

These are all flagged as deferred during the autonomous run, in priority order of how much the next minor depends on them:

### 1. **Field run** (still the highest-leverage move)

Same pick as last time. Two MORE minors shipped without the field-run signal — that's now four hygiene minors total since the last validation (v0.5.6→v0.5.9 + v0.5.10→v0.5.14). The architecture-vs-model question keeps waiting.

Prep doc unchanged: [docs/field-observations/2026-05-04-NEXT-RUN-PREP.md](field-observations/2026-05-04-NEXT-RUN-PREP.md). 2-3h of your live attention. Watchlist still maps every v0.5.6→v0.5.9 fix to a live observation cue.

### 2. **Should `resonant serve` be removed?**

`engine/server.py` (the WS server) and the `serve` subcommand in `tui.py:1420` are orphaned post-v0.4.4 — the bundled `resonant connect` client was removed but the server wasn't. The legacy docstring + tests are in place to catch regressions, but the question of whether to fully remove is yours.

**Options:**
- **Remove for symmetry.** Drop `engine/server.py`, the `serve` subcommand in tui.py, the test file. ~250 lines deleted. Matches the v0.4.4 logic.
- **Keep + commit to maintaining.** External integrations could still use it; document the protocol explicitly so external tools know what they can drive.
- **Status quo.** Leave the legacy marker, defer the decision again.

Recommendation: **Remove**. The v0.4.4 cut already established the precedent; the asymmetry has no users today. ~30 min of work to do cleanly.

### 3. **Harness deeper coverage push**

`harness/orchestrator.py` is now at 45% — the static helpers are covered. The active background-cycle methods (_run_cycle, _attempt_role_retry, _attempt_teacher_recovery + the public lifecycle methods that start threads) remain at 0% because they need a stub backend harness + threading discipline.

Same shape applies to `engine/session.py`'s remaining 27% — the run() loop's tool-dispatch / doom-loop / compression-fires / choice-handling branches all need a `backend.stream()` stub harness.

Building that harness is a real investment (~half a day) but unlocks 20+ percentage points of coverage on TWO of the most central modules in the codebase. Worth doing if a future minor wants to commit to it.

## Next-round candidates (in roughly highest leverage first)

| # | Item | Why | Cost |
|---|------|-----|------|
| 1 | **Field run (mdcheck)** | Unblocks v0.5.15+ direction; four hygiene minors deep without signal | 2-3h, your hands |
| 2 | **Strategic decision: remove `resonant serve`?** | Architectural cleanup; legacy with no users | One question to me, ~30min execution |
| 3 | **Validate unvalidated smoke specs** (`jsonlines`, `refactor-py`) on Mac Studio | Pin convergence numbers; flip `validated=True` | 30-60min, your hands |
| 4 | **Stub backend.stream() harness** for deeper session.py + orchestrator.py coverage | Unlocks 20+ pts on two central modules | ~half a day autonomous |
| 5 | `harness/service.py:build_resume_prompt` — the deferred 31% | Push service.py to ~95% | ~30min autonomous |
| 6 | `engine/mcp.py` (41%) | MCP integration; testable but moderately complex | ~1h autonomous |
| 7 | `engine/clipboard.py` (30%) / `engine/accessibility.py` (20%) | OS-specific; harder | Variable |
| 8 | Promote per-specialist routing defaults (P2) | Wait until P0 confirms it helps live | Trivial after P0 |

## P0-P3 carry-over (unchanged from v0.5.6+)

- **P0** — Field-validate via mdcheck. Highest priority. Inherently human-in-the-loop.
- **P1** — Validate unvalidated smoke specs on Mac Studio.
- **P2** — Promote per-specialist routing to defaults (waits on P0).
- **P3** — Parallel sub-mission dispatch (architectural; waits on P0).

## State at handoff

- Working tree: clean
- Branch: `main`, in sync with origin
- Test suite: 2083 passed, 2 skipped, ~70s run time
- Total tags on local + origin: synced
- Last commit: `29b5c80` (v0.5.14 GA — harness coverage push)
- Memory: standing E2E rule saved as `feedback_post_build_e2e` (applies to future builds automatically — every alpha + GA gets a preview pass before the next alpha starts)

## My recommendation for first move when you wake up

**Push the field run.** It's the highest-leverage thing and we keep deferring it. The longer we wait, the more we're guessing about what to ship next. If you have the 2-3h block today, mdcheck is dispatch-ready and the watchlist now covers FOUR minors of post-v0.5.5 changes (v0.5.6→v0.5.14) — you'll get a much richer post-mortem from a single field run than you would have got two minors ago.

If today isn't a field-run day: tell me to remove `resonant serve` (decision #2 above). It's the cleanest single architectural decision left in the queue, and the work fits in ~30min.

If you want me to keep grinding hygiene: build_resume_prompt → 95% on harness/service.py is the cheapest remaining gain.

— EOF —
