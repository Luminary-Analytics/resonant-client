# Resonant Client

Agentic coding client for the [Resonant Cognitive Engine](https://github.com/Luminary-Analytics/resonant-engine) — a Claude Code-like interface powered by oscillatory intelligence.

Supports multiple backends (Resonant Engine, Ollama, Claude, OpenAI, LM Studio) with both a terminal TUI and a web-based GUI. The client is lightweight (no torch/transformers) and connects over HTTP, so you can run it anywhere on your network.

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
- **Browser automation** — Playwright-based navigation, clicking, JS execution, screenshots

### GUI (Web Interface)
- **Browser-based UI** with real-time WebSocket streaming
- **Terminal status bar** — Codex-style running terminal indicator with per-terminal stop buttons
- **Project management** — switch projects, manage settings, session history
- **Tool permission dialogs** with diff preview and risk badges
- **Preview panel** — side-by-side screenshot viewer for computer/browser use
- **Dispatch system** — background task execution with status tracking
- **Scheduler** — cron-based recurring tasks
- **RAG controls** — index, search, view stats
- **Engram memory panel** — recall, remember, status
- **Settings management** — API keys, backend config, permissions

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

## Prerequisites

- Python 3.11+
- One of: Ollama server, Resonant Engine, Claude API key, or OpenAI API key

## Installation

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

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RESONANT_API` | `http://localhost:8000` | Resonant Engine API URL |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `LMSTUDIO_URL` | — | LM Studio server URL (OpenAI-compatible) |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key |

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

The project has a comprehensive automated test suite with 512 tests covering all features.

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
| **Total** | **512** | All passing |

## Architecture

```
resonant_client/
├── __init__.py              # Package version
├── __main__.py              # CLI entry point
├── backends.py              # Backend abstraction (Ollama, Engine, Claude, OpenAI, LM Studio)
├── protocol.py              # Tool prompt building, JSON parsing
├── events.py                # Event type definitions
├── tui.py                   # Terminal UI (Rich + prompt-toolkit)
├── engine/
│   ├── session.py           # Agentic loop orchestration
│   ├── tools.py             # Tool execution (bash, file ops, browser, desktop)
│   ├── diff_review.py       # Diff generation and risk analysis
│   ├── rag.py               # Codebase indexing and search
│   ├── memory.py            # Engram memory integration
│   ├── agents.py            # Sub-agent support
│   ├── mcp.py               # MCP server management
│   ├── hooks.py             # Pre/post tool hooks
│   ├── compression.py       # Context compression
│   ├── browser.py           # Browser automation (Playwright)
│   ├── computer.py          # Desktop automation (pyautogui + mss)
│   ├── computer_use.py      # Vision loop coordination, OCR
│   ├── clipboard.py         # Clipboard access
│   └── worktree.py          # Git worktree isolation
└── gui/
    ├── app.py               # Starlette web application
    ├── server.py             # Uvicorn launcher
    ├── runtime.py            # Shared backend/session construction
    ├── sessions.py           # Session management
    ├── settings.py           # Settings persistence
    ├── costs.py              # Token cost tracking
    ├── scheduler.py          # Task scheduling
    ├── task_runner.py        # Background task execution
    ├── project_instructions.py
    ├── templates/
    │   └── index.html        # Main GUI template
    └── static/
        ├── app.js            # Frontend application
        └── styles.css         # UI styles
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
