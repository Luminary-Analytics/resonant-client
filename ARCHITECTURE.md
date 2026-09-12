# Resonant architecture

Current baseline: [v0.18.1](docs/v0.18.1-release-notes.md), including the compact
toolbar, sidebar, new-session project chooser, and SONN connection. Subsequent work is tracked in [Unreleased](docs/unreleased.md). Contributor rules live in [AGENTS.md](AGENTS.md);
product priorities live in the [harness north star](docs/agentic-harness-north-star.md).

## Runtime boundaries

Resonant is a Python agent runtime with a Starlette/WebSocket GUI, a Rich TUI,
and a pywebview desktop shell. Ollama, EXO, Kimi, OpenRouter, and SONN supply models to
Resonant's engine loop. Codex and Claude Code adapters instead run installed
CLIs, whose native tool execution remains inside those CLIs.

The GUI owns interaction and rendering. Runtime construction owns provider,
project, permission, and context wiring. The engine owns model/tool iteration,
cancellation, durable state, and verification. Optional orchestration composes
these services; it is not required for ordinary chat-based coding.

| Area | Entry points | Responsibility |
| --- | --- | --- |
| Providers | `backends.py`, `openrouter.py`, `sonn.py`, `capabilities.py`, `content.py` | Wire formats, streaming, capability discovery, normalized content |
| Codex connection | `codex_account.py` | App-server lifecycle, account/login/model/quota RPCs |
| Session naming | `engine/session_titles.py`, `gui/session_titles.py` | Immediate task titles and bounded post-turn refinement, guarded against renames/navigation |
| Model context | `engine/model_prompts.py`, `protocol.py`, `engine/compression.py` | Stable prompt, tool schemas/parsing, context compaction |
| Agent loop | `engine/session.py`, `engine/tools.py`, `engine/sandbox.py` | Model/tool iteration, execution, permissions, cancellation |
| GUI server | `gui/app.py`, `gui/ws_commands.py`, `gui/chat_loop.py` | Startup, state, commands, streaming and active-run lifecycle |
| Construction | `gui/runtime.py` | Serializable `BackendSpec`, shared session construction |
| Saved work | `gui/sessions.py`, `gui/session_ledger.py`, `gui/ui_state.py` | Projects, session metadata, transcript ledger, composer drafts |
| Configuration | `gui/settings.py`, `network_defaults.py`, `gui/project_instructions.py` | Settings, endpoint resolution, layered repository instructions |
| Desktop UI | `gui/templates/index.html`, `gui/static/app.js`, `gui/static/styles.css` | Sidebar, composer, model picker, command palette, shell |
| Settings UI | `gui/static/settings_view.js` | Connection flows, API keys, preferences |
| Project resources | `engine/previews.py`, `engine/project_memory.py` | Managed preview servers, sourced project notes |
| Costs and diagnostics | `gui/costs.py`, `gui/diagnostics.py`, `engine/turn_outcomes.py` | Usage/cost display, redacted diagnostics, completion evidence |
| Durable workers | `engine/agents.py`, `engine/agent_runtime.py`, `engine/worktrees.py` | Worker state, execution, isolated writers |
| Context and evidence | `engine/context_broker.py`, `engine/artifacts.py`, `engine/checkpoint_timeline.py`, `engine/flight_recorder.py` | Context attachments, artifacts, rewind, traces |
| Optional orchestration | `orchestration/`, `gui/autonomous_*.py` | Specialists, plan graphs, autonomous iteration, skills |

Paths in the table are relative to `resonant_client/`.

## GUI and saved-work flow

1. Startup sends lightweight project/session metadata before model discovery.
2. Provider probes run off the event loop; saved work and drafts stay usable.
3. The frontend groups sessions by project on one scroll surface. Each expanded
   project shows six rows initially, retaining an active session outside that
   slice. Search filters the full loaded catalog; Show more adds 20 rows.
4. New session asks for an existing project or a folder, then opens a draft
   there. Project-row plus buttons choose their project directly. Cancellation
   preserves the active draft; first-message persistence creates the saved record.
5. `ChatRunLoop` coordinates active runs, queued follow-ups, cancellation, and
   commands. A model switch is rejected while a run is active.
