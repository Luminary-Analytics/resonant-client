# Resonant Client — Roadmap

## Overview

The client was refocused (April 2026) to a single agentic-coding experience powered by `deepseek-v4-flash:cloud` on Ollama running on the Mac Studio at `10.0.0.133`. Chat / Workspaces / Automations / Background-agents tabs were removed.

This document tracks **what shipped in the refocus** and **what's next**. For the as-built architecture, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Shipped — refocus to deepseek-v4-flash agentic coder

### Phase 1: kill chat / fleet / automations / background
- Removed Ask (chat) mode, Workspaces / Command Center / Fleet, Automations / Scheduler, Background agents / Dispatch.
- Deleted modules: `gui/scheduler.py`, `gui/command_coordinator.py`, `gui/command_projects.py`, `gui/command_tasks.py`, `gui/org_chart.py`, `gui/task_runner.py`, `engine/worktree.py`.
- Stripped `gui/sessions.py` of `chat_group` / `ask_workspace_path` and related methods.
- Stripped `gui/app.py` of all `command_*`, `dispatch_*`, `schedule_*`, `chat_group_*` WebSocket handlers and the orphan `_execute_coordinator_commands` / `_on_task_complete` / `_push_agent_event` methods.
- Stripped `gui/static/app.js` of `setSessionMode`, `_isAskSurface`, `initCommandCenter`, `requestCommandFleet`, `renderDispatchList`, `renderScheduleList`, `showDispatchDialog`, `showScheduleDialog`, chat-welcome handlers, project-filter dropdown, chat-group sidebar.
- Stripped `gui/templates/index.html` of mode tabs (Agent / Ask / Workspaces), sidebar nav buttons (Automations / Background), `#chat-welcome-screen`, `#schedule-view`, `#dispatch-view`, `#command-center`, `#sidebar-project-filter`.
- Stripped `ask_mode` parameter from `engine/session.py` (`Session.__init__`, `get_system_instructions`).
- Tests: `tests/test_ask_sessions.py` deleted; `tests/test_gui_hardening.py` cleaned of `TaskRunner`/`Scheduler` tests; `pytest` reports **514 passed**.

### Phase 2: Ollama-first defaults + deepseek-v4-flash
- Added `deepseek-v4-flash:cloud` (and `deepseek-v4`) to `OllamaBackend._KNOWN_TOOL_SUPPORT` and listed `deepseek-v4-flash:cloud` first in `OllamaBackend.CLOUD_MODELS`.
- Flipped `select_harness_backend` priority across planner / generator / evaluator to put `ollama` and `lmstudio` ahead of cloud backends.
- `select_harness_backend` prefers `deepseek-v4-flash:cloud` when Ollama is the chosen backend.
- `network_defaults.get_default_backend()` defaults to `"ollama"`; `get_default_model()` defaults to `"deepseek-v4-flash:cloud"`.

### Phase 3: harness polish
- `RESONANT_OLLAMA_*` defaults tuned for Mac Studio + 284B MoE: `num_ctx=32768`, `num_batch=1024`, `num_gpu=99`, `keep_alive=120m`, `http_timeout=300s`, `read_timeout=240s`.
- System prompt sharpened with rules for `batch` (parallel reads), `task` (isolated sub-investigations), and "think before acting".
- Smoke-tested via the preview server: UI renders cleanly, no console errors, only Agent + Settings nav remain.

---

## Active backlog

### Hygiene
- [x] Run full test suite green after refocus (514 passing).
- [ ] Strip dead CSS rules for removed features (chat-welcome, chat-group, dispatch-, schedule-, command-center, cmd-, fleet-, sidebar-project-filter, project-filter, pf-, body.chat-mode, workers-done-banner, agent-card-fleet) — ~107 selectors; bloat only, not load-bearing.
- [ ] Remove the unused `current_session_mode` payload field from `get_init_data` once we're confident no downstream consumes it.
- [ ] Delete the `currentSessionMode` field from the `ResonantApp` JS state if it ends up unread after the CSS pass.

