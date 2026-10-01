# Managed collaboration contract

**Status:** Source server and native admission seam, qualified with disposable
PostgreSQL, real signed human HTTP and two mutual-TLS host identities. Providers
are scripted. This is not a deployed organization service, a live-model result,
or packaged P7 completion. The [progress ledger](swarming-progress.md) tracks
the remaining desktop and release qualification.

This seam is separate from [personal same-owner collaboration](swarming-collaboration.md).
It never enrolls a personal session, discovers other conversations, transfers
credentials, or lets one owner control another owner's worker. Operators must
configure the managed service and explicit content keys before enabling it.

## Desktop workflow

In a saved conversation, open **Team**, enable the preview and explicitly select
**Organization managed team**. Under **Share with another managed conversation**,
prepare an idle team with your own objective and request allowance. Preparation
starts no model. Its opaque address appears after authenticated registration;
copy that address to the other owner yourself. Existing personal teams retain
their ownership and cannot become managed teams through this control.

The sender pastes the recipient address and offers a bounded agreement. The
recipient refreshes the metadata list, inspects the exact terms, then approves
their fingerprint. Selecting another agreement hides the previously selected
content. Polling does not fetch remote plaintext. Each **Read selected managed
message** is a separate authorized disclosure. Fields and focus survive ordinary
local status polling.

Send only the text you select. Artifact offers accept a strict JSON reference:

```json
{"artifact_id":"the exact known artifact UUID","sha256":"its 64-character lowercase SHA-256","byte_size":123}
```

Use a known reference; the UI does not invent an artifact identity. Delivery
shows those three values and conveys no blob access. For a work proposal, the
recipient must read it and choose a separate objective, readable folders,
request allowance and acceptance notes. The received text is not automatically
inserted into model input. A retained acceptance marker prevents another
dispatch of the same proposal; admission is not completion. Submitted findings
still need the recipient's own review before **Complete team** becomes available.

Remote sharing operations run in the background. Local **Stop team** remains
available. Revocation or sender Stop closes future disclosure and acceptance;
already accepted receiver work remains under the receiver's controls. After
restart, retained action outcomes and retry fences remain visible, but prior
sharing dispatches are never replayed automatically.

The local owner, epoch and run state are checked again after network preflight
and before each disclosure. A durable hash-only marker records a request admitted
to HTTP; Stop cannot retract a request already in flight. A server acknowledgement
remains recorded even if Stop, close or a connection failure prevents a later
metadata refresh. Missing acknowledgements retain uncertainty and do not permit
automatic replay. Metadata-only inspection can still inspect a stopped team's
history; it cannot use that exception to fetch new agreement terms or content.

## Administration and agreement

The signed human endpoint
`/v1/tenants/{tenant_id}/projects/{project_id}/sharing/policy` supports GET and
revisioned POST for a current `policy_admin`. No policy means sharing is disabled.
POST carries exactly `command_id`, `expected_revision`, and `policy`. The separate
version-1 policy defines enabled status, message kinds, content classes, peer
project IDs, message/byte/request ceilings, hop/fanout ceilings and maximum TTL.
Cross-project sharing requires both projects to name each other explicitly.

Both owners need current `host_admin`, `content_read` and `content_write` grants
in their respective project. Their ordinary project policies must allow explicit
content. Current enrolled host generation, protocol-2 lease, membership, policy,
run epoch, running-state observation and recent contact are checked at both ends.
A pending or applied remote Stop closes future disclosure. Host reports remain
attributed observations, not independent proof of a remote executor's behavior.

An origin offers terms to one explicitly supplied receiver run binding. The terms
include selected purpose, kinds/classes, limits and expiry; they fix the payer as
the receiver and allow either owner to revoke. The receiver reads the terms and
approves their exact SHA-256. Both ordinary and sharing-policy revisions are
captured. Later revision changes require a fresh agreement.

## Selected content and receipts

The host API exposes fixed `/v1/sharing/offer`, `approve`, `revoke`, `send`,
`deliver`, `accept`, `inspect`, `terms` and `messages` routes. Socket certificates
supply identity. Payloads cannot select a tenant, owner or certificate identity.

General inspection returns bounded IDs, directions and states. Message pages
return up to 20 IDs, kinds, classes and delivery states, with an explicit cursor.
They do not read plaintext. Purpose and selected message content use authenticated
encryption under operator-supplied keys. The addressed receiver explicitly reads
terms or delivers a message. Complete content authentication and hashes are
checked before returning any plaintext. Required audit archival gates apply to
disclosure, including a repeated delivery command; safe metadata and revocation
remain available when archival is blocked.

Selected text is limited to 8 KiB per message. An artifact reference permits only
an artifact UUID, SHA-256 and byte size; it conveys no filename, filesystem path,
blob access or verified ownership of a foreign artifact. Secret screening is an
additional check, not automatic permission to share a transcript or source tree.
No automatic conversation, tool-result, or model-context sharing occurs.

Queued, delivered, work-reserved, submitted and accepted results are distinct
states. A delivery receipt records disclosure to the receiving host. It does not
claim model input inclusion, comprehension, execution or verification. Revocation
does not erase bytes already disclosed. Expiry prevents future reads; ciphertext
and audit history remain subject to operator database/backup retention.

