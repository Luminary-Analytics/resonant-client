# PLAN — Organic AI Orchestration

> **Foundation cluster (pre-v0.2.0).** Shipped state preserved here for reference. See [ROADMAP.md](ROADMAP.md) → "Post-refocus state (v0.3.x → v0.5.9)" for the capability tracks that built on this foundation.
>
> Status: ✅ Shipped · Last updated: 2026-04-27 · 5 / 5 phases · 775 tests passing (+87 new)

## Shipped (2026-04-27)

| Phase | Result |
|---|---|
| **1** Plan-graph data model | `resonant_client/orchestration/plan_graph.py` + `persistence.py`. `PlanNode` / `PlanGraph` with mutable DAG semantics, snapshot/restore, drop-log with reasons. Per-project storage at `~/.resonant/projects/<sha1[:12]>/plans/{current,snapshots}/`. **27 tests** in `test_plan_graph.py`. |
| **2** Specialist dispatch | `specialists.py` registry (explore / implement / verify / repair / research / plan) with per-role tool allowlists + system blocks; `walker.py` `GraphWalker` runs nodes in dep order, auto-spawns verify on confidence drop, auto-spawns repair + re-verify on `verify revise`, caps repair attempts and total nodes. Cancellable via `threading.Event`. **25 tests**. |
| **3** Skill library | `skills.py` (Voyager-inspired storage at `~/.resonant/skills/{global,project,stack}/`), `skill_extraction.py` (auto-distill from successful plan-graphs), `skill_manifest.py` (per-project `.resonant/skills.toml` with required/optional/install). Token-overlap similarity matching, deprecation rules (90-day unused or >50% fail-rate). **26 tests**. |
| **4** Plan-graph viz | `static/plan_graph_view.js` self-contained renderer (depth-based layout, status-colored cards, dashed dep edges vs solid tree edges, click-to-detail, soft-checkpoint toast with countdown). New "Plan" tab in the preview panel with toolbar (Pause / History / Branch). |
| **5** Full Autonomy enforcement | `autonomy.py` irreversibility floor (force-push to protected branches, `rm -rf` outside project, writes to ~/.ssh/~/.aws/etc, destructive SQL on non-test DBs, external message sends, budget cap). `audit.py` append-only JSONL log per intent at `~/.resonant/projects/<hash>/intents/<intent-id>/audit.jsonl` with secret redaction + truncation. **37 tests**. |

**Verification:** `pytest tests/ -q` → 775 passing. Manual: plan-graph viz renders synthetic snapshots correctly; soft-checkpoint countdown + Pause work; tab toggle is mutually exclusive between Browser and Plan panes.

## Why this exists

The harness migration cleaned up *where* state lives. This plan replaces *what* the orchestrator actually does. The "sprint contract" pattern was borrowed from human Agile cycles where the bottleneck was coordination between people. With AI, the bottleneck is **coherent decomposition and recovery** — a different problem that wants different primitives.

## Five primitives

| # | Primitive | Replaces |
|---|---|---|
| 1 | **Intent** — durable user goal, mutable as understanding grows | `SprintContract.objective` |
| 2 | **Plan-graph** — DAG of nodes with `goal`, `status`, `confidence`, `dependencies`. Mutable: nodes added / pruned / rewritten as work proceeds | `ProgressState.current_phase` + sprint sequencing |
| 3 | **Specialist agent on demand** — per-node specialization (`explore` / `implement` / `verify` / `repair` / `research` / `plan`). Spawned when needed, killed when done | Fixed `session_role` (planner/generator/evaluator) |
| 4 | **Continuous reflection** — per-node confidence; threshold drops auto-spawn verify/repair siblings | `EvaluatorReport` post-hoc verdict |
| 5 | **Skill library** — reusable verified procedures, auto-extracted from successful plan-graphs, queried before decomposing from scratch | (new — Voyager-inspired) |

## Two non-negotiable tenets

### Full Autonomy by default

The agent does anything the user could do, without asking. The plan-graph viz is the intervention surface, not a gate.

**Runs without prompting:**
- All file edits / writes / deletes inside the project
- All bash, MCP, web fetches, container starts, dev-server launches
- All git operations except those in the "Hard checkpoint" list
- All sub-agent spawns and skill loads

