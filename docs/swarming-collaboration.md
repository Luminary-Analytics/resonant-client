# Explicit local collaboration contract

**Status:** Initial P7 source core and desktop preview for personal runs in the
same project and SQLite store. This is not hosted organization authorization,
cross-user qualification or a packaged-release completion claim. See the
[swarming plan](swarming-plan.md), [local user guide](swarming.md) and
[progress ledger](swarming-progress.md) for the broader delivery status.

`SwarmCollaboration` binds every operation to the caller's captured
`RunAuthority`. It has no global session discovery, provider credentials,
worker-command API or conversation export. Both participants must be distinct
conversations with the same owner/project and the `personal:<owner>` tenant.
Organization scopes require a separate managed collaboration policy and are
rejected by this initial core.

The separate [managed collaboration contract](swarming-managed-collaboration.md)
now documents the explicitly authorized cross-member source server/native seam.
It does not broaden this personal module's ownership rules.

## Explicit consent and disclosure

The origin offers immutable `CollaborationTerms` to one already-known run. The
receiver accepts the exact terms SHA-256. Terms include purpose, message kinds,
data classes, message/byte/request ceilings, hop/fanout limits, expiry and the
participant runs allowed to revoke. They fix the payer as the receiving run and
the cancellation contract as closing future delivery and work admission. Dollar
limits are explicitly unavailable; a numeric dollar limit or transferred payer
is rejected, rather than presented as enforced.

Message kinds are questions, findings, artifact offers, proposed work requests
and work results. Content classes are selected summary text, code text and
artifact-reference metadata. They are disclosure permissions, not automatic
classification or secret detection. Sending text is an explicit trusted-host
action; model output cannot call this API directly or obtain supervisor authority.

Sending queues a message. The addressed receiver explicitly records host delivery
before receiving its body. Pending inspection exposes metadata only. This receipt
does not claim model input inclusion, comprehension, work acceptance or artifact
quality. Already delivered content remains retained history after revocation;
revocation cannot erase knowledge already disclosed.

Artifact offers contain only explicitly selected, sender-owned artifact IDs,
content hashes and sizes. No label, filename, filesystem path, blob contents or
worker artifact-read grant is transferred. Another run cannot reshare a foreign
reference as its own. Cross-run blob access requires a later explicit adapter.

## Receiving work and paying for it

A delivered work request is untrusted proposal data. The receiver supplies its
own fresh work contract, chosen native model, worker identity, request allowance
and acceptance evidence. One SQLite transaction reuses the supervisor's normal
plan and assignment validation, checks the receiver's policy/slots/scopes/budget,
reserves its requests and binds the proposal to the new attempt. Failure rolls
back all of those changes. The result is a pending dispatch claim, not an
executed worker or accepted result.

The receiver's captured runner may dispatch that claim through its normal
`start(AttemptContext, BackendSpec)` interface. Provider requests, file tools,
accounting, Stop, final submission and verification retain their normal local
ownership. Neither the origin's credentials nor its request budget is borrowed.
Final results still require the receiver's ordinary verification or explicit
owner review. A peer finding is not an acceptance receipt.

The grant authorizes one attempt per accepted request. Native retry and assignment
reject a second allocation for that bound work item. Further collaborative work
requires a fresh explicit request; an owner may separately create an independent
task under their own policy. No automatic retry or allowance refund is inferred.

## Forwarding, cancellation and durability

Forwarding requires a parent message actually delivered to that sender. Origin
IDs, parent links and hop counts are derived from retained records. Reaching the
same run with the same causal kind is deduplicated. Hop, fanout and disclosure
permissions cannot exceed any ancestor grant; cumulative same-origin message,
byte and accepted-request quantities use the smallest inherited ceilings.
Accepted requests form cross-run dependency edges. A new edge that would create
a cycle among unresolved receiver work is rejected atomically.

