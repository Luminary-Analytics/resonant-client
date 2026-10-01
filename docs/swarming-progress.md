# Swarming delivery and evidence

Updated September 26, 2026. **MVP is not complete.**

The user authorized autonomous implementation and explicitly selected **all
phases P0–P7** as the completion target. Work proceeds local-first as in the
[roadmap](../ROADMAP.md#planned-feature-swarming-2026-09-26), then enterprise and
cross-session phases. The separate AI Employee initiative and its heartbeat
remain paused. The installed application has not been rebuilt or deployed.

The user clarified that work must continue continuously in this task, without a
timed schedule. The mistakenly created `complete-sonn-swarming-mvp` heartbeat
was removed. Continue toward the persistent P0–P7 goal; notify on full completion
or a concrete blocker needing input. Routine continuation needs no new prompt.

## Milestone ledger

| Phase | Current state | Evidence required before completion |
| --- | --- | --- |
| P0, SW-001–003 | Initial source gate complete | Reviewed contracts, predeclared benchmark fixture pack, atomic storage/protocol checks, initial schema installation, backup/reopen/version rejection and reconciliation are recorded below. No model baseline has been measured. |
| P1, SW-004–008 | Source gate verified | Graph, policy, reservations, revisions, leases, migrations and actual killed-host recovery fixtures pass; main regression passed 4,230 tests, with later fixes separately qualified |
| P2, SW-009–012 | Source, browser and candidate `.11` gate verified | Asynchronous readers, peer input receipts, initial and partial-result follow-up coordinator planning, pause/Stop and retained recovery pass. Explicit additional plans preserve existing work and require owner approval. Live provider qualification belongs to P5 |
| P3, SW-013–017 | Source and candidate `.11` reference scenarios verified | Native writers, real Git isolation/check/application, repair, dirty-checkout refusal and interruption recovery pass; final application proof binds the original checkout and branch |
| P4, SW-018–021 | Source and packaged reference gate verified, default-off Team preview | Actual browser controls, retained history/artifacts, compact keyboard use, delayed-reply Stop, CSV repair/application and killed-host recovery pass. This scripted qualification does not establish a supported live-model release |
| P5, SW-022–024 | Four-scenario comparative runner implemented | Guarded single/swarm CSV, serial, investigation and processor-repair fixtures run with scripted inference; existing batch remains unsupported pending its execution boundaries. Live local plus hosted qualification, controlled pilot and outcome comparison remain |
| P6, SW-025–029 | Managed source and packaged scenarios pass; operational qualification remains | Real PostgreSQL, OIDC/JWT HTTP, SCIM, mTLS host/quota, encrypted text, monitoring and separate audit archive fixtures pass. Candidate `.11` writer/check/apply and actual killed-host recovery pass. External identity, independent machines, deployment custody and return-to-service readiness remain |
| P7, SW-030–032 | Personal and managed browser/candidate `.11` controls verified with scripted inference | Bilateral grants, cumulative causal limits, session cycles, disclosure, revocation, retention and independent receiver budgets pass with two actual TLS identities and native runtimes. External-host qualification and P5/P6 dependencies remain |

## Work in this session

- Inspected the existing dirty checkout before edits. Preserved `.claude/launch.json`
  and the prior roadmap/plan/glossary/documentation additions.
- Audited the worker/runtime seams and recorded [implementation contracts](swarming-contracts.md).
  Existing delegation auto-approves children and does not provide a universal tool
  allowlist boundary. The new feature needs its own authoritative runtime.
- Implemented and independently reviewed the isolated SQLite storage spike and
  deterministic benchmark fixtures. Neither is wired into normal sessions or
  permits provider calls. `SwarmStore` is a trusted internal persistence API,
  not a supervisor, worker capability token, network API or authenticated service.
- Existing regression baseline at collection time: `python -m pytest -q --tb=short`
  **3,358 passed, 2 skipped**, 225.17 seconds, Python 3.13.5 on Windows. The new
  swarm tests had not been collected; their results are separate below.
- Existing Ruff check, `app.js`/`settings_view.js` syntax checks and all **22**
  Node UI recovery checks passed. These are source/fixture checks, not a new
  browser exercise or packaged qualification.
- New storage suite: `python -m pytest -q tests/test_swarm_store.py --tb=short`
  **25 passed**, 6.41 seconds. Real spawned Windows processes compete for a work
  claim and remaining allowance; child processes abruptly exit before/after a
  transaction commit. Other checks cover rollback, scoped reads/retries, message
  receipts, stale epochs, stop/terminal access, backpressure and retained uncertainty.
- New benchmark suite: `python -m pytest -q tests/test_swarm_benchmark_fixtures.py --tb=short`
  **30 passed**, 6.06 seconds. Unsolved seeds fail external checks, references pass,
  and changed inputs, incomplete results and verifier failures are detected.
  [Benchmark documentation](swarming-benchmarks.md) defines the predeclared design;
  no provider execution or comparison was performed.
- Independent review found and corrected authority renewal through creation retry,
  mailbox disclosure/new receipts after stop or terminal state, and retained SQLite
  administrative handles on Windows. The regressions above cover these cases.
- Full Ruff and `git diff --check` pass after the new code. Existing Git line-ending
  warnings remain. The fixture pack pins LF in its own `.gitattributes` so checked-in
  hashes survive Windows checkout. No tracked runtime state or credentials were added.

The next source slice adds schema version 2 and a deterministic `SwarmSupervisor`.
It owns the acyclic work graph, immutable assignment contracts, optimistic command
revisions, explicit native model selection, policy intersection, worker capacity,
scope conflicts, request admission and expiring supervisor leases. Lease takeover
fences previous epochs and retains uncertain requests/actions. Stop remains sticky
through recovery; a paused or cancelled label requires the relevant effects to
settle. Submitted read results need exact-candidate trusted criterion receipts;
writer acceptance remains unavailable until the integration protocol is present.

- Store/supervisor/policy suites: **127 passed**. Migration tests construct a
  version-1 fixture, verify the coherent pre-upgrade backup and retained history,
  and inject a failure during migration to prove transaction rollback. This is
  fixture migration evidence, not migration of existing user swarm sessions.
- Scoped artifact/mailbox suites: **44 passed**, 2.37 seconds. Content hashes,
  explicit same-run disclosure, sticky revocation, symlink escapes, input/body
  substitution, lost wakeups, rollback and stopped/foreign-scope access are checked.
  Artifact disclosure and message acceptance share one transaction.
- Native execution bridge: **31 passed**, 2.65 seconds. A controlled native
  provider exercises actual `Session` file reads and attributed artifacts; an
  enforced 15-request loop, changed backend identity, stop between input and
  request start, ambiguous commit and message receipt rollback are covered.
- Independent review identified hook effects outside the guard, mutable inputs
  after admission, malformed tool observations and unavailable classification
  capability. Focused regression fixes are being integrated with the worker runner.

Native request identity and input digest precede provider invocation. Main and
auxiliary requests are separately attributed; missing token/cost data stays unknown.
Tool intent and results bind the originating completed main request and argument
digest. Message context receipts mean a durably prepared input; request start and
completion are separate observations, and none prove model comprehension.
These are controlled-provider source tests. No live provider qualification,
packaged swarm or hosted governance is claimed.

Further integration checkpoints:

- Full Python suite collected after the reader boundaries: **3,626 passed,
  2 skipped**, 235.01 seconds. Later writer/UI changes are newer than that
  collection and require their own checks; this is not a final release gate.
- A later broad regression collection completed with **3,909 passed, 3 skipped,
  6 failed**, 519.10 seconds. The six failures came from GUI fixture startup
  interfaces and a missing optional command-context run queue. All were fixed;
  the three affected files then passed **63 tests**, 1.77 seconds. New writer
  workflow changes postdate that collection and are validated separately.
- Scheduler/recovery desktop checks passed **29 tests**, 6.24 seconds. Closing
  dispatch after assignment commit cannot launch a worker, terminal schedulers
  retire, and a fresh owner can explicitly review retained exact read findings
  after takeover and complete the run without replaying its model calls.
- The desktop writer service passed **3 real-Git source fixtures**, 7.60 seconds:
  explicit write scope/check configuration, a scripted native write, candidate
  construction, an actual named subprocess check, deferred application over a
  newly dirty checkout, exact reviewed application after the fixture restores its
  own file, separate owner acceptance, and completion. Dirty initial baselines
  are rejected before reserving work; the app never cleans them. These fixtures
  use injected providers and cooperative threads, not live model inference or
  packaged writer qualification.
- Native runner/guard integration: **111 passed, 1 POSIX-only skip** across the
  then-current worker, guard, execution and mailbox suites. Separate ordinary
  session/compression/restart regressions: **188 passed**. Two real native Session
  loops use controlled providers, exchange generated peer messages, and finish
  independently. Blocked providers remain visibly alive after Stop; a stopped
  response is not refunded automatically. Scope preflight no longer holds the
  control lock, so a slow filesystem inspection cannot block Stop admission.
- Desktop runtime/API: **18 passed**, 2.31 seconds, for default-off setup, explicit model and
  private credential capture, independent backends, setup retry identity,
  scope denial, disabled-feature controls, constructor failure and foreground
  chat/model gates. Later regressions cover startup ownership across saved sessions,
  orphan inspection with new execution blocked, closed-panel Stop releasing
  foreground ownership after cleanup, exact-result owner review and explicit
  completion. The same saved provider choice is preserved.
- Guarded transport retries: **114 passed** across the new seven no-retry
  fixtures and existing Ollama/Kimi/SONN/OpenRouter/EXO suites. Mock HTTP
  responses prove rejected, timed-out and interrupted guarded invocations do
  not silently launch another generation. Ordinary retry behavior is preserved.
  The seven focused fixtures were rerun after removing manual transport flags;
  the actual guard now activates single-request behavior for each native family.
- The Team panel's simulated-service headless Edge browser check passed, including
  keyboard focus, 390px layout, stale replies, captured conversation ownership,
  polling/reconnect, request bounds, active feature disable, recovery indicators,
  submitted findings and terminal New team controls. Ordinary UI recovery checks
  remain **22 passed**; bundle policy checks **2 passed**.
- A separate actual source-app headless Edge check passed in **5.10 seconds**.
  It uses the shipped template and WebSocket handlers, isolated temporary home,
  projects and SQLite state, and scripted native backends. Two real worker
  Sessions made four scripted requests and retained two completed file-read
  receipts and two submitted findings. Browser controls exercised pause/resume,
  socket reconnection to the server-owned run, blocked sidebar navigation, stop,
  draft preservation and the 390px panel. The screenshot was visually inspected.
  This exposed and fixed the Team button inheriting hidden legacy mission styles.
  Run `node tests/swarm_app.browser.cjs [playwright-module-path]` to reproduce;
  `tests/swarm_ui.browser.cjs` is the separate simulated-response check.
  Neither is packaged evidence, a live-model outcome, or complete P4 qualification.
- The source-app browser check was extended and passed in **7.88 seconds**:
  a second reader team requires explicit owner review notes for each stopped
  submission, binds each acceptance to its attempt epoch and candidate revision,
  preserves focused notes through polling, and completes only after both results
  are accepted. The full fixture now uses four scripted worker backends, eight
  requests and four file-read receipts across two teams (one stopped, one completed).
  Its two owner-review receipts are human decisions entered through browser controls,
  not executed verification checks or measured model quality. The compact owner-review
  screenshot was visually inspected; simulated responses exercise the same controls.
- The coordinator UI source-app browser check passed in **13.63 seconds**.
  It retains both manual-team scenarios and adds an approved three-investigation
  plan scheduled through two worker slots, separate owner reviews of all three
  findings, completion, and a rejected proposal that launches no workers. The
  complete fixture uses nine scripted backends and sixteen requests across four
  teams. Proposal decisions bind the displayed immutable digest and current run
  revision, require owner notes, and wait for coordinator cleanup. Keyboard approval,
  focused notes through polling, minimum allowance validation and the 390px plan
  layout were exercised; the compact screenshot was visually inspected. The
  simulated-service check passed in **11.39 seconds** and separately covers changed
  proposal identity, untrusted text, cleanup gating, scheduling attention and
  access to all outstanding reviews beyond the recent-history window. This is source behavior with
  scripted inference, not model-quality, live-provider or packaged qualification.
- The recovery controls passed an actual source-app browser check in **8.18 seconds**.
  A separate isolated fixture host records its real process identity, reserves an
  undispatched request and admits a file-read action, then deliberately exits
  without cleanup observations. No provider is invoked by that seed; its completed
  request record is injected test setup. The browser takes over the expired lease,
  checks the stopped original host, records termination, and explicitly reconciles
  the reservation and tool outcome from the fixture trace. Two selected retries
  and one previously ready investigation then make six scripted model requests,
  undergo three separate owner reviews, and complete. The 390px recovery screenshot
  was visually inspected. Simulated controls additionally cover unknown process
  observations, required evidence, no automatic retry selection, and Stop during
  owned recovery. This does not establish live-provider accounting or writer recovery.

At this earlier reader checkpoint, unfinished product paths included operator reconciliation/retry
controls, final packaged reference qualification, provider/outcome qualification,
enterprise governance and cross-session collaboration. The visible reader
preview calls submissions “awaiting verification” and does not claim completed
integrated work.

The later writer browser fixture passed in **27.39 seconds** against the actual
source app, WebSocket, SQLite and Git implementation. Two injected native workers
made four scripted requests and two observed file writes in separate worktrees.
The UI inspected a combined candidate, ran an actual named Python check, preserved
a newly dirty user checkout when application was refused, then applied an explicitly
reviewed clean candidate and separately accepted both tasks before completion.
It also downloaded and parsed the local metadata report: four used request units,
four cost-unknown observations, the passed check and two acceptances, with review
note content omitted. Compact setup/review screenshots were inspected. This uses
fixture threads/providers, not production model inference or packaged writers.

The corresponding simulated browser check passed in **17.67 seconds**, including
truncated/mismatched preview gates, repair controls and report download. The reader/
coordinator browser regression passed in **13.40 seconds**, the actual exited-host
recovery fixture in **7.70 seconds**, and the ordinary Node UI recovery suite kept
all **22** checks passing. Metadata export has **6** focused tests covering exact
scope, partial/unknown costs, invalid provider numbers, backwards/missing time and
exclusion of prompts, private diagnostics and authority. The active source guide is
[Team preview](swarming.md).

## Execution steps recorded at the earlier reader/writer checkpoint

These steps record the plan at that point in the session. Later sections and
the milestone ledger above supersede their completion status.

1. Finish the P3 integration process recovery and P4 controls, then rerun relevant
   source/browser regressions and the full ordinary-session suite.
2. Verify the final candidate's complete packaged reference scenario; preserve
   source, build and failure evidence separately from live inference results.
3. Complete the comparative benchmark runner and continue P5–P7 in dependency order.
   Prepare local candidates without publishing
   or replacing a running installed bundle. Live paid qualification has a separate
   authorization requirement in the product plan; finish independent work before
   asking for an exact provider/model/budget if none has been authorized.

Keep this ledger factual and update it after meaningful slices. No speedup,
learning benefit, supported swarm provider, released UI or enterprise enforcement
has yet been established.

## Individual controls and isolated packaged checkpoint

Individual pause/resume/cancel and owner guidance now bind to the exact participant
attempt and epoch, including coordinators. The runtime reports an admitted call,
pause checkpoint and confirmed cleanup separately. Generated guidance has its own
immutable records and exact primary-input receipts; it cannot impersonate peer
messages or grant additional tools. The metadata report omits guidance text.
The focused runtime suite passed **10** tests; broader native worker/process/guard
regression passed **124**, with one platform-specific skip. Durable controls and
guidance passed **106** combined tests, with **19** final focused checks.

Actual participant browser evidence passed in **9.04 seconds** at temporary run
`sonn-swarm-participants-browser-F377sZ`: one worker paused while another finished,
guidance entered the next actual Session input, and cancelling a third worker left
its peer active. The interrupted request correctly retained uncertain usage after
process cleanup. The reader/coordinator/concurrency regression passed **15.72s**,
writer/report **19.84s**, recovery **7.78s**, and ordinary UI recovery **22** tests.
The simulated test additionally caught a delayed dialog-close callback clearing a
newly reopened panel; the corrected ownership check has a keyboard-reopen regression.

An isolated clean Windows build succeeded at candidate version
`0.19.2.dev11+swarm20260926.1`. Packaged startup exposed a path-comparison bug:
discovery changed Windows separators for the same conversation and disabled Start.
Failure runs `sonn-swarm-packaged-PaNXDc` and `HLmEpJ` preserve the exact mismatch.
The source fix normalizes Windows path spelling for the UI gate, while the server
continues checking captured ownership. Simulated browser regression passed **20.61s**.

Candidate `.2` passed an **8.33s** real packaged reader run (`LKc902`): HTTP and
WebSocket UI, two independently observed child instances of the frozen executable,
four loopback scripted Ollama requests, two attributed file reads, two owner
acceptances, confirmed child cleanup, completion/report export, and served assets
matching packaged files. Its compact screenshot was inspected. This is native
adapter/process evidence, not live model quality or final packaged qualification.

A subsequent diagnostic run (`51eIaW`) exposed an identical-output artifact race:
Windows refused replacing a content-addressed blob while a peer read it. Artifact
publication now atomically creates the immutable name without replacement and
verifies an existing winner. A deterministic two-publisher/open-reader test fails
with the previous replacement behavior (`WinError 5`) and passes with the fix;
all **24** artifact tests pass. Candidate `.3` then passed the packaged test in
**7.99s** (`sonn-swarm-packaged-4vHPpi`), including startup/application logs,
accepted WebSocket connection and absence of request tracebacks. The same two
frozen readers completed with four scripted calls and observed cleanup.
The candidate source/patch hash receipts are separate from the main checkout,
whose version and installed application remain unchanged.

Metadata report and GUI service checks passed **40** tests. Source recovery UI
now exposes exact integration-effect/operation reconciliation without allowing
the caller to select a process outcome; its simulated controls passed **25.25s**.
Actual process-recovery integration and final packaged coverage remain in progress.

An additional transport audit found Ollama capability detection could issue a
hidden generation probe within a single supervised request. Supervised unknown
tool capability now fails closed after metadata inspection; ordinary session
probing is unchanged. All **8** guarded provider-request tests pass. The latest
desktop writer/report/GUI service combination passed **45** tests. These later
source changes still require the final candidate snapshot and full regression.

## Writer recovery, retained evidence and governance checkpoints

Candidate `0.19.2.dev11+swarm20260926.4` passed the frozen writer scenario in
**23.98s** (`sonn-swarm-packaged-VA56iV`). The unmodified executable served HTTP
and WebSocket controls, owned two actual frozen workers, and launched the real
Python verifier through its frozen `--swarm-effect` helper. Two file changes
stayed isolated until combined verification and explicit application. A dirty
checkout was preserved, then both applied results received separate acceptance.
The compact completion screenshot was inspected. The first attempt (`q5u7FX`)
correctly held a writer because both test assignments claimed the whole `src`
read scope; the fixture now declares independent exact-file scopes.

The same candidate passed an actual killed-host recovery scenario in **78.00s**
(`sonn-swarm-packaged-LjWCbl`). During a live named verification process the
fixture killed only its frozen GUI host; OS job ownership terminated the helper
and verifier. A fresh instance reopened the retained profile, waited for real
lease expiry, took ownership, reconciled the exact check and operation through
browser controls, and explicitly finished the stopped run. The check remains
cancelled with unknown exit status, no writer acceptance and no checkout change.
Four original scripted model requests remain the complete transport trace:
reopening/recovery invoked none. Earlier fixture runs (`Nupbgr`, `3t4rk5`) retain
the refused premature takeover and the required explicit finish-after-Stop step.
These fixtures establish process/recovery behavior, not model quality.

Source history and artifact inspection passed **48** combined GUI/service/helper
tests, including a blocked large-blob read while Stop completed independently.
Content pages verify all retained bytes and recheck complete scope before release;
Unicode offsets, corruption beyond the visible page and foreign-session cursors
are covered. Actual browser history evidence passed in **5.42s** (`DiERr3`): two
history pages, 27,314 retained Unicode characters reassembled and SHA-verified,
unsupported-image disclosure, preserved drafts and actual WebSocket scope denials.
These changes and the final partial-output retention fixes postdate candidate `.4`.

The full main test run collected 4,074 cases and finished with **4,067 passed,
3 skipped, 4 failed** in **800.11s**. Its failures are retained rather than called
a clean regression. A source-string process assertion was replaced by actual
spawn checks; a merge-acknowledgement injection now occurs after the real merge,
because the new pre-invocation gate correctly blocks earlier injected failures.
A failed isolated finalization with confirmed closed processes now asserts a
retained failed tree, unchanged HEAD, empty result revision and no submission.
The unrelated existing pause/decision test passed its focused rerun and all seven
tests in its module. Focused reruns passed the corrected cases; a final full run
against a stable desktop source snapshot remains required.

The separate `services/governance` foundation passed **61** tests across verified
resource tokens, real HTTP, PostgreSQL 18 transactions and launch configuration.
Tests use generated fixture signing keys, an isolated loopback cluster and a
restricted runtime database role. They cover tenant/project denial, current-grant
revocation before retry replay, concurrent revisions/idempotency, rollback when
audit fails, protected historical records and token signature/issuer/audience/
expiry/key-source validation. This is neither deployed SSO nor desktop governance
enforcement. The next service slice adds certificate-bound host enrollment and
shared request-unit reservations; no production service or credentials were deployed.

## Candidate `.5` and continuing service implementation

The separate clean Windows candidate `0.19.2.dev11+swarm20260926.5` includes
retained history, lease-expiry controls and the final partial-output fixes. Its
59 MiB / 262-file bundle passed frozen readers in **7.89s** (`Zw3Nbm`), the pinned
CSV reference/repair flow in **38.43s** (`sonn-swarm-packaged-csv-tu9AQc`), and actual
killed-host verification recovery in **78.91s** (`9QiHrT`). Both compact CSV and
recovery screenshots were inspected. The CSV case retained a deliberately failed
first check, explicitly retried one writer, independently checked quoting/form/
HTTP behavior, preserved a dirty checkout, then applied and separately accepted
both results. Recovery kept the interrupted check cancelled/unknown, with four
original scripted requests and no requests during restart. These are scripted
native-transport, process, Git and browser observations, not live model quality.

A full regression against the immutable candidate source copy finished with
**4,079 passed, 3 skipped, 7 failed and 10 setup errors** in **821.23s**. Every
failure/error was a benchmark `git rev-parse HEAD` failure: that disposable build
copy deliberately contains no `.git`. The benchmark correctly refused to invent
a source baseline. This is not recorded as a clean full suite. A new full run in
the real checkout, with the expanded benchmark runner stable, then passed:
**4,109 passed, 3 skipped**, **937.50s**, Python 3.13.5 on Windows. This complete
regression includes all 67 benchmark fixture/runner tests and predates subsequent
managed-client and P7 runtime changes, which need their own validation.

Governance now has real two-certificate host enrollment, 60-second online policy
leases and shared request-unit reservations. A clean isolated Python environment
with genuine stdlib TLS passed **88** combined identity/host/core tests before
later content/monitoring additions. Self-elevation into content/retention roles
now records a pending exact grant request and needs a different current authorized
approver; **10** focused real-PostgreSQL approval tests passed. It does not confer
content rights merely through organization administration.

The encrypted text gateway passed **27** real-PostgreSQL cases: AES-GCM integrity,
key rotation, scope/current-policy checks, revocation before replay, bounded
pages, audit failure rollback, holds/deletion and expiry during disclosure.
Recognizable credentials and invalid UTF-8 are rejected before persistence;
this screening is explicitly incomplete and binary/image uploads are unsupported.
The optional HTTP gateway and protected key-file configuration passed **18**
tests. Database tombstones do not erase backups or already downloaded copies.

Metadata/control storage passed **19** transaction tests and an actual human
HTTP/two-client mTLS scenario (with launch tests, **10** cases). Opaque run bindings,
strict numeric/enumerated projections, sequence gaps, quarantined revoked-host
reports, scoped lists and exact expiring pause/Stop requests are implemented.
Request, receipt and outcome remain distinct; repeated acknowledgements never
grant another dispatch permit. Outcomes remain attributed host reports, not
independently verified process termination. Independent review identified missing
`pausing`/`stopping` projections; both are now supported with pending-cleanup tests.
Desktop control delivery, protected archival and deployment qualification remain
open. No personal session has been enrolled, uploaded or remotely controlled.

## Managed execution and personal collaboration integration

The personal collaboration core now has schema-3 migration/backup tests, bilateral
exact grants, cumulative causal bounds, independent receiver allowances and
revocation/Stop fences. An actual two-session browser flow passed in **11.85s**
(`sonn-collaboration-browser-or6tKf`): empty team preparation made no provider
requests; the receiver explicitly approved and read a request, accepted work
against its own budget, ran a native file read, and completed independent review.
Origin Stop and revocation left already accepted receiver work running. A compact
390px layout and focused drafts were exercised. **183** related source tests
passed before the last desktop cases; all **6** desktop cases then passed.
This is personal, same-owner/project collaboration. It does not qualify managed
cross-user exchange or artifact-content transfer.

The separate service added protocol-2 shared worker slots and immutable bindings
from each model request to its worker, exact model, purpose and input hash. Tool
permits require the same worker's completed primary request; replay never grants
another dispatch. Schema 11 distinguishes a confirmed failed invocation from
success while consuming its request unit. **14** resource/migration tests passed.
The host launcher requires protocol 2; legacy accounting fixtures remain explicit.

The parent desktop adapter passed actual mTLS/PostgreSQL/native Session and owned
child tests for offline refusal, revoked owners, lost replies, exact remote Stop,
local Pause/Stop and conservative cleanup. The explicit service bridge also passed
**2** real managed desktop-flow tests, including personal/organization history
isolation and metadata-only reporting. Effective policy is displayed only after
an authenticated response. Restart dispatch remains closed while explicit remote
journal reconciliation is being completed; managed arbitrary Git/check execution
is not yet admitted by the service.

The operator CLI uses actual native OIDC sign-in with one explicit fixed-origin
API operation and no saved token. **10** CLI/client fixtures passed; **34** human
administration/identity HTTP tests passed. Membership changes, independent grant
approvals, provisioning mappings, scoped audit and host inspection now have
authenticated routes. Administrator status still grants no content access.

A combined governance run passed **248** cases with **7 setup errors** in
**160.59s**. All seven errors reused one fixture certificate across different
tenants, which enrollment correctly rejects. The fixture now generates separate
certificates per native test and its targeted suite passes. That combined run
is not a clean whole-suite result; it predates the latest administration and
desktop integration additions. A new aggregate run remains required.

## Managed browser, restart and owner effects

The protected startup configuration and real browser path passed **2** cases in
**23.33s**: explicit keyboard selection of organization ownership, zero provider
calls before Start, separate personal/organization history, native file reading,
metadata-only reporting and exact remote Stop. The second case shut down the
actual TLS listener, explicitly refreshed the observed revision and recorded
local Stop within three seconds while retaining the unconfirmed request unit.
The 390px layout and focused draft were exercised; private TLS configuration did
not enter WebSocket or model inputs. These remain scripted inference fixtures.

Explicit managed recovery passed **7** actual killed-host/mTLS/PostgreSQL cases
in **24.54s**. Retained request/action identities are checked against native hashes
and attributable local observations. Earlier shared worker slots require server
acknowledgement of cleanup; unresolved request units can remain visibly held.
Continuation creates fresh epoch identities and retains managed dispatch guards.
The corresponding desktop recovery controls are being integrated.

Schema 12 introduces explicit version-2 owner-effect permissions, separate from
model tools, for writer Git, candidate Git, named checks and checkout application.
The current enrolled owner also requires `control_execute`. An invocation binds
its run, host generation and immutable local semantics digest; authorization
replay never returns a second dispatch permit. Initial resource/migration tests
passed **24** cases. Restricted runtime database roles cannot rewrite resource
identity columns. Archival lag blocks new effects while allowing cleanup reports.

A new aggregate service run finished **300 passed, 1 failed** in **226.67s**. Its
only failure was the historical fixture that used policy version 2 as an unknown
version; it now uses 999, preserving unknown-version rejection while testing the
new v2 shape separately. The focused store/effect rerun passed **32** cases in
**9.11s**. Migration/archive/content checks passed **46** in **34.49s**, followed
by **2** focused archive cases in **4.24s**. Content audit records now retain the
opaque exact object UUID and revision for deletion/hold reconciliation against
an independent archive; older unbound records still require original receipts.
This does not claim backup erasure or an exercised production restore.

The source changes above are newer than candidate `.5` and the 4,109-test main
regression. A final integrated regression and new frozen candidate remain required.

## Managed writer, recovery and collaboration checkpoint

The managed writer completed the actual desktop HTTP/WebSocket/browser workflow
in **19.57s** (`sonn-managed-writer-browser-jMSmWG`). At 390px, keyboard controls
prepared an isolated writer, inspected the candidate, ran a named check, refused
application over the fixture's dirty checkout, applied the exact reviewed result,
and recorded separate owner acceptance before completion. All four central owner
effect kinds matched local immutable invocation digests and native process counts.
Host credentials remained outside the child and remote projections contained no
content. Related actual TLS/PostgreSQL/native cases passed **7** tests; focused
local writer/effect cases passed **22**. Inference remained scripted.

Recovery now includes explicit durable non-admission fences. Schema 14 records
an immutable server tombstone only when a resource is absent under the same locks
used by admission; any delayed reserve/start is then denied. Existing resources
return `present`, without inventing an outcome or refund. Native reconciliation
additionally requires original unclaimed/no-start evidence. All four resource
kinds, races, revoked/expired leases, foreign ownership and audit rollback passed
**21** real-database fence/migration checks in **11.31s**.

Actual killed-host recovery passed **12** cases in **38.85s**, native effect
recovery **4** in **5.80s**, and focused local journal/effect/GUI cases **86** with
one POSIX-only skip in **21.63s**. Both real recovery browser paths passed in
**19.15s** (`sonn-managed-recovery-browser-EOzB2o` and `...-BX4eMZ`): killed provider
host and missing uncommitted worker admission. The explicit fence control,
reconnect, keyboard use, compact layout and a fresh enforced epoch were exercised.
Historical permits never replay and unknown usage remains held.

Schema 13 adds encrypted managed sharing terms/content, default-off bilateral
project policy, metadata pagination and current membership/host/policy/epoch
checks. Two enrolled member certificates exercised the actual human HTTP and
mTLS routes. A receiving native worker performed a file read and submitted under
its own allowance while the origin stopped. Lost acceptance replies started no
backend. Durable main-store bindings prevent retry, scheduler or recovery from
giving accepted shared work a fresh unmetered allowance. Desktop controls are
being integrated; these source checks are not external IdP, live-model or
independently deployed host qualification.

The final managed sharing browser pass took **30.30s**
(`sonn-managed-sharing-browser-FsIZDu`): two separate GUI processes, enrolled
owners and certificates, one actual owned native child, explicit terms approval,
selected artifact-reference disclosure and independent receiver work acceptance.
Origin revocation and Stop left the accepted receiver process alive; a real file
read produced a submission, followed by separate owner review and completion.
The run exercised Enter-key approval, 390px layout, focused drafts during polling,
immediate hiding of a previously selected body when changing agreements and no
automatic peer-text inclusion in model input. Final central counts were one grant,
two messages, one acceptance, one worker slot and two model requests. Earlier
fixture failures and a rejected `bytes` versus canonical `byte_size` reference
payload remain retained; no ambiguous operation was automatically replayed.

The governance aggregate then passed **361 tests** in **346.41s**. Its preceding
run had **356 passes and one failure**: a rejected oversized TLS request could
lose its error response when Windows reset a connection with unread input.
The fix sends one response write and boundedly discards rejected input before
closing. A delayed-body TLS regression reproduces failure with that cleanup
disabled and passes with it enabled. The normal request limit is unchanged.
This aggregate predates the offline restore quarantine slice now being developed.

The main checkout regression finished **4,218 passed, 4 skipped, 2 failed** in
**904.35s**. The takeover fixture observed an owned launcher before executable
argv was released, so recovery correctly reported `not_started` instead of its
expected invoked-check cancellation. The test now waits for durable invocation.
The decision-parking fixture could spend its park deadline in the preceding real
shell check; its fixed `true` prelude now uses the documented test runner seam.
Control assertions and deadlines remain intact. Both affected suites plus real
process reconciliation passed **31 tests** in **73.34s**; a new full run is active.

Separate candidate `.6` (`0.19.2.dev11+swarm20260926.6`, 59.2 MiB, 265 files)
passed frozen readers (**6.87s**, `sonn-swarm-packaged-lc8JQl`), CSV failed-check
repair/application (**37.40s**, `sonn-swarm-packaged-csv-tJfmtM`) and two actual
managed browser cases (**17.15s**, `sonn-packaged-managed-MtXCm1` and `...-85fAuD`).
The latter used the unmodified executable, actual frozen workers, TLS and
PostgreSQL. With governance offline, Stop acknowledged in **68ms**, the child
stopped, the unresolved request remained held and no second request occurred.
Startup/HTTP/WebSocket and packaged assets were checked. Candidate `.7` is being
built to include the final sharing label/payload and strict revision fixes.

The [remaining live qualification plan](swarming-live-qualification.md) records
the exact proposed scenario/repetition/request envelope and missing deployment
inputs. Configured local provider inventory probes were unreachable; they invoked
no inference. Live provider budget/model and external host/identity details have
been requested while local work continues. These inputs are not inferred from
silence or from the user's general autonomous implementation request.

## Integrated regression and shutdown review

The next main checkout regression passed **4,222 tests**, with **4 skips**, in
**916.37s** on Windows/Python 3.13.5. This includes the corrected takeover and
decision-parking fixtures. Its collection preceded the final managed-sharing
shutdown review below; those changes have separate current-source checks.

Independent review reproduced a queued sharing send crossing a blocked lease
preflight after desktop Close. The adapter now marks each exact remote call under
a short native authority/state transaction after preflight. Pause, Stop and Close
can prevent admission; HTTP already admitted remains potentially in flight.
Primary acknowledgements are persisted before optional metadata refresh, and a
lost receipt after worker dispatch reports uncertainty instead of claiming no
worker started. Stopped agreements remain inspectable as metadata without new
terms/body disclosure. **9** actual TLS/PostgreSQL desktop-sharing cases passed
in **29.10s**. The preceding two-owner browser rerun passed in **30.37s**
(`sonn-managed-sharing-browser-klaXhz`); the stopped-metadata case is covered by
the later service test.

Offline retention recovery is now a separate operator workflow in
[RESTORE.md](../services/governance/RESTORE.md). It uses independently pinned,
complete archive evidence and an explicit one-way source seal. The original and
restored databases remain quarantined; no current authority or work is resumed.
Actual `pg_dump`/`pg_restore` fixtures passed **18** current-source tests in
**79.44s**. They cover deletion, unknown hold provenance, post-backup absent
objects, exact archive/prefix validation, transactional sequence advancement,
failed/interrupted operations and late-client rollback without termination.
Database CONNECT revocation blocks later nonoperator reconnection; stopped
services and network isolation remain prerequisites. Restore/archive test
configuration also pins the actual loopback address against libpq overrides.
The archive/configuration regression passed **20** cases in **25.89s**.

Candidate `.9` was built from a separate source snapshot with the final desktop
sharing fixes, using the clean Windows build and its running-target validation.
It is **59.2 MiB / 265 files**. All **214** captured desktop/package source files
matched the checkout at that checkpoint, and all **17** captured frontend assets matched the
frozen bundle. The executable SHA-256 is
`0100ef446ff0bc8b23d711fe1fb76153ed400c1382222490712206128fad9ce6`.
Candidates `.7` and `.8` remain earlier build checkpoints; neither substitutes
for qualification of later source changes. The separate governance package is
validated from the checkout, not shipped inside the desktop bundle.

## Follow-up planning and sharing retention

The final roadmap audit identified two unfinished mandatory slices: a fresh
coordinator turn over partial worker results and explicit retention of managed
sharing payloads. Both are being completed before another candidate is frozen.
The previous candidate `.9` remains a checkpoint and does not include these later
desktop changes.

The coordinator now accepts an explicit bounded follow-up request for an existing
team. It preserves the saved model and worker allowance, reads actual retained
findings and creates an additive proposal. A findings-only scope can run while
another writer remains active; worker scopes remain independently enforced.
Replay cannot start another coordinator and changed graph evidence invalidates
the proposal. **215** focused checks passed in **16.77s**. A real browser/native
fixture passed in **8.67s** (`sonn-followup-browser-tSnzqU`), with one worker held
inside its request while another finding reached the exact follow-up model input.
Explicit owner approval then started a third worker; separate result reviews
completed the work. Compact keyboard interaction and draft preservation were
checked. Subsequent review found that a pending planner response disabled Stop.
The panel now captures that run, obtains a fresh revision, and sends Stop once;
closing the panel cannot redirect it. Two actual-server browser cases passed in
**10.40s**, including disconnection before the fresh view and rejection of a late
planner reply. The source fixture's deliberately blocked thread remains visibly
stopping until its gate releases; frozen owned-child cleanup is checked separately.

The governance aggregate checkpoint recorded **393 passes and 2 failures** in
**450.55s**. One admin CLI success fixture shared an overly short login deadline;
a delayed-browser fixture now distinguishes the intended deadline failure from
successful login. The original generic sign-in failure did not preserve enough
diagnostic detail to establish its precise cause. The second failure exposed a
managed tool reaching admission before its originating completed model request
had received its queued server acknowledgement. Tool admission now sends that
exact immutable settlement first. A busy reporting queue and a lost settlement
acknowledgement have dedicated real service tests: no model call or tool is
replayed, and an unknown acknowledgement prevents tool admission. The local
journal/boundary/desktop/effect regression passed **59 tests**, with **1 skip**, in
**16.47s**. The next integrated governance checkpoint passed **426 tests**, with
no failures or skips, in **543.85s**. Its independently counted JUnit identities
and **126** unchanged captured source/test hashes are retained under
`sonn-governance-final-4657bdbe6b9c4587b604ba0022492052`.

Schema 15 provides bilateral `retention_admin` hold/release/delete for agreement
terms and message bodies, including human HTTP and operator CLI routes. Deletion
removes active encrypted payloads and their direct hashes, closes delivery and
acceptance replay, and preserves causal accounting and previously accepted work.
Immutable prior receipt fingerprints and existing plaintext copies/backups are
not erased. Quarantined restore now validates their exact creation/retention
chains. The combined content and sharing restore suites passed all **23 cases**
using real isolated PostgreSQL databases and actual `pg_dump`/`pg_restore`. They
include changed backup revision/state, post-backup absent objects, rollback after
staged sharing deletion, exact replay and permanent mutation/connection denial.
Sharing-only active payload deletion does not erase causal counts, immutable
prior receipts or accepted work.

A later restore review removed an unnecessary synthesized revocation timestamp:
retention deletion closes disclosure through `deleted_at` and must preserve the
agreement's actual historical `revoked_at`. All **5** actual sharing restore cases
passed again in **38.85s** with that additional assertion. This narrow change is
later than the 426-test aggregate and has its own evidence.

The latest full desktop regression passed **4,230 tests**, with **4 skips**, in
**921.47s** (20:00:19–20:15:43 UTC). The retained receipt is
`sonn-main-aggregate-661wa2go`. This aggregate used the Python modules imported
before the later application-recovery fix; it does not revalidate those later
edits. The pending-planner Stop UI has separate actual browser checks, and the
Git recovery change has its own focused suite and final candidate qualification.

An actual Git probe reproduced a false application claim: an interrupted apply
left the captured branch at its base, then a different branch at the candidate
commit was incorrectly recognized as applied. New writer/candidate manifests
capture the original checkout path, per-worktree Git directory, shared directory
and branch. Apply and recovery verify that destination before claiming an
observed result; a different branch, detached HEAD or linked worktree cannot
substitute merely because its HEAD matches. Known historical receipts remain
historical observations after later navigation. Missing destination provenance
stays unresolved. Candidate `.10` is a build-only checkpoint; `.11` includes
this correction and is undergoing executable qualification.

The checkout/branch fix passed **5** final-source actual Git cases in **60.24s**,
including known-receipt replay after later navigation. The related workflow,
desktop writer, Git boundary and candidate preview suite passed **31 tests** in
**166.77s**. Across the full integration suite, all **38 cases** have passing
observations: one broad run passed 36 before an expected-message mismatch in a
process that had imported an earlier wording, then that case and the remaining
case passed against final source in **10.77s** and **4.15s**. The actual denial
occurred in the earlier run; its assertion failure is retained rather than
reported as a clean single aggregate.

## Candidate 11 qualification

Candidate `0.19.2.dev11+swarm20260926.11` was built in a separate captured source
copy with the Windows clean-build script; running-target validation passed. It
contains **265 files / 59.2 MiB**. All **215** captured desktop/package source
files matched the checkout, and all **17** captured frontend assets matched the
bundle. Its executable SHA-256 is
`b5013f53c5b432cfe5bf8ec9c6a533e4fe686e0f1e809532d56ba4dea90d087f`.
The installed application remains unchanged. The separate governance service is
tested from source and is not embedded in this executable.

| Actual frozen scenario | Observed evidence |
| --- | --- |
| Two native reader processes | Passed **8.13s**, `sonn-swarm-packaged-BUGGJv` |
| Personal bilateral collaboration | Passed **16.75s**, `sonn-packaged-collaboration-aZ72jy`; explicit consent, receiver-owned assignment and independent review |
| Partial-result follow-up planning | Passed **16.68s**, `sonn-followup-browser-IaeiNm`; held peer, actual retained input, explicit additional plan, 8 scripted HTTP calls and 3 file observations |
| Delayed planning response and Stop | Passed **12.95s**, `sonn-frozen-followup-stop-mJXNQq`; real WebSocket delay, single captured Stop after panel close, actual worker cleanup while provider remains blocked, late reply ignored |
| Personal writer host-crash recovery | Passed **84.46s**, `sonn-swarm-packaged-4jN5XW`; actual killed host and retained verification recovery |
| CSV writer failure, repair and application | Passed **38.24s**, `sonn-swarm-packaged-csv-ylGviE`; failed check retained, explicit repair, dirty checkout refusal, exact verified application and separate acceptance |
| Managed writer/check/application | Passed **27.69s**, `sonn-managed-writer-browser-9AQLaV`; frozen worker/effect helpers, dirty edit preserved, exact checked application, separate acceptance, all four central effect kinds |
| Managed reader and offline Stop | Two cases passed **19.96s**, `sonn-packaged-managed-lDMSBq` and `...-0abLrK` |
| Managed bilateral sharing and deletion | Passed **40.80s**, `sonn-managed-sharing-browser-RvjtDL`; deleted message cannot be read, deleted terms remain metadata-only, accepted receiver work preserved |
| Managed killed-host recovery | Passed **71.40s**, `sonn-packaged-managed-recovery-lpS3Wb`; actual frozen host termination, observed child cleanup, unchanged protected state, real lease expiry, explicit reconciliation, fresh epoch/lease and independently accounted requests |

The delayed-Stop run retains its interrupted request uncertainty and remains
honestly `stopping` even after its owned processes are confirmed stopped. None
of these scripted inference checks establishes live model quality or added
swarming benefit. All listed candidate scenarios have now passed.

The first candidate `.11` CSV attempt (`sonn-swarm-packaged-csv-16B5I5`) observed
a definite stale-revision rejection of an owner decision, then the fixture
incorrectly waited for a retry form. Both work items remained submitted and the
failed check was retained. The harness now matches the exact command/response
identity and permits one explicit refresh/new owner decision only for that known
rejection; it never retries an unknown response. The successful run needed no
such intervention. The first managed recovery fixture (`...-9Plu8X`) timed out
waiting for a correctly disabled takeover control before its actual 60-second
lease expired. Its bounded wait now covers that real expiry; no clock, ledger or
application behavior was altered.

Final Ruff, five frontend syntax checks, 22 UI recovery checks and whitespace
checks passed. Candidate source/asset hashes were rechecked after the final
Git fix. All fixture apps and the owned PostgreSQL server were stopped; retained
evidence and protected fixture state remain outside the repository.

## Remaining completion inputs

The bounded final roadmap audit found no other concrete missing mandatory local
behavior. Full P0–P7 MVP is still **not complete**: P5 requires selected reachable
local and hosted models, the authorized live-request/spending envelope, repeated
baseline/comparison results and the declared pilot outcome. P6/P7 require the
designated independent hosts, external identity/provisioning setup, protected
configuration, operational ownership and archive/backup custody. The local
quarantined restore is retention reconciliation, not a production reopening or
current-authority recovery procedure. These exact pending inputs and the proposed
execution envelope are in [live qualification](swarming-live-qualification.md).
No recurring automation substitutes for continuous work in this conversation.
