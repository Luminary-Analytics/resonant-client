# PLAN — Session Ergonomics

> Status: ✅ Shipped · Tasks: 4 / 4 · Last verified: 2026-04-26

## Objective

Make the Agent UI feel like a serious workbench, not just a chat. Pre-cluster: every conversation was linear, diffs interrupted with a modal, the agent's actions couldn't be replayed, and you couldn't talk to it. After this cluster: branch from any past message, see edits inline with accept/reject, scrub through what the agent did, and use voice input.

## Context

Files a future executor (or anyone extending this cluster) must read first:

- [ARCHITECTURE.md](ARCHITECTURE.md) — `gui/sessions.py`, `gui/static/app.js` `ResonantApp` class, the `display_events` array on `SessionRecord`
- [resonant_client/gui/sessions.py](resonant_client/gui/sessions.py) — `SessionRecord`, `ProjectManager.fork_session()`
- [resonant_client/gui/app.py](resonant_client/gui/app.py) — `_run_session_streaming`, WebSocket `message` / `switch_session` / `fork_session` / `get_session_replay_events` handlers
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — `handleEvent` switch, `replayDisplayEvents`, `_renderInlineDiffPermission`, `_setupVoiceInput`, `_enterReplayMode`
- [resonant_client/engine/diff_review.py](resonant_client/engine/diff_review.py) — `generate_review`, `DiffReview`, `DiffHunk` (structured diffs the inline renderer consumes)

## Prior art (do NOT reinvent)

| Feature | Where it lives now |
|---|---|
| `SessionRecord.conversation_history` (backend-format messages) | [gui/sessions.py](resonant_client/gui/sessions.py) |
| `SessionRecord.display_events` (event stream for UI replay) | [gui/sessions.py](resonant_client/gui/sessions.py) |
| `replayDisplayEvents(events)` on session load | [gui/static/app.js](resonant_client/gui/static/app.js) |
| `generate_review` produces structured `DiffReview` | [engine/diff_review.py](resonant_client/engine/diff_review.py) |
| `#permission-dialog` modal still handles bash + risky non-edit tools | [gui/templates/index.html](resonant_client/gui/templates/index.html) |

The `display_events` array is the key enabler — every event the chat showed is recorded, so replay (Task 2.3) is purely a UI rendering problem, not a state-recording one.

## Tasks

All four tasks below are ✅ shipped. Each line points to the implementing files and a verify command that passes against the repo today.

---

### Task 2.1 — Conversation forking ("Fork from here") ✅ Shipped

**Lives in:**
- [resonant_client/gui/sessions.py](resonant_client/gui/sessions.py) — `ProjectManager.fork_session(source_id, fork_at_message_index) -> SessionRecord`
- [resonant_client/gui/app.py:6004](resonant_client/gui/app.py:6004) — WebSocket handler `fork_session` (rebuilds backend, restores conversation_history, sends `session_forked` then `session_loaded`)
- [resonant_client/gui/static/app.js:4278](resonant_client/gui/static/app.js:4278) — `data-action="fork"` button on `.msg-user` hover-actions; `_forkFromUserMessage(el)` computes the user-message index and sends the WebSocket command
- [tests/test_session_ergonomics.py](tests/test_session_ergonomics.py) — covers fork-from-first / fork-from-last / fork-with-subagent slicing

**Verify:**
```bash
pytest tests/test_session_ergonomics.py -k fork -v
```
Manual:
1. Have a session with 3 user messages.
2. Hover the 2nd user message → click the **↪ Fork** action.
3. New session opens with the first 2 user/assistant exchanges intact; original is untouched.

**Done when (✅):** Fork creates a new session with conversation up to and including the picked message, sets it as current, and the original session is untouched. Test covers slicing edge cases including subagent-nested events.

---

### Task 2.2 — Inline diff in chat (replace permission modal for `file_edit` / `file_write`) ✅ Shipped

**Lives in:**
- [resonant_client/gui/static/app.js:4187](resonant_client/gui/static/app.js:4187) — `_renderInlineDiffPermission(toolName, args, review)` called from the `tool.permission` handler at line 4098 when `tool_name in {file_edit, file_write}`
- [resonant_client/gui/static/styles.css](resonant_client/gui/static/styles.css) — `.inline-diff`, `.inline-diff-header`, `.inline-diff-body`, `.inline-diff-actions`, `.inline-diff-summary` styles
- [resonant_client/gui/templates/index.html](resonant_client/gui/templates/index.html) — `#permission-dialog` retained for `bash` and other risky non-edit tools (no template change needed; the modal still drives those)
- [resonant_client/gui/app.py](resonant_client/gui/app.py) — no change (same `tool.permission` event drives both rendering paths)

**Verify:**
```bash
# UI-only, no automated test. Manual:
python -m resonant_client.gui.server --port 8765 --browser
```
Trigger an agent edit, confirm the diff appears inline at the bottom of `#chat-messages` (not as a modal popup), with **Accept** / **Reject** buttons that drive the existing approve flow. After click, the block transitions to a compact summary `✓ Accepted: 3 changes in foo.py`.

