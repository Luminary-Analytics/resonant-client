# SONN connection

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

**September 15 status:** AI Employee source integration is paused at the user's
request. Read the [resume handoff](ai-employees-handoff.md) for implemented scope,
evidence and unfinished work. The following unreleased contracts are not present
in the unchanged installed bundle; no learned-benefit qualification is claimed.

## Unreleased native employee task panel

In a saved SONN conversation, **Employee task** sets a total allowance and an
optional declared complexity. Every following execution, compression and advice
call shares the saved root's eight-call/two-advice/15-minute bounds. Server task
serving and a bound employee are required. Setup/advice/recovery actions use the
same queue as chat, preventing an ordinary turn from racing task setup or advice.

The private task journal restores before generation after a backend restart.
Malformed or incompatible state fails closed. An unstarted setup can be corrected;
old creation identities remain in its history and late old responses cannot attach
or dispatch a model. Once a root is attached its allowance cannot be revised.

The panel shows charged/reserved allowance, calls and deadline, and offers explicit
frontier advice, cancellation, receipt recovery and **Inspect assignments**. The
inspector shows each employee's contract, dependencies, outputs and execution state.
Reported artifacts remain visibly unverified; blocked integration, expired leases
and unresolved file actions retain their recovery requirements. Worker capabilities
and private receipts stay on the host. Advice requires separate saved
server-side consent and feeds exactly one following execution. Recovery can settle
saved worker receipts too, without repeating model calls. Unknown accounting never
means zero. Returning to ordinary per-request work requires explicit action after
closure/deadline and reconciliation; the original task history remains. A fresh
conversation is required for another bounded task.

**Consultation purpose** selects execution advice or employee coordination. Both
use the saved frontier profile and share the two-consultation allowance. Coordination
also requires orchestration serving; it returns unverified advice for the next
execution and cannot itself launch employees or certify a result. Lost answers use
the original receipt after restart, without repeating the consultation.

Native component/browser and controller checks pass with scripted inputs. Full
packaged desktop, live WebSocket workflow and live-model qualification remain open.

## Unreleased scoped employee workers

The owner task controller exposes `assign_nodes`, `graph`, and durable `claim_node`
for a bounded server graph. Claim results contain a private capability for one
node; never serialize them into UI state or model instructions. A repeated claim
with `dispatch=False` cannot start another worker. Host recovery remains required.
After trusted server recovery, `reclaim_node(node_id, stopped_epoch=...)` can rotate
the native claim identity once. It requires the server's recorded stopped epoch,
preserves prior claim IDs, and reuses the renewed claim after a lost response or
restart. It cannot attest process termination or settle unknown model usage.

`SonnWorkerBackend` and `create_worker_session` use that capability under the exact
worker API path. Workers retain generated-input provenance, share the root model
allowance, and expose only file read/write/edit/glob/grep in an isolated epoch
directory. Their SQLite request journal must live outside the tool workspace.
Every file action requires a one-time server claim; uncertain actions are not
retried. `install_worker_inputs` verifies signed dependency identities and bytes,
preserving each dependency under `inputs/<source-hash>/<original-path>` with a
generated source-to-path manifest. Different versions of the same file remain
available to the coordinator. Malformed paths, case collisions within one source,
file/directory collisions and changed bytes fail before copying. Worker output remains reported
until an independent host verifies integration. Workers cannot create another
root, choose a different model, consult an advisor or access workspace account APIs.

This is a programmatic specialist candidate with real native-process/scripted-provider
evidence. The specialist desktop workflow, trusted host recovery and full G4 qualification
remain pending. No running bundle has been rebuilt.

## Unreleased employee task adapter

The native backend has an opt-in `enable_employee_task(journal_path,
ceiling_microusd=..., descriptor=None)` contract after a saved conversation is bound. This starts a
server-issued root with an explicit total allowance. The native employee task
panel now exposes the basic task/advice controls; specialist orchestration remains
programmatic. Existing sessions do not change automatically.

The SQLite journal persists the root and each request identity before dispatch.
Every task call uses a stable idempotency key and a one-upstream-call wrapper limit.
Generated compression shares the root while disabling capture and learning;
optional title generation is deferred. Restart cannot resubmit an unresolved call.
The optional descriptor freezes declared task/dependency breadth (0–8), ambiguity
(0–1), and a missing-prerequisite boolean. These are owner declarations, not verified
outcomes. Changing them requires an explicit new task. Generated compression declares
its own non-learning purpose so automatic routing does not inherit the parent task's
frontier complexity classification; it still consumes the same shared allowance.
`task_controller.recover()` queries the original request and only reconciles known
server usage. Missing or uncertain usage remains unresolved. Recovery never calls
the model. A new root is an explicit new task, not automatic error recovery.

