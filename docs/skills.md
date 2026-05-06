# Skills (v0.6.0)

The skills system lets autonomous missions teach themselves across runs. After every successful mission iter, an extractor specialist looks at what was learned and writes a reusable SKILL.md if there's a pattern worth keeping. The next mission in the same project sees those skills in its planner's context — patterns compound instead of evaporating with the chat history.

This is the [Hermes Agent](https://github.com/NousResearch/hermes-agent)-inspired self-improvement loop adapted to our autonomous-mission flow. Adapted, not lifted: our `Skill` dataclass and storage already existed (Voyager-inspired plan-graph extraction since v0.4.x); v0.6.0 added the provenance gate, mission-iter extraction path, deterministic curator, and discovery/injection layer that compose them into a closed loop.

---

## What's a skill?

A skill is a markdown file capturing a **reusable pattern** — a framework quirk, a multi-step workflow with non-obvious ordering, a failure-mode → resolution mapping. NOT a one-off bug fix and NOT trivial scaffolding work; the extractor explicitly skips both.

**Storage:** `~/.resonant/skills/<scope>/<id>/`
- `skill.json` — metadata (Skill dataclass shape)
- `procedure.md` — the human-readable body
- `verification.md` — optional success criteria
- `examples/` — past plan-graphs that used this skill (Voyager-style)

**Scopes:**
- `global` — applies across all projects (e.g. the bundled reference skills)
- `project` — applies to one project (e.g. `<project_path>` hash-keyed)
- `stack` — applies to projects with a matching tech-stack signature

## Provenance: who wrote it?

Every skill has a `created_by` field that gates how the curator treats it:

| `created_by` | Source | Curator-touchable? |
|---|---|---|
| `bundled` | Shipped with the package (in `resonant_client/orchestration/bundled_skills/`) | **No** — never archived or modified |
| `agent` | Auto-extracted from a successful plan-graph or autonomous mission iter | **Yes** if not pinned |
| `user` | Manually authored via CLI / GUI | **No** — user owns it |

Plus a `pinned: bool` field. Pinned skills are exempt from auto-deprecation and curator archival regardless of provenance — the user explicitly signaled durability.

## The lifecycle

### 1. Extraction (during mission)

When the autonomous mission daemon's REFLECT marks a roadmap item `verdict=satisfied` (and the verdict is NOT overridden by the cross-check), the extractor specialist runs:

1. **Threshold check** (`should_extract_from_iter`): conservative — verdict satisfied, not overridden, and at least 2 acceptance criteria passed. Trivial iters skip the model call entirely.
2. **Extractor prompt**: focused system prompt instructs the model to either emit `(no skill)` (the right answer most of the time) or a SKILL.md body in the agentskills.io frontmatter format.
3. **Persist**: parsed skill is saved with `created_by="agent"`, `scope="project"`, slug-derived id, tokens populated for matching.

The extractor is best-effort: any failure (backend error, parse error, exception) is logged and swallowed; the daemon continues unaffected.

[Source: `resonant_client/orchestration/skill_mission_extraction.py`](../resonant_client/orchestration/skill_mission_extraction.py)

### 2. Discovery (at next mission's PLAN_DEEP)

When the next mission's planner runs:

1. `match_skills_for_query` searches global + project scopes via token-overlap matching.
2. **Pinned skills always go first**, alphabetically — regardless of token overlap. The user pinned them; they stay in scope.
3. Non-pinned matches with score ≥ 0.05 follow, sorted by score descending.
4. Total capped at `max_skills` (default 8 — ~250 tokens of context).
5. `format_skills_for_prompt` produces a numbered markdown block with 📌 markers for pinned skills, match scores for the rest, and `skill_view <id>` view handles.

The block is injected into the planner's user message (NOT system prompt — keeps prompt-cache hits) so the model can reference relevant prior patterns BEFORE generating the plan-graph.

[Source: `resonant_client/orchestration/skill_loader.py`](../resonant_client/orchestration/skill_loader.py)

### 3. Curation (post-mission)

When the autonomous mission daemon hits a satisfied terminal state (`_emit_stop("satisfied", ...)`), it queues a curator pass:

1. Lists curator-touchable skills (`created_by="agent"` AND not pinned, in this project's scope).
2. For each, calls `Skill.is_deprecated()` with default thresholds (90 days unused OR ≥50% fail rate on ≥10 uses).
3. Archives matches via `archive_skill` — moves to `~/.resonant/skills/_archive/<scope>/<ts>__<id>/` with a `_archive_reason.txt` next to it. **Never deletes.**
4. Writes `run.json` + `REPORT.md` to `~/.resonant/projects/<hash>/curator/<ts>/`.
5. Updates `~/.resonant/projects/<hash>/curator/.state.json` (rate-limited to once per 24h per project).

The deterministic curator only handles stale archival in v0.6.0. Model-driven umbrella consolidation (where a forked agent merges narrow sibling skills into broader patterns) ships in v0.6.1+.

[Source: `resonant_client/orchestration/skill_curator.py`](../resonant_client/orchestration/skill_curator.py)

## SKILL.md format

Frontmatter (matches the [agentskills.io](https://agentskills.io) standard):

```yaml
---
name: Tauri v2 window config quirks
description: Tauri v2 differs from v1 — title is per-window not top-level.
version: 1.0.0
triggers: [tauri, window-config]
---

# Tauri v2 window config quirks

## Symptom
You set `title` at the top level of `tauri.conf.json` and Tauri
v2 fails to compile because the schema expects it per-window.

## What to do
Move `title` under `app.windows[0].title` instead.

## When NOT to apply
v1 still uses top-level title; check the version first.
```

## Bundled reference skills

v0.6.0a1 ships two pinned bundled skills as worked examples:

- `rigorous-grill-spec-refinement` — the 5-beat question pattern codified from the linux-bridge field run
- `autonomous-mission-iter-discipline` — per-iter conventions from the v0.5.6→v0.5.9 field findings

Both have `created_by="bundled"` (off-limits to curator) and `pinned=True` (always surfaced in skill discovery).

## Telling skills apart from memory

Two adjacent concepts; clean line:
- **Skills** are PATTERNS — procedural, "when you encounter shape X, do Y." Stored as files. Discovered via token matching at mission dispatch.
- **Memory** (Engram, opt-in) is FACTS — declarative, "Mac Studio at 10.0.0.133", "user prefers Pydantic v1." Stored via the memory provider. Retrieved via similarity search every turn.

Both live in `~/.resonant/`; both are project-aware; the `MEMORY_GUIDANCE` block in REFLECT's system prompt draws the line for the agent: "if it's a stable fact about the project, edit AGENTS.md; if it's a pattern that applies to a particular shape of task, write a skill."

## Open questions / v0.6.1+ candidates

- **Model-driven curator** (umbrella consolidation): forked agent reviews narrow sibling skills and merges them. Currently deferred; the deterministic stale-archival keeps the library from rotting in the meantime.
- **Embedding-based matching**: keyword/Jaccard works at the v0.6.0 scale (dozens of skills). Embeddings unlock cross-language / synonym matching at the cost of an embedding-model dep.
- **User-global elevation** via `resonant skill promote --global`: when a skill is general enough, lift from project to global scope.
- **CLI commands**: `resonant skill list / view / pin / archive / promote`. Stubbed for v0.6.0; full surface lands in v0.6.1.

## Related

- [PLAN-SELF-IMPROVEMENT.md](../PLAN-SELF-IMPROVEMENT.md) — the v1 design that drove this minor.
- [v0.6.0 release notes](v0.6.0-release-notes.md) — per-alpha breakdown.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the prior art this is adapted from.