**Done when (✅):** Editing files renders the diff inline, persists in `display_events` so it survives session reload, and accept/reject correctly drives the same permission flow as the modal did.

---

### Task 2.3 — Session replay scrubber ✅ Shipped

**Lives in:**
- [resonant_client/gui/static/app.js:4380](resonant_client/gui/static/app.js:4380) — `_enterReplayMode(events)`, `_toggleReplayPlay`, `_renderUpToEventIndex`
- [resonant_client/gui/app.py:5971](resonant_client/gui/app.py:5971) — WebSocket handler `get_session_replay_events`
- [resonant_client/gui/static/styles.css](resonant_client/gui/static/styles.css) — `.replay-scrubber` floating bar above the input
- [resonant_client/gui/templates/index.html](resonant_client/gui/templates/index.html) — scrubber element is created on demand by JS (no static markup needed)
- Replay entry point: a "▶ Replay" item on the session row's context menu

**Verify:**
Manual:
1. Pick a session with ≥ 5 events.
2. Right-click → **Replay**. The floating scrubber appears, chat is empty.
3. Drag the slider → events appear progressively.
4. Click **▶ Play** at 2× → events stream automatically.
5. Click **✕ Exit** → return to live chat.

**Done when (✅):** Replay correctly time-travels through `display_events`, play/pause/speed work, exit restores the live view without breaking subsequent agent runs.

---

### Task 2.4 — Voice input (push-to-talk) ✅ Shipped

**Lives in:**
- [resonant_client/gui/static/app.js:1954](resonant_client/gui/static/app.js:1954) — `_setupVoiceInput()` (Web Speech API primary, graceful fallback message in unsupported runtimes)
- [resonant_client/gui/templates/index.html](resonant_client/gui/templates/index.html) — `#mic-btn` in `.input-footer-left`
- [resonant_client/gui/static/styles.css](resonant_client/gui/static/styles.css) — `.mic-btn` idle / recording (red-pulse) / error states
- Desktop fallback (`whisper.cpp` shim) deliberately deferred — see "Future / nice-to-haves" below

**Verify:**
Manual in Chromium (or pywebview's WebView2, which exposes `webkitSpeechRecognition`):
1. Hold the mic button → speak *"list the files in the engine directory"*.
2. Release → text appears in `#user-input`.
3. Press Enter → agent runs `glob` or `bash ls`.

If the runtime doesn't expose SpeechRecognition, the mic button shows a tooltip: *"Voice input not supported (try a Chromium browser, or wire whisper.cpp on the desktop)"* — see [app.js:1970](resonant_client/gui/static/app.js:1970).

**Done when (✅):** Hold-to-talk transcription works end-to-end on Chromium / WebView2. Falls back to a clear tooltip message on unsupported runtimes.

---

## Overall verification

```bash
cd D:/Repos/resonant-client
python -m pytest tests/test_session_ergonomics.py -q
```

Manual smoke test:
1. Open the app, create a session, send 3 messages.
2. Hover middle user message → **Fork**. New session opens with first 2 messages.
3. In the new session, ask the agent to edit a file. Diff appears inline (not modal); accept it.
4. Right-click any session → **Replay**. Scrub through it; click Play.
5. Hold mic, dictate a prompt, release, send. Agent receives the transcript.

## Success criteria

- [x] `fork_session` test covers slicing edge cases (first message, last message, with subagent nesting).
- [x] Inline diff replaces modal for `file_edit`/`file_write` only; modal still appears for `bash`.
- [x] Replay correctly handles sessions with sub-agents nested in `display_events`.
- [x] Voice input works in the dev browser; desktop fallback shows a clear tooltip when unavailable.
- [x] No regression in `pytest`.

## Future / nice-to-haves (not yet built)

| Idea | Where it would go | Why it's not built yet |
|------|-------------------|-----------------------|
| Inline diff for **multi-file** edits (one block per file with a single accept/reject pair) | `gui/static/app.js:_renderInlineDiffPermission` | The agent currently emits one `tool.permission` per edit; would need batching at the engine layer first |
| Side-by-side diff view (split panes) toggle | `.inline-diff-body` | Current view is unified; split would require a renderer refactor |
| Replay export to `.json` for sharing | `engine/event_log.py` plus a `gui/app.py` handler | Sessions are already on disk; just needs an export button |
| `whisper.cpp` desktop fallback for voice input | NEW: `engine/transcribe.py` + `gui/app.py` `transcribe_audio` WS command | WebView2 SpeechRecognition is reliable enough today; would only matter on Linux desktop where Web Speech is missing |
| Branch-graph view of forked sessions (showing parent → child relationships) | sidebar tree rendering in `app.js` | Sessions don't currently store `parent_id`; would need a `SessionRecord` field |

## Output

When extending this cluster, append a status entry here:

> 2026-04-26 — All 4 tasks shipped. 7 tests pass. No deviations from this plan.
