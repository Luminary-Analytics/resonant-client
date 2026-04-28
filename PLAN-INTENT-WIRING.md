# PLAN — Intent Wiring (Live Orchestrator Flow)

> Status: ✅ Shipped · 5 / 5 phases · 815 tests passing (+40 new) · 2026-04-27

## Shipped (2026-04-27)

| Phase | Result |
|---|---|
| **1** Specialist runner | `orchestration/runner.py` — `LocalSpecialistRunner` builds a per-node `Session` with the right specialist profile + tool allowlist + step budget, drives it, infers confidence from session outcome, parses planner subgoals + verifier verdicts from JSON code-fences. **15 tests** |
| **2** IntentService | `orchestration/intent_service.py` — owns active intents, drives `GraphWalker` in worker threads, persists graph + snapshots on every mutation, integrates skill auto-extraction on completion. Cancel + pause + resume + restore all wired. **12 tests** |
| **3** WS + UI kickoff | New WebSocket commands (`intent_start` / `cancel` / `pause` / `resume` / `list_snapshots` / `restore_snapshot`); thread → asyncio bridge for streaming events. Frontend: "Plan this" button in input footer + `/plan` slash-prefix detection + snapshot history modal. Plan-graph viz auto-opens on intent kickoff |
| **4** Floor + audit | `engine/tools.py` `execute_tool` now runs `autonomy.check_floor` before dispatch; floor violations come back as `ToolResult(is_error=True, metadata.floor_violation)`. `Session` plumbs `project_path` + `settings` through. Per-tool-call audit logging happens via the runner's session-event hook. **10 integration tests** |
| **5** End-to-end | Stub-backend e2e test drives a 4-node intent (plan → 3 subgoals) through the full `IntentService` → `GraphWalker` → `LocalSpecialistRunner` → real `Session` pipeline; asserts all-DONE + audit lifecycle + skill auto-extracted. **3 tests** |

**Verification:** `pytest tests/ -q` → 815 passing. UI loads without console errors; "Plan this" button + Plan tab + snapshot modal all present and wired.

## Why this exists

`PLAN-ORGANIC-ORCHESTRATION.md` shipped the data structures + UI shell. Today they're inert: the GUI's existing message flow still drives one-shot `Session.run` calls and never touches a plan-graph. This plan wires it through.

After this lands:
- A user message can kick off an **intent** that runs through `GraphWalker`, spawning specialists per node.
- The Plan tab in the preview panel shows real activity, not synthetic fixtures.
- The irreversibility floor actually fires before risky tool dispatch.
- Every decision is captured in the per-intent audit log for replay.
- The skill library auto-extracts on successful runs.

## Design choices baked in

1. **Intent flow is opt-in alongside the existing chat flow.** A user can type a casual question and still get the current one-shot `Session.run` path. Intent flow activates when the user clicks "Plan this" (or types a `/plan` prefix). Existing harness path stays unchanged.
2. **Sequential specialist execution** for v1. Multiple runnable nodes run one at a time. Parallelism can come later — Ollama model-loading cost makes serial the right default.
3. **Background thread per intent** — the orchestrator runs in a worker thread; the WebSocket loop streams events back. Cancellation via `threading.Event`.
4. **Initial graph bootstrap = single `plan` node.** The walker runs it, the planner specialist returns subgoals, the walker expands. No "decompose first then walk" two-phase awkwardness.
5. **Persistence is eager.** Every node-status change saves the graph; every plan rewrite snapshots first. The user can always rollback or resume.

## Phases

### Phase 1 — Specialist runner adapter

**Files:**
- NEW: `resonant_client/orchestration/runner.py` — `LocalSpecialistRunner`
- MODIFY: `resonant_client/engine/session.py` — `Session.__init__` accepts `tool_allowlist: Optional[set[str]]`; if set, filters `AGENT_TOOLS` before the loop starts
- NEW: `tests/test_runner.py`

**Action:**
1. `LocalSpecialistRunner(backend, project_path, settings)` is callable: `(node, graph) -> SpecialistResult`.
2. Inside, it:
   - Resolves the `SpecialistProfile` for `node.specialization`
   - Builds the system prompt via `assemble_system_prompt(...)` with project conventions (AGENTS.md / RESONANT.md) + node goal + intent + extra context (results of dependency nodes)
   - Filters tools via `filter_tools_for_specialist`
   - Spins up a fresh `Session` with the filtered tools, that system prompt, and the node's goal as the user message
   - Runs the session with `max_steps = profile.max_steps`
   - Captures the final assistant text + tool-call count + any errors → builds `SpecialistResult`
