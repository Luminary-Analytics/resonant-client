# Resonant Client

**The Ollama-native agentic coding desktop app — purpose-built for DeepSeek and other open-source local models.**

If you want to code with Anthropic models, reach for [Claude Code](https://claude.com/product/claude-code). If you want OpenAI, reach for [Codex](https://github.com/openai/codex). For DeepSeek (and any open Ollama model), this is the tool.

```
┌──────────────────────────┐                ┌─────────────────────────┐
│  resonant-client         │                │  Ollama                 │
│                          │                │                         │
│  • Mission flow          │   HTTP /api    │  • deepseek-v4-flash    │
│  • Plan-graph specialists│ ──────────────>│  • deepseek-v4-pro      │
│  • Cycle guards          │   LAN / WAN    │  • Any open model       │
│  • await_user tool       │                │                         │
│  • Diagnostics ZIP       │                │  Mac Studio / local     │
└──────────────────────────┘                └─────────────────────────┘
```

## Why this exists

Anthropic and OpenAI both ship excellent first-party agentic coders for their own models. Nothing matches that quality if you're already in their ecosystem. But there's no equivalent purpose-built tool for the **open-source local model** ecosystem — DeepSeek's V4 family running through Ollama, in particular. This is that tool.

The product surface is shaped by the deepseek/Ollama path:
- Single backend, single trust path — every feature is exercised by every user
- Mission flow tuned for grill-style interviews that DeepSeek Pro/Flash do well
- Cycle guards + `await_user` escape hatch so smaller open models don't waste your tokens
- Mac Studio at `10.0.0.133:11434` is the canonical Ollama host (override anywhere)

## Features

### Mission flow (long-running agents)
- **Grill-me interview** to refine the spec before any code is written
- **Plan → implement → verify** specialist pipeline with `working_subdir` propagation across siblings
- **Cycle guards** that catch tool-call loops (windowed signature dedup + read-only churn cap)
- **`await_user` tool** so the agent can ask focused questions instead of cycling through speculative searches
- **Spec dispatch** to the planner with the full structured spec, not just a paraphrase

### Autonomous mission daemon (v0.5.x)
- **Roadmap-driven outer loop** that picks unchecked items, dispatches Phase-1 sub-missions, runs REFLECT every K iters, and exits via 7 priority-ordered stop rules
- **Resume after interrupt** — server restart / crash / sleep doesn't lose mission progress
- **Human-in-the-loop forks** — when REFLECT can't autonomously decide (e.g. path-mismatch), the daemon parks and surfaces a structured decision card to the GUI
- **Per-specialist Ollama routing** — pin pro for REFLECT/PLAN_DEEP, flash for IMPLEMENT/EXPLORE, via `general.specialist_model_overrides` in settings or `RESONANT_SPECIALIST_<NAME>_MODEL` env vars
- **Pause-after-iter + Stop** — graceful (finish current iter, exit) vs abrupt (cancel in-flight)
- **Live activity inspector** — header badge shows `running REFLECT · 12s` so you can tell stuck-vs-slow at a glance

### Smoke harness (`resonant-smoke`)
- `resonant-smoke run --spec wordcount --model pro` — single autonomous run against a bundled spec
- `resonant-smoke variance --n 5` — per-spec convergence + iter-duration variance
- `resonant-smoke baseline {set,list,show,rm}` + `--diff-baseline` flag — exit non-zero on regressions
- `resonant-smoke ci` — curated suite for cron / GitHub Actions
- 5 bundled specs: `minimal`, `wordcount`, `roguelite`, `jsonlines`, `refactor-py`

### Diagnostics + cost tracking (v0.5.9+)
- Per-iter cost attribution with per-model breakdown (e.g. pro for REFLECT + flash for IMPLEMENT shown as separate chips)
- Daily cost tracking with budget alerts
- **Help → Save Diagnostics ZIP** now bundles `costs.json`, per-iteration metadata files, and `mission-summary.json` index alongside the redacted logs

### Desktop GUI
- Frameless native window via pywebview
- Project picker per mission so the agent always writes where you expect
- Chat-header project path display with unsafe-folder warnings
- Live plan-graph view in the preview panel
- Inline diff review for every file edit
- **Help → Save Diagnostics ZIP** — bundles redacted logs / intent audits / settings into a single ZIP for GitHub issues
- Cmd palette (Ctrl/Cmd+K), keyboard shortcuts, dark/light themes

### Engine
- Native Ollama tool calling via `/api/chat` with streaming
- Adaptive text-mode fallback for models without native tool support
- Three-tier permission model (suggest / auto-edit / full-auto)
- Path sandbox tied to the project root
- Codebase indexing + RAG with incremental hashing
- Optional Engram memory across sessions

### Auto-update (Windows)
- Bundled installer + WinSparkle polls the appcast every 24 hours
- Every release is EdDSA-signed end-to-end (embedded public key verifies the download)

## Install

### Recommended — Windows installer

Download the latest `resonant-setup-X.Y.Z.exe` from the [Releases page](https://github.com/Luminary-Analytics/resonant-client/releases) and run it.

- Installs to `%LOCALAPPDATA%\Programs\Resonant Client\` (no admin / UAC prompt)
- Adds Start Menu shortcut "Resonant Client"
- Future updates land automatically; new versions are EdDSA-signed and verified before install

First-install only: Windows SmartScreen will show "Unrecognized publisher" — click "More info" → "Run anyway". The v0.x line is unsigned for now; code signing planned for v1.0+.

### Source install

```bash
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client

pip install -e .                    # Core (TUI only)
pip install -e ".[gui]"             # Desktop GUI (recommended)
pip install -e ".[all,dev]"         # Everything + tests
```

## Prerequisites

1. **Ollama running somewhere reachable.** Mac Studio at `10.0.0.133:11434` is the canonical default; localhost works too. Install from [ollama.com](https://ollama.com/download), then `ollama serve`.
2. **At least one DeepSeek model pulled:**
   ```bash
   ollama pull deepseek-v4-flash:cloud   # flagship — fast
   ollama pull deepseek-v4-pro:cloud     # higher quality, slower
   ```
3. **Bundled installer:** Windows 10+ (x64). Source install: Python 3.11+ on Windows / macOS / Linux.

If Ollama isn't reachable on first launch, the welcome screen renders a setup wizard with the URL field, install link, and pull commands. You never have to leave the window to get unstuck.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://10.0.0.133:11434` | Ollama base URL |
| `RESONANT_OLLAMA_NUM_CTX` | `4096` | Ollama context window size |
| `RESONANT_OLLAMA_NUM_BATCH` | `512` | Ollama batch size |
| `RESONANT_OLLAMA_NUM_GPU` | `99` | Ollama GPU layers |
| `RESONANT_OLLAMA_KEEP_ALIVE` | `60m` | How long Ollama keeps the model loaded |
| `RESONANT_OLLAMA_HTTP_TIMEOUT_SEC` | `180` | Streaming timeout |
| `RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC` | `120` | Read timeout |

The `~/.resonant/settings.json` file (managed via Settings → Network in the GUI) overrides env vars when set.

## Usage

### GUI (recommended)

```bash
resonant-gui
# Native frameless window opens; pick a project folder; pick a model; go.
```

### TUI

```bash
resonant                                          # auto-detect
resonant --backend ollama --model deepseek-v4-flash:cloud
resonant --backend ollama --api http://10.0.0.133:11434 --model deepseek-v4-pro:cloud
```

### Slash commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/quit` | Exit |
| `/cd <dir>` | Change working directory |
| `/clear` | Clear conversation history |
| `/status` | Show backend status |
| `/approve on\|off` | Toggle tool approval mode |

## Mission flow at a glance

1. Click the **🎯 Mission** toggle in the chat header (or "Or start a Mission" on the empty state).
2. Type the project folder + describe the feature.
3. The grill-me interviewer asks clarifying questions one at a time with recommendations.
4. When you have shared understanding, the model emits a `## Final spec` block.
5. Click **Build this roadmap** — the spec is dispatched to the plan-graph runner.
6. Specialists (plan → implement → verify) run with cycle guards + `await_user` escape; their working subdir is propagated across siblings so they don't scavenger-hunt each other's files.
7. When you hit a bug, **Help → Save Diagnostics ZIP** bundles the audit log into a redacted ZIP you can drop into a GitHub issue.

## Testing

```bash
pytest                              # all tests
pytest -m unit                      # fast unit tests
pytest tests/test_mission.py        # one module
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for module-by-module reference.

```
resonant_client/
├── backends.py              # OllamaBackend (single backend; v0.4.0 cut Anthropic / OpenAI / etc.)
├── events.py                # EngineEvent / ClientCommand enums
├── protocol.py              # Tool prompt building, JSON/XML parsing
├── tui.py                   # Terminal UI
├── network_defaults.py      # Mac Studio Ollama URL resolution chain
├── engine/
│   ├── session.py           # Agentic loop, cycle guards, await_user dispatch
│   ├── tools.py             # Tool definitions + execution routing
│   ├── sandbox.py           # Permission levels and tool allowlists
│   ├── browser.py           # Playwright browser tools
│   ├── computer.py          # Desktop automation
│   └── ...
├── orchestration/
│   ├── plan_graph.py        # PlanNode (with working_subdir field)
│   ├── runner.py            # LocalSpecialistRunner + working_subdir propagation
│   ├── walker.py            # GraphWalker
│   └── specialists.py       # plan / implement / verify / repair / research / explore
└── gui/
    ├── app.py               # Starlette + WebSocket
    ├── diagnostics.py       # Help → Save Diagnostics bundle (redacted logs ZIP)
    ├── sessions.py          # ProjectManager + safe-default project path
    ├── settings.py          # ~/.resonant/settings.json
    ├── templates/index.html # Frameless desktop app shell
    └── static/{app.js,styles.css}
```

## v0.4.0 — what changed

This release narrowed Resonant Client to a single backend (Ollama). The Anthropic, OpenAI, Claude Code, Codex, Resonant Engine, MLX, and LM Studio backends were removed along with their dependencies and UI surface. Roughly 1,700 lines of backend code went away; the model-selector dropdown collapsed from a multi-section grouped list to a flat picker with DeepSeek flagship variants pinned to the top; the Settings UI lost its API-key fields; and the welcome screen gained an Ollama setup wizard for first-run users.

If you were using a non-Ollama backend through Resonant Client, v0.4.0 is a hard cut — there's no migration path because the upstream tools (Claude Code, Codex) are better at their respective stacks anyway. v0.3.5 remains downloadable on the [Releases page](https://github.com/Luminary-Analytics/resonant-client/releases/tag/v0.3.5) if you need the old multi-backend behavior.

LM Studio support may return as a future addition once the Ollama path is fully tuned.

## License

MIT — see [LICENSE](./LICENSE). Resonant Client is open source: an open-source flagship for local-first, self-improving autonomous coding. Contributions welcome.
