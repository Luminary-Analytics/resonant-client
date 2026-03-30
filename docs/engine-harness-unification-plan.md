# Engine-Harness Unification Plan

## Goal

Make the harness a proper server-side system owned by `resonant-engine`, not by
the `resonant-client` GUI or TUI. The client should become a thin control and
rendering layer over a canonical engine-side harness runtime.

This plan assumes:

- role-specific local adapters now exist and are usable:
  - planner
  - generator repair
  - teacher recovery
- `resonant-engine` already exposes the primary transport surfaces:
  - `POST /v1/responses`
  - `GET /health`
  - `WS /ws/cognitive`
- the current harness implementation still lives in `resonant-client` under:
  - `resonant_client/harness/state.py`
  - `resonant_client/harness/orchestrator.py`
  - `resonant_client/harness/service.py`

## Non-Goals

- Do not keep the GUI/TUI as the harness system of record.
- Do not add a second long-term orchestration stack in parallel inside the
  client.
- Do not make transport selection depend on which UI is used.
- Do not require remote clients to have local model files, adapters, or direct
  workspace access.

## Architectural Decision

### Canonical ownership

`resonant-engine` becomes the owner of:

- harness state
- harness execution
- role routing
- tool execution
- recovery policy
- teacher escalation policy
- run history and export artifacts

`resonant-client` becomes responsible for:

- rendering sessions, state, and progress
- sending user actions and control commands
- showing approvals, diffs, logs, and artifacts
- subscribing to engine event streams

### State location

The canonical `.resonant-harness/` state should live with the engine-side
workspace on the machine that actually executes tools and model calls.

That means:

- a remote GUI/TUI is not the authority on harness files
- the engine writes and reads `.resonant-harness`
- the client only requests state snapshots and event streams

### Transport

Use the existing engine URL as the primary integration point.

- HTTP/SSE remains the canonical request/response transport for model and
  harness APIs
- WebSocket remains useful for live event streaming and interactive terminal
  style connections
- the client should not need a second custom harness transport beyond the
  engine base URL and optional engine WebSocket URL

## Target End State

### Engine-side packages

Create a dedicated server-side harness package in `resonant-engine`, for
example:

```text
resonant_engine/
  harness/
    state.py
    orchestrator.py
    service.py
    prompts.py
    router.py
    exports.py
    api_models.py
```

Responsibilities:

- `state.py`
  - `.resonant-harness` layout
  - structured state dataclasses
  - progress / contract / evaluator mutation helpers
- `orchestrator.py`
  - planner / generator / evaluator state machine
  - retry and escalation rules
  - stop conditions
- `service.py`
  - summary assembly
  - contract normalization
  - resume prompt construction
  - evidence and output-contract helpers
- `prompts.py`
  - role-specific prompt and bundle builders
- `router.py`
  - server-side role-to-model/profile routing
  - local adapter selection
  - frontier fallback policy
- `exports.py`
  - run history and training export hooks
- `api_models.py`
  - request / response schemas for harness endpoints

### Role router

The engine should expose role-aware execution profiles such as:

- `planner`
- `generator`
- `evaluator_fast`
- `generator_repair`
- `teacher_recovery`

Those profiles should map to:

- local role-specific adapters when available
- frontier fallbacks when policy requires them

The client should ask for a role or workflow action, not select raw adapters
itself.

### Harness API surface

The engine now owns the live harness control plane for the `resonant` backend.
Current endpoints:

- `GET /v1/harness/state`
  - returns canonical harness state for a project/workspace
- `POST /v1/harness/step`
  - prepares or executes exactly one planner/generator/evaluator step
- `POST /v1/harness/update`
  - applies a parsed `resonant-harness` payload server-side
- `POST /v1/harness/teacher-recover`
  - runs manual teacher recovery server-side when configured
- `POST /v1/harness/sprint`
  - updates the active sprint contract server-side
- `POST /v1/harness/contract-status`
  - mutates contract status server-side
- `POST /v1/harness/evaluator-verdict`
  - records evaluator verdicts server-side
- `GET /v1/harness/cycles`
  - lists engine-owned cycle runs
- `POST /v1/harness/cycles/start`
  - starts an engine-owned automated cycle
- `GET /v1/harness/cycles/{run_id}`
  - returns a full cycle run
- `POST /v1/harness/cycles/{run_id}/cancel`
  - cancels a running cycle
- `GET /v1/harness/schedules`
  - lists engine-owned recurring harness-cycle schedules
- `POST /v1/harness/schedules`
  - creates an engine-owned recurring harness-cycle schedule
- `PATCH /v1/harness/schedules/{task_id}`
  - updates an engine-owned recurring harness-cycle schedule
- `DELETE /v1/harness/schedules/{task_id}`
  - deletes an engine-owned recurring harness-cycle schedule

Streaming:

- step/cycle progress should emit structured events over SSE or WebSocket
- the same events should drive TUI, GUI, and future integrations

### Client architecture

`resonant-client` should keep:

- views
- controls
- settings
- project/session management
- event rendering

It should stop owning:

- canonical `.resonant-harness` mutations
- orchestration loops
- recurring harness-cycle schedules
- role routing logic
- teacher recovery decisions

The client should call an engine harness API client instead of instantiating the
harness runtime directly.

## Migration Plan

### Phase 0: Freeze current contracts

Before moving code, freeze and document the current wire contracts:

- harness state JSON shape
- planner/generator/evaluator update payloads
- teacher recovery payloads
- run history rows
- export row shape used by the training pipeline

Acceptance:

- current client and generated corpora continue to parse
- normalization edge cases are written down, not rediscovered later

### Phase 1: Move harness core to the engine repo

