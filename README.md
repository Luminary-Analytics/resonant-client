# Lumi

The coding agent by Luminary Analytics, formerly SONN Client and originally
Resonant. It runs on the model endpoints you choose, including SONN. The
rebrand keeps working setups working: `resonant` commands remain aliases,
`RESONANT_*` environment variables are still read, and `~/.resonant` moves to
`~/.lumi` on first launch. See [Unreleased](docs/unreleased.md).

**A provider-adaptive multimodal coding agent for local and hosted models.**

Lumi gives different model providers the same durable coding harness:
repository-aware system prompts, native tools, focused clarification, long-task
state, verification, and a desktop workflow with projects and sessions in one sidebar. Product
behavior is capability-driven; named models are not silently promoted or given
different operating rules.

See [docs/agentic-harness-north-star.md](docs/agentic-harness-north-star.md)
for the engineering contract that governs harness changes.

Start with the [desktop workflow](docs/desktop-workflow.md) for navigation and
provider selection, or the [documentation index](docs/README.md) for contributor
guides. [0.19.1](docs/v0.19.1-release-notes.md) fixes Codex live progress and completion evidence;
[Unreleased](docs/unreleased.md) tracks subsequent changes.

Version 0.19.0 adds a bottom-left SONN account menu, local display
name, and optional Echo companion. Settings opens a dedicated searchable category
sidebar with focused pages for preferences, connections, and integrations.

## Provider Support

- **Anthropic:** Claude through the Messages API with native tools, extended
  thinking (signed thinking replayed within a tool loop), prompt caching and
  cache-aware usage. Also on Amazon Bedrock and Google Vertex AI.
- **OpenAI:** GPT and o-series models through the Responses API, stateless
  (`store: false`) with encrypted reasoning carried between tool calls. Also on
  Azure OpenAI.
- **Custom connections:** any OpenAI-compatible endpoint (LiteLLM, vLLM, an
  internal gateway), Azure OpenAI, or Claude on Bedrock or Vertex, defined in
  **Settings > Connections** without code.
- **Provider extensions:** a capability pack can add any other model provider
  as a separate program, written against the Extension SDK without Lumi's
  source. See [Extensions](docs/extensions.md).
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
roles. Lumi's engine contract stays model-neutral. Installed CLI adapters
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
- Built-in Blender, Unity, and Unreal Engine 5 connection setup through community
  MCP bridges ([setup and validation](docs/creative-editors.md))
- Skills, plugins, LSP status, and project instructions
- Telegram and Slack: send requests, approve actions and stop work from the
  chat ([chat gateway](docs/chat-gateway.md))
- VS Code and JetBrains IDEs: send the selection or files into your message,
  and open Lumi's changes beside your files ([code editors](docs/code-editors.md))
- Jira, Linear, GitHub and GitLab issues: attach one with `@issue:ENG-12`, and
  have the agent comment on it ([issue trackers](docs/issue-trackers.md))
- Hand work to a teammate through Lumi Cloud, or to a CI run with
  `lumi run --handoff` ([hand-offs](docs/hand-offs.md))
- Your organization's skills and prompts, published and versioned in Lumi
  Cloud ([team library](docs/team-library.md))
- Agent changes that wait for a named reviewer: no merging or pushing to the
  default branch, reviewers requested, a review queue ([code review](docs/code-review.md))
- Risky commands your organization lists wait for a second person's approval
  in Lumi Cloud ([second-person approval](docs/second-approval.md))
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
explicit user configuration; Lumi does not silently switch models by role.

## Install

### Windows installer

