# Phase 1 Mission UI — End-to-end review

Smoke test conducted in the actual GUI driving Chrome MCP. Recorded the
real bugs discovered (and fixed in-flight), what worked, and a backlog
for future polish.

Test scenario: drive a Mission end-to-end on
"Add an /export command that exports the active chat session to a markdown
file with a header (model + ISO timestamp) and ### User / ### Assistant
turn headings."

---

## ✅ What works (verified end-to-end)

- **Mission toggle** in chat header opens composer; textarea autofocuses;
  Start button correctly disabled until input.
- **Composer submit** triggers `mission_start` → backend creates a fresh
  session flagged with `mission_state.phase = "drafting"`, frontend
  switches to it, badge appears with "Drafting" phase, toggle hides.
- **Grill phase** runs as expected. Model adopts the interviewer persona,
  uses `glob` first to inspect the codebase (the new prompt rule works),
  finds the right files (`session.py`, `events.py`, `gui/app.py`), then
  asks **one** focused question with a "My recommendation:" anchor. Model
  even recognized `grill_me.py` in the codebase and noted the meta-irony.
- **Project context injection** worked — the model used `RESONANT.md`
  conventions in its recommendations (e.g. respected the
  `EngineEvent`/`ClientCommand` pattern instead of inventing a new one).
- **Multi-turn dialogue** flows naturally through the existing chat loop.
- **Spec emission** — model wrote `## Final spec` with all required
  subsections (refined intent, assumptions, in-scope, out-of-scope,
  technical constraints, acceptance criteria, **plus** explicitly called
  out a path-traversal security risk under Open risks).
- **Build button** appeared beneath the spec message (gated correctly to
  drafting-phase only — the gate fix held).
- **Build click** → backend dispatched the **full spec markdown** (not
  just the refined-intent paragraph), advanced phase to
  `planning_dispatched`, returned `intent_id`, frontend auto-opened the
  plan-graph panel and disabled the button.
- **Badge phase indicator** updates live across phase transitions
  (drafting → planning_dispatched).
- **Sidebar Missions group** renders all 3 mission sessions with
  phase-colored dot glyphs; exited missions dim correctly; current
  session row highlighted.
- **Exit-mission affordance** (after the in-flight fix) — clicking the
  `×` on the badge with confirm()=true marks the session as exited,
  badge clears, toggle reappears, mission row in sidebar dims.
- **Zero console errors** across the full happy path.
- **No regressions** — 900 unit tests still green.

---

## 🐛 Real bugs found during the smoke test (all fixed in-flight)

These were genuine bugs that would have shipped without this end-to-end
pass — unit tests didn't catch any of them.

### 1. `mission_state` was unreachable to the sidebar fast-path scanner

`SessionRecord.to_dict()` placed `mission_state` *after* the
`conversation_history` array. For any session with even one model turn,
that pushed `mission_state` past the 4KB prefix the fast-path summary
regex examines — so the sidebar saw `mission_state: undefined` and
rendered every mission as a regular session.

**Fix:** reorder `to_dict()` so all small metadata fields (mission_state,
message_count, thinking_mode, etc.) come *before* the large arrays
(conversation_history, display_events). Ensures the fast-path scanner
finds them within 4KB.

### 2. `session_cleared` wiped the user's seed-feature message

`startMission` rendered the seed feature as a `msg-user` immediately for
responsive feedback, then dispatched `mission_start`. The backend's
`session_cleared` handler — which always wiped chat — fired ~50ms later
and erased the user message. The user then saw an empty chat for ~3s
until the model's first reply streamed in.

**Fix:** backend tags the `session_cleared` event with `mission_started:
true` when it was triggered by a mission. Frontend skips the chat wipe
in that case — the locally-rendered user message survives, and the
streaming events from the new session render below it.

### 3. `_currentSessionSummary` looked at stale `allSessions`

`session_cleared` only updates `this.sessions` (per-project), not
`this.allSessions` (cross-project). `_currentSessionSummary` was reading
`allSessions` first and falling through. So immediately after
`mission_start`, the lookup returned null, `_syncMissionUI` thought no
mission was active, and removed the badge it had just rendered.

**Fix:** `_currentSessionSummary` now prefers `this.sessions` (always
fresh after `session_cleared`) before falling back to `allSessions`.

### 4. `handleMissionExited` updated the wrong array + wrong render

The `mission_exited` event handler was updating `allSessions` (instead
of `sessions`), calling `renderSessionList()` (instead of the canonical
`renderFilteredSessions()`), and never running `_syncMissionUI` to
restore the toggle visibility. Result: after exit, the badge cleared
but the toggle stayed hidden, and the sidebar didn't re-sort the
exited mission to inactive.

**Fix:** mirror the `sessions_updated` flow — update `this.sessions`,
update `this.currentSessionId`, call `renderFilteredSessions()`, then
`_syncMissionUI()`.

### 5. (Test-environment, not a product bug) Browser cached `app.js`

The static-files server serves `app.js?v=65` with a *static* cache
buster. `location.reload(true)` was insufficient to pick up the JS
edits in Chrome MCP. Worked around by tagging the URL with a timestamp
query param. **Action:** consider switching `?v=` to a hash of the
file or a build timestamp so dev iteration on JS doesn't require manual
cache-busting.

