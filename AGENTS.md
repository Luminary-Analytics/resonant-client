# Contributor instructions

SONN Client (formerly Resonant) is an open-source, provider-adaptive coding agent and desktop app.
Read this file first, then the documentation relevant to the change. This is the
shared repository guide for coding agents; `CLAUDE.md` and `RESONANT.md` point here.

## Product and architecture

- Use **SONN Client** in product copy. Keep existing `resonant-client` package,
  repository, executable compatibility strings, and updater identifiers unless
  a migration is part of the task.
- Follow the [harness north star](docs/agentic-harness-north-star.md): correct
  completion, verification, maintainability, and time to a trustworthy result
  come before token efficiency.
- Keep behavior capability-driven. Ollama, EXO, Kimi, OpenRouter, and SONN adapters
  translate provider protocols into the engine contract. Codex and Claude Code
  run their own CLI tool loops; do not claim identical native tool behavior.
- Preserve explicit model choices. Account discovery may update available
  models, but adding a model must not silently change a user's default.
- SONN uses a user-configured project URL, `sonn-auto`, and standard Chat
  Completions. Preserve the complete URL path; never commit project credentials
  or infer vision/reasoning support from the routing alias. See [SONN](docs/sonn.md).
- ChatGPT/Codex and OpenRouter are separate connections. Codex owns its login
  credentials; OpenRouter uses a separately billed API key. Never expose secrets
  in UI responses, diagnostics, fixtures, or serialized `BackendSpec` values.

## Working in the codebase

- Python 3.11+; follow existing type hints, public docstrings, and module style.
  Explain non-obvious constraints in comments. Keep changes focused.
- Engine/tool behavior belongs in `engine/`, not frontend handlers. The GUI
  consumes engine events and sends commands through `gui/ws_commands.py`.
- Reuse `gui/runtime.py` and `BackendSpec` for session/backend construction.
  Preserve project instructions, permissions, history, and source-of-key settings.
- The frontend uses classic scripts and descriptor-based mixins. Do not convert
  one file to ES modules without updating the loading/build contract. New assets
  must be included by `packaging/resonant.spec` and the bundle policy as needed.
- Inspect the working tree before editing; preserve unrelated changes. Use
  isolated fixture projects and state for evaluation, not personal sessions.
- Keep runtime state out of the repository: normally `~/.resonant/projects/`
  for sessions, ledgers, notes, workers, checkpoints, artifacts, and worktrees.

## Behavior to preserve

- A single sidebar groups sessions under named projects. Search covers projects,
  paths, and session titles. Rendering is bounded per expanded project; preserve
  active-session visibility, scroll position, and keyboard focus.
- New session first offers existing projects and a folder picker; project-row
  plus buttons use that project directly. Cancel preserves the active draft.
  After choosing a project, start a draft there; do not persist empty
  conversations until the first message. Drafts remain scoped by project/session.
- Live working status follows the active turn output; preserve manual scroll
  position when the user reads older messages. Next-prompt suggestions are
  transient, scoped to the conversation, and never replace typed drafts. Tab
  accepts into the composer without sending; replay and cancelled/error turns
  must not suggest work.
- Automatic session titles summarize the first prompt. Preserve manual names;
  background title results must be scoped to the original record and yield to
  new coding work. Never start a CLI tool loop solely to name a session.
- Saved conversations retain their provider/model. A saved project preference
  applies to new sessions. Provider changes require a stopped or finished run.
- Deliver saved navigation before provider discovery. Network/account refreshes
  must not block the UI event loop. Preserve discovered account models on probes.
- Settings use a dedicated searchable category sidebar while open; preserve
  session drafts, saved sidebar/preview layout, and fields being edited during
  background refreshes. Search labels/help, never credentials or account data.
  Load remote settings data only for the selected page.
- Settings live in the bottom-left profile menu, with keyboard/application-menu
  fallbacks. Keep the local display name separate from the authenticated SONN identity and
  fetch account details on demand through SONN’s verified workspace API. ChatGPT
  identity must never supply the SONN profile. Echo is optional, respects reduced motion,
  and must not introduce model calls, polling, or completion claims.
- Use accessible names, tooltips, visible keyboard focus, and reliable targets
  for icon buttons. Session dates are hover details in the compact sidebar;
  retain working/needs-input states and pinned-session visibility.
- Codex receives a text handoff of instructions, project notes, recent history,
  and retained summaries. It does not receive the original native provider
  session or image attachments through that handoff.
- OpenRouter uses its own tool/message format, capability catalog, and reported
  costs. Preserve reasoning continuation only for the originating model.
- Never silently use a system/install directory as the project. Respect the
  sandbox and permission modes; writer worktrees must not reset, stash, or merge
  over a dirty user checkout. `working_subdir` may narrow, never broaden, scope.
- Keep cancellation and user input live. Report completion only after work and
  relevant checks finish. Named checks, screenshots, mock responses, and live
  model runs provide different evidence; describe which was actually exercised.
- Project notes need provenance; stale source fingerprints exclude them from
  recall. Preserve bounded skill retrieval and explicit pin/suppression policy.
- Creative editor connections are opt-in managed MCP profiles. Preserve separate
  bridge connectivity and scene-check evidence; never infer the active editor
  project from a connected server. Editor scripts execute outside the path
  sandbox. CLI editor handoff is per process and Full-auto only; never write
  global Codex/Claude configuration. See [creative editors](docs/creative-editors.md).

## Validation

Install development dependencies with `python -m pip install -e ".[all,dev]"`.
Run checks appropriate to the change; do not add tests that merely repeat CSS
or markup. For UI changes, exercise actual controls in a browser, including
keyboard interaction and relevant compact layouts.

```sh
python -m ruff check .
python -m pytest -q
node --check resonant_client/gui/static/app.js
node --check resonant_client/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs
git diff --check
```

Use `scripts/build_clean.ps1` for Windows release builds. Verify the packaged
executable, HTTP UI, WebSocket connection, startup logs, and changed packaged
assets. Stop only fixture processes you started. See [RELEASING.md](RELEASING.md)
for publishing and update-feed verification.

## Documentation and releases

Update user-facing instructions and architecture contracts with behavior
changes. Keep versioned release notes factual; pending changes belong in
[Unreleased](docs/unreleased.md). Do not rewrite historical test results as
current validation or treat old plans as implementation instructions.

Use [documentation status](docs/documentation-status.md) to distinguish active
guides from historical designs and evaluations. Update active guidance against
source and tests; mark superseded documents with replacement links. Preserve
historical dates and results instead of making an old report appear revalidated.
New provider integrations require a verified API contract, endpoint, authentication
configuration, streaming/tool semantics, and capability behavior. Do not infer
a new service's protocol from an old provider name or archived engine plan.

Commit/push/deploy when requested. Release version changes belong in both
`pyproject.toml` and `resonant_client/__init__.py`; publish a matching tag and
verify the release workflow and public appcast before reporting deployment.

Start with [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the
[documentation index](docs/README.md). Detailed references include the
[desktop workflow](docs/desktop-workflow.md), [prompt architecture](docs/model-prompt-architecture.md),
[durable runtime](docs/modern-agent-runtime.md), and
[notes, previews, and acceptance checks](docs/priority-improvements.md).
