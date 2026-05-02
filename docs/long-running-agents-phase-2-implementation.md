# Phase 2 Implementation Guide — Autonomous Mission (v0.5.0)

**Audience:** future humans / LLMs picking up v0.5.0 work after a context
reset. Read this *with* `docs/long-running-agents-phase-2.md` (the design
doc) — that one is the WHY and WHAT; this one is the HOW, STATUS, and
DECISIONS made during implementation.

**Status as of last commit:** alphas a1 → a4 shipped; a5 (autonomous loop
daemon) is next. 1279 tests passing. No live mission has run end-to-end
yet — that's v0.5.0 GA.

---

## TL;DR (read this first if nothing else)

- We're building an **Autonomous Mission**: a single user-initiated
  feature request that runs unattended for hours, ships multiple
  commits, and stops when typed acceptance criteria all pass (or budget
  runs out).
- Validation is the product. Every acceptance criterion is tagged
  `[bash]` / `[chrome]` / `[vision]` / `[manual]` so it's measurable
  and binary. The model **cannot fake convergence** — `[bash]` and
  `[vision]` checks are run deterministically by the runtime BEFORE
  the model session ever sees the roadmap.
- We are NOT replacing Phase 1 (one-shot Mission). Phase 2 wraps Phase
  1 in a loop: each iteration is a tiny Phase-1 mission against one
  roadmap item.
- Implementation is staged into 7 alphas (`a1`–`a7`) so each commit
  is independently testable. Right now the deterministic spine is
  done; the daemon, WS protocol, and frontend are still TODO.

---

## 1. Implementation status

| Alpha | Title | Lands in | Status | Tag |
|-------|-------|----------|--------|-----|
| **a1** | Roadmap data layer | `gui/roadmap.py` (~520 lines) + 55 tests | ✅ shipped | `v0.5.0a1` |
| **a2** | Acceptance-check dispatchers | `orchestration/acceptance_check.py` (~590 lines) + 59 tests | ✅ shipped | `v0.5.0a2` |
| **a3** | Rigorous-grill mode | `orchestration/grill_me.py` (rigorous additions ~220 lines) + 36 tests | ✅ shipped | `v0.5.0a3` |
| **a4** | REFLECT specialist + run_reflect_pass | `orchestration/specialists.py::REFLECT` + new `orchestration/reflect.py` (~250 lines) + 40 tests + 19-test roguelite integration | ✅ shipped | `v0.5.0a4` |
| **a5** | Autonomous loop daemon | new `gui/autonomous_loop.py` (~470 lines) + 22 tests | ✅ shipped | `v0.5.0a5` |
| **a6** | WS protocol + production hooks | new `gui/autonomous_factory.py` + `gui/autonomous_session.py` + WS handlers in `gui/app.py` (+150 lines) + 60 tests | ✅ shipped | `v0.5.0a6` |
| **a7** | Frontend (composer toggle, spec card, header badge) | `gui/static/app.js` + styling | ✅ shipped | `v0.5.0a7` |
| **a8** | GA-prep — 5 integration bugs found + fixed via smoke | misc + `scripts/smoke_autonomous_minimal.py` + 2 doc updates | ✅ shipped | `v0.5.0a8` |
| **a9** | Ollama 503 retry-with-backoff + vision default → qwen3-vl:8b | `backends.py` + 11 retry tests + `acceptance_check.py` | ✅ shipped | `v0.5.0a9` |
| **GA** | flash-vs-pro tight smoke + tier guidance update | `docs/v0.5.0-smoke-results-step2c.md` + RESONANT.md update | ✅ shipped | **`v0.5.0`** |

**Total tests:** 1361 passing, 2 skipped (was 1185 at start of v0.5.0;
added 176 across a1–a6).

**Lines added since `5584833` (design freeze):** ~6000 lines net across
17 files.

---

## 2. Architectural overview

### 2.1 The deterministic / agentic split

The single most important architectural property of v0.5.0:

```
              ┌─────────────────────────────────────────────────────┐
              │            Autonomous Mission iteration             │
              │                                                     │
              │   ┌──────────────────────┐                         │
              │   │  IMPLEMENT specialist│  (Phase 1, unchanged)    │
              │   │  ships one item via  │                         │
              │   │  plan-graph runner    │                         │
              │   └──────────┬───────────┘                         │
              │              │                                     │
              │              ▼                                     │
              │   ┌─────────────────────────────────────────────┐  │
              │   │  DETERMINISTIC HALF (run_reflect_pass)      │  │
              │   │                                              │  │
              │   │  For each acceptance criterion:              │  │
              │   │   • [bash]  → BashRunner.run + assertion    │  │
              │   │   • [vision]→ VisionRunner.ask  (Ollama)    │  │
              │   │   • [chrome]→ delegate_to_model (queue)      │  │
              │   │   • [manual]→ skip (handoff list)            │  │
              │   │                                              │  │
              │   │  Writes passed=true|false + evidence into    │  │
              │   │  Roadmap.acceptance_criteria BEFORE the       │  │
              │   │  model session starts.                       │  │
              │   └──────────┬──────────────────────────────────┘  │
              │              │                                     │
              │              ▼                                     │
              │   ┌─────────────────────────────────────────────┐  │
              │   │  AGENTIC HALF (REFLECT specialist session)   │  │
              │   │                                              │  │
              │   │  • Validates [chrome] criteria via real      │  │
              │   │    browser interaction                       │  │
              │   │  • Marks roadmap items with commit refs      │  │
              │   │  • Emits structured JSON verdict              │  │
              │   │  • CANNOT override [bash]/[vision] state —   │  │
              │   │    those are already truth in the roadmap   │  │
              │   └──────────┬──────────────────────────────────┘  │
              │              │                                     │
              │              ▼                                     │
              │   ┌─────────────────────────────────────────────┐  │
              │   │  DAEMON CROSS-CHECK                          │  │
              │   │                                              │  │
              │   │  Daemon validates the model's verdict        │  │
              │   │  against roadmap.is_converged():             │  │
              │   │   • Model says satisfied + roadmap agrees ✓  │  │
              │   │   • Model says satisfied + roadmap disagrees │  │
              │   │     → daemon overrides to continue            │  │
              │   │   • Validates each claimed commit_sha via    │  │
              │   │     `git rev-parse`                          │  │
              │   └─────────────────────────────────────────────┘  │
              └─────────────────────────────────────────────────────┘
```

This split is the mechanical enforcement of the "measure twice, cut
once" design principle. The model is in the loop; it just can't lie
about the parts of the loop that are mechanical.

### 2.2 Module map (v0.5.0 additions)

