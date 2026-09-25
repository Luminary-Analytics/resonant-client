# Unreleased

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

## September 25 client security: file exclusions, project trust, retention and tool switches — source only, not released

- **Files Lumi never reads** (`lumi/engine/exclusions.py`): gitignore-style
  patterns in **Settings > Privacy & security**, plus a project's `.lumiignore`.
  Both apply at once when edited.
  - File tools refuse excluded files, including `batch` children, and so does a
    search rooted at an excluded folder.
  - glob, grep, git status and git diff leave them out and say how many were
    hidden. The codebase index skips them.
  - `@file:` attachments explain the refusal instead of attaching.
    `@diff:working` excludes them from the diff text.
  - Opening a `file://` page in the browser tools now goes through the same
    sandbox and exclusion checks; before, it could read any local file.
  - Shell commands can still open excluded files.
- **Project trust** (`lumi/gui/workspace_trust.py`): until the user trusts a
  project, Lumi doesn't use what the repository brings:
  - its instruction files (AGENTS.md, LUMI.md, CLAUDE.md and similar), including
    in a Mission's first message;
  - its committed notes (`.lumi/memory.json`) and codebase summary;
  - the `allow` rules in its `lumi-policy.json`, which previously let a cloned
    repository skip approval prompts (its deny and ask rules still apply);
  - automatic lint and test runs, which execute the repository's own code.

  A banner in the chat offers **Trust this project** or **Keep restricted**.
  A policy file that changes after trust needs review again. Projects already in
  Recent projects are trusted on first run, so upgrading changes nothing for
  existing work.
- **Transcript retention** (`lumi/gui/retention.py`): "Delete transcripts after
  (days)" deletes everything holding conversation content that was last touched
  before then, at startup and daily:
  - sessions and their ledgers;
  - drafts, checkpoints, worker records, artifacts, traces and mission audit logs;
  - recordings and dated logs.

  The open session is never deleted. The default keeps everything.
- **Tools outside Lumi's own loop:** switches for Codex and Claude Code (they
  leave Models; a selected one falls back to another provider), computer use
  (tools hidden and refused) and the chat gateway (`lumi gateway` refuses to
  start). Organization policy will be able to lock these.
- `@diff:` mentions no longer pass a selector starting with `-` to git, where
  `--output=<file>` would have written a file.

Validation on September 25, 2026:

- New tests: `test_exclusions_and_trust.py`, `test_client_security.py`,
  `test_retention.py`. They cover the real tools, git, the context broker,
  project notes across a trust change, the policy builder and `AppState` wiring.
