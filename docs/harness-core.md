# Harness Core Boundary

`resonant-client` originally hosted both a user-facing client and the model
orchestration harness used for planner/generator/evaluator loops. That local
split still exists for compatibility, but the canonical direction has changed:
`resonant-engine` is now the system of record for harness state and remote step
/ cycle execution when the active backend is `resonant`.

**Two important conventions as of 2026-04:**

1. **The harness is opt-in.** Set `general.harness_enabled = true` in Settings
   (or in `~/.resonant/settings.json`) to wake up planner/generator/evaluator
   roles, sprint contracts, and the autonomous orchestrator cycle. Default is
   off — fresh projects get a plain agentic loop, no harness preamble.
2. **State lives outside the user's repo.** Storage path is
   `~/.resonant/projects/<sha1(project_path)[:12]>/harness/`, mirroring Claude
   Code's `~/.claude/projects/<proj>/` layout. Override the parent dir with
   `RESONANT_STATE_HOME` (used by tests). Legacy `.resonant-harness/` folders
   are migrated transparently on first load — see
   `HarnessWorkspace.maybe_migrate_legacy_layout`.

Current split:

- `resonant_client/harness/state.py`
  - out-of-repo artifact layout (`~/.resonant/projects/<hash>/harness/`)
  - one-shot legacy migration from `.resonant-harness/`
  - structured state dataclasses
  - progress / contract / evaluator report mutation helpers
- `resonant_client/harness/orchestrator.py`
  - background cycle state machine
  - planner / generator / evaluator loop control
  - retry / escalation / stop conditions
- `resonant_client/harness/service.py`
  - harness summary assembly
  - contract/status normalization
  - harness instructions and output contracts
  - resume-prompt construction
- `resonant_client/gui/app.py`
  - client integration
  - backend execution
  - backend/model selection
  - UI-facing command handling

Compatibility:

- `resonant_client/gui/harness_state.py`
- `resonant_client/gui/harness_orchestrator.py`

remain as thin import shims so existing imports do not break while the package
boundary settles.

Current remote ownership:

- canonical harness state: `resonant-engine`
- remote step execution: `resonant-engine`
- remote cycle registry and lifecycle: `resonant-engine`
- remote harness mutations and teacher recovery: `resonant-engine`
- remote recurring harness-cycle schedules: `resonant-engine`
- GUI/TUI rendering and controls: `resonant-client`

Current remote role runtime for the `resonant` backend:

- planner: engine-hosted `localcodingmodel-planner-clean`
- generator: engine-hosted `localcodingmodel-router`
- generator retry / repair: engine-hosted `localcodingmodel-generator-repair`
- evaluator: engine-hosted `resonant-engine`

Remaining local responsibilities inside `resonant-client` are now transitional:

1. local compatibility for non-`resonant` backends
2. UI event handling and session/project views
3. fallback local harness control when the engine backend is not active
4. local non-harness scheduling for non-`resonant` session tasks

Follow-on architecture plan:

- [engine-harness-unification-plan.md](./engine-harness-unification-plan.md)
