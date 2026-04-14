# Resonant Client — Architecture Guide

This document describes every major module, data flow, and extension point in `resonant-client`. It is written so that any LLM or developer can pick up the codebase and contribute effectively.

## Overview

Resonant Client is a **Cursor 3-style agentic coding IDE** that connects to multiple LLM backends (Ollama, Claude, OpenAI, LM Studio, Resonant Engine). It runs as a frameless desktop app (pywebview) or in a browser, providing:

- An agentic tool loop (LLM → tool → LLM → tool → ...)
- 25+ tools (file ops, bash, browser automation, desktop control)
- A Cursor 3-inspired GUI with project tree, command palette, preview panel
- Background agents, automations, and multi-project workspaces

```
┌──────────────────────────────────────────────────────────────┐
│                        GUI (browser/pywebview)                │
│  index.html + app.js + styles.css                             │
│  WebSocket ↕ JSON events                                      │
├──────────────────────────────────────────────────────────────┤
│                      gui/app.py (Starlette)                   │
│  State management, backend selection, session routing          │
├──────────────────────────────────────────────────────────────┤
│                     engine/session.py                          │
│  Agentic loop: stream → parse → execute tools → repeat        │
├──────────────┬───────────────┬────────────────────────────────┤
│  backends.py │  engine/      │  engine/browser.py             │
│  Ollama      │  tools.py     │  12 Playwright tools           │
│  Claude      │  bash, file_* │  navigate, click, type, read   │
│  OpenAI      │  glob, grep   │  screenshot, js, scroll, hover │
│  LM Studio   │  task (sub)   │  select, wait, back, tabs      │
│  Cloud models│               │                                │
└──────────────┴───────────────┴────────────────────────────────┘
```

## Module Reference

### `backends.py` — Backend Abstraction

Each backend implements: `stream()`, `health()`, `list_models()`, `classify()`.

| Class | Protocol | Notes |
|-------|----------|-------|
| `OllamaBackend` | HTTP `/api/chat` | Adaptive tool calling (native or text-mode), env-tunable options (`RESONANT_OLLAMA_*`), `CLOUD_MODELS` list auto-appended |
| `OpenAIBackend` | OpenAI SDK | Also used for LM Studio (`base_url` set), env-tunable read timeout |
| `ClaudeBackend` | Anthropic SDK | Native tool calling |
| `ClaudeCodeBackend` | CLI subprocess | Wraps `claude` CLI, `handles_tools=True` |
| `CodexBackend` | CLI subprocess | Wraps `codex` CLI |
| `ResonantBackend` | SSE `/v1/responses` | Cognitive engine with harness |
| `MLXBackend` | Local MLX adapter | Apple Silicon optimized |

**Key design rule for Ollama:** All requests to a given `OllamaBackend` instance must use identical `_ollama_options` (num_ctx, num_batch, num_gpu). If any option differs between requests, Ollama unloads and reloads the entire model (30-120s penalty). Options are set once at init from environment variables.

**Cloud models:** `OllamaBackend.CLOUD_MODELS` lists models available via Ollama's cloud routing (`:cloud` tag). These are appended to the model list even if not pulled locally. Current list: minimax-m2.7, minimax-m2.5, nemotron-3-super, kimi-k2.5, glm-5.1, glm-4.7-flash, deepseek-v3.2, qwen3.5, gemma4.

### `events.py` — Event Protocol

All communication between engine and clients uses typed events:

```python
class EngineEvent(str, Enum):
    SESSION_START, SESSION_END          # Session lifecycle
    STEP_START, STEP_END                # Agentic step boundaries
    TEXT_DELTA, TEXT_DONE                # Streaming text
    TOOL_CALL, TOOL_RESULT              # Tool execution
    TOOL_PERMISSION                     # Permission request
    PLAN_GENERATED, PLAN_APPROVED       # Plan mode
    SUBAGENT_START, SUBAGENT_END        # Sub-agent lifecycle
    TODOS_UPDATED                       # Markdown task list progress
    COMPRESSION                         # Context compressed
    STATUS, ERROR, CHOICES              # Metadata
```

