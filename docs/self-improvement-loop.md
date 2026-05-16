# The Self-Improvement Loop

> **Audience:** contributors and LLMs picking up `resonant-client`.
> **Status:** as-built through **v0.6.3** (2026-05-08). This is the
> definitive architecture reference for the skill subsystem. For the
> per-version change history see the `docs/v0.6.*-release-notes.md`
> files; for the original design intent see `PLAN-SELF-IMPROVEMENT.md`
> (note: that is the *plan*, this is the *as-built*).

---

## What it is

Resonant Client's differentiating feature: an autonomous coding agent
that **gets measurably better at a codebase every mission it runs**.

A *skill* is a reusable, verified procedure — a pattern, convention, or
failure-mode→resolution mapping — distilled from a successful mission
and surfaced back into the context of future missions. The loop has
two halves:

- **Write side** — after a mission iter succeeds, an extractor model
  decides whether the iter produced a reusable pattern and, if so,
  writes a skill.
- **Read side** — when a later mission is dispatched, a matcher pulls
  relevant skills into the planner's context so the agent skips the
  trial-and-error the first mission already paid for.

A background **curator** keeps the library healthy (archives stale
skills), and a **provenance system** decides what the curator is
allowed to touch.

The design is inspired by Voyager's skill library (plan-graph
distillation) and Hermes Agent's three-layer self-improvement (prompt
nudges + agent-callable tools + background curator).

---

## The loop at a glance

```
   ┌──────────────────────────────────────────────────────────────┐
   │  MISSION N                                                    │
   │                                                               │
   │  grill → spec → roadmap → [iter loop]                         │
   │                              │                                │
   │                              ▼                                │
   │              dispatch_item(roadmap item)                      │
   │                              │                                │
   │         ┌────────────────────┴───────────────────┐            │
   │  READ   │ build_skill_context(item goal)          │            │
   │  side   │  → match_skills_for_query (Jaccard +    │            │
   │         │     pinned-always)                      │            │
   │         │  → inject skills block into planner     │            │
   │         │  → mark_skill_surfaced (staleness clock)│            │
   │         │  → emit skill_context_loaded (→ chip)   │            │
   │         └────────────────────┬───────────────────┘            │
   │                              ▼                                │
   │                    PLAN_DEEP → IMPLEMENT → REFLECT             │
   │                              │                                │
   │         ┌────────────────────┴───────────────────┐            │
   │  WRITE  │ iter verdict == satisfied (not          │            │
   │  side   │   overridden)?                          │            │
   │         │  → extract_skill_hook                   │            │
   │         │  → extractor model: "(no skill)" OR     │            │
   │         │     a SKILL.md  → save (created_by=agent)│            │
   │         └────────────────────┬───────────────────┘            │
   │                              ▼                                │
   │              mission terminates (satisfied)                   │
   │                              │                                │
   │  CURATE  → queue_curation_hook (24h rate-limited per project)  │
   │          → archive stale agent-created skills                 │
   └──────────────────────────────────────────────────────────────┘
              │                                       ▲
              │  skills persist in ~/.resonant/skills/ │
              └───────────────────────────────────────┘
                          MISSION N+1 reads them
```

---

## Data model

### The `Skill` dataclass — `orchestration/skills.py`

| Field | Type | Meaning |
|---|---|---|
| `id` | str | kebab-case slug, unique within scope |
| `name` | str | human-readable display name |
| `description` | str | one-sentence summary |
| `scope` | str | `global` / `project` / `stack` |
| `triggers` | list[str] | phrases/situations for matching |
| `prerequisites` | list[str] | other skill ids |
| `success_count` / `fail_count` | int | quality signal (real attribution only) |
| `last_used_at` | float | staleness clock (epoch seconds) |
| `version` | str | semver string |
| `tokens` | list[str] | pre-tokenized bag for similarity scoring |
| `procedure_steps` | list[dict] | plan-graph node mirror (Voyager path only) |
| `created_by` | str | **provenance gate** — `bundled` / `agent` / `user` |
| `pinned` | bool | **durability gate** — user-marked, curator-exempt |

