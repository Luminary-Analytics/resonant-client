# Harness Core Boundary

`resonant-client` currently hosts both a user-facing client and the model
orchestration harness used for planner/generator/evaluator loops. To keep
iterating on the harness independently from GUI concerns, the core harness
implementation now lives under `resonant_client/harness/`.

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

Intended next extraction steps:

1. move prompt/evidence bundle builders into `resonant_client/harness/`
2. define a smaller interface for backend execution and teacher escalation
3. let the GUI call a harness service/controller instead of constructing the
   orchestration pieces directly

Follow-on architecture plan:

- [engine-harness-unification-plan.md](./engine-harness-unification-plan.md)
