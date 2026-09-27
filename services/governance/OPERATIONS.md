# Governance source operations

This is a self-hosted source candidate, not a deployed or qualified organization
service. The desktop adapter is exercised with actual local TLS, PostgreSQL and
browser controls; independent-machine deployment remains unqualified. Nothing
here enrolls an existing personal session automatically.

## Identity, host and service ownership

Use the pinned `requirements.lock` in an isolated Python environment. Run the
human resource API, mTLS host listener and SCIM listener only from explicitly
protected configuration files. Their modules and accepted fields are documented
in [README](README.md). Production resource/SCIM listeners require TLS; the host
listener always requires a trusted client certificate with client-auth usage.
Disable access logs and untrusted forwarded identity headers as the launchers do.

Register a public native OIDC client with its loopback callback and PKCE S256,
the exact resource audience, a configured issuer, and explicitly configured
authorization/token/JWKS endpoints. The current native client supports RS256
ID tokens and `at+jwt` resource tokens with an issuer parameter on authorization
responses. It has no refresh-token persistence. Real IdP configuration, member
accounts and group provisioning must be exercised separately from local fixtures.

Provision hosts with separate private keys/certificates; keep their paths and
contents outside workers, model context and browser messages. A human enrollment
command binds one certificate fingerprint and project; possession of the matching
certificate and a short-lived challenge activates it. A valid certificate alone
does not create membership, host identity or a lease. Deactivating the enrolled
member or removing its effective host-administration grant closes new admission.

## Database roles and upgrades

The operator owns schema migration. The application role has no table ownership,
schema creation, superuser or row-policy bypass privileges. Runtime startup
rejects historical-record mutation privileges, including column grants. The
application must never be given the migration owner's credentials. Roles and
their memberships affect effective privileges; inspect inherited and PUBLIC
grants as well as direct grants. [PostgreSQL privileges](https://www.postgresql.org/docs/18/sql-grant.html).

Back up the database and verify a restore into a separate environment before
upgrading. Preserve unresolved requests and retained command semantics. The
source currently rejects an unknown schema version; it does not promise mixed
old/new application operation during arbitrary migrations. Drain admission and
use a coordinated service upgrade until a specific rolling-upgrade matrix passes.
Never replay provider requests or remote controls just because a process restarted.

## Independently credentialed audit archive

Every successful audit insertion atomically creates a bounded metadata outbox
record. If capture fails, the surrounding decision rolls back. The application
can read its tenant-scoped metadata but cannot insert, change, delete or truncate
outbox/delivery/configuration records or disable their triggers. The outbox is
durable delivery state; it is not yet proof of archival at another custodian.

Provision a separate PostgreSQL destination under independent custody and backup
permissions. `initialize_archive(owner_dsn, writer_role=..., archive_id=...)`
creates the destination schema with an operator-chosen UUID and an existing
restricted writer role. The writer may append and read exact records but cannot
rewrite history or destination identity. Table/database owners remain trusted
infrastructure operators; these roles do not resist a machine administrator.

`configure_relay(source_owner_dsn, relay_role=..., tenant_id=..., archive_id=...,
source_id=..., max_delay_seconds=None)` grants a separate relay role read access
to outbox/configuration and append access to delivery receipts. It pins source
and destination UUIDs. Drain historical backlog, then call it with a delay from
30 to 86,400 seconds to enable fail-closed admission. Changing the pinned identity
requires a separately reviewed custody migration, not an ordinary retry.

The relay accepts only a credential whose mutation privileges in the source
schema consist of delivery-receipt insertion. It refuses a role that can alter
authorization grants, application state or historical records. Neither relay nor
destination writer credentials belong in the human resource API's configuration.

Run `sonn-governance-archive --config-file <absolute-protected-json-file>` to
process bounded pages continuously, or add `--once` for one page per configured
tenant. Configuration contains `source_dsn`, `archive_dsn`, `source_id`,
`archive_id`, `tenant_ids`, `profile` and `interval_seconds` (1–60). Production
database connections require `sslmode=verify-full`; `profile=test` only accepts
explicit loopback addresses. Keys, passwords and database URLs never appear in
relay output. OS access controls on configuration and certificate files remain an
operator responsibility, including Windows ACLs.

Destination records retain original bytes, their SHA-256 and a server-created
receipt. Exact delivery after a lost acknowledgement returns the same receipt;
different bytes for the same source/tenant/audit identity are a conflict. Unknown
historical destination/source bindings stay unresolved after migration. The
relay refuses inconsistent retained receipts instead of silently replacing them.

With required archival enabled, overdue undelivered audit closes new host leases,
request admission, run registration and content upload/read. Local cleanup,
central Stop, revocation and historical uncertainty reporting remain available.
Inspect relay health and both stores before reopening admission; an application
restart does not erase the backlog or extend its deadline.

## Content retention and restore

The initial content gateway accepts explicit UTF-8 text/JSON only. AES-256-GCM
keys are held outside the database; rotation writes a new key ID while old keys
remain available for authorized retained objects. Missing keys or authentication
failures close reads. Recognizable secrets are rejected before storage, but this
screen is not a guarantee that arbitrary text is safe. Prepare and deliberately
upload a sanitized copy; the gateway never silently rewrites evidence.

Content expiry closes disclosure. An explicit hold records its actor/reason and
prevents deletion; it never grants read access. Active-store deletion removes
ciphertext and content fingerprint while preserving minimal audit tombstones.
New content audit records bind `decision_id` to the opaque content UUID, plus its
project and revision, so independently archived deletion/hold records can be
matched to the exact restored object without storing its plaintext or hash.
Historical records lacking this binding are refused by the current restore
workflow. Their original receipts may inform separate operator investigation;
their semantic digest alone cannot identify an object for reconciliation.
Backups, exports, managed caches and previously downloaded plaintext need their
own deletion procedures. Do not restore a stale content backup into production
until later deletion/hold records have been reconciled and unauthorized content
is inaccessible. A fresh database hash does not establish that those copies were
erased. [PostgreSQL backup and restore options](https://www.postgresql.org/docs/18/backup.html).

The [quarantined retention recovery workflow](RESTORE.md) provides an explicit
offline source seal, independently pinned archive checkpoint, and reconciliation
into a separate permanently quarantined restore. It does not reopen service
access, reconstruct current authority, resume work, or erase old backups.
Schema 15 extends explicit retention and this restore boundary to sharing terms
and message bodies. Both projects must grant the operator `retention_admin`;
that permission gives no payload access. Deletion preserves historical receipt
fingerprints, causal accounting and independently accepted receiver work. See
the [sharing retention contract](../../docs/swarming-managed-collaboration.md).

## Qualification still required

Retain actual outcomes for two independently provisioned machines or VMs with
separate identities/state, the selected real IdP and provisioning configuration,
TLS/network partitions, revocation, process cleanup, backup restore, retention
and upgrade rollback. The current local tests use real PostgreSQL, HTTP, mTLS,
separate database roles and generated issuer keys on one developer machine.
They establish protocol/storage behavior, not a production storage policy,
external SSO integration, independent-host isolation or live model quality.
