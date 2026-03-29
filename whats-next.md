# Handoff Document — Resonant Command Center

<original_task>
Build a "Command Center" feature for the Resonant GUI — a third top-level mode tab (Code / Chat / Command) that serves as a project-level orchestration hub for coordinating multiple AI agents working in parallel. The user should be able to:
1. Create projects with high-level strategies
2. Have an AI coordinator break strategy into tasks and spawn worker agents
3. Chat interactively with the coordinator to give direction
4. Monitor agent progress, view results, and preview output
5. Launch new initiatives within existing projects

This was preceded by fixing multiple GUI bugs found during a UI audit.
</original_task>

<work_completed>
## GUI Bug Fixes (Commit 023642b)
- Fixed session messages not rendering (switchView condition, inputBar display inconsistency)
- Fixed settings sections not expanding on click (mousedown preventDefault)
- Fixed harness popover opening unexpectedly (stopPropagation)
- Added Escape key to close harness and RESONANT.md popovers
- Fixed connection status flickering ("Reconnecting..." instead of instant "Disconnected")
- Added RESONANT.md content popover with editor and save

## Command Center — Full Implementation (15 commits, 6316 lines added across 25 files)

### New Files Created:
- `resonant_client/gui/command_projects.py` (146 lines) — CommandProject dataclass + CommandProjectStore with JSON persistence to ~/.resonant/command_projects/
- `resonant_client/gui/command_tasks.py` (140 lines) — CommandTask dataclass + CommandTaskStore with JSON persistence to ~/.resonant/command_tasks/
- `resonant_client/gui/command_coordinator.py` (401 lines) — CoordinatorToolHandler with tools: update_plan, spawn_worker, check_workers, post_update, complete_project. Also has COORDINATOR_SYSTEM_PROMPT and build_coordinator_prompt()
- `resonant_client/network_defaults.py` (88 lines) — Network settings for remote engine

### Modified Files:
- `resonant_client/gui/templates/index.html` (+41 lines) — Added "Command" mode tab button, command-center container with sidebar + main layout
- `resonant_client/gui/static/app.js` (+1007 lines) — Complete Command Center frontend: mode switching, project sidebar, dashboard with 5 sub-tabs (Chat/Plan/Agents/Activity/Results), coordinator chat with streaming, file browser, code viewer, Preview in Browser, fleet rendering, agent cards
- `resonant_client/gui/static/styles.css` (+739 lines) — All Command Center styling
- `resonant_client/gui/app.py` (+3401 lines) — Backend WebSocket commands for projects, chat, initiatives, fleet, tasks, feed, monitoring, preview file serving
- `resonant_client/gui/task_runner.py` (+10 lines) — Added on_event callback to BackgroundTask

### Test Project:
- `D:/Repos/battleship-2d/` — Complete playable Battleship 2D game (index.html 1.7KB, styles.css 9.2KB, game.js 21.8KB)
- Game enhanced by coordinator (Opus 4.6) with Web Audio API sound effects, CSS animations, responsive layout, and bug fixes during interactive chat session

### Testing Results (Chrome MCP verified):
- Command tab layout: PASS
- Project dashboard with all 5 sub-tabs: PASS
- Results tab (file list, code viewer, Preview in Browser, Refresh): PASS
- Mode switching (Code/Chat/Command): PASS
- Coordinator Chat first message (analysis): PASS — Opus reviewed files with line references
- Coordinator Chat follow-up (tool use): HUNG on "Thinking..." for 60+ seconds
- Background agents (initiatives): FAILED — agents don't use tools, complete in 1 step with text only
- Battleship game fully playable: PASS
</work_completed>

<work_remaining>
## Priority 1: Fix Coordinator Chat Tool Execution

The coordinator chat works for analysis but may hang when tool use is required on follow-up messages.

**Investigation steps:**
1. Check `app.py` `command_project_chat` handler (~line 5575-5660) — verify session.run() handles follow-up messages correctly
2. Add logging to capture event types during chat execution
3. Verify `auto_approve=True` for coordinator sessions
4. Test with different backends (codex, openai) to isolate if it's a claude-code backend issue
5. Check if the session is waiting for permission approval in background thread

## Priority 2: Add Streaming Text to Chat

Chat shows "Thinking..." instead of incremental text. The `command_project_chat_delta` event handler exists in frontend but backend may not be emitting text.delta events properly.

