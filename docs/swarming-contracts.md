# Swarming implementation contracts

Status: P0 working contracts, September 26, 2026. The user authorized autonomous
implementation through **P7**, including enterprise governance and cross-session
collaboration. Delivery remains dependency ordered: local first. This document
records the runtime boundaries to implement; it does not claim a usable swarm.
See [progress and evidence](swarming-progress.md) and the [product plan](swarming-plan.md).

## Ownership and existing seams

| Boundary | Existing behavior | Swarm contract |
| --- | --- | --- |
| UI command | `gui/ws_commands.py:CommandContext` reads mutable `state.project` and `state.session` | Resolve a trusted owner and captured project/session/run before applying a command. Never infer the target from whichever session is currently displayed. |
| Foreground run | `gui/chat_loop.py:ChatRunLoop` owns the running turn independently of its attached viewer; `gui/app.py` captures `run_session` and `run_record` for ordinary chat | Reuse viewer detachment semantics, but retain an immutable swarm run handle for cancellation, persistence, subscriptions and worker construction. P0–P5 retain one foreground workspace. |
| Backend creation | `gui/runtime.py:BackendSpec` retains explicit provider/model and credential source | Build a separate mutable backend for each worker. Resolve credentials at execution; persist the nonsensitive spec only. Give each SONN worker attempt its own generated identity while the parent's saved conversation identity remains unchanged. |
| Worker record | `engine/agent_runtime.py:AgentRegistry` persists project-scoped JSON and transcripts | SQLite owns swarm state. The registry may project events by attempt ID; a failed projection cannot dispatch or acknowledge work. Legacy controls must route swarm-owned records to the supervisor or refuse them, and registry loading must not independently rewrite their status. |
| Delegation | `Session._execute_task` auto-approves children, may share a backend and may fall back to the shared checkout; batch results wait for the batch | Use a separate swarm execution adapter. Commit admission before dispatch; return promptly and expose partial results. Writers fail closed if isolation is unavailable. |
| Tool execution | `Session._allowed_tools` gates generic tools after task/MCP special branches | Add a separate universal swarm gate before **every** dispatch, including invented tool names and special handlers. Tool schemas and prompts cannot establish enforcement. |
| Artifacts | `ArtifactStore` has content hashes and project-local runtime storage | Resolve run/attempt authorization before metadata or content access. A message reference is neither an access grant nor evidence that its contents were inspected. |
| Checks and outcome | Existing handoffs may contain model-supplied validation prose | Trusted check receipts bind criterion, exact candidate/input revision, execution identity and exit status. Submission, acceptance and application are distinct transitions. |
| Jobs/processes | Existing jobs are project owned and time bounded | Capture run/attempt/epoch and reconcile actual process-tree termination. A stop request, expired authority, or closed UI is not termination evidence. |
| Legacy orchestration | `gui/app.py:_wire_session` retires Director configuration | A new versioned `swarming` configuration defaults off. No old flags or saved Director runs reactivate. AI Employee machinery remains paused. |

## Trusted identity and protocol

Version 1 uses explicit personal scope: owner, opaque project ID and saved session
ID. A filesystem path is a local resource locator, not a global principal or
remote project identity. Trusted adapters create scope objects; a model or socket
payload cannot choose an owner, tenant, role or supervisor identity. Local typed
objects do not provide remote authentication. P6 must bind them to authenticated
tenant membership and enrolled hosts before accepting remote traffic.

The target command envelope is:

```json
{
  "version": 1,
  "command_id": "opaque retry-stable identifier",
  "run_id": "captured run identifier",
  "expected_revision": 4,
  "epoch": 1,
  "kind": "pause",
  "payload": {}
}
```

The trusted adapter supplies principal and scope separately. Command results
contain the committed revision, event cursor and observed state. Reusing a command
ID with changed semantics is an error; retrying identical committed semantics
returns the original receipt without a second effect. Keys are scoped to run and
actor, and replay reauthorizes current captured scope before returning any receipt
content. Compare the full immutable semantics, not just the command kind.
Acknowledgement follows
durable commit. Model work and subprocess execution never occur inside a database
transaction. `SwarmSupervisor` implements optimistic revisions and the command
dispatcher. The original storage-spike mutation methods reject supervisor-managed
runs, so they cannot bypass policy. Neither API is a network endpoint.

Reads are authorized too: snapshots, replay, messages, artifacts, diagnostics and
exports resolve captured scope. Denied lookups must not reveal another owner's
content. P6 separates metadata viewing, content viewing, approval and run control.

## Admission and recovery invariants

1. Claim a ready item, reserve allowance, record an attempt, write dispatch intent
   and append the event in one transaction. A second process cannot claim the
   same active item or allocate the same remaining allowance.
2. One active supervisor lease owns each run; a monotonically increasing epoch
   fences out the previous supervisor. Possession of a current epoch alone cannot
   authorize a second supervisor. Attempt and epoch are immutable causal identities. A retry creates another
   attempt. Never relabel an interrupted attempt as successfully resumed.
3. Check current run state, epoch, policy, dependencies, scope and capacity at
   admission. P1 extends the storage spike with policy intersection and the graph.
4. Capture a durable model request ID before invocation; reserve its allowance
   separately from tools, auxiliary requests and dollar accounting. A model
   response never creates authority by supplying a claimed request ID.