3. **Confidence inference** (heuristic, not magic):
   - `1.0` → session ended cleanly with no errors and no plan-rewrite signals
   - `0.7` → some tool errors occurred but the model recovered
   - `0.4` → session hit max_steps or had repeated errors
   - `0.0` → session crashed; status `BLOCKED`
4. **Plan specialist parsing**: when the runner sees `node.specialization == "plan"`, it parses the model's final text for a JSON code-fence containing a `subgoals` array (`[{"goal":..., "specialization":..., "depends_on":[indices]}]`). If parsing fails, returns confidence 0.4 with no subgoals (walker will fall back to direct implementation).
5. **Verify specialist parsing**: same idea, but the model emits a `verdict` field (`pass`/`revise`/`blocked`) and a `findings` array.

**Verify:**
```bash
pytest tests/test_runner.py -v
# Manual sanity:
python -c "
from unittest.mock import MagicMock
from resonant_client.orchestration.runner import LocalSpecialistRunner
from resonant_client.orchestration import PlanGraph, PlanNode, NodeSpecialization, new_node_id
g = PlanGraph.new('test')
n = PlanNode(id=new_node_id(), intent_id=g.intent_id, goal='read README', specialization=NodeSpecialization.EXPLORE)
g.add_node(n)
backend = MagicMock()  # stub
runner = LocalSpecialistRunner(backend=backend, project_path='/tmp', settings=None)
print('runner constructed:', runner)
"
```

**Done when:** A unit test using a stub backend can drive a small graph (1 node) through `LocalSpecialistRunner` and get a `SpecialistResult` back with the right status. `Session` honors a tool allowlist so the model doesn't see disallowed tools at all.

---

### Phase 2 — Intent service

**Files:**
- NEW: `resonant_client/orchestration/intent_service.py` — `IntentService`
- MODIFY: `resonant_client/gui/app.py` — `AppState` instantiates one, holds active intents
- NEW: `tests/test_intent_service.py`

**Action:**
1. `IntentService(project_path, backend, settings, on_event)`:
   - Methods: `start_intent(text) -> intent_id`, `cancel(intent_id)`, `pause(intent_id)`, `resume(intent_id)`, `restore_snapshot(intent_id, ts_ms)`, `list_active() -> [intent_id, ...]`
   - Holds: `dict[intent_id, _ActiveIntent]` where `_ActiveIntent` bundles the graph, walker, cancel_event, worker thread, and lock
   - On `start_intent`: bootstraps a `PlanGraph` with one root `plan` node whose goal is the user text; saves to disk; spawns a worker thread that runs `GraphWalker.run`; returns immediately
   - Every mutation persists: `save_graph` after node updates, `snapshot_graph` before plan rewrites
2. **Event forwarding**: `IntentService` receives `WalkerEvent`s via the walker's `on_event` callback and forwards them through a thread-safe queue to `on_event` (the constructor-supplied callback). The GUI wires this to a WebSocket emitter.
3. **Audit log integration**: every state transition that matters writes to the audit JSONL. Specifically:
   - `start_intent` → `log_decision(summary="intent started", text=...)`
   - Each `node.start` → `log_decision(summary="dispatched specialist", node_id=..., specialization=...)`
   - Each `node.done` → `log_plan_change(node_id=..., change="status:done", confidence=...)`
   - `plan.rewrite` → `log_plan_change(change="rewrite", added=..., removed=...)`
4. **Skill library wiring**: when a graph completes successfully, call `extract_skill(graph)` and log the result. When loading a graph for a fresh intent, query `find_matching_skills(text)` and seed the initial plan if a high-match exists (deferred to a follow-up phase if too risky for v1).

**Verify:**
```bash
pytest tests/test_intent_service.py -v
# Stub-backend integration:
# - start_intent("test") → returns id, graph appears on disk
# - the worker fires node.start / node.done events through the callback
# - cancel(intent_id) terminates the worker promptly
```

**Done when:** `IntentService` can drive a small intent end-to-end with a stub backend; events flow through the on_event callback; cancel works; graph + audit log persist correctly.

---

### Phase 3 — WebSocket commands + UI kickoff

**Files:**
- MODIFY: `resonant_client/gui/app.py` — new WS commands
- MODIFY: `resonant_client/gui/static/app.js` — kickoff UI + event consumers
- MODIFY: `resonant_client/gui/templates/index.html` — "Plan this" button in input footer

