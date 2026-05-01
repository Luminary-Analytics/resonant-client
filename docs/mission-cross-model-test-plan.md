# Mission cross-model smoke test plan

**Why this exists:** Phase 1's grill prompt + spec extraction were only
verified against `deepseek-v4-flash:cloud`. The strict regex
(`^##\s+Final spec\s*$`) silently misses any model that emits a slightly
different heading. The interview persona may also collapse on smaller /
cheaper models that don't follow long instruction blocks.

This doc is the **manual** test plan we run against each model before
declaring Mission "works on backend X." Scope is small on purpose —
each pass takes ~5 min in the GUI.

## Models to test

Three classes, one model per class is enough:

| Class | Recommended model | Why |
|---|---|---|
| **Frontier** | `claude-code:sonnet` | Sets the upper bar for prompt adherence; if Mission fails here, the prompt itself is wrong. |
| **Frontier-budget** | `openai:gpt-4.1` or `codex:gpt-5.2` | Different RLHF / instruction style; catches Anthropic-specific assumptions in the prompt. |
| **Local / OSS** | `ollama:qwen3-coder-next:cloud` (or any non-deepseek Ollama) | Catches "smaller model can't sustain the persona" failures. |

(`deepseek-v4-flash:cloud` is the *baseline* — already verified in the
Phase 1 review.)

## Per-model checklist

For each of the three target models, do this in the GUI:

- [ ] Open a fresh project (not `resonant-client` — pick something with
      no prior Mission history so we exercise an empty Missions group)
- [ ] Switch the model selector to the target
- [ ] Click `🎯 Mission` toggle → composer opens
- [ ] Enter feature: `Add a /pin command that pins the active session to the top of the sidebar`
- [ ] Click Start — confirm the badge says "Mission · Drafting" and a
      fresh session appears
- [ ] **Round 1** — model should:
  - [ ] Use a tool to inspect the codebase (glob first, ideally)
  - [ ] Ask exactly **one** question
  - [ ] Include a "My recommendation:" line
  - [ ] **Not** ask for "everything you can think of" in a batch
- [ ] **Round 2-4** — same shape; pick the recommendation each round so
      we converge fast
- [ ] **Round ~5** — say "wrap up with the spec"
- [ ] Confirm:
  - [ ] Model emits a `## Final spec` block (renders as the styled
        spec card — B3 fix)
  - [ ] Build button appears below the spec
  - [ ] Backend's grill detection fires (badge stays drafting until
        click — no false transitions)
- [ ] Click Build → confirm phase advances to `Planning`, plan tab opens
- [ ] Click `×` on the badge → confirm/Stay/Exit dialog → Exit
- [ ] Confirm:
  - [ ] Past-mission indicator shows (B1)
  - [ ] Sidebar row drops to "Completed" subsection (B6)
  - [ ] Hover row → Resume button visible (B4)
- [ ] Click Resume → phase returns to `Planning` (since intent already
      dispatched); no re-dispatch
- [ ] Refresh the page → mission state survives (badge restored)

## Failure modes to record

For each failure, note:

- **What happened** (literal quote of the model's emission, or
  screenshot of the broken UI)
- **Why it broke** (best guess — wrong heading? prompt drift? tool
  output mis-parsed?)
- **Severity:**
  - `blocker` — Mission unusable on this model
  - `degraded` — Mission works but quality is poor (e.g. model asks
    multiple questions per turn)
  - `cosmetic` — looks bad but doesn't affect outcome

### Likely failure modes to look for

1. **Heading drift.** Model emits `### Final spec` (h3 not h2),
   `## Final Spec` (capital S), or `## Spec` only. Spec detection
   regex is strict — won't fire. *Mitigation*: relax the regex
   (case-insensitive, allow h2/h3) once we see the actual variations.
2. **Persona collapse.** Smaller models may forget the
   one-question-at-a-time rule by round 3 and start batching, or
   drop the "My recommendation:" anchor. *Mitigation*: tighten the
   prompt with a 2-sentence reminder near the end.