Move the current harness core from `resonant-client` into `resonant-engine`.

Start with:

- `resonant_client/harness/state.py`
- `resonant_client/harness/orchestrator.py`
- `resonant_client/harness/service.py`

Also move prompt/evidence helpers currently trapped in:

- `resonant_client/gui/app.py`

During this phase:

- preserve behavior first
- keep client shims temporarily if needed
- do not redesign the whole workflow yet

Acceptance:

- engine can instantiate the harness runtime without importing GUI modules

Status:

- done for the initial core slice
- `resonant-engine` now owns canonical harness state and single-step execution
- live endpoints now include:
  - `GET /v1/harness/state`
  - `POST /v1/harness/step`
  - `GET /v1/harness/cycles`
  - `POST /v1/harness/cycles/start`
  - `GET /v1/harness/cycles/{run_id}`
  - `POST /v1/harness/cycles/{run_id}/cancel`
- `resonant-client` now prefers those engine APIs when the active backend is `resonant`

### Phase 2: Move cycle ownership to the engine

This phase is now also in place for the `resonant` backend.

What changed:

- the planner / generator / evaluator cycle registry is server-owned
- cycle start / list / inspect / cancel operations are exposed by `resonant-engine`
- the client no longer has to be the cycle system of record when using the engine backend

What still remains:

- move teacher recovery / policy decisions fully server-side
- expose streamed cycle progress over a dedicated API event channel instead of polling result/list endpoints
- retire the remaining local harness orchestrator path once non-`resonant` fallbacks are intentionally handled
- client no longer needs direct harness internals for basic status rendering

### Phase 2: Add engine-side role router and local adapter catalog

Implement role-aware routing in the engine.

Required pieces:

- profile registry for planner / generator / evaluator / repair / recovery
- mapping from role -> local adapter path or frontier provider
- policy hooks for retry, repair, and escalation

Use the new role-specific adapters as initial local profiles.

Acceptance:

- engine can run each role by profile name
- no client code needs to know adapter file paths

### Phase 3: Add harness API endpoints

Add first-class engine endpoints for:

- state fetch
- step execution
- cycle execution
- control actions
- history/export access

Keep the existing `resonant` backend base URL. Do not invent a second service
unless forced by runtime constraints.

Acceptance:

- one harness cycle can be started, monitored, and stopped entirely through
  engine APIs
- outputs stream in a transport-neutral way

### Phase 4: Switch `resonant-client` to engine-owned harness

Replace local harness execution paths in the client with engine API calls.

Client changes:

- add a thin engine harness client
- replace local `HarnessOrchestrator` execution with remote commands
- keep local UI state only as cached display state

Acceptance:

- GUI and TUI both operate against the same engine-side harness state
- remote clients behave the same as local ones

### Phase 5: Remove client-owned harness state mutation

Once engine-backed paths are stable:

- remove local harness execution from the client
- remove local `.resonant-harness` writes from GUI/TUI code
- keep only compatibility adapters where necessary for loading legacy data

Acceptance:

- GUI/TUI are no longer systems of record
- engine-side workspace state is canonical

### Phase 6: Unify training/export path with engine artifacts

Make the engine the source for:

- harness export rows
- run history
- recovery events
- distillation-ready artifacts

The LocalCodingModel pipeline should consume engine exports instead of relying on
client-local harness internals.

Acceptance:

- export and training scripts can target engine-owned harness histories
- the data factory no longer depends on the GUI/TUI implementation

## Immediate Implementation Order

Do these in order:

1. Define the harness API schemas and event schema.
2. Move harness core modules into `resonant-engine`.
3. Move prompt/evidence builders out of `resonant_client/gui/app.py`.
4. Add server-side role router using the current role-specific adapters.
5. Add `GET /v1/harness/state` and `POST /v1/harness/step`.
6. Update `resonant-client` to read state and run a remote step.
7. Add `POST /v1/harness/cycle` and control endpoints.
8. Remove local harness mutation paths from the client.

## Data And Model Integration

The engine should be the place where these are configured:

- base local model
- planner adapter
- generator repair adapter
- teacher recovery adapter
- evaluator-fast policy
- frontier fallbacks

The client should receive:

- current selected role/profile
- current backend/provider
- recent recovery information
- run/cycle summaries

It should not own:

- adapter-path selection
- retry backend selection
- recovery routing policy

## Risks

### Migration risk

There is a real risk of running two harness implementations in parallel during
migration. Avoid that by:

- freezing payload contracts first
- moving the code instead of rewriting it immediately
- cutting the client over endpoint-by-endpoint

### Workspace mismatch

A remote client may point at a project path that only exists on the engine
machine. Make the engine workspace authoritative and treat client-side paths as
display hints unless the session is explicitly local.

### Transport drift

Do not split semantics across HTTP for one client and WS-only for another. Use
one canonical server-side event model and expose it consistently.

### Frontier cost drift

Keep recovery and teacher routing server-side so policy is centralized and can
be audited.

## Acceptance Criteria

The unification is done when:

- GUI/TUI are no longer the harness system of record
- engine owns `.resonant-harness` for active workspaces
- one harness cycle can run entirely through engine APIs
- planner/generator/evaluator/recovery routing happens server-side
- the client is only a viewer/controller for harness state
- LocalCodingModel export/training flows consume engine-owned harness artifacts

## Recommended Next Sprint

The next implementation sprint should be:

1. add engine-side harness package scaffold
2. move `state.py`, `orchestrator.py`, and `service.py` into the engine repo
3. define harness step/state schemas
4. add `GET /v1/harness/state`
5. add `POST /v1/harness/step`
6. switch one client path to use the remote step call

That is the smallest slice that starts the real migration without creating a
second long-lived architecture.
