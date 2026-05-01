# Long-Running Agents — 4-Phase Plan

## Vision

A user types a feature idea. The system grills them with questions until shared
understanding lands, builds a roadmap, hands the work off to a fleet of
sub-agents that execute in parallel, reviews the deliverables against the
spec, and loops on revisions until done — autonomously running for hours,
days, or a week at a time.

Inspired by [mattpocock/skills/grill-me](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md).

---

## What we already have

Resonant has ~70% of the primitives. The architectural map (sourced from
`tests/test_intent_e2e.py` end-to-end + the orchestration module):

| Need | Existing primitive |
|---|---|
| Discovery / interview | (gap — Phase 1 fills) |
| Roadmap data structure | `orchestration/plan_graph.py` — DAG of `PlanNode`s with `NodeStatus` and `NodeSpecialization` |
| Plan execution | `orchestration/walker.py` — `GraphWalker` traverses the graph, picks `next_runnable()` nodes |
| Per-task agents | `orchestration/runner.py` — `LocalSpecialistRunner` wraps `engine.Session` |
| Typed agents | `orchestration/specialists.py` — explore / plan / implement / verify / repair / research |
| Auto-review | walker auto-spawns verify siblings on low confidence; repair on revise verdict |
| Persistence | `orchestration/persistence.py` — graph + snapshots at `~/.resonant/projects/<hash>/plans/` |
| Skill extraction | `orchestration/skill_extraction.py` — auto-extract on completion when avg confidence ≥ 0.8 |
| Orchestrator lifecycle | `orchestration/intent_service.py` — `start_intent` / `pause` / `resume` / `cancel` / snapshot ops |
| GUI plumbing | `app.py` — WS commands `intent_start` / `intent_pause` / `intent_resume` / `intent_list_snapshots` / `intent_restore_snapshot`; events flow back as `plan.event` |

What's missing for **week-long** agents specifically:

1. **Headless / background execution** — the orchestrator currently lives
   inside an open WebSocket; close the GUI and it stops.
2. **Concurrency** — `walker` executes serially today. Multi-day work needs
   parallel leaf execution with isolated contexts.
3. **Convergence rules** — the verify/repair loop has no built-in maximum.
   Without stopping rules it can loop indefinitely on subjective taste.
4. **Cost & blast-radius controls** — no per-intent token budget, no "pause
   on $X spent", no automatic git-checkpoints between phases.
5. **Notification layer** — when the agent hits an ambiguity or budget,
   the user has no out-of-band way to know.

---

## Phased build

Phasing is non-negotiable. Building this monolithically is months of work
with high regression risk on the existing UI. Each phase ships value
on its own.

### Phase 1 — Wire the discovery → planning seam *(this milestone)*

**Goal:** prove the end-to-end pipeline on a real task in foreground mode.
User types a rough idea → grilled into a refined spec → spec hands off to
the existing `intent_service` → existing graph walker executes → user sees
result. No concurrency. No background execution. No cost controls. Just
the seam between the new "discovery" phase and the old "planning" phase.

**Design choice — grill-me as a slash command, not a graph node.**

The original instinct was a new `INTERVIEW` specialization in `plan_graph`.
The Explore-agent map turned up a hard blocker: the existing `engine.Session`
has no mid-turn user-input mechanism — adding interactive Q&A as a graph
node requires a significant Session refactor (yield-on-user-input, new tool
type, WebSocket round-trip per question). That's the wrong place to start.

Instead, ride the *existing chat loop*. The chat already supports multi-turn
back-and-forth. Slash commands like `/plan` already exist. Wire `/grill` as
a slash command that:

1. Starts a fresh chat with a grill-me system prompt overlay
2. Lets the agent and the user converse normally — one Q at a time, user
   types responses, all through the existing message flow
3. When the model emits a structured "spec" sentinel, render a one-click
   "Build this" affordance
4. Clicking that affordance calls `intent_start(spec_text)` — the existing
   intent flow takes over

This keeps Phase 1 surface area tiny: one prompt module, one slash command,
one detection regex, one button handler. Zero orchestration changes.

**Deliverables:**

- `resonant_client/orchestration/grill_me.py` — system prompt + structured
  output schema + spec-detection helper
- Slash-command wiring in `gui/static/app.js` (`/grill <description>`) that
  mirrors the `/plan` pattern at [app.js:715-720](resonant_client/gui/static/app.js)
- Backend support for the system-prompt overlay on a session (per-message
  prefix, no Session-class changes)