### Verification
- [ ] End-to-end test against `deepseek-v4-flash:cloud` on the Mac Studio: tool round-trip with `glob` + `file_read`, then a `file_edit`, then `bash` (`pytest`).
- [ ] Verify the `batch` tool fan-out actually parallelizes when deepseek calls it (not just sequential).
- [ ] Check whether deepseek's "thinking" tokens stream cleanly through the existing `text.delta` events or need a separate channel.

### Documentation
- [x] Rewrite [ARCHITECTURE.md](ARCHITECTURE.md) for the post-refocus scope.
- [x] Rewrite this file (PLAN.md) for the post-refocus scope.
- [ ] Update [README.md](README.md) screenshots if they show the deleted tabs.

---

## Roadmap (proposed)

Concrete additions to discuss before implementation. Grouped by theme.

### Computer-use upgrades
1. **Window-targeted screenshots / clicks** — `computer_window` already lists windows; add `target_window=` to `computer_screenshot` / `computer_click` so coordinates resolve relative to a window's client rect, not the desktop. Robust against multi-monitor and DPI changes.
2. **Accessibility-tree targeting** — on Windows, query UI Automation; on macOS, AXUIElement. Replace pixel-coord clicks with semantic targets ("button: 'Save'") wherever possible. Falls back to coords. Big reliability win.
3. **OCR fallback** — when the model can't see a screenshot well or wants text from a region, run Tesseract on the captured bitmap and return text + bounding boxes. Adds zero cost when not invoked.
4. **`computer_wait_for_change`** — poll the screen (or a region) until pixels change, with a timeout. Lets the agent wait for "loading" → "loaded" without a fixed sleep.
5. **App launcher** — `computer_launch_app(name)` that finds and starts an app via Start menu / Spotlight / `xdg-open`. Removes the awkward `bash("start ...")` workaround.
6. **Clipboard tools** — `clipboard_read` / `clipboard_write` (text + image). Lets the agent stash content across tool calls and across processes.
7. **Process tools** — `process_list` / `process_kill_by_name` for "is the dev server running?" workflows. Bounded; not a free-for-all.
8. **Recording mode** — short MP4 capture of the agent's actions for debugging or sharing. Off by default; toggled per session.

### Codebase intelligence
9. **Auto-lint feedback loop** — after `file_edit` / `file_write`, run the configured linter (eslint/ruff) on the changed files, feed errors straight back as a follow-up message. Closes a manual round-trip the agent currently has to do.
10. **Auto-test on edit (opt-in)** — same pattern: configurable test command, runs only on changed module, surfaces failures inline.
11. **First-class git tools** — `git_diff`, `git_commit`, `git_branch_create`, `git_status` as proper tools instead of bash. Cleaner UI rendering, safer arg parsing, no shell quoting bugs.
12. **Persistent REPL tool** — `repl_python` / `repl_node` that holds a long-lived process, so the agent can iterate on a snippet without `bash` cold-start every time.

### Session ergonomics
13. **Conversation forking** — branch from any past message, run a new turn in parallel without losing the original thread.
14. **Session replay** — scrubber that replays the recorded `display_events` over time. Already stored; just needs UI.
15. **Voice input** — push-to-talk in the input bar via Web Speech API (browser mode) or whisper.cpp (desktop). Quick prompts without typing.
16. **Inline diff in chat** — the diff-review dialog is modal; render the diff inline in the message stream instead, with accept/reject buttons.

### deepseek-v4-flash-specific
17. **Thinking-mode toggle** — surface deepseek's "no thinking / thinking / max thinking" modes as a per-session control (or even per-message). Today the model picks; explicit control would help speed/quality tradeoffs.
18. **1M-context profile** — a settings preset that bumps `num_ctx` to 131072+ and `num_batch` to 2048 for big-repo sessions, with a one-click switch in Settings.
19. **MoE expert utilization telemetry** — Ollama exposes which experts activated; surface it in the harness state badge so we can spot under-utilization.

These are ideas, not commitments. Pick what's worth building; happy to scope each one.
