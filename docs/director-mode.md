# Director Mode

Status: implemented foundation and canonical extension contract  
Audience: users, maintainers, coding agents, and model-adapter authors  
Last updated: 2026-07-21

Director Mode lets one selected frontier model supervise a selected pool of
worker models. It is designed for difficult, long-running coding tasks where
quality, verification, and reliable completion matter more than minimizing
tokens. It does not replace Resonant's normal session loop: the feature is
opt-in per session, and single-agent execution remains the default.

## Product contract

The Director owns the result. Workers never silently become peers with shared
authority. The Director must:

1. understand the request and create a dependency-aware task graph;
2. define bounded objectives, scopes, capabilities, and acceptance evidence;
3. dispatch only ready work to eligible configured workers;
4. inspect handoffs, diffs, artifacts, and actual validation output;
5. accept, revise, reassign, block, or escalate each result explicitly;
6. integrate writer branches only after deterministic gates pass; and
7. complete only when every task is accepted or integrated.

The runtime enforces the control flow. It does not rely on the frontier model
remembering the policy in prose.

## User workflow

The **Director** control in the chat composer opens session-local setup:

- enable or disable Director Mode;
- select the frontier Director model;
- select one or more worker models;
- mark workers as vision-capable when appropriate;
- choose safe parallelism;
- require deterministic validation and independent Director review; and
- optionally integrate automatically, but only after gates pass.

The strongest available model is the recommended Director. Worker selection is
fully explicit; Resonant does not silently spend against an unselected provider.
Changing this configuration between runs rebuilds the session runtime while
preserving conversation history. Switching or forking sessions restores the
configuration; a fork starts a new Director run so it cannot mutate the source
graph.

The **Agents → Director** view shows the current phase, task graph, dependencies,
worker assignments, validation counts, worker pool, and a project-local quality
benchmark. Normal agent transcripts and controls remain in the Agents view.

## Durable state

`engine/director.py` owns the provider-neutral state machine:

- `DirectorConfig` — frontier model, worker pool, parallelism, and evidence policy;
- `DirectorRun` — objective, phase, graph, decisions, and durable JSON/JSONL events;
- `DirectorTask` — dependency, scope, attempt, assignment, handoff, artifact,
  validation, worktree, review, and blocker state;
- `WorkerScheduler` — capability, role, preference, capacity, and verified-history routing;
- `WorkerPerformanceStore` — accepted outcomes, elapsed time, validations, and revisions; and
- `DirectorBenchmarkStore` — comparable single-agent and Director outcome telemetry.

Project state lives under:

```text
~/.resonant/projects/<project-hash>/director/
  <director-run-id>.json
  <director-run-id>.jsonl
  worker-performance.json
  benchmarks.jsonl
```

Session records store only the mode, configuration, and active run ID. The task
graph therefore survives a UI reconnect and session switch. As with the wider
runtime, in-process worker threads do not survive a process crash yet; their
records and evidence remain available for recovery.

## Worker routing

Each worker declares a backend, model, roles, capabilities, optional thinking
mode, optional concurrency, priority, quality weight, and system suffix. A task
declares its role and required capabilities. The scheduler filters ineligible
workers, respects a preferred worker and active capacity, then scores eligible
workers using project-local accepted results and revision history.

This routing is explicit at a worker boundary. Resonant never switches the
model invisibly in the middle of a reasoning turn. If an explicitly requested
worker cannot be constructed, the task does not silently move to an unrelated
model.

Adaptive scheduling optimizes verified quality first. Provider token statistics
may be recorded for diagnosis, but token use is not a negative scheduling or
benchmark signal.

## Attempts and evidence gates

Every dispatch increments the task attempt. Handoffs, decisions, and validation
records remain append-only. The acceptance gate evaluates the current attempt,
which allows a corrected revision to pass while retaining the failed attempt
for audit.

Acceptance fails closed when any required condition is missing:

- no structured worker handoff;
- the latest worker outcome is not completed;
- unresolved blockers remain;
- deterministic validation is required but absent or failed; or
- a declared acceptance check lacks corresponding evidence.

The Director can record an observed result with `director_validate`, but should
prefer evidence produced by real validation tool results. A claim in worker
prose is not proof that a test ran.

## Write isolation and integration

Every Director task with write scope uses a managed Git worktree when Git is
available. The worker's result is finalized and committed but not merged.
`director_decide(action="integrate")` first executes the acceptance gate and
then uses the serialized worktree integration lock.

If the user's primary checkout is dirty, integration is deferred. Resonant does
not stash, reset, or overwrite user work. Merge conflicts and post-merge
validation failures remain explicit task failures with the branch retained for
inspection.

Read-only workers may share the project. Parallel batch routing accounts for
per-worker capacity before threads launch. Parent cancellation still propagates
to every child; pausing, steering, or cancelling an individual durable worker
continues to use the existing agent runtime controls.

## Multimodal evolution

The graph is modality-neutral. Tasks can carry `artifact_ids` and required
capabilities. Image artifacts are rehydrated and passed natively to a compatible
worker; text and other artifacts are delivered as durable references or textual
content. A task requiring `vision` cannot route to a worker that was not marked
vision-capable.

This contract allows future multimodal GLM, DeepSeek, Qwen, or other open models
to participate without a separate orchestration system. Provider adapters only
need to report capabilities and translate normalized content parts.

## Tool contract

Director-only tools are exposed only to a root session with an active
`DirectorRun`:

- `director_plan`
- `director_status`
- `director_validate`
- `director_decide`
- `director_complete`

The ordinary `task` and `task_batch` tools gain `director_task_id`, `worker_id`,
and `artifact_ids` fields. Child sessions never receive Director tools or task
spawning tools, which prevents recursive orchestration.

## Evaluation

Every root session writes quality-oriented benchmark telemetry. Runs with the
same normalized objective share a task key, enabling direct single-agent versus
Director comparisons. Tracked fields include outcome, elapsed time, steps, tool
calls, validation activity, changed files, provider diagnostics, and optional
reviewer quality score.

Release confidence requires:

- Director state-machine and persistence tests;
- role/capability/capacity scheduler tests;
- retry and evidence-gate tests;
- session-local tool exposure and worker-routing tests;
- multimodal artifact-delivery tests;
- worktree defer/integrate/conflict tests;
- WebSocket configuration and session-switch tests;
- JavaScript syntax and GUI contract tests; and
- an end-to-end browser exercise of setup and live task rendering.

## Extension rules

New scheduling, budget, or autonomy features must preserve these invariants:

1. single-agent behavior remains unchanged while Director Mode is off;
2. users select every provider/model that may be invoked;
3. no implicit token, output, context, time, or cost cap weakens quality;
4. worker output is evidence for Director judgment, not automatic acceptance;
5. writes remain isolated until deterministic gates pass;
6. user work is never silently stashed, reset, or overwritten;
7. every decision and attempt stays auditable; and
8. unsupported modalities are represented honestly, never discarded.