```
resonant_client/
├── gui/
│   └── roadmap.py                  ← a1: pure data layer (Roadmap,
│                                       AcceptanceCriterion, parser/writer,
│                                       file locking)
├── orchestration/
│   ├── acceptance_check.py         ← a2: dispatch() routes a criterion
│   │                                   to the right runner; CheckResult,
│   │                                   BashRunner/VisionRunner, CheckContext
│   ├── grill_me.py                 ← a3: rigorous-mode addendum +
│   │                                   typed-criteria parser +
│   │                                   time-budget parser
│   ├── specialists.py              ← a4: NodeSpecialization.REFLECT +
│   │                                   SpecialistProfile (prompt + tools)
│   ├── plan_graph.py               ← a4: REFLECT added to enum + ALL set
│   └── reflect.py                  ← a4: run_reflect_pass — the
│                                       deterministic half
└── tests/
    ├── test_roadmap.py             ← a1: 55 tests
    ├── test_acceptance_check.py    ← a2: 59 tests
    ├── test_grill_me_rigorous.py   ← a3: 36 tests
    ├── test_reflect.py             ← a4: 40 tests
    └── test_roguelite_integration.py ← a4: 19 tests, end-to-end
```

Things that DO NOT exist yet (planned for a5+):
- `gui/autonomous_loop.py` — the daemon
- New events in `gui/app.py` (`autonomous_mission_started`, etc.)
- Composer toggle / spec-card budget UI / header badge in `gui/static/app.js`

### 2.3 End-to-end flow (when complete)

```
1. User clicks 🎯 Mission, toggles ∞ Run autonomously, types feature.
2. Standard Phase-1 grill phase runs but now with autonomous=True →
   rigorous-grill addendum applied. Model asks 10–25 questions, demands
   ≥4 binary type-tagged criteria, asks for time budget.
3. Spec emits with `**Time budget:**` + typed `[bash]/[chrome]/[vision]`
   acceptance criteria. Frontend shows ∞ Build autonomously CTA with
   budget confirmation card.
4. User confirms → backend creates `<project>/.resonant/roadmap-<id>.md`
   from the spec, spawns AutonomousMissionDaemon.
5. Daemon iterates:
   a. Read roadmap → pick first unchecked item
   b. Dispatch as Phase-1 sub-mission via intent_service.start_intent
   c. On completion: REFLECT in item-mark mode (mark item with SHA)
   d. Every K=3 iterations OR roadmap empty: REFLECT in full mode
      → run_reflect_pass (deterministic) → REFLECT model session
      (chrome + verdict) → cross-check → continue/satisfied/blocked
   e. Check stopping rules; sleep ~5s; iter += 1
6. Daemon stops. Final reflection summary as handoff document.
```

a5 is what wires steps 4–6 together.

---

## 3. The convergence-ground-truth contract

This is the design property that makes Autonomous Mission worth shipping.
Without it, "the mission is complete" means "the model thinks it is" —
which is the model's mood, biased toward `satisfied` because that ends
its work.

### 3.1 What the contract guarantees

> **`verdict=satisfied` from REFLECT is only honored when, for every
> non-`[manual]` acceptance criterion, the runtime ran the criterion's
> tagged check and the result was `passed=true`.**

The runtime — not the model — is the source of truth for `passed`. The
model can't write `passed=true` without going through the runtime's
dispatchers. Even the agentic `[chrome]` criteria are validated by the
daemon's cross-check after the model session ends.

### 3.2 How it's mechanically enforced

1. **Spec emission requires typed criteria.** The rigorous-grill prompt
   tells the model it cannot emit `## Final spec` until at least 4
   binary type-tagged criteria exist (R3 in `_RIGOROUS_GRILL_ADDITIONS`).
   The roadmap parser strict-matches the type tag — untyped criteria
   are silently dropped, returning `[]`. The daemon refuses to start
   an autonomous mission with `roadmap.has_any_acceptance_criteria()
   == False`.

2. **`run_reflect_pass` runs deterministic checks first.** Before the
   REFLECT model session opens, every `[bash]` is dispatched through
   `BashRunner.run`, every `[vision]` through `VisionRunner.ask`. The
   results land in `Roadmap.acceptance_criteria[i].passed` and
   `.evidence` fields. The daemon then dispatches the model session
   with the roadmap as context — the model sees the truth, not a blank
   slate.

3. **The model's only job is `[chrome]` + verdict.** REFLECT's prompt
   explicitly says "trust the runtime" for `[bash]`/`[vision]` and
   "drive the browser yourself" for `[chrome]`. It cannot rewrite
   `passed` for non-chrome criteria — `update_criterion` is allowed
   via `file_edit` to the roadmap, but the daemon will overwrite any
   tampering on its next pass.

4. **Daemon cross-check on verdict.** When the model emits
   `verdict=satisfied`, the daemon (a5) will compare against
   `roadmap.is_converged()`. Mismatch → override to `continue`.
   This is the final guard.

5. **Commit SHA validation.** Each `completed[].commit_sha` from the
   model is run through `git rev-parse` before being written to the
   roadmap (a5 will own this). Fabricated SHAs get stripped, the
   iteration logs as `<empty>`.

### 3.3 What the model CAN still mess up

The contract protects against fabricated convergence. It does NOT
protect against:

- The model picking the wrong roadmap item to work on
- The implementer scaffolding into the wrong directory
- A `[chrome]` criterion the model marks `passed=true` based on
  hallucinated evidence (it can write `passed=true` and bogus
  evidence into the roadmap; the daemon's only defense is the
  prompt's "DO NOT FABRICATE" rule + cycle guards on repeated
  identical browser navigations)
- Plain bugs in the implementer that pass tests but break behavior

Future hardening: a `[chrome]` audit-log validation pass that confirms
the model actually called `browser_navigate` to the URL it claimed to
hit. Out of scope for v0.5.0.

---

## 4. Module reference

Quick navigation. For each module: where it lives, key types, key
functions, what's tested, gotchas.

### 4.1 `gui/roadmap.py` (v0.5.0a1)

**Purpose:** pure data layer for the on-disk roadmap markdown. No
threading, no model calls, no I/O beyond `load`/`save`.

**Key types:**

| Class | Role |
|---|---|
| `AcceptanceCriterion(type, text, passed, evidence)` | One typed criterion. `type ∈ CRITERION_TYPES = ("bash","chrome","vision","manual")`. `passed: Optional[bool]` (None = not yet validated). `is_blocking` (False for manual). `is_satisfied` (True iff non-blocking OR `passed is True`). |
| `RoadmapItem(id, tier, title, description, checked, commit_sha, note)` | One row in the tier list. `id` is `T<tier>.<suffix>` (immutable post-creation). |
| `IterationLogEntry` | One entry in `## Iteration log`. |
| `Roadmap` | The whole document in memory. Has `items`, `acceptance_criteria`, `iteration_log`, plus `feature`, `intent_id`, `started_iso`, `time_budget_label`, `status`, `goal_spec_block`, `reflection_summary`. |

