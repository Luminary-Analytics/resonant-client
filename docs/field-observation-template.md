# Field-observation log — `<project-name>`

> Template for capturing real-world resonant-client autonomous-mission
> runs. Copy this file to `docs/field-observations/<date>-<project>.md`
> and fill in as the run proceeds. Findings here feed v0.5.6+ planning.

---

## Run setup

- **Project path:** `<absolute path>`
- **Workspace state:** greenfield / has-existing-code / mid-refactor
- **Model:** `deepseek-v4-pro:cloud` / `deepseek-v4-flash:cloud`
- **Started:** `<ISO timestamp>`
- **Mission intent_id:** `<filled in after dispatch>`
- **Roadmap path:** `<project>/.resonant/roadmap-<intent_id>.md`

## The prompt (verbatim)

```
<paste exactly what was sent into the Mission composer>
```

---

## Phase 1: Grill (spec refinement)

### What the grill asked

| # | Question | Quality (1-5) | Notes |
|---|---|---|---|
| 1 | <question> | <1-5> | <why; was it on-target?> |
| 2 | | | |
| ... | | | |

**Total questions:** <count>
**Style:** rigorous (10-25) / standard (5-15) / other

### Grill behaviors worth flagging

- [ ] Did the grill recognize the scope? (vision vs feature)
- [ ] Did it push back on vague success criteria?
- [ ] Did it surface scope-narrowing options? (e.g. "what's the first shippable slice?")
- [ ] Were any questions redundant or off-topic?
- [ ] Did the question cadence feel right? (too many, too few, too long, too short?)

### Final spec (paste the spec block)

```markdown
<paste the `## Final spec` block the grill produced>
```

**First-pass assessment:**
- [ ] Acceptance criteria are concrete and testable (≥3 [bash] / [chrome] / [vision])
- [ ] Time budget is reasonable for the scope
- [ ] In-scope / out-of-scope cleanly separated
- [ ] Refined intent matches what was actually wanted

---

## Phase 2: Autonomous loop

Track each iteration here. Add rows as the daemon works.

| Iter | Item picked | Duration | Outcome | Verdict | Notes |
|---|---|---|---|---|---|
| 1 | T1.1 | <s> | shipped / failed / stuck | continue | <what happened> |
| 2 | | | | | |

**Convergence:** ✅ satisfied / ⚠ paused (`<reason>`) / ✗ failed
**Total elapsed:** `<duration>`
**Sub-mission count:** `<n>` Phase-1 missions dispatched

### Iteration patterns to watch for

- [ ] Did REFLECT correctly mark passing criteria green?
- [ ] Did REFLECT add follow-up items via the `added` field when work needed to grow?
- [ ] Were there any "stuck" iterations — same item attempted twice?
- [ ] Did the walker auto-retry path fire? (look for `plan.rewrite` events)
- [ ] Did any iter exceed 2x the median? Where did the time go?
- [ ] Did the agent get the `working_subdir` right, or did it scaffold into the wrong place?

---

## Phase 3: GUI / UX observations

### What worked

- (e.g.) The mission badge clearly showed iter count + remaining time

### What broke or felt rough

- (e.g.) Switching away from the autonomous mission's session and back lost some chat-pane scroll position
- (e.g.) The criteria inspector didn't refresh until I forced a session-switch

### Surface area gaps

- [ ] Were there moments where I wanted to see something the GUI doesn't expose?
- [ ] Did I want to act on something but couldn't? (e.g. cancel a single iter without stopping the mission)
- [ ] Did any banner / inspector / badge stay stale after an event that should have refreshed it?

---

## Phase 4: Artifacts

After the run completes, capture what the daemon produced.

- **Files created (relative to project root):** <list>
- **Lines of code:** `<git diff --stat>`
- **Tests added by the agent:** <count>
- **Commits made:** `<git log --oneline | wc -l>`

**Quality check on artifacts:**
- [ ] Code runs / compiles
- [ ] Tests (if any) actually exercise the right thing
- [ ] No obvious dead-code / placeholder comments left behind
- [ ] Conventions match the project (file layout, naming, etc.)

---

## Phase 5: Post-mortem — what should v0.5.6 do about this?

After the run, list concrete action items keyed back to observations.

| Observation | Severity | Proposed v0.5.6 work |
|---|---|---|
| (e.g.) Grill emitted vague "users can run their apps" criterion | high | Sharpen R2+ "behavior-testing > existence-testing" rule with a 'avoid subjective adjectives' clause |
| (e.g.) Inspector stayed stale after REFLECT for 30s | medium | Push autonomous_mission_roadmap event from server when REFLECT writes the file, instead of polling-on-event |
| | | |

---

## Raw artifacts to archive

For posterity, paste paths to:

- Roadmap markdown: `<project>/.resonant/roadmap-<intent_id>.md`
- Audit log: `~/.resonant/projects/<hash>/intents/<intent_id>/audit.jsonl`
- Iteration metadata: `~/.resonant/projects/<hash>/intents/<intent_id>/iterations/`
- Diagnostics ZIP (if generated): `<path>`
- Smoke baseline (if relevant): `<project>/.resonant/smoke-baselines/`

---

## Final summary

Three sentences max. What did we learn?

1.
2.
3.
