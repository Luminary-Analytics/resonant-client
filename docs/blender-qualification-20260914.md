# Blender qualification checkpoint

Observed September 14, 2026, 23:34 EDT / September 15, 03:34 UTC. Both model turns
are stopped. No paid build was submitted during the documentation update.
The authoritative sealed protocol, artifacts and billing receipts are in the
SONN repository at `product/qualification/sonn-blender-20260914/`.

## Client and integration

Local package **0.19.2.dev11** uses the actual native SONN engine. Windows
window activation failed, so prospective qualification uses the SONN Client
browser frontend connected to that engine and live Blender MCP. This is not a
CLI replacement or desktop-window input qualification. Blender is 5.1.2 and the
managed bridge is pinned to 1.9.1, with external asset services disabled for tests.

The Check editor probe supplies the required `user_prompt`. Managed `job_start`,
`job_status` and `job_cancel` are discovered through tool search. Start the actual
foreground worker with an argument array; ordinary shell descendants are cleaned
up. One active job per project, bounded logs, two render threads in this protocol
and a maximum 20-minute deadline. Client exit stops owned jobs; application
checkpoint recovery is explicit. Browser reconnection and native client restart
are different boundaries. A request-limit stop does not prove every job stopped.

The full client suite passed 3,322 tests, two skipped, and 40 focused job/editor
checks. The package matched 146 code/static files; bundle policy passed at
55.6 MiB / 256 files. These are local-candidate results, not a public release.

## Project outcomes

- Product Studio: four capped turns, 45 main requests each, across two saved
  conversations. Saved lamp, 110 readable PNGs, 96-frame turntable and imported
  GLB. Managed cancellation/resume preserved 80 valid frame hashes and mtimes,
  repaired two deliberately damaged frames and completed the sequence. Invalid
  configuration preservation and actual-input cache invalidation pass isolated
  checks. Final exploded framing and documentation required operator finishing.
  Material visual distinction remains a quality limitation. No fifth model turn.
- Warehouse Digital Twin: first turn stopped at 45 main requests / 49 tools.
  Saved scene, GLB and 123 readable PNGs. Builder reports 249 owned objects,
  48 pallets and eight parcels; those semantic counts still need independent
  scene verification. Four render manifests report complete; animation reports
  24.97 seconds. Full scene/motion, cancellation/recovery, README and optional
  360-frame multiview qualification remain pending. Three capped turns remain.

No naturally measured render beyond 60 seconds has been qualified. Neither
project establishes flawless autonomous app delivery or causal learning benefit.

## Learning and lessons

The Studio project processed 188 observations, retained 24 examples and performed
50 replay updates; Warehouse processed 48, retained 26 and performed 16 replays.
Both inspected project views have zero durable memory entries. These are project
observations, not account-wide totals. Retained action traces include failures
and model-authored checks; they must not be presented as verified abstract rules.

Operator findings include managed process ownership, validating before mutation,
resuming from verified checkpoints, hashing current inputs and checking the
final output after all transformations. A future transfer claim requires a
conditioned lesson, independently verified application on another task and
appropriate controls. Private qualification grades remain outside learning.

## Source-only browser picker fix

A browser page sharing the desktop server opened a native picker and waited.
Warehouse registration used the native command interface as documented operator
setup; paid submission still used the frontend composer. The source fix chooses
native dialogs from the requesting page's `pywebview` bridge, otherwise opening
the typed-path modal. Twenty-two UI recovery tests plus real isolated browser
Add project, Enter selection, new-session intent and Escape cancellation pass.
The active dev11 package is unchanged; do not claim that fix is installed.

See [creative editors](creative-editors.md), [known issues](known-issues.md),
[Unreleased](unreleased.md) and [AGENTS.md](../AGENTS.md).
