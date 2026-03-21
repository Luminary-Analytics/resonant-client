# Instructions

This is the Resonant Client project — an agentic coding assistant with TUI and GUI interfaces.

# Conventions

- Use Python 3.12+ features
- Follow the existing code style (type hints, docstrings for public functions)
- Keep modules focused and small

# Architecture

- `resonant_client/engine/` — core agentic loop, tools, sessions
- `resonant_client/gui/` — web-based GUI (Starlette + WebSocket)
- `resonant_client/backends.py` — LLM backend integrations
- `resonant_client/events.py` — shared event protocol
