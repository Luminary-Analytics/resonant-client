# Team preview

Lumi's **Team** panel coordinates scoped investigations and isolated file
changes within a saved conversation. The feature is off by default. This guide
describes the current source implementation; it is not a packaged-release or
live-model qualification. See the [progress and evidence ledger](swarming-progress.md)
for the remaining roadmap work and the kinds of checks actually performed.

## Organization policy, excluded files and current limits

The preview was built before Lumi's organization controls and follows only some
of them so far (September 27, 2026 port onto the `lumi` package):

- **Organization policy.** Where a policy applies (a machine policy, a managed
  profile or one from Lumi Cloud), the panel can view, pause, stop, revoke and
  recover retained work, but it doesn't start or change team work: workers
  don't yet follow the organization's model, mode, shell, approval or sharing
  rules. An invalid or expired policy refuses the same actions. See
  `policy_refusal` in `engine/swarming/service.py`.
- **Excluded files.** Every worker gets the project's exclusions when it
  starts: Settings' excluded paths, the project's `.lumiignore` and, when one
  applies, the organization's `files.exclude`. A writer anchors them at its
  isolated worktree. Searches leave excluded files out. A worker's read of one
  is refused; the model never receives the file.
- **Calls outside an assignment.** A worker's file call outside its granted
  paths, or to a tool it wasn't given, is refused before it runs. The model
  hears "Refused before running" with the reason as that call's result and can
  narrow it; the run keeps a `tool_refused` event. Other guard failures (lost
  admission, an unobservable response) still stop the worker.
- **Hooks.** Workers don't run your Settings or capability-pack hooks; a
  guarded worker refuses them, unlike chats and plan specialists.
- **Providers.** Workers use native Ollama, EXO, Kimi, OpenRouter or SONN
  connections, or a custom connection of type **OpenAI-compatible** (Chat
  Completions: NVIDIA NIM, vLLM, a gateway) that authenticates with a key or
  none. A run reads the connection once when it starts. Anthropic, OpenAI
  Responses, Azure, Bedrock and Vertex connections, OAuth or Entra sign-in and
  client certificates aren't available to workers yet.
- **Usage and budgets.** Worker turns run through the same session path as a
  chat, so their requests are recorded in usage and the audit log and checked
  against spending limits before each turn. A writer's records carry its
  worktree's path rather than the project's.

## Start a team

Open a saved conversation in the intended project, then choose **Team** and
enable **team preview**. The panel displays that conversation's saved provider
and model. It preserves the explicit choice; it does not select a different
model for you. The source preview accepts native Ollama, EXO, Kimi, OpenRouter and
SONN sessions, and OpenAI-compatible connections such as NVIDIA NIM; live
provider qualification remains pending.
Codex and Claude Code CLI sessions are not team worker providers.
The [provider matrix](swarming-provider-matrix.md) distinguishes source eligibility
and execution checks from live qualification.

The panel captures the project, conversation and run it opened. Closing the
panel does not stop its workers. Its view refreshes while open and reconnects to
the same server-owned run after a socket interruption. Use **Refresh team** to
inspect current state after a rejected or uncertain command; a lost response
does not authorize replaying it. Ordinary chat, model changes and project/session
navigation are gated while the client owns active team work.

Choose one of two planning approaches:

- **Assign investigations myself:** enter focused tasks and readable paths.
  The UI offers one to eight worker slots, defaults to two, and permits up to
  eight tasks, bounded by the selected slots. Organization-managed teams keep
  their policy's limit (at most four). Tasks begin read only.
- **Ask a coordinator to propose a plan:** enter an objective, coordinator
  allowance and allowance per worker. The coordinator reads within its scope
  and submits a proposal. It does not approve its own plan or task results.

### Let the orchestrator run the team

With a coordinator plan, **Let the orchestrator run the team** and choose
**Orchestrator rounds** (one to eight). The coordinator becomes the team's
orchestrator, and you don't approve each step:

- Its plans run once the runtime has validated them against the team's
  scopes, criteria and request allowance, as it would for your approval.
- Workers' findings are accepted **under your grant**, so later tasks and the
  next round can use them. The record says so: executor `autonomy:<owner>`,
  "Accepted under the owner's autonomy grant, not reviewed". It never claims
  your review.
- A failed task, or an orchestrator turn without a usable plan, is retried once.
- When a round's work is accepted, the orchestrator plans again from the
  findings and from workers' messages to it. It can finish early by proposing
  no more work. After the last round, a closing turn writes the final report
  and may not start work; any work it proposes is rejected.

