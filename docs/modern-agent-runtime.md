# Modern agent runtime

Status: implemented foundation and canonical extension guide
Last updated: 2026-09-24 (capability-pack trust and tool approvals)

This document describes the runtime Resonant uses for long-horizon coding with
its native provider adapters. The design favors correct, verified
results and wall-clock performance over token minimization.

## Runtime contract

A run is no longer only an in-memory request. It is a graph of durable agent
records, workspace checkpoints, artifacts, lifecycle events, and a reproducible
trajectory. The GUI is one control client for that runtime; reconnecting the
GUI does not define the lifetime or history of the work.

The core execution loop remains in `engine/session.py`. New services are wired
by `gui/app.py::_wire_session` and inherited by child sessions:

| Service | Module | Responsibility |
|---|---|---|
| Agent registry | `engine/agent_runtime.py` | Durable workers, transcripts, status, steering, pause/resume/cancel, structured handoffs |
| Artifact bus | `engine/artifacts.py` | Typed text/image/audio/video/diff/DOM/terminal/trace evidence and capability-negotiated delivery |
| Checkpoint timeline | `engine/checkpoint_timeline.py` | Conversation-linked workspace snapshots and files/chat/both restore |
| Worktree manager | `engine/worktrees.py` | Isolated writer branches and serialized, clean-checkout-only integration |
| Flight recorder | `engine/flight_recorder.py` | Reproducible manifests, append-only events, comparison, OTLP JSON export |
| Context broker | `engine/context_broker.py` | Provenance-aware explicit `@provider:selector` attachments |
| Model roles | `engine/model_roles.py` | Explicit plan/explore/implement/test/review/vision boundaries and configured routing |
| Director Mode | `engine/director.py` | Opt-in frontier supervision, durable dependency graph, adaptive worker pools, evidence gates, and outcome benchmarks |
| Capability packs | `engine/capability_packs.py` | One user-approved, digest-pinned package for agents, skills, hooks, MCP, commands, recipes, and UI metadata |
| Code intelligence | `engine/code_intelligence.py` | Python AST and optional Tree-sitter symbols, imports, and calls |
| Lifecycle hooks | `engine/hooks.py` | Structured JSON decisions around models, tools, batches, permissions, workers, compaction, checkpoints, and validation |

Project runtime data is stored outside the repository under
`~/.lumi/projects/<project-hash>/`. User files and the user's Git index are
never silently stashed or reset.

## Durable workers and parallel writers

`task` creates a durable `AgentRecord` before execution and returns an
`AgentHandoff` containing outcome, evidence, changed files, validation,
blockers, artifacts, and the recommended next action. Transcripts remain
inspectable after completion or failure. A process restart marks nonterminal
records as `stuck` because their threads cannot survive, while preserving all
evidence for recovery.

`task_batch` runs two to four independent workers concurrently in ordinary
single-agent sessions. Director Mode may raise that boundary to its explicitly
configured worker-pool limit. Build workers are forced into Git worktrees. In
single-agent mode they commit and compete for one serialized integration lock.
In Director Mode, a writer's finalized branch remains isolated until the
frontier Director records passing evidence and explicitly integrates it.
Integration proceeds only when the user's checkout is clean; otherwise the
branch and worktree remain for review. Read workers may safely share the
project. Child cancellation is independent, while parent cancellation still
propagates to every child.

## Director Mode

Director Mode is opt-in and session-local. A selected frontier model owns a
durable dependency graph and delegates bounded work to a user-selected pool of
cheaper or specialized models. The ordinary single-agent prompt, tools, and
execution path are unchanged while the mode is off.

The Director must plan, dispatch only ready tasks, inspect structured handoffs,
attach deterministic validation evidence, revise or reassign weak work, pass a
fail-closed acceptance gate, and integrate isolated writer branches. Each
attempt stays auditable; failed evidence from a prior attempt remains in history
without poisoning a corrected revision. Scheduler decisions honor role and
modality capabilities, preferred workers, concurrency, and project-local
verified performance. No implicit token, context, output, time, or cost cap is
introduced.

