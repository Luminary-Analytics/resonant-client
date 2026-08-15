# DeepSeek Harness adoption — 2026-08-14

This change adopts the parts of DeepSeek Harness that close concrete Resonant
reliability and UX gaps without replacing Resonant's existing runtime.

## Shipped foundation

### One durable session authority

Each saved session now has two files:

- `<session-id>.json` — small sidebar/runtime metadata.
- `<session-id>.events.jsonl` — versioned, append-only conversation and display
  records with contiguous sequence numbers.

The existing `conversation_history` and `display_events` interfaces remain as
projections so backends, checkpointing, and the renderer migrate without a
flag day. Legacy embedded-array sessions migrate automatically on first load.
Completed display events are fsynced as they stream; an interrupted final JSONL
record is ignored and repaired before the next append. Checkpoint rewinds use
an explicit clear record followed by independently sequenced events.

### Tail-first, bounded history

Opening or restoring a session loads the most recent task-aligned page instead
of replaying the complete transcript. The user can request earlier activity in
240-event pages. At most 1,200 historical events are mounted at once; after
crossing that window the UI offers a one-click return to the authoritative
latest page.

Whole-session facts such as turn/tool counts, changed files, and last outcome
come from small ledger projections and do not require mounting the transcript.

### Tool-defined presentation

Every native tool call now carries a provider-independent `presentation`
object:

```json
{
  "kind": "edit",
  "view": "diff",
  "label": "Edit file",
  "locations": ["src/app.py"],
  "interactive": false
}
```

Core, browser, desktop, Git, agent, and location-bearing MCP calls map into the
same small vocabulary. Completion summaries use this intent and final turn
evidence to identify deliverables. Clicking a changed file opens it through a
server-side command that requires the target to exist inside the active
project.

## Deliberately staged next

DeepSeek Harness's unified background-job service and Code Mode are valuable,
but they are execution-boundary changes rather than presentation changes. They
should build on this ledger after it has been exercised in real long-running
sessions:

1. Introduce one owner-scoped job registry for shell processes and workers,
   with incremental output cursors, bounded retention, wait, and kill.
2. Move current long-running services onto it one producer at a time.
3. Prototype Code Mode behind an experimental setting with a fresh isolated
   runtime per call, a strict output budget, logged sub-dispatches, and no
   direct access to unregistered capabilities.
4. Compare task completion, tool-call count, latency, and failure recovery
   against native mode before making it a normal model surface.

This sequence avoids introducing an opaque execution layer before Resonant can
durably explain and replay everything that layer did.
