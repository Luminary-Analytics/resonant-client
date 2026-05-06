# PLAN — Agent Self-Improvement (Skills + Memory Nudges + Curator)

> **Status: 📐 Design / iterating** · **Last updated: 2026-05-06** · v1 draft, ready for review
>
> Forward-looking design doc, NOT a foundation cluster. v0.6.x candidate work; will lift to ROADMAP as concrete tasks once the design settles.

---

## TL;DR

Lift Hermes Agent's three-layer self-improvement architecture (system-prompt nudges + agent-callable skill/memory tools + background curator) onto the resonant-client autonomous mission daemon. Goal: every mission contributes durable, project-scoped skill files that compound across future missions; the curator periodically consolidates them; the agent reaches for them on next mission start.

Builds on existing primitives (REFLECT specialist, autonomous mission daemon, Engram memory integration, audit log, diagnostics ZIP) — not a rewrite. Phased over **3-4 alphas in a v0.6 minor**.

The headline payoff: **the linux-bridge mission's path-mismatch finding becomes a skill the next Tauri mission references**. Right now that knowledge dies with the chat history.

---

## Why now

Three converging factors as of v0.5.18:

1. **The streaming-stub harness from v0.5.17 makes this testable.** Every layer of the curator + skill-creator can be driven deterministically without a real backend. Pre-v0.5.17, this would have been a hairball.
2. **The orchestration plumbing is mature.** Specialist routing (v0.5.8a1), decision-request HITL (v0.5.8a2), the autonomous mission daemon's atomic terminal-state transitions (v0.5.6a3), per-iter cost attribution (v0.5.9a2) — all the boundaries needed for a curator-on-top integration are stable.
3. **The hygiene runway is exhausted.** v0.5.10 → v0.5.18 covered the lowest-hanging coverage gaps; central modules at 72-100%. Future hygiene minors will have diminishing returns. **Self-improvement is the next compounding investment.**

---

## Goal (concrete)

After this lands, a typical resonant-client mission flow looks like:

1. User dispatches an autonomous mission ("scaffold a Tauri+Svelte launcher").
2. **At dispatch:** the daemon's PLAN_DEEP specialist is given a list of relevant skills (matched via simple keyword + path heuristics) — *"There's a skill `tauri-v2-window-config-quirks` from a prior mission. View it before scaffolding."*
3. **During the mission:** when REFLECT marks a roadmap item satisfied AND the implementation involved ≥3 specialist invocations OR fixed a non-trivial bug, the daemon spawns a **skill-extraction sub-mission** that writes a SKILL.md with provenance `created_by: "agent"`.
4. **At mission end:** if the GA verdict is satisfied, the curator queues a consolidation pass (does NOT block the user). The pass forks a fresh agent, reviews the project's `.resonant/skills/` dir, merges narrow siblings into umbrella skills, archives stale ones.
5. **Next mission start:** the daemon loads the consolidated skill index into the planner's context.

Side effects we want:
- **Cross-mission compounding** (the v0.6.x flagship feature).
- **Smaller iteration counts** as common patterns get pre-encoded.
- **A queryable record of "what we learned"** that's not buried in the audit log.

---

## What we're borrowing from Hermes (and what we're NOT)

### Borrowing