The **Orchestrator** section shows the round, the current step and the final
report. You can pause, steer or stop the team at any time. The orchestrator
loop runs in the app that started the team; after a restart, continue the team
yourself. See `engine/swarming/autopilot.py`.

File changes wait for you to check, apply and accept them, unless you also
turn on **Apply changes that pass every check**, offered when the team has
writable folders and verification checks. Then, once a round's writers finish,
the orchestrator takes the steps you would, through the same integration path:

1. It combines the writers' changes in a separate worktree.
2. It runs every declared check on the combined change.
3. When all of them pass, it applies the change to your checkout (a
   fast-forward from the captured base, never over uncommitted work).
4. It accepts the writers under your grant. The acceptance is recorded with
   `autonomy:<owner>`, never as your decision, and an exported report marks it
   `autonomy_grant`.

Every declared check runs on each round's combined change, so declare checks
the project should pass after every round (its test suite, a linter), not a
check that only passes once all of the objective is done. The orchestrator is
told to put the work a check needs in the same round. A failing check sends
each writer in the change back once, with the check's output in its retry. Changes that conflict with each other, a step without a
known outcome, or a checkout that has uncommitted changes or moved hand the team
back to you; the orchestrator never retries an application. Later writers start
from the team's latest applied change, so later rounds build on earlier ones.
Keep the checkout clean and on its branch while such a team runs.

Paths are relative to the project. Separate roots with commas; `.` means the
project and its descendants. A file can also be a scope root. Absolute paths,
parent traversal and wildcard scopes are not supported. Scopes are permissions
for the guarded runtime, not a claim that arbitrary project code is an OS sandbox.

**Total model requests** is an execution allowance, not a token count or dollar
budget. It includes primary and auxiliary planning/compression requests. Reserved
or uncertain requests continue to hold allowance until their outcome is known.
Coordinator setup requires at least its allowance plus one worker allowance;
larger plans can need more. The panel displays blocked scheduling and remaining
allowance. No result is complete merely because its request allowance ended.

## Share with another personal conversation

Open **Share with another personal conversation** in the Team panel. For an
idle receiving team, enter its objective and total request allowance, then
choose **Prepare collaboration team**. This starts no model. Prepare a separate
team in another saved conversation in the same project, using its own saved
native model. Saved navigation remains available while prepared teams have no
active or uncertain work; ordinary chat and provider changes stay gated.

Copy **This conversation’s team address** into the other panel's offer form.
Choose the purpose, permitted message/content types, expiry, disclosure limits
and accepted-work request ceiling. Offers are direct only; either team can revoke.
The receiver must inspect and **Accept sharing agreement** before messages can
be sent. No history, credentials or budget transfers with the agreement.

**Read message** records explicit delivery before displaying incoming content.
It does not send the text to a model or accept work. A work proposal requires a
separate receiver-chosen investigation, readable paths, request allowance and
acceptance notes. **Accept and start my investigation** may launch immediately,
using that conversation's model and remaining allowance. Its findings still
need the normal **Accept findings** owner review. Collaborative work authorizes
one attempt; another attempt needs a fresh explicit proposal or independent task.

**Revoke sharing agreement**, Stop, expiry or an authority change closes future
deliveries and work acceptance. Already accepted peer work continues under its
own team's controls. Already delivered messages remain retained history, in
bounded pages. Artifact offers share selected IDs, hashes and sizes only; they
do not grant artifact content access or establish visual evidence. This source
preview supports personal conversations with the same owner/project. It does
not authorize managed-organization or cross-user collaboration.

## Review a proposed plan

Review the summary, every task's role and paths, dependencies, and required
acceptance checks. Enter **Plan review notes**, then approve or reject the exact
displayed proposal. Approval becomes available only after the coordinator has
stopped. A changed proposal invalidates draft decision notes.

Approval starts eligible work within the chosen slots and request allowance.
Dependencies wait for accepted prerequisite results. Rejection starts no proposed
workers. The coordinator may propose one task when a team would not help; the
preview makes no automatic speedup claim.

For a running team created with coordinator planning, use **Request follow-up
plan** to consider partial findings while independent workers continue. Choose
the **Follow-up coordinator allowance** from the team's remaining requests.
**Follow-up readable folders** starts with the original coordinator scope;
leave it empty for findings-only planning with no file read/search tools. The
saved model, trusted checks and allowance per worker remain unchanged. Editing
these fields or refreshing the panel makes no model call, and drafts survive
polling and navigation between saved teams while the panel stays open.

