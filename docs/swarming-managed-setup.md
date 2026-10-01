# Managed swarming setup (source implementation)

This page describes the source configuration and durability boundary. It does
not qualify an installed release, external identity provider, deployed governance
service, or resistance to an administrator who controls the client machine.
AI Employee work remains paused and is separate from this feature.

## Explicit operator setup

A managed team requires an enrolled certificate and a running protocol-2
governance host endpoint. Enrollment and permission administration happen through
the separate service. Starting the desktop does not enroll a host, authenticate
a human, infer tenant roles from a token, or convert a saved personal team.

Pass the operator configuration only through the GUI startup command:

```text
python -m lumi gui --swarm-managed-config C:/Users/operator/.lumi/managed/client.json
```

The option is not accepted by a WebSocket command. Invalid configuration stops
startup with a generic error instead of silently falling back to personal
execution. The startup log redacts this configuration path.

The JSON file has these exact fields:

```json
{
  "version": 1,
  "transport": {
    "endpoint": "https://governance.example.org:8444",
    "ca_file": "C:/Users/operator/.lumi/managed/ca.pem",
    "certificate_file": "C:/Users/operator/.lumi/managed/host.pem",
    "private_key_file": "C:/Users/operator/.lumi/managed/host-key.pem",
    "certificate_sha256": "<enrolled leaf certificate SHA-256, 64 lowercase hex characters>"
  },
  "tenant_id": "<tenant UUID>",
  "project_id": "<governance project UUID>",
  "host_id": "<enrolled host UUID>",
  "host_generation": 1,
  "owner_id": "<enrolling human actor ID, 64 lowercase hex characters>",
  "local_owner_id": "local:<desktop account name>",
  "workspace": "D:/Projects/example",
  "policy_revision": 1
}
```

Replace the placeholders with exact operator-issued identities. The lease must
attest the configured tenant, project, host, generation and enrolling actor. A
changed policy revision requires an explicit configuration change; a different
model is not silently selected.

Configuration, CA, certificate and private key files must be absolute existing
files outside the workspace. Symlinks and Windows reparse points are rejected.
On Windows, native ACL inspection requires the owner and allowed trustees to be
the current account, SYSTEM, or Administrators; an OWNER RIGHTS ACE is permitted
only after the actual owner is verified. Unexpected allow ACE types fail closed.
On POSIX, files must belong to the current user with no group/other permissions.
The client verifies these restrictions and does not silently rewrite ACLs.

The operator configuration binds one local owner and one canonical workspace.
Managed scope is a new organization scope using that captured local project and
session. Personal history retains its original scope. Personal collaboration
grants do not become organization collaboration authority.

## Authority and restart behavior

Managed protocol 2 requires online, authenticated decisions for worker slots,
request reservations and starts, and tool actions tied to a completed primary
request. The offline request allowance is zero. Limits are request units and
worker slots, not dollar guarantees or independent verification of local CPU.
Local Stop remains available when the governance endpoint is unavailable.

The parent process keeps TLS configuration and private keys. Worker/model inputs,
browser payloads and the local managed journal receive no private key paths or
certificate key material. Effective policy is displayed only after a successful
authenticated lease; a configured policy revision alone is not a current grant.

Each run/epoch has a separate SQLite journal under the project's external state
directory. Transactions use separate connections, `synchronous=FULL`, foreign
keys and short `BEGIN IMMEDIATE` writes with DELETE journaling. The journal does
not duplicate the swarm DAG or supervisor state.

The journal persists an immutable mapping from local identities to opaque wire
UUIDs before network calls. It captures the exact original lease before an
admission send. A first positive reply permits one same-instance claim, committed
before provider/tool/worker dispatch. A replayed reply, lost reply, expired lease,
or reopened journal never yields a second permit. A new desktop attachment over
retained run/epoch state is refused pending explicit recovery; startup does not
renew that execution authority or replay participants.

Uncertain request usage remains held. A durable remote start is never reported
as a zero-unit request. Worker slots are released only on an explicit trusted
stopped or known-unstarted observation. Restart itself proves neither process
termination nor provider success. Failed responses and pending observations remain
visible without reporting a completed team.

Observation delivery can retry immutable outcome payloads. Durable fair scheduling
prevents a permanently unavailable earlier request from starving later cleanup
records. A later concrete worker/tool outcome can supersede delivery of earlier
uncertainty while retaining the original record; this is not a fabricated remote
acknowledgement. Reports contain enumerated states, counters and revision numbers,
never prompts, tool arguments, handoffs, check output or arbitrary freeform text.

Remote Pause/Stop requests bind the exact epoch and local revision. Receiving a
request and applying a local command are separate durable receipts. An applied
Stop command does not itself claim that all processes have stopped. The observer
pump runs outside service controls and database transactions; closing it reports
whether its own thread actually joined, without asserting worker cleanup.

After the original host lease expires, choose **Take over expired team**. This
fences all retained epoch journals without renewing them. The **Organization
recovery observations** panel selects an epoch and pages through model requests,
worker slots, tool actions, or remote controls. **Derive and report retained
observation** uses the exact native record; it does not accept an outcome chosen
by the browser. First resolve any uncertain local accounting from attributable
evidence. Worker cleanup additionally requires actual captured process identity
proof. **Retry organization observations** retries delivery without replaying
execution. Stop remains available while delivery runs in the background.

**Continue reviewed team** requires native accounting/effects to be resolved and
every old shared worker slot and owner process to have acknowledged cleanup.
It creates a separately bound journal, registration and fresh lease for the new
epoch. Remote request units that are still unknown can remain held and visible;
continuation does not refund them. Missing journal history blocks continuation.