The `TODOS_UPDATED` event is emitted when the model's text contains GitHub-style checkboxes (`- [ ]` / `- [x]`), parsed by `parse_markdown_todos()` in `session.py`.

### `engine/session.py` — Agentic Loop

The core loop:

1. Build system instructions (platform-aware, includes project instructions from RESONANT.md)
2. Stream from backend (`backend.stream()`)
3. Collect text tokens → yield `TEXT_DELTA` / `TEXT_DONE`
4. Parse tool calls (native API or text-mode XML)
5. Execute tools via `execute_tool()` in `tools.py`
6. Feed results back → next iteration
7. Detect doom loops (same tool+args repeated N times)
8. Handle plan mode, choices, sub-agents

**System prompt rule 8** nudges models to use markdown task lists for multi-step work, enabling the live todo strip in the GUI.

### `engine/tools.py` — Tool Definitions and Execution

25+ tools in `AGENT_TOOLS` list (OpenAI function-calling format):

| Category | Tools |
|----------|-------|
| **File ops** | `file_read`, `file_write`, `file_edit`, `glob`, `grep` |
| **Shell** | `bash` (non-interactive, timeout-guarded) |
| **Browser** | `browser_navigate`, `browser_click`, `browser_type`, `browser_read`, `browser_screenshot`, `browser_js`, `browser_scroll`, `browser_hover`, `browser_select`, `browser_wait`, `browser_back`, `browser_tabs` |
| **Desktop** | `computer_screenshot`, `computer_click`, `computer_type`, `computer_scroll`, `computer_key`, `computer_drag`, `computer_hover`, `computer_window` |
| **Agent** | `task` (sub-agent spawning) |
| **Batch** | `batch` (parallel tool execution) |

### `engine/browser.py` — Playwright Browser Automation

Singleton `BrowserManager` manages one Chromium instance. Two modes:

- **Launch:** `mgr.launch()` starts headed Chromium (auto-triggered on first `browser_navigate`)
- **Connect CDP:** `mgr.connect_cdp("http://localhost:9222")` attaches to existing Chrome

Click strategy: tries text match → link role → button role → CSS `a:has-text()` selector (cascading fallback).

### `engine/sandbox.py` — Permission Levels

Four modes: `ask` (approve everything), `auto-edit` (files OK, shell asks), `plan` (plan first), `bypass` (full auto). Tools are categorized:

- `READ_ONLY_TOOLS`: file_read, glob, grep, browser_read, browser_screenshot, browser_scroll, browser_hover, browser_wait, browser_back, browser_tabs
- `FILE_WRITE_TOOLS`: file_write, file_edit
- `SHELL_TOOLS`: bash
- Everything else: requires explicit permission in non-bypass modes

### `gui/server.py` — Application Launcher

Starts uvicorn in a background thread, then either:
- **pywebview:** Frameless native window with `easy_drag=True`, custom `_WindowAPI` (minimize, toggle_maximize, close), Windows taskbar icon via `WM_SETICON`
- **Browser fallback:** Prints URL, waits for Ctrl+C

The `_set_icon_on_shown` callback uses Win32 `LoadImageW` + `SendMessageW` to replace the Python icon with `resonant.ico` in the taskbar. `SetCurrentProcessExplicitAppUserModelID` ensures Windows treats it as a standalone app.

### `gui/app.py` — Web Application (~8000 lines)

Starlette ASGI app with:
- `/` — serves `index.html`
- `/static/*` — JS, CSS, icons
- `/ws` — WebSocket for all client-server communication

**`AppState`** (singleton) holds: backends, session, project context, settings, costs, scheduler, harness state.

Key methods:
- `detect_backends()` — parallel network checks for Ollama/LM Studio/Resonant Engine + API key checks for Claude/OpenAI. Appends `OllamaBackend.CLOUD_MODELS` to Ollama results.
- `create_backend(type, model, session_mode, session_role)` — instantiates backend + session
- `_run_session_streaming(ws, user_msg, ...)` — streams engine events to WebSocket
- `get_init_data()` — sends full state to frontend on connect