---

## 🟡 Visible polish issues — backlog for follow-up

Roughly ranked by user-visible pain:

### Tier A — likely worth doing in a v0.3.x patch

**A1. Cancel during a mission doesn't ask about exit.** The Cancel button
kills the current turn but leaves the mission's `phase` at `drafting`.
A user who hits Cancel mid-question is probably trying to abandon the
mission, not just retry the question. *Fix:* on Cancel, surface a
small affordance "Exit mission?" alongside the regular cancel.

**A2. Composer backdrop click closes without confirmation.** A long
feature description typed into the textarea is lost if the user
fat-fingers the backdrop. *Fix:* if textarea has more than ~20 chars
of input, treat backdrop click as a no-op (only the explicit Cancel
button or Esc dismisses).

**A3. Seed-feature message has cosmetic whitespace.** Renders as
"\n            Add an /export command…" — leading whitespace from the
template literal in `addUserMessage`. *Fix:* trim before render, or
fix the template.

**A4. Static cache-buster `?v=65`.** Already mentioned above; this
slows down all dev iteration on the GUI, not just Mission work. *Fix:*
generate the cache-bust query param at server startup (e.g. seconds
since epoch) so each dev run gets a fresh value, or hash the file.

### Tier B — nice-to-have polish

**B1. No "you're viewing a past mission" indicator.** When the current
session is an exited/completed mission, the badge clears and the
toggle reappears as if nothing happened. *Fix:* if the current
session has `mission_state` with phase exited/completed, show a
muted "🎯 Mission · exited" inline label (no `×` button) instead
of the regular toggle.

**B2. Phase transition has no celebration animation.** Going from
`drafting` to `planning_dispatched` is a meaningful moment — the
spec was captured, the planner is dispatched. The badge phase label
just changes silently. *Fix:* a subtle 300ms pulse on the badge when
phase changes, or a brief toast.

**B3. Spec block visual treatment.** The model's `## Final spec` markdown
renders as a wall of bold-on-newline `**Refined intent:** …`
`**Key assumptions:** …` pairs. Functional but heavy. *Fix:* detect
the spec block and render it as a styled card (similar to run-card)
with each subsection as a compact row.

**B4. No "Resume mission" affordance on exited rows.** Once exited,
there's no way to re-enter a mission. *Fix:* on an exited mission row,
add a small "Resume" action — re-flips the phase to drafting and the
session continues. (Useful if the user accidentally exited.)

**B5. Composer Cmd+Enter hint isn't visible.** Code supports it, but the
modal doesn't show the hint. *Fix:* add `<kbd>⌘ Enter</kbd>` next to
the Start button.

**B6. No "Active / Completed" sub-grouping in sidebar.** Inactive
missions sort to the bottom but there's no visual section break.
*Fix:* group with a "Completed" subheader.

### Tier C — Phase 2/3 territory (deferred)

- **C1. Run-card title for *post-grill* turns.** The fix to anchor the
  run-card title to `mission_state.seed_feature` covers the drafting
  turns, but once the orchestrator is running (planning_dispatched and
  beyond), each verify/repair turn will produce its own run-card with
  its own user message. Deferring to Phase 3 when concurrent
  orchestration changes the rendering model anyway.

- **C2. Plan tab doesn't auto-update on intent events when on another
  tab.** If the user is on the Browser preview tab when intent
  dispatches, the plan tab opens but doesn't grab focus on every
  graph-rewrite event. Probably correct behavior, but worth a usage
  decision later.

- **C3. Mission state survives reload (verified)** — refreshing the
  page mid-mission correctly restores the badge with phase. Good.
  But the in-memory `_pendingMissionFeature` doesn't survive — minor
  edge case if the user closes during the very first frame after
  clicking Start. Probably never matters.

---

## ✨ Pleasant surprises

- **Project-context injection** noticeably changed the model's behavior.
  Before it: model claimed "this is a CLI app" because it had no idea.
  After: model used `RESONANT.md` conventions in its recommendations
  unprompted (referenced `EngineEvent`/`ClientCommand` pattern,
  respected the "tool execution lives in `engine/tools.py`" rule when
  proposing where the export logic should live).
- **Spec quality** was higher than expected. The model called out a
  path-traversal security concern under "Open risks" without being
  asked. Recommend keeping the prompt's "Open risks" subsection — the
  model uses it well.
- **Glob-first rule worked.** Round 1 model started with a single
  `glob **/*.py` instead of the 7-grep doom-spiral we saw in
  pre-Phase-1 testing.
- **Sidebar Missions group sorts correctly** — active phases first,
  exited at bottom, dimmed.

---

## Recommendation

Phase 1 is shippable as v0.3.1 once the four in-flight fixes (now
committed in this branch) are validated by the existing test suite.
The Tier A polish items can ride along; Tier B can wait for a v0.3.2
follow-up. Tier C is properly Phase 2/3 work.

The biggest unknown remains: the model's grill quality varies across
backends. Tested with `deepseek-v4-flash:cloud`. Worth a separate pass
across at least one Claude model and one OpenAI model before assuming
the prompt generalizes.