3. **Field-label drift.** Model uses `**Refined Intent:**` (capital I)
   or `**Intent:**` instead of `**Refined intent:**`. The
   `_REFINED_INTENT_RE` falls back to "first paragraph after the
   header" — which works but loses the structured field.
   *Mitigation*: extend the regex to be case-insensitive, accept
   "Intent" and "Refined intent" both.
4. **Tool-call doom-spirals.** Especially smaller models may grep
   speculatively. The "stop after 3 zero-greps" rule is in the
   prompt but isn't enforced — it's a request. If we see this in
   practice, consider an engine-level guard.
5. **Cancellation desync.** If the model crashes mid-stream (Ollama
   503, OpenAI rate-limit), does the badge correctly stay in
   `drafting`? Or does it get stuck "running" with no way out?
   Tested for Ollama 503 in Phase 1 review; verify on others.

## Pass criteria for a release

A model is "Mission-supported" if:

- 4 of 4 rounds elicit a single focused question with a recommendation
- Spec emits with the exact `## Final spec` heading on the first
  "wrap up" prompt (no retry needed)
- Build button fires correctly
- Badge phase transitions are accurate
- No console errors in the live browser tab

A model is "Mission-degraded" (acceptable but flagged in user-facing
docs) if:

- Spec emits but with format drift (model uses different label
  capitalization, or h3 instead of h2)
- Persona collapses by round 5+ (still functional, just rougher)

A model is "Mission-blocked" (must not appear as Mission-supported in
the model selector) if:

- Spec never emits even after explicit "wrap up" prompt
- Model can't sustain one-question-at-a-time even in round 1

## What to do with results

1. Record per-model results in this doc (append a "Test results" section
   below as we run each pass)
2. If a model is degraded with a known fix (e.g. relax regex), file
   the fix as a v0.3.x patch
3. If a model is blocked, either:
   - Fix the prompt to support it, or
   - Mark it as "Mission unsupported" in the model selector

## Test results

(Append per-model results below as we run them.)

### `deepseek-v4-flash:cloud` — Mission-supported (baseline)

Verified end-to-end in [Phase 1 review](./long-running-agents-phase-1-review.md).
Pass criteria met: spec emitted on round 5, Build button fired, full
spec dispatched to planner, plan tab auto-opened, exit/resume worked.

### `claude-code:sonnet` — Mission-supported

**Tested:** Full end-to-end, 4 rounds → spec → Build → exit.

**Verdict:** Pass criteria fully met. Quality is the highest of any
model tested.

**Detail:**
- Round 1: 4 tool calls (1 glob, 2 reads, 1 grep) — followed glob-first
  rule precisely. 40s, 77,519 input / 863 output tokens.
- Each round emitted exactly one focused question with "My
  recommendation:" anchor — perfect adherence to the prompt format.
- Rounds 1-4 all referenced the existing codebase (SessionRecord,
  list_sessions, st_mtime, the existing Missions group as a
  precedent for the new Pinned group).
- Spec emitted on the explicit "wrap up" prompt in round 4 with
  full `## Final spec` heading and all required subsections. Spec
  card rendered correctly (B3).
