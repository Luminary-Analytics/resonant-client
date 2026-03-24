# Instructions

This is the Resonant Client project — an agentic coding assistant with TUI and GUI interfaces.

# Conventions

- Use Python 3.12+ features
- Follow the existing code style (type hints, docstrings for public functions)
- Keep modules focused and small
- Prefer event-driven architecture — engine yields `EngineEvent`s consumed by TUI/GUI
- All backends implement the same streaming interface (`TEXT_DELTA`, `TOOL_CALL`, `DONE`, `ERROR`)
- Tool execution lives in `engine/tools.py`; never add tool logic to TUI or GUI layers

# Architecture

- `resonant_client/engine/` — core agentic loop, tools, sessions, sub-agents
- `resonant_client/gui/` — web-based GUI (Starlette + WebSocket)
  - `gui/app.py` — main WebSocket handler, event enrichment, command dispatch
  - `gui/runtime.py` — shared `BackendSpec` and `build_session()` for all session types
  - `gui/static/app.js` — frontend: event rendering, terminal bar, preview panel, settings
- `resonant_client/backends.py` — LLM backend integrations (Ollama, Claude, OpenAI, LM Studio, Resonant Engine)
- `resonant_client/events.py` — shared event protocol (`EngineEvent`, `ClientCommand`)
- `resonant_client/tui.py` — terminal UI (Rich + prompt-toolkit)

# Backends

- **Ollama** — local LLMs with adaptive tool-calling (native or XML fallback)
- **LM Studio** — OpenAI-compatible local inference (`LMSTUDIO_URL` or `--lmstudio-url`)
- **Claude** — Anthropic API via `anthropic` SDK
- **OpenAI** — OpenAI API via `openai` SDK
- **Resonant Engine** — oscillatory intelligence backend via SSE

# GUI Event Flow

1. User sends message via WebSocket
2. `app.py` spawns `_engine_thread()` running `session.run()`
3. Engine yields events → thread-safe queue → async WebSocket handler
4. Frontend `app.js` handles each event type (tool.call, tool.result, text.delta, etc.)
5. Terminal bar tracks active `bash` tool calls by `call_id`
