# Resonant Client

Agentic coding client for the [Resonant Cognitive Engine](https://github.com/Luminary-Analytics/resonant-engine) — a Claude Code-like interface powered by oscillatory intelligence.

Supports multiple backends (Resonant Engine, Ollama, Claude, OpenAI, LM Studio) with both a terminal TUI and a web-based GUI. The client is lightweight (no torch/transformers) and connects over HTTP, so you can run it anywhere on your network.

For the `resonant` backend, harness ownership is now engine-side:

- `resonant-engine` is the system of record for `.resonant-harness`
- step execution, cycles, harness mutations, teacher recovery, and recurring harness-cycle schedules live in the engine
- `resonant-client` acts as the viewer/control layer over those engine APIs

Related docs:

- [docs/harness-core.md](./docs/harness-core.md)
- [docs/engine-harness-unification-plan.md](./docs/engine-harness-unification-plan.md)

```
┌───────────────────────┐                ┌─────────────────────────┐
│  resonant-client      │                │  Backend (any of):      │
│                       │    HTTP/SSE    │  - Resonant Engine      │
│  TUI (terminal)       │ ──────────────>│  - Ollama (local LLMs)  │
│  GUI (web browser)    │   LAN / WAN   │  - Claude API           │
│                       │               │  - OpenAI API           │
│  Any machine          │               │  - LM Studio            │
│  No GPU needed        │               │                         │
└───────────────────────┘                └─────────────────────────┘
```

## Features

### Core
- **Multi-backend support** — Ollama, Resonant Engine, Claude (Anthropic), OpenAI, LM Studio
- **Agentic tool loop** — bash, file_write, file_read, file_edit, glob, grep
- **SSE streaming** — tokens appear live as the model generates them
- **Plan-first workflow** — asks clarifying questions, presents a plan, then executes
- **Auto-plan classification** — LLM-based complexity check routes small asks straight to execution
- **Interactive choice menus** — multiple-choice prompts with recommended defaults
- **Sub-agents** — spawn isolated agents for explore, plan, and build subtasks
- **Batch tool execution** — parallel tool calls via ThreadPoolExecutor
- **Works cross-platform** — Windows, macOS, Linux

### Adaptive Tool Calling (Ollama)
- **Auto-detection** of model tool-calling capability via 3-tier strategy:
  1. Known model lists (instant)
  2. Template inspection (`{{.Tools}}` in Modelfile)
  3. Probe request (live test)
- **Text-mode fallback** — models without native tool support get `<tool_call>` XML injected into the system prompt and parsed from responses
- **Transparent switching** — works identically from the user's perspective regardless of model capability

### Diff Review
- **Rich unified diffs** for file edits and writes before applying changes
- **Risk classification** — low/medium/high based on command patterns
- **Dangerous pattern detection** — rm -rf, sudo, curl|sh, SQL drops, fork bombs, force pushes
- **Sensitive file warnings** — .env, credentials, .ssh keys, git config

### RAG / Codebase Indexing
- **Automatic codebase indexing** with symbol extraction across 10+ languages
- **Keyword search** with bidirectional path matching
- **Incremental indexing** via content hashing — only re-indexes changed files
- **Persistent cache** at `.resonant/index.json`
- **Optional engram integration** for semantic search

### Computer Use & Desktop Automation
- **Full desktop control** — click, type, scroll, drag, hover via pyautogui
- **Vision loop** — auto-screenshots after every action with coordinate scaling
- **Window management** — list, focus, resize, minimize windows
- **OCR** — text extraction from screen regions for context
- **Browser automation** — 12 Playwright tools: navigate, click, type, read, screenshot, JS, scroll, hover, select, wait, back, tabs

### GUI (Cursor 3-style Desktop App)
- **Frameless native window** via pywebview with custom title bar (File/Edit/View/Help menus)
- **Custom window controls** — minimize, maximize/restore (icon swaps), close; double-click title bar to toggle maximize
- **Resonant brand icon** in Windows taskbar/macOS dock (purple waveform mark)
- **Design system** — Inter + Geist Mono fonts, semantic surface tokens, light/dark themes, comfortable/compact density
- **Command palette** — Ctrl/Cmd+K with fuzzy search and keyboard navigation
- **Collapsible project tree** — sessions grouped by project, expand/collapse, live search filter
- **Icon strip nav** — Agent, Automations, Background, Settings as compact icon buttons
- **Full-width input bar** — textarea with send button, `+` context, permission pill, model selector below (Cursor 3 layout)
- **Agent run cards** — todo progress from markdown checklists, file change summaries, follow-up chips
- **Message actions** — copy button on assistant messages
- **Real-time WebSocket streaming** with markdown rendering, syntax highlighting
- **Terminal status bar** — running terminal indicator with per-terminal stop buttons
- **Preview panel** — side-by-side screenshot viewer with persistent split width
- **Tool permission dialogs** with diff preview and risk badges
- **Dispatch system** — background task execution with status tracking
- **Scheduler** — cron-based recurring tasks
- **Settings** — Appearance (theme, density, font size), Local Backends (Ollama/LM Studio config), API keys, cost tracking
- **Keyboard shortcuts** — Ctrl+N (new agent), Ctrl+, (settings), Ctrl+K (palette), Alt+1-4 (views)

### Hooks & Extensions
- **Pre/post tool hooks** — custom shell commands triggered before or after tool execution
- **MCP server management** — integrate external tools via Model Context Protocol
- **Project instructions** — RESONANT.md files for per-project conventions

### Memory (Engram Integration)
- **Cross-session memory** via engram MCP server
- **Auto-recall** at session start based on user query
- **Session summaries** persisted at session end

## Recent Notes

- GUI/runtime hardening summary: [docs/gui-hardening.md](docs/gui-hardening.md)

## Install

### Recommended — Windows installer (with silent auto-updates)

Download the latest `resonant-setup-X.Y.Z.exe` from the [GitHub Releases page](https://github.com/Luminary-Analytics/resonant-client/releases) and run it.

- Installs to `%LOCALAPPDATA%\Programs\Resonant Client\` (no admin / UAC prompt)
- Adds Start Menu shortcut "Resonant Client"
- Future updates land automatically — WinSparkle polls the [appcast feed](https://luminary-analytics.github.io/resonant-client/appcast.xml) every 24 hours and prompts when a new version is available
- All updates are EdDSA-signed end-to-end (the embedded public key verifies every download against the private key on the publisher's machine)

First-install only: SmartScreen will show "Unrecognized publisher" — click "More info" → "Run anyway". The v0.x line is unsigned for now; code signing planned for v1.0+.

### Alternative — install from source

```bash
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client

# Core install
pip install -e .

# With all optional features
pip install -e ".[all,dev]"

# Or specific extras
pip install -e ".[gui]"        # Web GUI
pip install -e ".[claude]"     # Claude/Anthropic backend
pip install -e ".[openai]"     # OpenAI backend
pip install -e ".[dev]"        # Testing (pytest, ruff)
```

Source installs run identically to the bundled exe but without auto-update (pull + reinstall to upgrade).

## Prerequisites

- **Bundled installer:** Windows 10+ (x64). No Python install needed — everything's in the bundle.
- **Source install:** Python 3.11+ on Windows / macOS / Linux
- One of: Ollama server, Resonant Engine, Claude API key, or OpenAI API key

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RESONANT_API` | `http://localhost:8000` | Resonant Engine API URL |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `LMSTUDIO_URL` | — | LM Studio server URL (OpenAI-compatible) |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `RESONANT_OLLAMA_NUM_CTX` | `4096` | Ollama context window size (raise for large models) |
| `RESONANT_OLLAMA_NUM_BATCH` | `512` | Ollama batch size |
| `RESONANT_OLLAMA_NUM_GPU` | `99` | Ollama GPU layers |
| `RESONANT_OLLAMA_KEEP_ALIVE` | `60m` | How long Ollama keeps model loaded |
| `RESONANT_OLLAMA_HTTP_TIMEOUT_SEC` | `180` | Ollama HTTP timeout (streaming) |
| `RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC` | `120` | Ollama read timeout |
| `RESONANT_LMSTUDIO_READ_TIMEOUT_SEC` | `600` | LM Studio read timeout (long generations) |
| `RESONANT_OPENAI_READ_TIMEOUT_SEC` | `120` | OpenAI read timeout |

## Usage

### TUI (Terminal)

```bash
# Auto-detect backend
resonant

# Explicit Ollama backend
resonant --backend ollama --model llama3.1:8b

# Connect to Ollama on another machine
resonant --backend ollama --api http://10.0.0.133:11434 --model qwen2.5-coder:14b

# LM Studio backend (OpenAI-compatible)
resonant --lmstudio-url http://10.0.0.133:1234

# Require approval before tool execution
resonant --approve
```

### GUI (Web Browser)

```bash
resonant-gui
# Opens at http://localhost:8765
```

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/quit` | Exit |
| `/cd <dir>` | Change working directory |
| `/clear` | Clear conversation history |
| `/status` | Show engine/backend status |
| `/approve on\|off` | Toggle tool approval mode |

## Testing

The project has a comprehensive automated test suite with 516+ tests covering all features.

```bash
# Run all tests
python run_tests.py

# Fast unit tests only (~0.5s)
python run_tests.py --unit

# Just one module
python run_tests.py --module protocol
python run_tests.py --module diff_review
python run_tests.py --module rag
python run_tests.py --module backends

# With coverage report
python run_tests.py --coverage

# Adversarial edge cases only
python run_tests.py --adversarial

# Parallel execution
python run_tests.py --parallel

# Or use pytest directly
pytest                              # all tests
pytest -m unit                      # just unit tests
pytest tests/test_protocol.py       # one file
pytest -k "test_crlf"              # name filter
```

### Test Structure

| Module | Tests | Coverage |
|--------|-------|---------|
| `tests/test_protocol.py` | 62 | protocol.py (99%) — prompt building, JSON parsing, tool call extraction |
| `tests/test_diff_review.py` | 113 | diff_review.py (98%) — file diffs, risk detection, sensitive paths |
| `tests/test_rag.py` | 161 | rag.py (91%) — indexing, search, symbol extraction, caching |
| `tests/test_backends.py` | 129 | backends.py — tool conversion, model detection, adaptive calling |
| `tests/test_session_todos.py` | 3 | session.py — markdown task list parsing |
| `tests/test_gui_hardening.py` | 11 | GUI hardening checks |
| **Total** | **516+** | All passing |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a comprehensive guide to every module, data flow, and extension point.

```
resonant_client/
├── backends.py              # Backend abstraction (Ollama, Engine, Claude, OpenAI, LM Studio, cloud models)
├── events.py                # EngineEvent/ClientCommand enums, todos.updated event
├── protocol.py              # Tool prompt building, JSON/XML parsing
├── tui.py                   # Terminal UI (Rich + prompt-toolkit)
├── engine/
│   ├── session.py           # Agentic loop, markdown todo parsing
│   ├── tools.py             # Tool definitions + execution routing
│   ├── browser.py           # 12 Playwright browser tools
│   ├── computer.py          # Desktop automation (pyautogui + mss)
│   ├── computer_use.py      # Vision loop, OCR, auto-screenshot
│   ├── diff_review.py       # Diff generation and risk analysis
│   ├── rag.py               # Codebase indexing and search
│   ├── sandbox.py           # Permission levels and tool allowlists
│   ├── agents.py            # Sub-agent type definitions
│   ├── hooks.py             # Pre/post tool hooks
│   ├── mcp.py               # MCP server management
│   ├── memory.py            # Engram memory integration
│   └── ...                  # compression, clipboard, event_log, policies, worktree
├── gui/
│   ├── app.py               # Starlette web app + WebSocket handler (~8000 lines)
│   ├── server.py            # Uvicorn launcher, pywebview frameless window, taskbar icon
│   ├── sessions.py          # Project/session persistence
│   ├── settings.py          # ~/.resonant/settings.json
│   ├── costs.py             # Token cost tracking
│   ├── scheduler.py         # Cron-based recurring tasks
│   ├── command_projects.py  # Multi-project workspaces
│   ├── org_chart.py         # Agent hierarchy management
│   ├── templates/index.html # Menu bar, sidebar, chat, input bar, preview, dialogs
│   └── static/
│       ├── app.js           # Frontend (~6800 lines)
│       ├── styles.css       # Design system (~5300 lines)
│       ├── favicon.svg      # Browser tab icon
│       ├── resonant.ico     # Windows taskbar icon
│       └── resonant.png     # macOS dock icon
└── harness/                 # Harness orchestration (engine-side ownership)
```

## Dependencies

**Core** (3 packages):
- `rich` — terminal UI rendering
- `prompt-toolkit` — interactive input with history
- `httpx` — HTTP client with streaming support

**Optional**:
- `anthropic` — Claude API backend
- `openai` — OpenAI API backend
- `starlette` + `uvicorn` + `jinja2` + `pywebview` — Web GUI
- `playwright` — Browser automation
- `websockets` — Server mode

**Dev**:
- `pytest` + `pytest-cov` + `pytest-xdist` — Testing
- `ruff` — Linting