The historical Agents-panel Director view is not exposed by the current desktop
frontend. Runtime data can describe the current phase, frontier
model, worker pool, task dependencies, assignments, validation counts, and a
project-local single-agent versus Director outcome comparison. See
[`director-mode.md`](director-mode.md) for the complete contract and extension
guide.

The Agents pane is the runtime control surface. It exposes current and past
workers, transcripts and handoffs, plus pause, resume, cancel, and non-cancelling
steering.

## Checkpoint and rewind semantics

A checkpoint is created before each likely workspace mutation. One cursor links
the model conversation, replayable display events, and workspace state.

- Git projects use hidden checkpoint refs and a temporary index, never the
  user's index.
- Non-Git projects use ZIP snapshots.
- Restoring files first preserves the replaced state as a Git recovery branch
  or recovery archive.
- The Timeline UI offers files-only, conversation-only, or both.
- Compare reports the checkpoint-to-current Git delta where available.

Restores are refused while an agent is running. Every restore is a lifecycle
hook boundary so policy or audit integrations can observe it.

## Lifecycle hooks

Legacy environment-variable hooks remain supported. New hooks may set
`input_format: "json"`; Resonant writes a structured event to stdin and reads a
JSON decision from stdout. Supported output fields include `decision`,
`reason`, `additional_context`, `modified_args`, `retry`, `continue`, and
`metadata`.

Hook points cover session, model, tool, tool-batch, permission, sub-agent, task,
compaction, checkpoint, validation, user-input, worktree, and error boundaries.
Hooks can deny before side effects, repair arguments, inject deterministic
context, or reject an unsupported completion claim.

A missing or unknown `decision` is no decision; it is never read as consent.
When several hooks answer, `deny` outranks `ask`, which outranks `allow`, and a
gate hook that exits non-zero is a `deny`.

## Tool approvals

Each tool call passes, in order: PRE_TOOL_USE hooks, the execution policy, then
the autonomy tier. Built-in policy denies are checked before a project's
`lumi-policy.json`, so a repository can tighten the policy but cannot
weaken a built-in deny. A policy `prompt` rule requires approval even in
Full-auto.

When approval is required, the user's answer is final and only an explicit
`true` approves. A PERMISSION_REQUEST hook can neither run a call the user
denied nor block one they allowed. Only when no prompt is available (background
runs, or delegated work without a parent prompt) does an explicit allow or deny
from a matching PERMISSION_REQUEST hook settle the call. No answer fails
closed. Arguments that such a hook rewrites are checked against the policy
again. Delegated workers ask through the parent's prompt, one question at a
time. The GUI binds every prompt to a request id and ignores answers for a
prompt that is no longer waiting.

| Mode | Tier | Runs without asking |
|---|---|---|
| Ask | `ask` | Read-only tools; asks before file writes, shell and everything else |
| Auto-edit, Plan | `auto-edit` | Read-only and file-editing tools, `await_user`, `task`, `task_batch` |
| Full-auto | `full-auto` | Everything the policy allows |

Ask's built-in policy marks file writes and shell commands `prompt` and keeps
Auto-edit's denies (recursive `rm`, `chmod` on a system path, a download piped
into a shell), which are checked before a repository's rules. A repository or
organization `allow` rule can't skip Ask's approval: the tier still asks.
`lumi run --mode ask` uses the read-only `suggest` tier instead, whose policy
denies file writes and shell outright, since nobody can answer a prompt there.

Auto-edit asks before shell, MCP, browser, desktop, REPL, process and git
actions, and before any newly added tool. Changing the mode updates the live
session's tier and policy, including a run in progress.

## Flight recorder and evaluation

Every GUI run receives a manifest containing backend/model role, prompt/system
and tool-schema hashes, provider options, capability profile, checkpoint IDs,
artifact IDs, and status. The event stream is append-only and fingerprints
causal content while excluding clocks and elapsed time.

