# PLAN — Harness Migration & Concept Audit

> Status: ✅ Shipped · Last updated: 2026-04-27 · 5 / 5 phases complete

## Shipped (2026-04-27)

| Phase | Result |
|---|---|
| **A** Opt-in switch | `general.harness_enabled` setting added (default `False`). Master gate at `AppState.harness_enabled()`. When off: no `.resonant-harness/` created, no preamble, no message wrap, role selector hidden, badge hidden. |
| **B** Out-of-repo state | `HarnessWorkspace.root` now resolves to `~/.resonant/projects/<sha1[:12]>/harness/`. `RESONANT_STATE_HOME` env override for tests. `maybe_migrate_legacy_layout()` copies legacy `.resonant-harness/` content on first load (idempotent), surfaces a one-time status notice. **8 new tests** in `tests/test_harness_state.py`. |
| **C** AGENTS.md adoption | `INSTRUCTION_FILES` now: `AGENTS.md`, `.agents/AGENTS.md`, `RESONANT.md`, `.resonant/RESONANT.md`, `CLAUDE.md`. `_save_resonant_md` writes back to whichever file the project already uses; new projects get `AGENTS.md`. **8 new tests** in `tests/test_project_instructions.py`. |
| **D** Slim system prompt | Replaced 5-file-paths block with 1-line role description per role. Dropped JSON output contract for `generator` role (kept for `planner`/`evaluator` because the orchestrator parses it back). Generator prompt down from ~1300 chars to 306. Dataclass fields kept intact (load-bearing for orchestrator recovery — see "Deviation" below). |
| **E** Docs | Updated `docs/harness-core.md` and `ARCHITECTURE.md` with the opt-in + out-of-repo conventions. ROADMAP entry added. |

**Verification:** 660 tests pass (was 644 before; +16 net new across phases B and C).

### Deviation from original plan

Phase D was scoped to "slim dataclasses 11→4 / 13→5 fields". On audit, those "extra" fields are consumed by ~370 lines of orchestrator recovery logic in `gui/app.py` (e.g., `target_files`/`target_line_hints` drive generator-repair scaffold prompts; `validation_artifacts`/`acceptance_evidence` drive the evaluator-evidence loop). Removing them would gut the autonomous-cycle differentiator the user explicitly asked to expand on. **Decision:** keep dataclasses intact, focus the slim on the user-facing surface (system prompt + JSON contract). The fields stay available for the orchestrator and any future planner/evaluator workflow expansions.

## Why this exists

The current harness drops a `.resonant-harness/` folder full of JSON state files into the user's project repo. The 2026 industry survey (see `ROADMAP.md` companion research) found that **no shipping coding agent does this** — Claude Code, Codex CLI, OpenCode, Cursor, OpenHands, Aider, Cline all keep agent state out of the user's source tree, with one shared convention: a single `AGENTS.md` (or `CLAUDE.md`) committed to the repo for user-authored conventions.