6. The shared live-progress surface is the active
   task card's final child, after activity and response. Completion may offer
   a local next-prompt placeholder; it remains outside persisted drafts until
   the user accepts it. See [0.18.1 notes](docs/v0.18.1-release-notes.md).
7. Engine events travel through a thread-safe queue to WebSocket clients;
   classic JavaScript scripts and descriptor-based mixins render them.

Preserve render signatures, scroll/focus restoration, session-scoped draft
writes, and immediate catalog updates after session mutations. The compact
sidebar uses single-line rows and hover dates; see release status and accessible
control behavior in the [desktop guide](docs/desktop-workflow.md).

## Provider connections and selection

`CodexAccount` owns a managed `codex app-server` subprocess and serializes
JSON-RPC calls for login, account state, paginated model discovery, and quota.
Codex owns credentials. The GUI receives account metadata and a browser login
URL, never authentication tokens. Coding still uses `codex exec --json`.
Account-discovered models survive routine backend probes. Bootstrap models
include `gpt-6-astra`; the connected account catalog determines actual access.

`OpenRouterBackend` uses OpenRouter's chat-completions interface and its model
catalog, cached for five minutes. Catalog filtering requires tools and text
output and excludes batch variants. Tool requests require compatible provider
parameters. Reasoning continuation is replayed only for its originating model;
provider-reported `usage.cost` flows into the GUI cost tracker. Credentials are
resolved from settings/environment, not embedded in `BackendSpec`.

`SonnBackend` uses a configured project URL without modifying its path. It sends
standard Chat Completions messages and top-level function tools, retaining
system summaries without Moonshot or OpenRouter extensions. Authenticated model
discovery is cached for five minutes per URL and credential fingerprint, bounded
to 16 entries. URL/key changes rebuild the active backend after a run stops;
removing credentials disables sending until a valid connection is selected.
Keys are resolved locally and omitted from serialized backend specs and UI
responses. See [SONN contract and limits](docs/sonn.md).

Session metadata takes precedence when reopening a saved conversation. New
sessions use a saved project preference before recent/global defaults. Model
favorites and project preferences are local settings. Selecting a provider in
the composer is manual; this workflow does not add automatic cross-provider
fallback or role routing.

Codex receives project instructions, relevant notes, recent text history, and
retained summaries. This is a text handoff, not native thread continuation;
image attachments are not transferred. CLI tool displays must not be treated
as evidence that Resonant's own tool handlers executed.

## Instructions, notes, and skills

`gui/project_instructions.py` prefers `AGENTS.md`, then `.agents/AGENTS.md`,
`RESONANT.md`, `.resonant/RESONANT.md`, and `CLAUDE.md` at a given scope. Global
and working-directory hierarchy handling live in that module. This repository
uses `AGENTS.md` as its shared source; `CLAUDE.md` imports it.

Project notes retain source descriptions and optional source-file fingerprints.
Stale notes are excluded from recall. Skill catalogs are bounded; full procedures
load on demand. Pins, scope, provenance, suppression, and verification history
are meaningful state, not decoration. See [Priority improvements](docs/priority-improvements.md)
and [self-improvement loop](docs/self-improvement-loop.md).

## Storage, safety, and packaging

Settings normally live in `~/.resonant/settings.json`; project state lives under
`~/.resonant/projects/<project-hash>/`. Skills use `~/.resonant/skills/`.
Explicit project-local configuration files are separate from generated runtime
state. Avoid writing fixtures, secrets, or session data into the repository.

Project selection must reject unsafe system/install defaults. Writer isolation
must preserve dirty checkouts, and cancellation must stop owned subprocess
work. Managed previews survive turns and project switches but are runtime
resources, not persistent deployments. Named acceptance results describe their
actual commands and inputs, not universal proof of correctness.

Windows builds use `scripts/build_clean.ps1`, pinned asset fetches,
`packaging/resonant.spec`, and `packaging/bundle-policy.json`. New UI resources
must be bundled and cache-busted. Source tests do not prove frozen startup or
WebSocket dependencies work; verify the packaged app before releasing.

See [RELEASING.md](RELEASING.md), [prompt architecture](docs/model-prompt-architecture.md),
[durable runtime](docs/modern-agent-runtime.md), and the
[documentation index](docs/README.md) for deeper references.