**Key functions:**

| Function | Purpose |
|---|---|
| `load(path)` | Read + parse a roadmap.md. Empty/missing → empty Roadmap. |
| `save(rm, path)` | Render + write. Acquires file lock around write. |
| `default_path(project_path, intent_id)` | Returns `<project>/.resonant/roadmap-<id>.md`. |
| `file_lock(path, timeout_seconds=60)` | Context manager. 60s stale-lock theft. |
| `mark_item_complete(rm, item_id, commit_sha, note="")` | Flip checkbox `[ ]`→`[x]` + record SHA. |
| `update_criterion(rm, text_match, passed, evidence)` | Mutate a criterion's pass/fail by exact-text match. |
| `add_item(rm, tier, title, description="", source_iter=None)` | Append with auto-assigned tier ID. |
| `append_iteration_log(rm, iter_num, duration_label, note, **kw)` | One-line entry. |
| `Roadmap.next_unchecked_item()` | First unchecked, by tier+suffix order. |
| `Roadmap.acceptance_summary()` | `(passed_count, total_blocking_count)` for the chat-header indicator. |
| `Roadmap.is_converged()` | True iff every non-manual criterion has `passed=True`. |
| `Roadmap.has_any_acceptance_criteria()` | Daemon's "is this misconfigured?" check. |

**Parser anchors (do not loosen casually):**

```python
_TIER_HEADER_RE   = r"^### Tier (\d+)(?:\s*(?:—|-)\s*(.+))?\s*$"
_ITEM_LINE_RE     = r"^-[ \t]*\[([ x])\][ \t]*\*\*(T\d+\.\d+)[ \t]*(?:—|-)[ \t]*(.+?)\.\*\*([^\n]*)$"
_CRITERION_LINE_RE = r"^-\s*\[([ x])\]\s*`\[(bash|chrome|vision|manual)\]`\s*(.+?)\s*$"
```

**Gotchas:**
- Em-dash `—` and hyphen `-` are alternation `(?:—|-)`, never a range
  `[—-]` (the latter is a Unicode range U+2014..U+002D and silently
  matches every char in between).
- Trailing capture in `_ITEM_LINE_RE` uses `[^\n]*` not `\s*(.*?)\s*$`
  to prevent greedy `\s*` from chomping past line boundaries.
- `add_item` preserves tier-ID monotonicity even after items are
  removed — never re-uses an ID.
- The on-disk markdown is the source of truth. The in-memory `Roadmap`
  is regenerated from disk every read; un-tracked sections (`## Notes`)
  get stripped on the next REFLECT pass.

**Tests:** `tests/test_roadmap.py` (55 tests).

### 4.2 `orchestration/acceptance_check.py` (v0.5.0a2)

**Purpose:** dispatch a single typed criterion to its right
deterministic check. Stateless, hermetic — runners are stubs in tests.

**Key types:**

| Class | Role |
|---|---|
| `CheckResult(passed, evidence, error, skipped)` | Constructors: `CheckResult.skip_manual()`, `CheckResult.delegate_to_model(reason)`, `CheckResult.errored(reason)`. Mutually exclusive states. |
| `BashRunner(timeout_seconds, cwd, _run)` | Wraps subprocess. `_run` hook for tests: `lambda cmd, **kw: (rc, stdout, stderr)`. |
| `BashAssertion(command, mode, expected_value)` | Parsed structure of a `[bash]` criterion. 5 modes: `exit_zero` / `exit_nonzero` / `output_eq` / `output_lt` / `output_gt`. |
| `VisionRunner(ollama_url, model, _call, _list_models)` | Wraps Ollama vision API. `_call` and `_list_models` are test hooks. Default `model="qwen2.5vl:7b"`. |
| `CheckContext(project_path, bash_runner, vision_runner, image_provider)` | Everything `dispatch` needs that's NOT in the criterion itself. |

**Key functions:**

| Function | Purpose |
|---|---|
| `parse_bash_assertion(text)` | Extract `BashAssertion` from criterion prose. Picks LONGEST backtick block. |
| `run_bash_check(criterion, runner, cwd)` | Execute one `[bash]` criterion. |
| `run_vision_check(criterion, image_bytes, runner)` | Ask the vision model + parse yes/no. |
| `dispatch(criterion, context)` | Top-level router. `[bash]→run; [chrome]→delegate_to_model; [vision]→run if image_provider; [manual]→skip`. |
| `summarize_for_roadmap(result)` | "PASS:/FAIL:/ERROR:/SKIP: …" prefix for the roadmap. |

**Gotchas:**

- **Longest backtick block, not first.** Real criteria often have
  multiple backtick blocks (`No `any` types: `! grep -rn ': any' src/``).
  Length is the heuristic — commands have shell tokens; type identifiers
  are one word.
- **`!` is a parser hint, not a shell operator.** When a `[bash]`
  criterion's command starts with `! ` (literal prefix), the parser
  strips the `!` and switches assertion `mode` to `exit_nonzero`.
  `BashRunner` runs the bare command (`grep ...`), not `! grep ...` in
  a shell. Caught this during the roguelite integration test — easy to
  get the mental model wrong.
- **`VisionRunner.is_available()` probes Ollama by default.** Tests
  must stub `_list_models` AND `_call`, otherwise the live `/api/tags`
  call fails and the check errors with "model not available."
- **Vision yes/no parsing is strict.** First non-whitespace token must
  start with `YES` (case-insensitive). Anything else → False
  (defensive).
- **`exit_nonzero` evidence reads "exit=N (non-zero expected)".** Don't
  confuse this with errors.

**Tests:** `tests/test_acceptance_check.py` (59 tests).

### 4.3 `orchestration/grill_me.py` rigorous mode (v0.5.0a3)

**Purpose:** when `autonomous=True`, the grill prompt's behavior shifts
to demand binary, type-tagged acceptance criteria + a time budget. The
parser then extracts those typed criteria into structured form for the
daemon.

**Key additions to `format_grill_first_message`:**

```python
def format_grill_first_message(
    feature_description: str,
    project_path: Optional[str] = None,
    *,
    autonomous: bool = False,
    vision_available: bool = True,
) -> str:
```

When `autonomous=True`, `_RIGOROUS_GRILL_ADDITIONS` is appended to the
base prompt. When `vision_available=False` (and autonomous), an extra
`_VISION_UNAVAILABLE_NOTE` is appended so the model doesn't emit
`[vision]` criteria the runtime can't validate.

**`_RIGOROUS_GRILL_ADDITIONS` enforces:**

