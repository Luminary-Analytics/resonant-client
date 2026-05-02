# Long-running agents — Phase 2: Autonomous Mission

**Status:** **alphas a1–a4 shipped (deterministic spine done); a5–a7 + GA pending.** Target: v0.5.0.
**Author / date:** drafted 2026-05-02 by the autonomous-loop overnight run, iterating with the user.
**Predecessor:** [`docs/long-running-agents.md`](./long-running-agents.md) (Phase 1, shipped in v0.3.x).
**Implementation status doc:** [`docs/long-running-agents-phase-2-implementation.md`](./long-running-agents-phase-2-implementation.md) — read THAT for the HOW / STATUS / DECISIONS once you've digested this design doc.

> **TL;DR for the impatient:** this doc is the WHY and WHAT. It captures the product/UX decisions, the architecture, and the open questions, frozen at design time. As of `v0.5.0a4`, the deterministic spine (roadmap data layer, acceptance dispatchers, rigorous-grill prompt + parsers, REFLECT specialist + `run_reflect_pass`) is built and tested end-to-end. The autonomous loop daemon (a5), WS protocol additions (a6), and frontend (a7) are still TBD. See the implementation guide linked above for HOW the design materializes in code, decisions made along the way, and what to read first if you're picking this up cold.

## Design principle: measure twice, cut once

The whole point of a multi-hour autonomous mission is that **what gets delivered EXACTLY matches what the user wanted.** That's not solved by trusting the model's verdict at the end — the model is biased toward "satisfied" because that ends its work. It's solved by:

1. **Measuring twice up front** — the rigorous grill produces typed, executable acceptance criteria with the user's explicit sign-off. Every criterion is something a person could verify, expressed as a check the agent will run.
2. **Cutting once** — the agent ships the work over hours of autonomous iteration.
3. **Measuring again before declaring done** — REFLECT runs every typed acceptance check the same way a person would (bash command, browser interaction, screenshot inspection) and only emits `verdict=satisfied` when every non-`[manual]` criterion produces real evidence of passing.

Validation is therefore a **first-class citizen** of Resonant Client, not an afterthought. The grill phase, the roadmap format, and the REFLECT specialist all serve the same goal: making "the mission is complete" mean *measured to be complete*, not *the model thinks so*.

---

## 1. Why this exists

Phase 1 of long-running-agents shipped in v0.3.x:
- The Mission toggle creates a fresh chat session for a single feature
- The grill-me interviewer produces a `## Final spec` block
- Clicking "Build this roadmap" dispatches the spec to `intent_service.start_intent`, which spawns a `LocalSpecialistRunner` over a `PlanGraph`

That gets you **one mission session** end-to-end. After the spec dispatches, the plan-graph specialists run sequentially until they're done; you read the result; if there's more work, you kick off another mission. Each mission is a one-shot.

Phase 2 changes the unit of work from "one mission session" to **"a sustained loop that runs for hours, marks items complete as it ships them, reflects on what's still missing, and adds new items until either the goal is met or the time budget runs out."**

The pattern was demonstrated empirically in the v0.4.x overnight autonomous run (2026-05-02): 11 releases shipped across ~6 hours, each one picking the next unchecked roadmap item, executing it, marking it done, and self-scheduling the next iteration. That run happened OUTSIDE the codebase (via `ScheduleWakeup` + a Claude Code agent). Phase 2 brings the same pattern INSIDE Resonant Client as a first-class product feature: any user with Ollama can run an autonomous mission against deepseek without external scaffolding.

## 2. Naming

User confirmed **"Autonomous Mission"** with the **∞** glyph (suggests "keeps going on its own without supervision"; less twee than a moon). Surface treatment:
- New toggle in the composer next to "Start mission": **"∞ Run autonomously"**.
- The actual time budget is asked DURING the grill, not at composer-time (see §11 below). The composer toggle just opts the mission INTO the autonomous flow.
- The chat header on an active autonomous mission shows a small badge: `∞ Autonomous · 1h 23m left · iter 4`. For full-auto runs (no time budget) the elapsed-time replaces the countdown: `∞ Autonomous · 1h 47m elapsed · iter 4`.
- The "Build this roadmap" CTA on the spec card becomes "∞ Build autonomously" when the toggle is on, with the budget pre-filled from the grill spec but user-overridable.

## 3. Goals and non-goals

