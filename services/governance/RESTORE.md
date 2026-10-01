# Quarantined retention recovery

This operator-only source workflow reconciles encrypted-content retention after
an actual PostgreSQL restore. It keeps both the sealed source and the restored
target permanently unavailable to SONN governance services. It does not rebuild
current membership, policy, host authority, sharing permissions, request
accounting, or process state. It never resumes provider work.

Schema 15 adds the same recovery boundary for encrypted sharing agreement terms
and message bodies. Their creation and retention audit records bind the exact
resource UUID, kind, canonical origin project and revision. Older sharing history
without those bindings is refused; semantic fingerprints are not identity proof.
Content-only schema 14 backups remain supported when no sharing rows or sharing
retention history exist.

**`seal-source` is an irreversible offline operation in this implementation. It
is not an ordinary backup or checkpoint-export command. There is no unseal or
reopen command.** Use separately provisioned recovery databases with explicit
operator approval and custody. The automated qualification uses disposable
databases only; it does not establish a production recovery procedure or erase
historical backups, exports, keys, or plaintext copies.

## Inputs and custody

Run the commands with the separate governance package installed:

```text
python -m sonn_governance.restore_cli ACTION --config-file ABSOLUTE_PROTECTED_CONFIG
```

Configuration is a bounded JSON file, not command-line credentials. Each action
accepts only its exact fields below plus `profile`. `profile: "production"`
requires database `sslmode=verify-full`; `profile: "test"` permits only explicit
loopback hosts and pins libpq's actual `hostaddr` to that host. Filesystem redirects
are refused. Keep configurations and backup files under operator-only access;
OS ownership and ACL provisioning remain an operator obligation for this CLI.
No content-encryption keys are needed by this workflow.

| Action | Required configuration fields |
| --- | --- |
| `identity` | `operator_dsn` |
| `seal-source` | `source_dsn`, `archive_dsn`, `expected_database`, `tenant_id`, `source_id`, `archive_id`, `seal_id` |
| `inspect-seal` | `source_dsn`, `expected_database` |
| `prepare-target` | `target_dsn`, `expected_database`, `restore_id` |
| `reconcile` | `target_dsn`, `archive_dsn`, `expected_database`, `restore_id`, `tenant_id`, `source_id`, `archive_id` |

The `identity` command is read-only. It returns `database`, `database_oid`, and
`system_identifier`; copy the complete object into `expected_database` only after
checking the intended database independently. Pin source and target separately.
The source and target must differ. `seal_id` and `restore_id` are explicit UUIDs
chosen once for these operations. Never change them to retry an unknown result.

The source's existing archival requirement must match the independently pinned
`source_id` and `archive_id`. `archive_dsn` uses a separate **read-only** archive
credential: schema USAGE and SELECT on `sonn_archive.configuration` and
`sonn_archive.records`, with no INSERT, UPDATE, DELETE, TRUNCATE, ownership,
schema CREATE, superuser, or BYPASSRLS capability. The normal append-only archive
writer is deliberately rejected because it can insert records.

## Sequence

1. Retain a protected PostgreSQL custom-format backup of the
   `sonn_governance` schema. For example, use `pg_dump --format=custom
   --schema=sonn_governance --file=...` with independently configured protected
   connection credentials. This workflow does not make or alter the backup.
2. Stop every service, relay, and other client of the source database and isolate
   it from new client connections with operator-controlled network access. Drain the
   archive first. `seal-source` refuses any other connected client; it does not
   terminate one. Service transactions and sealing share a database admission
   lock, so an already-instantiated service cannot admit another transaction
   after sealing. The same transaction revokes database CONNECT from PUBLIC and
   every explicitly granted nonoperator role; the current trusted operator keeps
   access. After commit, old service binaries without the Python gate cannot
   reconnect with those roles. A final client observation refuses an observed late
   connection, but is not atomic network isolation and terminates nothing. An old
   binary does not honor the advisory lock, so externally enforced offline
   isolation remains required throughout this operation. Elevated operators and
   membership in their roles remain trusted and can bypass database restrictions.
3. Explicitly execute `seal-source --output-file ABSOLUTE_NEW_CHECKPOINT_FILE`.
   It verifies the complete selected tenant audit/outbox set against the archive,
   including exact hashes, payload validation, acknowledgements, source/archive
   identity, retention revision chains, and the global audit sequence. A maximum
   of 100,000 audit records is supported; larger sets are refused. Legitimate gaps
   in tenant audit IDs are allowed. A high watermark alone is never accepted.
