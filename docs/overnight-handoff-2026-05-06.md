# Overnight handoff — 2026-05-06

**Run window:** ~6h autonomous out of 8-9h budget. Shipped the Tier-1 strategic cleanup + the Tier-2 stub-harness investment + the cascade of coverage pushes that the harness unlocked.
**Driving instruction:** "Remove `resonant serve`. Ship it. Spend the rest of the night on the high-value automation. Morning handoff."
**All four boxes ticked.**

---

## What shipped overnight

**Three minor versions, eight alphas, +63 tests, all green, all on origin.** The ratio of payoff to alpha count is way higher this run than prior nights — the streaming-stub harness was a real infrastructure investment that paid off across two modules.

| Version | Theme | Net tests | Coverage delta |
|---------|-------|----------:|----------------|
| [v0.5.16](v0.5.16-release-notes.md) | Remove `resonant serve` (orphaned legacy) | -11 (deleted test file) | engine/server.py deleted entirely |
| [v0.5.17](v0.5.17-release-notes.md) | Stub backend.stream() harness + session.py coverage | +45 | engine/session.py 73%→**83%** |
| [v0.5.18](v0.5.18-release-notes.md) | orchestrator.py public lifecycle | +18 | harness/orchestrator.py 45%→**72%** |

**Test count:** 2091 (v0.5.16 baseline) → **2154** (v0.5.18). **+63 net new in this run** (and +52 if you count session.py + orchestrator.py coverage tests separately from the engine/server.py removal).

**Origin state:** every commit + tag pushed. `git log origin/main..HEAD` is empty.

## Per-alpha breakdown

### v0.5.16 — Remove `resonant serve`
The strategic decision queued in v0.5.12a2's triage and confirmed by you tonight. The asymmetric leftover from v0.4.4 (server kept, client removed) is gone:

- `resonant_client/engine/server.py` — deleted (191 lines)
- `tests/test_engine_server.py` — deleted (the v0.5.12a2 triage tests)
- `tui.py` — removed the `serve` subparser, dispatch, help text. Added a v0.5.16 sibling comment next to the v0.4.4 `connect`-removal note so anyone tracing the history finds both context lines together.

### v0.5.17 — Stub backend.stream() harness + session.py
The half-day infrastructure investment. **`tests/streaming_stub.py` is now a project asset** — any future test that wants to drive `Session.run()` deterministically uses it.

- **a1 (21 tests)** — Built `StreamingBackend` with declarative event constructors (`text_delta`, `tool_call`, `done`, `error`, `backend_status`). Per-call scripts for multi-iteration tests. Plus 21 happy-path / error / status / exception / cancellation tests + harness sanity checks.
- **a2 (11 tests)** — Tool-dispatch denial branches: hook denial, policy denial, permission tier denial, await_user (with + without callback), malformed JSON args, cancel mid-loop.
- **a3 (13 tests)** — Text-branch coverage: TODOS_UPDATED parsing, CHOICES flow with on_choice callback, handles_tools CLI shortcut, plan_mode early-return.

Net: `engine/session.py` went from 73% → **83%** (+10 percentage points). 161 missed lines → 106 missed lines.

### v0.5.18 — orchestrator.py public lifecycle
The harness investment paid off again — same testing approach (stub callables), different module.

- **a1 (18 tests)** — `start_cycle` / `list_runs` / `get_run` / `cancel` + a `TestSingleRoleCycle` class that runs the full background-cycle dispatch end-to-end with stub callables. Includes the duplicate-project-rejection contract, max_loops clamping, role_runner exception → status=FAILED path, cancel-during-runner path.

Net: `harness/orchestrator.py` went from 45% → **72%** (+27 percentage points).

## The harness investment, expanded

`tests/streaming_stub.py` (135 lines) is now usable for any test that wants to drive `Session.run()` or any composer that reads the same event protocol. The shape:

```python
from tests.streaming_stub import StreamingBackend, text_delta, tool_call, done

# Single-call sequence:
backend = StreamingBackend(events=[
    text_delta("Hello"),
    tool_call("file_read", {"path": "x"}, call_id="c1"),
    done(),
])

# Multi-iteration script (one entry per stream() call):
backend = StreamingBackend(scripts=[
    [text_delta("first iter"), done()],
    [text_delta("second iter"), done()],
])

# Error injection:
backend = StreamingBackend(raise_on_stream=RuntimeError("boom"))
```

Plus assertion helpers: `kinds_of(events)`, `events_of_kind(events, "tool.call")`, `first_of_kind(events, "session.end")`.

If any future minor wants to push session.py the rest of the way (the actual tool execution branches at lines 1025-1115, or the `_execute_task` sub-agent forwarding), the harness is the right starting point. Same for finishing orchestrator.py (`_attempt_role_retry` + `_attempt_teacher_recovery`).

## Findings + observations