- Full `pytest`: 3,631 passed, 2 skipped.
- In the browser pane against an isolated home with a scripted model:
  - The trust banner appeared for a project with AGENTS.md and a policy allow
    rule.
  - `.env` was excluded through Settings: the model's read of it was blocked
    with the rule named, and the file's value never reached the model.
  - **Trust this project** hid the banner, and the next request carried
    AGENTS.md (it hadn't before). The decision persisted with the policy
    digest.

Not exercised: organization policy locking these settings (next group), and
the packaged app.

## September 24 supply chain: pinned dependencies, audit, notices, SBOM and signing — source only, not released

- **Pinned, hash-checked release builds:** `scripts/build_clean.ps1` installs
  `packaging/requirements-release.txt` with `--require-hashes` instead of resolving
  `.[gui,desktop]` at build time. `python scripts/lock_release.py` (uv) regenerates
  it and the CI tools lock for every supported platform and Python version, and a
  test fails when `pyproject.toml` declares a dependency the lock does not pin.
- **Vulnerability audit:** a **Dependency audit** workflow runs pip-audit on every
  pin when the locks change and weekly. `packaging/audit_locks.py` removes platform
  markers first; otherwise pip-audit silently skips packages for other operating
  systems.
- **Third-party notices:** `THIRD_PARTY_NOTICES.txt` is generated from the build
  environment with each package's license texts, plus ripgrep, WinSparkle, the web
  assets and fonts, the Python runtime and PyInstaller's bootloader. It ships in
  the bundle (required by the bundle policy) and with each release.
- **License gate:** the build fails if a shipped Python package is GPL, AGPL or
  LGPL without a recorded review. The first run found two, both optional
  PyAutoGUI helpers that Lumi never calls: **MouseInfo** and **PyMsgBox** (GPL-3.0).
  They are now excluded from the bundle.
- **CycloneDX SBOM:** `build_clean.ps1 -SbomPath` writes a CycloneDX 1.6 SBOM of
  the build environment plus the bundled non-Python components, validated against
  the schema. The build check uploads it with the notices; releases attach both.
- **Authenticode:** the release workflow signs `lumi.exe` and the installer
  through `packaging/sign_windows.ps1` when a PFX or a cloud/hardware signing
  command is configured. The installer is signed before its EdDSA update
  signature. Without credentials the release continues unsigned with a warning.
  No certificate is configured yet, and macOS notarization waits for a macOS
  build pipeline.

Validation on September 24, 2026:

- 14 new tests in `test_release_supply_chain.py`: lock coverage and hashes,
  component versions against the fetch scripts, notices, the license gate, SBOM
  additions, marker stripping, and signing without credentials.
- A local `build_clean.ps1 -SbomPath` run from the locks built a 61.6 MiB bundle.
  - The notices listed 44 packages and 8 other components.
  - MouseInfo and PyMsgBox were absent from PyInstaller's module list.
  - The SBOM had 59 components and passed schema validation.
  - A scratch copy of `lumi.exe` reported its version.
- pip-audit on every pin of both locks: no known vulnerabilities.

Not exercised: Authenticode signing with a real certificate (none exists), the
release workflow itself (it runs only on a tag), and the new CI jobs until this
PR's checks run.

## September 24 network trust, credential store and secret hygiene — source only, not released

- **Corporate networks** (`lumi/net.py`, **Settings > Connections > Network**):
  - TLS is verified with the operating system's certificate store (`truststore`),
    so a company root certificate used for TLS inspection works without exporting
    PEM bundles. A toggle turns this off.
  - A configured proxy is exported as `HTTPS_PROXY`/`HTTP_PROXY`, and a bypass list
    is added to `NO_PROXY`. Local addresses always connect directly.
  - Clearing the proxy restores the variables the user's own environment had.
  - Proxy URLs with a user name or password are refused; authenticating proxies
    need a machine-level proxy or a local helper.
- **API keys in the OS credential store** (`lumi/secrets_store.py`): keys saved in
  Settings go to Windows Credential Manager, the macOS Keychain or the Secret
  Service (service `Lumi`), and `settings.json` keeps the placeholder
  `__keychain__`.
  - Existing plaintext keys move on first load.
  - Settings says where keys are kept.
  - The store is never used while settings live in the legacy `~/.resonant`
    folder: an older SONN Client sharing that folder would read the placeholder as
    its key.
  - `LUMI_KEYCHAIN=off` and `LUMI_KEYCHAIN_SERVICE` control it.
- **Keys stay out of child processes:** the agent's shell, hooks, stdio MCP servers,
  managed jobs and previews, the Python REPL, automatic tests and lint, and
  acceptance checks start without Lumi's model-provider keys
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the others in
  `secrets_store.PROVIDER_KEY_ENV`). An MCP server's own `env` entries still apply.
  Codex and Claude Code keep their environment.
- **Secrets removed before each model request** (`lumi/secret_scan.py`):
  - The values of saved keys, sensitive settings and provider keys in the
    environment are always replaced in tool output. Values shorter than 16
    characters are ignored.
  - **Settings > Privacy & security > Scan for secrets** (off by default) also
    replaces well-known credential formats in tool output and in your messages:
    cloud and platform keys, tokens, private keys, passwords in connection
    strings and `.env` lines.
  - The model sees `[REDACTED <kind>]`, and the chat shows a note saying what was
    removed. Codex and Claude Code read files through their own tools and are not
    scanned.
