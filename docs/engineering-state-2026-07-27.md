# Engineering state — 2026-07-27

Status: current working reference for the v0.11.7–v0.11.12 stretch
Audience: contributors and coding agents picking this repo up cold

`docs/agentic-harness-north-star.md` still outranks this document on product
and architecture direction. `ROADMAP.md` remains the backlog of record. This
file exists so someone resuming work does not have to reconstruct *why* things
are shaped the way they are, or re-learn the traps the hard way.

---

## 1. What changed, and why it mattered

Six releases, v0.11.7 → v0.11.12.

| | before | after |
|---|---:|---:|
| `gui/app.py` | 9,287 | 2,655 |
| `AppState` | 5,700 lines / 130 methods | 1,296 / 39 |
| `websocket_endpoint` | 2,398 lines / 82 commands | 606 / 5 |
| `gui/static/app.js` | 14,500 | 9,990 |
| `handleEvent` | 750 | 629 |
| `bindEvents` | 467 | 12 (+7 named groups) |
| tests | 2,794, **not run by CI** | 2,929, fully gated |

### The failure mode this repo keeps producing

Three separate bugs in one stretch had the same shape: **a capability that
worked on the developer's machine and silently did not for users, with no
signal either way.**

- **psutil** was declared in no dependency group, so `process_list` and
  `process_kill` were dead in every shipped installer while working locally —
  it rides along as a transitive dependency on dev boxes.
- **ripgrep** was preferred but unbundled, so shipped installs fell back to
  `findstr`, whose regex dialect has no alternation, no `+`, and no groups.
  Patterns matched nothing and the agent read `(no matches)` as "not in this
  codebase" — the most expensive possible wrong answer.
- **An orphaned `tool_call`** in conversation history bricked sessions
  permanently. Every later request was rejected, Retry and Continue reproduced
  it identically, and the UI truncated the provider error before the part that
  explained why.

- **Five CDN-loaded frontend assets** (fonts, marked, highlight.js, DOMPurify)
  blocked first render on two external hosts. On a developer machine with a warm
  cache this is invisible; for users it is a network round trip per launch (the
  Google Fonts stylesheet alone measured 273 ms), and offline it means an
  unstyled page with no markdown rendering at all. Fixed in v0.11.14 — see §7.

The CI gate found the first two within an hour of existing. When adding
anything the packaged app depends on, register it in `packaging/resonant.spec`
**and** `packaging/bundle-policy.json`, then confirm the gate fails without it.
`bundle-policy.json` can also *execute* a bundled binary — presence is not
proof that it runs.

---

## 2. Traps that cost real time

Each of these was discovered by breaking on it.

**The dispatch chain in `websocket_endpoint` sits at exactly 12 spaces.** The
`intent_*` branch is a single `elif command in (...)` wrapping its own nested
`if command ==` chain at 20 spaces. Matching those as top-level branches splits
one handler into six, each referencing an `intent_service` the parent built.

**The endpoint's own `except`/`finally` must bound the last branch**, or an
extraction absorbs the disconnect handling and cleanup block.

**`_webview_window` is assigned onto the app module by `server.py` at
runtime.** Read it late by attribute; a from-import captures `None` forever and
silently routes every desktop user to the browser fallback.

**Class methods are non-enumerable.** `Object.assign` copies *nothing* from a
class prototype. `applyMixin` in `app.js` uses property descriptors and throws
on a name collision or a missing mixin, because both otherwise fail silently at
call time.

**Substring matching on method names is dangerous.** `"perMISSION"` contains
`"mission"`, which swept four permission methods into an autonomous-session
mixin. Cluster extractions now use explicit name lists.

**`_is_pytest_temp_path` filters every `tmp_path`.** Tests exercising the
recents file must neutralise it or they silently assert nothing. The isolated
home is what keeps the real file safe.

**Ruff's default rule set changes between releases.** `pyproject.toml` pins
`select` explicitly and bounds the ruff version. An unpinned linter turned the
tree red on the gate's first CI run with no code change.

