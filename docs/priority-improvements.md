# Improvements from the end-to-end evaluation

This iteration implements the five priorities in `e2e-user-evaluation-2026-09-05.md`.
Restart Resonant to load the updated runtime and bundled procedures.

## Managed previews

Use `preview_start` with a program/argument array and an unused loopback HTTP URL:

```json
{"command":["python","-m","http.server","8000","--bind","127.0.0.1"],"url":"http://127.0.0.1:8000"}
```

The tool waits for readiness, returns a handle and recent logs, and keeps the
server alive across turns and project navigation. Use `preview_status` and
`preview_stop`, or **Previews** in the toolbar. Only the owning project can stop
a preview. Startup failure/cancellation cleans up the process. Normal shutdown
stops managed previews; Windows job handles also clean up their process trees
when the owning process exits. Ordinary shell commands retain their existing
short lifetime. Eight previews can run at once; returned logs are capped at 16 KiB.
Previews are local runtime resources, not restartable deployment records.

## Evidence for acceptance checks

Use `check_run` for acceptance commands with a description of the requirement:

```json
{"command":"python -m unittest discover -s tests","requirement":"CLI file/stdin behavior and malformed input","timeout":60}
```

Each turn records the command, requirement, actual result, call ID, timestamp,
and content hashes of its recorded changed files. The latest result of each
requirement/command pair wins. An unrelated success does not clear a failed check.
Later changes to the recorded files make an earlier passing result stale.
The finished card exposes the named checks and their statuses. Ordinary shell
commands, screenshots, attempted auto-feedback, and display-only CLI tool calls
do not count as acceptance evidence.

“Named checks passed” means exactly that. The runtime cannot establish that a
model chose adequate tests or covered every requirement. Hashes cover files
recorded as changed during that turn, not all external dependencies or services.
Browser interaction is still checked using actual input and reported separately;
a screenshot alone is never acceptance proof. Model-authored checklists remain
plans, not independent verification.

## Reasoning and progress

Kimi now offers **Low**, **High**, and **Max** effort, persisted per session and
applied when rebuilding/restoring the provider. **Provider default** resolves to
Max; thinking cannot be disabled. The default remains Max pending repeated
quality/latency measurements.

Buffered reasoning and tool argument generation emit throttled activity events
without exposing reasoning text or incomplete tool arguments. Cancellation no
longer generates a spurious empty-answer retry. Turn telemetry includes first
visible response, first semantic progress, first edit, preview readiness, and
first passed named check when those events occurred.

## Skills and project notes

Two bundled, unpinned procedures cover browser usability and transaction/API
acceptance. They include real keyboard input, focus restoration, narrow viewports,
refresh/restart persistence, recognizable business names, invalid-then-valid
requests, concurrent mutations, and Windows-compatible command recipes.

Retrieval uses bounded catalogs: 700 estimated tokens for library descriptions
and 500 for trusted pack descriptions. Full procedures load via `skill_view`.
Pack matching uses whole tokens and excludes generic request words. Equivalent
descriptions are deduplicated within library retrieval; project copies take
precedence over global pins. Scope, author, prior outcomes, match/pin reason and
prerequisites accompany suggestions. This is lexical retrieval, not a semantic
equivalence detector or a guarantee of relevance.

A project can override pin injection and suppress unwanted suggestions in
`.resonant/skill-policy.json`:

```json
{"include_global_pins":false,"suppressed_ids":["irrelevant-example"]}
```

Learned graph procedures prefer project scope when the caller supplies a project.
Existing project-scoped mission extraction and explicit promotion remain in
place. No personal skill files are deleted or automatically promoted by this
change. Legacy extraction callers without project context retain their existing
scope behavior.

**Project notes** provides add/edit/delete controls for facts, constraints,
decisions and procedures. Notes require a source description; optional relative
source files are fingerprinted. Changed or missing source files make notes stale
and exclude them from recall. Agent-created notes remain labeled **model
assertion**; user-authored notes say **user supplied**. A source hash establishes
freshness, not the truth of the claim. Notes without files rely on user review.
Storage is inspectable JSON at `.resonant/memory.json`, capped at 40 notes of
1,000 characters; prompt recall is capped at 2,400 characters.

Engram's fallback conversation excerpt is no longer silently persisted as
knowledge. Recalled external memories are bounded and labeled unverified.
`RESONANT_EVALUATION_MODE=1` disables Engram recall/storage and excludes personal
global skills, while allowing bundled references and the fixture's own project
state. Use a fresh project and state/home directory for each independent run.

## Navigation and validation

Adding a project with **Open** now activates it. Project switches clear the old
title. Session creation/completion refresh the cross-project sidebar immediately.
Finished cards use the authoritative normalized changed-file list rather than
accumulating duplicate paths.

The browser regression uses a deterministic Kimi transport fixture with real
engine/tool execution and an isolated home at `D:/Repos/resonant-priority-checks`.
It verifies low effort on the outgoing wire, a seeded failed check remaining
unverified after a successful environment command, preview interaction and
survival across project switching, project ownership, notes editing/persistence,
and immediate project activation. This is a runtime/UI regression, not a new
live-model coding benchmark. The earlier three live-build results remain the
baseline; repeated Kimi and reachable local-model comparisons are the next
evaluation step.

Validation completed: `python -m pytest -q --tb=short` — **3,133 passed,
2 skipped**, including 17 new regression cases. JavaScript syntax checks,
Python compilation, and whitespace checks passed. The browser also verified
explicit preview stopping, note deletion, Escape focus restoration, and focus
remaining inside the notes dialog after a mutation. The isolated test server
was stopped after validation.