Both participants' current epochs, leases, policies and admission states are
checked before new delivery or acceptance, including a final check before commit.
Ancestor grants are also checked when forwarding or receiving forwarded work.
Origin Stop, policy/epoch changes, expiry and explicit revocation close future
delivery and work acceptance. They do not cancel an already accepted peer attempt:
that attempt continues to belong to the receiving run. Its owner can independently
pause, stop or reconcile it through the ordinary runtime. A replay cannot create
a second delivery, assignment or reservation, or bypass current authority.

Schema 3 adds explicit grants, messages and acceptance bindings while retaining
the existing work graph and request ledger. Opening a schema-2 store for writing
first saves a coherent `.v2-<id>.backup`, then migrates transactionally. A failed
migration leaves schema 2 and its history intact. Read-only opening does not
migrate, and newer unsupported schemas remain rejected. Migration never resumes
workers or resolves uncertain requests.

## Evidence and remaining scope

Tests use real SQLite connections, including concurrent independent connections,
and an actual two-run native Session fixture with scripted provider backends and
real file tools. The fixture stops the origin while the receiver continues and
submits under its own model and allowance. Scripted thread backends establish
source behavior; they are not paid-provider, managed-host, packaged or comparative
performance evidence.

The Team panel has explicit preparation, invitation, agreement approval,
delivery, read-only work acceptance, revocation and bounded history controls.
Preparing an idle collaboration team makes no model request. Up to eight such
owned teams can coexist; saved navigation is allowed only while no worker or
uncertain request/action owns work. Normal chat and provider changes retain
their existing work gates. Each panel keeps its captured conversation and run,
including Stop after another page changes the selected conversation.

Desktop work acceptance fixes file-only read tools and owner review, uses the
receiver's captured native model, and durably records a dispatch intent before
launch. A failed or lost launch acknowledgement does not trigger a second launch.
The retained attempt/reservation requires explicit reconciliation. UI invitations
permit direct delivery only, name both participants as revokers, and require an
exact copied conversation/team address; there is no global session discovery.
Agreement and message history pages contain at most 20 records. Pending bodies
remain absent from projections, and retained message/terms hashes are checked.

The real source-app browser fixture prepares two saved conversations, uses two
captured panels, and exercises keyboard approval/delivery/work acceptance,
390-pixel layout, focused drafts, receiver navigation gating, origin revocation
and Stop isolation, real file reading, and separate result review/completion.
Provider responses are scripted; this is source HTTP/WebSocket/native Session
evidence, not live-provider or packaged P7 qualification. One expected stale
revision rejection in the fixture was explicitly refreshed and resubmitted,
not replayed as an ambiguous committed call.

Windows candidate `0.19.2.dev11+swarm20260926.6` separately passed an unmodified
frozen-app browser scenario with two saved conversations and an actual frozen
reader child. Preparation, agreement approval and explicit message reading made
no model calls. The receiver chose a three-request assignment under its own
six-request total; it used two requests and one attributed file read while the
origin used none. Origin revocation and Stop left accepted receiver work running,
and the receiver completed only after separate owner review. The 390-pixel consent
and result views, literal untrusted message text, startup logs and WebSocket
behavior were checked. A rejected stale revoke was explicitly refreshed and
resubmitted. The provider was a loopback scripted Ollama HTTP service; this proves
the exercised packaged path, without live inference or an installed release.

Managed policy authorization and cross-user/host transport live in the separate
[managed seam](swarming-managed-collaboration.md). Cross-run artifact content
access and model context-delivery receipts remain separate integration work.
An authority epoch change closes the old agreements; recovery does not
silently recreate a grant or retry accepted collaborative work.

```sh
python -m pytest -q tests/test_swarm_collaboration.py tests/test_swarm_store.py tests/test_swarm_supervisor.py tests/test_swarm_workers.py tests/test_swarm_scheduler.py
python -m pytest -q tests/test_swarm_collaboration_desktop.py tests/test_gui_swarming.py
node tests/swarm_collaboration.browser.cjs /path/to/playwright
node tests/swarm_packaged_collaboration.browser.cjs /path/to/candidate.exe /path/to/playwright
```
