# Next field-run prep — candidate prompts for the v0.5.7 dogfood

**Drafted:** 2026-05-04 (overnight, while user was asleep)
**Purpose:** queue up a high-signal field run that exercises the v0.5.6+v0.5.7 fixes against fresh real-world load, in a different domain than the linux-bridge run so we surface DIFFERENT findings rather than re-confirming known ones.

---

## What we're trying to learn

The linux-bridge run (2026-05-03) surfaced 12 findings from one ambitious greenfield prompt. v0.5.6 shipped 4 of them, v0.5.7 shipped 6 more. We've now hardened the easy ones AND the high-severity ones. The next run should answer:

1. **Did v0.5.6 + v0.5.7 actually feel better in practice?** Specifically: did the Ollama 503 banner (a1) reduce confusion during cloud retries? Did the spec-validity gate (a2) catch any truncations? Did the in-page picker (a4) and chip collapse (v0.5.7a4) clean up the chat?
2. **What surfaces in a DIFFERENT domain?** Linux-bridge was Tauri+Svelte+Rust+system-integration. The next run should be narrow-scope and bash-validatable so iterations cycle faster — that surfaces different failure modes (per-iter UX, REFLECT+planner cadence, cost-tracking) rather than the once-per-run scaffolding modes.
3. **Are deferred findings #7 (chat virtualization) and #10 (path-mismatch decision event) actually needed, or did v0.5.7 reduce their severity enough that we can skip them?**

---

## Recommendation: "mdcheck" — parallel markdown link checker

A small CLI utility that walks a directory, finds all markdown files, extracts every link, and validates them in parallel via HTTP HEAD with retry/timeout/depth options. Concrete I/O, easy [bash] criteria, plenty of natural grilling points (concurrency model, retry policy, output format, exclusion patterns).

### Why this prompt

