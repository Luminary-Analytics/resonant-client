# Known Issues — Bug Ledger

Living catalog of known bugs surfaced during real usage. Each entry has reproduction steps, severity, and a fix proposal.

> **Convention:** issues are numbered chronologically across all sources (dogfood passes, release pipeline, post-release reports). Numbers are stable — even after a fix lands, the issue number stays in this doc as a historical record.

| # | Title | Severity | Status | Surfaced by |
|---|-------|----------|--------|-------------|
| 1 | Project dropdown's "Open another project…" no-op in browser mode | Medium | ✅ Shipped fix | Dogfood pass 1 |
| 2 | Silent cold-start when cloud model isn't loaded | Medium | ✅ Shipped fix | Dogfood pass 1 |
| 3 | Agent uses Unix tools (`tail`/`head`/`sed`) on Windows | Low | ✅ Partial fix | Dogfood pass 1 |
| 4 | Browser MCP `navigate` adds `https://` to `file://` URLs | Low | Won't fix (not our bug) | Dogfood pass 1 |
| 5 | Agent placed rocket outside playable area | N/A (design) | Closed | Dogfood pass 2 |
| 6 | WS listener dies during context compaction | Medium | Open | Dogfood marathon |
| 7 | Git pill stale on project switch | Low | ✅ Shipped fix (v0.2.1) | Dogfood marathon |
| 8 | Chat panel doesn't clear on project switch | Low | ✅ Shipped fix (v0.2.1) | Dogfood marathon |
| 9 | Backend swap drops conversation history | **High** | ✅ Shipped fix (v0.2.1) | Dogfood marathon (Pass 9) |
| 10 | Cross-backend swap-back returns silent empty turn | **High** | ✅ Shipped fix (v0.2.1) | Dogfood marathon (Pass 10) |
| 11 | Multi-deliverable prompts can silently skip hard parts | Medium | Open | Dogfood marathon (Pass 11) |
| 12 | PR-time PyInstaller smoke build missing | Medium | ✅ Shipped fix (v0.2.1) | Phase 3 release CI |
| 13 | Appcast updater duplicate-version refusal too strict | Low | ✅ Shipped fix (v0.2.1) | Phase 3 release CI |
| 14 | Local PyInstaller bundle 2.6× bigger than CI build | Low | Open | Phase 3 release |
| 15 | Release CI re-runs invalidate prior signatures (PyInstaller builds aren't byte-deterministic) | **High** | Workaround documented; preventative fix open | v0.2.1 release |
| 16 | Bundled exe console window visible in production | Low | Open (v0.2.2) | v0.2.1 install |
| 17 | Browser doesn't always auto-open after install | Low | ✅ Shipped fix (v0.2.2) | v0.2.1 install |
| 18 | Per-user install invisible to Windows Search | High (UX-blocking on Win11) | ✅ Shipped fix (v0.2.3) | v0.2.2 install |
| 19 | console=False crashes uvicorn ColourizedFormatter at startup | **Critical** (app won't launch) | ✅ Shipped fix (v0.2.4) | v0.2.3 install |
| 20 | Frozen `Path(__file__).parent` breaks Jinja2 template lookup | **Critical** (500 on every page) | ✅ Shipped fix (v0.2.5) | v0.2.4 install |
| 21 | Stderr redirect to /dev/null hid all runtime errors | High (debugging blind) | ✅ Shipped fix (v0.2.5) — now logs to ~/.resonant/logs/resonant-startup.log | v0.2.4 install |
| 22 | Pre-tag smoke test only used dev Python, not bundled exe | High (process gap, not code) | Open — fix in build-check.yml workflow + RELEASING.md update | This session |
| 23 | Starlette 0.29+ TemplateResponse signature change | **Critical** (homepage 500) | ✅ Shipped fix (v0.2.6) | v0.2.5 install |
| 24 | websockets not bundled — every WS upgrade fails | **Critical** (UI hangs at "Reconnecting...") | ✅ Shipped fix (v0.2.7) | v0.2.6 install |

---

## #1 — Project dropdown's "Open another project…" no-op in browser mode ✅ Shipped fix

**Severity:** Medium (silent UX failure costs trust)

**Repro:** click project pill → "Open another project…" → nothing visible happens

**Cause:** browser mode has no `pywebview` available, so the `folder_dialog` WebSocket command silently fails

**Fix shipped:** server emits `folder_picker_unavailable` event when no native picker is available; frontend redirects to the welcome screen with a status message. Lives in `resonant_client/gui/app.py` and `resonant_client/gui/static/app.js`.

---

## #2 — Silent cold-start when cloud model isn't loaded ✅ Shipped fix

**Severity:** Medium (60-90 sec of "thinking" with no feedback erodes trust)

**Repro:** send a message when `deepseek-v4-flash:cloud` isn't currently warm on the Ollama server. Watch the chat sit on "thinking" for 60+ seconds with no other feedback.

**Cause:** Ollama has to pull/load the cloud model on first use; that can take 60-120s for big MoE models. The frontend showed a generic "...thinking" indicator throughout, with no hint that the delay was *model load*, not the model thinking.

**Fix shipped:**
- Pre-warm the model in the background when the user selects it, so first-message latency = streaming latency only
- Cold-start banner above the input fades once the first text.delta arrives
- Elapsed-time hint after 5s on the thinking indicator: `...thinking (12s)` so the user can tell it isn't dead

Lives in `resonant_client/gui/app.py` (background warmup thread + `model_warmup_started`/`model_warmup_complete` events), `resonant_client/gui/static/app.js` (banner + elapsed-time UI), and `resonant_client/gui/static/styles.css`.

---

## #3 — Agent uses Unix tools (`tail`/`head`/`sed`) on Windows ✅ Partial fix

**Severity:** Low (wasted shell call + tokens, but agent self-corrects)

**Repro:** ask the agent for a file inspection task. Watch it call `tail`, `grep`, `head`, `awk`, etc. — these don't exist on stock Windows.

**Cause:** the system prompt didn't specify the shell environment / OS. The model defaults to Unix idioms.

**Fix shipped (partial):** added a platform/shell hint to the system prompt in `resonant_client/engine/session.py`:

> Use `python` not `python3`... Unix tools like `tail`, `head`, `sed`, `awk`, `grep`, `wc`, `find` are NOT available — use `file_read` for inspection, the `grep` agent tool for content search, and `glob` for path listing instead of shelling out.

**Why partial:** agent still tries Unix tools first ~10% of the time, but recovers cleanly to PowerShell on the next step. A more emphatic prompt ("Do NOT call wc / tail / head / sed / awk on Windows — they are guaranteed to fail; the agent will look incompetent if you do") would prevent the first attempt entirely. Half the value of a "negative" prompt is loud signaling.

---

## #4 — Browser MCP `navigate` adds `https://` to `file://` URLs

**Severity:** Low (not our codebase)

**Cause:** the Chrome MCP `navigate` tool I used to open game URLs added `https://` to my `file:///` URL, breaking the request.

**Workaround:** spin up `python -m http.server` on a free port and navigate to `http://127.0.0.1:8910/index.html` instead.

**Status:** won't fix — not a Resonant bug. Flagging for upstream.

---

## #5 — Agent placed rocket outside playable area (closed)

**Severity:** N/A (design quality issue, not a bug)

**What happened:** during dogfood pass 2, the agent set `rocketGroup.position.set(65, 5, 0)` — but orbs spawn at radius 12-55 from origin. Starting at distance 65 placed the rocket OUTSIDE the orb zone. Code worked; UX didn't.

**Argument:** when extending an existing app, the agent should **test its own work** by simulating user flows. The `verify` specialist in the planner pattern is meant for this. Bigger ask than a code fix.

---

## #6 — WS listener dies during context compaction

**Severity:** Medium (recoverable but disruptive)

**Repro:** during a long-running session, when the LLM client (Claude/Codex/etc.) compacts context, the resonant-server's WebSocket listener stops accepting new connections. The python process keeps running with significant resident memory and ESTABLISHED-only loopback connections — but no LISTEN socket.

**Surfaced in:** dogfood marathon, between Pass 5 and Pass 6.

**Recovery (manual):**

```powershell
Stop-Process -Name resonant -Force
python -m resonant_client gui --port 8909
```

**Hypothesis:** Windows-specific socket eviction during long-idle periods. May be related to TIME_WAIT accumulation or a Starlette/uvicorn idle disconnect.

**Fix proposal:** add a heartbeat / auto-rebind:
- Periodic self-ping to localhost:port
- If ping fails, log warning and rebind
- If rebind fails, exit cleanly so the parent can restart

Where to add: `resonant_client/gui/app.py` — likely a periodic task spawned at startup.

---

## #7 — Git pill stale on project switch

**Severity:** Low (cosmetic but trust-eroding)

**Repro:** switch projects via the project picker dropdown. Watch the bottom-left git pill — it stays on the previous project's branch + dirty count until a new session is dispatched in the new project.

**Surfaced in:** dogfood marathon (multiple passes).

**Fix proposal:** `resonant_client/gui/static/app.js` should call the git-status refresh handler immediately on `project.changed` event, not wait for a session creation.

---

## #8 — Chat panel doesn't clear on project switch

**Severity:** Low (same UX class as #7)

**Repro:** switch projects mid-conversation. The session list updates correctly but the chat view continues showing the previous project's last conversation until you click on a different session.

**Fix proposal:** render an empty state ("Pick a session or start a new one") when the active project changes. Likely lives in `resonant_client/gui/static/app.js` near the project-change event handler.

**Note:** #7 and #8 are likely a single fix — both stem from "project change doesn't trigger a UI refresh."

---

## #9 — Backend swap drops conversation history ✅ Shipped fix (v0.2.1)

**Severity:** **High** — directly damages user trust on a feature users will absolutely try.

**Original repro:**
1. Start a session on Ollama with a few turns of history
2. Mid-session, swap the backend dropdown from `ollama:deepseek-v4-flash:cloud` to `claude-code:haiku`
3. Send a follow-up message asking what was just discussed
4. Backend reports it has zero context

**Surfaced in:** dogfood marathon Pass 9 — Haiku said *"I haven't read any files yet in this session—this is our first turn"* despite 8+ prior turns visible in the chat.

**Root cause:** TWO problems compounded:
1. `Session.set_backend()` deliberately called `self.conversation_history.clear()` on every swap (one line in `engine/session.py`).
2. The GUI's `switch_model` WebSocket handler called `state.create_backend()`, which builds an entirely fresh Session object via `build_session()` — silently discarding the prior session's `conversation_history`, `todos`, `allowed_tools`, sandbox, autonomy_tier, and event_logger.

**Fix shipped (v0.2.1):**
1. `Session.set_backend(backend, *, reset_history=False)` — now defaults to NOT clearing. Callers that genuinely want a fresh start (TUI's `/model` and `/backend` commands which advertise "conversation cleared") pass `reset_history=True` explicitly.
2. New `AppState.swap_backend()` method in `gui/app.py` — mutates the existing session's `.backend` attribute instead of rebuilding from scratch. The `switch_model` WebSocket handler now calls this instead of `create_backend()`.
3. For CLI-wrapper backends (`claude-code`, `codex`) which manage their own session via `--resume <id>` and ignore our `conversation_history` list, the GUI emits a `backend_swap_warning` WebSocket event so the user knows even though our session-level history survived, the new backend's CLI process can't see it.

**Test pinned:** `tests/test_backend_swap.py` (6 tests, all pass) — verifies default-preserve, opt-in clear, round-trip preservation (the bug #10 case), and signature stability against future regressions.

**Files changed:**
- `resonant_client/engine/session.py` — `set_backend` signature
- `resonant_client/gui/app.py` — `swap_backend()` method + `switch_model` handler
- `resonant_client/tui.py` — 5 call sites updated to `reset_history=True` (preserves their explicit "conversation cleared" UX)
- `tests/test_backend_swap.py` — new file, 6 regression tests

---

## #10 — Cross-backend swap-back returns silent empty turn ✅ Shipped fix (v0.2.1)

**Severity:** **High** — same root-cause family as #9.

**Original repro:**
1. Start a session on Ollama
2. Swap to Claude Code mid-conversation (triggered #9)
3. Swap BACK to Ollama
4. Send a new prompt
5. Receive a 1-step / N-second "success" with **zero actual work** — no tool calls, no text response, no file written, just a green-checkmark card

**Surfaced in:** dogfood marathon Pass 10. Manifested as a 44-second "Worked for 44s" run that produced nothing.

**Root cause:** same as #9 — both swaps wiped history. By the time the user swapped back to Ollama, conversation_history was empty and the system prompt alone was insufficient context for Ollama to produce useful output.

**Fix shipped (v0.2.1):** preserved by the same change as #9. With the new `swap_backend()` method, history survives the round-trip Ollama → Claude Code → Ollama. The third regression test (`test_round_trip_swap_preserves_history`) pins this specific scenario.

---

## #11 — Multi-deliverable prompts can silently skip hard parts

**Severity:** Medium

**Repro:** ask the agent for an N-deliverable spec like:
> "Build a multiplayer extension with (1) Node.js server, (2) modify index.html for client-side WebSocket, (3) add README section, (4) verify syntax"

Agent ships deliverable #1 (the easy new file) but silently skips #2 (modify a 428-line existing file) and #3 (README), reporting "complete" with only the easy parts done.

**Surfaced in:** dogfood marathon Pass 11 (multiplayer ambition test). The agent shipped a clean 71-line `server.js` + `package.json` + `npm install`, but `index.html` and `README.md` were untouched.

**Hypothesis:** modifying a large existing file is a much bigger context-load than writing two new small files. The agent may hit a token budget or implicit "I've done enough" trigger.

**Fix proposal — "deliverable checklist" mode in the planner specialist:**
1. At intent intake, the planner restates the prompt as N explicit deliverables
2. Each deliverable becomes a node in the plan graph
3. The verifier specialist checks `git diff --name-only` against the deliverable list
4. Run-summary card shows ✓/⚠ per deliverable (not just file count)

Lives in `resonant_client/orchestration/` (planner + verifier specialists).

---

## #12 — PR-time PyInstaller smoke build missing

**Severity:** Medium (caused 1 of 3 v0.2.0 CI failures)

**Surfaced in:** Phase 3 v0.2.0 release.

**What happened:** the v0.2.0 release CI failed on its first run because `plan_graph_view.js` was referenced by `packaging/resonant.spec` but had never been git-committed (only existed locally). Local builds worked because the file was on disk; CI's clean checkout didn't have it.

**Root cause:** there's no validation that runs the full PyInstaller spec on a clean checkout before tag push.

**Fix proposal:** add a `.github/workflows/build-check.yml` that runs on every PR:

```yaml
on:
  pull_request:
    paths:
      - 'resonant_client/**'
      - 'packaging/resonant.spec'
      - 'pyproject.toml'

jobs:
  smoke-build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: pip install -e ".[gui,desktop,claude,openai]" pyinstaller
      - run: pyinstaller packaging/resonant.spec --clean --noconfirm
      - run: ./dist/resonant/resonant.exe --version
```

Doesn't sign or publish — just confirms the bundle builds. Would have caught the missing-file issue in seconds instead of after a tag push.

---

## #13 — Appcast updater duplicate-version refusal too strict

**Severity:** Low (caused 1 of 3 v0.2.0 CI failures, easy workaround)

**Surfaced in:** Phase 3 v0.2.0 release.

**What happened:** `packaging/update_appcast.py` refuses to add a version that's already in the feed. A placeholder v0.2.0 entry from earlier dev work was sitting on `gh-pages`; the script refused.

**Current behavior** (`packaging/update_appcast.py` around the duplicate-check):

```python
for existing in channel.findall("item"):
    existing_version = existing.find(f"{{{SPARKLE_NS}}}version")
    if existing_version is not None and existing_version.text == version:
        print(f"ERROR: version {version} already in appcast — bump and retry", ...)
        sys.exit(1)
```

**Fix proposal:** turn the refusal into an UPDATE — replace the existing entry instead:

```python
for existing in channel.findall("item"):
    existing_version = existing.find(f"{{{SPARKLE_NS}}}version")
    if existing_version is not None and existing_version.text == version:
        channel.remove(existing)   # replace instead of refuse
        break
```

Add a `--strict` flag to preserve the old refuse-behavior for paranoid releases (most won't need it).

---

## #14 — Local PyInstaller bundle 2.6× bigger than CI build

**Severity:** Low (cosmetic, doesn't block users)

**What:** local dev machine produces a ~64 MB installer; CI's `windows-latest` runner produces a ~26 MB installer from the same `packaging/resonant.spec`.

**Hypothesis:** local Python env has `numpy`, `PyQt5`, `cv2`, etc. system-installed; PyInstaller pulls them transitively despite `excludes=` directives. CI's clean env doesn't have those packages, so the bundle stays slim.

**Why it matters:** developers might worry their local builds are ballooning and don't realize CI is fine.

**Fix proposal:** add a `--exclude-module` / `--debug=imports` audit step to the spec that prints which transitively-pulled modules the excludes ARE catching vs missing. Useful for cleaning up the local dev experience.

---

## #15 — Release CI re-runs invalidate prior signatures ⚠️ HIGH

**Severity:** **High** — silently breaks the auto-update channel. Users see "Update is improperly signed" with no further explanation; auto-update appears compromised.

**Surfaced in:** v0.2.1 release.

**What happened:**
1. CI run #1 built + signed `resonant-setup-0.2.1.exe` → published to GitHub Release → my appcast got that signature.
2. The CI run failed at the very last step (gh-pages push, due to bug "user.email missing" in the workflow), so I **re-ran the failed jobs** (`gh run rerun --failed`).
3. The re-run rebuilt the installer from scratch — PyInstaller embeds build timestamps in the PE header, plus ZIP file timestamps inside the bundle, so the resulting bytes differed from run #1.
4. The re-run uploaded the new bytes to the SAME release tag, **silently overwriting** run #1's installer.
5. WinSparkle on a v0.2.0 client downloaded the NEW bytes, but my appcast still had the OLD signature → verification failed → "Update is improperly signed" dialog.

**Diagnostic:** `md5sum` the previously-downloaded installer and a fresh re-download. If they differ, this bug fired.

```bash
md5sum resonant-setup-0.2.1.exe                                                        # what I signed
curl -sL -o fresh.exe https://github.com/.../releases/download/v0.2.1/resonant-setup-0.2.1.exe
md5sum fresh.exe                                                                        # what users actually get
# If hashes differ, the asset was rebuilt and re-uploaded
```

**Workaround (used for v0.2.1):** re-download the current GH release asset, re-sign with the EdDSA private key, regenerate the appcast entry with the new signature + new file size, push gh-pages.

**Fix proposal — preventative:**
1. **Don't `gh run rerun` after asset upload.** If a release CI fails AT or AFTER the "Create GitHub Release" step, do NOT re-run — instead, manually re-sign whatever's currently in the release and push the appcast directly. The current release page is the source of truth once an asset is uploaded.
2. **Make builds reproducible.** PyInstaller has `--no-cipher` and `pythonOptimize` settings; combined with `SOURCE_DATE_EPOCH` env var (set to the commit time) for deterministic timestamps, the build CAN be made byte-reproducible. Pin this for v1.0.
3. **Compute + upload the signature alongside the binary.** Store `sparkle:edSignature` in a sidecar `.exe.sig` file next to the installer in the GH release. The appcast can then reference it. Then a re-build + re-sign in the same CI job ensures both bytes and signature are consistent. If they diverge, the appcast updater fails loudly instead of producing a mismatch.

For v0.2.x: add a CI step `Verify signature is consistent with uploaded asset` that downloads its own just-uploaded artifact and re-verifies. Cheap, catches this exact failure.

---

## #16 — Bundled exe console window visible in production

**Severity:** Low (cosmetic; doesn't affect functionality)

**Surfaced in:** v0.2.1 install (visible to user as a black-on-yellow PowerShell console showing the URL).

**Cause:** `packaging/resonant.spec` has `console=True`. Was kept on for v0.x debugging — first-install Ollama-connection / port-bind issues are easier to triage when stderr is visible.

**Fix proposal:**
1. **Add proper logging-to-file.** Currently errors go to stderr (which the console swallows when `console=False`). Need a `~/.resonant/logs/resonant-YYYYMMDD.log` rotation.
2. **Flip `console=False`** in the spec for v0.2.2+.
3. Optionally add `--debug` flag that re-enables console for power users.

---

## #17 — Browser doesn't always auto-open after install

**Severity:** Low (user can copy-paste the URL)

**Surfaced in:** v0.2.1 install. User saw a console with `Open in browser: http://127.0.0.1:53992` but no browser tab opened automatically.

**Cause:** Mixed launch paths. `installer.iss [Icons]` puts `Parameters: "gui --browser"` on the Start Menu shortcut, which DOES auto-open. But the `[Run]` post-install line uses `Flags: nowait postinstall skipifsilent` which can lose argument context in some user flows.

**Fix proposal:**
- Make `--browser` the **default** behavior in `gui/server.py`. Add a `--no-browser` opt-out for headless server scenarios.
- Update `installer.iss` to drop the `--browser` arg (since it'd be default).
- Update Start Menu + Desktop shortcuts likewise.

---

## How to add a new entry

1. Pick the next number (#15, #16, ...)
2. Add a row to the summary table at the top
3. Add a section below following the template:

```markdown
## #N — Title

**Severity:** Low / Medium / High

**Repro:** Step-by-step instructions to trigger the bug.

**Surfaced in:** when/where it was first noticed.

**Hypothesis** (if root cause is unclear) **or Cause** (if known): explanation.

**Fix proposal** (if not yet shipped) **or Fix shipped** (if landed): what to do / what was done. Include file paths.
```

4. If the bug is severe, also add a row to `PLAN.md` so it gets prioritized in the next sprint.