4. Preserve the returned checkpoint and its SHA-256 under independent custody.
   The source seal stores the same immutable checkpoint. If acknowledgement or
   file writing failed, use `inspect-seal --output-file ABSOLUTE_NEW_FILE` with
   the original source identity. Inspection does not create another seal.
5. Provision an empty target database with a distinct identity and stop its
   clients. Execute `prepare-target` **before** restoring any schema or grants.
   The restore-only `sonn_restore` namespace is a permanent service quarantine
   marker. Even a partial or malformed marker closes service startup, migration,
   and new transactions through existing stores. Target preparation also revokes
   nonoperator database CONNECT before stale schema grants are restored.
6. Restore the retained custom-format schema backup into the prepared target
   with `pg_restore --exit-on-error --no-owner --dbname=... BACKUP`. The backup
   must not contain `sonn_restore`. Do not use `--clean` against the prepared
   database, restore over a live service, or enable application traffic.
   A schema-only restore preserves the prepared database's connection ACL.
7. Execute `reconcile --checkpoint-file ABSOLUTE_CHECKPOINT_FILE
   --checkpoint-sha256 INDEPENDENTLY_PINNED_SHA256`. The digest must come from
   custody of the sealed checkpoint, not just the wrapper in an untrusted file.
   Reconciliation rechecks the entire archive set; missing, changed, or later
   records require investigation and are refused. The restored audit must be an
   exact prefix of the sealed history, and restored content revisions/states must
   match that prefix.

Sealing validates the archive again after staging the seal, before commit.
Validation failure rolls back the marker. Reconciliation validates again after
staging its changes, before commit. Its deletions, receipts, content quarantine,
and audit-sequence restart commit together. PostgreSQL `ALTER SEQUENCE RESTART`
is used because it rolls back with the transaction; `setval` is not used. An
identical acknowledged reconciliation can be inspected by exact replay; a
different checkpoint cannot replace it.

## What the result means

Verified deletion removes restored active ciphertext, content hash, size, and
media metadata. An object created and deleted after the backup has no restored
content row; its exact opaque identity and deletion evidence are retained in the
restore-only table. All content insertion, mutation, and deletion then remain
blocked by a database trigger, including attempts to reuse absent identities.

For sharing terms and messages, deletion removes active ciphertext, nonce, key
identifier and direct payload hash. It preserves causal lineage, byte accounting,
historical receipts and already accepted receiver work. These are separate from
the selected payload and must not be treated as erased. Sharing retention evidence
is recorded in `sonn_restore.sharing_retention`; the result reports
`sharing_deleted`, `sharing_absent_objects` and
`sharing_held_with_reason_unresolved`. Triggers block subsequent writes to both
sharing payload tables. Neither disclosure authority nor sharing approval is
reconstructed.

Archived hold records identify the object, revision, actor and operation but do
not contain the original hold reason. Reconciliation retains that evidence and
reports `held_with_reason_unresolved`. It preserves existing row provenance and
does not fabricate a historical reason or pretend that a new operator hold was
the original hold. The permanent quarantine blocks disclosure and mutation while
that provenance remains unresolved. A fully evidenced hold → release → delete
sequence can remove its restored ciphertext.

The audit sequence is validated before mutation and advanced to the sealed
source's next value, avoiding reuse of archived audit identities. This does not
authorize new audit events or establish a new archive-custody branch.

Only the selected tenant's retention is reconciled; the whole restored database
remains quarantined. Other tenants, current grants and policies, revoked hosts,
sharing permissions, and unknown worker/request/effect outcomes are not reconstructed,
cleared, or settled. There is no supported production reopening path in this
slice. Such a path needs current authoritative state, independent review of
retention and custody, and separate operational qualification.

## Local evidence

`tests/test_restore.py` and `tests/test_restore_sharing.py` use fresh source, archive, and restore databases with
separate credentials and actual `pg_dump`/`pg_restore` binaries. It checks
retention, absent post-backup identities, exact archive/prefix checks, sequence
handling, failed sealing, interrupted reconciliation, immutable replay, and
service refusal before and after reconciliation. The fixture deletes only the
databases and roles it created. Independent production archive custody, disaster
recovery, key restoration, and backup erasure remain separate obligations.
