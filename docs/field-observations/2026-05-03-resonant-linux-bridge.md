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

### Final spec (after the truncation-then-continue retry)

> ⚠ **Major v0.5.6 finding:** the model's first attempt at the spec was TRUNCATED mid-sentence after the In-scope bullets — no Acceptance criteria, Out of scope, Time budget, Technical constraints, or Open risks. **The autonomous-mission dispatch card rendered anyway**, with budget options + Build button enabled — clicking it would have hit `extract_spec()` and failed with a parser error. I had to manually prompt "please continue and complete the spec" for the model to fill in the remaining sections. Backend-side this needs a "spec validity gate" before exposing the dispatch card.

After the manual continue, the full spec is comprehensive and high-quality:

- 4 key assumptions (named the Windows-dev-host Wine-untestable constraint explicitly)
- 27 in-scope bullets
- 9 explicit out-of-scope items (games, cross-distro, remote recipes, Snap/Flatpak, etc.)
- Time budget: 4h
- 10 technical constraints (Tauri v2 + Rust backend + Svelte stores, atomic file writes, etc.)
- 6 `[bash]` acceptance criteria covering: `cargo build`, `cargo test`, `npm run build`, `cargo run --help` panic-free, 10 recipe YAML files exist + parse, every recipe satisfies the schema (required fields + valid compatibility + valid step types)
- 6 open risks named

Full spec persisted alongside this file as `2026-05-03-resonant-linux-bridge.spec.md`.

**First-pass assessment:**
- [x] Acceptance criteria are concrete and testable (≥3 [bash]) — **6 [bash] criteria**, all binary
- [x] Time budget is reasonable for the scope — **4h** matches the user's ask
- [x] In-scope / out-of-scope cleanly separated — explicit 9-item out-of-scope list
- [x] Refined intent matches what was actually wanted — **launcher-only v0.1 slice**, not the full distro vision
- [x] **Specifically: defensible v0.1 slice** — Tauri+Svelte launcher with one canonical recipe + 9 stubs; games / cross-distro / distro itself all out

---

## Phase 2: Autonomous loop

Track each iteration here. Sidebar inspector should mirror this in real time
once the daemon starts — watch how the two diverge if at all.

### Iter 1 — in flight (capturing as it runs)