**PowerShell 5.1 wraps native stderr as errors.** Piping PyInstaller output
through `Select-Object` trips `$ErrorActionPreference = "Stop"` and looks like
a build failure. Backticks in double-quoted here-strings also interpolate —
`` `r `` becomes a carriage return.

---

## 3. Architecture as it now stands

### Python

- **`gui/ws_commands.py`** — 99 registered WebSocket handlers taking an
  explicit `CommandContext` (`ws`, `state`, `msg`, `runs`). To add one, it must
  read only `ctx`, not mutate the endpoint's loop state, and finish in one
  request/response exchange.
- **`gui/chat_loop.py`** — `ChatRunLoop` owns the connection's chat state: the
  pending queue, the in-flight task, cancel bookkeeping, the clear cache. This
  is what unblocked the relocation; the commands were never conceptually
  entangled, they needed a variable only code inside one function could see.
  The message processor is injected to avoid `app → ws_commands → chat_loop →
  app`.
- **`harness/prompts.py`** — 95 methods of prompt construction and payload
  inference, reaching the application through a stated 13-name surface.
  `tests/test_harness_prompts_seam.py` fails if that surface grows.
- **Five commands stay in `websocket_endpoint`**: `mission_start`,
  `shell_exec`, `agent_restart` start a streaming run;
  `autonomous_mission_resume` and `mission_dispatch_autonomous` build the
  autonomous event forwarder bound to the socket.

### JavaScript

`ResonantApp` is split across prototype mixins, loaded as classic scripts
**before** `app.js`:

- `autonomous_view.js` — 52 methods, autonomous-session UI
- `settings_view.js` — 15 methods, everything that opens over the main view
- `run_cards.js` — 27 methods, live-run and task-card rendering

`handleEvent` resolves single-delegation events through
`RESONANT_EVENT_DELEGATES` before its switch; the remaining 100 cases carry
inline logic and stayed deliberately. `bindEvents` is an orchestrator over
seven `_bind*` groups.

**Adding a new static file requires**: `resonant.spec` datas,
`bundle-policy.json` required_globs, an `index.html` script tag before
`app.js`, and an `applyMixin` call. `_asset_version` globs the static
directory, so cache-busting is automatic.

---

## 4. Testing conventions established here

- **Never assert on a line range in a source file.** Several tests broke
  whenever code moved without the behaviour changing. Use
  `inspect.getsource(ws_commands.HANDLERS["name"])` for Python, and for
  JavaScript glob the static directory (`frontend_source()`) rather than naming
  files — naming them fails open, silently narrowing what the assertion sees.
- **`handles_event(source, name)`** checks both dispatch mechanisms, because
  "is this event handled" is the contract, not "which mechanism handles it".
- **Verify in a real browser.** Across this stretch the browser pass caught a
  `TypeError` on every render of the launch card, a mixin that would have
  copied nothing, a `KeyError` repr leaking into a user-facing message, and a
  command that had stopped being routable. None failed a test.
- **Stop the preview server when done** — a `steer` or `message` probe starts a
  real model turn and bills tokens until stopped.

---

## 5. Open work

**Frontend.** `app.js` is 9,990 lines. Remaining: the 100 inline `handleEvent`
cases (naming them is per-case design work, not a refactor script), and the
session/sidebar rendering.

**Restart-resume is worker-scoped.** `Session.restart_agent` re-dispatches an
interrupted worker. An autonomous *mission* still resumes through
`resume_autonomous_mission`, and a plain chat turn killed mid-stream is lost.

**Tree-sitter has zero test coverage.** `code_intelligence.py` imports it;
nothing in `tests/` does. CI deliberately omits the `code-intelligence` extra
rather than pretend to cover that path.

**From the 2026-07-01 harness analysis**, still open: tool-output eviction
before summarization, shadow-git checkpointing with rollback, todo recitation
at the context tail, wiring skills into interactive sessions, phase-scoped tool
tiering (~52 schemas sent on every call), glob-pattern bash permissions,
GLM-5.2 smoke baselines, tree-sitter repo mapping, and the architect/editor
split. Explicitly **do not build**: mid-session automatic model swapping, or
embedding RAG as primary retrieval.

**Deferred with rationale**: calling the GLM / Z.ai API directly for its own
quota instead of routing through Ollama Cloud. The user ruled out a second
cloud account and a second box; the AIMD governor is sufficient at current
load.

---

## 6. Release procedure

1. Bump `resonant_client/__init__.py` `__version__` **and** `pyproject.toml`
   `version` to the tag minus the `v`. CI reads `__init__.py`, not pyproject,
   and fails in ~15s on drift.
2. Write `docs/vX.Y.Z-release-notes.md` — user-visible behaviour, not
   internals. A restructuring release should say so plainly.
3. Run ruff and the full suite locally.
4. Push `main` and **wait for `Tests` and `Build check` to go green before
   tagging.** `release.yml` runs the same gate, so tagging onto a red tree
   fails later and noisier.
5. Tag, push, watch `Release`, then verify the published asset and that the
   appcast head is the new version.

`git push` needs `gh auth switch -u LA-Rich` first — `rbellantoni85` is often
active and gets a 403.

---

## 7. Startup performance — vendored frontend assets (v0.11.14)

**Symptom:** launch was getting slower.

**Cause:** `index.html` opened with five render-blocking external requests to
two hosts — a Google Fonts stylesheet (measured 273 ms, and it in turn pulls
the font files), plus marked, highlight.js, its theme CSS, and DOMPurify from
jsdelivr. `marked` was loaded from an **unpinned** `latest` URL, so the app's
markdown renderer could change under it without a commit.

**Fix:** `packaging/fetch_web_assets.ps1` downloads eight pinned assets
(~263 KB), verifies each against a recorded SHA-256 *before* extracting, and
writes them to `gui/static/vendor/`. `build_clean.ps1` runs it before
PyInstaller; `resonant.spec` globs the directory; `bundle-policy.json` requires
the files, so a fetch failure stops the build instead of shipping an app that
phones home at launch. Fonts moved to a local `fonts.css` with
`font-display: swap`.

**Result:** external requests at startup 7 → 0. Startup no longer depends on
the network at all, which also means it works offline.

Details worth keeping:

- **`vendor/` is gitignored**, same as `packaging/ripgrep/` — pinned binaries
  in git are permanent weight and every re-pin adds another copy forever. The
  consequence is that a fresh clone has no vendored libraries until
  `fetch_web_assets.ps1` runs.
- **That made `DOMPurify === undefined` a reachable state**, where before it
  was theoretical. `renderMarkdown` fed the unsanitized string to `innerHTML`
  in that case, and marked passes raw inline HTML straight through — with
  model output and tool-read file contents as the input. It now tracks whether
  the sanitizer actually ran and degrades to `textContent` when it did not.
- **The `marked` `highlight` callback in the constructor was already dead** —
  marked removed that option in v5 and the page was loading an unpinned build.
  Highlighting has been done by `hljs.highlightElement` over rendered
  `pre code` blocks for a long time. Removed rather than "fixed".
- **`defer` on the vendor scripts is safe** only because `app.js` merely
  registers a `DOMContentLoaded` handler at top level and constructs
  `ResonantApp` inside it; deferred scripts are guaranteed to run before that
  event. A top-level `marked.`/`hljs.`/`DOMPurify.` call in `app.js` would
  break this.
- **`_asset_version()` now globs `static/vendor/*`.** Vendored filenames are
  stable across upgrades (`marked.min.js` stays `marked.min.js`), so without
  it a re-pin that touched no top-level asset would leave every existing
  client on the cached old library.

Guarded by `tests/test_vendored_web_assets.py`, which asserts the template
references no external host, the policy requires each vendored file, the
sanitizer fallback cannot reach `innerHTML`, and the cache-buster sees
`vendor/`.