- **Different domain.** No GUI, no Wine, no Tauri — pure CLI, network, and concurrency. Surfaces a different REFLECT pattern and planner cadence than the linux-bridge run.
- **Bash-validatable end-to-end.** Every acceptance criterion can be `[bash]`-pinned with a fixture markdown file + expected JSON output. No browser needed.
- **Narrow scope = fast iters.** Ambition cap: ~500 LoC, single CLI, no UI. The autonomous loop should cycle 5-10 iters per hour, generating per-iter UX feedback (the chat-message virtualization concern from finding #7).
- **Has natural concurrency / failure-mode questions.** Grills well — questions about thread pool size, retry backoff, what counts as "broken" (404 vs timeout vs DNS), how to handle redirects, etc.
- **Real, reusable artifact.** If it converges, you actually have a useful tool at the end. Same property as linux-bridge — the dogfood produces real value, not just test data.
- **Small enough to run in a 2-3h budget**, vs linux-bridge's 4h+ that was always going to hit the cap.

### Suggested workspace

`D:\Repos\resonant-mdcheck\` — fresh greenfield directory.

### The prompt to paste into the Mission composer

```
We are building "mdcheck" — a CLI tool for finding broken links in
markdown files. It should:

1. Take one or more directory paths as input (defaults to current
   directory if none given)
2. Recursively find all `.md` files
3. Parse each file for markdown link syntax: `[text](url)` and bare
   `<url>` links
4. For each external link (http/https), validate it via HTTP HEAD
   with a configurable timeout
5. For each relative link (file paths), validate the target file
   exists
6. Run external HTTP checks in parallel with a bounded worker pool
7. Print a summary: total files, total links, broken links by file
   with line numbers
8. Exit non-zero if any broken links found

Stack: pure Python 3.11+, stdlib + httpx for HTTP. CLI via argparse.
JSON output mode for CI integration. Reasonable defaults: 20 parallel
workers, 5s timeout, retry once on transient failures (5xx, timeouts).
Project goes at D:\Repos\resonant-mdcheck. Greenfield — no existing
code to integrate with.
```

### What to watch for during the run

- **Ollama 503 banner (v0.5.6a1).** Does it actually appear on retry-rate spikes? Does it fade smoothly? Is the message readable mid-iteration?
- **Spec validity gate (v0.5.6a2).** Did the model emit a complete `## Final spec`, or was there any truncation? If truncated, did the dispatch button correctly become "Spec incomplete"?
- **Stuck-state atomicity (v0.5.6a3).** If the daemon hits stuck/blocked, do all three state holders (roadmap.md, session.mission_state, GUI badge) converge?
- **Picker fallback (v0.5.6a4).** Try Shift-clicking the project header — does the modal text input open? Does Enter submit cleanly?
- **default_model honored (v0.5.7a1).** When you switch projects (or start fresh), is the configured `deepseek-v4-pro:cloud` (or whatever) actually selected, not flash by default?
- **iter counter clarity (v0.5.7a2).** During a long iter, does the header show `iter N (running)` and the inspector show `iter N-1 completed`? Does the visual mismatch still bother you, or did the disambiguation help?
- **Leading-dash guard (v0.5.7a3).** Hopefully you DON'T see this fire — it means the agent stayed clean. If it DOES fire, the error message should be actionable.
- **Dispatch chip (v0.5.7a4).** After clicking Build autonomously, does the card collapse to a one-line chip? Does the Stop button on the chip work?
- **Grill exemplar quality (v0.5.7a5).** Are the questions still 5/5? Does the model actually follow the 5-beat pattern, or has codifying it caused the model to mechanically format without substance?
- **REFLECT failure annotation (v0.5.7a5).** When a `[bash]` criterion fails, does REFLECT now annotate the criterion line with the diagnosis? Linux-bridge run was already doing this — did codifying it preserve or improve the behavior?
- **Long-run renderer state (deferred finding #7).** If the chat accumulates 100+ messages, does the renderer stay responsive? If it does, finding #7 might not be worth the virtualization refactor.
- **Path-mismatch handling (deferred finding #10).** If REFLECT hits a path-mismatch (e.g. spec says `output/` but agent puts files in `out/`), does the daemon get stuck the same way it did in linux-bridge? Or did the v0.5.7a5 REFLECT exemplar guidance help?

### Pre-run checklist

- [ ] Mac Studio (10.0.0.133) reachable, Ollama serving `deepseek-v4-pro:cloud`
- [ ] `D:\Repos\resonant-mdcheck\` does NOT exist (autonomous mission creates it)
- [ ] resonant-client running locally (NOT on Mac Studio — the GUI runs Windows-side, just hits Mac Studio for inference)
- [ ] settings.json has `"default_model": "deepseek-v4-pro:cloud"` (verifies a1 fix in flight)
- [ ] Take a screenshot of the chat panel BEFORE dispatching for visual baseline
- [ ] Set time budget to **2h** (mdcheck scope is much smaller than linux-bridge — don't burn cloud tokens on a too-generous cap)

### Workflow once dispatched

1. Watch the grill. Score each question 1-5 (use the v0.5.7a5 5-beat pattern as the rubric).
2. When the spec lands, save it to `docs/field-observations/2026-05-04-resonant-mdcheck.spec.md` BEFORE clicking Build.
3. Click Build autonomously. Confirm the card collapses to a chip.
4. Check the autonomous badge — does it show `iter N (running)` mid-iter?
5. Let the loop run. Take notes whenever something surprises you (good or bad).
6. After it stops (satisfied/stuck/blocked/cap), copy this template into `docs/field-observations/2026-05-04-resonant-mdcheck.md` and fill it in.

---

## Alternative prompts if mdcheck doesn't appeal

### Alternative A — "envcheck" — a project environment doctor

CLI tool that audits a project directory for common config issues: missing env vars referenced in code, untracked .env files in .gitignore, mismatched Python version between pyproject.toml and .python-version, etc. Pure read-only stdlib walk + AST parse. Even smaller scope (~300 LoC). Good for a 1h budget. Same domain rationale.

### Alternative B — "diffstats" — a git-aware diff statistics tool

Run against any git repo and produce a JSON/markdown report: changes per file by author over a date range, hottest files (most-churned), largest blast-radius commits. Wraps `git log --numstat` parsing. ~400 LoC. Has natural date-range / filter grilling points and produces concrete output you can verify.

### Alternative C — Re-run the linux-bridge prompt with v0.5.7

Worth doing at SOME point so we can directly A/B the v0.5.6 vs v0.5.7 experience on the same prompt. But probably not first — same scope means same iteration count and the per-iter UX deltas are what we care about. Save this for after a different-domain run.

---

## Why I didn't kick this off overnight

The user was asleep when I got to this todo. Running an autonomous mission against the Mac Studio overnight would burn 2-3h of cloud-model spend without their realtime feedback on the live observations (which is most of the value of a field run — the post-mortem is just half the picture). Better to leave it staged + ready to dispatch, so they can kick it off whenever they're ready in the morning and watch it live.

If `D:\Repos\resonant-mdcheck\` exists in the morning, that's a sign the user already started. Otherwise this brief is the dispatcher.