- **Diagnostics redact by value:** **Help > Save diagnostics** removes the actual
  values of saved keys (including keys in the credential store) wherever they
  appear, in addition to the existing patterns. The embedded `settings.json` is
  masked field by field, so a triager still sees which providers were configured.
- Settings no longer rebuilds a form under a click. After a toggle or menu saved,
  clicking straight into a text or key field lost the typing, because the
  deferred refresh ran before the clicked field had focus.
- `truststore` and `keyring` are now core dependencies. `packaging/bundle-policy.json`
  requires keyring's entry-point metadata in the bundle, because without it the
  frozen app would silently keep keys in `settings.json`.

Validation on September 24, 2026:

- 53 new tests: `test_secret_hygiene.py`, `test_network_settings.py`,
  `test_secret_scan.py`.
  - The credential store is an in-memory keyring.
  - The shell, a hook and an MCP server are checked for the key they must not
    receive.
  - Two turns with a scripted backend confirm that the model never received a
    planted token or a saved key.
- Full `pytest`: 3,570 passed, 2 skipped.
- In the browser pane against an isolated home, with an in-memory keyring and the
  scripted Ollama stub:
  - The scan toggle, the proxy (a credentialed URL was refused, a valid one saved
    normalized), the bypass list and the certificate toggle (by keyboard) were set
    through Settings.
  - An OpenAI key was saved to the store and cleared.
  - A turn that read a file containing a fake GitHub token showed the redaction
    note, and the stub received `[REDACTED GitHub token]`, never the token.
  - The Privacy page was checked at phone width.

Not exercised: a real proxy or TLS-inspecting network, the real Windows Credential
Manager or macOS Keychain (the fixture replaced them), and the packaged app.

## September 24 model connections: Anthropic, OpenAI and custom endpoints — source only, not released

- **Anthropic (Claude):** a native Messages API adapter (`lumi/anthropic_api.py`)
  with tool use, extended thinking and prompt caching (system prompt, tools and the
  latest user turn). Signed thinking blocks are replayed within a tool loop only
  to the model that produced them. If a loop started without thinking, thinking is
  dropped for that request instead of failing.
  - The same adapter reaches Claude on Amazon Bedrock (SigV4 signing and AWS event
    stream decoding in-house, botocore used for credentials when installed, or a
    Bedrock API key) and Vertex AI (google-auth or the gcloud CLI).
- **OpenAI:** a Responses API adapter (`lumi/openai_api.py`) for OpenAI and Azure
  OpenAI. It is stateless (`store: false`). Encrypted reasoning items are replayed
  only to the same model, quota errors are not retried, and chat model discovery
  drops embedding, audio and image models.
- **Custom connections** (`lumi/connections.py`): OpenAI-compatible gateways, Azure
  OpenAI, Bedrock, Vertex, and Anthropic or Responses proxies are defined as
  validated data in **Settings > Connections**. Each has a test button and appears
  in the model menu under its name.
  - HTTPS is required outside localhost and private networks, and credentials in
    URLs are refused.
  - Keys are stored as `conn_<id>`, are written only by the connection commands,
    and are never returned to the page.
  - A connection in use by a running turn can't be edited or removed.
- Anthropic and OpenAI keys sit alongside the other API keys and have their own
  connection checks. Capability profiles now cover the Claude, GPT/o-series and
  Gemini families, where the generic fallback had assumed a 32K window.
- The runtime status shows a connection's name, not its internal key.

Validation on September 24, 2026:

- 38 new tests: `test_anthropic_api.py`, `test_openai_responses.py`,
  `test_connections.py`. The SigV4 signature matches botocore's for a Bedrock model
  path that needs double encoding.
- Full `pytest`: 3,517 passed, 2 skipped.
- In the browser pane against an isolated home, three connections were added
  through the Settings form (typing, selects and Enter), tested, saved, and used
  from the model menu. Each completed a scripted tool loop through its own wire
  format: Chat Completions, Anthropic Messages and OpenAI Responses.

