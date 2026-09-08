# Navigation, drafts, and project-memory validation

## User-visible changes

Project selection now publishes the new workspace before model preparation completes. A separate preparation notice explains that saved work and drafting remain available. Runtime construction remains serialized so a model cannot be attached to the wrong workspace.

Unsent text is saved per project and session in Resonant's local state directory, outside the project repository. Drafts survive changes to the desktop app's launch port. Switching conversations restores the appropriate text; sending clears the saved draft. Late reads cannot overwrite fresh typing, another session, or a sent message. An unavailable connection leaves the message in the composer. Image attachments are not persisted as drafts.

The sidebar remembers separate desktop and compact-window preferences on disk. Collapsed content is inert and hidden from assistive technology; the toggle announces expanded state. Collapsing while focus is inside returns focus to the visible toggle.

Project notes now offer build/test commands, project conventions, and recurring fixes alongside existing note types. Retrieval includes at most six relevant, non-stale notes within a 2,400-character budget. Constraints take priority. Build/test questions can retrieve command notes even when their text is only a command. File fingerprints indicate freshness, not successful execution; model assertions remain labeled as assertions.

## Packaged startup measurements

Nine isolated launches of the clean Windows bundle: three runs per scenario. Each launch used a different local port and an isolated home directory. The responding provider was a local Ollama-compatible protocol fixture; no model inference was performed. Both configured LAN model endpoints and the local model endpoints were unreachable during validation, so live model loading and generation remain unmeasured.

Medians in milliseconds:

| Scenario | Process launch to HTTP ready | WebSocket connection to catalog | WebSocket connection to completed initialization | Session request to visible history page |
| --- | ---: | ---: | ---: | ---: |
| Offline provider | 543 | 4 | 2,302 | 1 |
| Responding provider fixture | 539 | 4 | 2,333 | 1 |
| Responding fixture, 10,000-event history | 517 | 4 | 2,342 | 67 |

Large-history responses included 240 display events rather than the whole conversation. Timings measure server responses, not browser paint, GPU loading, or time to first generated token. Three runs per scenario are a smoke benchmark rather than a statistical performance guarantee.

## Verification

- Full automated suite: 3,140 Python tests passed, two skipped. Four frontend draft-recovery tests passed and are included in the CI workflow. Python lint and JavaScript syntax checks passed.
- Browser checks: two independent session drafts, restoration after reload, sidebar persistence, collapsed accessibility state, compact-window operation, and project-note editing.
- Deterministic frontend checks: delayed reads across session switches, preserving fresh typing, clearing sent drafts, and restoring each session's own draft.
- Backend checks: draft isolation and persistence across launch origins, cross-origin write rejection, HTTP draft saving while model preparation is blocked, immediate project selection, and selective bounded memory recall.
- Clean Windows bundle: 55.5 MiB, 256 files. Packaged JavaScript matched the source hash. Bundled HTTP/WebSocket startup, skill installation, and clean-log smoke checks passed.

No broad system-prompt changes were made. Compare model task outcomes before changing the general prompt or promoting model assertions into verified project guidance. This work has not been published as a release.
