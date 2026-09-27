# SONN governance service source

This package is separate from the desktop. The initial source foundation stores
tenant membership, explicit permissions, versioned project policy, certificate
enrollment and online request accounting in PostgreSQL. An optional native host
adapter binds conforming worker/model/tool dispatch to those decisions. Deployment,
organization onboarding and full P6 completion remain separate qualification. See
[the governance contracts](../../docs/swarming-governance-contracts.md).

Use Python 3.11+ and PostgreSQL 18. Install this package in its own environment:

```powershell
python -m pip install -r services/governance/requirements.lock
python -m pip install --no-deps -e services/governance
```

An operator creates separate database owner and application logins. The
application login must have no superuser, BYPASSRLS, schema ownership or inherited
owner authority. `sonn_governance.store.migrate(owner_dsn,
application_role=role)` installs versioned transactional SQL and restricted
runtime privileges. `bootstrap(owner_dsn, tenant_id=..., administrator=...,
project_ids=[...])` is an operator-only initial enrollment API. It is not an HTTP
endpoint. The initial administrator receives policy and membership administration;
metadata, content, control and audit access are separate explicit permissions.
Credentials belong in protected operator configuration, never source or browser
payloads. Runtime construction rejects unsafe database roles.

`GovernanceStore.query(Principal, Query)` and
`GovernanceStore.command(Principal, CommandEnvelope)` are trusted service APIs.
`Principal` is created only by a verified identity adapter; constructing the
dataclass does not authenticate a caller. The issuer and subject determine
identity, and every call resolves current tenant/project grants. Token roles,
email, desktop `Scope.personal`, paths and local usernames are not authorization.

Commands bind protocol, actor, tenant, project, operation, expected revision and
complete payload semantics. Current authorization precedes receipt replay.
Policy mutation, membership mutation, audit insertion and a retry receipt commit
together; audit failure rolls the decision back. A grant change serializes against
in-flight admitted operations. Transaction-local tenant row policies protect
against accidental unscoped SQL. They supplement the service's authorization;
clients never receive database credentials. Historical policy, audit and command
receipts are insert-only for the runtime login. The database owner can still
rewrite the primary database. A separate append-only archive adapter and
restricted relay are implemented; independent production custody and restore
qualification remain required. See the [operations guide](OPERATIONS.md).

Metadata projections exclude project labels/paths, prompts, transcripts, check
output, errors and arbitrary payloads. Initial query kinds are `metadata`,
`policy` and `audit`; content and execution-control endpoints live in explicit
separate adapters. Successful access decisions are audited. Authentication/denial
ingress audit and deployed desktop enrollment still require qualification. The
SCIM, content, monitoring and archive source adapters are described below and in
the operations guide. The separate host facade implements certificate activation,
leases and project request units. Database audit cursors currently use a global sequence: gaps
are not tenant event counts and can expose aggregate service activity.

To run real database tests, provision a disposable database and a protected JSON
file containing `owner_dsn`, `application_dsn` and `application_role`. Set only its
path in `SONN_GOVERNANCE_TEST_CONFIG`, then run:

```powershell
python -m pytest -q services/governance/tests
python -m ruff check services/governance
```

The cross-package `test_managed_host_client.py` and
`test_managed_native_runtime.py` fixtures run from the repository root with the
desktop source available on `PYTHONPATH` (also include `services/governance/src`
if it is not installed). They additionally need the desktop's `psutil` dependency;
the clean fixture environment used `psutil==7.1.0`. Keep a standard-library TLS
environment: inherited HTTP proxy and alternate certificate-store hooks are not
part of these mutual TLS proofs.

## Managed native admission adapter

`HostChannelClient` and `ManagedRuntime` live in the desktop engine and import no
governance server or PostgreSQL modules. They are explicit trusted-parent seams;
ordinary personal runs construct neither. The client uses a captured HTTPS
origin, configured CA and pinned client certificate, sends bounded fixed JSON
operations without automatic retries, and never serializes certificate keys to
children or browser state. A loopback test profile requires explicit opt-in.