Not exercised: live Anthropic, OpenAI, Azure, Bedrock or Vertex accounts (no keys
were used), extended thinking against a real model, and the packaged app.

## September 24 dark and light themes — source only, not released

- **The saved theme now survives a restart.** The desktop window uses a new
  port and private browser storage on every launch, and the page restored the
  theme only from browser storage. A Light choice therefore came back Dark, while
  Settings still showed Light; density and font size were lost the same way. The
  server now renders the saved appearance into the page (`gui/appearance.py`).
  The native window also opens in the theme's background instead of white.
- **Match system:** a third theme choice that follows the operating system's
  light or dark setting, including changes while Lumi is open.
- **Light theme coverage.** 268 color literals that only suited the dark
  theme now use theme tokens. They had left light mode with a dark composer,
  code blocks, task cards, menus and status popover, a dark "Review" button with
  dark text, and hints below 2.5:1 contrast. Code blocks get a light
  highlighting palette. New tokens cover gold-tinted text and chips, softer
  status text, and diff colors. Native controls and scrollbars follow the theme
  through `color-scheme`.
- Dark mode is unchanged. Computed colors of every element were compared before
  and after in the chat view, the command palette, the status popover, menus,
  the model and permission pickers, New session, and Settings. The only
  differences were the same colors written in a new format, one border moving
  from 12% to 15% opacity, and native checkboxes now following the dark scheme.

Validation on September 24, 2026, in the browser pane against an isolated home
and a scripted Ollama-compatible model (no live model):

- Light mode had no text below 3:1 and no dark surfaces in the chat, all 17
  Settings pages, the command palette, status popover, menus, the model and
  permission pickers, and New session. Only hint-level text is between 3.7:1
  and 4.5:1; dark mode's hints measure 3.3–3.8:1.
- Choosing Light in Settings saved it, and a reload with empty browser storage
  still showed Light.
- Match system followed an emulated OS switch from dark to light without a
  reload, and resolved on load.
- New tests: `tests/test_appearance.py` and `tests/appearance.test.cjs`.

Not exercised: the packaged desktop window (its native background color and a
real OS theme switch), macOS, and surfaces the fixture did not open (live-run
progress, steer queue, onboarding, mission and autonomous views). Those use the
same tokens, but no one has looked at them in light mode.

## September 24 rebrand to Lumi — source only, not released

SONN Client (originally Resonant) is now **Lumi**. SONN keeps its name as the
model service Lumi can connect to. The new identity is the Lantern mark, an L
holding a gold light on night; see [brand/README.md](../brand/README.md).

- **Name everywhere a user or admin looks:** window and page titles, menus,
  prompts, notifications, the browser extension, the diagnostics bundle, the
  Windows taskbar id, the model's system prompt, `lumi.exe`, the `lumi`,
  `lumi-gui`, `lumi-tui`, `lumi-smoke` and `lumi-skill` commands, and the
  `lumi` package and distribution.
- **Logo and icons:** new favicon, sidebar mark and wordmark, welcome and
  empty-chat icons, `lumi.ico` (16–256 px; 16 and 32 px pixel-aligned),
  notification PNG, a macOS `lumi.icns` on Apple's icon grid, and the Dock
  name and icon when run from source on macOS. `scripts/build_brand_assets.py`
  draws the rasters from the SVG masters in `brand/`.
- **Palette:** gold on night replaces SONN teal. The light theme uses bronze on
  paper. Warnings are orange so they are not confused with the accent. The
  titlebar and sidebar now follow the theme; the light theme previously drew a
  dark sidebar with dark text.
- **Installer:** "Lumi", `lumi-setup-X.Y.Z.exe`, `Program Files\Lumi`, a new
  AppId. It silently removes the pre-rebrand SONN Client/Resonant install
  first, so Apps & Features keeps one entry. The macOS `Lumi.app` bundle step
  is in `packaging/lumi.spec`, but no macOS build has been made or tested.

Compatibility:

- `~/.resonant` moves to `~/.lumi` on first launch. The move happens before the
  startup log opens. If it is blocked, for example by an older build that is
  still running, the old folder stays in use and the move is retried next
  launch.