The fresh coordinator receives the captured graph and bounded retained findings.
Its proposal adds work without replacing attempted tasks. Review and approve the
exact proposal separately; a changing graph may require another explicit plan.
Requesting a plan never accepts existing findings, cancels peers, or automatically
replays interrupted work. The panel explains when the current coordinator,
remaining allowance or missing execution host prevents another request.

## Team messages

Workers running at the same time can share findings and ask each other
questions with `swarm_send`, and wait up to 60 seconds for an answer with
`swarm_receive` (Pause or Stop ends the wait). A worker can also write to
`orchestrator`: the orchestrator's turn may have ended, so its next round
reads the message as planning input. Messages are untrusted data. They never
widen a worker's paths, tools or allowance, and a reply doesn't prove the
recipient followed it. **Team messages** in the panel lists who told whom what.

## Review findings

Expand a worker to see its assigned task, captured model, read/write access,
execution state, submitted findings and retained evidence references. **Awaiting
verification** means a result was submitted; it does not mean the task passed.

For a stopped read-only result, enter **Review notes** explaining what you
checked, then choose **Accept findings**. This records an owner decision bound to
that exact result. It does not pretend to be an executed automated check.

**Reject result** requires reasons and preserves the old attempt. **Retry task**
requires a separate decision and requests a fresh attempt of the retained task
contract, subject to available allowance and resolved prior effects. Neither
button silently resets unknown work or refunds uncertain requests.

## Guide an individual participant

Expand an active worker or coordinator to pause, resume or stop just that
participant. **Pause** records a request first; the panel calls it paused only
after the runtime observes a checkpoint. An admitted provider call or tool may
still be finishing. **Stop** is irreversible for that attempt and does not stop
its peers. The panel distinguishes a stop request from confirmed termination.
Even after termination, an interrupted model request can remain uncertain and
keep its allowance held.

Global Pause and Stop take precedence: resuming one participant cannot resume
a paused team or undo cancellation. Resume the team first, then resume any
participant that remains individually paused.

Enter **Task guidance** to clarify the existing assignment, then select **Send
guidance**. This keeps the same task permissions, file scopes and tools. Guidance
is bound to the exact worker or coordinator attempt; its draft survives polling
and does not transfer to a replacement attempt. The panel shows **Queued for
this participant** after durable acceptance and **Included in prepared model
input** only when there is an exact input receipt. These receipts do not establish
model comprehension, compliance, provider completion or task quality. If a reply
is lost, inspect retained guidance before choosing whether to send it again.

## Allow and review file changes

Writer setup requires a clean, committed Git checkout at the repository root
on a branch. A project opened at a repository subdirectory cannot start writers;
the client does not widen its workspace. The team
captures its base revision and target branch before dispatch. Enable **Allow
scoped file changes**, declare the team's writable roots, and define trusted
verification commands before starting. The UI allows up to eight named checks.
Each check specifies an executable, literal arguments entered one per line,
and a timeout from one to 1,200 seconds. These commands execute locally with
your account's permissions. They are owner-selected verification, not commands
chosen by a worker. Do not put shell quoting, pipes or redirects in argument lines.

For each manual writer, select **Implement file changes**, narrow its writable
roots within the team's roots, and list its required check names. The total
allowance must cover the configured per-worker allowances. A coordinator may
propose writers only when writable roots and trusted checks were configured.

Writers work in isolated Git worktrees. Their submitted output does not change
your working checkout. The **Review file changes** section shows the captured
base, finalized writer revisions and changed paths.

1. Select compatible stopped writer results and choose **Prepare selected
   changes**. This creates a combined candidate for review.
2. Choose **Inspect candidate** and read the changed paths and diff for its exact
   base and result revisions. A failed, mismatched or truncated preview keeps
   application unavailable. The current panel has no full-diff fallback for a
   truncated preview.
3. Run the named checks. Review their observed exit status and output. Passing
   checks apply only to the exact candidate revision that was checked.
4. Enter **Application review notes** and choose **Apply reviewed changes**.
   Application requires the captured branch/base still to match and the checkout
   to be clean. An edit you made after the team started is preserved and blocks
   application until you resolve it yourself. The client does not reset or stash it.
5. Review each applied task and enter **Acceptance notes** before choosing
   **Accept applied result**. Applying the candidate and accepting its tasks are
   separate decisions.

Conflicts, check failures and uncertain application remain visible. A failed
operation is not evidence that all effects were rolled back. Inspect retained
state before deciding on repair, retry or recovery.

