# Field-observation log — `resonant-linux-bridge`

> First ambitious-greenfield stress test of the autonomous-mission flow.
> A vision-scope prompt (open-source Linux experience for Windows apps)
> fed into the Mission composer to see how the grill + daemon handle
> something deliberately too large for one mission. Findings here feed
> v0.5.6+ planning.

---

## Run setup

- **Project path:** `D:\Repos\resonant-linux-bridge\`
- **Workspace state:** greenfield (fresh `git init`, single empty initial commit)
- **Model:** `deepseek-v4-pro:cloud` (default; PLAN_DEEP planner)
- **Started:** _<fill in when dispatched>_
- **Mission intent_id:** _<filled in after dispatch — visible in the chat header badge>_
- **Roadmap path:** `D:\Repos\resonant-linux-bridge\.resonant\roadmap-<intent_id>.md`

## The prompt (verbatim)

```
We are creating an open-source Linux experience that makes running
Windows apps and games feel natural, simple, and reliable for everyday
users. Instead of forcing people to understand Wine, Proton, runners,
prefixes, dependencies, launch flags, and compatibility tweaks, the
system would provide a clean Windows-like interface that automatically
chooses the best compatibility path, installs what each app needs, and
clearly explains when something cannot run. The goal is not to pretend
Linux can run everything perfectly, but to bridge the gap between
Windows and Linux in a practical, transparent way: run what works,
simplify what is complex, document what breaks, and give users a
friendly fallback when needed. In the long term, this becomes a
community-driven compatibility layer and eventually a full distro for
people who want the freedom, privacy, and openness of Linux without
giving up the Windows software and games they still rely on. We will
make it opensource on github for everyone.
```

---

## Phase 1: Grill (spec refinement)

### What the grill asked

| # | Question | Quality (1-5) | Notes |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |
| 8 | | | |
| 9 | | | |
| 10 | | | |

**Total questions:** _<count>_
**Style:** rigorous (10-25 expected for autonomous) / standard (5-15) / other

### Grill behaviors worth flagging (key research questions for this run)

- [ ] **Did the grill recognize the scope?** This is a multi-year, multi-team product vision, not a single mission. A "good" grill should push back: "this is too big — what's the FIRST shippable slice you'd accept?"
- [ ] **Did it surface the choice between launcher / detector / docs / distro as separate slices?**
- [ ] **Did it ask about the host platform?** (We'd need WSL or a Linux VM to actually develop / test this — does the grill catch that?)
- [ ] **Did it ask about target distros?** (Ubuntu vs Arch vs Fedora — implementation diverges hugely)
- [ ] **Did it push back on subjective criteria** like "feel natural" / "simple and reliable"?
- [ ] **Did it ask about scope of "compatibility path detection"?** (Wine version selection is a deep rabbit hole — does it recognize that?)
- [ ] Were any questions redundant or off-topic?

### Final spec (paste the spec block once it appears)

```markdown
<paste the `## Final spec` block the grill produced>
```

**First-pass assessment:**
- [ ] Acceptance criteria are concrete and testable (≥3 [bash] / [chrome] / [vision])
- [ ] Time budget is reasonable for the scope (1h / 4h / 24h / full-auto)
- [ ] In-scope / out-of-scope cleanly separated
- [ ] Refined intent matches what was actually wanted (not "swallow whole vision")
- [ ] **Specifically:** does the spec carve out a defensible v0.1 slice, or did it try to encompass the full distro?

---

## Phase 2: Autonomous loop

Track each iteration here. Sidebar inspector should mirror this in real time
once the daemon starts — watch how the two diverge if at all.

| Iter | Item picked | Duration | Outcome | Verdict | Notes |
|---|---|---|---|---|---|
| 1 | T1.1 | _s_ | shipped / failed / stuck | continue | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |

**Convergence:** ✅ satisfied / ⚠ paused (`<reason>`) / ✗ failed
**Total elapsed:** _<duration>_
**Sub-mission count:** _<n>_ Phase-1 missions dispatched

### Iteration patterns to watch for (run-specific)

- [ ] Does the implementer try to scaffold a full Linux distro repo? (Probably out of scope; how does it handle?)
- [ ] Does it pick a single language / framework for the launcher? (Tauri, Electron, GTK, Qt, raw shell, etc.)
- [ ] Does it require Wine / Proton / wineserver to be installed locally to satisfy [bash] criteria? (We're on Windows — these calls would fail.)
- [ ] Does REFLECT correctly identify "we can't run this on the host platform" and pivot, or just mark criteria failed forever?
- [ ] Did the walker auto-retry path fire? (look for `plan.rewrite` events)
- [ ] Did the daemon hit `blocked / needs human decision` cleanly?

---

## Phase 3: GUI / UX observations

### What worked

-

### What broke or felt rough

-

### Surface area gaps

- [ ] Did the orphan banner appear if the GUI was reloaded mid-run?
- [ ] Did the sidebar mission browser show the new mission immediately?
- [ ] Did the inspector criteria list update as REFLECT marked things passed?
- [ ] Did the "Copy roadmap.md path" button actually put the path on the clipboard?
- [ ] Did switching to a different session and back lose any state?

---

## Phase 4: Artifacts

After the run completes — even if it didn't converge — capture what the
daemon actually produced.

- **Files created (relative to project root):** _<list, e.g. README.md, package.json, src/launcher.tsx>_
- **Lines of code:** _<git diff --stat output>_
- **Tests added by the agent:** _<count>_
- **Commits made:** _<git log --oneline | wc -l>_

**Quality check on artifacts:**
- [ ] Code runs / compiles (where the host platform allows)
- [ ] Tests (if any) actually exercise the right thing
- [ ] No obvious dead-code / placeholder comments left behind
- [ ] The agent didn't bury the lede in a giant doc file with no actual implementation
- [ ] If it picked a stack, the choice is reasonable for the goal

---

## Phase 5: Post-mortem — what should v0.5.6 do about this?

Fill in after the run. Each row maps an observation to concrete v0.5.6 work.

| Observation | Severity | Proposed v0.5.6 work |
|---|---|---|
| | | |
| | | |
| | | |

---

## Raw artifacts to archive

- Roadmap markdown: `D:\Repos\resonant-linux-bridge\.resonant\roadmap-<intent_id>.md`
- Audit log: `~/.resonant/projects/<hash>/intents/<intent_id>/audit.jsonl`
- Iteration metadata: `~/.resonant/projects/<hash>/intents/<intent_id>/iterations/`
- Diagnostics ZIP (if generated): _<path>_

---

## Final summary

Three sentences max. What did we learn?

1.
2.
3.