- `RESONANT_*` environment variables are read as their `LUMI_*` names.
- Hooks receive both the `LUMI_*` and `RESONANT_*` variables.
- The `resonant*` commands remain as aliases.
- Projects keep an existing `.resonant/` folder, and new projects get `.lumi/`.
- `resonant-pack.json`, `resonant-policy.json` and `RESONANT.md` are still read,
  alongside `lumi-pack.json`, `lumi-policy.json` and `LUMI.md`.
- An existing `~/Documents/Resonant Projects` stays the default projects folder.
- Agent handoff and checkpoint commits are now authored `@lumi.local`.

Deliberately unchanged:

- The update feed stays at the current GitHub Pages address, because every
  installed SONN Client polls it.
- Moving the feed to a Lumi domain needs a bridge release. Rename the
  repository only after that, because GitHub does not redirect Pages project
  sites after a rename.
- The SONN conversation-id prefix, the Engram memory namespace, the editor
  MCP entry names (`resonant_blender`…), model-facing tool names, the
  harness output fence, the `refs/resonant/checkpoints` git refs and the
  checkpoint archive marker are unchanged, because saved data uses them.

Validation on September 24, 2026:

- `ruff` and `git diff --check`: clean.
- UI recovery checks: 30.
- Full `pytest` on the final source: 3,472 passed, 2 skipped.
- New `tests/test_rebrand_compat.py` covers the state move, a blocked move,
  overrides, legacy project folders, and legacy environment and hook variables.

The browser pane checked a source run with an isolated home:

- the launch link and socket;
- the sidebar mark and wordmark;
- the gold send button;
- Settings;
- the light theme;
- a 375 px width without horizontal overflow;
- no remaining "SONN Client" or "Resonant" text.

A local PyInstaller build of `lumi.exe` passed the bundle policy (146.6 MiB,
316 files). Run against an isolated home holding a legacy `~/.resonant`:

- `--version` printed `lumi 0.19.2.dev11` and moved the folder to `~/.lumi`;
- the page was titled Lumi and served the new icons;
- the launch code was redeemed;
- the socket returned 403 without the token and 101 with it;
- the startup log was clean;
- the executable carries the Lantern icon, and Windows `LoadImageW` (used for
  the window icon) loads `lumi.ico` at 16–256 px.

WinSparkle was disabled for that local run so it would not write the real
registry; CI checks its bundling. That build predates the final text-only
command-help changes.

Not exercised: compiling the installer (no Inno Setup locally) or upgrading
an installed SONN Client, a macOS build, and live models.

## September 24 local GUI access control — source only, not released

**Security fix.** The GUI server bound to 127.0.0.1 accepted any WebSocket
without a credential. Any local process, another account on the machine, or a
web page could open the socket; WebSockets are exempt from CORS, and DNS
rebinding defeated the only Origin check. Such a client could then run
`shell_exec`, rewrite settings including hooks and MCP servers, switch the
permission mode and answer approval prompts.

- Each launch creates an access token that the server never prints or logs.
  Pages redeem a one-time code from the launch link's URL fragment at
  `/api/access`. They keep the token in origin storage, which is port-isolated
  unlike cookies, and send it as a WebSocket subprotocol or `X-Lumi-Access` header.
- `/ws` and `/api/ui-state` also require an exact `Host` (`127.0.0.1:<port>`,
  `localhost:<port>`, or a literal non-loopback bind address) and this server's
  `Origin`. A refused handshake is closed before `accept()`, and the client
  receives HTTP 403. A Host guard covers every route; the page refuses framing.
- Launch links: the desktop window opens with one; `--browser` prints one;
  **File > Open in Browser** mints one through the desktop bridge only. A page
  without access shows how to get a link instead of retrying. Pasting a new link
  into an open tab reloads it and redeems the code. Diagnostics ZIPs redact
  launch links.