### From the eight E2E preview passes
Every pass was clean. Zero failed network requests, zero console errors, layout intact. The Geist-Mono fix from v0.5.12a1 continues to hold; no regressions across any of the eight overnight changes.

### Architectural state
With v0.5.16's removal of `resonant serve`, the post-refocus product surface is now consistent:
- **GUI-first** via `gui/server.py` (live, tested, used by `resonant-gui`)
- **TUI for embedded mode** via `resonant` (no orphan paths)
- **Smoke harness** for autonomous validation
- **No legacy WS server** floating around with no client

The v0.4.4 cut's loose end is closed.

### Test architecture maturity
This is the first night where the test investment changed the *shape* of how future tests get written, not just the count. The streaming-stub harness is a one-time build that lowers the cost of every future agentic-loop test from "build inline mocks" to "compose declarative event lists." Coverage on the central agentic-loop modules (session.py + orchestrator.py) jumped meaningfully because of it — the same modules will be cheaper to test against any future regression.

## Decisions queued for you (priority order)

### 1. Field run (still highest-leverage)
**Six** hygiene minors deep without field-run signal (v0.5.10 → v0.5.18 GAs, plus v0.5.6 → v0.5.9 before that). The architecture-vs-model question keeps waiting. Prep doc unchanged: [docs/field-observations/2026-05-04-NEXT-RUN-PREP.md](field-observations/2026-05-04-NEXT-RUN-PREP.md). 2-3h of your live attention.

The watchlist now covers SIX minors of post-v0.5.5 changes. A single field run will produce a much richer post-mortem than two minors ago.

### 2. Validate the unvalidated smoke specs (`jsonlines`, `refactor-py`)
Mac Studio + 30-60min. Pin convergence numbers, flip `validated=True`. P1 carry-over since v0.5.6.

### 3. Optional: finish orchestrator.py + session.py the rest of the way?
If the answer is "yes, ship coverage all the way to ~95%," the harness is in place — just need ~half-day to write retry-backend stub + teacher-escalator stub + execute_tool stub, then 30-50 more tests across the remaining branches. Not high-leverage on its own; only worth doing if you want the central agentic-loop modules at near-100% coverage as a ship-readiness gate.

## Next-round candidates (highest leverage first)

| # | Item | Cost |
|---|------|------|
| 1 | **Field run (mdcheck)** | 2-3h, your hands |
| 2 | **Validate jsonlines + refactor-py smoke specs on Mac Studio** | 30-60min, your hands |
| 3 | Finish orchestrator.py recovery paths (`_attempt_role_retry` + `_attempt_teacher_recovery`) | ~half-day autonomous |
| 4 | Finish session.py — tool execution (lines 1025-1115) + `_execute_task` (lines 1403-1494) | ~half-day autonomous |
| 5 | `engine/mcp.py` (41%), `engine/clipboard.py` (30%), `engine/accessibility.py` (20%) | Variable |
| 6 | Promote per-specialist routing defaults (P2) | Trivial after P0 |

P0-P3 carry-over from v0.5.6 unchanged.

## State at handoff

- Working tree: clean
- `main` in sync with origin
- Test suite: 2154 passed, 2 skipped, ~37s run time (faster than before — the harness tests are tighter than the prior cumulative integration tests)
- Last commit: `a9e2da9` (v0.5.18 GA — orchestrator.py public lifecycle)
- 11 new tags on origin (3 GAs + 8 alphas)
- E2E rule still standing as auto-memory `feedback_post_build_e2e`

## Total project state

| Metric | At v0.5.11 (8d ago) | After tonight |
|---|--:|--:|
| Test count | 1884 | **2154** (+270) |
| `engine/session.py` coverage | 62% | **83%** |
| `harness/state.py` coverage | 61% | 99% |
| `harness/service.py` coverage | 15% | **100%** |
| `harness/orchestrator.py` coverage | 28% | **72%** |
| `engine/event_log.py` coverage | 50% | 100% |
| `engine/compression.py` coverage | 37% | 99% |
| Total project coverage | ~65% | ~78% (estimated) |
| Stable architecture cleanups | — | resonant serve removed |
| Reusable test infra | — | streaming_stub.py |

The gap from "what we've built" to "what we've validated" hasn't shrunk on the field-run dimension — but on the test-coverage dimension, it's narrower than it has been since the v0.4.0 refocus.

## My recommendation when you wake up

Same as last morning, but with stronger conviction now that we're six hygiene minors deep:

**Push the field run.** The hygiene work is in genuinely good shape — central modules at 72-100% coverage, no orphan code paths, reusable test harness in place. The remaining ~22% of project coverage is concentrated in OS-specific modules and recovery paths that don't justify a real backend stub. There is no longer a high-leverage hygiene path forward without the field signal.

If today still isn't a field-run day:
- The smoke-spec validation (#2) is half an hour. Cheap quick win.
- "Finish orchestrator + session" (#3, #4) is real value but at this point it's polishing a polished surface.

— EOF —