An explicit human retention administrator can inspect, hold, release a hold, or
delete one agreement's encrypted terms or one message body. The administrator
needs current `retention_admin` on both captured projects; these rights grant no
content read access. Holds block deletion. Every change uses an exact resource
revision and command ID and appends an immutable audit receipt.

Deletion removes the active ciphertext, key identifier, nonce and direct payload
hash. It blocks future terms disclosure, message delivery, acceptance and relay,
including old command retries. Deleting terms closes that agreement's future
sharing. Previously accepted receiver work keeps its captured allowance. IDs,
byte counts, limits, causal links and accepted-work records remain for accounting;
prior immutable receipt fingerprints, already disclosed copies and backups are
not erased. Expiry does not launch a background purge.

## Receiver execution and uncertainty

The receiver explicitly plans its own task, chosen native model, tools/scopes,
criteria and request allowance. Normal local supervisor admission reserves that
allowance. A durable main-store receipt binds the exact work item, attempt, epoch,
message and contract hash before any network operation or backend construction.
The receiver then reserves its own managed worker slot and obtains one remote
work-acceptance permit. The server requires that slot to belong to the receiver
run, and counts every subsequent bound main or auxiliary request against the
accepted allowance and the receiver project's normal quota.

Only the first acknowledged permit authorizes dispatch. A lost response leaves a
pending local record and no model call. Reopening restores no permit. Native
retry, scheduler assignment and recovery retry selections cannot reuse the bound
work item, including after a new epoch or a new private journal. Further shared
work requires a new explicit peer request and a new owner work item. No automatic
refund or blind replay is inferred from process exit or a sender's report.

The receiver owns its process, Stop, request accounting, artifact review and final
acceptance. The origin's Stop or grant revocation closes new delivery/acceptance
without cancelling already accepted receiver work. Successful workers submit
findings; they still require the receiver's ordinary checks or owner review.

New managed journals use an opaque stable session identity for cycle detection.
Historical random session identities remain immutable and cannot opt into new
sharing. Forwarding requires an actually delivered parent and a new approved
grant. All ancestor grants remain current at commit, and cumulative disclosure,
request and recipient ceilings include retained history after revocation/expiry.
Session identity, rather than run-binding UUID alone, detects causal cycles.
Independent accepted work also rejects dependency cycles; the initial graph
validator conservatively caps its retained tenant edge set at 256.

## Source evidence

Tests exercise independent PostgreSQL connections, policy/offer/ingestion races,
same-project and bilateral cross-project policies, current-grant revocation,
expiry during publication, encrypted-content tampering, cumulative forwarding,
session rebinding cycles, metadata pagination and required-archive rollback.
The transport fixture uses actual signed human HTTP, two independently enrolled
members and certificates, and two independent native local stores/runtimes. A
guarded receiver performs a real file read while the origin stops; the receiver
continues under its own allowance and remains submitted pending owner review.
A separate lost-acknowledgement lane records uncertainty and starts no backend.

The source browser fixture runs two independent GUI processes with separate
member certificates against actual mutual TLS and PostgreSQL. It exercises
390-pixel layouts, explicit preparation, inspected approval, selected text and
artifact references, receiver-funded owned-child execution, sender revocation
and Stop during receiver work, then separate receiver review and completion.
The native provider is scripted; the file read, child ownership, server decisions
and browser controls are real. This does not qualify an installed or deployed
organization service.

The September 26 candidate 9 checkpoint also passed this flow in two unmodified
Windows executables (34.66 seconds), with separate certificates and actual TLS /
PostgreSQL. The receiver used two scripted model requests and one real file read
in the frozen owned child; the sender used no model requests. Startup, WebSocket,
packaged asset bytes, selected-content isolation, sender Stop/revoke, and separate
receiver review were checked. This is packaged protocol evidence with scripted
inference, not live-model quality or production deployment evidence.

Candidate `0.19.2.dev11+swarm20260926.11` repeated the frozen bilateral flow with
the retention controls (40.80 seconds). An external fixture custodian explicitly
deleted a selected message and agreement terms through the actual service. The
packaged UI cleared selected plaintext, disabled new reads/acceptance for the
deleted message, and retained safe agreement metadata. Already accepted receiver
work still completed under its original request allowance and separate review.

```sh
python -m pytest tests/test_swarm_managed_collaboration.py
# In a Lumi Cloud checkout, where the governance service now lives:
python -m pytest services/governance/tests/test_managed_collaboration.py services/governance/tests/test_managed_collaboration_native.py
node tests/swarm_managed_collaboration.browser.cjs /path/to/playwright
```

Set `SWARM_PACKAGED_EXECUTABLE` to an explicit candidate executable to run the
same browser scenario against two unmodified frozen apps. The fixture also
requires `SWARM_MANAGED_PYTHON` and `SONN_GOVERNANCE_TEST_CONFIG`; all provider
responses and fixture controls remain in separate loopback infrastructure.

The service tests require the existing protected disposable PostgreSQL fixture
configuration and a clean TLS environment. They use no paid provider, personal
session, arbitrary external host enrollment, or production deployment.
