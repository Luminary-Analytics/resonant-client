# GUI Hardening Pass

This document captures the behavior-preserving hardening work that closed the recent GUI and background-runtime review findings without changing the product surface area.

## Goals

- Keep existing chat, GUI, dispatch, scheduling, and session-replay features intact.
- Fix correctness bugs around project context, backend reconstruction, cancellation, settings application, and replay state.
- Improve internal consistency so chat, restored sessions, dispatch jobs, and scheduled jobs all use the same runtime wiring.
- Add regression coverage for the new invariants.

## What Changed

### Shared Runtime Construction

The GUI used to build sessions in several different ways. That caused drift between:

- normal chat sessions
- restored sessions
- dispatch/background jobs
- scheduled jobs

The GUI now uses a shared runtime path in `resonant_client/gui/app.py` backed by `resonant_client/gui/runtime.py`.

Key pieces:

- `BackendSpec` serializes the backend configuration needed to recreate a real backend later.
- `build_session(...)` applies the same hooks, MCP tools, Engram integration, project instructions, RAG index, and permission defaults everywhere.
- `apply_project_context(...)` centralizes cwd switching, project-manager updates, project instructions, Engram namespace updates, and codebase-index rebuilding.

### Backend Reconstruction For Dispatch And Scheduler

Dispatch and scheduled jobs now recreate backends from `BackendSpec` instead of the old `create_backend(bt, model=m)` shortcut.

That preserves:

- Resonant URL
- Claude/OpenAI key source
- LM Studio base URL
- CLI backend cwd
- Claude Code permission mode

The task runner and scheduler now submit sessions built with the same wiring as interactive chat.

## Settings Behavior

### Safe Secret Editing

API-key settings are now rendered safely:

- password fields render empty
- the UI shows whether a secret is already stored
- blank blur leaves the stored secret unchanged
- clearing a secret is explicit

Server-side handling also preserves existing secrets unless the request explicitly clears them.

### Immediate Settings Application

Settings changes now apply to runtime state instead of only writing JSON:

- backend availability is re-detected
- hooks are reloaded
- Engram settings are reloaded
- current session wiring is refreshed
- saved default permission mode is applied to new/current sessions where safe

## Cancellation Model

Cancellation is now cooperative and real across the main runtime:

- `Session` has an internal cancellation event
- backend streaming paths accept cancellation
- subprocess-backed tools terminate on cancel
- background tasks call `session.cancel()`
- server mode cancel is implemented instead of stubbed

The goal is not force-kill semantics for every possible tool, but a consistent cooperative stop path that works for the core session loop and the long-running subprocess paths.

## Replay And Terminal State

The GUI terminal bar now only tracks live engine-managed terminal work.

Fixes included:

- no live terminal tracking for CLI backends that do not emit matching start/finish lifecycle events
- replay no longer repopulates active terminal state
- interrupted/cancelled sessions do not resurrect phantom running terminals
- the per-row terminal action is labeled to match its real session-wide cancel behavior

### Terminal Bar Code Review Fixes

Post-hardening review identified and resolved:

- **Event listener leak** — per-button click handlers on dynamically created stop buttons replaced with event delegation on the list container
- **Missing error cleanup** — `clearTerminals()` now called on fatal errors and cancellation, not just session end
- **Race guard** — `trackTerminalEnd()` guards against duplicate `tool.result` events for the same `call_id`
- **DOM safety** — `el.parentNode` check before removing entries in delayed cleanup callbacks

## MCP Routing

MCP tool dispatch now resolves the longest matching server-name prefix after `mcp_`.

This fixes server names containing underscores, for example:

- `mcp_my_server_tool`

which now correctly routes to server `my_server` and tool `tool`.

## Engram Scoping

Engram is now treated as project-scoped in the GUI runtime:

- the namespace is derived from the active project
- project changes rebuild the current Engram view
- sessions and codebase indexing use the project-scoped Engram instance

## Files Touched

Primary implementation files:

- `resonant_client/gui/app.py`
- `resonant_client/gui/runtime.py`
- `resonant_client/gui/task_runner.py`
- `resonant_client/gui/scheduler.py`
- `resonant_client/gui/settings.py`
- `resonant_client/gui/static/app.js`
- `resonant_client/engine/server.py`
- `resonant_client/engine/mcp.py`
- `resonant_client/engine/tools.py`
- `resonant_client/engine/memory.py`
- `resonant_client/backends.py`

Regression tests:

- `tests/test_gui_hardening.py`

## Verification

The hardening pass was verified with:

- `python -m pytest -q`
- `python run_tests.py`
- `python -m py_compile ...`
- `node --check resonant_client/gui/static/app.js`

Current automated coverage added by this pass includes:

- backend-spec reconstruction
- underscore-safe MCP routing
- background-task cancellation
- scheduler task submission wiring
- secret-preserving settings updates
- permission-mode restoration
- cross-project GUI context/session setup