- **R1:** 10–25 questions (vs the standard 5–15)
- **R2:** Each criterion binary AND type-tagged
  (`[bash]`/`[chrome]`/`[vision]`/`[manual]`)
- **R3:** Minimum 4 binary criteria
- **R4:** Time-budget question near the end of the decision tree
  with presets `1h | 4h | 6h | 8h | 12h | 24h | 48h | full auto`
- **R5:** "Don't pad the spec" — surface stuckness rather than
  inventing a 4th criterion

**Spec format additions:**

```markdown
**Out of scope:**
- ...

**Time budget:** 4h          ← new subsection

**Technical constraints:**
- ...

**Acceptance criteria:**
- `[bash]` `npm install` exits 0      ← typed format
- `[chrome]` Counter button increments
- `[vision]` Single green circle
```

**New parsers:**

```python
extract_acceptance_criteria(spec_block) → list[AcceptanceCriterion]
extract_time_budget(spec_block) → str   # "" when absent
```

`extract_spec` now populates `time_budget` and `acceptance_criteria`
fields on `ExtractedSpec` (defaulted, so legacy freeform specs still
work).

**Gotchas:**
- Strict regex: untyped criteria, indented sub-bullets, star markers,
  and unknown tags all silently drop. Better to fail loud than
  fuzzy-match.
- The parser scopes to the `**Acceptance criteria:**` section only;
  criterion-shaped lines elsewhere (e.g. `**Open risks:**` notes) are
  not picked up.

**Tests:** `tests/test_grill_me_rigorous.py` (36 tests).

### 4.4 `orchestration/specialists.py::REFLECT` + `orchestration/reflect.py` (v0.5.0a4)

**Purpose:** the convergence pass. Validates typed acceptance criteria,
marks roadmap items done with commit refs, emits a structured verdict.

**Two parts:**

#### 4.4.1 The deterministic part (`reflect.py`)

```python
def run_reflect_pass(
    roadmap: Roadmap,
    context: Optional[CheckContext] = None,
) -> ReflectPassResult
```

Iterates `roadmap.acceptance_criteria`, dispatches each via
`acceptance_check.dispatch`, writes definitive pass/fail back via
`update_criterion`. Idempotent: `passed=True` criteria are not re-run.

`ReflectPassResult`:
- `bash_results: list[(criterion, CheckResult)]`
- `vision_results: list[(criterion, CheckResult)]`
- `chrome_pending: list[AcceptanceCriterion]` — model session's job
- `manual_pending: list[AcceptanceCriterion]` — handoff list
- `converged: bool` — `roadmap.is_converged()` after the pass
- Tally helpers: `bash_passed/failed/errored`, same for `vision`
- `needs_model_session() -> bool` — daemon's escape hatch (False
  means daemon can mechanically converge without calling the model)

#### 4.4.2 The agentic part (`specialists.py::REFLECT`)

`SpecialistProfile`:
- `name="reflect"`
- `max_steps=20` (generous — multiple `[chrome]` checks)
- `confidence_threshold=0.7`
- 27 tools total: `READ_ONLY_TOOLS` (20) + `_AWAIT_USER` (1) +
  `{file_edit, bash, browser_navigate, browser_click, browser_type, browser_select}` (6)

**Prompt invariants (4601 chars total):**
- Two modes: `mode: item-mark` (bookkeeping only) vs `mode: full`
  (the convergence pass)
- "Trust the runtime" — `[bash]`/`[vision]` already validated, don't
  re-run
- Model's job: `[chrome]` validation via real browser interaction +
  structured JSON verdict
- "DO NOT FABRICATE" — commit SHAs come from `git log` only;
  evidence strings only quote what tools actually returned
- Verdict gating: `satisfied` requires every non-manual criterion to
  pass; even one `passed=false` blocks it

**JSON envelope (required):**

```json
{
  "completed": [{"id": "T1.2", "commit_sha": "abc123f", "note": "..."}],
  "chrome_results": [
    {"criterion": "<exact text>", "passed": true, "evidence": "..."}
  ],
  "added": [{"tier": 2, "title": "...", "description": "..."}],
  "blocked": [{"id": "T1.4", "reason": "..."}],
  "manual_pending": ["<criterion text>", ...],
  "verdict": "continue" | "satisfied" | "blocked",
  "summary": "<one paragraph>",
  "estimated_remaining_minutes": 0
}
```

**Daemon's responsibilities (a5):**
- Build the goal string with `mode: item-mark` or `mode: full`
- Run `run_reflect_pass` BEFORE dispatching the model session
- Pass `chrome_pending` + `manual_pending` into the session as context
- Parse the JSON verdict
- Cross-check `verdict=satisfied` against `roadmap.is_converged()`
- Validate each `commit_sha` via `git rev-parse`

**Tests:** `tests/test_reflect.py` (40), `tests/test_roguelite_integration.py` (19).

### 4.5 `gui/autonomous_loop.py::AutonomousMissionDaemon` (v0.5.0a5)

**Purpose:** the outer iteration loop. One daemon instance per
in-flight autonomous mission (per intent_id). Background thread
picks roadmap items, dispatches each as a Phase-1 sub-mission, runs
REFLECT every K iterations, stops on the first triggered rule.

**Key types:**

| Class | Role |
|---|---|
| `AutonomousMissionConfig` | intent_id, roadmap_path, time_budget_seconds (None = full-auto), max_iterations (100 default), full_reflect_cadence (3), tick_pause_seconds (5.0), blocked_streak_limit (3), check_failed_streak_limit (2). |
| `DaemonHooks` | All I/O the daemon needs, injected for testability: `dispatch_item` / `wait_for_dispatch` / `cancel_dispatch` / `get_commit_sha` / `validate_sha` / `run_full_reflect` / `check_context_factory`. |
| `DispatchOutcome` | Result of one Phase-1 sub-mission. `success: bool`, `error: str`, `handle: Any`. |
| `FullReflectOutcome` | Result of one full REFLECT pass — both halves combined: `pass_result: ReflectPassResult` + verdict / chrome_results / added_items / blocked_items / manual_pending / summary / estimated_remaining_minutes / error. |
| `AutonomousMissionDaemon` | The class. Public: `start()` / `stop(reason, message)` / `is_running()` / `join(timeout)` / `state_snapshot()`. |

**Stopping rules (priority order):**
1. `user_stop` — `daemon.stop()` was called
2. `time_budget_exhausted` — wall-clock budget elapsed (skipped for full-auto)
3. `iteration_cap` — defensive backstop at 100 iters
4. `satisfied` — full-reflect verdict + cross-check agree
5. `blocked` — `blocked_streak_limit` consecutive blocked verdicts
6. `check_failed` — `check_failed_streak_limit` consecutive failed sub-missions
7. `stuck` — roadmap empty, verdict=continue, no items added (would infinite-loop)
8. `misconfigured` — roadmap has no acceptance criteria

