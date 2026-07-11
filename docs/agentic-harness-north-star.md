# Resonant agentic harness north star

Status: canonical product and engineering contract
Audience: contributors, maintainers, coding agents, and model adapters
Last updated: 2026-07-11

This document defines what Resonant is optimizing for and the architectural
direction that should guide new work. When a historical plan, release note, or
model-specific playbook conflicts with this document, this document wins.

## Mission

Resonant is an Ollama-native, model-agnostic agent runtime for open models. Its
job is to help GLM, DeepSeek, Qwen, and future open models complete difficult,
long-running software tasks with the quality, control, durability, and evidence
expected from a first-class coding agent.

The harness owns orchestration, lifecycle, context, tools, evidence, recovery,
and user control. The model supplies reasoning and decisions inside that
dependable runtime. New model capabilities should improve results without
requiring a redesign of the harness.

## Optimization order

Resonant optimizes in this order:

1. Correct user outcome and production-quality code.
2. Task-completion reliability.
3. Verification confidence and maintainability.
4. Wall-clock time to a trustworthy result.
5. Effective autonomy, recovery, steering, and observability.
6. Compute and token efficiency.

Token count is a diagnostic, not a primary objective. Do not truncate useful
context, cap normal generation, weaken verification, or stop productive work
merely to reduce tokens. Remove duplication and stale material when it improves
model precision. Safety limits exist for real failure modes such as repeated
identical actions, stalled processes, unsafe operations, and uncontrolled
concurrency; they should trigger recovery or escalation before termination.

A useful mental model is:

```text
agent value =
    correctness × completion reliability × verification confidence × maintainability
    -------------------------------------------------------------------------------
                                  wall-clock time
```

## Architectural principles

### Capability-driven, not model-name-driven

Every model has a capability profile describing context, output behavior,
modalities, native tools, structured output, reasoning controls, prompt caching,
continuation support, and safe concurrency. Deterministic family overlays may
compensate for known behavior, but product features must depend on capabilities.
A newly multimodal GLM or DeepSeek release should work by reporting a capability,
not through a new parallel implementation.

### Multimodal internally, graceful for text-only models

Messages use typed content parts: text, image, audio, video, document, file, and
diagnostic evidence. A native multimodal model receives supported original
parts. A text-only model receives an explicit textual representation produced
by processors such as OCR, transcription, accessibility extraction, document
parsing, frame sampling, or a vision captioner. Unsupported media is never
silently discarded.

### Durable jobs, not fragile requests

Long-running work is a resumable job with an objective, acceptance criteria,
plan, todo state, decisions, evidence, file changes, validation results,
sub-agent registry, managed processes, and checkpoints. Work should survive a
WebSocket reconnect, UI restart, backend restart, model switch, and recoverable
tool failure without losing completed progress.

### Maximum useful context

Use the largest practical model context and normal output allowance. Assemble
context by operational importance rather than filling the window arbitrarily:

1. invariant agent contract and project instructions;
2. objective, acceptance criteria, user decisions, plan, and current todos;
3. active working-set files and dependency/symbol map;
4. recent dialogue, tool evidence, changes, and validation results;
5. durable discoveries and sub-agent handoffs;
6. retrievable archived transcript and artifact receipts.

Nothing important is silently lost. Large or old evidence may move to a durable,
rehydratable artifact store when keeping it inline would reduce precision.
Compaction must mechanically preserve the objective, constraints, decisions,
checklist, changes, verification, unresolved failures, and exact next action.

### Evidence-driven execution

The runtime reinforces this loop:

```text
Orient → Plan → Inspect → Act → Observe → Verify → Checkpoint → Continue
```

Claims require evidence. A file change must be observed in the workspace. A
successful result must have relevant validation. A command being started is not
the same as it completing. Discoveries update the plan. Repeated failed
approaches trigger re-planning or escalation.

### First-class workers

Sub-agents receive scoped assignments, relevant context packages, permissions,
budgets, deadlines, expected evidence, and return contracts. They report
heartbeats and structured handoffs. The parent owns integration, conflict
resolution, and final verification. Parallel work is preferred when independent
and safe; edit ownership or isolated worktrees prevent collisions.

### Recovery and portability

Malformed tool calls, empty responses, transient model failures, timeouts,
stalled tools, and interrupted sessions are expected operating conditions.
Resonant repairs, retries, checkpoints, or changes models without discarding
durable task state. A task is portable across compatible model profiles.

### User control remains live

Users can observe progress, inspect todos and workers, steer a running task,
queue follow-ups, answer questions, and stop the complete session—including
managed subprocess trees. Control messages must not wait behind the model run.

## Performance policy

- Parallelize independent repository inspection, research, validation, and
  worker tasks when doing so reduces time to a correct result.
- Cache repository maps, hashes, model probes, and immutable evidence.
- Avoid redundant scans and repeated tool output, but re-read when freshness or
  correctness requires it.
- Validate incrementally after risky changes and broadly before completion.
- Prefer targeted fast checks early, then the strongest relevant final checks.
- Route specialized work to the best configured model or processor.
- Never trade away correctness or evidence merely to improve a latency metric.

## Evaluation contract

Behavior changes are evaluated across representative open models using fixed
tasks. Required suites include multi-file implementation, diagnosis and repair,
long-context retention, restart recovery, steering and stop, sub-agent synthesis,
malformed-call recovery, visual tasks, model switching, and verification
honesty. Track:

- acceptance-check and test pass rate;
- unsupported success claims;
- elapsed time to a trustworthy result;
- malformed/rejected calls and recovery success;
- duplicate exploration and loop interventions;
- regressions and unnecessary edits;
- checkpoint/restart recovery;
- context retention and first causal divergence;
- sub-agent handoff quality and edit conflicts;
- tokens and compute as secondary diagnostics.

## Delivery roadmap

1. Capability profiles and runtime discovery.
2. Normalized multimodal content parts and text-only fallback processors.
3. Durable task ledger and artifact-receipt store.
4. Restartable execution independent of a WebSocket.
5. Structured worker context packages, handoffs, and edit isolation.
6. Adaptive context assembly and mechanically validated compaction.
7. Capability-based model routing and recovery policies.
8. Cross-model long-horizon and multimodal evaluation baselines.

Implementation status as of 2026-07-11:

- Capability profiles and Ollama runtime enrichment: foundation implemented.
- Typed multimodal content and honest text-only fallback: foundation implemented.
- Durable task ledger and artifact receipts: next implementation slice.

Each slice must remain useful independently, include contract tests, and preserve
existing chat and tool behavior.

## Updating this contract

Change this document in the same pull request as any intentional priority or
architectural shift. Explain the user outcome, tradeoff, migration impact, and
evaluation evidence. Historical documents should retain their record but gain a
status note pointing here when their recommendations are no longer current.
