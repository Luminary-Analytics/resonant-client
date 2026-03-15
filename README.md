# Resonant Client

Agentic coding TUI for the [Resonant Cognitive Engine](https://github.com/Luminary-Analytics/resonant-engine) — a Claude Code-like terminal interface powered by oscillatory intelligence.

The client is lightweight (no torch/transformers) and connects to the engine over HTTP, so you can run it on any machine on your network while the engine runs on your GPU server.

```
┌─────────────────────┐                ┌─────────────────────────┐
│  resonant-client    │                │  resonant-engine        │
│  (TUI / future GUI) │    HTTP/SSE    │  (API server + model)   │
│                     │ ──────────────>│                         │
│  Any machine        │   LAN / WAN   │  60K+ knowledge patterns│
│  No GPU needed      │               │  Ollama decoder         │
│                     │               │  Hopfield + Kuramoto    │
└─────────────────────┘                └─────────────────────────┘
```

## Features

- **SSE streaming** — tokens appear live as the engine generates them
- **Agentic tool loop** — bash, file_write, file_read, file_edit, glob, grep
- **Plan-first workflow** — asks clarifying questions, presents a plan, then executes
- **Interactive choice menus** — multiple-choice prompts with recommended defaults
- **Cognitive status bar** — energy, coherence, clusters, processing mode
- **Syntax-highlighted code** — file previews, diffs, and tool output
- **Works cross-platform** — Windows, macOS, Linux

## Prerequisites

- Python 3.11+
- A running [Resonant Engine](https://github.com/Luminary-Analytics/resonant-engine) server (can be on another machine)

## Installation

### Windows

```powershell
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client
pip install -e .
```

Set the engine URL (replace with your engine host's IP):

```powershell
# Set for current session
set RESONANT_API=http://10.0.0.133:8000

# Or set permanently (run in admin PowerShell)
[System.Environment]::SetEnvironmentVariable("RESONANT_API", "http://10.0.0.133:8000", "User")
```

### macOS

```bash
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client
pip install -e .
```

Set the engine URL:

```bash
# Add to ~/.zshrc for persistence
echo 'export RESONANT_API="http://10.0.0.133:8000"' >> ~/.zshrc
source ~/.zshrc
```

If the engine is running on the same machine (localhost:8000), no environment variable is needed — it auto-connects.

### Linux

```bash
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client
pip3 install -e .
```

Set the engine URL:

```bash
# Add to ~/.bashrc for persistence
echo 'export RESONANT_API="http://10.0.0.133:8000"' >> ~/.bashrc
source ~/.bashrc
```

## Usage

Navigate to any project directory and run:

```bash
resonant
```

Or with explicit options:

```bash
# Connect to a specific engine host
resonant --api http://10.0.0.133:8000

# Set a working directory
resonant --dir ~/projects/my-app

# Require approval before tool execution
resonant --approve
```

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/quit` | Exit |
| `/cd <dir>` | Change working directory |
| `/clear` | Clear conversation history |
| `/status` | Show engine status |
| `/approve on\|off` | Toggle tool approval mode |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RESONANT_API` | `http://localhost:8000` | Engine API URL |

## Starting the Engine

The engine must be running before the client can connect. On your engine host (e.g., Mac Studio):

```bash
cd ~/Repos/Resonant-Project/resonant-engine
source ../.venv/bin/activate
python -m resonant_engine --load data/resonant_engine
```

The engine binds to `0.0.0.0:8000` by default, making it accessible to other machines on the network.

## Dependencies

Only three lightweight packages — no ML frameworks required:

- `rich` — terminal UI rendering
- `prompt-toolkit` — interactive input with history
- `httpx` — HTTP client with streaming support
