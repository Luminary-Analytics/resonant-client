# Session handoff — 2026-05-04

**Purpose:** load this at the start of a fresh session to inherit the working context + the autonomous-iteration pattern that made this session productive. This doc is BOTH a state-of-the-product summary AND a how-to-work guide.

If you're a future Claude: read all of this before doing anything. The bottom section ("How to work in the next session") is the operating manual.

If you're Rich: this is your "what happened" recap + a contract for how the next session should behave.

---

## Where we are

**Current version:** `v0.5.9` (GA tag local; not pushed)
**Test count:** **1790 passed, 2 skipped** (was 1538 at session start)
**Net new tests this session:** +252

Local commits ahead of origin (last push was somewhere mid-v0.5.7):

```
b3252cc v0.5.9 GA
d54c0c3 v0.5.9a5  diagnostics ZIP enrichment
71872aa v0.5.9a4  pause-after-current-iter
b9d4f15 v0.5.9a3  verdict-override provenance
9923116 v0.5.9a2  per-iter cost + model attribution
1e6f882 v0.5.9a1  live daemon activity inspector
5ae4610 v0.5.8 GA
5e2e8da v0.5.8a4  smoke spec library expansion
e637928 v0.5.8a3  chat virtualization
64da976 v0.5.8a2  human-in-the-loop on stuck verdict
0c47468 v0.5.8a1  per-specialist Ollama model routing
1076ebd docs: prep brief for next field run (mdcheck)
ecc4819 v0.5.7 GA
```

Tags created locally (none pushed): `v0.5.7a1..a5 + v0.5.7`, `v0.5.8a1..a4 + v0.5.8`, `v0.5.9a1..a5 + v0.5.9`.

**Push state:** Rich's preference is to review before pushing. Don't push unless explicitly told to.

---

## Session arc — what happened

This session ran in five distinct phases. Capturing the arc because the WAY we worked matters as much as what shipped.

### Phase 1: linux-bridge field run (the data source)

We ran resonant-client autonomously against a hard greenfield prompt: a Linux launcher app (Tauri + Svelte + Rust) that the host couldn't even execute. The mission produced ~3,900 lines of working code in 2h, hit `verdict=stuck` honestly on a path-mismatch, and surfaced **12 concrete findings** ranked by severity. Logged in `docs/field-observations/2026-05-03-resonant-linux-bridge.md`.

The 27 grill questions all rated 5/5. The autonomous loop produced real implementation under stress. The "stuck" was an honest report, not a code regression. The findings were all about state management, UX, and observability — none about model quality.

### Phase 2: v0.5.6 — high-severity findings (a1-a4)

Closed the 4 highest-severity findings:
- a1: Ollama 503 retries surfaced as `backend.status` events (was invisible)
- a2: Spec-validity gate prevents truncated specs from crashing dispatch
- a3: Atomic terminal-state transition across roadmap.md / session.mission_state / GUI badge (closed `verdict=stuck` desync)
- a4: In-page text input fallback when native folder picker hangs (browser-mode)

### Phase 3: v0.5.7 — backlog burn-down (a1-a5)

Closed the remaining 6 lower-severity findings + 2 doc items:
- a1: `default_model` honored on every project switch
- a2: `iter N (running)` vs `iter N completed` disambiguation
- a3: Reject `file_write`/`file_edit` paths starting with `-` (foot-gun guard)
- a4: Dispatch card collapses to a one-line chip after click
- a5: Codified grill 5-beat exemplar + REFLECT failure-annotation pattern in prompts

10 of 12 findings shipped. #7 (chat virtualization) and #10 (path-mismatch decision event) deferred — both wanted design work + a fresh field run.

### Phase 4: v0.5.7 → v0.5.8 strategic conversation

Rich asked for a state assessment + frontier comparison. Honest answer: harness architecture is at/near frontier on several dimensions; DeepSeek V4 itself is the bottleneck. Roughly 70-80% of frontier; missing 20-30% is mostly model capability + iteration latency.

Identified 5 priorities (P0-P5) by leverage. Rich said "tackle in order." Two scoping caveats I raised:
- **P0 (field-validate)** isn't a "me" task — needs Rich in the loop. Prepped the dispatch (`docs/field-observations/2026-05-04-NEXT-RUN-PREP.md`) but didn't run it.
- **P1 (model routing)** had a positioning constraint. v0.4.0 explicitly cut Anthropic/OpenAI to be Ollama-native. Re-adding them is a strategic shift, not a feature. Reinterpreted P1 as **per-specialist Ollama model routing** (pin pro for REFLECT/PLAN_DEEP, flash for IMPLEMENT/EXPLORE) — captures the benefit without breaking positioning.