Our pattern is research-lineage (closest to MetaGPT's PM/Architect/Engineer SOP) and was originally added as part of "Cursor 3 parity" (commit [`111d97e`](https://github.com/example/repo/commit/111d97e)). It pre-dates the fleet/Command Center work — **it is not a fleet holdover**, it's its own architectural choice with its own justification. The pieces still need scrutiny.

This plan is the result of interrogating each piece. It separates **what's genuinely useful**, **what's over-engineered**, **what's misplaced**, and **what should die**.

## Concept audit (one row per moving part)

| # | Piece | Where it lives | Verdict | Why |
|---|---|---|---|---|
| 1 | **Planner / Generator / Evaluator roles** | [`harness/service.py:243`](resonant_client/harness/service.py:243), `SessionRecord.session_role` | ✅ **Keep** | MetaGPT's SOP pattern, genuinely useful for long structured features. Already wired through every session. |
| 2 | **`HarnessOrchestrator` autonomous cycle** | [`harness/orchestrator.py:127`](resonant_client/harness/orchestrator.py:127) | ✅ **Keep** | The closest thing in this codebase to "background agent that completes a feature end-to-end." Differentiating capability. |
| 3 | **`handoff.md`** (free-form role-to-role summary) | [`harness/state.py:117`](resonant_client/harness/state.py:117) | ✅ **Keep** (relocate) | Useful for long-running multi-day work. Move out of user's repo. |
| 4 | **Evaluator's acceptance-check verification** | [`harness/state.py:54`](resonant_client/harness/state.py:54) `EvaluatorReport` | ✅ **Keep** (slim) | "After I implement, run focused checks before declaring done" is a sound habit. The 8-field dataclass is overkill — drop to 4 fields. |
| 5 | **`ProductSpec` (`spec.json`)** with `user_stories`, `sprint_order`, `design_principles`, `technical_notes` | [`harness/state.py:67`](resonant_client/harness/state.py:67) | ⚠️ **Replace with `AGENTS.md`** | Product-management ceremony for a single user. Nobody maintains a JSON spec by hand. Replace with the cross-tool `AGENTS.md` convention. |
| 6 | **`SprintContract`** (11 fields, gates generator) | [`harness/state.py:39`](resonant_client/harness/state.py:39) | ⚠️ **Slim to 4 fields** | Most fields just parrot the system prompt. Keep `sprint_id`, `objective`, `acceptance_checks`, `status`. Drop `target_files`, `target_line_hints`, `evaluator_focus`, `validation_commands`, `edit_strategy`, `feature_name`, `deliverables`. |
| 7 | **`ProgressState`** (13 fields) | [`harness/state.py:22`](resonant_client/harness/state.py:22) | ⚠️ **Slim to 5 fields** | Keep `current_phase`, `active_sprint_id`, `summary`, `next_steps`, `last_updated`. Drop product_goal, blockers (move to handoff), touched_files (read git), validation_*, acceptance_evidence. |
| 8 | **`.resonant-harness/` location** in user's repo | [`harness/state.py:18`](resonant_client/harness/state.py:18) `HARNESS_DIRNAME` | ❌ **Relocate** to `~/.resonant/projects/<hash>/harness/` | No shipping tool drops state files in the user's repo. Mirror Claude Code's `~/.claude/projects/<proj>/memory/` pattern. |
| 9 | **`run_history.jsonl` + `teacher_escalations.jsonl`** | [`harness/state.py:122`](resonant_client/harness/state.py:122) | ❌ **Relocate** with rest | Agent-internal telemetry. Definitely doesn't belong in user's repo. |
| 10 | **System-prompt "Read first: spec.json / progress_state.json / ..."** block | [`harness/service.py:262`](resonant_client/harness/service.py:262) | ⚠️ **Slim** | Even gated, it lists 5 file paths and forces a tool-call cycle. With `AGENTS.md` adoption, this becomes "Read AGENTS.md first" — one file, one call. |
| 11 | **`resonant-harness` JSON output contract** the model emits each turn | [`harness/service.py:220`](resonant_client/harness/service.py:220) | ⚠️ **Conditional** | Confusing for general use. Only emit the contract when sprint mode is active AND the role is planner/evaluator. Drop entirely for generator (it just edits files). |
| 12 | **Engine-owned vs local harness fallback** | [`gui/app.py:248`](resonant_client/gui/app.py:248) `_get_remote_harness_step_payload` | ✅ **Keep** (clarify) | Remote engine path is the future per `docs/engine-harness-unification-plan.md`. Local fallback is fine. Just surface in UI which mode is active. |
| 13 | **Default-on for every code session** | [`gui/app.py:5246`](resonant_client/gui/app.py:5246) `build_harness_instructions` call | ❌ **Make opt-in** | The bug behind the "casual question triggers 5-file read" weirdness. Default off; user enables per-project. |

## Migration phases

### Phase A — Make harness fully opt-in (1 task, ~1 commit)

**Files:**
- [`resonant_client/gui/settings.py`](resonant_client/gui/settings.py) — register `general.harness_enabled: bool` (default `False`)
- [`resonant_client/gui/app.py`](resonant_client/gui/app.py) — `build_harness_instructions` early-returns `""` when setting is off
- [`resonant_client/gui/app.py`](resonant_client/gui/app.py) — `wrap_user_message_for_harness` early-returns the raw text when off
- [`resonant_client/gui/static/app.js`](resonant_client/gui/static/app.js) — Settings UI toggle in General section: "Sprint workflow (planner / generator / evaluator)" with help text "Enable for long-running structured features. Off by default."
- [`tests/test_session_auto_feedback.py`](tests/test_session_auto_feedback.py) — confirm setting=off → empty system-prompt block

**Action:** add a single `bool` setting that gates *all* harness behavior. When off:
- No `--- HARNESS INSTRUCTIONS ---` block in system prompt
- No first-message wrap
- No `.resonant-harness/` directory created (`HarnessWorkspace.ensure_layout` is a no-op when setting is off)
- Header badge stays hidden
- Role selector in welcome screen hidden (stays "generator" implicit)

**Verify:**
```bash
pytest tests/ -q
# Manual: new project with default settings → ask "help me with desktop issues" → agent answers directly, no `.resonant-harness/` created
```

**Done when:** A fresh project with default settings never creates `.resonant-harness/`, never injects harness preamble, and the agent answers conversational questions in one turn.

---

### Phase B — Relocate state out of the user's repo (1 task, ~1 commit)

**Files:**
- [`resonant_client/harness/state.py`](resonant_client/harness/state.py) — change `HarnessWorkspace.__init__` to compute `self.root` as `~/.resonant/projects/<sha1(project_path)[:12]>/harness/` (mirror `~/.claude/projects/<proj>/`)
- [`resonant_client/harness/state.py`](resonant_client/harness/state.py) — keep `project_path` field for back-references; the `.resonant-harness/` constant stays for migration only
- New: `resonant_client/harness/migration.py` — one-time helper that, on first load of a project containing `.resonant-harness/`, copies the contents to the new location and prints a one-line note to the GUI status bar ("Migrated 7 harness files to ~/.resonant/projects/.../harness/")
- [`tests/test_harness_state.py`](tests/test_harness_state.py) — NEW: covers the new path + migration round-trip

**Action:**
1. Compute new root: `Path.home() / ".resonant" / "projects" / sha1(str(project_path))[:12] / "harness"`
2. On `ensure_layout()`, if old `.resonant-harness/` exists in the project root, copy files to new root, then leave the old folder alone (don't auto-delete — let the user remove it after they confirm).
3. Add a one-time GUI status notice on first migration: *"Moved Resonant harness state out of your repo. You can `git rm -r .resonant-harness/` when you're ready."*
4. Update `.gitignore` template hint in `RESONANT.md` docs to recommend ignoring `.resonant-harness/` on legacy projects.

**Verify:**
```bash
pytest tests/test_harness_state.py -k path -v
pytest tests/test_harness_state.py -k migration -v
# Manual: project with old `.resonant-harness/spec.json` → load it once → check `~/.resonant/projects/<hash>/harness/spec.json` exists with same content
```

**Done when:** New projects never create `.resonant-harness/` in the repo. Existing projects get a one-time copy + a clear migration notice.

---

### Phase C — Adopt `AGENTS.md` cross-tool standard (1 task, ~1 commit)

**Files:**
- [`resonant_client/engine/session.py`](resonant_client/engine/session.py) `get_system_instructions` — when assembling project instructions, look for `AGENTS.md` in the project root *first*, then fall back to existing `RESONANT.md`. If both exist, concatenate (`AGENTS.md` first).
- [`resonant_client/gui/app.py`](resonant_client/gui/app.py) — `get_resonant_md` / `save_resonant_md` WebSocket commands: when a project has neither, the editor UI prompts "Create AGENTS.md (recommended for Codex/Cursor/Claude Code interop)" or "Create RESONANT.md (Resonant-only)"
- [`resonant_client/gui/static/app.js`](resonant_client/gui/static/app.js) — RESONANT.md editor UI: rename badge to "Project conventions", show which file is loaded, allow switching
- [`README.md`](README.md) — document `AGENTS.md` as primary, `RESONANT.md` as legacy alias
- [`tests/test_session_todos.py`](tests/test_session_todos.py) or NEW `tests/test_project_instructions.py` — round-trip both file conventions

**Action:**
1. Project-instructions loader prefers `AGENTS.md`. If present, that's the source of truth.
2. Bridge-import: if Claude Code's `CLAUDE.md` or Cursor's `.cursor/rules/*.md` exist, surface them in the GUI as "Imported from Claude Code / Cursor" panels. Don't auto-merge — just expose so the user can copy-paste.
3. New default for new projects: prompt "Create AGENTS.md?" with a starter template. Don't force it.

**Verify:**
```bash
pytest tests/test_project_instructions.py -v
# Manual: project with AGENTS.md → agent loads it; project with both AGENTS.md + RESONANT.md → both appear in system prompt; project with only RESONANT.md → still works
```

**Done when:** A project with only `AGENTS.md` works identically to a project with `RESONANT.md`. Users coming from Codex/Cursor get instant interop.

---

### Phase D — Slim the dataclasses & system-prompt block (1 task, ~1 commit)

**Files:**
- [`resonant_client/harness/state.py`](resonant_client/harness/state.py) — slim dataclasses per the audit table:
  - `ProductSpec`: keep `title`, `summary`. Drop user_stories, sprint_order, design_principles, technical_notes (delegate to `AGENTS.md`).
  - `SprintContract`: keep `sprint_id`, `objective`, `acceptance_checks`, `status`, `last_updated`. Drop the other 7 fields.
  - `ProgressState`: keep `current_phase`, `active_sprint_id`, `summary`, `next_steps`, `last_updated`. Drop product_goal, blockers, touched_files, validation_*, acceptance_evidence.
  - `EvaluatorReport`: keep `sprint_id`, `verdict`, `findings`, `required_revisions`, `last_updated`. Drop score, passed_checks, failed_checks (subsumed by findings).
- [`resonant_client/harness/service.py`](resonant_client/harness/service.py) `build_instructions`:
  - Replace the "Read first: 5 file paths" block with one line: *"For sprint workflow context, read AGENTS.md (project conventions) and call `get_harness_state()` for live sprint state."*
  - For generator role, drop the JSON output contract entirely (it just confuses one-shot tasks). Keep it only for planner and evaluator roles.
- [`tests/test_harness_state.py`](tests/test_harness_state.py) — confirm slim dataclasses round-trip JSON without losing fields the dropped ones used to carry

**Action:**
- New tool `get_harness_state()` registered in [`engine/tools.py`](resonant_client/engine/tools.py) that returns the current `harness_summary` JSON on demand. Replaces the always-on file-read pattern.
- Update [`resonant_client/harness/service.py:262`](resonant_client/harness/service.py:262) `build_instructions` to use the slim system-prompt block.
- Drop the `output_contract` injection for `generator` role; keep for `planner` and `evaluator` (where the structured output actually feeds back into the orchestrator).

**Verify:**
```bash
pytest tests/ -q
# Manual: with sprint mode ON, run a planner session → verify it emits a valid resonant-harness JSON block at the end
# Manual: with sprint mode ON, run a generator session → verify it does NOT emit the JSON contract (just edits + summary)
```

**Done when:** Sprint mode is leaner (4-field SprintContract instead of 11-field), system prompt is shorter, generator sessions don't get spammed with JSON-output instructions.

---

### Phase E — Verify end-to-end & document (1 task, ~1 commit)

**Files:**
- [`docs/harness-core.md`](docs/harness-core.md) — rewrite to reflect new opt-in, out-of-repo state, AGENTS.md-first model
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — update the harness section to note: opt-in feature, state lives in `~/.resonant/projects/<hash>/harness/`, project conventions live in `AGENTS.md`
- [`README.md`](README.md) — update setup section: mention `AGENTS.md` as primary
- [`PLAN.md`](PLAN.md) — mark this migration as Done; link to `SUMMARY-HARNESS-MIGRATION.md`
- NEW `SUMMARY-HARNESS-MIGRATION.md` — final write-up

**Action:**
- E2E manual flow with sprint mode OFF (the new default):
  1. New project → casual question → agent answers in one turn, no `.resonant-harness/` created.
  2. Edit a file → no harness preamble injected, no JSON contract noise.
- E2E manual flow with sprint mode ON:
  1. Toggle setting in Settings → General.
  2. Open a project → harness directory created at `~/.resonant/projects/<hash>/harness/`, NOT in the repo.
  3. Start a planner session → it reads `AGENTS.md` (if present) + emits a sprint contract JSON block at the end.
  4. Start a generator session → reads the active sprint, implements, no JSON output contract.
  5. Start an evaluator session → reads the work, emits a verdict JSON block.
- Migration flow:
  1. Project containing legacy `.resonant-harness/` → load → state copied to `~/.resonant/`, status bar shows "Moved harness state out of your repo".
  2. User runs `git rm -r .resonant-harness/` → no regression.

**Verify:**
```bash
pytest tests/ -q  # all 644+ tests pass
grep -r '.resonant-harness' --include='*.py' resonant_client/ | wc -l  # only the migration helper references
```

**Done when:** Both modes work cleanly, migration is non-destructive, docs reflect new architecture.

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# Default (sprint OFF)
python -m pytest tests/ -q
python -c "from resonant_client.gui.settings import Settings; s = Settings(); print('default harness:', s.get('general', 'harness_enabled', False))"
# Expected: 'default harness: False'

# Sprint ON
python -c "
from resonant_client.harness.state import HarnessWorkspace
from pathlib import Path
ws = HarnessWorkspace('/tmp/fake-project')
print('harness root:', ws.root)
assert '.resonant' in str(ws.root) and 'projects' in str(ws.root), 'should live under ~/.resonant/projects/'
print('OK — out of user repo')
"
```

## Success criteria

- [ ] Default-off: a fresh project never creates `.resonant-harness/` and never injects harness preamble
- [ ] Out-of-repo: when sprint mode is on, state lives in `~/.resonant/projects/<hash>/harness/`
- [ ] AGENTS.md interop: project with only `AGENTS.md` works identically to one with `RESONANT.md`
- [ ] Slim contracts: `SprintContract` is ≤ 5 fields, `ProgressState` is ≤ 5 fields
- [ ] Migration: legacy `.resonant-harness/` projects get one-time copy + status notice; nothing breaks
- [ ] Tests: 644+ tests still green; new tests cover migration + new path + slim dataclasses

## Out of scope (deferred)

- **Renaming "harness"** to a less jargony word ("structured workflow", "sprint mode") — bikeshedding; defer until users complain.
- **Deleting the `HarnessOrchestrator` autonomous-cycle code** — it's a real differentiator (background agent runs planner→generator→evaluator unattended). Keep it; just gate it behind the same opt-in setting.
- **Deleting roles entirely** — the planner/generator/evaluator roles are useful conceptually even outside sprint mode (e.g., user might want a "planner" session that just brainstorms without coding). Keep them as session-role labels; just stop forcing the contract/JSON output for plain "generator" sessions.

## Future / nice-to-haves

| Idea | Why later |
|------|-----------|
| Bridge-read Claude Code's `~/.claude/projects/<proj>/memory/MEMORY.md` so users get continuity if they switch tools | Nice but not load-bearing |
| Bridge-read Cursor's `.cursor/rules/*.md` for one-way migration into `AGENTS.md` | Same |
| Per-project `AGENTS.md` linter (warns on missing sections, dead refs) | Polish |
| Sprint-mode templates (e.g., "Bug fix sprint", "New feature sprint", "Refactor sprint") with starter `SprintContract` JSON | Worth building once core migration lands |
| Visualize the `HarnessOrchestrator` cycle in the preview panel (planner → generator → evaluator timeline) | Cool but expensive |

## Output

When this migration is done, write `SUMMARY-HARNESS-MIGRATION.md` covering:
- Phases shipped (one bullet each, with commit hash)
- Number of `.resonant-harness/` references removed from non-migration code
- Test count delta
- Any deviations from this plan
- Screenshot of the new `~/.resonant/projects/<hash>/harness/` layout for documentation

Then update `ROADMAP.md` to add a new row for this migration as ✅ Shipped.
