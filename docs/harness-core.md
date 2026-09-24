# Harness core boundary

Current ownership is local to this package. Resonant's normal model/tool loop
lives in `lumi/engine/`; the optional planner/generator/evaluator
workflow lives in `lumi/harness/`. The former remote `resonant`
backend and engine-hosted role names in early migration plans do not describe
the current provider/runtime contract. See [ARCHITECTURE.md](../ARCHITECTURE.md).

## Enabling the optional workflow

`general.harness_enabled` defaults to `false` in `gui/settings.py`. Enable it
explicitly for sprint roles and harness cycles. Ordinary coding sessions still
use the engine and its tools when this optional workflow is disabled.

Harness state lives under `~/.lumi/projects/<project-hash>/harness/`.
`LUMI_STATE_HOME` overrides the state root for this subsystem.
`HarnessWorkspace.maybe_migrate_legacy_layout` migrates old project-local
`.resonant-harness/` artifacts. Do not introduce new runtime state into a user's
checkout.

## Module ownership

Paths below are relative to `lumi/`.

| Module | Responsibility |
| --- | --- |
| `harness/state.py` | Workspace paths, legacy migration, structured state, persisted progress/contracts/reports |
| `harness/orchestrator.py` | Background cycle lifecycle, role steps, retries, and stopping |
| `harness/service.py` | Summary assembly, output contracts, normalization, and resume instructions |
| `harness/prompts.py` | Role prompt construction through the explicit application interface |
| `gui/runtime.py` | `BackendSpec` and shared backend/session construction |
| `gui/app.py` | Application state and runtime integration |
| `gui/ws_commands.py` | UI command dispatch and harness controls |
| `engine/session.py` | Model/tool execution, conversation state, cancellation, and verification |

`gui/harness_state.py` and `gui/harness_orchestrator.py` remain compatibility
import shims. Add behavior to the owning module, not to those shims.

## Provider boundaries

Native adapters feed Resonant's engine contract. Installed Codex and Claude
Code adapters execute their own CLI tool loops. Explicit provider/model choices,
project instructions, permission settings, and saved conversation state must
survive runtime reconstruction. Optional role settings do not authorize silent
model switching.

Any new API integration needs its actual service contract and tests. The
[old engine-unification plan](engine-harness-unification-plan.md) is historical
design context, not evidence that a service currently implements those endpoints.
See [prompt architecture](model-prompt-architecture.md),
[modern runtime](modern-agent-runtime.md), and
[documentation status](documentation-status.md) for current guidance.