### Phase 5: v0.5.8 — frontier-gap closures (a1-a4) + v0.5.9 — QoL/observability (a1-a5)

v0.5.8:
- a1: Per-specialist Ollama model routing
- a2: Human-in-the-loop on stuck verdict (closed deferred finding #10)
- a3: Chat virtualization via iter folding (closed deferred finding #7)
- a4: Smoke spec library expansion (jsonlines + refactor-py + seed_files mechanism)

**Linux-bridge backlog: 12/12 complete after v0.5.8.**

v0.5.9 (no field-finding driving it; pure QoL/observability):
- a1: Live daemon activity inspector ("is it stuck or just slow?")
- a2: Per-iter cost + model attribution on iter cards (visualizes a1)
- a3: Verdict-override provenance on reflection cards
- a4: Pause-after-current-iter (graceful counterpart to Stop)
- a5: Diagnostics ZIP enrichment

---

## Capability map (what the product does today)

Eight major capability tracks:

1. **Engine + agentic loop** — Ollama-native session runner. Tool palette: file ops, bash, glob/grep, browser (Playwright), MCP, RAG codebase index, hooks, diff review, sandbox.
2. **Discovery → Mission seam (rigorous grill)** — structured interview producing typed acceptance criteria + time budget. 5-beat exemplar codified.
3. **Autonomous Mission daemon** — roadmap-driven outer loop. Picks unchecked items → dispatches as Phase-1 sub-missions → REFLECT every K iters → 7 priority-ordered stop rules. Resume + orphan detection. Atomic terminal-state transitions.
4. **REFLECT specialist** — deterministic `[bash]/[vision]` pre-pass + model-driven `[chrome]` validation + structured JSON verdict. Failure-annotation pattern codified. Decision-request schema for human-in-the-loop forks.
5. **Per-specialist routing** — pin different Ollama models per `NodeSpecialization` (settings or env-var).
6. **Smoke harness** — `resonant-smoke run/variance/baseline/ci`. 5 specs (3 validated, 2 unvalidated). Seed-files mechanism for refactor-style specs.
7. **GUI** — chat + plan-graph + inspector + harness UI + diff review + image attachments + cost tracker + permission modes + mission browser + orphan banner + decision card + iter folding + activity panel.
8. **Diagnostics + cost tracking** — full-fidelity ZIP bundle with redaction, per-iter cost attribution + model split, daily cost tracking with budget alerts.

---

## What's actually deferred for the next session

These are the genuine open items. **Don't do them speculatively** — read the section "How to work in the next session" first.

| Priority | Item | Why deferred |
|---|---|---|
| P0 | Field-validate v0.5.7+v0.5.8+v0.5.9 by running mdcheck or another greenfield prompt | Inherently needs Rich in the loop; field-run value comes from live observation |
| P1 | Validate the unvalidated smoke specs (`jsonlines`, `refactor-py`) on Mac Studio | Needs Mac Studio reachable; Rich's call when to run |
| P2 | Promote per-specialist routing to defaults (pro for REFLECT/PLAN_DEEP) | Wait until P0 confirms it actually helps live |
| P3 | Parallel sub-mission dispatch | Risky architectural change; explicitly waits on P0 signal — speculatively doing it without that signal is bad |
| P4 | Push the v0.5.7+v0.5.8+v0.5.9 commits + tags | Rich's review-before-push preference; needs explicit go-ahead |

**Field-run prep doc:** `docs/field-observations/2026-05-04-NEXT-RUN-PREP.md` — ready to dispatch, has the verbatim prompt, watchlist mapping every fix to a specific in-flight observation, and a pre-run checklist. Rich's one-click start.

---

## How to work in the next session

This is the contract. The session you're loading from did 23 alphas + 4 GAs across 4 minor versions in roughly 24 hours of intermittent work. That throughput came from a specific operating pattern.

### The autonomous-iterate-then-block pattern

**Default behavior: keep moving.** When Rich says "tackle these," "keep going," or anything similar, the expectation is that you work autonomously through the planned items without asking permission for each one. You commit but don't push (unless told to). You run the full test suite after each alpha. You move to the next alpha when the current one is green.

**The cycle:**

1. **Pick the next item** from the prioritized backlog (or the explicit todo list)
2. **Investigate** — read the relevant code, understand the failure mode or feature shape
3. **Implement** — small focused changes; one alpha = one finding or one capability
4. **Test** — write tests that actually exercise the new behavior; not just unit tests of helpers
5. **Verify** — run the new tests, then run the full pytest suite, confirm green
6. **Bump version** — `__init__.py` and `pyproject.toml`, e.g. `0.5.10a1`
7. **Commit** — HEREDOC commit message with WHAT and WHY; co-author footer; tag
8. **Update todos** — mark done, mark next as in_progress
9. **Loop** — go to step 1

**Self-review checkpoints:**

After every 2-3 alphas, ask yourself:
- Am I still on the highest-leverage item? Did something get deferred that's now more urgent?
- Are the tests actually exercising the behavior I changed, or just shape-matching?
- Did I introduce technical debt by skipping something? Worth flagging.
- Is the running test count growing in a healthy way (each alpha adds tests proportional to the change)?

**End-of-batch review:** before tagging GA for the minor version, write the release notes. The discipline of writing them surfaces gaps — if you can't articulate WHY a change matters, it probably doesn't.

### When to stop and ask Rich

A "blocker" is a place where Rich's input genuinely improves the outcome. Stop when you hit one of these:

| Blocker class | Example |
|---|---|
| **Strategic positioning** | "Should we re-add Anthropic backends?" — that's a product decision, not a feature |
| **Multiple valid architectures** | "Should the cost data attach to iter_complete or fire as a new event?" — flag the trade-off, propose, ask |
| **Infra access required** | "Need Mac Studio for live validation" — Rich's call when to run |
| **Destructive ops without explicit approval** | force-push, push to main, deleting tags, --no-verify |
| **UX/copy decisions with no obvious right answer** | "Should the Pause button copy say 'Pause' or 'Stop after this iter'?" — sometimes you just have to pick, but if there's no signal, ask |
| **Genuine ambiguity in the prompt** | If Rich's instruction has two reasonable interpretations and they lead to materially different work, ask |

### What is NOT a blocker

Don't stop to ask about:
- Implementation details with one obvious right answer
- Test failures during implementation — fix and continue
- Edge cases you discover mid-implementation — handle them
- Refactor choices that preserve behavior
- Whether to write tests for the new code (yes, always)
- Whether to commit each alpha individually (yes, always)
- Whether to push (no, never, unless explicitly told)

### How to handle scope changes mid-session

Rich sometimes says things like "tackle these in order" then mid-flight says "also do X." Two cases:

**X is a small detour:** integrate it into the current alpha if it fits, or insert as the next alpha if it warrants its own.

**X is a strategic shift:** stop the current sequence at a clean boundary (finish the current alpha, commit, tag), then either pivot to X or ask if the original sequence is still the priority.

The signal: does X imply the original priority list was wrong? If yes, surface that.

### Things that worked this session that you should keep doing

- **Honest scope-down on P1.** When asked to "re-add Anthropic backends," I instead reinterpreted as per-specialist Ollama routing because v0.4.0 explicitly cut those backends. Pushed back, proposed alternative, got go-ahead.
- **TodoWrite for every alpha sequence.** Visible progress; Rich could see what was happening when checking in.
- **Tests that exercise the behavior.** When I added per-iter cost tracking, I wrote tests for the lifecycle including thread-safety stress test. Not "import the module and check it has the function."
- **Release notes per minor version.** Not just changelog — real "why this matters" prose. Surfaces gaps in your own thinking.
- **Self-flagging blockers.** Multiple times this session I said "this is something you (Rich) need to decide" or "this needs your hands, not mine." Don't do work that requires the user when the user isn't there.
- **Version bumping discipline.** Every alpha gets a tag. Makes git log readable; makes rollback trivial.
- **Commit messages with WHY not just WHAT.** "Closes linux-bridge field-observation #10" is more useful than "add decision_request handling."
- **HEREDOC commit messages with co-author footer.** Consistent format across the session.

### Things that didn't work / would do differently

- **Push state confusion.** At one point origin had advanced past where I thought (commits got pushed somewhere outside my view). Always trust `git ls-remote` over `git status`'s "ahead by N" message.
- **The first attempt at the constructor-failure test in v0.5.8a1** patched `OllamaBackend` in a way that broke `isinstance()` later in the same code path. Lesson: when patching a class, think about ALL uses of that class in the function under test, not just the one you're trying to fail.
- **CRLF/LF warnings on every commit.** Cosmetic but noisy. Could set up `.gitattributes` to suppress.

---

## Conventions to inherit

### Versioning

- Alpha pre-releases: `0.5.9a1`, `0.5.9a2`, ... `0.5.9a5`
- GA: `0.5.9` (drop the alpha suffix)
- Tag every alpha + GA. Annotated tags with descriptive messages.
- Bump `resonant_client/__init__.py::__version__` AND `pyproject.toml::version` together.

### Commit messages

- Use HEREDOC for multi-line. Single-quoted EOF (`<<'EOF'`) so `$` and backticks don't expand.
- Title line: `vX.Y.Za_: <one-line summary>` for alphas, `vX.Y.Z GA — <theme>` for GA.
- Body: explain the WHY (closes finding #N, addresses gap from v0.5.5, etc.) before the WHAT.
- Test count delta in the body: `Full suite: 1773 passed, 2 skipped (was 1759/2 in v0.5.9a1).`
- Co-author footer: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

### Release notes (per minor version)

- File: `docs/v0.5.X-release-notes.md`
- Sections: Date / Predecessor / Theme / TL;DR table / one section per alpha / Validation table / Carry-over
- TL;DR table: one-liner per alpha, hits the "what" fast
- Per-alpha sections: cover architecture decisions, test coverage, defensive contracts
- Validation table: pytest pass count, JS check, Python AST check, any CLI smoke
- Carry-over: what's still open

### Tests

- Every behavioral change gets a test that would FAIL without the change
- Use `pytest.fixture` for shared setup, parameterize when appropriate
- Thread-safety where applicable (the iter cost tracker has a 10-thread stress test)
- Negative cases: malformed inputs, missing fields, edge cases
- Defensive contracts: when a function says "no-op on bad input," test that it actually no-ops

### Tool calls in this codebase

- `LocalSpecialistRunner` is the engine boundary; instrument there for cross-cutting features
- `AutonomousMissionDaemon` owns the iter loop; instrument there for daemon-level features
- `_make_autonomous_event_forwarder` (in app.py) is where daemon events meet the WS; instrument there for GUI-side enrichments
- The chat-stream `status` event handler in app.py is where token counts flow; tap there for cost-style features

---

## Suggested first move for next session

Based on the deferred list, the highest-leverage moves are (in order):

1. **Decide push state.** "Push v0.5.7+v0.5.8+v0.5.9 to origin?" One-line confirmation from Rich, then the next session can push or hold.

2. **Field run.** Rich kicks off mdcheck (or alternative). Watch live, take notes on the watchlist in `2026-05-04-NEXT-RUN-PREP.md`. After it finishes, post-mortem against the watchlist. This produces the v0.5.10 backlog.

3. **THEN** — based on what the field run reveals — decide whether v0.5.10 focuses on:
   - **Promoting routing defaults** (if a1 helped live)
   - **Parallel sub-missions** (if model is fine and iter latency is the ceiling)
   - **A new generation of findings** (if the field run surfaces things v0.5.6-v0.5.9 didn't anticipate)

If Rich isn't around to make these calls, default to: **don't speculatively start v0.5.10**. The whole architecture-vs-model question hinges on the field-run signal. Use the wait time to push code review, write deeper tests for under-covered modules, or revise documentation — but don't ship a v0.5.10 alpha without signal.

---

## Closing note for the next Claude

This session worked because Rich gave long autonomous stretches and I used them well. The pattern was:

> **Default to forward motion. Stop only at genuine forks.**

You're going to want to ask permission for things you don't need permission for. Resist that. The user trusted the previous session with hours of unattended work; honor that by staying productive when they step away.

But also: when you DO hit a real blocker — strategic, infra-bound, ambiguous — stop fast and surface it cleanly. A 15-minute "should I do A or B?" exchange beats a 3-hour wrong direction.

The goal isn't autonomy for its own sake. It's that Rich's hands and time are scarce; yours aren't. Your job is to make every hour Rich is in front of the screen high-leverage.

**Test count when you started:** 1790
**Test count after your work:** I expect it to be higher.

Have fun. Don't push without permission.