**Cross-check enforcement:** when REFLECT model emits
`verdict=satisfied`, the daemon RE-LOADS the roadmap from disk and
checks `roadmap.is_converged()`. If it disagrees, the daemon
overrides to `continue` and notes "[Daemon override]" in the
summary. This is the convergence-ground-truth contract's runtime
guard.

**Tests:** `tests/test_autonomous_loop.py` (22 tests).

### 4.6 `gui/autonomous_factory.py` + `gui/autonomous_session.py` (v0.5.0a6)

**Purpose:** the production wiring that turns the dependency-injected
daemon into a working autonomous mission. Two modules:

#### `gui/autonomous_factory.py`

Builds production `DaemonHooks` from a live `IntentService`, git
subprocess calls, and a `LocalSpecialistRunner` for REFLECT.

| Function | Purpose |
|---|---|
| `DispatchTracker` | Subscribes to IntentService events; signals per-intent `threading.Event`s on terminal events. Bridges async events into `wait_for_dispatch`'s sync interface. |
| `make_git_get_commit_sha(project_path)` | Wraps `git log -1 --format=%H` into a callable. Returns None on any failure. |
| `make_git_validate_sha(project_path)` | Wraps `git rev-parse --verify <sha>^{commit}`. Returns bool. |
| `build_reflect_goal(roadmap, pass_result, roadmap_path)` | Pure function. Builds the goal string for the REFLECT specialist's full pass — includes mode, criteria status, chrome_pending list, manual_pending list, tally, cross-check reminder. |
| `parse_reflect_verdict(text)` | Pure function. Extracts the JSON envelope from REFLECT's response. Lenient: handles fenced ```json blocks, unfenced trailing JSON, stray `{` chars, embedded braces in strings. Returns `{"verdict": "continue", "_parse_error": "..."}` on failure (never raises). |
| `make_reflect_runner(...)` | Constructs the REFLECT model session callable. Internally builds a one-node PlanGraph with `specialization=REFLECT` and runs it via `LocalSpecialistRunner`. |
| `make_check_context_factory(project_path, settings, image_provider)` | Returns the per-pass CheckContext factory. Reads vision-model setting from settings, falls back to `qwen2.5vl:7b`. |
| `build_autonomous_mission_hooks(...)` | Top-level: assembles all of the above into a `DaemonHooks`. |

#### `gui/autonomous_session.py`

Mid-tier orchestration — the bridge between the WS handler and
the daemon.

| Function | Purpose |
|---|---|
| `parse_time_budget(label)` | Convert `"4h"` / `"full auto"` / `"30m"` to seconds (None = no time ceiling). |
| `build_roadmap_from_spec(feature, intent_id, spec_markdown, project_path, started_iso)` | Parse a rigorous-grill spec, build a Roadmap, persist to `<project>/.resonant/roadmap-<id>.md`. Raises ValueError on malformed input. |
| `start_autonomous_mission(state, intent_id, feature, spec_markdown, on_event, ...)` | Top-level: builds roadmap + hooks + daemon, wires events, starts daemon, registers on AppState. |
| `stop_autonomous_mission(state, intent_id, reason, message)` | Looks up the active daemon and signals it. |
| `get_autonomous_daemon(state, intent_id)` | Lookup. |
| `cleanup_finished_daemons(state)` | Drop exited daemons from the AppState registry. Called on each new dispatch + on project switch. |

#### WS protocol additions in `gui/app.py`

Two new commands:

| Command | Payload | Effect |
|---|---|---|
| `mission_dispatch_autonomous` | `{spec_markdown}` | Validates the spec, builds the roadmap, spawns the daemon, advances mission phase to `autonomous_running`, returns `mission_phase_changed` + `sessions_updated` events. |
| `autonomous_mission_stop` | `{intent_id}` (optional — falls back to current mission's intent_id) | Calls `daemon.stop("user_stop", ...)`. The daemon emits `autonomous_mission_paused` itself; we don't preempt the phase transition here to avoid races with the daemon's own emission. |

The daemon's events flow through the WS naturally via the `on_event`
callback the dispatch handler installs. The frontend (a7) renders
`autonomous_iteration_*` and `autonomous_reflection` events as
styled chat cards.

**Mission state phases** (in `gui/sessions.py`):
- `autonomous_running` — daemon iterating
- `autonomous_complete` — daemon ended with `verdict=satisfied`
- `autonomous_paused` — daemon ended for any other reason

**Tests:** `tests/test_autonomous_factory.py` (35), `tests/test_autonomous_session.py` (25).

---

## 5. Decisions log (mini-ADRs)

Architectural decisions made during implementation that aren't captured
in the design doc. Each is "what we chose, why, and what we'd revisit."

### ADR 1 — Per-project roadmap location: `<project>/.resonant/roadmap-<id>.md`

**Choice:** roadmap lives in the user's project repo, in a `.resonant/`
subdir gitignored by default.

**Why:**
- The roadmap is a project artifact (diff-able, archivable). Keeping
  it tied to the project means a clone of the repo carries the mission
  history forward.
- A user can `cat` the file mid-run, hand-edit it, see what shipped
  without opening the GUI.
- Multi-mission per project is naturally supported (each mission gets
  its own `roadmap-<intent_id>.md`).

**Trade:** the user's project root gets a `.resonant/` subdir. We
gitignore by default; a Settings option opt-ins to git tracking.

**Revisit when:** v0.6.0 adds cross-mission continuity. The per-mission
file model might become a single per-project `mission-history.md`.

### ADR 2 — Typed validation, `[manual]` discouraged but allowed

**Choice:** four criterion types, with `[manual]` deliberately
deprioritized.

**Why:**
- Without typed validation, "the feature works" criteria let
  convergence become the model's mood.
- Real visual / behavioral features need first-class validation —
  hence `[chrome]` (Playwright DOM/CSS) and `[vision]` (screenshot
  + vision model).
- `[manual]` exists as an escape valve for true edge cases (audio
  output, stuff that needs human judgment) but the rigorous-grill
  prompt explicitly discourages it. Manual items are excluded from
  convergence and listed separately in the handoff doc.

**Alternative rejected:** "REFLECT decides if it's done." That's
exactly the convergence-as-mood failure mode the design explicitly
rejects.

### ADR 3 — Deterministic-then-agentic split

**Choice:** `run_reflect_pass` runs `[bash]`/`[vision]` BEFORE the
REFLECT model session opens.

**Why:**
- The model is biased toward `satisfied` (ends its work). Running
  deterministic checks first locks the truth into the roadmap before
  the model can rationalize around it.
- It's also a cost optimization: `needs_model_session() == False`
  means the daemon can skip the LLM call entirely for trivially-
  converged missions.

**Alternative rejected:** "model runs everything via tool calls."
Cheaper at first glance, but the model can fake `passed=true` by
hallucinating tool results. Cycle guards help but don't fully close
the gap.

### ADR 4 — `!` is a parser hint, not a shell operator

**Choice:** in a `[bash]` criterion `! <command>`, the parser strips
`!` and runs `<command>` with assertion mode `exit_nonzero`.

**Why:**
- Decouples the criterion semantics from bash's specific `!` operator.
  Works the same on Windows / non-bash environments.
- The criterion text "no `any` types: `! grep -rn ': any' src/`" reads
  naturally to humans AND to the parser.

**Surfaced by:** the roguelite integration test. First implementation
of the test stub returned `(0, "", "")` for grep, expecting `! grep`
to be shelled out literally. Test failed → fix was to return rc=1
(no match) and let the parser invert. Better mental model.

### ADR 5 — Idempotency across reflect passes

**Choice:** `run_reflect_pass` skips criteria that already have
`passed=True`.

**Why:**
- A flaky bash check (network blip, timing) shouldn't ratchet down a
  previously-passing roadmap.
- Daemon runs full reflection every K=3 iterations; without
  idempotency, expensive checks (npm install, full build) would re-run
  on every full pass, multiplying cost.

**Trade:** if the underlying world genuinely changes (user reverts a
fix), the previously-passing criterion stays green until the user
hand-edits the roadmap to reset. Acceptable: the user is the authority
on world state.

### ADR 6 — `needs_model_session()` lets daemon skip the LLM call

**Choice:** when `chrome_pending` is empty AND `manual_pending` is
empty, the daemon doesn't need to run the REFLECT model session — it
can mechanically declare `satisfied` (if `converged`) or `continue`
otherwise.

**Why:**
- Pure-bash specs can fully converge without ANY LLM calls in REFLECT.
- A 4h autonomous mission running REFLECT every 3 iterations across
  ~30 iterations could save ~10 model dispatches if the spec is mostly
  bash. That's real money on cloud-hosted models.

**Trade:** the model never gets a chance to "add new items" or
"observe what shipped" for pure-bash specs. The user loses the
narrative summary in REFLECT messages. Mitigated: the per-iteration
chat events still summarize what shipped.

### ADR 7 — Rigorous-grill prompt is an addendum, not a replacement

**Choice:** `_RIGOROUS_GRILL_ADDITIONS` is APPENDED to
`_GRILL_ME_BASE_PROMPT` when `autonomous=True`, not replacing it.

**Why:**
- The base prompt's behavior (one question at a time, recommendation
  format, escape hatch) is the right baseline for both modes.