**Files:** app.js ~line 5150-5200 (frontend handler), app.py _push_chat_event (backend emitter)

## Priority 3: Fix Background Agent File Writing

Background agents spawned via TaskRunner complete in 1 step without using tools.

**Key insight:** Interactive coordinator chat CAN write files (Opus modified Battleship game successfully). The issue is ONLY with BackgroundTask agents. Investigate difference between interactive session.run() and background _run_task().

**Files:** task_runner.py lines 160-180 (_run_task event loop), app.py make_background_session

## Priority 4: Wire CoordinatorToolHandler into Sessions

`command_coordinator.py` has full tool implementations but they're NOT injected into coordinator sessions. Need to modify `build_session()` or use Session's custom tools parameter.

## Priority 5: Polish
- Persist chat messages across tab switches
- Add Stop button for running chat responses
- Auto-scroll activity feed
- Update project status when new initiative launches
</work_remaining>

<attempted_approaches>
## Agent Execution (All Failed for Background Agents):
1. Basic prompt → agent responded text only, no tools
2. Strengthened "CRITICAL: Act autonomously" prompt → codex agent still asked questions
3. Tried resonant, codex, claude-code backends → all failed to write files in background mode
4. Direct file writing (workaround) → confirmed Preview/Results UI works

## Coordinator Chat (Partially Working):
- First message (analysis) → WORKS perfectly with tool use (read files, identify bugs)
- Second message (requesting edits) → HUNG on "Thinking..." for 60+ seconds
- Interactive chat DID successfully write files (Opus enhanced Battleship game)
- This proves tool execution works in interactive context but may timeout on complex operations

## Server Launch:
- Background processes from sandbox exit immediately (native GUI)
- `nohup` approach works: `nohup python -m resonant_client gui --port 5555 > /dev/null 2>&1 &`
- Must use 127.0.0.1 not localhost for Chrome
</attempted_approaches>

<critical_context>
## Environment:
- Windows PC, Python 3.13, D:\Repos\resonant-client
- Backends: openai (gpt-4o), claude-code (sonnet/opus/haiku), codex (gpt-5.4), resonant
- Resonant API: http://10.0.0.133:8000, Remote WS: ws://10.0.0.133:8765
- User wants Claude Max / Codex subscriptions, not direct API keys
- Default backend: resonant

## Architecture:
- Command Center is a mode tab, not sidebar view
- Projects are primary work unit with coordinator agent
- WebSocket-only communication
- Project data in ~/.resonant/command_projects/
- Preview via /preview/<project_id>/<path> HTTP route
- Main sidebar hidden in Command mode

## KEY DISCOVERY:
Interactive coordinator chat CAN write files (Session + tool execution works). Background agents via TaskRunner CANNOT. This is the crucial distinction — the Session infrastructure works fine, the issue is in how TaskRunner._run_task() handles the session differently.

## CoordinatorToolHandler Status:
FULLY BUILT in command_coordinator.py but NOT INTEGRATED into sessions. Tools (spawn_worker, update_plan, etc.) need to be injected into the Session's tool list for the coordinator to actually orchestrate workers.

## Git: 15 commits ahead of origin/main on `main` branch. All committed, nothing uncommitted.
</critical_context>

<current_state>
## Feature Status:
| Feature | Status |
|---------|--------|
| Command mode tab + switching | COMPLETE |
| Project sidebar + CRUD | COMPLETE |
| Project dashboard (5 sub-tabs) | COMPLETE |
| Coordinator Chat (interactive) | MOSTLY WORKING (first msg OK, follow-up may hang) |
| Results tab (files, viewer, preview) | COMPLETE |
| Model selector | COMPLETE |
| Background agent execution | BROKEN (no tool use) |
| Coordinator tool integration | NOT WIRED (code exists, not injected) |
| Streaming text in chat | PARTIAL |

## Server: Start with `python -m resonant_client gui --port 5555` or `nohup` variant. Access at http://127.0.0.1:5555

## Battleship game: Modified by coordinator with sound effects + animations. Files on disk but uncommitted in battleship-2d repo.

## Next action: Debug why coordinator chat hangs on tool-use follow-up messages, then wire CoordinatorToolHandler into sessions for multi-agent orchestration.
</current_state>