- `update_settings` accepts only the fields Settings edits. Hooks, LSP servers,
  plugins, the gateway, stdio MCP servers and whole-section writes are refused.
  HTTP MCP entries are rebuilt without `command`/`args`/`env`. The Ollama setup
  wizard's `values` payload was previously ignored, so its typed URL was never
  saved; it is now validated and saved.
- `set_permission_mode` requires an explicit known mode; the backends had
  treated a missing or unknown mode as Full-auto. `approve` requires an explicit
  `true`. Wildcard `--host` binds print and probe a loopback URL.

Compatibility: bookmarks and scripts that open the page or socket without a
launch link are refused. After a restart, a tab needs the new link.

Validation on September 24, 2026: 3,416 passed / 3 skipped (baseline before the
change 3,357 / 3), 29 UI recovery checks, ruff and `git diff --check`. Live
checks used an isolated home and a scripted Ollama-compatible model, not a live
model. In the browser pane, with real keyboard events, they covered:

- the locked page at desktop and phone widths, and link redemption;
- sending a message, F5 reload with draft restore, and a dropped socket with
  automatic reconnect;
- a run that continued across a mid-run reload;
- a stale tab after a restart, recovered by pasting the new link, and a reused
  link falling back to the stored token.

Raw HTTP against the live server returned 403 for missing, wrong, cross-origin,
dev-server-origin and rebinding handshakes, and 101 with `lumi.v1` otherwise.

The desktop window (pywebview 6.1, WebView2) redeemed its code and connected.
**Open in Browser** was triggered through its click handler from the fixture's
own `evaluate_js`; operating-system input was not used. The minted link,
recorded by a stub `webbrowser.open`, connected a browser pane that sent a
message.

Not exercised: a packaged build, the real default-browser handoff, macOS/Linux
webviews and live models.

## September 24 security fixes: capability-pack trust and tool approvals

Source-only; not bundled or released. These are separate from the AI Employee pause.

**Repository capability packs could approve themselves.** The client
discovered `<project>/.resonant/packs` automatically, and a pack's own manifest
could set `trust`, `enabled` and `sha256`. Opening a cloned repository could
then connect the pack's MCP servers and register its shell hooks. The digest
covered only the manifest.

- Trust and enablement now come only from user settings. Manifest `trust`,
  `enabled` and `sha256` are ignored.
- Approval is location-bound. A repository pack can only be trusted by a user
  approval of that pack directory, so an approval never follows a copied pack
  into another repository. Pinned trust by pack id still works for packs
  outside the project.
- Every approval pins one digest covering every file in the pack (except
  `.git`) plus the repository files that its hook and MCP commands name. Any
  change turns the pack off until it is approved again. Packs with links, more
  than 4,000 files, or more than 64 MB cannot be verified.
- Pack hooks re-verify the digest before each run. Skills, agents and MCP
  servers stop contributing once the pack changes.
- Pack hooks now live on per-session runners. Opening another project
  disconnects the previous project's pack MCP servers; previously only a
  settings reload removed them.
- **Settings > Capability packs** shows what each pack would run and offers
  Approve or Revoke. If the pack changed after the list was drawn, approval is
  refused with an explanation. A banner above the composer names packs that
  are waiting for review.
- A project's `resonant-policy.json` could weaken built-in denies: an earlier
  `allow` beat, for example, Auto-edit's recursive-delete deny. Built-in denies
  are now checked first. Repository rules can still tighten the policy, and a
  policy `prompt` rule now requires approval.

**A user's Deny could run the tool.** After a Deny, the engine emitted
PERMISSION_REQUEST and read `HookResult`'s default decision, "allow", as
approval. The GUI always attaches a hook runner, so in Ask mode a denied tool
ran anyway.

- The user's answer is final, and only an explicit `true` approves.
- A missing or unknown hook decision is no decision. When no prompt can be
  shown, only an explicit allow or deny from a matching PERMISSION_REQUEST hook
  decides; otherwise the call fails closed. Arguments rewritten by a hook are
  checked against the policy again.