WebSocket commands: `message`, `switch_session`, `select_backend`, `update_settings`, `dispatch`, `schedule_create`, `rag_index`, `mcp_connect`, etc.

### `gui/static/app.js` — Frontend (~6800 lines)

`ResonantApp` class handles everything:

**Initialization:**
- `constructor()` — DOM refs, state init, `_restoreAppearance()`, `_bindMenuBar()`, WebSocket connect
- `bindEvents()` — mode tabs, sidebar nav, keyboard shortcuts, search, preview resize

**Event handling:**
- `handleEvent(event)` — routes `session.start`, `text.delta`, `text.done`, `tool.call`, `tool.result`, `step.start/end`, `session.end`, `todos.updated`, `subagent.start/end`, etc.
- `handleTodosUpdated(event)` — updates live todo progress strip
- `handleSessionEnd(event)` — renders run summary card, follow-up chips

**Sidebar:**
- `renderFilteredSessions()` → `_renderProjectTree(sessions)` — groups sessions by project path, renders collapsible tree with expand/collapse state
- `_createTreeSessionRow(session)` — creates agent-row with `role="button"`, `tabindex="0"`, click/keyboard handlers

**Command palette:**
- `openCommandPalette()` / `closeCommandPalette()` — Ctrl/Cmd+K overlay
- `_cmdPaletteCommands()` — list of available commands
- `_renderCommandPaletteResults(query)` — fuzzy filter + keyboard nav

**Settings:**
- `renderSettingsView()` — builds sections: General, Appearance, Local Backends, Network, API Keys, Cost Tracking, Memory, RAG, Hooks, MCP Servers
- `_applyAppearance(key, value)` — applies theme/density/font-size to DOM + localStorage
- `_restoreAppearance()` — hydrates on startup

**Menu bar:**
- `_bindMenuBar()` — wires File/Edit/View/Help actions + window controls (min/max/close)
- `doToggleMaximize()` — calls `pywebview.api.toggle_maximize()`, swaps icon SVG

### `gui/static/styles.css` — Design System (~5300 lines)

**Token hierarchy:**
```css
:root {
    /* Surfaces: base → raised → inset → overlay */
    --bg, --bg-sidebar, --bg-panel, --bg-elem, --bg-hover, --bg-input, --bg-surface, --bg-overlay

    /* Text: primary → secondary → muted → dim */
    --text, --text-secondary, --muted, --dim

    /* Borders: hairline → strong */
    --border (rgba alpha), --border-strong

    /* Status: ok, warn, err, tool, file, brand */
    /* Type scale: --text-xs through --text-2xl */
    /* Density: --density-row-py, --density-msg-py, --density-gap */
    /* Transitions: --ease-out, --duration-fast, --duration-normal */
}
```

**Themes:** `[data-theme="light"]` overrides all tokens. `[data-density="compact"]` reduces spacing.

**Key component classes:**
- `.app-menubar` — frameless title bar with File/Edit/View/Help + window controls
- `.sidebar-icon-nav` — horizontal icon strip (Agent/Automations/Background/Settings)
- `.proj-tree-header` / `.proj-tree-sessions` — collapsible project/session tree
- `.agent-row` — session list item with `role="button"`, focus-visible outline
- `.input-wrapper` → `.input-top` + `.input-footer` — full-width textarea with controls row below
- `.cmd-palette-overlay` — Ctrl+K command palette
- `.agent-run-card` — session complete summary with todo strip
- `.follow-up-chips` — suggestion pills after session.end
- `.msg-actions` — hover-visible copy button on messages

### `gui/templates/index.html` — HTML Structure

