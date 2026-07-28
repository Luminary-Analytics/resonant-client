# Resonant Client

**A provider-adaptive multimodal coding agent for local and hosted models.**

Resonant gives different model providers the same durable coding harness:
repository-aware system prompts, native tools, focused clarification, long-task
state, verification, and a desktop workflow modeled after OpenCode. Product
behavior is capability-driven; named models are not silently promoted or given
different operating rules.

See [docs/agentic-harness-north-star.md](docs/agentic-harness-north-star.md)
for the engineering contract that governs harness changes.

## Provider Support

- **Ollama:** the zero-credential local-first default. Models are discovered
  from the configured endpoint.
- **Kimi:** Moonshot's API with native tools, multimodal content, reasoning
  continuity, retries, and cache accounting.
- **Codex:** an installed Codex CLI, using the same project and permission
  boundaries.

Provider adapters may translate wire formats, reasoning tokens, and message
roles. The system prompt, agent loop, clarification policy, and verification
contract stay model-neutral.

## Features

### Agent harness

- Stable model-neutral system prompt with project instruction layering
- Focused Grill Me clarification only when repository evidence cannot resolve a
  consequential ambiguity
- Native `await_user` multiple-choice prompts with a required recommended option
- Long-running checklist, context compression, resumable sessions, and steering
- One-click live health snapshots and non-interrupting agent progress updates
- Tool calling with adaptive text fallback when native tools are unavailable
- Capability-aware context windows, reasoning controls, tools, and vision
- Multimodal attachments with safe handling for text-only models
- Focused and end-to-end verification before completion
- Durable sub-agent registry with transcripts, handoffs, live controls, and
  concurrent worktree-isolated writers
- Universal file/conversation checkpoints with files-only, chat-only, or full rewind
- Reproducible flight recorder with causal trajectory comparison and OTLP export
- Provenance-aware `@file`, `@symbol`, `@diff`, `@checkpoint`, `@agent`, and
  `@artifact` context attachments
- Trusted capability packs combining agents, skills, hooks, MCP, recipes, and UI metadata

### Tools and extensions

- File, search, shell, git, batch, task, skill, and user-input tools
- User-configured MCP servers
- Skills, plugins, LSP status, and project instructions
- Built-in browser control (native CDP) and desktop computer-use tools
- Permission modes and a project-root path sandbox
- Optional codebase indexing, RAG, and Engram memory

### Desktop client

- Native frameless window with project and session navigation
- Folder picker for opening projects
- Runtime provider/model picker without pinned model policy
- Inline file diff review
- Collapsible long-task status with EXO connection/model-progress telemetry
- Recommended decision prompts and a non-interrupting Check status control
- Diagnostics export and cost tracking
- Agents/Timeline/Traces/Artifacts/Packs runtime inspector
- Opt-in Director Mode: use a frontier model to plan, review, and safely integrate work from a selected pool of open or lower-cost worker models
- Signed Windows update feed with in-app update checks

### Optional orchestration

The sprint and autonomous workflows remain optional and off by default. They
provide planner, generator, evaluator, specialist, and recovery flows for users
who need structured long-running execution. Specialist model overrides are
explicit user configuration; Resonant does not silently switch models by role.

## Install

### Windows installer