- Auto-edit used to run shell and MCP actions without asking. The GUI attached
  a prompt only in Ask mode, and the engine fell back to a legacy
  `auto_approve` flag. Auto-edit now asks before shell, MCP, browser, desktop,
  REPL, process and git actions, and before any new tool. `auto_approve` now
  follows the autonomy tier. Background work without a prompt, such as sprint
  roles in Auto-edit, now skips calls that need approval.
- Changing the permission mode now updates the live session's tier and policy.
  Before, switching Full-auto to Ask mid-session kept auto-approving.
- Delegated workers ask through the parent's prompt, one at a time, instead of
  auto-approving; restarted workers use their own run's prompt.
- Every prompt has a request id. Late or stale answers are ignored instead of
  approving the next request, and a missing or non-boolean `approved` no longer
  counts as approval.
- The approval dialog now takes focus when it opens: Tab reaches Deny and
  Allow, Escape denies, and focus returns to the composer. Denied shell cards
  read "not run" instead of "running…".

Validation: `ruff`, 23 Node UI tests (one new), `git diff --check`, and the
full suite: 3,405 passed, 2 skipped. The new tests are in `test_permission_decisions.py`,
`test_gui_permission_modes.py` and `test_capability_pack_trust.py`. Of these,
45 were written before the fix: 41 failed against the unfixed code, and 4
contract tests passed. Two more cover the restarted-worker prompt and dropping
servers of a pack edited after approval; they were written with their fixes
and mutation-checked. Real browser events drove the actual app, WebSocket,
engine, hook runner and approve handler. The model was scripted, no provider
was called, and the home and project were temporary. Checked there:

- Opening a repository with a self-trusting pack ran neither its hook nor its
  MCP server.
- In Auto-edit, a shell command prompted. Deny left no file; Allow ran it.
- After switching to Ask mid-session, a keyboard Deny (Tab, then Enter) and an
  Escape each left no file.
- Approving a pack edited after review was refused. Approving the current
  content started its MCP server and ran its hook on the next turn.
- Editing the approved pack stopped the hook, showed "Changed since approval",
  and brought the banner back. Revoking worked.
- The Settings page and dialog fit a 375 px viewport without horizontal
  overflow.

No live model run, packaged build or CLI-provider path was exercised.

## September 15 AI Employee source integration — paused

The user paused implementation; see the [resume handoff](ai-employees-handoff.md).
Integrated durable SONN task/advice controllers, a task panel with assignment
inspection, scoped file-only workers, isolated artifact handoffs and recovery.
Full native candidate regression: **3,351 passed / 3 skipped**. Product-side real
native-process checks use scripted providers and synthetic release artifacts; they
do not establish learned quality/cost benefit. No new bundle or release was built.
Automatic supervision, trusted outcome attribution, host enrollment and full
qualification remain open. No paid training phase is authorized.

## Prior package checkpoint

**Latest local package: 0.19.2.dev11; not a public release.** Its full suite passed
3,322 tests (two skipped), 40 focused job/editor checks, and packaged source/asset
comparison across 146 files. Later browser picker changes remain source-only;
22 UI recovery checks and isolated actual-browser interaction pass. Historical
candidate sections below retain their original results.
See [Blender qualification](blender-qualification-20260914.md).

Historical candidate 0.19.2.dev3 follows [0.19.1](v0.19.1-release-notes.md).

Native SONN desktop turns bind outgoing requests to a saved project/session
identity across model switches and reloads. Generated titles and compression
requests use independent identities with learning disabled. Generated repair
and diagnostic user turns carry explicit learning exclusions; actual tool
observations retain their original role. These controls do not grant account
access or imply that retained observations are useful lessons.

Successful edits that alternate four times between the same two text states
trigger a specific diagnostic recovery message. This does not terminate the
run, erase failures, weaken assertions, or limit ordinary repository reads.