## Pause, stop and recover

**Pause new work** stops admission and waits for admitted activity to reach an
observed checkpoint. **Resume** continues the retained run. **Stop team** asks
the owned workers to stop and preserves observations, results and unknown effects.
Disabling the preview prevents new teams; it does not cancel an existing one.

To adjust parallel work, choose an **Active worker limit** and select **Apply
worker limit**. This works while the team is running, pausing or paused. The
limit ranges from one to the team's original worker cap; it cannot expand that
policy. Lowering it leaves existing workers running and limits only new
assignments. The panel shows assigned/unresolved worker occupancy beside the
current limit and counts the coordinator separately. Editing the selection does
not send a command, and background refreshes preserve that draft. A stale
revision requires inspection and a new explicit decision.

After a host interruption, open the affected saved conversation and inspect its
team. **Take over expired team** fences the prior supervisor's admission. It
starts no workers and does not prove that a process stopped or a request was free.
While the prior lease is still active, the panel shows an approximate remaining
wait and disables takeover. Polling updates that display; the server makes the
final ownership decision.

- **Check process** obtains a trusted host observation of the captured process.
  **Record host observation** is available only when that observation establishes
  termination. An unknown observation remains unknown; the UI has no field for
  asserting a PID or claiming termination.
- Reconcile uncertain requests and tool actions only from observed evidence.
  Outcome fields start empty. A request marked completed or failed consumes one
  request; **not started** uses zero only when the retained lifecycle and evidence
  support that conclusion. There is no automatic refund.
- Choose exactly which failed or cancelled tasks should be retried. **Continue
  reviewed team** also includes already-ready work and respects dependencies.
  Unknown effects and held allowances can prevent continuation. During owned
  recovery, Stop clears retry selection; **Finish stopped team** drains the
  stopped run without starting replacement workers.

The panel also offers retained writer, candidate, check and application effect
reconciliation. Select **Inspect and reconcile** on the relevant interrupted
record. For writer, candidate and check effects, the host checks the captured
local process identity; lost writer or candidate results are abandoned, and a
lost check result is not recorded as passing. Integration operations close only
from their retained effect evidence. Branch application inspection separately
compares the checkout revision with the exact approved base and target; process
exit alone does not prove that changes were applied.

Some evidence cannot be reconstructed safely: legacy records without the current
process-containment protocol or captured application-checkout identity, a process
on another or unavailable host, or an application whose checkout matches neither
its approved base nor target. A different branch or linked worktree at the same
commit cannot prove application to the captured destination. These
remain unresolved and block continuation where applicable. Preserve the records
and investigate the original host or checkout; the panel cannot replace missing
evidence with an assertion of success or silently reset the user's work.

## Finish and retain a report

**Complete team** appears only when every task is accepted; the server also
checks unresolved activity before completing. A finished or stopped run remains
inspectable. **New team** opens setup for another run without deleting history.

Open **Saved teams in this conversation**, choose a run, then select **Open saved
team** to inspect it. **Older teams** and **Newer teams** page through retained
runs without changing conversations. All controls remain bound to the selected
run and the conversation captured when the panel opened. Setup and review drafts
survive switching teams within the open panel; background refreshes preserve
focused fields. A late response from a previously opened panel cannot replace
the current selection.

Expand a worker, choose **Retained evidence**, and select **Read evidence** to
inspect its exact saved text. The host checks the entire immutable blob's size,
SHA-256 digest and UTF-8 content before returning each page, including bytes beyond
that page. **Next evidence page** and **Previous evidence page** navigate Unicode
character offsets. The viewer renders plain text, including any markup as literal
characters; it does not execute evidence. Missing or changed content prevents
disclosure. Images, audio, video and binary artifacts are identified without a
text preview or a claim of visual interpretation. Owner access to these records
does not grant workers new access or execution authority.

**Export run report** downloads a local `SONN-swarm-<run-id>.json` file for the
captured run. It contains metadata and evidence references, request allowances,
known/unknown usage and review/check outcomes. It omits message bodies,
transcripts and prompts. This is a local download, not an upload or an external
share, and reported completion is not a measurement of model quality or speedup.

For implementation contracts, see [swarming contracts](swarming-contracts.md).
For declared fixture objectives and evidence limits, see
[swarming benchmarks](swarming-benchmarks.md). Production provider evaluation,
packaged qualification, enterprise governance and cross-session collaboration
remain tracked separately in the [roadmap](swarming-plan.md).