Download the latest `lumi-setup-X.Y.Z.exe` from the
[Lumi download page](https://luminary-analytics.github.io/resonant-client/).

- Installs without an administrator prompt
- Adds a Start Menu shortcut
- Checks the signed appcast for future updates. **Settings > Updates** picks
  the stable or beta channel, pins a release line, or checks only when asked or
  never; see [Updates](docs/updates.md)

IT departments can deploy the MSI package (`lumi-X.Y.Z.msi`) silently per
machine through Intune, Configuration Manager or Group Policy; see
[Deploying on Windows](docs/deploy-windows.md).

A macOS build (`Lumi.app` in a DMG, Apple silicon) is built in CI but not yet
released; see [Lumi on macOS](docs/macos.md).

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

### Anthropic

Add an Anthropic key under **Settings > API keys**, or set `ANTHROPIC_API_KEY`,
then use **Settings > Connections > Check connection & refresh models**. Lumi
lists the models the key can use. The thinking selector sets an extended-thinking
budget (off, low, med, high, max). Usage is billed to your Anthropic account.

### OpenAI

Add an OpenAI key under **Settings > API keys**, or set `OPENAI_API_KEY`. Lumi uses
the Responses API without storing conversations at OpenAI, and maps the thinking
selector to reasoning effort. This is separate from signing in with ChatGPT for
Codex and is billed to the API account.

### Custom connections

**Settings > Connections > Custom connections** adds an endpoint without code:

- **OpenAI-compatible:** a base URL ending in `/v1` (LiteLLM, vLLM, an internal
  gateway), a bearer token, a key in a custom header, or no key; extra headers;
  and models listed by hand or discovered from `/models`.
- **Azure OpenAI:** `https://NAME.openai.azure.com/openai/v1`, an `api-key` or
  Microsoft Entra ID sign-in, and your deployment names as models.
- **Claude on Amazon Bedrock:** a region and model or inference-profile ids. Lumi
  signs requests with your AWS credentials (environment, profile or SSO through
  botocore when installed) or uses a Bedrock API key.
- **Claude on Google Vertex AI:** a project, region and model ids, using Google
  Application Default Credentials (google-auth or the gcloud CLI).
- **Anthropic or OpenAI Responses proxies:** the same APIs at another URL.
- **A provider from a capability pack:** a model provider an approved personal
  pack runs as a program ([Extensions](docs/extensions.md)). Lumi starts it for
  each request and passes the connection's key to it.

Instead of a key, a connection can sign in with OAuth client credentials (a
gateway behind Okta, Auth0, Keycloak or Entra ID) or, for Azure OpenAI, with
Microsoft Entra ID: an app registration, or this computer's `az login`. Gateways
that require mutual TLS take a client certificate. See
[Signing in to enterprise model endpoints](docs/connection-sign-in.md).

**Test connection** checks credentials and lists models before saving. Each
connection appears in **Models** under its own name. Connection keys are kept
with your other API keys and never returned to the page. HTTP (not HTTPS) is
accepted only for localhost and private-network endpoints.

### Ollama

Install [Ollama](https://ollama.com/download), start it, and pull a model:

```bash
ollama serve
ollama pull your-model
```

Lumi probes `http://127.0.0.1:11434` by default. Set `OLLAMA_HOST` or use
**Settings > Network** for a remote endpoint.

### EXO

Lumi connects directly to EXO's OpenAI-compatible API. The bundled default
is `http://127.0.0.1:52415/v1`; change **Settings > Network > EXO OpenAI API
URL** or set `EXO_API_URL` for another cluster.

The model picker orders running models first, downloaded models second, and the
remaining EXO catalog after them. When a downloaded model is selected but not
running, Lumi requests the first valid EXO placement and waits for the
instance to become ready before starting the turn. Tool calls, streaming usage,
and OpenAI-format image content use the same agent runtime as other providers.

### Kimi

Create a key in the [Kimi API platform](https://platform.kimi.ai/), then add it
under **Settings > API keys** or set `MOONSHOT_API_KEY`.

### Codex

Install the Codex CLI, then open **Settings > Connections > Sign in with ChatGPT**.
Follow the browser link and select **Refresh account & models** after signing in.
Lumi displays the connected account, its available models, and remaining
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
Lumi discovers models that support text output and tools from the
[OpenRouter catalog](https://openrouter.ai/docs/quickstart), excluding batch variants.
Streaming, native tool calls, reasoning continuation data, and provider-reported
costs use OpenRouter's API. API keys are kept in your system's credential store
(see [Keys, network and privacy](#keys-network-and-privacy)) and are masked in the
settings UI and session configuration.

Search **Astra** in **Models** to select `openai/gpt-6-astra` through OpenRouter.
Its API usage is billed separately from ChatGPT/Codex subscription usage.

### Claude Code

Install and authenticate Claude Code, then select its discovered CLI models in
the model picker. Lumi uses the installed CLI's native execution path.
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

## Keys, network and privacy

- **API keys** saved in Settings are kept in Windows Credential Manager, the
  macOS Keychain or your Linux keyring (service `Lumi`); `settings.json` holds
  only a placeholder. Where no credential store exists, keys stay in
  `~/.lumi/settings.json` and Settings says so. Set `LUMI_KEYCHAIN=off` to keep
  them in the file.
- **Commands the agent runs**, hooks and MCP servers start without Lumi's
  model-provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and similar). An MCP
  server still receives the variables its own entry sets.
- **Before each model request**, the values of your saved keys are removed from
  tool output. Turn on **Settings > Privacy & security > Scan for secrets** to
  also remove well-known credentials (cloud keys, tokens, private keys,
  passwords in connection strings and `.env` files) from tool output and your
  messages. The model sees `[REDACTED ...]` instead, and the chat notes what was
  removed. Codex and Claude Code read files with their own tools and are not
  scanned.
- **Corporate networks:** TLS is verified with your operating system's
  certificate store, so a company root certificate works. Set a proxy and hosts
  that bypass it under **Settings > Connections > Network**, or use
  `HTTPS_PROXY`/`NO_PROXY`. Local addresses always connect directly. Proxies that
  need a user name and password aren't supported yet.
- **Save diagnostics** removes your actual key values and masks secrets in the
  bundled `settings.json` before anything is written.
- **Files Lumi never reads:** gitignore-style patterns under **Settings >
  Privacy & security** (for example `.env`, `*.pem`, `secrets/**`), plus a
  project's own `.lumiignore`. The file tools refuse them, and searches, git
  output, the codebase index and attachments leave them out. Shell commands
  can still open them.
- **Project trust:** a newly opened project's instruction files, committed
  notes, codebase summary and `lumi-policy.json` allow rules (which skip
  approval in Auto-edit) apply only after you choose **Trust this project**.
  Automatic lint and test runs, which execute the project's code, also wait
  for trust.
- **Transcript retention:** set **Delete transcripts after (days)** to remove
  old sessions, drafts, checkpoints, artifacts and logs automatically.
- **Tool switches:** turn off Codex and Claude Code, computer use or the chat
  gateway under **Settings > Privacy & security**.
- **Organization policy:** administrators can lock settings, limit permission
  modes and models, and allowlist MCP servers and capability packs with Group
  Policy, a configuration profile or a policy file. See
  [Organization policy for administrators](docs/enterprise-policy.md).
- **Audit log:** a local, hash-chained record of turns, model usage, tool
  calls, file changes, approvals and settings changes. It keeps metadata
  only unless you choose a content level, can be verified under **Settings >
  Privacy & security**, and can stream to an OpenTelemetry collector. See
  [Audit log](docs/audit-log.md).
- **Usage and cost:** one record per model call, with tokens, cost and
  purpose, priced from what the provider reports, your organization's or
  your own prices, or a dated list of Anthropic and OpenAI prices. Models
  without a price show as unpriced, never $0. Budgets alert, ask before
  continuing, or stop a turn, per day, month, project or turn, from your
  settings or your organization's policy. See **Settings > Usage & cost**
  or run `lumi usage`, and [Usage records and prices](docs/usage-and-costs.md).

## Browser Tools

Browsing is built in. The `browser_*` tools drive your installed Chrome over
the Chrome DevTools Protocol — navigate, click, type, read, screenshot, run
JavaScript, and manage tabs. Nothing to install or configure: Chrome starts on
first use.

Chrome runs under a dedicated Lumi profile (`~/.lumi/browser-profile`)
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
| `LUMI_BROWSER_CDP_PORT` | `9222` | DevTools port |
| `LUMI_BROWSER_CHROME_PATH` | auto-detected | Chrome executable |
| `LUMI_BROWSER_USER_DATA_DIR` | `~/.lumi/browser-profile` | Profile directory |
| `LUMI_BROWSER_HEADLESS` | `0` | Run without a visible window |
| `LUMI_BROWSER_GROUP_TITLE` | `Lumi` | Tab group label |

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
| `LUMI_EXO_CONTEXT_TOKENS` | conservative model inference | Effective EXO deployment context window; set this to the server's actual configured window |
| `LUMI_EXO_CONNECT_TIMEOUT_SEC` | `15` | EXO connection timeout |
| `LUMI_EXO_PROGRESS_WARNING_SEC` | `120` | Informational threshold for showing that a quiet EXO generation is still working; it never stops the run |
| `LUMI_EXO_STREAM_IDLE_TIMEOUT_SEC` | `0` (disabled) | Optional operator-defined hard limit for seconds without semantic EXO progress; long generations are unlimited by default and remain user-stoppable |
| `LUMI_EXO_READ_TIMEOUT_SEC` | `0` (disabled) | Legacy alias for the optional EXO stream idle timeout |
| `LUMI_DEFAULT_BACKEND` | `ollama` | Explicit default provider |
| `LUMI_DEFAULT_MODEL` | auto-discovered | Explicit default model |
| `MOONSHOT_API_KEY` | none | Kimi API key |
| `SONN_API_URL` | none | Complete SONN project API base URL ending in `/openai/v1` |
| `SONN_API_KEY` | none | SONN private invitation key |
| `OPENROUTER_API_KEY` | none | OpenRouter API key |
| `ANTHROPIC_API_KEY` | none | Anthropic API key |
| `OPENAI_API_KEY` | none | OpenAI API key |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` / `AWS_PROFILE` | none | Credentials for Claude on Bedrock connections |
| `AWS_BEARER_TOKEN_BEDROCK` | none | Bedrock API key, used when a Bedrock connection has no key of its own |
| `GOOGLE_APPLICATION_CREDENTIALS` | none | Service account for Claude on Vertex AI connections |
| `LUMI_ANTHROPIC_READ_TIMEOUT_SEC` / `LUMI_OPENAI_READ_TIMEOUT_SEC` | `600` | Stream read timeouts for the Anthropic and OpenAI adapters |
| `MOONSHOT_BASE_URL` | `https://api.moonshot.ai/v1` | Kimi-compatible API URL |
| `LUMI_OLLAMA_NUM_CTX` | capability-derived | Ollama context override |
| `LUMI_OLLAMA_NUM_BATCH` | Ollama default | Optional batch override |
| `LUMI_OLLAMA_NUM_GPU` | Ollama default | Optional GPU layer override |
| `LUMI_OLLAMA_KEEP_ALIVE` | `120m` | Ollama keep-alive |
| `LUMI_OLLAMA_HTTP_TIMEOUT_SEC` | `360` | Ollama request timeout |
| `LUMI_OLLAMA_HTTP_READ_TIMEOUT_SEC` | `300` | Ollama stream read timeout |
| `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` | none | Proxy for outbound traffic and hosts that bypass it; a proxy set in Settings takes precedence |
| `LUMI_KEYCHAIN` | `on` | `off` keeps API keys in `settings.json` instead of the OS credential store |
| `LUMI_KEYCHAIN_SERVICE` | `Lumi` | Service name for keys in the OS credential store |

Persistent configuration lives in `~/.lumi/settings.json` and is managed
through the desktop Settings view.

## Run

```bash
lumi-gui
lumi --backend ollama --model your-model
lumi --ollama-url http://192.168.1.20:11434 --model your-model
```

`lumi-gui --browser` prints a one-time link to open the app in a browser.
From the desktop window, use **File > Open in Browser**. The local server refuses
pages that were not opened from such a link.

`lumi` without a subcommand opens the [terminal UI](docs/terminal-ui.md) in the
current folder. It runs tools without asking unless you pass `--approve`; the
project's rules, project trust and your organization's policy apply as in the
app.

For servers, containers and CI, `lumi run` runs one task without a UI and
prints a JSON result with an exit code a job can act on:

```bash
lumi run "Fix the failing test" --provider anthropic --model claude-sonnet-5 --mode bypass
```

See [Running Lumi without a UI](docs/headless.md), including a GitHub
Actions example and `packaging/docker/Dockerfile`. With a GitHub token, the
agent can read a pull request's reviews and failing checks, open pull
requests, and reply to reviews; see [GitHub pull requests](docs/github.md).

If a model request fails, a turn can continue with fallback models you list,
and a separate model can name sessions and compact long conversations; see
[Fallback models, roles and capabilities](docs/models.md).

## Develop

Read [AGENTS.md](AGENTS.md) for shared contributor instructions. `CLAUDE.md`
imports that guide; `RESONANT.md` remains a legacy pointer to it.

```bash
python -m pytest -q
python -m ruff check .
node --check lumi/gui/static/app.js
node --check lumi/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs tests/appearance.test.cjs
git diff --check
```

The durable runtime architecture and extension contracts are documented in
[docs/modern-agent-runtime.md](docs/modern-agent-runtime.md).
Use [RELEASING.md](RELEASING.md) for clean Windows builds and publishing checks.

The smoke harness accepts either a legacy shorthand or any Ollama model ID:

```bash
lumi-smoke run --spec wordcount --model your-model
lumi-smoke variance --spec wordcount --model your-model --n 3
```

## License

MIT. See [LICENSE](LICENSE).

Managed previews, named acceptance checks, Kimi effort controls, and sourced project notes are described in [Priority improvements](docs/priority-improvements.md).