The clean packaged candidate passed the bundle gate. The client suite passed
3,280 tests with 2 skipped, plus 19 UI recovery tests and lint. A native desktop continuation after process restart reached the same hosted
SONN learning session and retained the new request. Generated model-summary
exclusions are contract-tested; a later live continuation compacted the saved
history and resumed coding under the same hosted session. Malformed-summary
fallback was verified in a no-network replay, not exercised in that live attempt.
Delegated-worker identities and automatic learning benefit remain unqualified.
This is a local candidate, not a published release.

The Windows clean-build script checks for a running target bundle before deleting
any build files. `-ValidateOnly` exercises that guard without changing files.
An operator build interrupted the first candidate; the guard prevents that
specific packaging mistake from recurring.

Malformed or empty model summaries can now recover through a complete archived
transcript plus mechanically retained requirements, checklist and tool evidence.
The archive must be readable through the allowed artifact tool; unavailable
storage or provider errors keep history intact and stop compaction. This fallback
does not infer decisions, successful checks or task completion. The actual failed
desktop transcript shrank from 51,144 to 12,307 estimated history tokens in a
no-network replay with a deliberately malformed summary.

The guided drawing repair finished with 7/7 backend tests, nine independent
browser checks, a six-round game and durable account/score recovery. The builder
exceeded its prompted 15-tool boundary (17 observed) and was stopped; its extra
Windows launch command failed quoting. These checks do not establish unattended
completion or an enforced tool-call budget.


## September 14 long-run candidate: 0.19.2.dev5

- Optional enforced main model-request allowance, counted through recovery loops;
  reaching it retains work and permits continuation instead of asking for /clear.
- Reject active project/session replacement; fix Stop to target the active engine
  and final saves to target the captured record. Save completed step history.
- Native dev4 qualification exposed the project-switch failure; it was stopped
  before the new application was submitted. Keep it as a failed product-path test.
- Two-project native qualification is ongoing. No beta or public release claim.

September 14 dev6 candidate: classify SONN HTTP failures without leaking provider
text, explain interrupted generation, disable inherited paid-request retries, and
make SONN timeout wording explicit. Targeted adapter/identity checks:41passed.
Native two-project qualification remains in progress.

Dev7 also rejects multiline Windows shell commands before dispatch. The Windows
command runner can otherwise return zero while ignoring code after a newline.
Write a project script and invoke it with a single-line command. Four boundary
checks pass; the observed silent non-execution is preserved in qualification.

Dev8 fixes browser screenshots in SONN tool history. The gateway currently accepts
text only: retain image artifacts locally, preserve tool text, and direct the
agent to DOM/accessibility/evaluation evidence. Do not append a synthetic human
image message. The actual failed GearDesk history was rejected by the gateway
parser before this fix and accepted afterward, without changing saved history.

- SONN learning queue admission now has a bounded, cancellable cooldown/retry.
  Only the structured pre-dispatch code qualifies; uncertain paid failures remain terminal.

- Fix Blender Check editor argument compatibility with pinned blender-mcp1.9.1; live command-handler check and regression coverage.
## Installers and updates served from GitHub Pages

The release workflow now copies each stable installer to the GitHub Pages site
under `downloads/vX.Y.Z/` and points the signed appcast there, instead of at the
GitHub Release asset. This lets the source repository become private without
breaking updates for installed apps, as long as Pages stays public. The feed URL
in `updater.py` is unchanged, so no client rebuild is required. The Pages site
keeps the three newest installers and is published as one fresh commit per
release. Pre-release tags no longer reach the appcast. The installer's publisher
and support links now point to getsonn.com and the download page.

## Local dev11 long-job candidate

Adds project-owned `job_start`, `job_status`, and `job_cancel` for bounded
foreground workers such as Blender rendering. Preserves ordinary shell cleanup
and separates worker completion from artifact verification. Jobs stop on client
exit; application checkpoint recovery is explicit. This is a local candidate,
not a published release or a completed Blender qualification.

### Browser folder picker recovery
Source candidate: browser pages sharing the desktop server now open an in-page path dialog based on the page native bridge, rather than sending a native picker command. The running dev11 Blender qualification package is unchanged. Twenty-two UI recovery checks and isolated browser keyboard/cancel interaction pass.
