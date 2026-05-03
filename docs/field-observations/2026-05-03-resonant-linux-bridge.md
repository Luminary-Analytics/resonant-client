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

| # | Question (one-line summary) | Quality | Notes |
|---|---|---|---|
| 1 | First concrete artifact / scope of v0.1 | 5 | Recognized vision-vs-feature mismatch; recommended GUI launcher |
| 2 | Where do Wine/Proton recipes live (bundled vs remote)? | 5 | Surfaced legal/operational tradeoff; recommends bundled YAML |
| 3 | Manage Wine runtime internally vs system Wine? | 5 | Recommends managed (`~/.resonant/runtimes/`); mirrors Steam/Lutris |
| 4 | Per-app isolated Wine prefix? | 5 | Recommends yes; named cross-contamination as the #1 failure mode in this space |
| 5 | Where do Windows installers come from? | 5 | URL+SHA256 in recipe; (synthesized my answer into "drop games entirely from v0.1" follow-up) |
| 5b | Confirmation: drop games entirely from v0.1 | 5 | Surfaced new dependency: GPU driver / DXVK / VKD3D as game-only complexity |
| 6 | What makes the interface "Windows-like"? | 5 | Forced specificity on imprecise phrase; recommends native window with Windows UX patterns |
| 7 | Compatibility status: static vs runtime checks? | 5 | Static in-recipe + minimal runtime (installed-or-not); per-machine probing as v0.2 |
| 8 | Failure-mode walkthrough: install fails midway | 5 | Concrete scenario (Notepad++ vcrun fail); modal with summary/diagnostics/retry/report |
| 9 | Install progress UI: bar/spinner+phases/log? | 5 | Spinner with phase labels; live-log behind disclosure |
| 10 | Mid-run crash handling (Notepad++ segfaults 10s in) | 5 | Non-blocking toast + View details + per-launch logs |
| 11 | First-run before Wine downloaded | 5 | Setup wizard, blocking, real % bar; refinement: "skip and browse read-only" |
| 12 | Recipe step schema — flat list vs dependency graph? | 5 | Flat ordered list; typed steps (download/verify-checksum/run-exe/winetricks/copy-file) |
| 13 | Uninstall semantics — what gets removed? | 5 | Full app-id-namespaced removal + confirm dialog with Delete app data checkbox |
| 14 | Concurrent installs vs queue? | 5 | One-at-a-time queue; in-memory only for v0.1 |
| 15 | Distribution mechanism (.deb / Snap / curl…) | 5 | .deb on GitHub releases; Snap/Flatpak/PPA as v0.2+ |
| 16 | Wine runtime tarball source + first-run download failure UX | 5 | Self-hosted GitHub release artifact; clear error + Manual setup fallback |
| 17 | Launcher state storage (SQLite vs JSON)? | 5 | JSON `state.json` for v0.1; schema-versioned, atomic writes; SQLite as v0.2+ |
| 18 | Disk space failure mid-install (ENOSPC) | 5 | Detect → stop → modal with disk/free/needed; AUTO-cleanup of half-prefix |

**Total questions:** _<count>_
**Style:** rigorous (10-25 expected for autonomous) / standard (5-15) / other

### Grill behaviors worth flagging (key research questions for this run)

- [x] **Did the grill recognize the scope?** ✅ Q1 immediately pushed back: "autonomous runs need a tight first milestone" + concrete recommendation
- [x] **Did it surface the choice between launcher / detector / docs / distro as separate slices?** ✅ Q1 listed 4 options and recommended the launcher slice
- [ ] **Did it ask about the host platform?** No — assumed Ubuntu without asking. (Actually NOT a regression — my Q1 answer named Ubuntu, so subsequent questions could anchor)
- [x] **Did it ask about target distros?** ✅ Yes (single distro for v0.1 confirmed in Q1 / Q15 .deb)
- [x] **Did it push back on subjective criteria** like "feel natural" / "simple and reliable"? ✅ Q6 explicitly pinned down "Windows-like" with 3 concrete options
- [x] **Did it ask about scope of "compatibility path detection"?** ✅ Q3, Q7 — surfaced Wine version mgmt + compatibility status authoring concretely
- [x] Were any questions redundant or off-topic? **No.** All 18 questions had distinct, valuable axes. Notably, the model adapted dynamically — Q5b was a synthesis-confirmation rather than a new question, demonstrating active narrowing.

### Additional observations during the grill

**Strong patterns the grill consistently used:**
- Acknowledge the previous answer in 1 line ("Got it. One runtime, ...")
- Bridge to next question with a one-sentence motivation ("Now let's talk about install/launch plumbing — because 'one-click install' is doing a lot of work")
- Frame the question as a list of options (a/b/c/...)
- Recommend ONE option with explicit rationale
- Invite override ("...so I want your call.")

**This is excellent interviewer behavior** — much better than passive enumeration. It ANCHORS the user's thinking on specific tradeoffs rather than open-ended free-association.

**Ollama upstream 503 retries:**
- Between Q5 and Q6 the backend hit "Server overloaded, please retry shortly" and the resonant-client backend transparently retried (logged "retrying in 1.5s" in server output).
- Wall time for that question: 128.6s with no streaming output and no UI indication of the retry.
- Other questions: 8-35s wall.
- ⚠ **Major UX gap:** the GUI shows nothing about the retry. Just keeps incrementing "thinking N s". User has no signal whether the model is generating, stalled, or dropped.

**Concrete v0.5.6+ candidate:** Wire `WARNING:resonant_client.backends:Ollama 503` log lines into a WS event the GUI can render as an inline status: "Backend rate-limited, retrying (attempt 2/4)…"

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