Two methods carry the policy:

- **`is_deprecated()`** — auto-deprecation: unused > 90 days, or
  fail-rate > 0.5 over ≥ 10 uses. **Pinned skills are always exempt.**
- **`is_curator_touchable()`** — `created_by == "agent" and not
  pinned`. The curator must never touch `bundled` or `user` skills.

### Provenance — the load-bearing gate

`created_by` decides who is allowed to mutate a skill:

| Provenance | Source | Curator | Auto-deprecate |
|---|---|---|---|
| `bundled` | ships in the package (`bundled_skills/`) | ✗ never | ✗ (also pinned) |
| `agent` | auto-extracted from a successful run | ✓ if not pinned | ✓ if not pinned |
| `user` | hand-authored (CLI / field-obs ingest) | ✗ never | ✗ (ingest pins them) |

This is the single most important invariant in the subsystem: **the
curator only ever archives `agent`-created, non-pinned skills.** Bundled
reference skills and human knowledge are immortal unless a human
removes them.

### Scopes

- **`global`** — `~/.resonant/skills/global/<id>/` — applies everywhere
- **`project`** — `~/.resonant/skills/project/<sha1(path)[:12]>/<id>/`
  — applies to one project
- **`stack`** — `~/.resonant/skills/stack/<stack-sig>/<id>/` — applies
  to a tech-stack signature (defined, lightly used)

Agent-extracted skills default to **`project`** scope (a pattern
learned in one codebase is presumed project-specific until promoted).
Bundled and field-obs-ingested skills are **`global`**.

### On-disk layout

```
~/.resonant/skills/
├── global/
│   └── <skill-id>/
│       ├── skill.json          # the Skill dataclass, serialized
│       ├── procedure.md        # the human-readable body
│       └── verification.md     # how success was verified (Voyager path)
├── project/
│   └── <sha1(project_path)[:12]>/
│       └── <skill-id>/ …
├── stack/
│   └── <stack-sig>/
│       └── <skill-id>/ …
├── _deprecated/                # auto-deprecation moves land here
└── _archive/
    └── <scope>/
        └── <ts>__<skill-id>/   # archive_skill moves land here
            ├── skill.json
            ├── procedure.md
            └── _archive_reason.txt
```

`_deprecated/` (auto) and `_archive/` (curator + manual) are kept
separate so a user can audit *what archived a skill and why*. Nothing
is ever hard-deleted — destruction is always a move.

---

## Write side — the extractors

There are **two** extractors because there are two units of work in
the codebase, and they pre-date each other:

### 1. Plan-graph extractor — `skill_extraction.py`

Voyager-style. Distills a skill from a completed **plan-graph**
(`is_complete()` with overall confidence ≥ 0.8, ≥ 3 nodes, all DONE).
Translates node-id dependencies into positional indexes so the skill
can be replayed into a fresh graph. Produces `procedure_steps` +
`verification.md`.

### 2. Mission-iter extractor — `skill_mission_extraction.py`

