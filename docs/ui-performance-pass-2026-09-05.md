# Resonant navigation and startup evaluation

Historical evaluation of the navigation work later released in v0.16.0. Its
flat-list row limits and scope labels describe that measured build. v0.17.0
uses sessions grouped under projects; see the [desktop guide](desktop-workflow.md)
and [Unreleased](unreleased.md) for the later compact layout. The measurements
below have not been rerun for that layout.

## Changes

- Named project rows with session counts, a visible add button, and keyboard-accessible action menus replace the initials-only rail.
- Sidebar search matches projects, paths, and session titles. Session scope offers this project, all projects, and pinned sessions. The command palette also searches projects and sessions.
- Session lists render 40 rows initially, with a Show more button. Unchanged lists keep their DOM, scroll position, and focus.
- Saved navigation is delivered before provider discovery. Discovery runs in the background; runtime construction remains in the command handler. Empty discovery results use the existing probe cache.
- Session file enumeration uses directory-entry metadata, reducing Windows filesystem calls. Extension lists load when the status panel opens.
- The new teal wave mark ships as SVG, PNG, and a Windows icon. Current product copy says Resonant. Package/repository identifiers and the legacy updater settings key remain compatible.

## Controlled timing comparison

Local Windows fixture: 10 project folders, 120 saved sessions each (1,200 total), with provider discovery deliberately delayed by four seconds. No paid inference or user projects were used.

| Observation | Before | After |
| --- | ---: | ---: |
| WebSocket connection to full saved-project catalog | 4,172 ms | 31 ms |
| Initial application state after connection | 4,172 ms | 67 ms |
| Initial rendered session rows in all-project scope | Up to 1,200 | 40 |

These are single-run diagnostic timings, not statistical benchmarks. They measure navigation availability after the server accepts a connection, not process launch, model loading, or generation speed. Session history hydration and model requests were not benchmarked in this pass.

## Browser checks

The actual local application was exercised in Chrome against the fixture:

- Search, project switching, current-project selection, and active-project visibility.
- All-project and pinned scopes; 10 pinned sessions displayed across the fixture.
- Show more grew the rendered list from 40 to 80 rows.
- Command palette project search and keyboard selection.
- Project action menu, Escape, and focus restoration.
- Normal desktop, 900 × 650, and 800 × 600 layouts. At 800px the sidebar becomes a collapsible drawer; opening and closing it worked. No horizontal document overflow at 900px.
- No browser warning/error logs during these checks.

## Build validation

The full automated suite passed: 3,136 tests passed, two skipped. Python lint, JavaScript syntax, and whitespace checks also passed.

The clean Windows bundle completed at 55.5 MiB / 256 files. Its JavaScript and SVG matched the source hashes. An isolated bundled-executable smoke check verified HTTP 200, a WebSocket connection, bundled skill installation, clean startup logs, and process shutdown.

Regression coverage includes navigating while discovery is blocked, caching empty discovery results, and immediately reflecting created, renamed, and deleted sessions in the catalog. This pass has not been published as a release.

## Follow-up measurements

Measure real cold-start distributions on representative local and remote model setups before setting a startup SLA. If projects with tens of thousands of sessions remain slow, move catalog paging/search to the server. Measure history replay and time to first generated token separately so navigation improvements do not mask inference delays.
