# Harness Core Boundary

`resonant-client` originally hosted both a user-facing client and the model
orchestration harness used for planner/generator/evaluator loops. That local
split still exists for compatibility, but the canonical direction has changed:
`resonant-engine` is now the system of record for harness state and remote step
/ cycle execution when the active backend is `resonant`.

Current split:

- `resonant_client/harness/state.py`
  - `.resonant-harness` artifact layout
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
- GUI/TUI rendering and controls: `resonant-client`

Remaining local responsibilities inside `resonant-client` are now transitional:

1. local compatibility for non-`resonant` backends
2. UI event handling and session/project views
3. fallback local harness control when the engine backend is not active

Follow-on architecture plan:

- [engine-harness-unification-plan.md](./engine-harness-unification-plan.md)
