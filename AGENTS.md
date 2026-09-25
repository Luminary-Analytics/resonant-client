# Contributor instructions

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

Lumi (formerly SONN Client, originally Resonant) is a provider-adaptive coding
agent and desktop app.
Read this file first, then the documentation relevant to the change. This is the
shared repository guide for coding agents; `CLAUDE.md` and `RESONANT.md` point here.

## AI Employee integration boundaries

The user paused AI Employee work to resume later. Read the
[client handoff](docs/ai-employees-handoff.md) and its linked product PRD/handoff
before continuing. The associated heartbeat is paused; documentation maintenance
does not resume implementation or paid qualification. Existing task/advice/panel/
worker changes are integrated source-only; the running bundle remains unchanged.
Preserve the dirty checkout and historical source-integration receipts.

The next outcome-integration seam must bind each native action to its originating
model request. Final-node success, ordinary client-reported grades and private
qualification results must not become blanket route-training credit. Workers keep
one-node capabilities, file-only tools, isolated epochs and private journals; never
give them owner credentials or independent-verifier authority. Automatic supervision,
host enrollment and actual packaged/learned-benefit qualification remain open.

## Product and architecture

- Use **Lumi** in product copy and new identifiers (`lumi` package, commands,
  executable, `~/.lumi`, `.lumi/`, `LUMI_*`). SONN is the separate model
  service; keep its name for SONN connections, accounts and the SONN
  conversation id. Pre-rebrand locations and names (`~/.resonant`,
  `.resonant/`, `RESONANT_*`, `resonant*` commands, `resonant-pack.json`,
  `resonant-policy.json`) are still read; do not write new state under them.
  The update feed URL and repository name stay until a bridge release moves
  the feed (see [Unreleased](docs/unreleased.md)).
- Follow the [harness north star](docs/agentic-harness-north-star.md): correct
  completion, verification, maintainability, and time to a trustworthy result
  come before token efficiency.
- Keep behavior capability-driven. Anthropic, OpenAI, Ollama, EXO, Kimi, OpenRouter,
  SONN and custom-connection adapters translate provider protocols into the engine
  contract. Connections are validated data (`lumi/connections.py`), not code per
  provider; their keys live in `api_keys` as `conn_<id>`. A connection that
  signs in (`auth_tokens.py`) sends its client secret only to the token
  endpoint, and an app registration never falls back to the computer's
  sign-in. Codex and Claude Code run their own CLI tool loops; do not claim
  identical native tool behavior.
- Preserve explicit model choices. Account discovery may update available
  models, but adding a model must not silently change a user's default.
- SONN uses a user-configured project URL, `sonn-auto`, and standard Chat
  Completions. Preserve the complete URL path; never commit project credentials
  or infer vision/reasoning support from the routing alias. See [SONN](docs/sonn.md).
- Preserve saved SONN conversation identity across ordinary turns and reloads.
  Auxiliary title/compression requests and generated lifecycle/repair messages
  must not become human learning input. Keep actual tool observations distinct.
  Malformed-summary recovery requires a readable complete transcript archive and
  factual retained evidence. Never replace a provider error with format recovery
  or infer success from compaction.
  Test graphical features through real browser events, alongside backend checks;
  repeated edit reversals require diagnostic evidence, not weaker assertions.
- ChatGPT/Codex and OpenRouter are separate connections. Codex owns its login
  credentials; OpenRouter uses a separately billed API key. Never expose secrets
  in UI responses, diagnostics, fixtures, or serialized `BackendSpec` values.