**Action:**
1. **New WebSocket commands** (server side):
   - `intent_start { text }` → calls `IntentService.start_intent`; emits `plan.snapshot` immediately + `plan.event`s as the walker progresses; emits `intent.complete` when done
   - `intent_cancel { intent_id }` → cancel the worker; emit `intent.cancelled`
   - `intent_pause { intent_id }` → set the soft-pause flag (the walker checks between nodes); emit `intent.paused`
   - `intent_resume { intent_id }` → clear the pause flag
   - `intent_list_snapshots { intent_id }` → returns `list_snapshots(...)`; emits `plan.snapshot_list`
   - `intent_restore_snapshot { intent_id, ts_ms }` → loads the snapshot, replaces the live graph, emits a fresh `plan.snapshot`
   - `intent_branch_from { intent_id, node_id }` → forks the subtree rooted at `node_id` into a new intent; the walker for the new intent starts immediately
2. **UI kickoff**:
   - New "Plan this" button in `.input-footer-left` next to the mic button. Tooltip: "Run as intent (decompose into a plan-graph and execute)."
   - Slash-prefix detection: a message starting with `/plan ` strips the prefix and is sent as `intent_start { text: rest }` instead of the regular `message` command.
   - Sending an intent puts the chat into "intent mode" for the duration: the chat pane shows a single status card ("Running intent: 'X'") + the Plan tab opens in the preview panel.
3. **Event consumers** in app.js: `intent.complete` / `intent.cancelled` / `intent.paused` add status messages; `plan.event` / `plan.snapshot` / `plan.checkpoint` are already wired (Phase 4 of the orchestration plan).

**Verify:**
- Manual: open the app, click "Plan this", type "list the largest files in this project", confirm:
  - Plan tab opens automatically
  - A root `plan` node appears
  - It expands into subgoals
  - Specialists fire in dep order
  - Status messages appear in chat at start / complete

**Done when:** Round-trip works in the browser: type a goal → see the plan-graph populate live → see the final summary in chat. Cancel button stops the walker within ~1 second.

---

### Phase 4 — Floor enforcement + audit integration in tool dispatch

**Files:**
- MODIFY: `resonant_client/engine/tools.py` — `execute_tool` runs `check_floor` before dispatching; on violation, emits a hard-checkpoint event (re-using the existing `tool.permission` flow)
- MODIFY: `resonant_client/orchestration/runner.py` — wraps tool dispatch with audit logging
- MODIFY: `resonant_client/engine/session.py` — accepts an optional `audit_logger` callback that fires per tool call
- NEW: `tests/test_floor_integration.py` — drive a plan-graph with a tool that hits the floor; assert the violation is recorded + the user is prompted

**Action:**
1. In `execute_tool`, before any branch:
   ```python
   from resonant_client.orchestration.autonomy import check_floor
   violation = check_floor(
       tool_name=name, args=args, project_path=cwd, settings=settings,
   )
   if violation is not None:
       # Re-use the existing permission-pause UX
       return ToolResult(
           output=f"FLOOR_VIOLATION: {violation.rule}\n{violation.reason}",
           is_error=True,
           metadata={"floor_violation": violation.__dict__},
       )
   ```
   The `tool.permission` event handler in app.js already pauses for approval; we extend it to render the floor's `reason` + `suggested_action` more prominently.
2. `LocalSpecialistRunner` passes an audit logger to each Session it spawns:
   ```python
   def audit_logger(tool_name, args, result_summary, is_error, duration_ms):
       log_tool_call(project_path, intent_id,
                     tool_name=tool_name, args=args, result_summary=result_summary,
                     is_error=is_error, duration_ms=duration_ms)
   ```
3. `Session` calls the audit logger inside its tool-dispatch loop after each tool result lands.
4. Floor violations also fire `log_floor_violation(...)` so the audit JSONL captures the checkpoint event for replay.

**Verify:**
```bash
pytest tests/test_floor_integration.py -v
# Manual: have an intent attempt `git push --force origin main`. Confirm the
# permission dialog appears with the floor's reason; confirm the audit JSONL
# has a "floor_violation" entry.
```

**Done when:** Floor checks fire reliably from inside the live tool-dispatch path. Every tool call is captured in the audit log. The audit log can be replayed for any past intent.

---

### Phase 5 — End-to-end verification + skill auto-extraction

**Files:**
- NEW: `tests/test_intent_e2e.py` — uses a stub backend that returns scripted responses; drives a 4-node graph through the full IntentService → walker → runner pipeline; asserts events + persistence + audit log
- MODIFY: `resonant_client/orchestration/intent_service.py` — call `extract_skill` on graph completion; log the result
- Manual smoke: real run against deepseek-v4-flash with a small intent

**Action:**
1. Stub backend that returns scripted responses keyed by the system-prompt's role marker:
   - When system prompt mentions "SPECIALIZATION: PLAN" → return a JSON code-fence with 3 subgoals
   - When mentions "SPECIALIZATION: IMPLEMENT" → return a short summary + simulated tool calls
   - When mentions "SPECIALIZATION: VERIFY" → return a `verdict: pass` JSON block
