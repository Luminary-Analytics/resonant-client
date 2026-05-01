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