| Mechanism | Source | How we adapt |
|---|---|---|
| `SKILLS_GUIDANCE` system-prompt block with concrete triggers | Hermes `agent/prompt_builder.py` | Inject into PLAN_DEEP + REFLECT specialist prompts (not the chat session — see "Out of scope") |
| `MEMORY_GUIDANCE` block | Hermes | Inject into REFLECT (the natural "I just learned something" moment); leverage existing Engram |
| Skill format (YAML frontmatter + markdown body) | [agentskills.io](https://agentskills.io) | Adopt verbatim. Add one resonant-specific frontmatter block. |
| `created_by: "agent"` provenance gate | Hermes curator | Same exact contract — bundled skills off-limits, agent skills consolidatable |
| Periodic curator (forked fresh agent, consolidation prompt) | Hermes `agent/curator.py` | Adapt cadence to mission-driven (post-GA), not idle-time-driven |
| Archive-only destruction | Hermes | Same — never delete agent skills |

### NOT borrowing

| Hermes feature | Why we skip |
|---|---|
| Honcho dialectic user modeling | Single-user, single-machine. Engram + RAG covers our needs. |
| Cross-platform messaging gateway | Orthogonal — we're GUI/TUI-first. |
| `session_search` (FTS5 across all sessions) | Already partially covered by codebase RAG + diagnostics ZIP. Maybe a v0.7 polish item. |
| Idle-based curator cadence (7d / 2h-idle) | We're mission-driven; we'll trigger on natural mission boundaries instead. |
| User-global skill storage (`~/.hermes/skills/`) | We're project-first; user-global as v0.7 escalation. |

---

## Architecture: where it fits in our stack

```
┌─────────────────────────────────────────────────────────────────┐
│                  Autonomous Mission Daemon                       │
│  (gui/autonomous_loop.py — already exists)                       │
│                                                                   │
│   ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌────────────┐  │
│   │ PLAN_DEEP│→│  IMPLEMENT  │→│ REFLECT  │→│ ↻ next iter │  │
│   └──────────┘  └────────────┘  └──────────┘  └────────────┘  │
│        ▲              │              │                            │
│        │              │              │                            │
│        │     LOAD     │   [trigger]  ↓ at satisfied verdict       │
│        │              │     ┌──────────────────────────┐          │
│        │              │     │  Skill-Extraction        │  NEW     │
│        │              │     │   sub-mission            │          │
│        │              │     │   (forked specialist)    │          │
│        │              │     └────────┬─────────────────┘          │
│        │              │              │                            │
│        │              │              ↓ writes SKILL.md            │
│        │              │     ┌──────────────────────────┐          │
│        │              └────►│ <project>/.resonant/      │          │
│        │   discover via     │   skills/<slug>/SKILL.md  │  NEW     │
│        └────────────────────│                            │          │
│                             └──────────────────────────┘          │
│                                       ↑                            │
│                                       │ post-GA trigger             │
└───────────────────────────────────────┼────────────────────────────┘
                                        │
                              ┌─────────┴──────────────┐
                              │   Skill Curator         │  NEW
                              │   (forked fresh agent)  │
                              │   gui/skill_curator.py  │
                              │                          │
                              │   - merge umbrellas      │
                              │   - archive stale        │
                              │   - never delete         │
                              └─────────────────────────┘
                                        │
                                        ↓
                              ┌─────────────────────────┐
                              │  ~/.resonant/projects/   │
                              │   <hash>/curator/        │  NEW
                              │   {YYYYMMDD-HHMMSS}/     │
                              │     run.json + REPORT.md │
                              └─────────────────────────┘
```

**Three additions to the codebase:**
1. `resonant_client/agent/skill_extractor.py` — invoked by REFLECT when satisfied; spawns a sub-mission to write a SKILL.md.
2. `resonant_client/gui/skill_curator.py` — periodic consolidation pass (mirrors `harness/orchestrator.py` shape).
3. `resonant_client/agent/skill_loader.py` — discovers + injects relevant skills into specialist prompts at dispatch.

**Two existing modules touched:**
- `resonant_client/orchestration/specialists.py` — add SKILLS_GUIDANCE + MEMORY_GUIDANCE blocks to PLAN_DEEP + REFLECT system prompts.
- `resonant_client/gui/autonomous_loop.py` — add post-satisfied-verdict hook calling the skill extractor; add post-GA hook queueing the curator.

---

## Data model: the SKILL.md format

Adopting [agentskills.io](https://agentskills.io) verbatim with one resonant-specific frontmatter block:

```yaml
---
name: tauri-v2-window-config-quirks
description: "Tauri v2 window config differs from v1 — title is per-window, not top-level."
version: 1.0.0
author: resonant-agent
license: MIT
metadata:
  resonant:
    created_by: agent          # MUST be "agent" for curator to touch
    created_at: 2026-05-03T20:29:37Z
    source_intent_id: 8d821d125f3c
    source_iter: 1             # which iter of the mission produced it
    source_finding_id: ""      # optional ref to a field-observation finding
    pinned: false              # user-pin protects from auto-archive
    last_used_at: 2026-05-03T20:29:37Z
    use_count: 0
    related_skills: []
---

# Tauri v2 window config quirks

## Symptom
[Markdown body — what triggered the skill, what to watch for]

## What to do
[The actual reusable content — code snippets, commands, etc.]

## References
[Link to source mission's audit log + iter cards]
```

**Storage layout (project-scoped, v0.6.0):**

```
<project>/
  .resonant/
    skills/
      tauri-v2-window-config-quirks/
        SKILL.md                          # the skill itself
        references/                       # session-specific evidence
          mission-8d821d125f3c.md         # extracted from audit log
        templates/                        # optional
        scripts/                          # optional
      .archive/                            # curator-archived skills
        2026-05-12T10-30-stale-skill/
      .index.json                         # generated by skill_loader
```

**Why project-scoped first:**
- One project's skills aren't always relevant to another (Tauri quirks ≠ data-pipeline quirks).
- Lives next to the project the agent is operating on (no global state surprise).
- Matches how the autonomous mission daemon already scopes everything.
- v0.7 escalation path: user-global elevation via a `skill promote` command when a skill is general enough.

---

## The lifecycle

### Phase 1: extraction (during mission, at REFLECT satisfied verdict)

**Trigger** (in `gui/autonomous_loop.py` `_run_full_reflect`):

```python
if outcome.verdict == "satisfied" and not verdict_overridden:
    # The mission item ACTUALLY succeeded.
    if self._meets_skill_extraction_threshold(rm, pass_result):
        self._spawn_skill_extraction(rm, pass_result, outcome)
```

**`_meets_skill_extraction_threshold` heuristic:**
- ≥3 specialist invocations in the iter, OR
- REFLECT's `unpassed_criteria` was non-empty during the iter (i.e. we resolved a tricky one), OR
- `findings` list in REFLECT contained ≥1 entry tagged with severity ≥ medium.

**Extraction sub-mission:**
- Spawned as a sibling of the main daemon (not blocking the next iter).
- Forked specialist with this system prompt:

```
You are the SKILL_EXTRACTOR specialist. The autonomous mission
just completed roadmap item "<item.title>" successfully. Your
job: produce a single agentskills.io-format SKILL.md that captures
the REUSABLE pattern from this iter — not the specific work that
was done.

Reusable means: the next mission in this codebase (or a similar
codebase) facing the same shape of problem should be able to
follow this skill and skip your trial-and-error.

DO NOT extract:
- Project-specific code that won't apply elsewhere.
- One-off bug fixes.
- Trivial work (file scaffolding, boilerplate).
- Skills that are restating something already in AGENTS.md or
  an existing skill (run skill_list first).

DO extract:
- Patterns, conventions, framework quirks the agent had to learn
  by trial.
- Workflows that took ≥3 specialist calls.
- Failure-mode → resolution mappings.

Output: write the SKILL.md via skill_write. Frontmatter MUST
include `metadata.resonant.created_by: agent`.

If nothing reusable emerged, exit silently. It is BETTER to write
zero skills than to pollute the library.
```

**Tools available to the extractor specialist:**
- `skill_list(filter=None)` — see what skills exist (avoid duplicates)
- `skill_view(name)` — read existing skill body
- `skill_write(name, frontmatter, body, references=[])` — create new skill
- `skill_patch(name, patch)` — refine existing skill (rare; mainly for the curator)
- `audit_query(intent_id, kind=None)` — read audit log entries from the source mission
- `roadmap_view(intent_id)` — read the roadmap.md the mission ran against

**Cost guardrail:** extraction sub-missions get a hard 5-minute time budget + 10-iteration cap. If the extractor doesn't write a skill in that window, log + abort.

### Phase 2: discovery + retrieval (at mission dispatch)

**At PLAN_DEEP invocation** (in `orchestration/runner.py` or wherever specialist context is built):

```python
relevant_skills = skill_loader.match_skills(
    project_path=...,
    user_msg=spec_markdown,           # the spec the user grilled
    file_hints=detected_filetypes,    # from project_path scan
    max_skills=8,                     # avoid prompt bloat
)
```

**`skill_loader.match_skills` ranking** (simple keyword overlap, no embeddings in v0.6.0):
- Token-overlap between skill `description` + `tags` and the spec text.
- File-extension match (`.svelte` files in project → favor svelte/tauri skills).
- Recency boost for skills used in the last 30 days.
- Pinned skills always included.

The matched skills get injected into PLAN_DEEP's user message (NOT system prompt — keeps cache hits) as:

```
## Relevant skills from prior missions in this project

The following skills were extracted from past successful missions.
Reference them BEFORE planning:

1. `tauri-v2-window-config-quirks` — Tauri v2 window config differs
   from v1 — title is per-window, not top-level.
   View with: skill_view tauri-v2-window-config-quirks

2. ...

If a skill is directly applicable, follow it. If you decide a skill
is wrong or stale for this mission, note WHY in your spec and the
curator will see it on next consolidation.
```

The agent isn't FORCED to use them — it's nudged. (Mirrors the v0.5.7a5 grill exemplar pattern.)

### Phase 3: curation (post-GA, between missions)

**Trigger** (in autonomous mission daemon's `_emit_stop` for `verdict=satisfied`):

```python
if final_verdict == "satisfied":
    skill_curator.queue_consolidation(
        project_path=...,
        intent_id=intent_id,
        reason="post_mission_satisfied",
    )
```

The curator does NOT block the user — it queues to a background ThreadPoolExecutor (mirrors `harness/orchestrator.py`'s shape). Status visible in a new sidebar entry; user can watch progress without it gating their next mission.

**Curator sub-mission system prompt** (lifted directly from Hermes' framing, with our terminology):

```
You are the SKILL_CURATOR. The autonomous mission just completed
successfully. Your job is to review the project's accumulated
skills (those with `created_by: agent` provenance ONLY) and
consolidate them.

The goal of the skill collection is a LIBRARY OF CLASS-LEVEL
INSTRUCTIONS. A collection of dozens of narrow skills where each
one captures one mission's specific bug is a FAILURE of the
library. Aggressive merging is the right move.

Operations available:
- skill_list(filter="created_by:agent")
- skill_view(name)
- skill_patch(name, patch)        — REFINE an existing umbrella
- skill_archive(name, reason)     — move to .archive/ (NEVER delete)
- skill_pin(name) / unpin         — flag durability

Hard rules:
- DO NOT touch skills with `created_by: bundled` or
  `created_by: user`. They are off-limits.
- DO NOT delete. Archive is the maximum destructive action.
- DO NOT touch skills with `pinned: true` unless the user-content
  needs a factual correction.
- Archive any skill with `last_used_at` >90 days old.
- Mark `stale: true` (do not archive yet) for skills 30-90 days old.

Goal: at the end of your run, produce a REPORT.md summarizing
what you did. Save it to <project>/.resonant/curator/<timestamp>/
alongside a machine-readable run.json.
```

**Cadence & idempotency:**
- Triggered on `verdict=satisfied` GA, but rate-limited to once per 24h per project.
- Pure re-runs are no-ops if the skill set hasn't changed since last run (signature on `os.listdir + mtimes`).
- The curator is forked from the same backend the user already configured (no new model dependency).

---

## System-prompt integration: the actual nudge blocks

### `SKILLS_GUIDANCE` (added to PLAN_DEEP system prompt)

```
## Skills

This project has a `.resonant/skills/` directory with extracted
patterns from prior successful missions. The dispatcher already
loaded the relevant ones into your context above.

Before deciding the plan-graph, consider:
- Does any of the loaded skills apply directly? If yes, plan-graph
  should reference it.
- Is there a skill that warns of a quirk in the framework you're
  about to use? Read it via skill_view BEFORE planning.

Skills are nudges, not commands. If a skill is wrong for this
mission, note WHY in your spec — the curator will see it.
```

### `MEMORY_GUIDANCE` (added to REFLECT system prompt)

```
## Memory writes

When the deterministic [bash] / [chrome] checks pass AND the work
materially advanced the roadmap, consider whether anything
DURABLE was learned that should be written to memory:

- A user preference observed across multiple turns (e.g. "user
  prefers fish over zsh", "user wants smoke runs against pro by
  default").
- A project convention you discovered the hard way (e.g. "this
  codebase uses Pydantic v1 not v2 — `class Config:` not
  `model_config`").
- A tool quirk you'd want to know on day 1 of the next mission.
- A finding worth carrying across project boundaries (rare; usually
  belongs in a skill, not memory).

DO NOT write to memory:
- Task progress / current state.
- One-off bug fixes (that's a skill, not memory).
- Things already in AGENTS.md (the project conventions live there).

Use the memory tool when set; if not configured, just note in your
REFLECT summary that "would have written: [fact]" so the user can
manually capture if they want.
```

### `SKILL_EXTRACTION_GUIDANCE` (the extractor specialist)

Already shown above in Phase 1.

---

## Storage decisions (proposed defaults)

| Decision | Default | Reasoning |
|---|---|---|
| **Per-project vs user-global** | Per-project at `<project>/.resonant/skills/` | Tauri quirks ≠ data-pipeline quirks. v0.7 can add elevation. |
| **Skills written into the project repo OR sandboxed?** | Sandboxed (`.resonant/` is gitignored, mirrors session state) | The user might not want skill files in their repo; they can opt in via a `skill_promote` command later. |
| **Skill format compatibility** | agentskills.io verbatim | Future-proof — swap to other agent ecosystems if needed. |
| **Curator log location** | `~/.resonant/projects/<hash>/curator/<timestamp>/` | Mirrors existing autonomous mission audit log layout. |
| **Skill reference attachments** | `<skill_name>/references/` subdir | Match Hermes' convention. |
| **Provenance tag default** | `created_by: agent` for extractor output | Curator-touchable. |

---

## Phased rollout

### v0.6.0a1 — skill format + storage scaffolding

- Define the SKILL.md frontmatter schema in code.
- Build `skill_loader.py` with file discovery + index generation (no matching yet).
- Add 2-3 hand-curated reference skills as `created_by: bundled` examples (one for resonant-client itself, one for grill-style spec refinement).
- Tests: round-trip parse/write, frontmatter validation, glob discovery.

**No agent integration yet** — purely the data layer.

### v0.6.0a2 — extractor specialist

- Add SKILL_EXTRACTOR to specialists.py.
- Wire skill_write / skill_view / skill_list tools.
- Add the extraction trigger in `_run_full_reflect`.
- The streaming-stub harness from v0.5.17 lets us drive the extractor without a real backend.
- Tests: extractor produces a SKILL.md with valid frontmatter; threshold heuristic correctly skips trivial work; cost cap enforced.

### v0.6.0a3 — curator

- Add `skill_curator.py` mirroring `harness/orchestrator.py` shape.
- Curator system prompt + tools.
- Integration into autonomous mission daemon's `_emit_stop`.
- Tests: curator merges narrow siblings; archives stale; respects `pinned`; never touches non-agent skills.

### v0.6.0a4 — discovery + injection

- Wire `skill_loader.match_skills` into PLAN_DEEP context build.
- Test: with seeded skills + a mission spec, the right skills get loaded; pinned skills always included; skill list capped at 8.

### v0.6.0 GA

- Release notes + ROADMAP update.
- Add `resonant skill list / view / promote / pin / archive` CLI commands.
- Document the SKILL.md format in a new `docs/skills.md` page.

### v0.6.1+ (deferred)

- Embedding-based skill matching (currently keyword-only).
- User-global skill elevation (`skill promote --global`).
- Field-run validation: dispatch mdcheck or similar with skills enabled, verify the daemon actually references and improves them.
- Integration with the smoke harness — pre-seed skills in test specs.

---

## Open questions for iteration

These are the decisions I'm not confident about. Each one is a "what would you prefer?" rather than an architectural blocker.

### Q1: Extraction trigger granularity

**Proposed:** every `verdict=satisfied` REFLECT iter that meets the threshold heuristic.

**Alternatives:**
- Only at GA (mission complete). Coarser, cheaper, but misses mid-mission learnings.
- After every iter regardless of threshold. Will produce too many skills; relies on curator to fix.
- User-flagged ("save this iter as a skill"). Simplest but requires explicit user action; misses the autonomous compounding payoff.

**My pick:** the proposed, because the threshold heuristic gates noise. But I'm open to GA-only as v0.6.0a2 starting point if the threshold is hard to tune.

### Q2: Curator cadence

**Proposed:** triggered on every satisfied GA, rate-limited to once per 24h per project.

**Alternatives:**
- Time-based (Hermes' 7d-idle approach). Doesn't fit our episodic model.
- Manual only (`resonant skill curate` command). Lowest risk; loses the autonomous compounding.
- After every N missions (count-based). Simpler than time-based; might miss long-quiet projects.

**My pick:** the proposed. The 24h rate-limit prevents thrash; the satisfied-GA trigger ensures it only runs after real success.

### Q3: Where does SKILLS_GUIDANCE live in the prompt stack?

**Proposed:** PLAN_DEEP system prompt + REFLECT system prompt only. NOT in chat-session prompt (where the user does discovery / general chat).

**Why:** chat-session is mission-AGNOSTIC; injecting SKILLS_GUIDANCE there would clutter the system prompt for users who never use the autonomous mission daemon. Keep the nudge in the place where it pays off.

**Alternative:** include in chat-session too, gated on whether `<project>/.resonant/skills/` exists. Safer — agent can mention skills in conversation. Worse — every chat session pays the prompt-cache cost.

**My pick:** the proposed — surgical injection. Easy to expand later.

### Q4: User-global vs project-scoped first?

**Proposed:** project-scoped only in v0.6.0; user-global as v0.7 escalation via `skill promote`.

**Why:** project boundaries are real (Tauri quirks ≠ Python project quirks). User-global is harder to reason about — when should a skill apply? Embeddings? Heuristics? Wait until we see real cross-project value.

**Alternative:** user-global from day 1, scoped by tag/match. More flexible but harder to keep clean.

**My pick:** the proposed. v0.7 can add a `tags: [language:python, framework:tauri]` matcher.

### Q5: How do extracted skills relate to AGENTS.md?

**Tension:** AGENTS.md is the canonical project-conventions file (per cluster 5 in ROADMAP). Skills are project-conventions-adjacent. Risk of duplication or contradiction.

**Proposed:** skills are STRICTER than AGENTS.md — they apply only when their description matches the current task. AGENTS.md is always loaded; skills are matched + loaded conditionally. The MEMORY_GUIDANCE block tells the agent: "if this is a stable fact about the project, edit AGENTS.md; if it's a pattern that applies to a particular shape of task, write a skill."

**Open:** does the curator check AGENTS.md for "this should have been a skill" candidates? Or is that a v0.7 polish? 

**My pick:** v0.7 — defer. Get the basic loop working first.

### Q6: What about Engram memory in this picture?

**Proposed:** Engram is for FACTS (declarative — "Mac Studio at 10.0.0.133"); skills are for PATTERNS (procedural — "when scaffolding Tauri v2, do X"). Different shapes, different storage, different retrieval (Engram via similarity search, skills via keyword match). MEMORY_GUIDANCE block draws the line.

**Alternative:** unify — store skills in Engram. Tempting (one storage backend) but the curator pattern doesn't fit Engram's API and we'd lose the file-based simplicity.

**My pick:** keep them separate. Skills as files, memory as Engram. Two well-defined modes.

---

## Out of scope for v0.6.x

Explicitly NOT shipping in this design:

- **Cross-mission session search** (Hermes' `session_search` tool). Punt to v0.7. Diagnostics ZIP already captures the data.
- **Honcho-style user modeling.** Not applicable to single-user product.
- **Cross-platform messaging gateway.** Out of charter.
- **User-global skill promotion.** v0.7.
- **Skill embeddings / semantic search.** v0.7. Keyword matching is good enough to start.
- **Per-user skill marketplace / hub.** Out of charter.
- **Curator's auto-stale heuristic** (Hermes uses 30/90 days). Adopt the same defaults but make them configurable in `general.skill_curator_*` settings.
- **Multi-language skill descriptions.** English only.

---

## Validation criteria (Definition of Done for v0.6.0)

- [ ] A fresh greenfield project can run a 1-hour mission, hit `verdict=satisfied` on a non-trivial iter, and produce ≥1 valid SKILL.md without user action.
- [ ] Running a SECOND mission in the same project demonstrably loads the prior skill into PLAN_DEEP's context (verified by snapshot of the planner's first prompt).
- [ ] The curator runs after a satisfied GA, generates a REPORT.md, and either consolidates or no-ops cleanly.
- [ ] Pytest suite stays ≥99% pass rate. New code is ≥85% covered.
- [ ] E2E preview pass clean across all 4 alphas.
- [ ] mdcheck (or another field run) dispatched WITH the v0.6 build to verify skills actually compound across two missions in the same project.
- [ ] Documentation: `docs/skills.md` describes the format + lifecycle. ROADMAP timeline updated.

---

## Risks + mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Skill explosion (the very failure mode the curator is meant to fix) | High | Aggressive curator umbrella-merging from day 1. Threshold heuristic is conservative. |
| Stale skills mis-leading the next mission | Medium | `last_used_at` tracking. Curator marks stale @30d. Skill-loader can demote stale matches. |
| Extractor over-extracts on trivial work | Medium | Hard threshold heuristic (≥3 specialist calls / non-trivial bug / etc.). Conservative defaults. |
| Curator hallucinates "consolidation" that loses information | Medium | Archive-only, never delete. Pre-curator snapshot in `<project>/.resonant/skills/.snapshots/<ts>/`. |
| Performance regression — skill loading bloats PLAN_DEEP prompt | Low | Hard cap at 8 skills. Length limit per skill description. Cache the index. |
| Skills become tied to a specific model's output style | Low | Skills are PROSE markdown — model-portable. Frontmatter is structured. |
| Curator runs during an active mission | High (if it happens) | Hard rule: curator queue checked at GA only; never preempts a running daemon. |

---

## Future / nice-to-haves (v0.7+)

- User-global skill promotion + `skill promote` command.
- Embedding-based matching (sentence-transformer locally; no extra cloud dep).
- Cross-project skill borrowing via tags (`framework:tauri` tag matches across projects).
- A "skill confidence score" from the curator (how often it's been used + helped).
- Integration with Honcho or similar for opt-in user modeling.
- Skill A/B testing — when two skills overlap, the curator can flag for the user to merge.
- Field-run-driven skill creation: the linux-bridge-style post-mortem produces skills automatically.

---

## Why this is a real-deal compounding investment

The streaming-stub harness from v0.5.17 was a one-time infrastructure investment that lowered the cost of every future test. **This proposal is the same shape but for cross-mission learning.** Built once, every future autonomous mission gets cheaper because:

1. The next mission starts with relevant skills pre-loaded — fewer iters to figure out the same thing twice.
2. The curator keeps the skill library from rotting — the user doesn't have to manage it.
3. The agent's "what should I have known about this codebase" gap shrinks every successful mission.

The downside is real but bounded: maybe 1.5-2 weeks of focused work spread across v0.6.0a1 → v0.6.0a4 → GA. With the v0.5.x infrastructure in place, there's no foundational refactor required — this is purely additive.

---

## Status of this doc

**v1 draft, ready for iteration.** I'll fold in your pushback (open questions Q1-Q6 + anything else) and produce v2 before any code lands. After v2 stabilizes, this becomes the spec the v0.6.0a1 alpha is built from.

— EOF v1 —
