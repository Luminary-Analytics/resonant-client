# Resonant Client — Architecture Guide

This document describes every major module, data flow, and extension point in `resonant-client`. It is written so that any LLM or developer can pick up the codebase and contribute effectively.

## Overview

Resonant Client is an **Ollama-native agentic-coding desktop app** — an
open-source flagship (MIT, since v0.6.3) for local-first, self-improving
autonomous coding. The flagship configuration is **`glm-5.2:cloud`
on Ollama running on the Mac Studio at `10.0.0.133`** (756B, 1M context,
native tool calling). The `deepseek-v4-pro` / `flash` tiers remain one
click away in the model picker as the secondary high-quality option.

> **Single-backend.** The v0.4.0 refocus cut every non-Ollama backend
> (Anthropic, OpenAI, Claude Code, Codex, Resonant Engine, MLX, LM
> Studio). Resonant Client is now an Ollama-only client. If you want
> Anthropic models use Claude Code; for OpenAI use Codex. Some older
> docs/comments still reference the cut backends — treat this section
> as authoritative.

It runs as a frameless desktop app (pywebview) or in a browser, providing:

- An agentic tool loop (LLM → tool → LLM → tool → ...)
- 25+ tools (file ops, bash, browser automation, desktop control, sub-agents, batch)
- A focused single-mode GUI: Agent + Settings only
- Project tree, command palette, preview panel, AGENTS.md badge, git badge
- **Autonomous missions** — a rigorous-grill → roadmap → unattended
  iter loop with REFLECT verdicts (`orchestration/` + `gui/autonomous_*`)
- **A self-improvement loop** — skills auto-extracted from successful
  missions and surfaced back into future ones. This is the project's
  differentiating feature; see [`docs/self-improvement-loop.md`](docs/self-improvement-loop.md).
- An optional **sprint workflow** (planner / generator / evaluator with an autonomous orchestrator) — off by default; opt in via Settings → General

**Project conventions:** Resonant reads `AGENTS.md` from the project root (the cross-tool standard adopted by Codex CLI, OpenCode, Cursor, and OpenHands). Legacy `RESONANT.md` and Anthropic's `CLAUDE.md` are also recognized as fallbacks.

**Per-project state** lives at `~/.resonant/projects/<sha1(project_path)[:12]>/`,
NOT in the user's repo — plan-graphs, intents, harness state, curator
state. Skills live at `~/.resonant/skills/`. Mirrors Claude Code's
`~/.claude/projects/<proj>/` pattern.

```
┌──────────────────────────────────────────────────────────────┐
│                        GUI (browser/pywebview)                │
│  index.html + app.js + styles.css                             │
│  WebSocket ↕ JSON events                                      │
├──────────────────────────────────────────────────────────────┤
│                      gui/app.py (Starlette)                   │
│  State management, session routing, autonomous-mission wiring  │
├──────────────────────────────────────────────────────────────┤
│                     engine/session.py                          │
│  Agentic loop: stream → parse → execute tools → repeat        │
├──────────────┬───────────────┬────────────────────────────────┤
│  backends.py │  engine/      │  orchestration/                │
│  OllamaBackend│  tools.py    │  plan-graph, specialists,       │
│  (only       │  bash, file_* │  autonomous missions,          │
│   backend)   │  glob, grep   │  the self-improvement loop      │
│  + CLOUD_    │  task (sub)   │  (skills / extractor / curator  │
│   MODELS     │  batch        │   / loader)                    │
└──────────────┴───────────────┴────────────────────────────────┘

★ Mac Studio @ http://10.0.0.133:11434 with glm-5.2:cloud
```

## Scope (April 2026 refocus)