- Appending keeps standard mission flow unchanged — no risk of
  regression for non-autonomous missions.
- Diff-able: future prompt edits can compare base vs base+rigorous to
  see what's different.

**Trade:** the base prompt's "Begin now with your first question." line
appears mid-message followed by more rules. Slightly awkward but the
model handles it fine in practice (verified by extracting and re-
running the prompt against deepseek-flash).

### ADR 8 — `AcceptanceCriterion` is mutated in place

**Choice:** `update_criterion`, `chrome_pending`, etc. all hold the
same `AcceptanceCriterion` objects that live in
`Roadmap.acceptance_criteria`. Mutations propagate.

**Why:**
- The roadmap's `is_converged()` method reads `passed` from each
  criterion. If the chrome model session validates a criterion and
  writes `passed=True` to its local copy, the roadmap stays out of
  sync.
- Single-object identity is the simplest fix and matches the rest of
  the codebase (Roadmap items are also mutated in place).

**Trade:** be careful not to copy criteria when passing into helpers.
The `chrome_pending` list contains the SAME objects as
`roadmap.acceptance_criteria` — don't `[c for c in chrome_pending]`
and lose the reference.

### ADR 9 — Item-mark mode is daemon-only, not a model session

**Choice:** when an iteration's sub-mission ships, the daemon marks
the roadmap item complete itself (read SHA via `git log -1`,
validate via `git rev-parse`, write via `mark_item_complete`). It
does NOT invoke the REFLECT model session for item-mark — only for
full-reflect every K iterations.

**Why:**
- The daemon already knows EXACTLY which item it dispatched. There's
  nothing for a model to "synthesize."
- `git log -1 --format=%H` is the authoritative SHA source — having
  the model fetch it via `bash` is purely indirection.
- A model dispatch costs tokens + latency. Across a 30-iteration run
  with `full_reflect_cadence=3`, that's ~30 saved REFLECT calls
  (item-mark only) + 10 actual REFLECT calls (full mode). About 75%
  cost reduction vs. doing both modes through the model.

**Trade:** the design doc's §7 envisioned both modes through REFLECT.
Skipping item-mark means the model session never gets a chance to
"observe what shipped" between full passes. The chat events
(`autonomous_iteration_complete` per iter) still surface this to
the user, just not to the model.

**Revisit when:** real missions show the model needs the per-iteration
context to make better full-pass decisions. Current bet: the
roadmap state + iteration log give it enough. If we see REFLECT
making confused calls because it doesn't know what the implementer
just did, we can pipe iteration summaries into the next full pass's
context.

### ADR 10 — "Stuck" stopping rule for empty-roadmap-not-converged

**Choice:** when `roadmap.next_unchecked_item()` returns None AND
the full-reflect verdict is `continue` AND no new items were added,
the daemon stops with `reason="stuck"` instead of looping forever.

