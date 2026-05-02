# Long-running agents — Phase 2: Autonomous Mission

**Status:** design doc, pre-implementation. Target: v0.5.0.
**Author / date:** drafted 2026-05-02 by the autonomous-loop overnight run, iterating with the user.
**Predecessor:** [`docs/long-running-agents.md`](./long-running-agents.md) (Phase 1, shipped in v0.3.x).

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

User confirmed **"Autonomous Mission"**. Surface treatment:
- New toggle in the composer next to "Start mission": **"🌙 Run autonomously"** (the moon glyph is suggestive without being precious — the mode runs while you're away).
- When active, the composer shows a time-budget picker (1h / 4h / overnight / custom). Default: 4h.
- The chat header on an active autonomous mission shows a small badge: `🌙 Autonomous · 1h 23m left · iter 4`.
- The "Build this roadmap" CTA on the spec card becomes "🌙 Build autonomously" when the toggle is on.

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
2. Toggles **🌙 Run autonomously** in the composer. Picks a time budget. Types the feature description.
3. Hits Start. The grill phase runs as today (one focused question at a time).
4. When the spec lands, clicks **🌙 Build autonomously**. The autonomous loop begins.
5. Watches mid-run progress in the chat (each iteration logs a summary message: "Iteration 3 complete — shipped T1.5, marked T2.1 in progress"). Walks away.
6. Comes back. Reads the handoff document the agent emits at the end. If the goal is met, opens the resulting commits + diff in their tool of choice. If not, decides whether to extend the budget or hand off the remaining work to themselves.

What the user sees during the run (visible in the chat panel):
- Each iteration: "🌙 Iteration N · picked item: ..." → tool calls (collapsed in groups, like today) → "✓ Iteration N complete — shipped to commit `<sha>`. Marked `<item-id>` done."
- After every K iterations or when the roadmap empties: a "🌙 Reflection" message — short summary of what's done, what's pending, what new items the agent added.
- At the end (time budget exhausted, convergence, or user stop): a "🌙 Mission complete" or "🌙 Mission paused" message with a link to the handoff doc.

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
    description="Review autonomous-mission progress. Mark items done. Add new items if needed. Emit structured verdict.",
    system_block=(
        "You are the REFLECT specialist for an autonomous mission. Your job is "
        "to keep the roadmap honest: mark items as complete with their commit "
        "refs, identify items that should be added based on what shipped, and "
        "emit a structured verdict that drives the loop daemon's next move.\n\n"
        ...
        "End your response with a fenced JSON code block:\n\n"
        "```json\n"
        "{\n"
        '  "completed": [{"id": "T1.2", "commit_sha": "abc123f", "note": "..."}, ...],\n'
        '  "added": [{"id": "T2.1", "tier": 2, "title": "...", "description": "..."}],\n'
        '  "blocked": [{"id": "T1.4", "reason": "needs schema decision"}],\n'
        '  "verdict": "continue" | "satisfied" | "blocked",\n'
        '  "summary": "<one-paragraph user-facing summary>",\n'
        '  "estimated_remaining_minutes": <int>\n'
        "}\n"
        "```\n"
    ),
    tool_allowlist=READ_ONLY_TOOLS | _AWAIT_USER | frozenset({"file_edit"}),
    max_steps=12,
    confidence_threshold=0.7,
),
```

Note `file_edit` is allowed — REFLECT directly edits `roadmap.md`. That's the one specialist that needs write access to the roadmap; everyone else reads.

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
2. **Time budget hit** — at the top of each iteration, check `time.time() - started_at >= time_budget_seconds`. Emits `autonomous_mission_complete` with `reason="time_budget_exhausted"`. **Always the hard ceiling — never auto-extends in v0.5.0.**
3. **Convergence** — full-REFLECT verdict is `satisfied`. Emits `autonomous_mission_complete`.
4. **Hard block** — full-REFLECT verdict is `blocked` AND the loop has tried `await_user` once already this run. Emits `autonomous_mission_paused`.
5. **Cycle / pytest break** — if a sub-mission breaks pytest and the next iteration also breaks pytest, daemon.stop("pytest_broken"). The user will see the failures and decide.
6. **Repeated failures** — if 3 consecutive iterations end with the sub-mission's REFLECT marking the item BLOCKED, daemon.stop("blocked"). Don't keep grinding.

When the daemon stops, it emits a **handoff document** as the final reflection summary in `roadmap.md`. The handoff includes: tally of shipped items, list of pending items, list of blocked items with reasons, recommended next session.

## 9. Failure modes + recovery

- **Server restart mid-run.** The daemon dies. On next launch, the AppState detects the live mission with `mission_state.phase == "autonomous_running"` and offers to resume (button in the chat header). Resume re-reads `roadmap.md` and picks up from the next unchecked item. The previous in-flight item gets re-dispatched (idempotent: if the work was already shipped, REFLECT will mark it done immediately).
- **`roadmap.md` corrupted (user edit + parse fail).** Daemon emits a `status_msg` warning, falls back to the last known good in-memory roadmap, continues. User can fix the file mid-run.
- **The agent ships breaking changes.** The daemon doesn't run pytest itself per iteration; it relies on the IMPLEMENT specialist's existing post-run summary. To catch breakage, the user can configure a "post-iteration check" command (defaults to `pytest -x --quiet`) that the daemon runs after each item; failure → `autonomous_mission_paused` with `reason="check_failed"`.
- **REFLECT specialist hallucinates a commit ref.** REFLECT's prompt explicitly says "DO NOT fabricate commit SHAs — read them from `git log` if needed." The daemon also validates each claimed commit via `git rev-parse <sha>` before writing it to the roadmap; a fabricated SHA gets stripped with a warning.
- **`await_user` mid-loop with the user away.** The daemon's tick is 5s; the await_user prompt sits in the chat indefinitely. If the time budget expires before the user replies, the loop stops with `reason="awaiting_user_at_deadline"` and the unanswered question stays in chat for the user's morning.

## 10. UI surface

### 10.1 Composer

- New toggle below the textarea: **🌙 Run autonomously**. When OFF: existing one-shot Mission flow.
- When ON: a budget picker appears: `1h | 4h | 8h | overnight (12h) | custom`. Default: 4h.
- The "Start mission" button text becomes "Start autonomous mission".

### 10.2 Spec card (after grill phase)

When the spec lands AND the autonomous toggle was on at composer-start:
- "Build this roadmap" button is replaced by **"🌙 Build autonomously"**.
- A subtitle clarifies: "This mission will run for ~4 hours, picking items off the roadmap, shipping commits, reflecting on progress, and adding new items as needed. You can stop it anytime."

### 10.3 Chat header (during autonomous run)

- The mission badge gets a moon glyph and live counter: `🌙 Autonomous · 1h 23m left · iter 4`.
- A **Stop** button appears next to it.

### 10.4 Plan-graph view

- The existing plan-graph view extends to show the roadmap as a top-level structure: tier sections, items with checkboxes, expand-to-see the underlying plan-graph for each shipped item.

### 10.5 Mid-run messages in chat

- **Iteration start:** "🌙 Iteration 4 — picked T1.3: Add the export-to-markdown handler."
- **Iteration complete:** "🌙 Shipped at `abc123f`. Marked T1.3 done."
- **Reflection:** styled card with the structured fields (completed, added, blocked, verdict, summary).
- **Mission complete:** big "🌙 Autonomous mission complete" banner with a "View handoff document" CTA.

## 11. Persistence and replay

The autonomous mission lives at:
- `<project>/.resonant/roadmap-<intent_id>.md` — the source of truth, user-readable
- `~/.resonant/projects/<hash>/intents/<intent_id>/audit.jsonl` — per-iteration tool calls (existing v0.3.x audit format)
- `~/.resonant/projects/<hash>/intents/<intent_id>/iterations/<n>.json` — per-iteration metadata: started_at, finished_at, item_id, commit_sha, notes
- The session JSON for the autonomous mission carries `mission_state.phase = "autonomous_running" | "autonomous_paused" | "autonomous_complete"` plus `mission_state.intent_id` so resume detection works.

The diagnostics ZIP (v0.3.4) automatically includes the roadmap.md and iterations/*.json files when present.

## 12. Open questions / TBD

These are the questions I'd expect to surface during implementation. Resolving them now would shorten the v0.5.0 cycle.

1. **REFLECT-only-on-explicit-tools or REFLECT-from-conversation-history?** The cheaper path is "read the conversation history of the last K iterations and synthesize" — no extra tool calls. The more thorough path is "REFLECT runs `git log`, `git diff`, reads roadmap.md, runs the test suite to confirm green." First-pass: cheap path with a `git log`-only sanity check on commit refs.

2. **Multiple parallel autonomous missions per project.** Non-goal for v0.5.0 but the architecture should NOT preclude it. Decision: each daemon is keyed by `intent_id`, AppState holds a `dict[str, AutonomousMissionDaemon]`. v0.5.0 enforces one-at-a-time at the UI layer; v0.6.0+ relaxes.

3. **Time-budget UX when the user is *almost* done.** If the budget expires with one item left in the roadmap, the daemon should NOT auto-extend — but it COULD emit a chat message: "🌙 Time budget expired. One item left (T1.7). Resume for ~10 more minutes?" with a Resume button. v0.5.0 ships without auto-prompt — too much risk of nag-fatigue. v0.5.x adds it once we have data.

4. **Roadmap rendering in the existing sidebar.** Today the sidebar groups sessions by project + missions. An autonomous mission's roadmap could render as an expandable item under the Mission row. Defer to v0.5.1 — the in-chat iteration messages are enough for v0.5.0.

5. **Per-iteration commit signing / `--allow-empty` policy.** If REFLECT marks an item done that didn't actually produce a commit (e.g. a "rename a variable" task that the implementer somehow no-op'd), should we allow `git commit --allow-empty` to keep the audit trail? Probably yes; flag it in the iteration log as `<empty>`.

6. **The grill spec's "Acceptance criteria" section as the convergence ground truth.** REFLECT's `verdict == "satisfied"` should be backed by something more concrete than the model's opinion. Idea: REFLECT explicitly checks each acceptance-criteria bullet and returns `verdict = "satisfied"` only when ALL are checked. Defer concrete shape to implementation but pin this as the design intent.

## 13. Scope estimate

**v0.5.0 (foundation):**
- New `gui/autonomous_loop.py` (~300 lines: daemon class, threading, stopping-rule logic)
- New `orchestration/specialists.py::REFLECT` profile (~100 lines including prompt)
- New `gui/roadmap.py` (~250 lines: parser, writer, conflict resolution)
- WS event additions + frontend handlers (~150 lines spread across `app.py` + `app.js`)
- Composer UI changes: autonomous toggle, budget picker, run-autonomously CTA (~80 lines)
- Chat-header autonomous badge + stop button (~50 lines)
- Tests (~400 lines covering daemon lifecycle, roadmap parser, REFLECT prompt content, stopping rules, resume-from-restart)

Total: roughly **1,300 lines of new code + tests**, 3-5 days of focused work. Not autonomous-loop-friendly because it touches Mission flow architecture, has real human-design moments (the UI calls), and needs real-mission validation.

**v0.5.x (refinement):**
- Roadmap rendering in the sidebar (T3.x scope from the v0.4 roadmap)
- Real-time iteration status in the chat header
- Auto-extend prompt at budget-expired (open question 3)
- Conflict resolution UI for user-edited-mid-run roadmaps

**v0.6.0 (cross-mission):**
- A roadmap that spans multiple missions for sustained multi-day work
- "Pin" autonomous missions across machines via git-tracked roadmaps

## 14. Recommended morning sequence (when implementation starts)

1. **Walk through this doc with the user** — get pushback on naming, the budget defaults, the moon glyph, anything.
2. **Lock the roadmap format spec** — open question 1 (REFLECT depth), question 6 (acceptance-criteria convergence).
3. **Build `gui/roadmap.py` first.** Pure data layer, easy to unit-test, no threading, no WS. Lands as v0.5.0a1.
4. **Build `gui/autonomous_loop.py` second.** Mock the dispatch + reflect to keep the daemon test surface small. Lands as v0.5.0a2.
5. **Wire `orchestration/specialists.py::REFLECT` and the WS protocol.** Lands as v0.5.0a3.
6. **Frontend toggle + chat-header badge.** Lands as v0.5.0a4.
7. **End-to-end smoke against `ollama:deepseek-v4-flash:cloud`** with a small scoped feature ("scaffold a tiny Python CLI"). Lands as v0.5.0.

Each `a*` step is a separate commit, tagged for testing. Treat v0.5.0 itself as the GA after the smoke is green.

## 15. Success criteria

- A user can launch an autonomous mission, walk away, and come back to a complete handoff document.
- The roadmap.md is human-readable mid-run; you can see what's done and what's pending without opening the app.
- Stopping the mission mid-run leaves a clean state (no half-shipped commits, no dangling daemons).
- Restarting the server preserves the mission state — Resume just works.
- An empty roadmap that REFLECT can't fill (because the goal is met) cleanly emits `mission_complete` rather than spinning.
- The cycle guards (v0.3.3) and per-model thresholds (v0.4.9) keep working inside each iteration's sub-mission.
- A repeat of the v0.4.x overnight run could in principle be expressed AS an autonomous mission ("ship every item in this roadmap doc, time budget 6h"), proving the pattern matches what we did manually.

That last criterion is the test: if v0.5.0 can replicate the overnight run as a feature instead of as ad-hoc scaffolding, we've codified the pattern correctly.