Stop persists local cancellation immediately and sends root cancellation in a
bounded background request. This blocks new claims; already dispatched work and
uncertain billing still need reconciliation. An ordinary connection without a root
retains its existing local-stream cancellation behavior.

`backend.consult_employee(question, source_teaching_ids=None)` requests one
owner-consented frontier consultation within that root. It uses a closed input
prompt and disables observation capture. The journal retains the recorded answer
and its identity; the next ordinary execution sends that identity to SONN, which
adds the original answer only after checking all source dependencies. Compression
does not consume this link. A settled answer lost before local persistence is
recovered through the owned knowledge receipt without another model call.
The root caps advice at two calls within eight total calls. Automatic learned
advice selection and a desktop task/advice UI remain qualification work; this is
an explicit adapter contract. A fluent answer is not a successful execution.

The task adapter passed real local HTTP/SSE, file-tool, restart and compression
checks with SONN's actual gateway and a scripted provider. This is native source
engine evidence, not packaged desktop, paid-model, or learned-routing qualification.
Advice and specialist orchestration are still under development.

Available in Lumi v0.18.0. SONN supplies a project-scoped Chat Completions
endpoint; Lumi runs the coding tools and retains project instructions,
permissions, notes, history, and verification through its existing engine.

## Set up in the desktop app

1. Open **Settings > Connections > Network**. Paste your complete SONN API base URL, for example
   `https://getsonn.com/v1/workspace/projects/<project-id>/openai/v1`.
2. Under **API keys**, enter your private invitation in **SONN API key**, then
   leave the field to save. The field becomes empty and shows **Stored**. Keys
   are stored locally in `~/.lumi/settings.json`, not returned to the UI.
3. Under **Connections**, select **Check SONN connection & refresh models**.
   This performs authenticated model discovery without generating tokens.
4. Return to your session, open **Models**, and select `sonn-auto` under SONN.
   Connecting does not change your existing model or provider. Save a project
   preference only if you want SONN to apply to that project's new sessions.

`YOUR_PRIVATE_INVITATION` is a placeholder, not a usable key. Keep the real key
out of chat, logs, fixtures, and source control. Stop an active run before
changing its SONN connection. Clearing its credentials disables sending until
you select a working connection; an environment key can still supply access.

For managed configuration, `SONN_API_URL` overrides the saved URL and
`SONN_API_KEY` supplies the key when no saved key exists. HTTPS is required
except for HTTP loopback endpoints used by local services and tests. Embedded
URL credentials, query strings, and fragments are rejected.

## Wire contract and capabilities

### SONN account and credits

The bottom-left profile belongs to SONN. **SONN account & credits** reads
`GET /v1/workspace` with the configured invitation as a Bearer credential, at the
same origin/prefix as the documented project URL. The invitation is account-wide;
the project URL chooses the inference project. It is not a project-restricted key.

This contract was checked against SONN's `product/src/sonn_server/workspace.py`
(`handle`) and `sonn_billing/service.py` (`customer_account`) in the Lumina_DO
source on September 13, 2026. `identity.user` is an authenticated identifier;
there is no full-name/avatar or OAuth contract in this version. Profile's
display name is a local label. ChatGPT account identity never supplies this profile.

The account panel shows available, reserved, and total-charged integer microusd
values as dollars. Missing amounts remain unavailable, not zero. SONN is prepaid;
billing-off and test-checkout modes are explicit. The last-check timestamp marks
a snapshot. Refresh reads account state without generation; top-ups are handled
in the SONN web workspace. No automatic checkout or credit purchase occurs.

Account requests have an eight-second HTTP timeout and a two-megabyte response
limit, reject redirects, and expose only allowlisted identity/billing fields.
Credential/URL changes invalidate in-flight results. Keys remain in the existing
secret store and are never included in workspace links or frontend account events.

### Model transport

- `GET {base_url}/models`, with `Authorization: Bearer <key>`, discovers model IDs
  from the standard `data` array. The documented fallback is `sonn-auto`.
- `POST {base_url}/chat/completions` sends standard messages, streaming SSE,
  optional `max_tokens`, `stream_options.include_usage`, and top-level function
  tools. No extra `/v1` is appended to the supplied project path.
- Text deltas, fragmented tool arguments, tool results, and token usage use the
  existing engine event contract. Retained system summaries survive handoff.
  Moonshot tool catalogs and provider-specific reasoning continuation are not
  replayed to SONN.
- Model catalogs are cached for five minutes per URL and credential fingerprint,
  with at most 16 cached catalogs. The connection button forces a refresh.
  Network probes run outside the UI event loop.