Protocol 2 binds an enrolled owner/host generation, shared worker slot, exact
model selection, request purpose and input digest before one model dispatch.
Each tool requires a first-use permit tied to its completed primary request and
argument digest. The local SQLite journal commits send intent and consumes each
permit once; local Stop/epoch checks and a conservative monotonic lease deadline
still apply immediately before invocation. Pause retries local admission only.
Provider/backend construction follows slot admission. Owned children use the
existing parent RPC channel and receive no governance credentials.

Offline or lost dispatch replies prevent invocation and retain uncertainty.
Bounded fair retries deliver only immutable observations and metadata; restarts
do not restore dispatch or control authority. Actual resource cleanup releases
the worker slot while unknown model/action accounting stays held. Remote controls
are exact-revision Pause/Stop requests with separate receive and outcome receipts;
a local command acknowledgement is not process-stop evidence. Local human-led
Git finalization/check/application remains outside central model-tool permits.
These are source and local fixture guarantees for a conforming managed runner,
not control of arbitrary local executables or a deployed organization claim.

Managed restart recovery is explicit. `ManagedRecovery` opens earlier epoch
journals through the captured desktop configuration and fences their retained
permits without acquiring old leases. It derives request/action reports from
exact native input and action identities plus retained observations or explicit
operator reconciliation receipts. Old worker slots require a fresh native
process-identity observation and an acknowledged remote cleanup report. Only
then, with native accounting/effects resolved, can a new epoch attach through the
same managed admission path. Missing or foreign history blocks this transition.

A known failed invocation consumes one request unit; it is not relabeled as a
successful response. Unknown remote request units can remain visibly held while
a locally reconciled new epoch proceeds within the remaining server allowance.
A denial or absent response never proves that an old reservation was unused.
The recovery fixture kills a real owning host, reopens SQLite state, observes
its named-job child cleanup, reports exact retained evidence over mutual TLS,
and dispatches the replacement epoch through PostgreSQL-backed permits.
The desktop service exposes paged epoch/record inspection and asynchronous
proof-derived reconciliation. It never accepts remote outcomes or process IDs
as browser assertions. Its real browser fixture exercises a killed owning host,
keyboard controls, reconnection, a compact layout and resumed native execution;
the blocked-HTTP fixture verifies that local Stop remains independent of delivery.
Retained owner-effect journals separately gate continuation on native cleanup and
acknowledged observations, including when an effect's result remains unknown.
An explicit absence check can close an old unclaimed admission only after the
server atomically stores a tombstone for that exact original lease and resource
ID. The local journal retains that proof separately from settlement receipts.
Existing resources and possible started requests retain their accounting. Actual
TLS fixtures cover lost uncommitted worker/request/effect sends and rejection of
delayed admissions after the fence.

Tests use unique fixture tenants and independent connections; they never infer a
default database or drop existing application tables. Missing configuration skips
database tests, which is not qualifying evidence. The source checkpoint exercised
real PostgreSQL 18 under a restricted application login for cross-tenant/project
denial, revocation before replay, revision/idempotency races, transactional rollback,
composite foreign keys and row-policy context reset. This fixture is not a real
organization identity deployment or a two-host managed execution qualification.

Schema upgrades run under an advisory migration lock in one transaction. Version
4 retains prior receipts and policy history and binds each host to the member
who enrolled it. A historical host without a matching retained enrollment
receipt fails closed for new admission. Real isolated-database fixtures verify
this upgrade and reject unknown future schema versions without mutation.

## Independent sensitive grant review

Adding `content_read`, `content_write` or `retention_admin` to oneself, including
broadening an existing project grant to the tenant, creates a pending exact
membership snapshot. It changes no effective permission. A different current
member with both `membership_admin` and `membership_approve` must approve its
digest at the captured authorization revision through `GrantApprovals.command`.
The requester and target cannot approve the request. Any intervening membership
revision makes approval stale; rejection and withdrawal remain explicit audited
decisions. Revoking effective access takes effect immediately, and replaying an
old approval receipt does not restore it. Independent explicit grants to another
member remain a membership administrator responsibility.