Download the latest `resonant-setup-X.Y.Z.exe` from the
[Releases page](https://github.com/Luminary-Analytics/resonant-client/releases).

- Installs without an administrator prompt
- Adds a Start Menu shortcut
- Checks the signed appcast for future updates

Windows SmartScreen may show "Unrecognized publisher" for the v0.x line. Code
signing is planned for v1.0.

### Source install

```bash
git clone https://github.com/Luminary-Analytics/resonant-client.git
cd resonant-client

pip install -e .
pip install -e ".[gui]"
pip install -e ".[all,dev]"
```

Python 3.11 or newer is required.

## Configure A Provider

### Ollama

Install [Ollama](https://ollama.com/download), start it, and pull a model:

```bash
ollama serve
ollama pull your-model
```

Resonant probes `http://127.0.0.1:11434` by default. Set `OLLAMA_HOST` or use
**Settings > Network** for a remote endpoint.

### EXO

Resonant connects directly to EXO's OpenAI-compatible API. The bundled default
is `http://127.0.0.1:52415/v1`; change **Settings > Network > EXO OpenAI API
URL** or set `EXO_API_URL` for another cluster.

The model picker orders running models first, downloaded models second, and the
remaining EXO catalog after them. When a downloaded model is selected but not
running, Resonant requests the first valid EXO placement and waits for the
instance to become ready before starting the turn. Tool calls, streaming usage,
and OpenAI-format image content use the same agent runtime as other providers.

### Kimi

Create a key in the [Kimi API platform](https://platform.kimi.ai/), then add it
under **Settings > Kimi API** or set `MOONSHOT_API_KEY`.

### Codex

Install and authenticate the Codex CLI. Resonant detects the executable and
offers the models exposed by that provider adapter.

## Browser Tools

Browsing is built in. The `browser_*` tools drive your installed Chrome over
the Chrome DevTools Protocol — navigate, click, type, read, screenshot, run
JavaScript, and manage tabs. Nothing to install or configure: Chrome starts on
first use.

Chrome runs under a dedicated Resonant profile (`~/.resonant/browser-profile`)
rather than your everyday one, because Chrome locks a profile directory while
it is in use — sharing yours would mean you and the agent could not browse at
the same time. Log into sites once in that window and the session persists.

Agent tabs are collected into a labelled Chrome tab group so they are easy to
tell apart from your own. That group is created by a small bundled extension:
tab groups are the `chrome.tabGroups` extension API and are not reachable
through the DevTools protocol.

No Playwright and no bundled Chromium — CDP is JSON-RPC over a WebSocket, so
this adds nothing to the installer.

| Variable | Default | Purpose |
|---|---|---|
| `RESONANT_BROWSER_CDP_PORT` | `9222` | DevTools port |
| `RESONANT_BROWSER_CHROME_PATH` | auto-detected | Chrome executable |
| `RESONANT_BROWSER_USER_DATA_DIR` | `~/.resonant/browser-profile` | Profile directory |
| `RESONANT_BROWSER_HEADLESS` | `0` | Run without a visible window |
| `RESONANT_BROWSER_GROUP_TITLE` | `Resonant` | Tab group label |

[BrowserOS](https://github.com/browseros-ai/BrowserOS) and other browser MCP
servers still work if you prefer them. Enable **Settings > MCP Servers >
browseros** and paste the URL from `chrome://browseros/mcp`; it ships disabled
now that browsing works out of the box.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama base URL |
| `EXO_API_URL` / `EXO_BASE_URL` | `http://127.0.0.1:52415/v1` | EXO OpenAI-compatible API URL |
| `EXO_API_KEY` | none | Optional bearer token for an authenticated EXO proxy |
| `RESONANT_EXO_CONNECT_TIMEOUT_SEC` | `15` | EXO connection timeout |
| `RESONANT_EXO_PROGRESS_WARNING_SEC` | `120` | Informational threshold for showing that a quiet EXO generation is still working; it never stops the run |
| `RESONANT_EXO_STREAM_IDLE_TIMEOUT_SEC` | `0` (disabled) | Optional operator-defined hard limit for seconds without semantic EXO progress; long generations are unlimited by default and remain user-stoppable |
| `RESONANT_EXO_READ_TIMEOUT_SEC` | `0` (disabled) | Legacy alias for the optional EXO stream idle timeout |
| `RESONANT_DEFAULT_BACKEND` | `ollama` | Explicit default provider |
| `RESONANT_DEFAULT_MODEL` | auto-discovered | Explicit default model |
| `MOONSHOT_API_KEY` | none | Kimi API key |
| `MOONSHOT_BASE_URL` | `https://api.moonshot.ai/v1` | Kimi-compatible API URL |
| `RESONANT_OLLAMA_NUM_CTX` | capability-derived | Ollama context override |
| `RESONANT_OLLAMA_NUM_BATCH` | Ollama default | Optional batch override |
| `RESONANT_OLLAMA_NUM_GPU` | Ollama default | Optional GPU layer override |
| `RESONANT_OLLAMA_KEEP_ALIVE` | `120m` | Ollama keep-alive |
| `RESONANT_OLLAMA_HTTP_TIMEOUT_SEC` | `360` | Ollama request timeout |
| `RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC` | `300` | Ollama stream read timeout |

Persistent configuration lives in `~/.resonant/settings.json` and is managed
through the desktop Settings view.

## Run

```bash
resonant-gui
resonant --backend ollama --model your-model
resonant --ollama-url http://192.168.1.20:11434 --model your-model
```

## Develop

```bash
pytest -q
ruff check resonant_client tests
node --check resonant_client/gui/static/app.js
```

The durable runtime architecture and extension contracts are documented in
[docs/modern-agent-runtime.md](docs/modern-agent-runtime.md).

The smoke harness accepts either a legacy shorthand or any Ollama model ID:

```bash
resonant-smoke run --spec wordcount --model your-model
resonant-smoke variance --spec wordcount --model your-model --n 3
```

## License

MIT. See [LICENSE](LICENSE).