**Why:**
- Caught while writing v0.5.0a5 tests. Without this rule, an empty
  roadmap with un-converged criteria (e.g., a [chrome] criterion the
  model can't validate) sits in an infinite "next iter, still nothing
  to do" loop — the daemon picks no item, runs reflect, gets
  `continue`, sleeps, repeats.
- The right semantics for empty + not-converged + no-items-added is
  "we're stuck; user must intervene." Distinguished from `blocked`
  (which is the model's verdict) so the user knows the issue is
  scope, not implementation.

**Trade:** a generous user might want the daemon to keep trying even
in this state ("maybe the chrome criterion will pass on a re-test
after a config change"). They can simply re-trigger the mission.
Stuck is a stop, not a permanent block.

**Revisit when:** real missions show false-stuck cases where the
model SHOULD have added items but didn't. If REFLECT under-adds
items, we can either tune its prompt or relax this rule.

### ADR 11 — JSON-verdict parsing tolerates model drift

**Choice:** `parse_reflect_verdict` accepts both fenced (```json)
and unfenced JSON, picks the LAST balanced top-level `{...}` block,
ignores stray unmatched `{` characters in prose, and never raises —
on any failure it returns `{"verdict": "continue", "_parse_error":
"..."}` so the daemon can keep going.

**Why:**
- Real model output drifts: sometimes the fence is missing,
  sometimes the model writes a sketch JSON before the real one,
  sometimes there's a stray `{` from prose. Strict parsing would
  hand the daemon a `verdict=continue` from a parse failure even
  though the model produced a valid JSON — losing real signal.
- The lenient parser is unit-tested across 4 drift modes
  (`tests/test_autonomous_factory.py::TestParseReflectVerdict`)
  including the "stray `{` then real JSON" case which the original
  forward-scan implementation got wrong.

**Trade:** lenient parsing means we accept slightly malformed JSON
that strict parsing would reject. Mitigated: the daemon's cross-
check still validates `verdict=satisfied` against the roadmap
state, so a parse-success with a wrong verdict doesn't translate
into false convergence.

---

## 6. What's NOT built yet

### 6.1 a7 — Frontend

**Estimated:** ~250 lines across `gui/static/app.js` + styling.

**UI surfaces:**

1. **Composer** — new toggle: `∞ Run autonomously` below the textarea.
   When ON, the Start button text becomes "Start autonomous mission"
   and a subtitle reads "I'll ask for a time budget after we've nailed
   the spec."

2. **Spec card** (after grill phase) — when the grill ran in autonomous
   mode, the "Build this roadmap" CTA becomes "∞ Build autonomously"
   with a budget confirmation card pre-filled from the spec.

3. **Chat header (active autonomous run)** — badge showing live
   status:
   - With budget: `∞ Autonomous · 1h 23m left · iter 4 · $0.34 (~$0.85/h)`
   - Full auto: `∞ Autonomous · 1h 47m elapsed · iter 4 · $0.34 (~$0.85/h)`
   - Stop button next to the badge.

4. **In-chat messages** — iteration_started, iteration_complete,
   reflection rendered as styled cards. Mission_complete as a banner.

5. **Plan-graph view** — extended to show roadmap as top-level
   structure with tier sections + checkboxes.

### 6.2 GA — End-to-end smoke

**Target:** a real autonomous run against the bootstrap-roguelite
spec, exercising at least one criterion of EACH type:
- `[bash]`: build passes
- `[chrome]`: counter button click increments via DOM assertion
- `[vision]`: rendered counter is centered + readable

The bootstrap-roguelite spec from `docs/long-running-agents-phase-2.md`
§11.2 is the canonical test mission. We have it as a unit-level
integration test (`tests/test_roguelite_integration.py`); GA is when
the same spec runs through the live daemon.

**Multi-model GA: flash vs pro side-by-side.** Per user direction
(see `docs/v0.5.0-smoke-plan.md`), GA includes a comparison run on
both `deepseek-v4-flash:cloud` and `deepseek-v4-pro:cloud` so we
can characterize where pro's extra deliberation pays off (planner /
REFLECT) vs where it doesn't (cheap implementer iterations).
Detailed protocol + scoring rubric in the smoke plan doc.

---

## 7. For an LLM picking up this work after a context reset

### 7.1 Reading order

1. **`RESONANT.md`** — overall project conventions
2. **`docs/long-running-agents.md`** — Phase 1 design (the foundation
   Phase 2 wraps in a loop)
3. **`docs/long-running-agents-phase-2.md`** — Phase 2 design (the WHY
   and WHAT of v0.5.0)
4. **This doc** — Phase 2 implementation status (HOW, STATUS, DECISIONS)
5. **`docs/v0.4.x-deepseek-harness-roadmap.md`** — what came before
   v0.5.0 (Tier 1 + Tier 2 done; Tier 3 deferred to v0.5.x)

### 7.2 Run the tests first

```bash
# Full suite (1279 passing, 2 skipped)
python -m pytest

# Just the v0.5.0 modules
python -m pytest tests/test_roadmap.py tests/test_acceptance_check.py tests/test_grill_me_rigorous.py tests/test_reflect.py tests/test_roguelite_integration.py

# The end-to-end integration that exercises a1+a2+a3+a4 together
python -m pytest tests/test_roguelite_integration.py -v
```

If any test fails on a fresh checkout, **stop and figure out why
before adding new code.** A failing test on a clean tree means
something is wrong with your environment OR the previous commit
was bad — both worth knowing about.

### 7.3 Where to start coding (a5)

1. Read `tests/test_reflect.py::TestReflectPassResultHelpers` and
   `tests/test_roguelite_integration.py::TestRogueliteReflectHappyPath`
   to understand the contract `run_reflect_pass` provides to the
   daemon.

2. Sketch `gui/autonomous_loop.py::AutonomousMissionDaemon` with
   stub methods. Start with `_pick_next_item` (calls
   `roadmap.next_unchecked_item()`) and `_dispatch_item` (calls
   `intent_service.start_intent` with the item's title as the goal).

3. Mock the intent service + REFLECT model session for unit tests.
   The daemon should be testable WITHOUT real model calls.

4. Wire the threading + cancellation shape from the design doc §8.2.
   `threading.Event` for cancellation; the engine's existing
   `cancel_event` is the in-flight signal.

5. Stopping rules in priority order (see §8.4 of the design doc).
   `user_stop` first, then time budget, then convergence, then
   blocked, then consecutive-check-fail.

6. WS events emitted via `on_event` callback supplied by the daemon's
   constructor — keep the daemon decoupled from the WS layer for
   testability.

7. **DO NOT** wire the frontend yet (a7). a5 should be unit-testable
   with the daemon firing events into a mock callback.

### 7.4 Testing patterns to follow

The existing v0.5.0 tests demonstrate the patterns:

- **Stub runners, not real subprocesses.** `BashRunner(_run=lambda
  cmd, **kw: (rc, stdout, stderr))`. `VisionRunner(_call=lambda ...,
  _list_models=lambda: [...])`.
- **Build minimal Roadmap fixtures.** `_make_roadmap([("bash", text),
  ...])` from `tests/test_reflect.py` is a good pattern.
- **Pin invariants, not exact prose.** Prompt-content tests check for
  required substrings + JSON keys, not full strings — small wording
  edits shouldn't break the suite.
- **Group tests by behavior class.** `TestRunReflectPassBash`,
  `TestReflectPassResultHelpers`, etc. Makes failures easy to
  attribute.

### 7.5 What NOT to do

- **DO NOT** weaken any parser regex in `gui/roadmap.py` to "be more
  forgiving." Strict parsing is a feature — it forces the model to
  produce the agreed format. Loose parsing is how convergence-ground-
  truth gets quietly compromised.
- **DO NOT** add a path that lets the model write `passed=true` for
  `[bash]` or `[vision]` criteria. Those go through the runtime only.
- **DO NOT** introduce a "fast path" that skips `run_reflect_pass`.
  The deterministic part is what makes convergence real.
- **DO NOT** copy `AcceptanceCriterion` objects when passing them
  around. The mutate-in-place pattern (ADR 8) depends on object
  identity.
- **DO NOT** add new criterion types without updating
  `gui/roadmap.CRITERION_TYPES`, the parser regex
  `_CRITERION_LINE_RE`, the grill prompt's R2 enumeration, the
  `acceptance_check.dispatch` switch, and the REFLECT prompt's
  type-tag list. A criterion type is touched in 5+ places.

---

## 8. Glossary

**Autonomous Mission.** A user-initiated mission that runs unattended
for hours, ships multiple commits, and stops on typed-acceptance
convergence or budget exhaust. The unit of work is the *roadmap*,
not the individual chat session.

**Roadmap.** The on-disk markdown file at
`<project>/.resonant/roadmap-<intent_id>.md` that holds the mission's
goal spec, item checklist, iteration log, and reflection summary. The
source of truth — every server restart re-reads from disk.

**Roadmap item.** One row in the tier list, e.g. T1.3. Has a
checkbox, title, description, optional commit_sha, optional note. IDs
are immutable post-creation.

**Acceptance criterion.** A typed, binary, measurable check that gates
convergence. One of `[bash]` / `[chrome]` / `[vision]` / `[manual]`.
Lives in `Roadmap.acceptance_criteria`.

**Convergence.** When `roadmap.is_converged() == True` — every
non-`[manual]` acceptance criterion has `passed=True`. The autonomous
loop's stopping condition.

**REFLECT.** The new specialist (v0.5.0a4) that validates acceptance
criteria, marks roadmap items done, and emits the structured verdict.
Two trigger modes: `item-mark` (per-item bookkeeping) and `full`
(convergence pass).

**Daemon.** `AutonomousMissionDaemon` (a5, TBD). Runs a background
thread that iterates the roadmap, dispatching sub-missions and REFLECT
passes until a stopping rule fires.

**Sub-mission.** One iteration's Phase-1 mission. The daemon dispatches
each unchecked roadmap item as a sub-mission via
`intent_service.start_intent`. Phase 1's plan-graph runner handles
execution.

**Rigorous grill.** The grill phase when `autonomous=True`. 10–25
questions, demands ≥4 binary type-tagged criteria, asks for a time
budget. Implemented as `_RIGOROUS_GRILL_ADDITIONS` appended to the
base grill prompt.

**Time budget.** User-confirmed wall-clock ceiling for the autonomous
run. Presets: `1h | 4h | 6h | 8h | 12h | 24h | 48h | full auto`.
Captured in the spec as `**Time budget:**`.

**Iteration log.** Per-iteration entries in the roadmap markdown
recording iter#, timestamp, duration, item picked, commit ref, kind
(shipped / reflect / blocked).

**Image provider.** Callable supplied by the daemon to `CheckContext`
that produces screenshot bytes for `[vision]` checks. Wraps
`browser_screenshot` (web) or `computer_screenshot` (desktop).

**Cycle guard.** v0.3.3 / v0.4.9 mechanisms in `engine/session.py`
that hard-stop a specialist when it loops on identical tool signatures
or churns on read-only ops. Apply inside each Phase-1 sub-mission;
unchanged in v0.5.0.

**`needs_model_session()`.** Method on `ReflectPassResult` that
returns False when the daemon can mechanically converge without
calling the REFLECT model — pure-bash specs that all pass
deterministically. Cost-saving optimization.

**Cross-check.** The daemon's defensive verification of the model's
verdict. If REFLECT says `verdict=satisfied` but
`roadmap.is_converged() == False`, the daemon overrides to `continue`.
Prevents a confused or adversarial model from declaring victory
prematurely.

---

## Appendix A — File / line / test pointers

For an LLM that wants to navigate directly:

| Want to look at | Where |
|---|---|
| `Roadmap` dataclass | `resonant_client/gui/roadmap.py:146` |
| `AcceptanceCriterion` dataclass | `resonant_client/gui/roadmap.py:62` |
| `is_converged` | `resonant_client/gui/roadmap.py:195` |
| `update_criterion` | `resonant_client/gui/roadmap.py:534` |
| `add_item` | `resonant_client/gui/roadmap.py:555` |
| `CheckResult` constructors | `resonant_client/orchestration/acceptance_check.py:72` |
| `BashRunner`, `BashAssertion`, `parse_bash_assertion` | `acceptance_check.py:122–280` |
| `VisionRunner.is_available` (the gotcha) | `acceptance_check.py:348` |
| `dispatch` (top-level) | `acceptance_check.py:517` |
| `_RIGOROUS_GRILL_ADDITIONS` | `resonant_client/orchestration/grill_me.py:139` |
| `extract_acceptance_criteria` | `grill_me.py:354` |
| `extract_time_budget` | `grill_me.py:388` |
| REFLECT specialist profile | `resonant_client/orchestration/specialists.py:230` |
| `run_reflect_pass` | `resonant_client/orchestration/reflect.py:154` |
| `ReflectPassResult.needs_model_session` | `reflect.py:131` |
| Bootstrap roguelite spec | `tests/test_roguelite_integration.py:54` |

| Want to read | Test file |
|---|---|
| Roadmap parser/writer | `tests/test_roadmap.py` |
| Bash + vision dispatchers | `tests/test_acceptance_check.py` |
| Rigorous-grill prompt + parsers | `tests/test_grill_me_rigorous.py` |
| REFLECT specialist + run_reflect_pass | `tests/test_reflect.py` |
| End-to-end (a1+a2+a3+a4 together) | `tests/test_roguelite_integration.py` |

---

## Appendix B — Why this doc exists

After v0.4.x's overnight autonomous run codified a pattern that was
worth shipping as a product feature, we wrote
`docs/long-running-agents-phase-2.md` as the design doc. That doc is
599 lines of WHY/WHAT — it answers "why does Autonomous Mission
exist," "what does the user experience look like," "what's the
interaction model."

This doc answers the *next* set of questions, the ones that come up
when someone (human or LLM) sits down to write the next 350 lines of
code:

- "Where am I in the implementation?"
- "What modules already exist and what do they expose?"
- "What design decisions were made along the way that aren't in the
  design doc?"
- "What can I trust about the existing tests' coverage?"
- "What should I NOT touch?"

If you're picking this up after v0.5.0a5+ ships and this doc hasn't
been updated, look at the git log between then and `v0.5.0a4` to see
what's changed. The decisions log in §5 is a good place to add new
ADRs as you go.