The client is laser-focused on **the Agent (agentic-coding) experience**. Removed in the refocus:
- **Ask / chat mode** (`#chat-welcome-screen`, `chat_group`, `ask_workspace_path`, all chat-mode branches)
- **Workspaces / Command Center / Fleet** (`#command-center`, `command_*` WebSocket commands, `command_coordinator`, `command_projects`, `command_tasks`, `org_chart`, fleet worktree isolation)
- **Automations / Scheduler** (`#schedule-view`, `scheduler.py`, `schedule_*` commands)
- **Background agents / Dispatch** (`#dispatch-view`, `task_runner.py`, `dispatch*` commands)
- **All non-Ollama backends** (v0.4.0) — Anthropic, OpenAI, Claude Code, Codex, Resonant Engine, MLX, LM Studio

Everything else (the agentic loop, all 25+ tools, RAG, MCP, harness, sub-agents via the Task tool, browser/computer-use, autonomous missions, the self-improvement loop) is preserved.

## Module Reference

### `backends.py` — Backend Abstraction

`OllamaBackend` is the **only** backend (v0.4.0 cut the rest). It
implements `stream()`, `health()`, `list_models()`, `classify()`.

| Class | Protocol | Notes |
|-------|----------|-------|
| `OllamaBackend` | HTTP `/api/chat` | Adaptive tool calling (native or text-mode), Mac Studio defaults (32k ctx, 1024 batch, 120m keep-alive), `CLOUD_MODELS` list auto-appended |

**Key design rule for Ollama:** All requests to a given `OllamaBackend` instance must use identical `_ollama_options` (num_ctx, num_batch, num_gpu). If any option differs between requests, Ollama unloads and reloads the entire model (30-120s penalty, much worse for large MoE models). Options are set once at init from environment variables.

**Cloud models:** `OllamaBackend.CLOUD_MODELS` lists models routed via Ollama's cloud (`:cloud` tag). The flagship `glm-5.2:cloud` is listed first (v0.6.5 — 756B, 1M context, native tools). The `deepseek-v4-pro:cloud` / `deepseek-v4-flash:cloud` tiers follow as the secondary option (pro's PLAN_DEEP convergence is well characterized — see `docs/v0.5.1-smoke-results.md`).

**Backend priority** (`select_harness_backend` in `gui/app.py`): there is only one backend; per-harness-role model preference is pro for planner/evaluator, flash for the generator role (a deliberate fast-iter trade-off for the test harness). Override with `RESONANT_HARNESS_<ROLE>_MODEL`.

### `network_defaults.py` — Default backend/model resolution

- `get_default_backend()` → `RESONANT_DEFAULT_BACKEND` env / `general.default_backend` setting / `"ollama"`
- `get_default_model()` → `RESONANT_DEFAULT_MODEL` env / `general.default_model` setting / `"glm-5.2:cloud"` (v0.6.5 — switched the flagship from `deepseek-v4-pro:cloud`, which stays the secondary tier)

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
    SUBAGENT_START, SUBAGENT_END        # Sub-agent lifecycle (Task tool)
    TODOS_UPDATED                       # Markdown task list progress
    COMPRESSION                         # Context compressed
    STATUS, ERROR, CHOICES              # Metadata