**Dispatched:** 2026-05-03T20:29:37Z (4h budget, iteration cap 100)
**Plan-graph after PLAN_DEEP:** 5 IMPLEMENT subgoals (1 RUNNING, 4 PENDING)
- Scaffold the Tauri v2 + Svelte (RUNNING)
- 6ddec57ee9ca / 98c6d2f3bc78 / ae97cf181de9 / ef9b800dab15 (PENDING — hash IDs only, no titles visible in plan-graph; they'll resolve once they pick up)

**Real-time observations during iter 1:**

What the agent has DONE so far (visible in the chat trace + on disk):
1. Searched codebase → confirmed empty greenfield
2. Ran `git_status` → clean
3. Wrote `README.md` (86 lines)
4. Created `index.html`, `package.json`, `svelte.config.js`, `vite.config.js`, `src/main.js`, `src/App.svelte` (90 lines), `src/styles.css`
5. Created `src-tauri/` Tauri Rust project: `Cargo.toml`, `build.rs`, `src/main.rs`, `src/lib.rs`, `tauri.conf.json`, `gen/`, `icons/`
6. Vendored `vendor/winetricks` (the bash script we agreed to bundle in Q22)
7. Ran `npm install` → exit 0 (8.9s)
8. Ran `cargo check` in src-tauri/ → **exit 101 (73.6s)** — Rust compilation failure

**Bug it found in its own scaffold (mid-iter):** `tauri.conf.json` had `title` at top-level, but Tauri v2 wants it per-window. Model identified this from the cargo error output and is iterating on the fix.

**Bug I spotted that the model also has:** `src-tauri/src/main.rs` calls `resonant_linux_bridge_lib::run()` but the package is named `resonant-linux-bridge` (crate name `resonant_linux_bridge` — no `_lib` suffix). Either Cargo.toml needs `[lib] name = "resonant_linux_bridge_lib"` or main.rs needs to drop the `_lib`. We'll see if model catches both.

**Stray artifact:** A file literally named `-p` exists at the project root — almost certainly a `mkdir -p src` that got tokenized as `mkdir`, `-p`, `src` somewhere; the agent created the file `-p`. Worth noting as a v0.5.6 sanity check ("if a tool creates a file whose name starts with `-`, flag it").

**Iteration table:**

| Iter | Item picked | Duration | Outcome | Verdict | Notes |
|---|---|---|---|---|---|
| 1 | T1.1 (full launcher slice as one item) | **44m** | shipped | continue | Tauri+Svelte scaffold + 10 recipes + Rust modules. Self-corrected: `tauri.conf.json` title placement, `_lib` suffix mismatch, stray `-p` file. **3/6 criteria green** (cargo build/test/--help). **2/6 FAIL**: `npm run build --prefix src/` (path wrong — package.json at root), recipes-count check (recipes at `src-tauri/recipes/` not root `recipes/`). 1 criterion (schema validator) not yet validated. T1.1 commit: `224f43b32d`. |
| 2 | _in flight_ | | | | |
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

Mission stopped manually after the daemon hit `verdict=stuck` mid-iter-2.
Despite "stuck", the agent built ~3,900 lines of substantive Rust + Svelte
in ~2h wall time:
- 5 Rust modules (`lib.rs` 313, `pipeline.rs` 889, `recipe.rs` 412, `state.rs` 347, `wine_runtime.rs` 292 = 2,253 lines)
- 11 Svelte components (1,638 lines: AppCatalog, AppDetail, AppTile, ContextMenu, FailureModal, IconAvatar, InstallModal, LaunchLogs, SetupWizard, ToastContainer, UninstallDialog)
- 10 recipe YAMLs (real catalog: 7-zip, audacity, everything-search, foxit-reader, irfanview, keepass, mpc-hc, notepad-plus-plus, putty, winmerge)
- Vendored winetricks
- 3/6 acceptance criteria green (cargo build, cargo test, cargo run --help)

The 2 FAIL criteria were both **path-mismatch artifacts**, not code bugs:
- `npm run build --prefix src/` — but `package.json` is at root (correct Vite/Tauri convention)
- `recipes/` at root — but agent put them at `src-tauri/recipes/` (correct embedded-resources convention)

The 6th criterion (schema validator) never ran because it depends on the recipes-exist check.

| # | Observation | Severity | Proposed v0.5.6 work |
|---|---|---|---|
| 1 | **Upstream Ollama 503 retries are invisible in the GUI.** Backend retries transparently; user sees `thinking N s` counter climbing with no signal whether stalled or in retry. | high | Wire `WARNING:resonant_client.backends:Ollama 503...retrying` log lines into a `status_msg` WS event surfaced as inline status: "Backend rate-limited, retrying (attempt 2/4)…". |
| 2 | **Spec generation truncated mid-sentence**, no Acceptance criteria emitted, but the autonomous-mission dispatch card rendered anyway with Build button enabled — clicking would have hit `extract_spec()` with a ValueError. | high | Backend should gate the dispatch-card emission on a spec-validity check (presence of `## Final spec` block + ≥1 typed acceptance criterion). If the spec is incomplete, render a "Spec incomplete — ask the model to continue" banner instead of the Build card. |
| 3 | **Project-switch dead end in browser mode.** "Open another project" triggers a native folder picker that hangs; status message points users to a workspace-folder-input field that's only visible on the welcome screen. | medium | Add an in-page text-input fallback for `set_project` that's accessible from the dropdown. |
| 4 | **Defaults flip is first-launch-only.** New project switched to flash, not the v0.5.2 pro default. | low | Apply the configured `default_model` on every project switch, not only on first launch. |
| 5 | **Iter counter desync.** Chat-header autonomous badge counts live (in-flight) iter; sidebar inspector counts COMPLETED iters via the iteration_log. Both correct but visually disagree. | low | Either (a) align both UIs to the same metric, or (b) label the badge "iter N (in flight)" vs the inspector's "iter N completed" so the relationship is explicit. |
| 6 | **State desync after `verdict=stuck`.** Frontend `app._autonomousState.lastVerdict = "stuck"` but session.mission_state.phase still `autonomous_running` AND roadmap.md `**Status:** running`. The autonomous mission badge disappeared from the GUI but session record + roadmap weren't updated. | high | When daemon reaches `stuck`, ensure the daemon's `stop()` path commits the phase transition + roadmap status atomically before clearing the GUI badge. The GUI vs disk vs JS-state divergence here is exactly the "is the mission alive?" confusion users will hit. |
| 7 | **Long-running mission pins the renderer.** After ~2h of accumulated chat content (388 messages) Chrome's `Page.captureScreenshot` started timing out. The DOM is too large. | medium | Virtualize the chat-message list past N messages, or fold older iterations into a collapsed "Iter N transcript" group. |
| 8 | **Tool-call argument tokenization can create stray files.** Mid-iter, a file literally named `-p` appeared at the project root (almost certainly from a `mkdir -p src` call where the agent tokenized `mkdir`, `-p`, `src` as three separate args to the wrong tool). Agent self-cleaned later. | low | Validate file_write / shell tool args: reject filenames starting with `-` unless explicitly escaped. |
| 9 | **REFLECT correctly diagnosed criteria failures with annotation.** When npm build failed, REFLECT updated the criterion line in roadmap.md with the actual Vite error ("semicolon-prefixed path resolution bug"). When recipes-count failed, it noted "actual recipes live at src-tauri/recipes/". This is excellent behavior — surfaces the diagnosis, doesn't just show pass/fail. | n/a (positive) | Document this as the gold-standard REFLECT pattern; verify the daemon doesn't regress here. |
| 10 | **Path-mismatch criteria deadlock.** When the spec's `[bash]` criterion has the wrong path (e.g. `recipes/` vs `src-tauri/recipes/`), REFLECT can flip the criterion to `[FAIL]` but cannot autonomously decide whether to (a) move the file to match the criterion or (b) edit the criterion to match the file. The daemon goes "stuck" rather than picking. | medium | For path-only mismatches where REFLECT detects the file IS present at a different location, surface a structured `human-decision-required` event with explicit options: "move file to criterion-path / update criterion to actual path / both / neither". |
| 11 | **The dispatch card stays visible after click.** After clicking "Build autonomously" the card persists in the chat, with the button just relabeled "Daemon dispatched" (greyed). It's not a real problem but it's visual clutter for the rest of the run. | low | Collapse the dispatch card to a one-line "✓ Mission dispatched at HH:MM:SS · Stop" affordance once the daemon is live. |
| 12 | **Grill quality is exceptional and consistent.** All 27 questions rated 5/5. Pattern: acknowledge previous answer (1 line) → bridge with motivation → frame as a/b/c options → recommend with rationale → invite override. Active scope-narrowing emerged at Q5b (synthesis-confirmation rather than a new question). | n/a (positive) | Codify this pattern into the rigorous-grill prompt's R1+ as the EXEMPLAR — the addendum's "demand behavior-testing criteria" rule already implies it but doesn't show. |

---

### Net assessment

This was a stress test of every part of the system on an ambitious-greenfield prompt and **the system held up well**. The grill produced a publishable spec. The autonomous loop scaffolded a real Tauri+Svelte launcher with a substantial Rust install pipeline + 11 React-quality Svelte components + 10 curated recipes — in 2 hours, on a host where it couldn't even run the resulting binary. The "stuck" verdict was the result of REFLECT honestly reporting two path-mismatches between the spec author's expectations and the agent's reasonable structural choices, not any kind of code regression. v0.5.6 backlog has 12 concrete items ranked by severity.

---

## Raw artifacts to archive

- Roadmap markdown: `D:\Repos\resonant-linux-bridge\.resonant\roadmap-<intent_id>.md`
- Audit log: `~/.resonant/projects/<hash>/intents/<intent_id>/audit.jsonl`
- Iteration metadata: `~/.resonant/projects/<hash>/intents/<intent_id>/iterations/`
- Diagnostics ZIP (if generated): _<path>_

---

## Final summary

Three sentences max. What did we learn?

1. **The grill is excellent.** 27 questions, all 5/5 quality, with a consistent acknowledge-bridge-options-recommend-invite-override pattern that aggressively narrowed scope from "open-source Linux distro" to "Tauri+Svelte launcher for Ubuntu" in the first question. The grill alone justifies the autonomous-mission feature.
2. **The autonomous loop produces real implementation under stress** — ~3,900 lines of substantive code (5 Rust modules with a 889-line install pipeline, 11 production-quality Svelte components, 10 curated recipes) in ~2h of wall time on a Windows host that couldn't even execute the resulting binary; despite hitting `verdict=stuck`, the work shipped is genuinely useful and resumable.
3. **Three high-severity v0.5.6 fixes emerged** that have nothing to do with model quality and everything to do with state management: (a) Ollama 503 retries are invisible, (b) the dispatch card renders before the spec is parseable, (c) when the daemon reaches `stuck`, the GUI/session/roadmap state diverges. All three are concrete, narrow, and pure backend work.