- Spec-detection on text.done — when the model emits the spec sentinel,
  surface a "Build this roadmap" button beneath the message
- Button handler → wraps the spec into an `intent_start` call with
  existing wiring
- Tests: unit test for the spec-detection regex; integration test that
  drives a mock backend through grill → detect spec → kick off intent

**Exit criteria:**

- [ ] `/grill add a dark-mode toggle to settings` produces multi-turn
      Q&A in chat
- [ ] When the agent writes the spec sentinel block, a "Build this" button
      appears
- [ ] Clicking it kicks off intent flow with a `plan` graph that decomposes
      into explore/implement/verify nodes (existing behavior)
- [ ] Non-grill chat is unaffected
- [ ] Cancellation mid-grill works
- [ ] All existing tests still pass; ≥3 new tests cover the new module

**Out of scope for Phase 1:**

- Headless / background execution
- Parallel leaf execution
- Cost / budget controls
- Notifications
- Replay of the grill phase as a graph node (lives in chat replay)

### Phase 2 — Detach the orchestrator from the WebSocket *(weeks 2–3)*

**Goal:** the orchestrator runs as a project-scoped service. Closing and
reopening the GUI doesn't kill in-flight work.

**Why now:** Phase 1 ships value but is foreground-only. Real "week-long"
needs survival across GUI restarts.

**Design sketch:**

- Promote `orchestration/intent_service.py` to a daemon-style service that
  binds to a project path, not a WebSocket
- Persist worker state per intent (running threads, current node, partial
  results) so a service restart can resume
- GUI subscribes to the project's intent stream when reconnecting; events
  catch up via snapshot replay
- Single intent per project for now — concurrency is Phase 3

**Exit criteria:**

- [ ] Start an intent, close the GUI, reopen. Intent is still running.
- [ ] Reopening shows the live event stream resume mid-flight.
- [ ] Service crash + restart: intent resumes from last snapshot.

### Phase 3 — Parallel leaf execution + convergence rules *(multi-week)*

**Goal:** the walker dispatches independent leaves to parallel sub-agent
processes; verify/repair has explicit stopping rules.

**Sub-deliverables:**

- Parallel walker (job pool, isolated `engine.Session` instances per leaf,
  per-leaf token+step budgets)
- `max_revisions_per_node` cap with escalation event when hit
- "Human checkpoint" node type that pauses and emits an inbox event
- Per-intent token budget and "pause on $X spent" gate

### Phase 4 — Notifications + cost dashboard + steering UI *(after Phase 3)*

- OS-level push notifications on escalation / budget / completion
- Live cost dashboard per project / intent / node
- Steering UI: pause, redirect, cancel a leaf without killing the intent

---

## Risks & tradeoffs

**The biggest tradeoff is honesty in the user-facing pitch.** "Week-long
agents" doesn't materialize until Phase 3. Phase 1 gives a discovery-→-plan
demo loop; Phase 2 gives "survives restart". Marketing the long-running
promise before Phase 3 ships builds the wrong expectation.

**Cost runaway is real.** Every phase needs to add another guardrail:

- Phase 1: existing per-session step limit + cancel button (no change)
- Phase 2: persistence makes runaway harder to kill — add a hard kill-switch
- Phase 3: explicit per-intent and per-leaf budgets, gates on overrun
- Phase 4: live spend dashboard, notification on threshold breach

**Verify-loop infinite loop.** The most likely failure mode in Phase 3 is
verify→revise→repair→verify→revise forever on subjective tasks. Hard cap
+ escalation to human is the cheapest defense.

**Concurrency surface area.** Parallel leaves means race conditions on
shared filesystem state (git, project files). Phase 3 needs either
per-leaf workspaces (cheap: isolated worktrees) or pessimistic locking
(expensive). Default to worktrees.

---

## Open design questions (deferred to later phases)

- Q: When a leaf completes, who decides if it goes to verify or just merges
  into the parent? *Today: walker auto-spawns verify on low confidence.
  Phase 3: needs a configurable policy (always-verify, never-verify,
  threshold-based).*
- Q: How does the user steer a running intent? *Phase 4 — needs a UI
  surface beyond the existing "cancel".*
- Q: What's the unit of "skill" emerging from a long-running run? *Today:
  one skill per completed intent. For week-long runs, may want
  per-phase or per-deliverable skills.*

---

## What this doc is and isn't

This is a working roadmap, not a contract. Each phase's exit criteria are
the verifiable bit. The "design sketch" sections are starting points — the
real design lands when we start building each phase.

When a phase ships, update this doc with what actually got built (and what
got cut).