5. A crash after dispatch intent creates uncertainty. Reconciliation revokes the
   old epoch and preserves reservations whose execution outcome is unknown.
   Inspect owned processes/effects before explicitly recovering or replacing work.
6. Stop revokes new admission immediately. Existing effects remain tracked until
   observed completion or confirmed termination. Neither stop nor pause rolls back
   files or refunds unknown spending. Receipt settlement must be idempotent.
7. Completed work and earlier failures remain readable through recovery. New
   requirements invalidate affected acceptance without erasing earlier evidence.

Request-unit accounting distinguishes reservation, actual request start, known
use, outstanding allocation and unknown execution. The native guard enforces this
before primary and auxiliary requests. It is not a dollar cap or provider billing
implementation; missing cost/usage observations remain unknown.

## Message and result contracts

Agents send untrusted typed data: findings, questions, answers, blockers, change
proposals and handoff references. Runtime events and authorized commands have
separate sources. A message cannot change policy, grant access, accept a result,
spawn workers or authorize spending. The sender is derived from its captured
attempt, not from message text.

Persist a message and its addressed delivery before acknowledging it. Replays
use durable recipient cursors with at-least-once semantics and retry-stable IDs.
Distinguish runtime receipt from inclusion in a particular model input; bind the
latter to attempt, epoch, input revision and model request. Context acknowledgement
is the adapter's responsibility. A worker answer or result is a separate record.
The context receipt proves durable assignment to a prepared input, not remote
provider receipt or comprehension. Its model request separately records whether
dispatch started, completed or remains uncertain. A failed input transaction leaves
the message pending; an ambiguous request never authorizes blind replay.
After revocation, preserve historical evidence but refuse new disclosure/effects.
Backpressure may reject data messages; it must leave control commands usable.

Initial reader tools are file reads, glob/search, authorized artifact reads and
swarm tools. Unrestricted shell, nested delegation, browser/desktop mutation,
creative editors and MCP are excluded until separately enforced and qualified.
Generated assignments/messages/repair text must retain generated provenance;
they cannot become human SONN learning input.

Submission creates an immutable handoff. Acceptance requires attributable checks
and current requirements. Writers produce a separately verified combined candidate.
Application rechecks the user's clean checkout and expected base under a repository
lock. A retained candidate with application pending does not complete a requested
integrated change. The P0 store's terminal attempt record implements none of these
acceptance or integration guarantees by itself.

The read-only preview declares `owner_review` explicitly. An owner's review
command must name the exact stopped submission and retain a nonempty observation;
the command records the owner as reviewer and accepts atomically. It does not
represent an executed test or model verification. The owner completes the run
separately after every work item is accepted and all effects/accounting resolve.
The runtime watches durable foreground ownership even with no panel connected.
Unreconciled saved work blocks new team execution in its project; ordinary chat,
model changes and missions wait only in the conversation that owns it
(`SwarmRuntime.blocking`), and navigation remains available to inspect orphaned
runs. A run this host starts or takes over holds its conversation at once,
before the ownership observer's next refresh, and any active run whose owner
isn't known yet holds every conversation. Finishing Stop releases ownership
without viewer polling. When a run ends, the worktrees and team branches of
writers whose change was applied, or who made none, are removed from the
repository; unapplied writer results and candidates, and applied candidates'
worktrees (for Inspect candidate), stay until the owner's Discard
(`discard_kept_work`, or `discard_all_kept_work` for every ended run of the
conversation). A branch is deleted only at its recorded tip, right after its
own worktree, once a fresh read shows no worktree, rebase or bisect uses it; a
branch someone committed to is never deleted, and the folder left with it goes
only by the owner's `remove_left_worktree` (`cleanup.py`, recorded as run
events). The run's records stay.

## Local persistence decision

Use `engine/swarming/` and a versioned SQLite store below the existing project
runtime directory (`swarm/state.sqlite`), never in the project source or a shared
network database. Initial connections use `foreign_keys=ON`, `synchronous=FULL`,
short `BEGIN IMMEDIATE` write transactions, busy handling and the rollback journal
(`DELETE`). The source Python runtime reports SQLite **3.49.1**. WAL is deferred
until the packaged SQLite version and concurrency/recovery behavior are qualified.

The store rejects unknown writable schemas and provides read-only version/integrity
inspection and SQLite-consistent backup. Future nontrivial migrations require a
backup and explicit supported-version steps; a schema number alone is not an
upgrade implementation. Schema 2 includes a backed-up migration from the actual
version-1 fixture schema, with injected-failure rollback coverage. Initial
installation and reopen are tested separately from downgrade inspection. The
store does not promise repair of arbitrary corruption.

## Verification envelope

- P0: reviewable ownership/command contracts, predeclared benchmark inputs and
  checks, and the atomic persistence/reopen/backup/reconciliation spike.
- P1: graph/policy/allowance enforcement, command revisions, leases and realistic
  crash boundaries including external-effect uncertainty.
- P2: two native file-only workers with incremental results, scoped artifact
  exchange, input-delivery evidence, explicit recovery and observed stop.
- P3–P5: isolated writers and candidate verification, real browser controls,
  packaged Windows tests, local and hosted provider qualification and outcome pilot.
- P6–P7: authenticated two-member/two-host governance, protected audit and content
  access, revocation/offline leases, then explicitly granted cross-session work.

Fixtures, live provider results, packaged operation and hosted enforcement are
different evidence. Full P0–P7 MVP completion requires every corresponding gate.