Distills a skill from an autonomous-**mission iter** completion (the
roadmap-item + REFLECT-verdict abstraction that pre-dates plan-graphs
as the autonomous coder's unit of work).

- **Threshold heuristic** (`should_extract_from_iter`): only
  `verdict == satisfied`, not verdict-overridden, ≥ 2 acceptance
  criteria passed. Cheap pre-filter before any model call.
- **The model decides.** `SKILL_EXTRACTOR_SYSTEM_PROMPT` instructs the
  extractor to emit *either* the literal sentinel `(no skill)` *or* a
  single agentskills.io-format `SKILL.md`. "(no skill)" is the right
  answer most of the time — it is better to write zero skills than to
  pollute the library.
- **Best-effort.** Any exception is logged and swallowed; the daemon
  is never broken by a failed extraction.
- Saves with `created_by="agent"`, `scope="project"`.

### Slug generation (v0.6.2a2)

`slugify()` produces skill ids. The v0.6.2 field run found names like
`actually-create-wordcountpy-with-word-counting-logic-the-fil` (naive
`text[:60]`). The fix:

- default `max_len` 30 (single eye-scan width)
- word-boundary truncation — cut at the last `-` ≤ max_len, never
  mid-word
- strip verb-article noise prefixes (`create-a-`, `build-a-`,
  `add-a-`, `actually-`, …) so similar intents don't clump

---

## Curate — the curator

`orchestration/skill_curator.py`. Runs **on mission termination** when
the mission ends with `reason == satisfied`.

- **Rate-limited** to once per 24h per project (state in
  `~/.resonant/projects/<hash>/curator/`). The cheap rate-limit check
  happens *before* any thread is spawned.
- Enumerates skills with `include_deprecated=True` (the curator's whole
  job is finding deprecated ones — the read paths default to
  `include_deprecated=False`).
- Archives skills that `is_deprecated()` **and** `is_curator_touchable()`
  — i.e. stale, agent-created, not pinned.
- Archival is a move to `_archive/`, with `_archive_reason.txt`. Never
  a delete.

**Deferred:** model-driven umbrella consolidation (a forked agent that
merges narrow sibling skills into broader umbrellas). Spec is in
`PLAN-SELF-IMPROVEMENT.md`; deferred until enough agent skills
accumulate to stress-test it.

---

## Read side — the loader

`orchestration/skill_loader.py`. Wired into the runtime in **v0.6.3a2**
(before that it was built but never called — dead code).

### Matching — `match_skills_for_query`

1. **Pinned skills go in first** — global + project pinned, regardless
   of token overlap. The user pinned them; they always make the cut.
2. **Token-overlap matches** via `find_matching_skills` (Jaccard
   similarity over the pre-tokenized `tokens` bag). `partial_threshold`
   is lowered to `min_score` (0.05) so the loader's loose
   "worth-surfacing" gate replaces the matcher's strict 0.6
   "auto-substitute" gate.
3. Capped at `max_skills` (default 8 — ~250 prompt tokens).

### Injection — `build_skill_context`

The single entry point the daemon uses (v0.6.3a2):

- `match` + `format_skills_for_prompt` in one call
- returns `SkillContext(block, skill_ids, loaded)`
- **best-effort by contract** — a matcher exception logs and returns
  an empty context; mission dispatch is never broken

### Where it plugs in — `autonomous_factory.py::dispatch_item`

Per roadmap item, gated by `enable_skill_loader` (default `True`):

1. match skills against the item's goal text
2. append the formatted block to the planner goal
3. `mark_skill_surfaced` every surfaced skill
4. emit a `skill_context_loaded` telemetry event

### `mark_skill_surfaced` vs `record_skill_use`

A subtle but load-bearing distinction (v0.6.3a2):

- **`record_skill_use(success=...)`** — bumps `success_count` /
  `fail_count` *and* `last_used_at`. For *measured outcomes*.
- **`mark_skill_surfaced`** — bumps **only** `last_used_at`. For
  *surfacing into context*.

Surfacing is not a quality signal — a skill can be surfaced and
ignored. But it *is* evidence the skill is still relevant, so it must
reset the curator's 90-day staleness clock. Without `mark_skill_surfaced`,
every agent skill would auto-deprecate 90 days after extraction even
while being surfaced into every mission — the loop would silently
collapse. So: bump the staleness clock, never the quality counts.

---

## How it's wired into the daemon

`gui/autonomous_loop.py` defines `DaemonHooks` — injectable I/O so the
`AutonomousMissionDaemon` is testable without booting real services.
Three skill-related hooks:

| Hook | Fires | Wired by |
|---|---|---|
| `extract_skill_hook` | after `_run_full_reflect`, when `verdict == satisfied` and not overridden | v0.6.1a1 |
| `queue_curation_hook` | in `_emit_stop`, when `reason == satisfied` | v0.6.1a1 |
| skill loader | inside `dispatch_item` (factory) | v0.6.3a2 |

`build_autonomous_mission_hooks` (`autonomous_factory.py`) connects all
three to real implementations. Flags (all default `True`):
`enable_skill_extraction`, `enable_skill_curator`, `enable_skill_loader`.

The extract + curator hooks run in **daemon threads** so a slow model
session or curator pass never pins the iter loop. The hooks use module
attribute access (`sme.extract_skill_from_iter`) rather than
closure-captured imports so `unittest.mock.patch` works in tests.

### F1 — why the grill must not self-execute (v0.6.3a1)

The v0.6.2 field run found a silent loop failure: given a *concrete
one-shot instruction* ("append a line to file X"), the grill agent
recognized it as "not a feature" and **did the work itself** with
`file_edit` + `bash`. The mission stuck in `drafting` and never
entered the iter loop — so `extract_skill_hook` never fired. The loop
missed the simplest, most common class of mission.

Fix: **Rule 0** in the grill prompt (`orchestration/grill_me.py`) —
"you are an INTERVIEWER, never an IMPLEMENTER." The grill may use
read-only tools for research but must never mutate; even a one-shot
task gets a minimal spec so it goes through the loop. This is a
prompt-level fix; structural enforcement (stripping mutating tools
from the grill session) is a deferred hardening.

---

## Surfaces

### CLI — `resonant-skill` (`orchestration/skill_cli.py`)

| Command | Purpose |
|---|---|
| `list [--scope/--created-by/--pinned/--json]` | enumerate live skills |
| `list --archived` | enumerate the `_archive/` folder |
| `view <id> [--json]` | print metadata + `procedure.md` |
| `pin` / `unpin <id>` | toggle the durability flag |
| `archive <id>` | manual archival (refuses bundled/user/pinned) |
| `restore <id> [--force]` | bring an archived skill back |
| `curate [path] [--dry-run]` | run a curator pass now (skips rate limit) |
| `promote <id> --project-path <p>` | elevate project → global |
| `demote <id> --project-path <p>` | move global → project |
| `ingest-field-obs <path> [--force/--dry-run]` | field-obs docs → user skills |

Bundled skills auto-install on every CLI invocation (idempotent).

### GUI

- **Skills sidebar group** (v0.6.2a3) — below Missions; rows show
  `[PIN]` markers, scope + provenance chips; click → detail modal with
  the full `procedure.md` body + pin/archive controls. Backed by WS
  commands `skill_list` / `skill_view` / `skill_pin_toggle` /
  `skill_archive`.
- **Iter-card chip** (v0.6.3a3) — a "🛠 N skills" chip on each
  autonomous-mission iter card, rendered from the `skill_context_loaded`
  event. Each skill id is a clickable pill → opens the detail modal.
  This makes the read side of the loop *visible*.

---

## Field-observation ingest (v0.6.2a5)

`orchestration/field_observation_ingest.py`. Human-authored
field-observation docs (`docs/field-observations/*.md`) ingest directly
into the skill library — no model call, no extraction:

- `created_by="user"` (provenance gate — curator-exempt)
- `pinned=True` (durability gate — auto-deprecation-exempt)
- `scope="global"` (field observations are usually cross-project)

`ingest_field_observation_dir` walks `*.md`, skipping planning docs
(`NEXT-RUN` / `TODO` / `DRAFT` heuristics). This makes every field run
auto-compounding: write the doc, ingest it, and that knowledge becomes
durable, matchable context for future missions.

---

## Design decisions & rationale

| Decision | Why |
|---|---|
| Archive-only destruction | A mistaken auto-deprecation must be recoverable. `_deprecated/` + `_archive/` + `_archive_reason.txt` give a full audit trail. |
| Provenance gate on the curator | Human knowledge (bundled refs, hand-authored skills) must never be silently archived by a heuristic. |
| The extractor model can say "(no skill)" | Library quality beats library size. Most iters produce nothing reusable, and that is the correct outcome. |
| Two extractors, not one | Plan-graphs and mission-iters are genuinely different units of work with different state. Forcing one extractor would lose fidelity. |
| Keyword Jaccard, not embeddings | No new heavy dependency. Embedding-based matching is a deliberate v0.7-class change (adds a sentence-transformer dep). |
| `mark_skill_surfaced` ≠ `record_skill_use` | Surfacing keeps a skill alive without lying about its success rate. |
| Hooks run in daemon threads | A slow extractor/curator must never pin the iter loop or delay terminal events. |
| Best-effort everywhere | A skill-subsystem failure must degrade to "no skills this run", never break a mission. |

---

## What's wired vs deferred

**Wired and working (v0.6.3):**
- Write side — both extractors, hook-fired on satisfied iters
- Curator — hook-fired on satisfied mission termination, rate-limited
- Read side — loader wired into `dispatch_item`, skills injected
- Provenance + pinning gates throughout
- CLI (11 subcommands), GUI sidebar + detail modal + iter-card chip
- Field-observation ingest

**Deferred:**
- **Model-driven umbrella consolidation** in the curator
- **Embedding-based matching** (currently keyword Jaccard) — v0.7-class
- **Grill tool-restriction hardening** — structurally prevent
  self-execution, not just prompt-discourage it
- **Closed-circuit field validation** — two missions in one project
  where the second provably references skills the first produced

---

## File map

| File | Role |
|---|---|
| `orchestration/skills.py` | `Skill` model, storage, `list/load/save`, archive/restore, promote/demote |
| `orchestration/bundled_skills/` | shipped reference skills + `install_bundled_skills` |
| `orchestration/skill_extraction.py` | plan-graph extractor + `slugify` |
| `orchestration/skill_mission_extraction.py` | mission-iter extractor |
| `orchestration/skill_curator.py` | curator pass + 24h rate limit |
| `orchestration/skill_loader.py` | matcher, prompt formatting, `build_skill_context` |
| `orchestration/skill_cli.py` | `resonant-skill` console script |
| `orchestration/field_observation_ingest.py` | field-obs docs → user skills |
| `orchestration/grill_me.py` | grill prompt (Rule 0 — F1 fix) |
| `gui/autonomous_loop.py` | `DaemonHooks`, hook fire points |
| `gui/autonomous_factory.py` | `build_autonomous_mission_hooks`, loader wiring |
| `gui/app.py` | WS commands + payload helpers for the GUI surface |
| `gui/static/app.js` | Skills sidebar, detail modal, iter-card chip |

## Test coverage

The subsystem is heavily tested — the relevant files:
`test_skills.py`, `test_skills_provenance.py`, `test_skill_naming.py`,
`test_skill_curator.py`, `test_skill_loader.py`,
`test_skill_loader_wiring.py`, `test_skill_mission_extraction.py`,
`test_skill_cli.py`, `test_skill_promotion.py`,
`test_skill_archive_restore.py`, `test_gui_skill_endpoints.py`,
`test_autonomous_factory_skills.py`, `test_field_observation_ingest.py`,
`test_grill_me_rigorous.py`, `test_skills_daemon_integration.py`.

## Version history

| Version | Self-improvement-loop delivery |
|---|---|
| v0.6.0 | data layers — provenance, bundled skills, both extractors, curator, loader; daemon hook plumbing |
| v0.6.1 | productionization — hook factory wires extract+curator; `resonant-skill` CLI; promote/demote |
| v0.6.2 | field-tested — skill naming fix; GUI Skills sidebar; archive/restore; field-obs ingest |
| v0.6.3 | loop closed — F1 grill Rule 0 (extractor fires); loader wired into runtime; iter-card chip |

See the matching `docs/v0.6.*-release-notes.md` for per-alpha detail.
