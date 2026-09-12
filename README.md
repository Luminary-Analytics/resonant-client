# Resonant

**A provider-adaptive multimodal coding agent for local and hosted models.**

Resonant gives different model providers the same durable coding harness:
repository-aware system prompts, native tools, focused clarification, long-task
state, verification, and a desktop workflow with projects and sessions in one sidebar. Product
behavior is capability-driven; named models are not silently promoted or given
different operating rules.

See [docs/agentic-harness-north-star.md](docs/agentic-harness-north-star.md)
for the engineering contract that governs harness changes.

Start with the [desktop workflow](docs/desktop-workflow.md) for navigation and
provider selection, or the [documentation index](docs/README.md) for contributor
guides. [0.18.2](docs/v0.18.2-release-notes.md) improves live progress, session titles, and follow-up prompts;
[Unreleased](docs/unreleased.md) tracks subsequent changes.

## Provider Support

- **Ollama:** the zero-credential local-first default. Models are discovered
  from the configured endpoint.
- **EXO:** distributed inference through its OpenAI-compatible endpoint, with
  running/downloaded model discovery and instance startup.
- **Kimi:** Moonshot's API with native tools, multimodal content, reasoning
  continuity, retries, and cache accounting.
- **OpenRouter:** a searchable model catalog, native tools, and provider-reported API costs.
- **SONN:** project-scoped Chat Completions with `sonn-auto`, model discovery, and native function tools. See [SONN setup](docs/sonn.md).
- **Codex:** an installed Codex CLI, using the same project and permission
  boundaries.
- **Claude Code:** an installed CLI adapter for existing Claude Code users.

Provider adapters may translate wire formats, reasoning tokens, and message
roles. Resonant's engine contract stays model-neutral. Installed CLI adapters
use their own native tool loops; see the [architecture guide](ARCHITECTURE.md)
for their context-handoff and verification boundaries.

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
- Built-in browser control (native CDP) and desktop computer use, with an
  on-screen indicator while the agent drives the machine
- Permission modes and a project-root path sandbox
- Optional codebase indexing, RAG, and Engram memory

### Desktop client

- Native frameless window with sessions grouped under projects in one sidebar
- Project/session filtering, pinned scope, and a global command palette
- New-session project chooser with searchable existing projects and a folder picker
- Drafts become saved conversations under the chosen project on the first message
- Searchable provider/model picker, favorites, and explicit project defaults
- Inline file diff review
- Collapsible long-task status with EXO connection/model-progress telemetry
- Recommended decision prompts and a non-interrupting Check status control
- Diagnostics export and cost tracking
- Standard agentic workflow with tools, MCP integrations, and bounded task delegation
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
under **Settings > API keys** or set `MOONSHOT_API_KEY`.

### Codex

Install the Codex CLI, then open **Settings > Connections > Sign in with ChatGPT**.
Follow the browser link and select **Refresh account & models** after signing in.
Resonant displays the connected account, its available models, and remaining
subscription usage when Codex reports it. Existing Codex API-key authentication
is labeled separately because it is billed separately from a ChatGPT subscription.
Credentials remain managed by Codex. This uses the official
[Codex app-server account protocol](https://learn.chatgpt.com/docs/app-server)
for connection management and the installed CLI for coding runs.

GPT-6 Astra uses the model ID `gpt-6-astra`. Refresh account models to see the
connected account's catalog; bootstrap model entries do not guarantee access.

### SONN

Enter your project API base URL under **Settings > Network** and your private
invitation key under **Settings > API keys**. Use **Connections > Check SONN
connection & refresh models**, then select `sonn-auto` in **Models**. Connection
checks do not change your selected provider. See [SONN setup and validation
boundaries](docs/sonn.md).

### OpenRouter

Add an OpenRouter key under **Settings > API keys**, or set `OPENROUTER_API_KEY`.
Use **Settings > Connections > Check connection & refresh models** to verify it.
Resonant discovers models that support text output and tools from the
[OpenRouter catalog](https://openrouter.ai/docs/quickstart), excluding batch variants.
Streaming, native tool calls, reasoning continuation data, and provider-reported
costs use OpenRouter's API. API keys are stored locally in `~/.resonant/settings.json`
and are masked in the settings UI and session configuration.

Search **Astra** in **Models** to select `openai/gpt-6-astra` through OpenRouter.
Its API usage is billed separately from ChatGPT/Codex subscription usage.

### Claude Code

Install and authenticate Claude Code, then select its discovered CLI models in
the model picker. Resonant uses the installed CLI's native execution path.
ChatGPT connection controls apply to Codex, not Claude Code.

### Choosing providers per session

Click **Models** beside the composer to search across connected providers and
star favorites. The adjacent quick selector keeps favorites and a small set of
models immediately available. Model prices in the picker are catalog prices;
reported run costs can differ due to caching or provider routing.

Check **Use for new sessions in this project** when selecting a model to save a
project default. Leave it unchecked for a session-only override. New sessions
return to the project default; saved conversations retain their own selection.
Changing models preserves conversation history and drafts. Project instructions
and relevant project notes are included in both provider paths; Codex receives a
text handoff with recent history and retained conversation summaries, rather than
the original provider's native session. Image attachments in that text handoff
are not transferred to Codex.

Provider changes are manual and require the current run to finish or stop.
There is no automatic cross-provider fallback or role routing in this workflow.

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
| `RESONANT_EXO_CONTEXT_TOKENS` | conservative model inference | Effective EXO deployment context window; set this to the server's actual configured window |
| `RESONANT_EXO_CONNECT_TIMEOUT_SEC` | `15` | EXO connection timeout |
| `RESONANT_EXO_PROGRESS_WARNING_SEC` | `120` | Informational threshold for showing that a quiet EXO generation is still working; it never stops the run |
| `RESONANT_EXO_STREAM_IDLE_TIMEOUT_SEC` | `0` (disabled) | Optional operator-defined hard limit for seconds without semantic EXO progress; long generations are unlimited by default and remain user-stoppable |
| `RESONANT_EXO_READ_TIMEOUT_SEC` | `0` (disabled) | Legacy alias for the optional EXO stream idle timeout |
| `RESONANT_DEFAULT_BACKEND` | `ollama` | Explicit default provider |
| `RESONANT_DEFAULT_MODEL` | auto-discovered | Explicit default model |
| `MOONSHOT_API_KEY` | none | Kimi API key |
| `SONN_API_URL` | none | Complete SONN project API base URL ending in `/openai/v1` |
| `SONN_API_KEY` | none | SONN private invitation key |
| `OPENROUTER_API_KEY` | none | OpenRouter API key |
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

Read [AGENTS.md](AGENTS.md) for shared contributor instructions. `CLAUDE.md`
imports that guide; `RESONANT.md` remains a legacy pointer to it.

```bash
python -m pytest -q
python -m ruff check .
node --check resonant_client/gui/static/app.js
node --check resonant_client/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs
git diff --check
```

The durable runtime architecture and extension contracts are documented in
[docs/modern-agent-runtime.md](docs/modern-agent-runtime.md).
Use [RELEASING.md](RELEASING.md) for clean Windows builds and publishing checks.

The smoke harness accepts either a legacy shorthand or any Ollama model ID:

```bash
resonant-smoke run --spec wordcount --model your-model
resonant-smoke variance --spec wordcount --model your-model --n 3
```

## License

MIT. See [LICENSE](LICENSE).

Managed previews, named acceptance checks, Kimi effort controls, and sourced project notes are described in [Priority improvements](docs/priority-improvements.md).
