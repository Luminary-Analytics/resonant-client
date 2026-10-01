# Swarming governance contracts

Design checkpoint: September 26, 2026. **P6 is not implemented or qualified by
this document.** This is a proposed implementation sequence for SW-025–029,
derived from [plan sections 10–11](swarming-plan.md#10-monitoring-and-enterprise-governance).
It preserves the [local contracts](swarming-contracts.md) and leaves AI Employee
work paused. No deployment, external account, paid request, or organization
enrollment is authorized by publishing this design.

October 1, 2026: the governance service built from this design moved out of
this repository into Lumi Cloud, at the same `services/governance/` path.
References to that path below describe the service there.

## The first implementation seam

Build an independently runnable organization module with a small authenticated
interface and its own transactional database. Its first deliverable is **tenant
authorization exercised through real HTTP requests and PostgreSQL**, before
adding a desktop synchronization adapter. Start with a denied-by-default
metadata query and a versioned project policy command. Every result, including
an idempotent replay, must reauthorize current membership and resource access.

The organization module owns memberships, permissions, host enrollment,
organization policy, global allowances, remote control delivery and protected
audit receipts. The local supervisor remains the only owner of its run's DAG,
assignments, execution epoch, submissions and acceptance. A central run registry
stores an authorized host binding and projections; it does not duplicate the
local work graph or turn uploaded model claims into accepted results.

Suggested interfaces, names provisional:

```python
Identity.authenticate(request) -> Principal
Governance.query(principal, Query) -> AuthorizedProjection
Governance.command(principal, CommandEnvelope) -> CommittedDecision
HostChannel.exchange(host_principal, HostBatch) -> HostReceipt
```

`Principal` is constructed only by the transport authentication adapter and
distinguishes human members, enrolled hosts and provisioning clients. A query
or command may name a target tenant/project; the module resolves and authorizes
that target before accessing data. A caller-provided `Scope`, role or owner is
never an authenticated principal. These interfaces include authorization,
transactional revision checks, idempotency and audit; callers must not assemble
those steps independently.

`CommandEnvelope` includes protocol version, command ID, expected resource
revision, operation and immutable arguments. Retry keys are scoped by tenant,
authenticated actor and operation namespace. Changed semantics are rejected.
Commands return a durable decision ID, revision and audit cursor. Authentication
and authorization failures precede replay lookup and do not disclose whether a
foreign resource or command exists. Background effects use a transactional
outbox and separate observations; a committed intent never means completion.

## Proposed self-hosted source envelope

Use a separate `services/governance/` package and entry point, with Starlette/ASGI
and PostgreSQL. Starlette is already used in the desktop's dependency set, but
this package must have its own pinned, tested dependency lock and import neither
the desktop GUI nor mutable `state.session`. Use explicit SQL transactions and
versioned migrations. Start with one application deployment and database; add
replicas only after race tests pass through independent connections. Do not add
a message broker merely to carry the durable outbox.

A fixture Compose profile can provide the organization module, PostgreSQL,
an OIDC provider, a TLS test proxy, protected test object storage, and two
independently configured host simulators. Keycloak is a suitable fixture OIDC
provider; its development container is a test dependency, not a production
identity deployment. [Keycloak container documentation](https://www.keycloak.org/server/containers).

Production configuration must separately supply HTTPS, database credentials,
host certificate authority, signing keys, content encryption keys, storage and
backup policy. Secrets enter the process through an operator-managed secret
store or protected file, not committed examples or desktop browser messages.
Development credentials and test issuers must be rejected by the production
configuration profile. No listener is exposed publicly as a side effect of
installing Lumi.

PostgreSQL transactions own global decisions. Tenant IDs occur in primary or
unique keys and in every cross-record foreign key. Use restricted database
roles and tenant row policies as defense in depth, with transaction-local
tenant context reset before a pooled connection is reused. Application
authorization remains mandatory: PostgreSQL table owners and `BYPASSRLS` roles
can bypass ordinary row policies. Exercise tests as the actual application
role, not the migration owner. [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

Content objects use opaque tenant-scoped identifiers and an authenticated
content gateway. Knowing a hash or storage key is never enough to fetch bytes.
Database backups, object backups and indexes need the same access and encryption
contract as live storage. Local fixture encryption settings cannot establish
that an operator's actual disks or backups are encrypted.

## Identity, membership and permissions

Human authentication uses a configured OIDC issuer and Authorization Code flow.
Validate signatures, issuer, audience, expiry, allowed algorithms and the login
transaction's state/nonce. Identify a person by the verified issuer and subject,
then resolve a server-owned member record; email, display name and OS username
are not identity keys or tenant authorization. ID tokens prove a login event;
resource requests require the correct resource audience and current membership.
[OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html).

The desktop uses the external system browser and PKCE as a public native client,
without an embedded client secret. Browser administration uses a separate client
registration and protected session cookies with CSRF defenses. Do not reuse
SONN provider credentials, Codex login state, or ChatGPT identity for organization
membership. Native redirect handling follows
[RFC 8252](https://www.rfc-editor.org/info/rfc8252/); token rotation, audience
restriction and grant choices must follow the current
[OAuth security guidance](https://www.rfc-editor.org/info/rfc9700/).

OIDC authentication does not provision permission. Implement SCIM 2.0 Users and
Groups under a dedicated, tenant-bound provisioning credential. Support
pagination, PATCH, deactivation, stable external IDs and conditional updates;
document unsupported operations rather than acknowledging ignored writes.
IdP group IDs map to explicit server roles at tenant/project scope. Removing a
member or group invalidates new leases and online authorization immediately
after the server commits the change. Existing offline authority remains limited
by its expiry. Use [SCIM protocol](https://www.rfc-editor.org/info/rfc7644/) and
[core schemas](https://www.rfc-editor.org/info/rfc7643/).

An issuer is explicitly associated with a tenant; arbitrary discovery URLs,
JWT key URLs and a token's claimed tenant must not select network endpoints or
authority. Issuer changes, key rotation, group mapping changes and provisioning
failures are recorded. No automatic email-domain enrollment or silent tenant
creation. An invitation, if supported later, cannot override an IdP suspension.

| Permission | Resource scope and intended use | Does not imply |
| --- | --- | --- |
| `run.metadata.read` | Authorized projects/runs; operational state and bounded aggregate accounting | Objectives, filenames, prompts, messages, source, check output |
| `run.content.read` | Explicit run content grant; conversation and handoff material | Source artifact access, control, export |
| `artifact.read` | Specific artifact/data class within a permitted project | Every referenced artifact, unrelated session content |
| `run.control` | Owner or delegated project operator; pause, stop, allowed steering/recovery | Expanding policy, approving application, accepting evidence |
| `run.approve` | Explicit action and exact resource/revision under policy | Arbitrary future actions or grants to the requester |
| `policy.admin` | Policy, role mappings, enrollment and limits within delegated scope | Reading private conversations or source |
| `audit.read` / `audit.export` | Authorized metadata audit query / separately approved export | Payload content or policy mutation |
| `retention.admin` | Scoped retention, holds and deletion procedures | Deleting required audit records or reading retained content |

Roles are named bundles of these permissions with resource filters. A run owner
receives permitted access to their own managed runs, not unrestricted tenant
administration. A project operator can stop a blocked run without seeing its
prompt. An organization administrator is not automatically a content reader.
Content role grants are explicit and audited; sensitive self-elevation requires
a separately authorized approver. Platform/database operators remain a distinct
infrastructure trust domain, not a product role pretending to resist root access.

Every list, count, search, subscription, replay, attachment, diagnostic, export
and administration operation applies this contract. Permission changes invalidate
server caches and subscriptions; caches include tenant, principal and permission
revision. Existing downloaded plaintext cannot be recalled. Do not promise that
revocation erases copies already legitimately disclosed.

## Host enrollment and run ownership

Enrollment has states `pending -> active -> suspended/revoked`. An authorized
member requests a one-use, short-lived enrollment challenge for a specified
tenant and project set. The host generates its key locally and proves possession;
the server binds an opaque host ID, public key, enrolling member, enrollment
generation and approved capabilities. Reusing the challenge or moving it to a
different key/tenant fails. A host label is display text, never identity.

Use mutually authenticated TLS for hosts, with short-lived certificates and
certificate-bound resource tokens where needed. The authenticated certificate
must match the token's key binding. A reverse proxy may convey certificate
identity only over a protected, authenticated hop; client-supplied certificate
headers are stripped. The protocol basis is
[OAuth mutual TLS and certificate-bound tokens](https://datatracker.ietf.org/doc/html/rfc8705).

Host credentials stay outside workers and model context, using the operating
system credential facility or an explicitly supported key store. Enrollment
does not establish hardware attestation or resistance to a local administrator.
Compromise/revocation disables new online allocations, commands and content
access, increments the host generation, and leaves unfinished effects uncertain.
Re-enrollment creates a new identity/generation; it cannot inherit the old host's
unsettled work by naming its PID or copying its SQLite file.

The server registers an opaque managed project ID and an explicit host-local
project binding. `gui/swarming.py` currently derives personal project IDs from
normalized paths and owners from the local username; neither value may be
promoted into global identity. `Scope.personal(...)` remains the offline personal
mode. The managed adapter constructs `Scope` only from the authenticated remote
binding and captured local session. Personal runs do not start reporting merely
because the user signs in to an organization.

A run binding includes tenant/project/member, host generation, server ownership
generation and local run/epoch. Server ownership and local execution epoch are
distinct. Moving a binding does not stop the old host or make its artifacts
safe. Handoff requires revoked old admission, retained reservations and actual
process/effect reconciliation before conflicting new writers can execute.

Remote controls become a durable host inbox entry bound to the exact run,
expected local revision/epoch, action digest, policy revision, expiry and actor.
The host resolves its retained local authority; it never deserializes an incoming
supervisor identity into `RunAuthority`. It records admission and completion
separately. Show central request, host acknowledgement, authority expiry and
confirmed process termination as different states. A disconnected host's Stop
request remains unconfirmed. An inbox retry cannot run an action twice.

## Leased policy, allowances and enforcement claims

| Mode | Enforced by | Honest disconnected behavior |
| --- | --- | --- |
| Personal local | Local trusted runtime | Existing local rules continue; no organization reporting |
| Managed local with reporting | Server membership, enrollment, remote resources and scoped leases; local runtime reports execution | New effects require a valid bounded offline allocation; expiry closes admission. Local-machine ownership can bypass local software. |
| Managed execution | Organization-owned executor, controlled credentials and network paths, plus server admission | Finish only already admitted bounded effects or terminate under policy; no new unrestricted request after lease/revocation denial. |

Implement the reporting mode explicitly before claiming managed execution.
Organization-owned credentials alone are insufficient if a client can reuse
them directly outside the enforcing gateway. Managed execution requires either
a controlled runner or a credential gateway which independently admits every
provider invocation, plus the tested isolation and egress policy for tools.
Server policy cannot stop arbitrary local CPU work on an offline user-owned PC.

An allocation binds tenant/project/member/run/host generation, policy digest,
server ownership generation, provider/model restrictions, request purposes,
tool/data scopes, units, issue time, expiry, minimum runner protocol and a unique
allocation ID. The server signs it; the host validates audience and key binding.
Use conservative short defaults, configurable within policy: a 60-second online
authority lease and an explicit offline allocation of at most five minutes for
initial qualification. These are proposed product defaults, not measured SLAs.

Global quota reservation, allocation receipt and audit entry commit atomically.
Two hosts cannot allocate the same remaining units. No quota is automatically
refunded on host expiry, disconnect or Stop. Settlement is idempotent and keeps
known use, unused units and unknown execution separate. Primary and auxiliary
requests remain distinguishable; tools are not request units, and request units
are not dollars. Provider-reported or estimated cost is labeled separately.

The controlled-resource adapter admits each request with an immutable request
ID before invocation, records the attempt/epoch and allocation, and refuses
duplicate invocation. All retries and auxiliary calls cross this interface.
Unknown transport completion retains the reservation. Local reported settlement
is attributed as a host report; only independently observed controlled-resource
usage supports centrally enforced accounting. A hard dollar cap is not claimed
without a separately qualified price/upper-bound and settlement contract.

Policy changes deny new leases immediately and cannot silently broaden active
grants. Lease expiry limits new effects; it does not terminate an admitted
process by definition. Clients use server time with a conservative monotonic
deadline, never extend a lease by moving the wall clock backward, and require
renewal after restarting without a trustworthy deadline. An offline policy cache
is not a renewable source of authority. Unknown policy versions fail closed.

## Metadata, content and protected audit

Metadata synchronization uses a strict versioned allowlist: opaque causal IDs,
states, policy versions, bounded numeric accounting, host contact/lease times,
process-stop observations, check status and evidence references. Freeform
objectives, project labels/paths, owner guidance, error strings, tool arguments,
check stdout, prompts and transcripts are content. The current local report is
a useful projection input, not already an organization-safe schema or protected
audit. Never serialize `store.snapshot()` directly to the organization endpoint.

Content uploads require an explicit project data policy and artifact grant.
References do not grant disclosure. Authorize metadata and byte/range requests
separately, record content access, and avoid long-lived direct storage URLs that
bypass revocation checks. Secret filtering precedes persistence and export; its
limitations do not justify treating arbitrary freeform text as safe metadata.
Show the managed-workspace and collection policy before execution. No unrelated
personal sessions, keystrokes, covert screenshots or hidden model reasoning are
part of this monitoring contract.

Audit ingestion authenticates the host and binds each bounded batch to its
tenant, enrollment generation, run binding, event sequence and payload digest.
Same sequence/different digest is a conflict. The server supplies receipt time,
ingestion ID and integrity status separately from host-reported time and event
claims. Detect gaps and stale owners; quarantine inconsistent late evidence.
Host authentication proves the source of a report, not that its claimed test
passed or its local ledger was never edited. A revoked host cannot regain
execution authority by submitting historical telemetry.

Server authorization, policy, approvals, enrollment, access, export and deletion
decisions append audit records in the same transaction as the decision. An
outbox copies them into retention-protected storage under credentials which
application operators cannot use to rewrite history. Track delayed archival and
fail closed on critical operations when required audit persistence fails.
Hashes/checkpoints expose changes only when their anchors and keys are protected
independently; a local JSON file or database hash chain alone is not tamper proof.

Retention policy covers primary stores, object versions, indexes, local managed
caches, exports and backups. Deletion is a durable procedure with a scoped
request, applicable hold/authority, tombstone, per-store acknowledgements and
documented backup expiry. Restore replays tombstones before serving content.
Minimum audit tombstones retain opaque IDs and deletion provenance, not the
deleted payload. Report unreachable host caches as unconfirmed; never claim
global erasure from a central delete. Holds require recorded scope, actor and
expiry/review. Organization-specific legal/retention decisions remain deployment
prerequisites; this design makes no jurisdictional compliance claim.

## Proposed implementation slices

The roadmap currently assigns the range SW-025–029; the following is a proposed
mapping within that range, not a claim that these individual tickets already
have detailed accepted implementations.

| Slice | Reviewable source deliverable | Required evidence |
| --- | --- | --- |
| SW-025: identity and tenant authorization | Separate ASGI package, PostgreSQL schema, OIDC adapter, membership/role projection, initial SCIM Users/Groups, command/replay authorization and transactional audit append | Real fixture OIDC login; wrong issuer/audience, revoked member, cross-tenant read/list/count/export/replay denial; actual database role and pool reuse tests |
| SW-026: enrollment and policy | Host challenge/key binding, project mappings, generation revocation, signed expiring policy, minimum protocol negotiation | Two independent host keys; copied/replayed enrollment denied; key rotation, revoked host, policy downgrade and reconnect tests |
| SW-027: quota and managed execution seam | Atomic global allocations and settlement; desktop managed adapter; controlled-resource request admission; explicit enforcement-mode reporting | Parallel host allocation races; interrupted request remains reserved; expiry/clock rollback; direct credential/egress bypass tests before managed-execution claims |
| SW-028: monitoring and controls | Metadata projection, authorized subscriptions, durable control/approval inbox, host receipts and grouped actionable alerts | Metadata-only operator sees no content; exact owner approval; lost replies; disconnected Stop remains unconfirmed; permission changes invalidate subscriptions |
| SW-029: protected audit and operations | Protected archival, content gateway, retention/holds/deletion/restore, deployment and upgrade runbooks, two-host qualification harness | Tamper/access/retention/backup tests using deployment credentials; restart and mixed-version tests; separate-machine qualification evidence |

Audit authorization is required from SW-025 onward. SW-029 adds archival and
operational qualification rather than permitting unaudited earlier endpoints.
No module is declared complete by an in-memory fake standing in for its
authoritative storage or authentication adapter.

## Upgrade and two-host acceptance matrix

Negotiate organization protocol, event schema, runner/tool protocol and policy
version explicitly. Preserve a run's policy/grants and already-admitted command
semantics through upgrades. Drain incompatible runners; do not hot-rewrite an
attempt. Database changes use reviewed migrations with backup/restore tests and
compatibility during rolling deployment. An unsupported client may inspect
authorized history, but cannot acquire new authority. Unknown events are retained
as bounded opaque evidence without being interpreted as success or a command.

| Scenario | Required observation |
| --- | --- |
| Two tenants, two members with identical names, two hosts | No identity collision; foreign IDs deny across all transport and storage paths |
| Tenant administrator without content grant | Can administer permitted policy; content/artifact/search/export payload access remains denied |
| Host B replays host A's token, allocation, event or command receipt | Key/run binding rejects before any content or new effect |
| Two host processes race the last allocation | Exactly one durable allocation; uncertain starts are never double refunded |
| Group removal, member disable and host revocation during active execution | Online effects deny after committed revocation; admitted effects remain observed; offline exposure is bounded and reported honestly |
| Network partition, clock rollback, application restart | No silent lease renewal or replay; expired host pauses new effects and retains unknown reservations |
| Remote Stop during model/tool/Git/check execution | Request/acknowledgement/termination distinct; independent OS/process observations establish cleanup |
| Metadata subscriber attempts an artifact or transcript fetch | Separate denial and logged access decision; no raw content in alerts/errors |
| Duplicate, reordered, missing and conflicting event batches | Deterministic dedupe/conflict; gap visible; replay cannot change execution state |
| Audit writer attempts update/delete; content deletion then backup restore | Protected history rejects unauthorized mutation; deleted content does not reappear; unavailable caches remain unconfirmed |
| Old/new server and host versions overlap | Compatible work drains under original semantics; unsupported new admission denied |

Two processes or containers on one developer machine prove protocol/storage
behavior only. Final two-host evidence must use independently provisioned
machines or VMs with separate keys/state, real TLS, network partitions, restart,
actual IdP/provisioning configuration and the deployment's protected storage.
For writer qualification, use an execution platform with proven process
containment; the current arbitrary-argv integration gate requires Windows named
jobs. A Linux fixture does not qualify an unsupported writer executor.

## P7 compatibility without premature collaboration

Reserve an explicit collaboration-grant identifier and causal origin fields in
the remote envelope. Project membership or enrollment does not grant discovery
of other sessions. P7 requires independent run ownership and bilateral grants
bound to source/destination, purpose, message/data classes, expiry, revocation,
limits and paying owner. The receiving supervisor accepts work against its own
policy and budget; a sending run cannot command its workers or inherit credentials.

Cross-run delivery needs durable dedupe, hop/fanout bounds, dependency-cycle
tests and an explicit contract for already accepted work after revocation or
source Stop. Same-owner/project exchange comes first; cross-tenant federation
is outside this design. P6 authorization and audit must be reusable at this seam
without making every tenant session globally visible.

## Prerequisites for final hosted qualification

Source development can implement and test these interfaces with local fixtures
without external accounts. Final P6 qualification additionally needs:

- A named deployment owner and selected self-hosted environment, HTTPS domain,
  certificate/key custody, encrypted storage/backups and restore access.
- A real organization IdP client registration and SCIM provisioning integration,
  two member accounts/groups and an administrator able to exercise deactivation.
- Two independent enrolled hosts with supported containment, network-failure
  controls and approved project bindings; host/operator trust assumptions recorded.
- An explicit choice of reporting versus managed execution, with controlled
  provider credentials, egress and executor isolation for the latter.
- Approved content classes, collection notice, retention/hold/deletion rules,
  audit operators and a protected archival destination.
- A declared provider/model and authorized budget only if real provider traffic
  is needed for qualification; local mocks cannot establish billing enforcement.
- Operational owners for revocation, incident response, upgrade rollback and
  recovery of unresolved effects, plus retained outcomes from the matrix above.

These are concrete environment and operator inputs, not a request to stop source
development. A finished fixture suite may justify a source milestone; it must
not be reported as deployed enterprise enforcement or full P0–P7 MVP completion.