```
body
├── #app-menubar          — Logo + File/Edit/View/Help + window controls (−□×)
└── #app
    ├── aside#sidebar     — Brand + icon nav + search + project tree + footer
    └── main#main
        └── #main-split
            ├── #main-content
            │   ├── #welcome-screen      — Project picker + backend selector
            │   ├── #chat-welcome-screen — Ask mode welcome
            │   ├── .agent-panel         — Chat container + scroll-end button
            │   ├── #terminal-bar        — Running terminals indicator
            │   ├── #input-bar           — .input-wrapper > .input-top + .input-footer
            │   ├── #settings-view       — Settings sections
            │   ├── #schedule-view       — Automations
            │   ├── #dispatch-view       — Background agents
            │   ├── #command-center      — Workspaces (multi-project)
            │   └── #shortcuts-overlay   — Keyboard shortcuts
            ├── #preview-resize          — Drag handle
            └── #preview-panel           — Browser/screenshot preview
```

## Data Flow

### Message send → response:
1. User types in `#user-input`, presses Enter
2. `sendMessage()` → WebSocket `{ command: 'message', text: '...' }`
3. `gui/app.py` → `_run_session_streaming()` → `session.run(user_msg)`
4. Engine streams `EngineEvent` dicts back via WebSocket
5. `app.js` `handleEvent()` routes each event type to its handler
6. `text.delta` → streaming markdown, `tool.call` → tool card, `session.end` → run summary

### Session switching:
1. Click agent-row → `{ command: 'switch_session', session_id, project_path }`
2. Server: switch project if needed → load session → recreate backend → replay display_events
3. Client: `session_loaded` → `showChatInterface()` → `replayDisplayEvents(events)`

### Settings changes:
1. Change in Settings UI → `{ command: 'update_settings', section, key, value }`
2. Server: `settings.set(section, key, value)` → persists to `~/.resonant/settings.json`
3. Client: `_applyAppearance()` for theme/density/font → `localStorage`

## Extension Points

### Adding a new tool:
1. Add definition to `AGENT_TOOLS` in `engine/tools.py`
2. Add icon mapping to `TOOL_ICONS`
3. Add execution case to `execute_tool()`
4. Add to appropriate sandbox category in `engine/sandbox.py`
5. Implement the executor function

### Adding a new backend:
1. Create class with `stream()`, `health()`, `list_models()`, `classify()` methods
2. Add detection logic to `gui/app.py` `detect_backends()`
3. Add creation logic to `create_backend()`
4. Add to `_KNOWN_TOOL_SUPPORT` if applicable

### Adding a new settings section:
1. Add section definition to `renderSettingsView()` in `app.js`
2. Handle the section in the settings change handler
3. Persist via `settings.set(section, key, value)` (auto-saved to JSON)

### Adding a new sidebar view:
1. Add icon button to `.sidebar-icon-nav` in `index.html`
2. Add view div to `#main-content`
3. Add case to `switchView()` in `app.js`

## Key Files by Size

| File | Lines | Purpose |
|------|-------|---------|
| `gui/app.py` | ~8000 | Server-side state, WebSocket handler, all API commands |
| `gui/static/app.js` | ~6800 | Frontend: all UI logic, event handling, rendering |
| `gui/static/styles.css` | ~5300 | Design system + all component styles |
| `backends.py` | ~2400 | All backend implementations |
| `engine/tools.py` | ~1100 | Tool definitions + execution routing |
| `engine/session.py` | ~950 | Agentic loop orchestration |
| `engine/browser.py` | ~550 | Playwright browser automation |
| `engine/computer_use.py` | ~700 | Desktop vision loop |
| `tui.py` | ~1800 | Terminal UI (alternative to GUI) |

## Running

```bash
# Desktop app (pywebview frameless window)
python -m resonant_client.gui.server --port 8765

# Browser-only mode
python -m resonant_client.gui.server --port 8765 --browser

# Terminal UI
resonant --backend ollama --model qwen3:8b

# With Ollama tuning for large models (Mac Studio 256GB)
RESONANT_OLLAMA_NUM_CTX=131072 RESONANT_OLLAMA_NUM_BATCH=1024 python -m resonant_client.gui.server --port 8765
```

## Testing

```bash
pytest                              # all 516+ tests
pytest -m unit                      # fast unit tests
pytest tests/test_backends.py       # backend tests
pytest tests/test_session_todos.py  # todo parsing
pytest tests/test_gui_hardening.py  # GUI checks
```