**Goals.**
- A single user-initiated mission can run for hours unattended and ship multiple commits.
- The user can leave the laptop / window open and come back to a clean handoff document describing what shipped, what's still pending, and any blockers.
- Every cycle's work is observable mid-run via the existing chat + plan-graph view.
- The roadmap survives session restarts (saved on disk as a real markdown file in the user's project).
- The agent can self-extend the roadmap when it discovers new work during execution.
- A clear stopping rule prevents runaway resource use.

**Non-goals (this release).**
- Cross-mission roadmap continuity. Each autonomous mission has its own roadmap; sustained multi-day work that spans missions is v0.6.0+.
- Cost ceilings or budget alerts beyond the existing v0.3.x cost-tracking. Time budget is the v0.5.0 lever.
- Resuming a paused autonomous mission across machines. The loop runs against a local uvicorn; if the machine sleeps or crashes, the user resumes by re-launching and re-firing.
- Multiple parallel autonomous missions in the same project. v0.5.0 ships single-mission-at-a-time.
- An "agent supervisor" that watches and intervenes mid-loop. The user is the supervisor; they can stop or interrupt at any time.

## 4. The user's mental model

What the user does:
1. Opens a project, clicks 🎯 Mission.
2. Toggles **∞ Run autonomously** in the composer. Types the feature description (no time budget here yet — that's a grill question).
3. Hits Start. The grill phase runs as today (one focused question at a time) PLUS one new question near the end about time budget (see §11).
4. When the spec lands with a recommended budget baked in, clicks **∞ Build autonomously**. A confirmation card lets the user override the budget before starting the loop. Defaults to whatever the model recommended.
5. Watches mid-run progress in the chat (each iteration logs a summary message: "Iteration 3 complete — shipped T1.5, marked T2.1 in progress"). Walks away.
6. Comes back. Reads the handoff document the agent emits at the end. If the goal is met, opens the resulting commits + diff in their tool of choice. If not, decides whether to extend the budget or hand off the remaining work to themselves.

What the user sees during the run (visible in the chat panel):
- Each iteration: "∞ Iteration N · picked item: ..." → tool calls (collapsed in groups, like today) → "✓ Iteration N complete — shipped to commit `<sha>`. Marked `<item-id>` done."
- After every K iterations or when the roadmap empties: a "∞ Reflection" message — short summary of what's done, what's pending, what new items the agent added.
- At the end (time budget exhausted, convergence, or user stop): a "∞ Mission complete" or "∞ Mission paused" message with a link to the handoff doc.

## 5. Architecture

```
                               ┌──────────────────────────────────────┐
                               │  Frontend (app.js)                   │
                               │  • Composer "Run autonomously" toggle │
                               │  • Chat-header autonomous badge       │
                               │  • Stop button                        │
                               │  • Iteration / reflection messages    │
                               └────────────────┬─────────────────────┘
                                                │ WS
                                                ▼
┌───────────────────────────────────────────────────────────────────────┐
│  Backend (gui/app.py + new gui/autonomous_loop.py)                    │
│                                                                       │
│  ┌─────────────────────┐    ┌──────────────────────────────────────┐  │
│  │  Mission state      │───▶│  AutonomousLoopDaemon                │  │
│  │  (mission_state +   │    │  (background thread per active       │  │
│  │   roadmap_path)     │    │   mission; tick every ~60s)          │  │
│  └─────────────────────┘    └─────────────────┬────────────────────┘  │
│                                               │                       │
│                                               ▼                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  For each iteration:                                            │  │
│  │   1. Read roadmap.md → pick first unchecked item                │  │
│  │   2. Dispatch as a sub-mission via intent_service.start_intent  │  │
│  │      (the existing plan-graph runner handles execution)         │  │
│  │   3. On node.complete: REFLECT specialist marks the item done   │  │
│  │      in roadmap.md, appends commit ref                          │  │
│  │   4. Every K iters or on empty: full REFLECT pass —             │  │
│  │      append new items, emit a summary, decide continue/stop     │  │
│  │   5. Check stopping rules (time / convergence / user-stop)      │  │
│  │   6. If continuing, sleep a bit, then iter += 1                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Persistence: <project>/.resonant/roadmap-<intent_id>.md              │
│  Audit:       ~/.resonant/projects/<hash>/intents/<id>/audit.jsonl    │
└───────────────────────────────────────────────────────────────────────┘
```

The big architectural insight: **Phase 2 doesn't replace Phase 1; it wraps it in a loop.** Each iteration is a tiny Phase-1 mission against one roadmap item. The plan-graph runner, cycle guards, working_subdir propagation, await_user, diagnostics — every v0.3.x and v0.4.x mechanism still applies inside each iteration. We're adding the *outer loop*, not rewriting the inner machinery.

## 6. The roadmap artifact

### 6.1 Location and lifetime

- Lives at `<project>/.resonant/roadmap-<intent_id>.md` in the user's project root.
- The `.resonant/` subdir is created on first autonomous mission and added to `.gitignore` UNLESS the user opts in to tracking it (Settings → Autonomous → "Commit roadmaps to git" defaults OFF).
- Persists across server restarts. Resuming a mission re-reads the file.
- Multiple missions per project share the `.resonant/` dir; each gets its own `roadmap-<intent_id>.md`.

**Why per-project, not per-AppState:** the user picked this in the design conversation. The roadmap is a project artifact (diff-able, archivable, copyable across machines via git when the user opts in). Keeping it tied to the project rather than to the resonant data dir means a clone of the repo carries the mission history forward.

### 6.2 Format

```markdown
# Autonomous Mission: <feature description from grill spec>

**Intent ID:** <uuid>
**Started:** 2026-05-02T08:14:03Z
**Time budget:** 4h
**Status:** running | paused | complete | failed

## Goal (from grill spec)

<paste the `## Final spec` block from the grill phase verbatim>

## Roadmap

### Tier 1 — initial decomposition

- [x] **T1.1 — <item title>.** <one-paragraph description> *(shipped at <commit-sha>: <one-line commit summary>)*
- [x] **T1.2 — <item title>.** <description> *(shipped at <commit-sha>)*
- [ ] **T1.3 — <item title>.** <description>
- [ ] **T1.4 — <item title>.** <description>

### Tier 2 — discovered during iteration

- [ ] **T2.1 — <item title>.** <description> *(added in iteration 4)*

## Iteration log

- **Iter 1** (2026-05-02T08:18:11Z, 14m) — picked T1.1, shipped at `abc123f`. Notes: <one-line>.
- **Iter 2** (2026-05-02T08:33:07Z, 21m) — picked T1.2, shipped at `def456a`. Notes: <one-line>.
- **Iter 3** (2026-05-02T08:54:20Z, 8m) — REFLECT — added T2.1 (discovered T1.4 needs schema migration first).

## Completed

(populated by REFLECT — duplicate of checked items above for fast scanning)

## Blocked / needs human decision

(populated by REFLECT when items hit ambiguity the agent can't resolve via await_user)

## Reflection summary (latest)

> Last updated by REFLECT on 2026-05-02T09:14:15Z (iter 5).
>
> 3 items shipped, 2 remaining (T1.3, T1.4 + the new T2.1).
> Verdict: continuing. Estimated 1.5h to T1.3 completion based on
> current pace.
```

The format is markdown by convention, but the parser is strict about a few things (regex defined in `roadmap.py`):
- `^- \[([ x])\] \*\*(T\d+\.\d+)` — checkbox + tier ID per item
- `^### Tier \d+` — tier section headers
- `^## Roadmap`, `^## Iteration log`, `^## Reflection summary` — pinned section headers

### 6.3 Constraints

- **Tier IDs are immutable.** Once T1.3 is created, it stays T1.3 even if other items reorder. Re-using an ID is forbidden (the parser warns).
- **Items can move between tiers** by REFLECT, but the ID stays.
- **Items only enter via REFLECT or the initial spec parse.** The plan-graph runner can NOT directly mutate the roadmap — its scope is execution, not planning.
- **The user can hand-edit `roadmap.md` mid-run.** The loop daemon re-reads the file before each iteration. Conflicts (user edited the same item the daemon was about to mark done) resolve last-write-wins with a warning event in chat.

## 7. The REFLECT specialist

A new node specialization in `orchestration/specialists.py`:

```python
NodeSpecialization.REFLECT: SpecialistProfile(
    name="reflect",
    description="Review autonomous-mission progress. Validate acceptance criteria via bash / chrome / vision. Mark items done. Add new items if needed. Emit structured verdict.",
    system_block=(
        "You are the REFLECT specialist for an autonomous mission. Your job is "
        "to keep the roadmap honest: validate each typed acceptance criterion, "
        "mark items as complete with their commit refs, identify items that "
        "should be added based on what shipped, and emit a structured verdict.\n\n"
        "For each acceptance criterion in the spec, run the check tagged in "
        "the criterion:\n"
        "- `[bash] <command>` → run via `bash` tool; pass = exit 0 (or as "
        "  specified in the criterion text)\n"
        "- `[chrome] <interaction + assertion>` → use `browser_navigate`, "
        "  `browser_click`, `browser_type`, `browser_js`, `browser_screenshot` "
        "  to drive the UI and check the assertion\n"
        "- `[vision] <visual claim>` → take a screenshot via "
        "  `browser_screenshot` (web) or `computer_screenshot` (desktop), "
        "  pass it to a vision-capable model, ask the question literally\n"
        "- `[manual] <description>` → SKIP. List in handoff. Doesn't gate "
        "  convergence.\n\n"
        "Run each check ONCE, capture the result, mark the checkbox. DO NOT "
        "loop on a failing check — it stays `[ ]` and the criterion is "
        "reported as still-pending in the verdict.\n\n"
        "End your response with a fenced JSON code block:\n\n"
        "```json\n"
        "{\n"
        '  "completed": [{"id": "T1.2", "commit_sha": "abc123f", "note": "..."}, ...],\n'
        '  "acceptance_results": [\n'
        '    {"criterion": "npm install exits 0", "type": "bash",\n'
        '     "passed": true, "evidence": "exit_code=0, stdout truncated"},\n'
        '    {"criterion": "Click theme toggle, body bg becomes dark",\n'
        '     "type": "chrome", "passed": true,\n'
        '     "evidence": "getComputedStyle returned rgb(26,26,46)"},\n'
        '    {"criterion": "Single green circle on dark navy", "type": "vision",\n'
        '     "passed": true,\n'
        '     "evidence": "vision model answered: yes, one circle visible"}\n'
        '  ],\n'
        '  "added": [{"id": "T2.1", "tier": 2, "title": "...", "description": "..."}],\n'
        '  "blocked": [{"id": "T1.4", "reason": "needs schema decision"}],\n'
        '  "verdict": "continue" | "satisfied" | "blocked",\n'
        '  "summary": "<one-paragraph user-facing summary>",\n'
        '  "estimated_remaining_minutes": <int>\n'
        "}\n"
        "```\n\n"
        "Verdict rules:\n"
        "- `satisfied` requires EVERY non-`[manual]` acceptance criterion has\n"
        "  `passed: true`. Even one `passed: false` blocks `satisfied`.\n"
        "- `blocked` if you tried `await_user` once already this run AND the\n"
        "  user response didn't unblock progress.\n"
        "- `continue` otherwise — the loop will pick the next item or call\n"
        "  you again after more iterations.\n"
    ),
    tool_allowlist=(
        READ_ONLY_TOOLS
        | _AWAIT_USER
        | frozenset({
            "file_edit",            # write roadmap.md
            "bash",                 # [bash] checks
            "browser_navigate",     # [chrome] checks
            "browser_click",
            "browser_type",
            "browser_screenshot",   # for [vision] checks via web
            "browser_js",
            "computer_screenshot",  # for [vision] checks via desktop
        })
    ),
    max_steps=20,  # generous — multiple acceptance checks may need real interaction
    confidence_threshold=0.7,
),
```

REFLECT's allowlist is intentionally broader than VERIFY's. VERIFY is read-only (its job is "check the implementer's claims"); REFLECT does the same plus owns the roadmap (writes via `file_edit`) and drives real UI interactions for `[chrome]` / `[vision]` checks. The only specialist with write access to `roadmap.md` is REFLECT — everyone else treats it as read-only context.

REFLECT runs in two trigger modes:
- **Item-mark mode** (after every successful item): minimal pass, mark item done with commit ref.
- **Full-reflection mode** (every K=3 items, or when roadmap is empty, or before stopping): full review pass that may add items / move items to blocked / change the verdict.

## 8. The autonomous loop daemon

New module: `resonant_client/gui/autonomous_loop.py`.

### 8.1 Lifecycle

```python
class AutonomousMissionDaemon:
    def __init__(self, state, mission_id, intent_id, time_budget_seconds, on_event):
        ...

    def start(self) -> None:
        """Spawn the background thread. Idempotent."""

    def stop(self, reason: str = "user_stop") -> None:
        """Signal the daemon to wind down at the next safe point.
        Emits a final mission_paused event. Does NOT kill in-flight
        tool calls — those finish naturally."""

    def _run(self) -> None:
        """Thread entrypoint. Iterates until a stopping rule fires."""
        while not self._should_stop():
            item = self._pick_next_item()
            if item is None:
                self._reflect_full()
                if self._verdict == "satisfied":
                    self._emit("mission_complete", ...)
                    return
                if self._verdict == "blocked":
                    self._emit("mission_blocked", ...)
                    return
                # Continuing means REFLECT added new items; loop again.
                continue

            self._dispatch_item(item)            # Phase-1 sub-mission
            self._reflect_item(item)             # mark done
            if self._iter_count % 3 == 0:
                self._reflect_full()             # add new items, update summary

            self._iter_count += 1
            time.sleep(self._tick_pause_seconds)  # default 5s — small breathing room
```

### 8.2 Threading + cancellation

- Each AppState carries at most one active `AutonomousMissionDaemon` (multiple parallel autonomous missions are non-goal).
- The daemon thread is a daemon thread (Python `threading.Thread(daemon=True)`), so a server crash kills it cleanly.
- Cancellation is via `threading.Event`. `stop()` sets the event; the daemon checks it at the top of each iteration AND between phases of the same iteration (after dispatch, before reflect, etc.).
- The in-flight Phase-1 mission's existing `cancel_event` is the same signal — `stop()` sets both.

### 8.3 WS events emitted

```
autonomous_mission_started         # daemon began
autonomous_iteration_started       # picked an item, about to dispatch
autonomous_iteration_complete      # sub-mission shipped + REFLECT marked done
autonomous_reflection              # full REFLECT pass result
autonomous_mission_complete        # convergence ("satisfied") or budget exhausted
autonomous_mission_paused          # user_stop or blocked
autonomous_mission_failed          # daemon crashed (shouldn't happen but log it)
```

Each event carries the `intent_id`, current `iter_count`, and a `roadmap_snapshot` (the parsed roadmap state) so the frontend can re-render the sidebar checklist live.

### 8.4 Stopping rules (priority order)

Per user-confirmed scope:

1. **User stop** — chat-header "Stop autonomous mission" button → daemon.stop("user_stop"). Emits `autonomous_mission_paused`.
2. **Time budget hit** — at the top of each iteration, check `time.time() - started_at >= time_budget_seconds`. Emits `autonomous_mission_complete` with `reason="time_budget_exhausted"`. **Always the hard ceiling — never auto-extends in v0.5.0.** Skipped entirely for **full-auto** runs (where `time_budget_seconds is None`).
3. **Iteration cap** — every autonomous mission has a hard ceiling of `MAX_ITERATIONS = 100` regardless of time budget or full-auto setting. Defensive backstop against a daemon that somehow keeps extending the roadmap forever. A user who legitimately needs more than 100 iterations should run a follow-up mission against the same project.
4. **Convergence** — full-REFLECT verdict is `satisfied`. Emits `autonomous_mission_complete`.
5. **Hard block** — full-REFLECT verdict is `blocked` AND the loop has tried `await_user` once already this run. Emits `autonomous_mission_paused`.
6. **Cycle / pytest break** — if the post-iteration check fails twice consecutively, daemon.stop("check_failed"). The user will see the failures and decide.
7. **Repeated failures** — if 3 consecutive iterations end with the sub-mission's REFLECT marking the item BLOCKED, daemon.stop("blocked"). Don't keep grinding.

For **full-auto** missions (no time budget set), rules 2 and 3 invert in priority: the iteration cap is the ONLY hard ceiling, and rules 4-7 do all the real work. Full-auto is the right choice for "I'll watch the chat occasionally and stop it myself" use cases; it should NOT be the default.

When the daemon stops, it emits a **handoff document** as the final reflection summary in `roadmap.md`. The handoff includes: tally of shipped items, list of pending items, list of blocked items with reasons, recommended next session.

## 9. Failure modes + recovery

- **Server restart mid-run.** The daemon dies. On next launch, the AppState detects the live mission with `mission_state.phase == "autonomous_running"` and offers to resume (button in the chat header). Resume re-reads `roadmap.md` and picks up from the next unchecked item. The previous in-flight item gets re-dispatched (idempotent: if the work was already shipped, REFLECT will mark it done immediately).
- **`roadmap.md` corrupted (user edit + parse fail).** Daemon emits a `status_msg` warning, falls back to the last known good in-memory roadmap, continues. User can fix the file mid-run.
- **The agent ships breaking changes.** The daemon doesn't run pytest itself per iteration; it relies on the IMPLEMENT specialist's existing post-run summary. To catch breakage, the user can configure a "post-iteration check" command (defaults to `pytest -x --quiet`) that the daemon runs after each item; failure → `autonomous_mission_paused` with `reason="check_failed"`.
- **REFLECT specialist hallucinates a commit ref.** REFLECT's prompt explicitly says "DO NOT fabricate commit SHAs — read them from `git log` if needed." The daemon also validates each claimed commit via `git rev-parse <sha>` before writing it to the roadmap; a fabricated SHA gets stripped with a warning.
- **`await_user` mid-loop with the user away.** The daemon's tick is 5s; the await_user prompt sits in the chat indefinitely. If the time budget expires before the user replies, the loop stops with `reason="awaiting_user_at_deadline"` and the unanswered question stays in chat for the user's morning.

## 10. UI surface

### 10.1 Composer

- New toggle below the textarea: **∞ Run autonomously**. When OFF: existing one-shot Mission flow.
- When ON: subtitle reads "I'll ask for a time budget after we've nailed the spec." No budget picker here — that's a grill question (see §11).
- The "Start mission" button text becomes "Start autonomous mission".

### 10.2 Spec card (after grill phase)

When the spec lands AND the autonomous toggle was on at composer-start:
- "Build this roadmap" button becomes **"∞ Build autonomously"**.
- The card shows a budget confirmation block, pre-filled from the grill spec's recommendation. Presets as buttons:
  `1h (lunch break)` `4h` `6h` `8h` `12h` `24h` `48h` `Full auto (no time cap)`
  Plus a custom-minutes input for power users.
- A subtitle clarifies: "Acceptance criteria from the spec drive the convergence check. The mission stops when ALL criteria are met (regardless of budget remaining), the budget runs out, or you click Stop."
- A second subtitle (only shown for "Full auto"): "Full auto skips the time ceiling. The mission stops only on convergence, blocking, or your Stop click — but a 100-iteration cap is always enforced as a defensive backstop."

### 10.3 Chat header (during autonomous run)

- The mission badge shows the live status:
  - With budget: `∞ Autonomous · 1h 23m left · iter 4 · $0.34 (~$0.85/h)`
  - Full auto: `∞ Autonomous · 1h 47m elapsed · iter 4 · $0.34 (~$0.85/h)`
- Cost + burn rate use the existing v0.3.x cost-tracking infrastructure. `$/h` is computed as `current_total / hours_elapsed` and recomputed each iteration.
- A **Stop** button sits next to the badge. Clicking it confirms via a small popover ("Stop after current iteration completes? In-flight tool calls will finish first").

### 10.4 Plan-graph view

- The existing plan-graph view extends to show the roadmap as a top-level structure: tier sections, items with checkboxes, expand-to-see the underlying plan-graph for each shipped item.
- Acceptance criteria from the spec render as a separate pinned section ("Acceptance criteria · 3/7 met") — the user gets visual signal of how close to convergence the mission is, independent of how many roadmap items remain.

### 10.5 Mid-run messages in chat

- **Iteration start:** "∞ Iteration 4 — picked T1.3: Add the export-to-markdown handler."
- **Iteration complete:** "∞ Shipped at `abc123f`. Marked T1.3 done. Acceptance: 3/7 met."
- **Reflection:** styled card with the structured fields (completed, added, blocked, verdict, summary, **acceptance-criteria delta**).
- **Mission complete:** big "∞ Autonomous mission complete · 7/7 acceptance criteria met" banner with a "View handoff document" CTA.

## 11. The grill→budget flow

Per user direction: **the grill phase should be deep and rigorous when feeding into autonomous mode**, because the acceptance criteria it produces are the convergence ground truth — REFLECT only emits `verdict=satisfied` when all criteria are met. The grill prompt and structure both change when the autonomous toggle is on at composer-start.

### 11.1 Two grill modes

**Standard grill (autonomous OFF, current behavior):**
- 5–15 substantive questions, one at a time
- Final spec block as today
- Acceptance criteria section is OPTIONAL and often vague ("the feature works as expected")
- This is unchanged in v0.5.0

**Rigorous grill (autonomous ON, new):**
- 10–25 substantive questions, one at a time
- Same decision-tree structure (scope → users → data → integrations → constraints → acceptance → risks) but the model is told to push HARDER on acceptance criteria specifically
- Each acceptance-criteria bullet must be **measurable and binary** (the agent can check it as pass / fail without judgment calls). "The feature works" → rejected; "Running `npm run build` exits 0 and produces a `dist/` folder containing `index.html`" → accepted.
- Final spec block adds a `**Time budget:**` line with the model's recommendation
- New rule: the model is told it CANNOT emit the spec until it has at least 4 binary acceptance-criteria bullets. If the user says "we have enough" before that, the model asks one more question.

### 11.2 Spec block additions for autonomous missions — typed acceptance criteria

In addition to the existing `## Final spec` sections, an autonomous mission's spec includes a `**Time budget:**` line plus a typed acceptance-criteria list. **Each criterion is tagged with the validation strategy REFLECT will use to check it.** Three types are supported in v0.5.0:

- **`[bash]`** — runs a shell command, checks exit code or output. Cheapest. Right for builds, type checks, file existence, grep results.
- **`[chrome]`** — uses the engine's existing browser tools (`browser_navigate` / `browser_click` / `browser_type` / `browser_screenshot` / `browser_js`) to drive a real Chromium instance. Right for any feature that produces a webpage or web app. The criterion specifies the URL, interaction sequence, and the assertion (DOM state, computed style, screenshot pixel pattern).
- **`[vision]`** — takes a screenshot (browser via Playwright OR desktop via `mss`) and asks a vision-capable model "does this match X?". Right for features whose output is visual but not easily DOM-scriptable: native desktop apps, image generation, layout fidelity. Falls back to the bundled vision model (Ollama: `qwen2.5vl:7b` or similar; configurable in Settings → Network).

Concrete example (the bootstrap-roguelite mission spec):

```markdown
**Time budget:** 4h
*(My recommendation. Adjustable in the next step.)*

**Acceptance criteria:** *(must all be true at convergence; REFLECT validates each
one using its tagged strategy. Model can't fake them — runner verifies the check
actually ran and the output matches.)*

- [ ] `[bash]` `npm install` exits 0 with no peer-dependency errors
- [ ] `[bash]` `npm run build` exits 0 and produces `dist/index.html`
- [ ] `[bash]` `npx tsc --noEmit` exits 0 (strict mode passes)
- [ ] `[bash]` Exactly 6 source files (`find src -type f | wc -l == 4`)
- [ ] `[bash]` No `any` types: `! grep -rn ": any" src/`
- [ ] `[chrome]` `npm run dev`; navigate to http://localhost:3000;
      verify the canvas element exists AND its first 100×100 pixel sample
      from the center is pure green (RGB 0±5, 255±5, 153±5)
- [ ] `[chrome]` On the same page, `document.querySelector("canvas").width === 800`
      and `.height === 600`
- [ ] `[vision]` Open the dev server screenshot; verify "a single green
      circle on a dark navy background, no other shapes visible"
```

Notice the `[ ]` checkboxes — REFLECT marks them `[x]` as it confirms each criterion. A criterion stays `[ ]` until REFLECT validates it via the tagged check.

### 11.3 Why typed validation, not "the model decides if it works"

This was the user's pushback (better than my "manual-check escape valve" first draft). Without typed validation:
- Visual / behavioral features can't have measurable acceptance criteria → convergence is the model's mood
- The escape valve ("`[manual-check]` items don't gate convergence") meant half of features couldn't actually converge

With typed validation:
- `[bash]` works for builds, type checks, file shape, grep patterns — about half of typical mission acceptance criteria
- `[chrome]` works for anything that lives in a browser — webapps, dashboards, dev servers. **REFLECT validates the same way a person would: open the page, click the toggle, check that the background is actually dark.**
- `[vision]` works for native desktop apps and any "does this LOOK right?" check — REFLECT takes a screenshot and asks a vision model "is the dark-mode toggle on AND the background actually dark?"

The model can't fake any of these — the runner verifies that:
1. The check actually ran (audit log shows the bash exec / browser navigation / screenshot)
2. The output / pixels / vision-model verdict matched the criterion

If the criterion can't be expressed as `[bash]` or `[chrome]` or `[vision]`, the rigorous grill rejects it and asks the user to refine. The user CAN add a `[manual]` criterion as a final escape (the design keeps that affordance for true edge cases) but the rigorous grill discourages it — anything `[manual]` is excluded from convergence and listed in the handoff doc as "user must verify."

### 11.4 The infrastructure is already there

Worth noting: nothing in §11.2-11.3 requires NEW runtime infrastructure. The engine already has:
- `bash` tool — for `[bash]` checks
- `browser_navigate`, `browser_click`, `browser_type`, `browser_screenshot`, `browser_js`, `browser_read` — for `[chrome]` checks (Playwright via `[browser]` extras, already shipped)
- `computer_screenshot` and Pillow — for `[vision]` checks (already in `[desktop]` extras)
- Ollama's vision-capable models (e.g. `qwen2.5vl:7b`) — for `[vision]` checks

The work in v0.5.0 is exposing these to REFLECT (expanding its `tool_allowlist`), giving it the prompt-level guidance to USE them for typed validation, and adding the criterion parser that routes each `[type]`-tagged bullet to the right strategy.

### 11.5 The budget question itself

### 11.6 The budget question itself

Inserted into the rigorous-grill decision tree near the end (after acceptance criteria, before risks). Concrete example:

> **Question:** Based on the scope above (6 files, ~150 lines of code, no third-party assets needed), I'd estimate this needs ~1–2 hours of agent work. How long should I run autonomously? My recommendation: **4 hours** — gives buffer for one round of revisions if the verifier flags anything.
>
> **My recommendation:** 4h.
>
> Options: `1h (lunch break)` · `4h` · `6h` · `8h` · `12h` · `24h` · `48h` · `Full auto`

The user can answer with the preset name, a custom number ("3h"), or "full auto". The grill records the answer in the spec block as `**Time budget:** 4h`.

### 11.7 Why ground convergence in acceptance criteria

This was the user's pushback on open question #6 (now closed). Without it, `verdict=satisfied` is just the model's opinion — and the model is biased toward "satisfied" because that ends its work. With typed-acceptance-criteria-as-ground-truth:

- The model emits a deterministic, executable check per bullet during the rigorous grill (with a `[bash]` / `[chrome]` / `[vision]` type tag).
- REFLECT runs each check via the tagged strategy and reads the output; it cannot fake a `[x]` because the runner validates that the criterion was actually run + the output matches.
- The convergence signal becomes a real measurement, not a mood.
- **Visual / behavioral features that bash can't check now have first-class validation.** Dark-mode toggle? `[chrome]` opens the page, clicks the toggle, checks `getComputedStyle(document.body).backgroundColor`. Screenshot fidelity? `[vision]` asks the model. The agent validates the same way a person would.

Trade: the rigorous grill takes longer (more questions, more push). For a 5-minute fix the user might find this excessive. Mitigation: the autonomous toggle is opt-in; users who want a quick one-shot don't see the rigorous grill at all.

## 12. Persistence and replay

The autonomous mission lives at:
- `<project>/.resonant/roadmap-<intent_id>.md` — the source of truth, user-readable
- `~/.resonant/projects/<hash>/intents/<intent_id>/audit.jsonl` — per-iteration tool calls (existing v0.3.x audit format)
- `~/.resonant/projects/<hash>/intents/<intent_id>/iterations/<n>.json` — per-iteration metadata: started_at, finished_at, item_id, commit_sha, notes
- The session JSON for the autonomous mission carries `mission_state.phase = "autonomous_running" | "autonomous_paused" | "autonomous_complete"` plus `mission_state.intent_id` so resume detection works.

The diagnostics ZIP (v0.3.4) automatically includes the roadmap.md and iterations/*.json files when present.

## 13. Open questions / TBD

Updated through the design conversation. Resolved questions are kept here with their answers for the implementation team.

1. **[OPEN] REFLECT-only-on-explicit-tools or REFLECT-from-conversation-history?** The cheaper path is "read the conversation history of the last K iterations and synthesize" — no extra tool calls. The more thorough path is "REFLECT runs `git log`, `git diff`, reads roadmap.md, runs each acceptance-criteria check via bash to confirm." Given §11.4 (acceptance criteria are ground truth), the thorough path is mandatory for the convergence-check pass; the cheap path is fine for the per-item mark-done passes. **Resolution: full pass uses real bash checks; mark-done passes can synthesize.**

2. **[DEFER] Multiple parallel autonomous missions per project.** Non-goal for v0.5.0 but the architecture should NOT preclude it. Each daemon is keyed by `intent_id`, AppState holds a `dict[str, AutonomousMissionDaemon]`. v0.5.0 enforces one-at-a-time at the UI layer; v0.6.0+ relaxes.

3. **[DEFER] Time-budget UX when the user is *almost* done.** If the budget expires with one item left in the roadmap, the daemon should NOT auto-extend — but it COULD emit a chat message offering "Resume for ~10 more minutes?" with a button. v0.5.0 ships without auto-prompt; v0.5.x adds it once we have real-mission data.

4. **[DEFER] Roadmap rendering in the existing sidebar.** v0.5.0 ships with in-chat iteration messages only; v0.5.1 adds the sidebar checklist.

5. **[OPEN] Per-iteration commit signing / `--allow-empty` policy.** If REFLECT marks an item done that didn't actually produce a commit (e.g. a "rename a variable" task that the implementer no-op'd), should we allow `git commit --allow-empty` to keep the audit trail? Lean yes; flag it in the iteration log as `<empty>`. Open: who chooses — REFLECT, the loop daemon, or always-on?

6. **[CLOSED] Acceptance criteria as convergence ground truth.** Confirmed by user. See §11.4. REFLECT's `verdict=satisfied` is gated on every acceptance-criteria checkbox being `[x]`, with each `[x]` validated by an actual bash check whose output the runner inspects. The model can't fake convergence.

7. **[CLOSED] Glyph and budget UX.** Confirmed: ∞ glyph, budget asked during grill (not at composer time), with presets `1h | 4h | 6h | 8h | 12h | 24h | 48h | Full auto`. Composer toggle is just opt-in to the autonomous flow.

8. **[CLOSED] Should the grill itself change in autonomous mode?** Confirmed: yes. The "rigorous grill" mode is more thorough specifically because acceptance criteria are the convergence ground truth — soft criteria → no real convergence signal → mission either runs to budget exhaust or relies on the model's mood. See §11.1 + §11.4.

9. **[CLOSED] Cost / burn-rate display.** Confirmed: yes. Chat-header badge shows `$total ($/h burn rate)` alongside the time + iteration counters. Reuses the v0.3.x cost-tracking infrastructure.

10. **[CLOSED] Validation strategy for visual / behavioral acceptance criteria.** Confirmed: typed acceptance criteria with `[bash]` / `[chrome]` / `[vision]` tags. REFLECT validates each via the tagged strategy. Existing engine browser tools (Playwright via `[browser]` extras) handle `[chrome]`. Existing screenshot infra + Ollama vision models handle `[vision]`. **No manual-check escape valve as default — `[manual]` is allowed but discouraged in the rigorous-grill prompt and excluded from convergence.** See §11.2-§11.4 + §7.

11. **[CLOSED] Vision model selection for `[vision]` criteria.** User chose to ship all 3 validation types in v0.5.0 (validation is first-class). Resolution:
    - **Settings → Vision model** with a default of `qwen2.5vl:7b` (Ollama vision model, broadly available, Apache-2 licensed)
    - At mission start, `detect_backends` confirms the configured vision model is in the Ollama model list. If absent, the rigorous grill is told NOT to emit `[vision]` criteria — the budget UI shows a warning ("Vision model `qwen2.5vl:7b` not pulled — `[vision]` checks unavailable. Pull it or use only `[bash]`/`[chrome]` criteria"). User can dismiss or pull the model.
    - At REFLECT runtime, a `[vision]` criterion routes to the configured model; failure emits a `[vision]` criterion with `passed: false, evidence: "vision model unavailable: <reason>"` rather than crashing the run.
    - Future: per-criterion model override (e.g. `[vision:llava-next-13b]`) is non-goal for v0.5.0 but the schema reserves it.

## 14. Scope estimate

**v0.5.0 (foundation — all three validation types):**
- New `gui/autonomous_loop.py` (~350 lines: daemon class, threading, stopping-rule logic, resume-from-restart)
- New `orchestration/specialists.py::REFLECT` profile (~250 lines: rigorous prompt, typed-validation logic for all three types, structured-output schema with per-criterion evidence)
- New `gui/roadmap.py` (~400 lines: parser, writer, conflict resolution, typed-acceptance-criteria tracking with checkbox state, file locking)
- New `orchestration/acceptance_check.py` (~400 lines: dispatchers for `[bash]`, `[chrome]`, `[vision]` strategies; evidence capture; idempotency; vision-model availability detection)
- Vision-model integration (~150 lines: Ollama vision-API wrapper, prompt-and-parse for "does this match", configurable default `qwen2.5vl:7b`)
- Rigorous-grill prompt extension in `orchestration/grill_me.py` (~150 lines: the additional questions + binary-criteria rule + type-tag instruction + budget question + vision-availability gate)
- Settings: `vision.default_model` field; vision-availability check in `detect_backends`
- WS event additions + frontend handlers (~250 lines spread across `app.py` + `app.js`)
- Composer UI changes: autonomous toggle (~30 lines)
- Spec-card budget confirmation card with presets + "Full auto" handling (~120 lines)
- Chat-header autonomous badge + stop button + cost/burn-rate display (~80 lines)
- Tests (~900 lines covering daemon lifecycle, roadmap parser, REFLECT prompt content, typed-acceptance-criteria validation for all three strategies including stub vision model, stopping rules, resume-from-restart, the "vision model not pulled" graceful-degradation path)

Total: roughly **3,000 lines of new code + tests**, 7-10 days of focused work. All three validation types ship together because validation IS the product — the design doesn't make sense without `[vision]` for native desktop apps and image features.

**v0.5.x (refinement):**
- Roadmap rendering in the sidebar (T3.x scope from the v0.4 roadmap)
- Real-time iteration status in the chat header
- Auto-extend prompt at budget-expired (open question 3)
- Conflict resolution UI for user-edited-mid-run roadmaps

**v0.6.0 (cross-mission):**
- A roadmap that spans multiple missions for sustained multi-day work
- "Pin" autonomous missions across machines via git-tracked roadmaps

## 15. Recommended morning sequence (when implementation starts)

1. **Walk through this doc with the user** — final pushback before any code.
2. **Build `gui/roadmap.py`.** Pure data layer: parse the typed-acceptance-criteria format, write checkbox updates, file locking, conflict resolution. Easy to unit-test, no threading, no WS, no model. Lands as **v0.5.0a1**.
3. **Build `orchestration/acceptance_check.py`.** Dispatchers for `[bash]`, `[chrome]`, `[vision]` strategies. Evidence capture. Idempotency. Vision-model availability detection. Mock the underlying tools so the unit tests don't need a live Playwright/Ollama. Lands as **v0.5.0a2**.
4. **Extend `orchestration/grill_me.py` with the rigorous-grill mode.** Autonomous-aware prompt extension, binary-criteria rule, type-tag instruction, budget question, vision-availability gate. Tests pin prompt invariants. Lands as **v0.5.0a3**.
5. **Build `orchestration/specialists.py::REFLECT`.** Wire to `acceptance_check.py`. Two trigger modes (item-mark + full pass). Lands as **v0.5.0a4**.
6. **Build `gui/autonomous_loop.py`.** Mock the dispatch + reflect to keep daemon test surface small. Threading, stopping rules, resume-from-restart. Lands as **v0.5.0a5**.
7. **Wire WS protocol.** New events, command handlers in `app.py`. Lands as **v0.5.0a6**.
8. **Frontend.** Composer toggle, spec-card budget confirmation, chat-header badge with cost/burn-rate, stop button, in-chat iteration messages, `[vision]`-availability warning. Lands as **v0.5.0a7**.
9. **End-to-end smoke** against `ollama:deepseek-v4-flash:cloud` with a small scoped feature including at least one criterion of EACH type — `[bash]` (build passes), `[chrome]` (counter button click increments), `[vision]` (the rendered counter is centered and readable). Watch one full run hit real convergence. Lands as **v0.5.0**.

Each `a*` step is a separate commit, tagged for testing. Treat v0.5.0 itself as the GA after the smoke is green.

## 16. Success criteria

- A user can launch an autonomous mission, walk away, and come back to a complete handoff document.
- The roadmap.md is human-readable mid-run; you can see what's done and what's pending without opening the app.
- The acceptance-criteria checkboxes track real, measured progress — you can scan them mid-run and see what's *demonstrably* working vs what's still pending.
- **Convergence (`verdict=satisfied`) is real, not a model mood.** Every `[x]` was set because REFLECT ran the criterion's tagged check (`[bash]` / `[chrome]` / `[vision]`) and the output matched. The runner validates this — the model can't fake it.
- **Visual / behavioral features have first-class validation.** A "dark mode toggle works" criterion translates to a `[chrome]` check that opens the page, clicks the toggle, and verifies the body's computed background color. The agent validates the same way a person would.
- Stopping the mission mid-run leaves a clean state (no half-shipped commits, no dangling daemons).
- Restarting the server preserves the mission state — Resume just works.
- An empty roadmap that REFLECT can't fill (because the goal is met) cleanly emits `mission_complete` rather than spinning.
- The cycle guards (v0.3.3) and per-model thresholds (v0.4.9) keep working inside each iteration's sub-mission.
- The chat-header cost / burn-rate stays accurate to within ±5% of the actual API spend (confirmed against the v0.3.x cost-tracking infrastructure).
- The rigorous grill produces well-typed acceptance criteria — auditing a sample of grill outputs shows ≥90% of bullets have a `[bash]` / `[chrome]` / `[vision]` tag. `[manual]` bullets should be rare (≤10%) and obviously appropriate when present.
- The end-to-end smoke against `ollama:deepseek-v4-flash:cloud` produces a working scaffold + a clicked-and-verified UI element (the counter test) in one autonomous run.
- A repeat of the v0.4.x overnight run could in principle be expressed AS an autonomous mission ("ship every item in this roadmap doc, time budget 6h, acceptance: `[bash] pytest -q` exits 0 + `[bash] git log --oneline | wc -l >= 5` after each tier"), proving the pattern matches what we did manually.

That last criterion is the test: if v0.5.0 can replicate the overnight run as a feature instead of as ad-hoc scaffolding, we've codified the pattern correctly.