- API keys live in the OS credential store; `settings.json` keeps `__keychain__`.
  Read keys only through `settings.get("api_keys", name)`. Never enable the store
  for the legacy `~/.resonant` folder. Children that run code Lumi doesn't
  control (the agent's shell, hooks, MCP servers, jobs, previews, checks) get
  `secrets_store.child_env()`; the CLI backends keep their environment.
  `secret_scan` removes saved key values from tool output before each request.
  Tests and fixtures use `LUMI_KEYCHAIN=off` or an in-memory keyring, never the
  real credential store.
- File exclusions (`engine/exclusions.py`) are enforced at
  `Session._prepare_workspace_tool_args` and inside the listing tools. Any new
  path that reads project files for the model must check `session.exclusions`.
- Repository-provided instructions, notes, index summaries, policy `allow`
  rules and automatic lint/test runs require project trust
  (`gui/workspace_trust.py`). Repository content must never grant itself trust.
- Organization policy (`policy.py`) outranks user settings, repositories and
  tiers: read settings through `SettingsManager.get` (which applies locked
  values), and check `policy.current()` where a new model, mode, MCP server,
  pack or shell path is chosen. User-writable locations must never replace a
  machine policy, and an invalid policy blocks requests instead of vanishing.
- Every agent turn goes through `Session.run`, which records its events in
  the audit log (`audit.py`); run new entry points through it rather than
  `_run_turn`. Record content only through `audit.content` (capture levels)
  and paths through `audit.name`; never record setting or key values.
- Model calls outside a turn go through `engine/request_purpose.auxiliary_stream`
  with a purpose, so `usage.py` records them. Prices come from `pricing.py`;
  a model without a known price is unpriced (`None`), never $0. Budgets
  (`budgets.py`) are checked before every model request of a turn; a new
  loop that calls a model repeatedly must check them too.
- `lumi run` (`headless.py`) builds its session from the same pieces as the
  app: `engine/policies.project_execution_policy`, `ExclusionRules`, workspace
  trust and policy checks. Keep the two in step, and never let a headless run
  trust a repository unless it was trusted in the app or `--trust-project` is set.

## Working in the codebase

- Python 3.11+; follow existing type hints, public docstrings, and module style.
  Explain non-obvious constraints in comments. Keep changes focused.
- Engine/tool behavior belongs in `engine/`, not frontend handlers. The GUI
  consumes engine events and sends commands through `gui/ws_commands.py`.
- Reuse `gui/runtime.py` and `BackendSpec` for session/backend construction.
  Preserve project instructions, permissions, history, and source-of-key settings.
- The frontend uses classic scripts and descriptor-based mixins. Do not convert
  one file to ES modules without updating the loading/build contract. New assets
  must be included by `packaging/lumi.spec` and the bundle policy as needed.
- Inspect the working tree before editing; preserve unrelated changes. Use
  isolated fixture projects and state for evaluation, not personal sessions.
- Keep runtime state out of the repository: normally `~/.lumi/projects/`
  for sessions, ledgers, notes, workers, checkpoints, artifacts, and worktrees.

## Behavior to preserve

- A single sidebar groups sessions under named projects. Search covers projects,
  paths, and session titles. Rendering is bounded per expanded project; preserve
  active-session visibility, scroll position, and keyboard focus.
- New session first offers existing projects and a folder picker; project-row
  plus buttons use that project directly. Cancel preserves the active draft.
  After choosing a project, start a draft there; do not persist empty
  conversations until the first message. Drafts remain scoped by project/session.
  Browser pages can share a server with the desktop wrapper. Choose native
  dialogs from the requesting page's native bridge capability, not merely a
  server-side window. Keep typed-path selection, cancellation and new-session
  intent working without a native picker.
- Every GUI endpoint that reads or changes state goes through
  `gui/local_access.py` (exact Host, own Origin, per-launch token), checked
  before a WebSocket is accepted. Launch codes travel only in URL fragments and
  come from the launcher or the desktop bridge. Never put the token in a cookie,
  URL, log or printed output. The socket's `update_settings` edits only
  the fields Settings shows; hooks, stdio MCP servers, LSP servers, plugins and
  the gateway stay file-edited.
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
- Forward Codex JSONL messages and tool lifecycle observations as they arrive.
  CLI observations never enter native tool execution. Count only successful
  file-change results, and retain exit status and source for named CLI checks;
  prose claims and arbitrary shell success are not verification. Partial text
  must not mask a later CLI failure.
- OpenRouter uses its own tool/message format, capability catalog, and reported
  costs. Preserve reasoning continuation only for the originating model.
- Never silently use a system/install directory as the project. Respect the
  sandbox and permission modes; writer worktrees must not reset, stash, or merge
  over a dirty user checkout. `working_subdir` may narrow, never broaden, scope.
- Long foreground workers use `job_start`, `job_status`, and `job_cancel`.
  Ordinary shell children are cleaned up at tool completion. Managed jobs are
  project-owned, limited to20minutes, and stopped on client exit; explicit
  application checkpoint recovery is required after restart. Do not detach a
  launcher, silently replay jobs, or equate process exit with artifact quality.
  A stopped model turn may leave an owned worker running. Inspect job state and
  durable manifests before restart or claiming idle. Distinguish job-reported
  duration from independent artifact checks and actual long-duration evidence.
- The native chat loop owns one active workspace. Do not swap project/session
  state during a run; Stop and persistence must use captured run ownership.
  Keep main request allowances distinct from tool counts, auxiliary requests and
  dollar budgets. Preserve partial checkpoints and interrupted request uncertainty.
- Keep cancellation and user input live. Report completion only after work and
  relevant checks finish. Use enforced execution limits for qualification; a
  prompt-only tool-call limit is not enforcement. Preserve observed overruns and
  operator stops even if the application tests pass. Named checks, screenshots, mock responses, and live
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
node --check lumi/gui/static/app.js
node --check lumi/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs tests/appearance.test.cjs
git diff --check
```

Release builds install the hash-pinned `packaging/requirements-release.txt`.
After changing dependencies in `pyproject.toml`, run `python scripts/lock_release.py`
and commit the locks. A shipped package under GPL, AGPL or LGPL needs a
`license_reviews` entry in `packaging/third-party-components.json`, or exclusion
under `not_shipped`; the build fails otherwise.

Use `scripts/build_clean.ps1` for Windows release builds. Never clean a running
bundle; use a separate source copy for a candidate build while testing. The
script's `-ValidateOnly` checks the running-target guard without cleanup. Verify the packaged
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
`pyproject.toml` and `lumi/__init__.py`; publish a matching tag and
verify the release workflow and public appcast before reporting deployment.

Start with [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the
[documentation index](docs/README.md). Detailed references include the
[desktop workflow](docs/desktop-workflow.md), [prompt architecture](docs/model-prompt-architecture.md),
[durable runtime](docs/modern-agent-runtime.md), and
[notes, previews, and acceptance checks](docs/priority-improvements.md).

Windows native shell tools reject multiline commands because cmd.exe can silently
truncate them. Use project script files and single-line invocations; never treat
a zero exit code with missing expected evidence as a successful diagnostic.

SONN's text-only transport must also handle tool-result screenshots; user-content
conversion alone does not cover images appended by shared adapters. Preserve
local artifacts and tool provenance, and never claim visual analysis from a
text placeholder. Verify the next model call after a browser screenshot.