Operator bootstrap supplies the initial approval authority; it is not a public
endpoint. Migration preserves historical grants and receipts rather than claiming
that old grants received this new review. New source tests prove no access before
approval, concurrent approval/rejection, stale scope rejection and audit rollback.

## SCIM provisioning profile

The separately launched SCIM adapter implements bounded Users/Groups creation,
retrieval, listing, replacement, PATCH and deletion. `If-Match` is required for
updates and deletion; resource versions and `ETag` change atomically with the
stored representation. Lists support `startIndex`, `count` (0–100), and exact
`externalId eq` or user `userName eq` filters. Unsupported attributes, filters,
bulk operations and PATCH paths return errors. These choices implement the
supported subset of [SCIM protocol](https://www.rfc-editor.org/rfc/rfc7644.html)
and [resource schemas](https://www.rfc-editor.org/rfc/rfc7643.html).

An operator uses `register_provisioner` to create a tenant/issuer-bound credential
with an explicit expiry, and delivers its one-time returned token through a
protected channel. Only its hash is stored. Current revocation is checked on
every operation. Provisioning credentials are separate from human resource
tokens and host certificates. User `externalId` and the explicit identity
extension's `subject` are immutable; the issuer comes from the credential.
Username, email-like values and token roles never infer identity or authority.
The required extension is
`urn:sonn:params:scim:schemas:extension:identity:2.0:User`, containing `subject`.

Human membership administration configures exact group mappings through
`ScimStore.map_group`. This profile permits metadata, control, host administration
and audit grants. Sensitive content/retention and membership/policy administration
group mappings are rejected; they cannot bypass independent grant review.
Removing a group removes only that source of permission. Local suspension and
IdP suspension are separate checks: neither interface can override the other's
suspension. Removing an enrolling member's host administration authority fences
new host requests and leases, while old accounting observations remain possible.

The current bounds are 10,000 users and 1,000 groups per tenant, 256 members per
group and 20 PATCH operations. Usernames preserve display spelling and compare
with NFKC/casefold normalization. Nested groups, arbitrary filtering, IdP-specific
schema extensions and an external IdP integration are unqualified. Deletion
deactivates access and retains identity tombstones; it is not personal-data
erasure. Source tests exercise real PostgreSQL and HTTP, including cross-tenant
credentials, revocation, conditional races, mapping removal, suspension, identity
immutability and audit rollback. They do not establish real organization SSO or
provisioning configuration.

## Resource-token HTTP boundary

The separately launched HTTP API accepts RS256 resource tokens with header
`typ=at+jwt`, a bounded `kid`, the configured issuer and one exact resource
audience. It requires `sub`, `iat`, and `exp`, validates optional `nbf`, and limits
token lifetime to one hour. This verifies API resource requests. The separate
native OIDC and SCIM adapters described in this guide implement login and
provisioning; they are not implicit features of this token-verification module.

Only protected operator configuration selects the issuer and JWKS URL. The
verifier rejects token-supplied key URLs/keys, unrecognized JOSE extensions,
redirects, compressed/oversized key responses, duplicate keys and private key
material. HTTPS verification stays enabled. The bounded key cache defaults to
five minutes; unknown-key or signature rotation can refresh at most once per
30 seconds, with at most two transport attempts. Removed signing keys may remain
valid until the configured cache expiry. Membership and project permission
revocation always use fresh database authorization, including command replay.
These choices follow the [PyJWT 2.10.1 verification API](https://pyjwt.readthedocs.io/en/2.10.1/api.html).

Available authenticated routes are:

- `GET /v1/tenants/{tenant_id}/projects/{project_id}/metadata`
- `GET /v1/tenants/{tenant_id}/projects/{project_id}/policy`
- `POST /v1/tenants/{tenant_id}/projects/{project_id}/commands`

The command body contains exactly `protocol_version`, `command_id`,
`expected_revision`, `operation="set_policy"`, and `payload={"policy": ...}`.
The route supplies the target scope; a body cannot supply a principal, tenant
role, or supervisor authority. Optional service adapters add the separately
authorized routes described below. Missing and foreign
resources share the same denied response. Responses disable caching; errors
exclude tokens, connection strings, bodies and internal exception text.

## Explicit service launch

Run `sonn-governance --config-file <absolute-protected-json-file>` or
`python -m sonn_governance --config-file <absolute-protected-json-file>` after
operator migration/bootstrap. Importing or installing the package opens no
listener and performs no enrollment. The JSON object requires:

- `database_dsn`: the restricted application connection string.
- `identity`: `issuer`, `audience`, `jwks_url`, and optional bounded
  `IdentityConfig` settings. The default profile is `production`.
- `listen`: an explicit IP literal `host` and integer `port`.
- `tls`: absolute existing `certificate_file` and `private_key_file` paths.

Production requires HTTPS on the service itself and rejects reserved test
issuers, loopback issuers, and the test transport opt-in. The initial listener
does not trust proxy or certificate headers; a TLS-terminating proxy which
forwards plaintext is not a supported production topology. Access logs are
disabled because request URLs can contain credentials. Operators remain
responsible for protecting configuration/key files, database transport and
backup/storage security; these fixtures establish none of those deployment
properties.

Disposable HTTP fixtures require both `profile="test"` and
`allow_test_loopback=true` for a loopback issuer. That profile only binds a
loopback listener and rejects non-loopback peers. `tls=null` is accepted only in
that explicit test profile. Never use test issuers or fixture keys as production
identity configuration.

The identity/HTTP/CLI checkpoint exercised real loopback HTTP with signed local
RSA tokens and real PostgreSQL authorization, including wrong signature,
issuer, audience, expiry, not-before, algorithm and key-source rejection,
rotation/cache bounds, revoked replay, and secret-free error responses. It used
local generated issuer keys, not an external SSO account or deployed service.

## Certificate-authenticated host channel

The human API also exposes
`POST /v1/tenants/{tenant_id}/projects/{project_id}/hosts/commands` when constructed
with `HostGovernance`; the standalone human-service launcher wires that adapter.
The same command envelope permits only `enroll_host` and `revoke_host`, guarded
by the separate `host_admin` permission. Enrollment registers a pending opaque
host ID and expected certificate SHA-256 fingerprint, then returns a 300-second
single-use challenge. It grants no lease until that certificate proves possession
over mTLS and presents the challenge. Revocation targets the exact current host
revision; replay never creates another enrollment or generation.

`sonn-governance-host --config-file <absolute-protected-json-file>` launches a
separate TLS listener. Its configuration contains `database_dsn` and `transport`:
an explicit IP `host`, `port`, existing absolute `certificate_file`,
`private_key_file`, `client_ca_file`, and optional `profile`/bounded
`maximum_connections`. Test profile restricts the listener to loopback; TLS and
client certificate verification remain mandatory in both profiles. Use a clean
independent Python environment with the pinned dependencies and the standard
Python SSL implementation. Global startup patches to SSL are outside this
qualified source envelope; the initial shared development environment's
`pip.truststore` server wrapper failed client-certificate negotiation and was not
used as TLS evidence.

The host identity comes only from the peer certificate verified on that actual
TLS socket: fingerprint and certificate expiry. A certificate requires client-auth
extended usage, must be a leaf, and must chain to the configured CA. Forwarded
certificate headers, bearer credentials and JSON identity/scope fields never
replace it. The listener supports one bounded HTTP/1.1 JSON request per
connection, at most 16 KiB incomplete headers and 32 KiB bodies, a five-second
request-read deadline, and a default 32 concurrent connections. The parser is
the pinned [h11 protocol implementation](https://h11.readthedocs.io/en/latest/api.html),
with [Python TLS certificate verification](https://docs.python.org/3/library/ssl.html).
After an early rejection, the listener discards at most 64 KiB of arriving input
for at most 250 ms before closing. This bounded transport cleanup preserves the
error response without parsing or admitting the rejected body, addressing the
[HTTP connection-close reset race](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.6).

Fixed host routes are `POST /v1/activate`, `/v1/leases`,
`/v1/requests/reserve`, `/v1/requests/start`, and `/v1/requests/settle`.
Their bodies contain only the relevant challenge, opaque command/request/lease
IDs, expected policy revision, or observation outcome. The store resolves all
tenant/project/host/generation authority from the verified certificate.

A lease carries a server-expiring policy snapshot and zero offline request
allowance. Two hosts share the project's request limit. Each request is reserved
before its first start; only the first committed start returns a dispatch permit.
An exact retry returns no new permit, so a lost response must not trigger another
provider invocation. Revocation and lease/policy expiry close new admissions.
Exact old observations can still be attributed to the enrolled certificate;
uncertainty stays reserved, and a recorded start cannot be refunded as
`never_started`. The result labels this as authenticated host reporting. It does
not independently prove provider execution, dollar spending, artifact quality,
or offline/local-machine enforcement.

Real local TLS fixtures exercised two separately certified clients plus wrong
CA, expired, absent and unenrolled certificates. A combined human HTTP/mTLS/
PostgreSQL fixture exercised activation/replay, shared limits, cross-host denial,
one-time dispatch permits and post-revocation uncertainty. No desktop enrollment,
real organization CA, external host or provider execution was used.

## Explicit encrypted content API

Constructing the human API with `content=ContentStore(...)` enables scoped
`/v1/tenants/{tenant_id}/projects/{project_id}/content/{content_id}` routes:

- `POST` uploads exactly `command_id`, canonical `data_base64`, `media_type`,
  and `retention_seconds`. Decoded content is limited to one MiB. Current
  supported uploads are complete UTF-8 text/plain or application/json, subject
  to the store's credential-pattern screening; image publication is unavailable.
- `GET` returns metadata only. `GET .../bytes?offset=0&limit=16384` requires
  separate content read permission and current explicit-content policy, and
  returns authenticated byte pages as `data_base64`, at most 65,536 decoded bytes.
- `POST .../retention` takes `command_id`, `expected_revision`, `operation`,
  and optional hold `reason`. Hold, release and deletion use the separate
  retention permission and immutable command receipts.

Responses disable caching, expose no direct content URLs or encryption keys,
and use generic unavailable errors. Current permissions and policy are checked
before upload replay or plaintext disclosure. Metadata access is separate from
plaintext access. Secret-pattern screening is not proof that arbitrary text
contains no sensitive information; rejected content must be explicitly sanitized
and uploaded again.

The launcher enables this adapter only with optional
`"content": {"key_ring_file": "<absolute-protected-file>"}`. The separate JSON
ring contains `active_id` and `keys`, mapping one to sixteen key IDs to canonical
base64-encoded 32-byte keys. Existing key IDs must remain available while their
ciphertext is retained. No key is stored in PostgreSQL or copied to a response.
Operators must restrict Windows ACLs to the service identity and administrators;
POSIX additionally rejects group/other access. Symlink, relative, oversized,
duplicate-field and invalid-length rings are rejected. Key loss prevents reads;
this source fixture does not establish production key custody or backup recovery.

## Managed runner protocol 2

The host launcher requires runner protocol 2. The lease request includes
`runner_protocol: 2`; responses retain envelope `protocol_version: 1`, identify
the host generation, and include `issued_at` and `expires_at`. A conforming host
uses a fresh command ID and a monotonic deadline measured from request start.
An exact lease replay never extends its original authority. Offline allowance
is zero; restart does not restore a dispatch permit.

Under `/v1/resources`, `workers/reserve` admits a captured managed-run binding
against the shared project worker cap. Uncertain slots remain held until an
explicit host cleanup observation. `requests/bind` records the exact worker,
selected provider/model, primary/planning/compression purpose and input digest
before `/v1/requests/start` can grant one dispatch. `tools/authorize` requires a
completed primary request from the same worker and current model/tool policy;
it records the action and argument digest without receiving content. Worker and
tool admissions return a permit only on the first successful call. Lost replies
cannot be turned into additional launches by retrying.

`workers/observe`, `tools/observe` and request settlement preserve attributed
cleanup or uncertainty after host revocation. These reports are not independent
proof of provider success or process termination. The protocol constrains the
managed client; it cannot prevent unrelated programs from using credentials.
The source constructor retains explicit protocol-1 compatibility for accounting
fixtures, but the production host launcher refuses protocol-1 admissions.

## Native OIDC client seam

The operator entry point is `sonn-governance-admin --config-file
<absolute-client-json> --identity`, or `--request-file <absolute-request-json>`
for one explicit resource operation. Client configuration contains `server_url`,
the `oidc` fields below and optional absolute `ca_file` for resource TLS. It never
contains a database password, client secret or saved access/refresh token.
Each invocation opens native browser sign-in, holds the credential only in
memory, sends one request to the configured origin, and exits. Redirects and
automatic mutation retries are disabled. An ambiguous result is reported as
`outcome_unknown`; inspect the original command identity before retrying.

Request JSON contains `method`, a fixed `/v1/...` `path`, optional bounded
`query`, and a `body` for POST. Supported human routes include explicit member
inspection and tenant commands (`set_membership`, `decide_grant`, `revoke_grant`,
`map_scim_group`), scoped audit/host/run views, policy/enrollment commands, remote
pause/Stop and the separate content gateway. `/v1/identity` returns only the
verified opaque actor ID and expiry. It does not discover tenants or enroll the
desktop. All routes resolve current server permissions before receipt replay.
Membership pages separate manual and provisioned grants and expose the current
authorization revision; sensitive self-elevation still requires another approver.

For example, a policy inspection file has this shape (replace both UUIDs with
explicitly assigned organization/project IDs):

```json
{"method":"GET","path":"/v1/tenants/00000000-0000-0000-0000-000000000001/projects/00000000-0000-0000-0000-000000000002/policy"}
```

Enrollment command results contain a one-use challenge intended for the enrolled
host. Protect saved output deliberately; this command does not write credentials
to local configuration automatically. Admin status grants no content permission.

`NativeOIDC(NativeOIDCConfig(...)).login()` is a separate opt-in client module,
called from a host background thread. It binds an ephemeral numeric loopback
listener before launching the operating system's external browser, uses random
state/nonce and PKCE S256, and closes that listener on success, cancellation,
timeout or error. The callback path is explicitly registered; its port is chosen
by the OS. It does not embed a client secret or use an embedded browser.

Configuration pins issuer, client ID, resource audience, authorization/token/JWKS
endpoints and scopes. Production endpoints require HTTPS; local fixtures require
both the test profile and loopback opt-in. The initial protocol envelope requires
the RFC 9207 `iss` authorization-response parameter, RS256 signed ID tokens for
the registered client, and `at+jwt` access tokens for the configured resource.
Both tokens must identify the same subject. Nonce, issuer, audience, expiry,
signature, authorized-party and optional access-token hash are checked. Token
redemption is bounded and never retried after an ambiguous response.
An optional HTTPS `resource` URI is sent on both OAuth requests when configured;
it is separate from the resource token's potentially opaque audience string.
The current callback listener uses IPv4 loopback.

The returned access token stays in memory and is excluded from object repr.
There is no refresh-token storage, offline-access scope, desktop sign-in UI or
automatic account enrollment. Callers must not serialize the credential object.
Real local issuer fixtures exercise browser redirects and one-use code exchange;
an external IdP registration and organization account have not been qualified.
The protocol follows [native OAuth](https://www.rfc-editor.org/rfc/rfc8252.html),
[PKCE](https://www.rfc-editor.org/rfc/rfc7636.html),
[OIDC token validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation),
and [issuer response binding](https://www.rfc-editor.org/rfc/rfc9207.html).

## Sharing content retention

Sharing content has separate explicit retention controls in schema 15. Human
`GET /v1/tenants/{tenant_id}/sharing/content/{kind}/{resource_id}` returns only
retention metadata, where `kind` is `terms` (grant UUID) or `message` (message
UUID). `POST` to that path plus `/retention` takes `command_id`,
`expected_revision`, and one of `hold_sharing_content`,
`release_sharing_content_hold`, or `delete_sharing_content`. A hold also requires
`reason` from `owner_request`, `security_review`, or `legal_review`; free text is
not retained. Current `retention_admin` on both captured projects is required
before inspection or retry. No decryption keys or content-view permissions are
needed. The ordinary human service launcher exposes this seam; the operator
CLI accepts these fixed paths through its explicit `--request-file` workflow.

Holds prevent deletion. Deletion removes active ciphertext/key/nonce/direct hash
and closes future disclosure and new acceptance/relay, while preserving causal
accounting and already accepted receiver work. Prior immutable fingerprints,
downloaded copies and backups remain. New creation and retention audits bind an
exact resource ID and retention revision; older unbound sharing history is not
fabricated into restore evidence.

## Managed owner effects

Schema 12 separates owner Git/check commands from model-originated tools. Policy
version 1 grants no owner effects. Version 2 requires an additional explicit
`allowed_effects` list, drawn from `writer_git`, `candidate_git`, `candidate_check`
and `checkout_apply`. The enrolled owner also needs current `control_execute`.
An allowed model/tool list alone cannot grant branch application or a named check.

The mTLS routes `/v1/resources/effects/authorize` and `/v1/resources/effects/observe`
bind each invocation to its current lease, run binding, host generation, effect
UUID and immutable local semantics digest. Paths, command arguments and output
stay local. Authorization replay returns no dispatch permit. Observations remain
available after revocation and distinguish completed, failed, never started and
uncertain; they are attributed host reports, not independent artifact verification.
Required archive delivery also gates new owner effects. The desktop execution
hook has separate source qualification before managed writing is enabled.

## Tenant-bound SCIM provisioning profile

`sonn-governance-scim --config-file <absolute-protected-json-file>` launches a
separate provisioning listener. Its configuration contains the restricted
`database_dsn`, `listen` IP/port, `transport` profile, and absolute TLS certificate
and private-key files. Production requires actual HTTPS; forwarded transport
headers cannot replace it. Disposable HTTP fixtures require both
`transport.profile="test"` and `transport.allow_test_loopback=true`, a loopback
listener, and may set `tls=null`. Access logs stay disabled.

An operator enrolls a random provisioning bearer with `register_provisioner` and
an explicit tenant, identity issuer and expiry. Only its digest is retained in
the credential directory. This credential is separate from human and host
identity. Each request resolves its current tenant/issuer and revocation in the
store; no route or supplied identity attribute can override that authority.

The supported [SCIM 2 protocol](https://www.rfc-editor.org/rfc/rfc7644.html)
profile exposes `/scim/v2/Users` and `/scim/v2/Groups`: create, get, bounded list,
replace, the documented PATCH subset, and delete. Responses use
`application/scim+json`; writes require JSON, are limited to 256 KiB and reject
compressed or duplicate-field documents. Updates and deletion require exactly
one current `If-Match: W/"revision"`; returned metadata and ETag carry the new
version. Lists support `startIndex`, `count` (0–100), and equality filters on
`externalId` or user `userName`. Bulk, sorting, nested groups, password changes
and arbitrary filter/PATCH expressions are unsupported.

Users require the standard User schema, stable `externalId`, `userName`, and
`urn:sonn:params:scim:schemas:extension:identity:2.0:User` with an explicit immutable
`subject`. Username or external ID never guesses an OIDC subject. The credential
pins the issuer. Groups contain same-issuer User IDs; only a separately authorized
human server mapping can assign supported permissions. Provisioned documents
cannot supply roles, permissions or tenant IDs. Local suspension and manual grant
provenance remain distinct from IdP status; deactivation and group removal close
effective access. Missing/foreign resources and failures use bounded SCIM errors
without credential or internal exception text.

This source profile has local HTTP/PostgreSQL fixtures. External IdP provisioning,
organization schema mapping, discovery endpoints and production token custody
remain separate qualification work; no general IdP compatibility is claimed.