```

The `TODOS_UPDATED` event is emitted when the model's text contains GitHub-style checkboxes (`- [ ]` / `- [x]`), parsed by `parse_markdown_todos()` in `session.py`.

### `engine/session.py` — Agentic Loop

The core loop:

1. Build system instructions (platform-aware, includes RESONANT.md project instructions, encourages `batch` for parallel reads and `task` for isolated sub-investigations)
2. Stream from backend (`backend.stream()`)
3. Collect text tokens → yield `TEXT_DELTA` / `TEXT_DONE`
4. Parse tool calls (native API or text-mode XML)
5. Execute tools via `execute_tool()` in `tools.py`
6. Feed results back → next iteration
7. Detect doom loops (same tool+args repeated N times)
8. Handle plan mode, choices, sub-agents

**System prompt rule 8** nudges models to use markdown task lists for multi-step work, enabling the live todo strip in the GUI. **Rule 3** nudges models to use the `batch` tool for parallel independent reads. **Rule 9** nudges models to use the `task` tool for sub-investigations.

### `engine/tools.py` — Tool Definitions and Execution

25+ tools in `AGENT_TOOLS` list (OpenAI function-calling format):

| Category | Tools |
|----------|-------|
| **File ops** | `file_read`, `file_write`, `file_edit`, `glob`, `grep` |
| **Shell** | `bash` (non-interactive, timeout-guarded) |
| **Browser** | `browser_navigate`, `browser_click`, `browser_type`, `browser_read`, `browser_screenshot`, `browser_js`, `browser_scroll`, `browser_hover`, `browser_select`, `browser_wait`, `browser_back`, `browser_tabs` |
| **Desktop** | `computer_screenshot`, `computer_click`, `computer_type`, `computer_scroll`, `computer_key`, `computer_drag`, `computer_hover`, `computer_window` |
| **Agent** | `task` (sub-agent spawning — see `engine/agents.py`) |
| **Batch** | `batch` (parallel tool execution, max 25 calls) |

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

### `orchestration/` — Autonomous Missions & the Self-Improvement Loop

The `orchestration/` package is the autonomous-coding subsystem. It is
the largest body of new work since the April refocus and is **not**
optional scaffolding — it is the project's differentiating capability.

**Five AI-native primitives** (`orchestration/__init__.py`):

1. **Intent** — a durable user goal, mutable as understanding grows
2. **Plan-graph** — a DAG of nodes (`plan_graph.py`): goal / status /
   confidence / dependencies
3. **Specialist** — per-node agent specialization (`specialists.py`):
   explore / plan / plan_deep / implement / verify / repair / research
4. **Reflection** — continuous confidence signals + auto-spawned
   verify/repair (`reflect.py`)
5. **Skill** — reusable verified procedures auto-extracted from
   successful runs (`skills.py` + the loop modules below)

**Autonomous missions.** A mission is: rigorous grill (`grill_me.py`)
→ structured spec → roadmap (`gui/roadmap.py`) → an unattended iter
loop (`gui/autonomous_loop.py`) that dispatches each roadmap item as a
Phase-1 sub-mission and runs REFLECT to decide `satisfied` / `continue`
/ `stuck`. `gui/autonomous_factory.py` wires the daemon's injectable
`DaemonHooks`. State persists under `~/.resonant/projects/<hash>/`.

**The self-improvement loop.** Skills are auto-extracted from
successful missions, curated by a background pass, and surfaced back
into future missions' planner context. Provenance (`created_by`) gates
what the curator may touch; pinning gates auto-deprecation. This is
documented in full in **[`docs/self-improvement-loop.md`](docs/self-improvement-loop.md)**
— read that doc before touching any `skill_*` module.

Key modules: `skills.py` (data model + storage), `skill_extraction.py`
+ `skill_mission_extraction.py` (extractors), `skill_curator.py`
(curator), `skill_loader.py` (matcher + injection), `skill_cli.py`
(`resonant-skill` console script), `field_observation_ingest.py`,
`bundled_skills/` (shipped reference skills).

### `gui/server.py` — Application Launcher

Starts uvicorn in a background thread, then either:
- **pywebview:** Frameless native window with `easy_drag=False`; draggable area is elements with class `pywebview-drag-region` (menubar logo + centered title strip — menus and window buttons stay interactive). `-webkit-app-region` in CSS does not drive WebView2 dragging; pywebview uses its injected `customize.js` + `pywebviewMoveWindow`. Custom `_WindowAPI` (minimize, toggle_maximize, close), Windows taskbar icon via `WM_SETICON`
- **Browser fallback:** Prints URL, waits for Ctrl+C

### `gui/app.py` — Web Application

Starlette ASGI app with:
- `/` — serves `index.html`
- `/static/*` — JS, CSS, icons
- `/ws` — WebSocket for all client-server communication

**`AppState`** (singleton) holds: backends, session, project context, settings, costs, harness state.

Key methods:
- `detect_backends()` — network check for Ollama (`http://10.0.0.133:11434`). Appends `OllamaBackend.CLOUD_MODELS` to the detected models.
- `select_harness_backend(session_role, project_path)` — picks the per-harness-role model: pro for planner/evaluator, flash for the generator role.
- `create_backend(type, model, session_role)` — instantiates backend + session
- `_run_session_streaming(ws, user_msg, ...)` — streams engine events to WebSocket
- `get_init_data()` — sends full state to frontend on connect

WebSocket commands (post-refocus): `init`, `message`, `cancel`, `clear`, `switch_session`, `select_backend`, `switch_model`, `delete_session`, `rename_session`, `set_project`, `folder_dialog`, `list_dirs`, `approve`, `choice_select`, `get_settings`, `update_settings`, `get_costs`, `git_status`, `git_quick`, `get_resonant_md`, `save_resonant_md`, `mcp_list`, `mcp_connect`, `rag_index`, harness commands.

### `gui/static/app.js` — Frontend (~4800 lines)

`ResonantApp` class handles everything:

**Initialization:**
- `constructor()` — DOM refs, state init, `_restoreAppearance()`, `_bindMenuBar()`, WebSocket connect
- `bindEvents()` — sidebar nav, keyboard shortcuts, search, preview resize

**Event handling:**
- `handleEvent(event)` — routes `session.start`, `text.delta`, `text.done`, `tool.call`, `tool.result`, `step.start/end`, `session.end`, `todos.updated`, `subagent.start/end`, `harness_state`, `git_status`, etc.
- `handleTodosUpdated(event)` — updates live todo progress strip
- `handleSessionEnd(event)` — renders run summary card, follow-up chips

**Sidebar:**
- `renderFilteredSessions()` → `_renderProjectTree(sessions)` — groups sessions by project path, renders collapsible tree with expand/collapse state
- `_createTreeSessionRow(session)` — creates agent-row with `role="button"`, `tabindex="0"`, click/keyboard handlers

**Command palette:**
- `openCommandPalette()` / `closeCommandPalette()` — Ctrl/Cmd+K overlay
- `_cmdPaletteCommands()` — list of available commands (new agent, settings, preview, sidebar, shortcuts)
- `_renderCommandPaletteResults(query)` — fuzzy filter + keyboard nav

**Settings:**
- `renderSettingsView()` — builds sections: General, Appearance, Local Backends, Network, API Keys, Cost Tracking, Memory, RAG, Hooks, MCP Servers
- `_applyAppearance(key, value)` — applies theme/density/font-size to DOM + localStorage
- `_restoreAppearance()` — hydrates on startup

**Menu bar:**
- `_bindMenuBar()` — wires File/Edit/View/Help actions + window controls (min/max/close)
- `doToggleMaximize()` — calls `pywebview.api.toggle_maximize()`, swaps icon SVG

### `gui/static/styles.css` — Design System

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

(Note: dead style rules from the deleted chat/fleet/dispatch/schedule features still exist in styles.css. They're harmless dead code; clean up opportunistically.)

### `gui/templates/index.html` — HTML Structure

```
body
├── #app-menubar          — Logo + File/Edit/View/Help + window controls (−□×)
└── #app
    ├── aside#sidebar     — Brand + icon nav (Agent / Settings) + search + project tree + footer
    └── main#main
        └── #main-split
            ├── #main-content
            │   ├── #welcome-screen      — Project picker + backend selector
            │   ├── .agent-panel         — Chat container + scroll-end button
            │   ├── #terminal-bar        — Running terminals indicator
            │   ├── #input-bar           — .input-wrapper > .input-top + .input-footer
            │   └── #settings-view       — Settings sections
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

(Approximate; the GUI files in particular grow steadily.)

| File | Lines | Purpose |
|------|-------|---------|
| `gui/static/app.js` | ~10,400 | Frontend: all UI logic, event handling, rendering |
| `gui/static/styles.css` | ~9,100 | Design system + all component styles |
| `gui/app.py` | ~8,200 | Server-side state, WebSocket handler, all API commands |
| `backends.py` | ~2,400 | `OllamaBackend` implementation |
| `tui.py` | ~1,800 | Terminal UI (alternative to GUI) |
| `engine/tools.py` | ~1,100 | Tool definitions + execution routing |
| `engine/session.py` | ~950 | Agentic loop orchestration |
| `orchestration/*` | — | Autonomous missions + self-improvement loop (many files; see that section) |

## Running

```bash
# Desktop app (pywebview frameless window) — defaults to Ollama on Mac Studio
python -m resonant_client.gui.server --port 8765

# Browser-only mode
python -m resonant_client.gui.server --port 8765 --browser

# Override the default model (Ollama is the only backend)
RESONANT_DEFAULT_MODEL=deepseek-v4-flash:cloud python -m resonant_client.gui.server --port 8765

# Tune Ollama for the Mac Studio (already defaults to 32k ctx / 1024 batch / 120m keep-alive)
RESONANT_OLLAMA_NUM_CTX=131072 RESONANT_OLLAMA_NUM_BATCH=2048 \
  python -m resonant_client.gui.server --port 8765

# Terminal UI
resonant --backend ollama --model deepseek-v4-pro:cloud
```

## Testing

```bash
pytest                              # full suite — 2469 pass / 2 skip as of v0.6.3
pytest -m unit                      # fast unit tests
pytest tests/test_backends.py       # backend tests (Ollama tool detection, format conversion)
pytest tests/test_skill_loader_wiring.py   # self-improvement loop — read-side wiring
pytest tests/test_grill_me_rigorous.py     # grill prompt invariants (incl. F1 Rule 0)
```

## Release & Distribution

As of v0.2.0 (April 2026), Resonant Client ships as a Windows installer with silent auto-update. The full pipeline is:

1. `git push origin vX.Y.Z` triggers `.github/workflows/release.yml`
2. CI builds a PyInstaller bundle, wraps it in an Inno Setup installer, signs it with EdDSA, publishes a GitHub Release, and updates `appcast.xml` on the `gh-pages` branch
3. Every running v0.2.0+ client polls the appcast on a 24-hour interval via WinSparkle (`resonant_client/updater.py`) and prompts the user when a new version is available
4. EdDSA signature verification protects against update-channel hijacking

### Release pipeline files

| Purpose | File |
|---------|------|
| **Operational runbook** ("ship a release") | [`RELEASING.md`](RELEASING.md) |
| **Architectural deep-dive** ("understand the pipeline") | [`docs/release-pipeline.md`](docs/release-pipeline.md) |
| **Bug ledger** ("what's broken, what's known") | [`docs/known-issues.md`](docs/known-issues.md) |
| PyInstaller bundle config | [`packaging/resonant.spec`](packaging/resonant.spec) |
| Inno Setup installer script | [`packaging/installer.iss`](packaging/installer.iss) |
| Appcast XML mutator (CI script) | [`packaging/update_appcast.py`](packaging/update_appcast.py) |
| Vendored WinSparkle 0.9.2 binaries | [`packaging/winsparkle/`](packaging/winsparkle/) (3.3 MB) |
| WinSparkle ctypes wrapper (runtime) | [`resonant_client/updater.py`](resonant_client/updater.py) |
| Entry point with updater wiring + `--version` | [`resonant_client/__main__.py`](resonant_client/__main__.py) |
| GitHub Actions release workflow | [`.github/workflows/release.yml`](.github/workflows/release.yml) |

### Key invariants

- **Tag must match `__version__`** — CI step 3 fails the run if `resonant_client/__init__.py:__version__` doesn't match the pushed tag (sans `v` prefix).
- **EdDSA private key never leaves the dev machine** — lives at `~/.resonant/keys/eddsa_priv.key`, mirrored to GitHub repo secret `EDDSA_PRIVATE_KEY` for CI access only. Never committed.
- **Public key is hardcoded** in `resonant_client/updater.py:EDDSA_PUBLIC_KEY` and bundled into every binary. Required for offline signature verification.
- **`gh-pages` branch is orphan** — separate from `main` history, contains only `appcast.xml`, `index.html`, `.nojekyll`. GitHub Pages serves from this branch automatically.
- **Auto-update is fire-and-forget** — `init_updater()` runs in a background thread; updater failures never block app startup (try/except wraps the call site).
