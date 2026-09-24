# Unreleased

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

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
  unlike cookies, and send it as a WebSocket subprotocol or `X-SONN-Access` header.
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
dev-server-origin and rebinding handshakes, and 101 with `sonn.v1` otherwise.

The desktop window (pywebview 6.1, WebView2) redeemed its code and connected.
**Open in Browser** was triggered through its click handler from the fixture's
own `evaluate_js`; operating-system input was not used. The minted link,
recorded by a stub `webbrowser.open`, connected a browser pane that sent a
message.

Not exercised: a packaged build, the real default-browser handoff, macOS/Linux
webviews and live models.

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