- Build → phase advanced to `planning_dispatched`, B2 pulse fired,
  plan tab auto-opened (UX fix #5).
- Exit → past-mission indicator shown (B1), badge cleared, toggle
  stayed hidden (correct for past mission).

**No format drift, no degraded behavior.**

### `codex:gpt-5.2` — Mission-degraded

**Tested:** Round 1 completed; round 2 cancelled at 3+ minutes due
to slowness; format remained correct throughout the partial run.

**Verdict:** Format works, but the model is too slow for
practical Mission use without backend caching / faster shell
execution. Recommend marking degraded in user-facing docs until
we can either parallelize Codex's shell exploration or limit the
tool palette.

**Detail:**
- Round 1: format perfect — single question, "My recommendation:"
  anchor, codebase-aware (caught a partial /pin scaffolding the
  user had introduced separately).
- BUT: tool palette is `Shell` only — no `glob`/`grep`/`file_read`
  tools are exposed by the Codex backend. The "glob-first / stop
  after 3 zero-greps" rule in the prompt translates to verbose
  PowerShell `Get-ChildItem` / `Select-String` invocations that
  are ~4× slower per tool call.
- Round 2 (after my reply requesting GUI-only scope): codex ran
  14+ shell commands across 3+ minutes without producing any
  prose, exploring the codebase aggressively. Did not reach the
  spec emission within the test window. Cancelled.
- After cancel, the backend stayed in a "session running" state
  for ~30s before accepting the next mission_start — required a
  hard server restart to clear (process kill + relaunch).

**Mitigations to consider:**
- Limit codex max_steps lower for Mission turns (cap exploration).
- Or: prefer `claude-code` / `ollama` family for Mission work and
  surface a hint when codex is selected ("Mission mode is faster
  with claude-code or ollama backends").

### `ollama:qwen3-coder-next:cloud` — Mission-degraded (format drift)

**Tested:** Full end-to-end, 3 rounds → spec → Build.

**Verdict:** Functional with mild format drift; the "trust the
project context" rule needs reinforcement for codebase-aware
small models.

**Detail:**
- Round 1: format good — focused question with recommendation, but
  ended with a redundant "What do you prefer?" trailer (mild
  drift; doesn't break anything).
- Round 2: model went off-script. Discovered the partial /pin
  implementation in the codebase and shifted into "verifying
  existing code" mode instead of producing a spec. Required an
  explicit re-prompt ("Wrap up with the structured Final spec block
  — exactly the format you were given") to course-correct.
- Round 3 (post-nudge): emitted a spec with the `## Final spec`
  heading correctly, but used UPPERCASE section labels
  (`ACCEPTANCE CRITERIA:` instead of `**Acceptance criteria:**`).
  The spec-card decoration still wrapped it correctly because the
  detection only checks the h2 heading; the `_REFINED_INTENT_RE`
  fallback path engaged for the refined-intent paragraph.
- Build → phase advanced to `planning_dispatched`, B2 pulse fired,
  plan tab auto-opened.

**Mitigations to consider:**
- Reinforce the "exactly this format" instruction with a more
  prominent reminder near the end of the grill prompt.
- Make the field-label regex (`_REFINED_INTENT_RE`) case-insensitive
  to catch "Refined Intent" / "REFINED INTENT" variants. Already
  has a fallback path so this is polish.
- The "discovered existing implementation, abandoned interview"
  failure mode is general — it can hit any model when the Mission
  feature overlaps with codebase scaffolding. Consider adding a
  prompt rule: "If you discover the feature already exists, ask
  the user whether they want to extend it or build it fresh —
  don't assume the existing implementation is what they want."

---

## New bug discovered during testing

**Stale `allSessions` across mission events.** When a Mission is
created via `mission_start` or its phase is changed via
`mission_dispatch_roadmap` / `mission_exit` / `mission_resume`,
the backend sends a `sessions_updated` (or `session_cleared` /
`mission_exited`) event with `sessions` (per-project) but **not**
`all_sessions` (cross-project). The frontend's
`renderFilteredSessions` reads from `allSessions` if available,
which means the just-created or just-exited mission session can
be missing from the sidebar until something else triggers an
`all_sessions` refresh.

**Reproduction:**
1. Start a mission via the toggle (claude-code:sonnet).
2. Drive through to spec, click Build, click Exit on the badge.
3. Inspect the sidebar — the just-exited mission row is missing
   from the "Completed" subsection. The current session ID is
   not findable in `this.allSessions` even though it's in
   `this.sessions`.
4. Resume affordance unreachable because the row isn't there.

**Severity:** degraded UX. The mission is correctly exited on
disk; the data is there; the UI just doesn't surface it until
the next page reload or project switch.

**Fix path:** add `all_sessions` to the WS responses for
`mission_start`, `mission_exit`, `mission_dispatch_roadmap`,
`mission_resume`, and probably the regular `message` flow's
`sessions_updated` broadcast too. Alternatively, have the
frontend `renderFilteredSessions` merge `sessions` over
`allSessions` rather than preferring one or the other.

**Severity classification:** ship-blocker for "Mission feature
fully working" claim, NOT a v0.3.1 regression (this bug existed
in v0.3.0 too). File for v0.3.2.

---

## Summary table

| Model | Format | Speed | Codebase-aware | Spec emitted | Verdict |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` (baseline) | ✅ | medium | ✅ | round 5 | **Supported** |
| `claude-code:sonnet` | ✅ | fast | ✅ (best) | round 4 | **Supported** |
| `codex:gpt-5.2` | ✅ (partial) | very slow | ✅ | not reached | **Degraded** |
| `ollama:qwen3-coder-next:cloud` | ⚠️ (drift) | medium | ✅ | round 3 (after re-prompt) | **Degraded** |

**Net recommendation:** Mission ships as "Supported on claude-code,
deepseek; Degraded on codex, qwen3" — surface the distinction in
the model selector or release notes. The `_REFINED_INTENT_RE` fallback
path absorbs Qwen's format drift cleanly so users don't see broken
extraction. The Codex slowness is the real issue for daily use.

---

# v0.3.3 — Bug #25 fix verification (2026-05-01)

After the user's roguelite mission revealed Bug #25 (project_path
defaulted to `C:\Program Files\Resonant Client` because the bundled
exe inherits that as cwd), v0.3.3 added: a sane project-path default
chain, an explicit "Build it at" picker in the mission composer, a
clickable project-path display in the chat header, and two new cycle
guards (windowed signature dedup + read-only churn cap).

## E2E pass — Chrome MCP against `claude-code:sonnet`

Run: dev server via `python -m resonant_client gui --browser --port 8909`,
viewport 1400×900, project pre-set to `D:\Repos\resonant-client`.

### Chat-header path display (A3)

✅ Visible top-left of chat header on a regular project — renders
   `Repos/resonant-client` (last two segments) with full path on hover.
✅ Click target wired — fires `folder_dialog` to swap projects.
✅ Unsafe path detection works — calling
   `_updateHeaderProjectPath('C:\\Program Files\\Resonant Client')`
   adds `header-project-path-unsafe` class, renders orange-tinted
   border + 8% wash, title becomes "⚠ Project is in a system /
   install folder…". Visual confirmation via screenshot.

**Verdict:** A3 ships clean.

### Mission composer project picker (A2)

✅ "BUILD IT AT" label + path input + 📁 Browse button visible on
   composer open. Pre-fills with `currentCwd`.
✅ Inline hints update on every keystroke:
   - empty → "Pick or type a folder — the agent will write files
     here." (warn class)
   - `C:\Program Files\evil` → "⚠ This is a system / install directory.
     Pick somewhere under your home folder instead." (warn class)
   - `D:\Repos\test-v033-target` (non-existent OK path) → "Folder
     will be created if it doesn't exist yet." (ok class)
✅ Browse button fires `folder_dialog` with `_pendingFolderPickConsumer
   = 'mission'` so the picked path lands in the composer field, NOT
   the welcome flow.

**Verdict:** A2 ships clean.

### Real mission — explicit path, no scavenger hunt (A2 + Bug #25)

Hooked the WebSocket to capture sends. Opened composer, set path to
`D:\Repos\test-v033-target` (a directory that didn't exist), entered
feature `Add a tiny TypeScript file that logs hello world.`, hit Start.

**Captured WS payload:**
```json
{
  "command": "mission_start",
  "feature": "Add a tiny TypeScript file that logs hello world.",
  "project_path": "D:\\Repos\\test-v033-target"
}
```

**Backend behavior (verified via session JSON on disk):**
- ✅ `D:\Repos\test-v033-target` was created on disk (was missing
  before the call).
- ✅ Session JSON at `~/.resonant/projects/be1ba420f3a0/sessions/0cdf68c2.json`
  records `project_path: "D:\\Repos\\test-v033-target"` and
  `mission_state.phase: "drafting"`.
- ✅ The session/project hash `be1ba420f3a0` matches `sha256(D:/Repos/test-v033-target)[:12]`,
  so apply_project_context was reached.

**Agent's actual tool calls (display_events):**
1. `Read C:\Users\richa\AppData\Local\Temp\resonant_prompt_*.txt` —
   internal claude-code IPC reading the prompt file Resonant wrote
   for it. Not a scavenger hunt; this is normal.
2. `Glob "**/*" path=D:\Repos\test-v033-target` — **globs the
   explicit project path.** Empty, as expected.
3. `text.done`: "The project directory is empty — no existing code
   to reference. **Question:** Where should this file live, and what
   should it be called? **My recommendation:** `src/hello.ts` …"

Total elapsed: 24.9s. Compare to the v0.3.2 baseline where the agent
hit the 24-step cap searching `C:\Users\richa\Desktop`,
`C:\Users\richa\source\repos`, etc., for files it had created in a
sibling specialist's session.

**Verdict:** Bug #25 fix works end-to-end. The agent does NOT scavenger-
hunt when the project path is explicitly set.

### Cycle guards (windowed dedup + read-only churn cap)

Trusted via 12 new unit tests (all pass):
- `TestWindowedCycleRepeat`: 6 cases covering empty history, single
  call, interleaved repeats (`A→B→A→C→A` catches at 3), turn
  boundary respect, window cap, threshold trip.
- `TestCountReadOnlyChurn`: 6 cases covering empty, consecutive reads,
  write-resets-streak, bash-is-neutral-not-truncating, turn boundary,
  sane threshold constant.

E2E loop trigger deferred — forcing claude-code to loop is expensive
and the unit-test surface is already comprehensive. Wire is identical
to the existing strict-trailing doom-loop, which has E2E coverage.

**Verdict:** Ship as covered by unit tests.

## Bugs surfaced during E2E

### Bug A — chat-header path goes stale after explicit-path mission

When the user opens a mission with a project_path different from
`currentCwd`, the backend correctly switches the project context but
the frontend's `currentCwd` (and therefore the chat-header path
display) doesn't update until the next init event.

**Reproduction:**
1. Open the app on project `D:\Repos\foo` (chat header shows `Repos/foo`).
2. Open mission composer. Set "Build it at" to `D:\Repos\bar`.
3. Type a feature, hit Start.
4. Backend creates `D:\Repos\bar`, switches project context, runs grill.
5. Chat header still shows `Repos/foo`. Title hover still says "Project: D:/Repos/foo".

**Why:** `this.currentCwd` is only assigned in two places:
`app.js:2360` (handles `init` event) and `app.js:7400`
(`selectProjectFolder`, the welcome screen flow). Neither fires on a
mission_start project swap. The `session_cleared` event payload
doesn't carry `cwd` either.

**Severity:** UI display bug, not a data corruption. The session JSON
is correct, the agent works in the right place, the user just sees
the wrong path label. Confusing but not destructive.

**Fix path for v0.3.4:** Either (a) include `cwd` field in the
`session_cleared` event when `apply_project_context` was called, and
have the frontend update `currentCwd` + call `_updateHeaderProjectPath`
on receipt; or (b) emit a separate `cwd_changed` event whenever
`apply_project_context` runs.

### Bug B — `header-project-path-empty` class never fires in practice

The `_updateHeaderProjectPath('')` branch sets the empty state, but
`apply_project_context` always falls through to a non-empty path
(`os.getcwd()` or the safe-default), so empty cwd is unreachable on
the live app. Dead code, low priority — drop the branch in v0.3.4
unless we add a "no project" lifecycle.

## v0.3.3 verdict

**Bug #25 root cause is fixed.** The four pieces (sane default,
explicit picker, header display, cycle guards) all work as designed
when exercised end-to-end against the gold-standard model
(`claude-code:sonnet`).

**One real follow-up bug** (chat-header staleness on mission project
swap) and **one cosmetic loose end** (dead empty-state branch) — both
file for v0.3.4.

**Recommended next iteration after v0.3.4:**
1. **Telemetry/log shipping** (deferred from v0.3.3 plan) — at minimum,
   a "Help → Save diagnostics ZIP" button so users can attach logs to
   GitHub issues without hunting `~/.resonant/`.
2. **Plan-graph node `working_subdir` field** — even with the right
   project_path, sibling specialists still have no formal channel to
   tell each other "I created `src/scenes/` — work there." This was
   the deferred A3 from v0.3.3 planning. Lower priority now that the
   common case (explicit path) works, but worth doing before Phase 2.
3. **Specialist "stop and ask" affordance** — the cycle guards stop
   loops, but the agent still can't ASK the user for missing context
   ("which folder under the project should I scaffold into?"). A new
   `await_user` tool would close that gap without a Session refactor.