The Traces UI lists runs, opens their complete trajectory, compares two runs at
the first causal divergence, and exports dependency-free OTLP-compatible JSON
as a trace artifact. This is the basis for deterministic cross-model regression
tasks and replay-from-checkpoint evaluation.

## Context broker and code intelligence

Explicit attachments are inserted in chat with:

```text
@file:path/to/file.py
@symbol:ClassName
@diff:working
@checkpoint:cp_00001_abcd1234
@agent:agt_1234abcd
@artifact:art_1234abcd
@test-failure:last
@terminal:last
@plan:current
```

Every resolved item carries a provider, label, provenance, freshness metadata,
and estimated size. The Context cockpit lists available providers. Repository
maps use Python ASTs and optional `tree-sitter-language-pack` grammars before
falling back to conservative regex extraction.

## Model-role pipeline

Roles are explicit and user-configurable: `primary`, `plan`, `explore`,
`implement`, `apply`, `test`, `review`, `vision`, and `summarize`. A role may
select a backend/model, thinking mode, permission mode, step boundary, system
suffix, and independent-review requirement. Routing occurs only at a visible
worker/phase boundary; Resonant never silently changes the model mid-turn.

The default implement role requests independent review as policy metadata. A
deployment may bind review to a different configured model, but the runtime
falls back to the active backend if that route cannot be built.

## Capability packs

A pack directory contains `lumi-pack.json` (a legacy `resonant-pack.json` is
still read) plus referenced resources. It
may declare agents, skills, lifecycle hooks, MCP servers, commands, recipes,
and UI panels. Repository packs live under `.lumi/packs` (legacy
`.resonant/packs` is also scanned); global packs live
under `~/.lumi/packs`.

A manifest describes a pack; it never approves it. `trust`, `enabled` and
`sha256` written in a manifest are ignored, because a cloned repository
controls its own files. Trust comes only from user settings (`plugins`):

- **Approval by location.** **Settings > Capability packs** shows every
  discovered pack, what its hooks and MCP servers would run, and its content
  digest. **Approve and enable** records
  `plugins.<id>.approvals.<pack directory> = {sha256, enabled}`. The server
  refuses the approval if the pack changed after the list was drawn. This is
  the only way to trust a pack inside the open project, so an approval never
  follows a copied pack into another repository.
- **Pinned trust by id**, for packs outside the project only:
  `plugins.<id> = {trust: local|trusted|signed, enabled: true, sha256}`.

Both pin one digest covering every file in the pack directory (except `.git`)
and the repository files that its hook and MCP commands name, such as
`python scripts/check.py`. A pack that contains a link, exceeds 4,000 files or
64 MB, or names a file linked outside the project cannot be verified or
approved. Changing any covered file withdraws trust until the pack is approved
again: hooks re-verify the digest before each run, and skills, agents and MCP
servers stop contributing. Files that a named script runs in turn are not
followed, so review what the commands do before approving.

Only approved, enabled, unchanged packs register hooks, connect MCP servers,
contribute skills, or create agent types. Pack hooks ride on per-session
runners rather than the shared settings runner. Opening another project
disconnects the previous project's pack MCP servers, and its sessions' pack
hooks go with those sessions. When the open project has packs waiting for a
decision, the banner above the composer links to the review page.

## Multimodal artifact bus

All observations are typed artifacts rather than image-only exceptions. The
bus already stores native user images, screenshots, large terminal/tool output,
sub-agent handoffs, and exported traces. Its capability negotiation returns:

- native text for textual evidence;
- a native data URL when the model declares that modality and transport;
- a durable reference with an explicit reason when native delivery is not yet
  supported.

Future audio, video, document, DOM, accessibility, and vision processors plug
into this boundary. Unsupported evidence is never silently discarded.

## Verification rules

Changes to this runtime require focused tests for the affected service, the GUI
contract test, JavaScript syntax validation, Ruff, and the complete Pytest
suite. Release validation additionally builds the Windows bundle and exercises
the production update path. Tests for these foundations live in
`tests/test_modern_harness_runtime.py`.