**Hard checkpoints (always pauses):**
- `git push --force` to protected branches (main, master, prod, release/*)
- `git reset --hard` with uncommitted changes
- `rm -rf` outside the project root
- Spending past the configurable budget
- External messages on the user's behalf (email, slack)
- Destructive SQL on non-test DB
- Editing `~/.ssh/`, `~/.aws/`, `~/.kube/`, OS-level config

**Soft checkpoints (5s countdown, configurable):**
- Plan rewrites > 30% of nodes
- Single node touching > 5 files
- Low-confidence node entering the critical path
- Any destructive-flagged operation

Set `countdown_seconds = 0` for silent, `-1` for always-pause.

### Skills travel via manifest, implementations stay per-machine

Skills live in `~/.resonant/skills/` (out of repo). Projects declare what they need via `.resonant/skills.toml`:

```toml
[required]
skills = ["fix-python-import-error", "add-mcp-server", "deploy-to-vercel@>=1.2"]

[install]
auto = true
warn-on-missing = true
```

## Context

Read these before starting any phase:

- [ARCHITECTURE.md](ARCHITECTURE.md) — module reference
- [PLAN-HARNESS-MIGRATION.md](PLAN-HARNESS-MIGRATION.md) — what just shipped and why some pieces survived
- [resonant_client/harness/orchestrator.py](resonant_client/harness/orchestrator.py) — the existing background cycle (will be reshaped, not deleted)
- [resonant_client/harness/state.py](resonant_client/harness/state.py) — current dataclasses (kept; we add new ones alongside)
- [resonant_client/harness/service.py](resonant_client/harness/service.py) — system-prompt assembly (the slim version)
- [resonant_client/engine/agents.py](resonant_client/engine/agents.py) — Task tool sub-agent spawning (foundation for specialist dispatch)

## Tasks

### Phase 1 — Plan-graph data model

**Files:**
- NEW: `resonant_client/orchestration/__init__.py`
- NEW: `resonant_client/orchestration/plan_graph.py` — `PlanNode`, `PlanGraph`, snapshot/restore
- NEW: `resonant_client/orchestration/persistence.py` — read/write `~/.resonant/projects/<hash>/plans/`
- NEW: `tests/test_plan_graph.py`

**Action:**
1. `PlanNode` dataclass:
   ```python
   @dataclass
   class PlanNode:
       id: str                              # ULID
       intent_id: str                       # parent intent
       goal: str                            # one-sentence node objective
       specialization: str                  # "explore" | "implement" | "verify" | "repair" | "research" | "plan"
       status: str                          # "pending" | "running" | "done" | "blocked" | "abandoned"
       confidence: float                    # 0.0 – 1.0, updated as the node works
       parent_id: Optional[str]             # tree edge
       depends_on: list[str]                # DAG edges (must be done before this can run)
       skill_id: Optional[str]              # if this node was loaded from a skill
       agent_session_id: Optional[str]      # which specialist owned this
       result: Optional[dict]               # {summary, touched_files, tool_calls_count, ...}
       created_at: float
       updated_at: float
       audit_log: list[dict]                # decisions made on this node
   ```
2. `PlanGraph`:
   - holds nodes by id
   - methods: `add_node`, `prune_node`, `rewrite_subtree`, `next_runnable()` (returns nodes whose deps are all done), `mark_done`, `mark_blocked`
   - `to_dict()` / `from_dict()` for persistence
   - `snapshot()` returns a deep-copy with timestamp; `restore(snapshot)` swaps in
3. `persistence.py`:
   - `save_graph(graph, project_path)` → `~/.resonant/projects/<hash>/plans/current.json`
   - `snapshot_graph(graph, project_path)` → `.../snapshots/<ts>.json`
   - `list_snapshots(project_path)` → list of (ts, summary, diff_count)
   - `restore_snapshot(project_path, ts)` → returns a `PlanGraph`
   - Auto-purge snapshots older than configurable `snapshot_retention_days` (default 30)
4. Tests cover: build a graph, run nodes in dependency order, snapshot, mutate, restore, snapshot purge.

**Verify:**
```bash
pytest tests/test_plan_graph.py -v
python -c "
from resonant_client.orchestration.plan_graph import PlanGraph, PlanNode
g = PlanGraph(intent='ship dark mode')
a = PlanNode(id='a', intent_id=g.intent_id, goal='css vars', specialization='implement', status='pending', confidence=1.0, parent_id=None, depends_on=[])
g.add_node(a)
print('runnable:', [n.id for n in g.next_runnable()])
"
```

**Done when:** Graph round-trips JSON without loss, snapshot/restore works, `next_runnable()` honors DAG deps, retention purge runs.

---

### Phase 2 — Specialist agent dispatch

**Files:**
- NEW: `resonant_client/orchestration/specialists.py` — registry of specializations + their system-prompt builders
- MODIFY: `resonant_client/harness/orchestrator.py` — `HarnessOrchestrator` becomes a thin wrapper around the new graph-walker (kept for back-compat; the actual loop moves to `orchestration/walker.py`)
- NEW: `resonant_client/orchestration/walker.py` — `GraphWalker` runs nodes by spawning the right specialist for each
- MODIFY: `resonant_client/engine/session.py` — accept a `specialization` field that drives which system-prompt block ships
- NEW: `tests/test_specialists.py`, `tests/test_graph_walker.py`

**Action:**
1. Specialization registry:
   ```python
   SPECIALIZATIONS = {
       "explore":   {"system_block": "...", "tool_allowlist": READ_ONLY_TOOLS, "max_steps": 8},
       "implement": {"system_block": "...", "tool_allowlist": ALL_TOOLS,        "max_steps": 24},
       "verify":    {"system_block": "...", "tool_allowlist": READ_ONLY_TOOLS | TEST_TOOLS, "max_steps": 12},
       "repair":    {"system_block": "...", "tool_allowlist": ALL_TOOLS,        "max_steps": 16},
       "research":  {"system_block": "...", "tool_allowlist": WEB_TOOLS | READ_ONLY_TOOLS, "max_steps": 10},
       "plan":      {"system_block": "...", "tool_allowlist": READ_ONLY_TOOLS, "max_steps": 8},
   }
   ```
2. `GraphWalker.run(graph, intent)`:
   - Loop: pick `next_runnable()` → spawn specialist for each node (parallel where deps allow) → collect result → update node status + confidence → on confidence drop below threshold, auto-add a `verify` sibling → on `verify` failure, auto-add `repair` child → repeat until all nodes done/abandoned.
   - Yield orchestration events (`node.start`, `node.done`, `node.confidence_changed`, `plan.rewrite`) for the UI.
3. `Session` gets a `specialization` parameter; system prompt assembled by combining (a) AGENTS.md project conventions + (b) the specialization's system block + (c) the active node's goal.
4. `HarnessOrchestrator` retained as a back-compat wrapper that builds a 3-node plan-graph (planner → generator → evaluator) and runs it through `GraphWalker`. Existing tests keep passing.

**Verify:**
```bash
pytest tests/test_specialists.py tests/test_graph_walker.py -v
pytest tests/ -q  # confirm no regression
```

**Done when:** Each specialization produces a distinct system prompt; `GraphWalker` runs a multi-node graph with deps in the right order; auto-verify/repair fires on low confidence; old `HarnessOrchestrator` tests still pass.

---

### Phase 3 — Skill library

**Files:**
- NEW: `resonant_client/orchestration/skills.py` — `Skill` dataclass, library reader/writer
- NEW: `resonant_client/orchestration/skill_extraction.py` — extract a skill from a completed plan-graph
- NEW: `resonant_client/orchestration/skill_manifest.py` — read `.resonant/skills.toml` per project, install/check status
- NEW: `tests/test_skills.py`, `tests/test_skill_manifest.py`
- MODIFY: `resonant_client/orchestration/walker.py` — query skill library at intent start; load high-match skills as subtrees

**Action:**
1. Storage layout:
   ```
   ~/.resonant/skills/
     global/<skill-id>/
       skill.json           # name, description, triggers, prereqs, success/fail counts, embedding
       procedure.md         # human-readable steps
       verification.md      # success criteria
       examples/            # past plan-graphs that used this
     project/<project-hash>/<skill-id>/
     stack/<stack-sig>/<skill-id>/
   ```
2. `Skill` dataclass:
   ```python
   @dataclass
   class Skill:
       id: str                     # slug
       name: str
       description: str
       scope: str                  # "global" | "project" | "stack"
       triggers: list[str]         # trigger phrases / situations
       prerequisites: list[str]    # other skill IDs this depends on
       success_count: int
       fail_count: int
       last_used_at: float
       version: str
       embedding: list[float]      # for similarity search (computed lazily)
   ```
3. Discovery flow in `GraphWalker`:
   - On new intent, embed the intent text, search top-k similar skills (cosine).
   - **High match (>0.85)**: load skill's pre-built subtree as a starting point.
   - **Partial match (0.6–0.85)**: use as scaffold, decompose gaps from scratch.
   - **No match**: decompose from scratch.
4. Auto-extraction on success:
   - When a plan-graph completes with `confidence > 0.8` and `> 3` non-trivial nodes, extract a candidate skill.
   - Stash in `~/.resonant/skills/global/<id>/` with `success_count = 1`.
   - User can review/curate via a Skills view (Phase 4).
5. Manifest reader:
   - On project load, read `.resonant/skills.toml`.
   - For each `required` skill, check `~/.resonant/skills/global/<id>/` exists.
   - Missing → status banner in sidebar; if `auto = true` and a registry is wired (future), attempt install.
   - "Save skill set" command writes the manifest based on currently-used skills.
6. Decay rules: skills unused for 90 days OR fail-rate > 50% over last 10 uses → auto-deprecate (move to `~/.resonant/skills/_deprecated/`).

**Verify:**
```bash
pytest tests/test_skills.py tests/test_skill_manifest.py -v
# Manual: complete a multi-step plan-graph → check ~/.resonant/skills/global/ for an auto-extracted skill
# Manual: write .resonant/skills.toml with a missing skill → load project → see banner
```

**Done when:** Auto-extraction produces a usable skill JSON; high-match lookup loads a skill into a fresh plan-graph; manifest banner surfaces missing skills; decay/deprecation works.

---

### Phase 4 — Plan-graph visualization

**Files:**
- NEW: `resonant_client/gui/static/plan_graph_view.js` — D3 / vanilla SVG renderer
- MODIFY: `resonant_client/gui/templates/index.html` — preview panel: add `<div id="plan-graph-viz">` panel + tab toggle
- MODIFY: `resonant_client/gui/static/styles.css` — node colors per status, edge styles, soft-checkpoint toast
- MODIFY: `resonant_client/gui/app.py` — push `plan.event` over WebSocket
- MODIFY: `resonant_client/gui/static/app.js` — wire `plan.event` handler, plan-history modal, restore-snapshot flow

**Action:**
1. Renderer:
   - Force-directed DAG layout (nodes as cards with goal/specialization/status pill, edges as arrows).
   - Live updates as `node.start` / `node.done` / `confidence_changed` events arrive.
   - Color encoding: pending=gray, running=brand-color (pulsing), done=ok-green, blocked=warn-orange, abandoned=dim-strikethrough.
   - Click a node → side panel: full goal, audit log, tool calls made, files touched, restore-from-here button.
2. Top-of-viz toolbar:
   - **Pause** (global pause; halts new node spawns; running nodes finish their step then hold)
   - **Plan history** (modal: timeline of snapshots with diff per entry, Restore button)
   - **Branch from here** (fork plan-graph + active intent into a new sibling intent for what-if exploration)
   - **Confidence floor** (slider, persists per-project)
3. Soft-checkpoint UX:
   - When orchestrator triggers a soft checkpoint, render a toast at top of viz:
     - *"Rewriting plan: 4 dropped, 3 added · auto-continuing in 5s"*
     - **Pause** / **Show diff** buttons
   - 5s countdown; if no action → continues.
4. Hard-checkpoint UX: real modal, blocks the orchestrator, requires explicit Approve/Reject.

**Verify:**
- Manual: start a multi-node intent → watch nodes pop into the viz, transition through states, completion animations.
- Manual: trigger a plan rewrite → see toast + countdown.
- Manual: open plan history → restore a past snapshot.
- Manual: click "Branch from here" on a node → fork creates a new intent with that subtree as the root.

**Done when:** Viz reflects real-time orchestration state; soft + hard checkpoints behave as specified; plan history + restore round-trips correctly; branching produces a usable fork.

---

### Phase 5 — Full Autonomy enforcement

**Files:**
- MODIFY: `resonant_client/engine/sandbox.py` — `IRREVERSIBILITY_FLOOR` set + check helper
- MODIFY: `resonant_client/engine/tools.py` — pre-execution check for floor violations
- NEW: `resonant_client/orchestration/audit.py` — append-only audit log per intent
- MODIFY: `resonant_client/gui/static/app.js` — Audit-log view (per-intent timeline of every decision)
- MODIFY: `resonant_client/gui/settings.py` — `general.budget_usd_max`, `general.autonomy_protected_branches`, `general.autonomy_external_paths`

**Action:**
1. Codify the floor:
   ```python
   IRREVERSIBILITY_FLOOR_RULES = [
       check_force_push_to_protected_branch,
       check_hard_reset_with_uncommitted,
       check_rm_rf_outside_project,
       check_budget_exceeded,
       check_external_message_send,
       check_destructive_sql_non_test,
       check_writes_to_protected_paths,  # ~/.ssh, ~/.aws, ~/.kube
   ]
   ```
2. `tools.execute_tool()` runs each floor check before dispatch. On hit → emit `tool.permission_required` (existing event, hard pause).
3. Settings:
   - `general.budget_usd_max` (default `5.00`) — hard floor on cumulative API spend per intent.
   - `general.autonomy_protected_branches` (default `["main", "master", "prod", "release/*"]`).
   - `general.autonomy_external_paths` (default OS-level config dirs).
4. Audit log: every node decision (specialist picked, skill loaded, plan rewrite, confidence change, tool call) is appended to `~/.resonant/projects/<hash>/intents/<intent-id>/audit.jsonl`.
5. Audit-log UI: per-intent timeline with filters (decisions only, tool calls only, plan changes only).

**Verify:**
```bash
pytest tests/ -q
# Manual: try `git push --force origin main` via the agent → hard checkpoint fires
# Manual: try editing ~/.ssh/config via the agent → hard checkpoint fires
# Manual: rapid file edits inside project → no prompts (autonomy preserved)
# Manual: check ~/.resonant/projects/<hash>/intents/<id>/audit.jsonl after a run → all decisions recorded
```

**Done when:** Hard-floor checks fire reliably; soft checkpoints respect their countdown; everything else runs without prompts; audit log captures every decision for forensic replay.

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# All phases
pytest tests/ -q  # 660 + new tests

# Sanity: a real intent end-to-end
python -m resonant_client.gui.server --port 8765 --browser
# Type intent: "add dark mode toggle to settings page"
# Watch: plan-graph populates → specialists fire in dep order → soft checkpoint when scope expands → done
```

## Success criteria

- [ ] Five primitives implemented (intent / plan-graph / specialist / reflection / skill library)
- [ ] Default-on full autonomy: no prompts for normal actions; hard checkpoints only on irreversibility floor
- [ ] Soft checkpoints with configurable countdown; default 5s
- [ ] Plan-graph viz shows live orchestration state; rollback works via snapshot restore
- [ ] Skill library auto-extracts on success; manifest declares per-project deps
- [ ] Audit log captures every decision; replayable from disk
- [ ] All 660+ tests still green plus new coverage for each phase

## Out of scope (deferred)

- **Skill registry** (centralized hosting / discovery of skills) — depends on a server we don't have. For now, skills are exported as JSON files and shared via copy-paste.
- **Multi-user collaboration on intents** — single-user IDE remains the focus.
- **Cross-machine plan-graph sync** — same as harness state, intentionally per-machine.
- **Speculative parallel execution of competing plan branches** — interesting (Tree-of-Thoughts style) but adds complexity. Defer until v2.

## Future / nice-to-haves (post-shipping)

| Idea | Where it would go | Why later |
|------|-------------------|-----------|
| LLM-driven plan critique ("score this plan-graph for completeness before executing") | `orchestration/walker.py` pre-flight | Polish — orchestrator already auto-verifies on confidence drops |
| Skill versioning + semver pin in manifest | `skill_manifest.py` | Need a registry to make versioning meaningful |
| Branch-and-merge: run two competing plan branches in parallel, pick winner | `walker.py` + viz | Cool but expensive; needs careful cost guards |
| Confidence calibration learning ("this specialist tends to over-confidence on TS code; adjust") | New `orchestration/calibration.py` | Real differentiator but only valuable after enough data |
| Voice-driven intent input ("hey resonant, add dark mode") | `gui/static/app.js` voice path (already exists) | Tie-in, not core |

## Output

When all 5 phases ship, write `SUMMARY-ORGANIC-ORCHESTRATION.md` covering:
- Phases shipped + commit hashes
- Number of "sprint" / "harness" references replaced
- Test count delta
- Any deviations from this plan
- Screenshot of the live plan-graph viz
- One example skill auto-extracted from a real run

Then update `ROADMAP.md`: mark this row as ✅ Shipped, link to the SUMMARY.