2. Drive `IntentService.start_intent("add a hello-world README")` through this stub and assert:
   - 4 nodes ended in DONE status
   - 4 events fired in order (`node.start`/`node.done` for each)
   - Audit log contains 4 `decision`, 4 `plan_change`, ≥1 `tool_call`
   - A skill was auto-extracted with id `add-a-hello-world-readme`
   - The graph is on disk under `~/.resonant/projects/<hash>/plans/current/`
3. Manual smoke against deepseek-v4-flash on the Mac Studio:
   - Open the GUI, set sprint workflow OFF (default), sprint UI hidden
   - Type `/plan add a CHANGELOG.md to this project` and send
   - Watch the plan-graph populate live in the preview panel
   - Confirm specialists fire (`plan` then 2-3 children)
   - Confirm the CHANGELOG.md was actually created
   - Confirm a skill was extracted to `~/.resonant/skills/global/`

**Verify:**
```bash
pytest tests/test_intent_e2e.py -v
# Then manual smoke per above.
```

**Done when:** Stub-backend e2e test passes; manual smoke works against deepseek; a skill is auto-extracted and visible in the global skills folder.

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# Full test suite (775 + new tests)
pytest tests/ -q

# Tool registry sanity (unchanged)
python -c "from resonant_client.engine import tools; \
  names = sorted({t['function']['name'] for t in tools.AGENT_TOOLS}); \
  print(len(names), 'tools registered')"

# Intent end-to-end smoke
python -c "
from unittest.mock import MagicMock
from resonant_client.orchestration.intent_service import IntentService
events = []
svc = IntentService(project_path='/tmp/proj', backend=MagicMock(), settings=None,
                    on_event=events.append)
intent_id = svc.start_intent('test')
print('started:', intent_id)
"
```

Manual end-to-end:
1. Open app, default settings (sprint workflow OFF).
2. Click "Plan this" or type `/plan` and an intent.
3. Plan tab opens, graph populates, specialists fire.
4. Final result lands in chat as a status message.
5. Inspect `~/.resonant/projects/<hash>/intents/<intent-id>/audit.jsonl` — full timeline of decisions.
6. Inspect `~/.resonant/skills/global/` — a new skill if the run completed successfully.

## Success criteria

- [ ] User can launch an intent from the GUI; plan-graph populates live in the preview panel
- [ ] All five specialist types execute correctly against a real backend
- [ ] Auto-verify spawns when confidence drops; auto-repair spawns on `verify revise`
- [ ] Irreversibility floor fires reliably from inside the tool-dispatch path
- [ ] Audit log captures every decision; replay works
- [ ] Successful intents auto-extract into skills
- [ ] Plan history (snapshots) supports rollback to a past state
- [ ] Cancel terminates a running intent within 1s
- [ ] Existing chat / harness flows are unchanged (no regression)

## Out of scope (deferred)

- **Parallel specialist execution** — sequential is fine for v1; parallelism on Ollama would thrash model loading anyway. Add later if backend supports it cheaply.
- **Skill auto-loading on intent kickoff** — when a high-match skill exists, load its subtree as the initial plan. Adds risk; ship v1 with from-scratch decomposition only.
- **Cross-intent dependencies** — one intent waiting on another. Single-intent-at-a-time is enough.
- **Branch merge** — `Branch from here` creates a new intent today; merging two divergent intents back together is a v2 problem.
- **Cost-aware scheduling** — picking the cheapest backend per specialization (e.g., `verify` on a small local model, `implement` on deepseek). Worth doing once we have telemetry; not now.

## Future / nice-to-haves

| Idea | Where it would go | Why later |
|------|-------------------|-----------|
| Auto-detect "should this be an intent?" — short messages stay chat, complex ones decompose automatically | `gui/app.py` message handler | Need data on what "complex" looks like in practice |
| Visual diff view in plan-history modal (snapshot-A vs snapshot-B node list) | `gui/static/plan_graph_view.js` | Polish — text list of changes is enough for v1 |
| Confidence calibration learning ("this specialist over-confidences on TS code; adjust") | NEW `orchestration/calibration.py` | Real differentiator but only valuable after enough data |
| Skill versioning + semver pin in manifest | `skill_manifest.py` | Need a registry to make versioning meaningful |
| Replay UI: walk through an audit log step-by-step in the viz | `plan_graph_view.js` | Forensic debugging tool, defer to user demand |

## Output

When all 5 phases ship, write `SUMMARY-INTENT-WIRING.md` covering:
- Phases shipped + commit hashes
- Test count delta
- Any deviations
- Screenshot of a real plan-graph executing live
- One example skill auto-extracted from a real run

Then update `ROADMAP.md`: mark this row as ✅ Shipped, link to the SUMMARY.