If an admission send lost its reply before local dispatch, **Check and fence
missing admission** asks the server to atomically retain a tombstone for that
exact ID and original lease. Only an absent resource and an original unclaimed
local permit can be closed this way. The tombstone blocks delayed admissions;
an existing resource, denied lookup, or ordinary missing response proves no
outcome and releases no allowance. Claimed effects and possible remote starts
still require their own retained observations.

Owner Git mutations and named verification checks use a separate per-epoch
`.effects.sqlite` journal. Policy version 2 must explicitly list the relevant
`allowed_effects`: `writer_git`, `candidate_git`, `candidate_check`, or
`checkout_apply`. The enrolled human also needs current `control_execute`
permission. Version 1 policies permit none of these effects. Each actual process
requires its own online admission, bound to hashes of the frozen argv,
environment, working directory, candidate inputs and exact apply approval.
Only hashes and public identities leave the host; check output is not uploaded.

The single-use permit is consumed under the local Stop/epoch gate immediately
before the owned child receives executable arguments. Lost replies and expired
or replayed permits launch no command. Cleanup observations can be delivered
after revocation. A completed remote owner-effect observation describes one
observed command and its cleanup; it does not accept a work item or prove that a
candidate passed every check. Historical effects require native process cleanup
evidence and acknowledged observation delivery before restart continuation.
Missing coverage records fail closed; process disappearance alone never invents
a successful command result.

## Evidence and remaining qualification

Focused source tests exercise independent-process reservation races, single-use
dispatch, lost replies, rollback, restart uncertainty, exact leases and owners,
fair outcome delivery, metadata filtering, and actual Windows ACL rejection.
The transport/runtime suite separately exercises real TLS and disposable
PostgreSQL with fixture providers. Real organization provisioning, cross-host
deployment and protected operator rollout require their own deployment evidence;
local packaged proofs do not establish those conditions.

Windows candidate `0.19.2.dev11+swarm20260926.6` passed two browser cases using its
unmodified GUI launcher, a protected managed configuration, actual frozen reader
child, mutual TLS and PostgreSQL. The scripted reader completed two accounted
requests and an attributed file read without disclosing configuration or content
in remote metadata. With governance offline and inference still blocked, local
Stop responded in 68 ms, the child stopped, and the interrupted request remained
uncertain with no tool action or second provider call. These are local candidate
proofs; no live model, deployed identity provider, or installed-bundle change was
involved. The fixture is `tests/swarm_packaged_managed.browser.cjs`.

The source recovery browser fixture uses a real killed host and owned child,
mutual TLS, PostgreSQL, actual controls, keyboard input, reconnection and a 390px
layout. Its resumed scripted reader acquires fresh managed authority and records
its model/tool admissions. This establishes that exercised recovery path, not
live-provider quality or deployed organization qualification.

The final September 26 candidate `0.19.2.dev11+swarm20260926.11` repeated the
managed reader and offline Stop cases (19.96 seconds together), then passed
packaged managed writing (27.69 seconds) and actual app crash/restart recovery
(71.40 seconds). The writer used the frozen native file tool and owned Git/check
processes, kept the original checkout unchanged before review, refused to apply
over a dirty personal edit, and required a separate final acceptance. All four
declared owner-effect kinds retained their central observations.

The recovery test killed the exact frozen app after its first provider request,
observed named-job child cleanup, restarted the same HOME/config/conversation,
and waited for the actual 60-second ownership lease to expire. Explicit retained
proof and accounting charged one failed request before the new epoch acquired
fresh managed authority and completed two requests. Reconnection, compact
controls, exact frozen worker identity and absence of credential/content leakage
were checked. Inference remained scripted through a loopback Ollama-compatible
HTTP fixture; process, Git, browser, mutual TLS and PostgreSQL effects were real.

Use `tests/swarm_managed_writers.browser.cjs` with
`SWARM_PACKAGED_EXECUTABLE` set to an explicit candidate, and
`tests/swarm_packaged_managed_recovery.browser.cjs <candidate-exe> <playwright>`
to repeat those isolated packaged workflows. Both require the explicit
`SWARM_MANAGED_PYTHON`, `LUMI_GOVERNANCE_SOURCE` and protected
`SONN_GOVERNANCE_TEST_CONFIG` fixture settings.

## Running the managed browser tests

The governance service lives in Lumi Cloud, not in this repository. The
managed browser tests (`tests/swarm_managed*.browser.cjs` and
`tests/swarm_packaged_managed*.browser.cjs`) import it from an explicit
external checkout. Without all three settings below each test is reported as
skipped, naming the missing ones; that is not evidence. A set but wrong
`LUMI_GOVERNANCE_SOURCE` fails the test instead of skipping it.

- `LUMI_GOVERNANCE_SOURCE`: a Lumi Cloud checkout, or its `services/governance`
  directory. The fixtures add its `src` and `tests` directories to the path.
- `SONN_GOVERNANCE_TEST_CONFIG`: a protected JSON file for a disposable
  PostgreSQL database (`owner_dsn`, `application_dsn`, `application_role`).
- `SWARM_MANAGED_PYTHON`: an isolated Python with this client's dependencies
  and the governance service's pinned requirements installed.

```sh
LUMI_GOVERNANCE_SOURCE=/path/to/lumi-cloud SONN_GOVERNANCE_TEST_CONFIG=/protected/db.json \
SWARM_MANAGED_PYTHON=/path/to/venv/python node tests/swarm_managed.browser.cjs /path/to/playwright
```