- The supplied contract does not specify vision, reasoning controls, prices,
  parallel tool support, or context size. The adapter uses text-only conservative
  defaults and a 32,768-token context unless discovery supplies a positive
  `context_length`. It attempts standard function tools; live routed-model
  support still needs validation. No SONN dollar-cost accuracy is claimed.
- **Stop** closes the local streaming response. SONN has not supplied a remote
  cancellation endpoint, so this does not prove server computation or billing
  stopped. Errors are sanitized before display and retry diagnostics.

## Validation and troubleshooting

Unreleased source now sends an opaque `user` conversation label derived from
the saved desktop project/session before native SONN turns. It remains stable
across reloads and model changes, and separates fresh sessions. This label is
not an account credential. The installed September 13 qualification build did
not send a stable identity, so its server-side session continuity is unqualified.
The 0.19.2.dev3 candidate excludes generated title/compression calls from learning
and isolates their identities. Harness-generated user turns carry explicit input
exclusions. The clean candidate resumed a saved native desktop conversation after process
restart; new observations reached the same hosted SONN session. Worker identity
remains unqualified; live model-summary compaction and continued coding passed;
see [Unreleased](unreleased.md).

The automated suite exercises mock model discovery, SSE streaming, function
calls, cancellation, credential changes, redaction, and a real engine loop
writing a fixture file. Browser checks use a local mock HTTP endpoint. These
checks validate the client contract. The historical 0.18.0 release did not
qualify authenticated generation; the September 14 local candidate now has
native SONN coding and restored-session evidence. See the historical
[release evidence](v0.18.0-release-notes.md) and current [Unreleased](unreleased.md).

For rejected credentials, check the key and access to the configured project.
For a missing endpoint/model, check the complete project URL and model ID.
For a usage limit, check your SONN account allowance. A successful connection
check proves model discovery only; send a small coding task to validate
generation after selecting SONN.

### Long-session compaction

The September 14 live repair exposed a malformed compaction-summary failure.
The candidate can preserve the complete transcript as a readable artifact and
continue from mechanically retained requirements, checklist, tool observations
and the most recent call/result pair. It explicitly reports that no valid model
summary was accepted. Missing archive access/storage and provider errors retain
the previous fail-closed behavior; recovery is not an automatic paid retry.


### Long-run controls (0.19.2.dev5 candidate)

Settings > General > Workflow now offers **Model requests per turn**. Zero keeps
normal runs unlimited. A positive integer bounds main native engine requests,
including empty-response and completion recovery attempts. At the boundary the
client retains work and offers continuation; a new turn receives a fresh allowance.
This is not a dollar budget, a tool-call count, an auxiliary-summary limit or a
limit on Codex/Claude CLI loops or delegated workers.

The native chat runner currently owns one active project at a time. Finish or
stop its turn before switching projects, loading another session, forking or
clearing. The client rejects those changes during a run, including the race while
a folder resolves. Stop targets the captured active engine; persistence targets
the captured session record. Completed step boundaries save conversation history
before the whole turn ends. Interrupted external calls can still finish remotely;
check SONN reservations before retrying an uncertain paid request.

The September 14 long-run qualification found the old project-switch/Stop failure
through the native desktop. That interrupted attempt is preserved separately from
subsequent candidate checks. The candidate is local and has not been published.

### Interrupted SONN requests (0.19.2.dev6 candidate)

Credit exhaustion, request limits, capacity errors and service failures have
distinct messages. Stream failures no longer direct users to change working
credentials. Messages retain no raw provider detail. SONN does not automatically
replay HTTP or in-stream errors: an uncertain upstream generation can still incur
usage. Check request status and reservations before explicitly continuing.
SONN timeout messages use explicit SONN wording; the adapter continues to inherit
Kimi transport and does not inherit EXO runner state.

Dev7 also rejects multiline Windows shell commands before dispatch. The Windows
command runner can otherwise return zero while ignoring code after a newline.
Write a project script and invoke it with a single-line command. Four boundary
checks pass; the observed silent non-execution is preserved in qualification.

Dev8 fixes browser screenshots in SONN tool history. The gateway currently accepts
text only: retain image artifacts locally, preserve tool text, and direct the
agent to DOM/accessibility/evaluation evidence. Do not append a synthetic human
image message. The actual failed GearDesk history was rejected by the gateway
parser before this fix and accepted afterward, without changing saved history.

### Learning queue recovery (0.19.2.dev9 candidate)

The client recognizes the structured HTTP429 `learning_queue_full` admission code.
It waits for the worker and retries the identical request at most twice, with
60–120 seconds per wait and responsive Stop. Invalid or longer Retry-After values
stop the turn. This code is issued before reservation and paid model dispatch;
other429s, service failures, timeouts and interrupted streams are not replayed.
The final message distinguishes pending learning from insufficient credits.
This bounded recovery does not guarantee sustained queue throughput.
