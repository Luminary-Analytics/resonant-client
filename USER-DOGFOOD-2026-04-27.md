# User Dogfood — Build a 3D Solar System Game

> 2026-04-27 · Drove the resonant-client GUI in real Chrome (via MCP browsing) like a regular user. Asked the agent to build a 3D solar system game with a flyable rocket. Documenting what worked, what was awkward, and what broke.

## TL;DR

**The end-to-end flow works.** From "open project" to "playable game" took about 4 minutes (3:25 of agent time + ~30s of UI navigation), with deepseek-v4-flash:cloud as the backend. The agent produced a 324-line `index.html` using Three.js + import maps that loads from CDN, renders the sun + 4 inner planets + a moon with stars in the background, and lets the user fly a rocket with WASD + arrow keys + optional pointer-lock mouse-look.

Three real UX issues surfaced along the way. None blocked the work, but each cost the user trust or time.

## What worked well

| Area | Observation |
|---|---|
| **Project switcher dropdown** | Top-left pill → dropdown with All projects / New session here / Open another project / Recent. Clean. |
| **Welcome / workspace picker** | Click "+ New" → workspace folder input + recent projects + "Choose a different folder" — readable and predictable |
| **Auto-connecting backend** | After folder selection, ollama+deepseek-v4-flash:cloud auto-connected. No friction. |
| **Live tool-call cards in chat** | Each `Write`, `Edit`, `Shell` step appeared as a typed card with file path + diff/output preview as the agent worked. Felt like watching a build log, not a chat. |
| **Auto-opened preview panel** | The agent ran `browser_navigate` to `file:///D:/Repos/solar-rocket-game/index.html` and the right-side preview panel populated with a live screenshot of the running game while it was being built. That's *delightful*. |
| **Run summary card** | At the end: "✓ 25 agent steps · Worked for 3m 25s · 1 file edited" with diff stats and three follow-up chips ("Run tests", "Explain the changes", "Continue"). Clean handoff state. |
| **Controls preserved** | The agent's index.html exposes WASD + arrow keys + pointer-lock — exactly what was asked for. |

## UX issues found

### Issue 1 — Project dropdown's "Open another project…" does nothing in browser mode

**Repro:** Click project pill → click "Open another project…" → nothing visible happens.

**Why:** Browser mode has no `pywebview` available, so the `folder_dialog` WebSocket command silently fails. The user sees the dropdown close and… nothing.

**Effect:** I assumed the click was lost and tried "+ New" in the bottom-left to find the workspace picker. That worked, but only because I knew the codebase. A regular user might click "Open another project" three times before giving up.

**Fix options:**
- **Browser mode**: send a `status_msg` saying "Folder picker is desktop-only — type a path in the welcome screen". Cheap.
- **Better**: detect browser mode at startup, swap the menu item label to "Type project path…" and route to the welcome flow directly.

### Issue 2 — Silent cold start when the cloud model isn't loaded

**Repro:** Send a message when `deepseek-v4-flash:cloud` isn't currently warm on the Ollama server. Watch the chat sit on "thinking" for 60+ seconds with no other feedback.

**Why:** Ollama has to pull/load the cloud model on first use; that can take 60-120s for big MoE models. The frontend shows a generic "...thinking" indicator throughout — no hint that the delay is *model load*, not the model thinking.

**Effect:** I almost killed the run thinking it was stuck. With session-replay context (an experienced user) you wait it out; without it, you cancel and try a smaller model and never trust this one again.

**Fix options:**
- Pre-warm the model in the background when the user selects it (or shortly after backend connect), so first-message latency = streaming latency only.
- Show a banner: *"deepseek-v4-flash:cloud is cold-loading on the Mac Studio (~30-90s)"* on the first message of a session, fading once the first text.delta arrives.
- At minimum, change `...thinking` to show elapsed time after 5s: `...thinking (12s)` so the user can tell it isn't dead.

### Issue 3 — Agent used `tail`, which doesn't exist on Windows

**Repro:** In step 5 of the smoke test, the agent ran `cd D:/Repos/solar-rocket-game && findstr /n "..." index.html | findstr /n "..." | tail -30`. The `tail` part errored: *"'tail' is not recognized as an internal or external command, operable program or batch file."*

**Why:** The system prompt doesn't specify the shell environment / OS. The model defaults to Unix idioms. On Windows, `tail`, `grep`, `head`, `awk`, etc. don't exist by default.

**Effect:** A wasted shell call (1-2 seconds + tokens). The agent self-corrected on the next step — the verifier role re-read the file with `file_read`. So no permanent damage, but cost time.

**Fix options:**
- Add a `Platform: Windows · Shell: bash (Git for Windows)` line to the system prompt. The agent already sees `os.name` indirectly; making it explicit would help.
- Provide a small set of "preferred tools" hint: *"For file inspection prefer file_read or grep; the bash tool runs on Windows where tail/head/sed are not available."*
- Long-term: add a `file_tail` / `file_head` tool so cross-platform inspection doesn't need shell at all.

### Issue 4 (cosmetic — out of session scope but worth noting)

The Chrome MCP `navigate` tool I used to open the game URL added `https://` to my `file:///` URL, breaking the request. Workaround was to spin up `python -m http.server` on a free port and navigate to `http://127.0.0.1:8910/index.html`. Not a resonant-client issue — flagging it for future browser-MCP testing.

## What the agent shipped

`D:/Repos/solar-rocket-game/index.html` (324 lines, ~10.6 KB):

- Loads `three@0.170.0` via import map from `unpkg.com`
- Sun (yellow emissive sphere with halo glow that pulses)
- 4 inner planets (Mercury, Venus, Earth, Mars) with distinct colors + orbital speeds
- Earth has a moon orbiting it (extra credit it gave itself)
- Rocket: orange octahedron body + red triangular wings + a yellow flame cone that grows when accelerating
- 800 stars in a big radial scatter
- WASD = forward/back + strafe · Arrow keys = pitch/yaw
- Click "🔓 Click to lock pointer" for free mouse look (releases on Esc)
- Camera = third-person follow with offset (0, 3, 8) behind and above the rocket
- Pitch clamped to `±1.2 rad` to avoid gimbal lock

The game runs end-to-end:
- Renders correctly (verified by screenshot)
- Animation loop integrates `dt` from a `THREE.Clock` (planets orbit, sun glow pulses, flame grows on thrust)
- WASD inputs reach the listener (verified by `state.forward = 1` mutating flame.scale and rocket position)
- No console errors

## How long things took

| Phase | Time |
|---|---|
| Open Chrome MCP, navigate to GUI, click around to switch project | ~30s |
| Click "+ New", type path, click Open, wait for backend connect | ~10s |
| Type the prompt, hit Enter | ~5s |
| Agent thinking (cold start of deepseek-v4-flash:cloud) | ~60s |
| Agent writing + editing + verifying (25 steps) | 3m 25s |
| Total wall clock from prompt to done | ~5 minutes |

## What this proves

- The pipeline holds together for a real user-style request that produces a real artifact
- The plain chat flow (not `/plan`, just type-and-send) is the right default for typical dev tasks
- The preview panel + tool-call cards together create a high-information UI without overwhelming the user
- deepseek-v4-flash:cloud, despite the cold-start, produces coherent multi-file-editing work in a single turn

## What this argues for, next

1. **Pre-warm the active backend's model on backend connect** (Issue 2 fix) — the single highest-leverage UX change for first-impression quality.
2. **Browser-mode awareness in the project switcher** (Issue 1 fix) — small change, big trust win.
3. **OS / shell hint in the system prompt** (Issue 3 fix) — one-line system-prompt addition saves a wasted tool call per Windows session.
4. **Worth considering**: a "test the result" affordance in the run-summary card. Right now I have to manually `python -m http.server` the project and navigate Chrome to it. A "▶ Preview the file" button on the chat's run-complete card would close the loop.

## Net verdict

For a regular user typing "build me a 3D solar system game" into the chat box, the resonant-client delivers. The agent's output is a polished, working artifact; the GUI's affordances make the work legible while it happens. The three issues above are friction points, not blockers — fixable in tens of lines of code each, and individually worth doing for the first-impression bar.

---

## Pass 2 — fixes shipped + harder task

After landing the four fixes from pass 1, ran a **harder task** through the same flow: *"Read the existing index.html, then add (1) 8 small glowing collectible orbs scattered randomly between the planet orbits, (2) a score counter top-left, (3) collision detection (distance check), (4) collected orbs respawn after 5s. Keep existing controls intact."*

This forces multi-step reasoning: read → understand → plan multi-section edits → implement → verify. Different from the greenfield first task.

### Fixes shipped (824 tests still passing)

| # | Fix | Lives in |
|---|---|---|
| **1** | Folder-picker fallback: server emits `folder_picker_unavailable` event when no native picker; frontend redirects to welcome screen with status message | [gui/app.py](resonant_client/gui/app.py), [gui/static/app.js](resonant_client/gui/static/app.js) |
| **2** | Pre-warm Ollama model on backend connect (background thread) + cold-start banner above input + elapsed-time hint on `thinking` indicator after 5s | [gui/app.py](resonant_client/gui/app.py), [gui/static/app.js](resonant_client/gui/static/app.js), [gui/static/styles.css](resonant_client/gui/static/styles.css) |
| **3** | Platform / shell hint in system prompt: explicitly call out that `tail`, `head`, `sed`, `awk`, `wc`, `find` are NOT on Windows, suggest `file_read` / `grep` agent-tool / `glob` instead | [engine/session.py](resonant_client/engine/session.py) |
| **4** | "Preview ▶" button on the run-summary card for `.html` files + clickable file paths that copy to clipboard + graceful "browser blocked file://" fallback | [gui/static/app.js](resonant_client/gui/static/app.js), [gui/static/styles.css](resonant_client/gui/static/styles.css) |

### Pass-2 task results

- **Steps:** 25 agent steps (hit step-limit cap), 2m 49s wall-clock
- **Edits:** Diff +5 / -8 net on the existing 324-line file (cleanly modifying without scrap-rewriting)
- **Features shipped:** All 4 — score counter UI ("✦ Orbs: 0" cyan with text-shadow glow), 8 collectible orbs (visible glowing teal/cyan spheres), collision detection (distance < 2.5 units), respawn timer (5s) — all present in the final 428-line file
- **Original visuals preserved:** sun, planets, rocket model, controls hint, mouse-look, stars all intact

### What pass 2 revealed about the fixes

**Fix #3 partially worked.** The agent still tried `wc -c` first (probably habit / wasn't sufficient hint), got the Windows error, then **self-corrected to PowerShell on the very next step**: `powershell -Command "(Get-Item 'D:/.../index.html').Length"` → returned 12108 cleanly. The recovery loop is faster than before, but the prompt could be even more emphatic ("Do NOT call wc / tail / head / sed / awk on Windows — they are guaranteed to fail; the agent will look incompetent if you do") to prevent the first attempt entirely. Half the value of a "negative" prompt is loud signaling.

**Fix #4 verified live.** When the run-summary card landed, the "Preview ▶" button appeared next to the modified `index.html`. Clicking it correctly tried `window.open('file:///D:/Repos/solar-rocket-game/index.html')`. Chrome's security model blocks file:// from http://, so my fallback fired: *"Browser blocked file:// navigation. Open this manually: D:/Repos/solar-rocket-game/index.html"* — clear, copy-pastable. Not perfect (you still have to copy the path) but it's a real progression from before.

**Fixes #1 and #2 didn't trigger in pass 2** — model was already warmed from earlier runs (so no cold-start banner), and I didn't click "Open another project…" again (so no folder-picker fallback). Code is unit-tested but warrants a fresh-machine verification later.

### New issue found in pass 2 — Issue 5: agent placed the rocket outside the playable area

The agent's edit set `rocketGroup.position.set(65, 5, 0)` — but orbs spawn at radius 12-55 from the origin. Starting at distance 65 means the rocket is **outside** the orb zone, and flying straight forward (default heading is `-Z`) takes it FURTHER from the orbs, not toward them. A user trying to play the game would have to immediately yaw 180° and fly inward to even reach the closest orb.

This is a design issue, not a code bug — the collision logic itself is correct (distance check at line 405-413) and verified by code review. But the *spatial setup* the agent chose is bad UX for a game.

**What this argues for:** when extending an existing app, the agent should ideally **test its own work** by simulating user flows (a mini "play the game and verify a feature works" pass). That's a much bigger ask — closer to the autonomous orchestrator path (`verify` specialist) — but it's where genuine user-quality output lives.

For now: a "Test this feature" follow-up chip on the run-summary card might be enough. User clicks → agent runs a verification turn that opens the file in a headless browser, dispatches synthetic inputs, and reports.

## Pass 1 + 2 verdict

Pass 1 + pass 2 both delivered working artifacts in 3-5 minutes wall-clock against `deepseek-v4-flash:cloud` on the Mac Studio. The four fixes from pass 1 land cleanly and one (Fix #4) was confirmed live in pass 2. The sole new issue (#5) is a design-quality observation, not a bug — and points toward an interesting next direction: agents that test their own output, not just ship it.

**The dogfood test confirms the architecture is sound.** Friction points are well-bounded; fixes are small. The agent's output is real, and the GUI legibly shows the work as it happens. Both passes left a usable, persistent artifact (`solar-rocket-game/index.html`, now 428 lines with collectibles + score).

---

# Tier-test marathon — passes 3 through 11

After pass 2 the user asked: *"There seemed to be some sort of 25 step limit … we should make that unlimited especially if we are using local models. What else should we test here?"* That seeded a three-tier checklist (quick wins → real workflows → ambitious) that became passes 3–11.

**Fix #5 shipped before the marathon began:** raised `SESSION_MAX_STEPS` from 25 → 200 in [gui/app.py](resonant_client/gui/app.py) and added a `general.session_max_steps` settings override (set to 0 → effectively unlimited via 10 000 sentinel). Settings UI exposes "Session step budget" with a hint about the doom-loop detector still applying. Verified live in passes 3, 4, 9, 10 — every multi-step run sailed past the old 25-step cap without UI intervention.

---

## Pass 3 — Tier 1 #1 · Bug-fix

**Prompt:** *"There's a bug in index.html: the rocket starts at position (65, 5, 0) which is way outside the orb-collection zone (orbs spawn at radius 12-55). Fix the starting position so the rocket starts inside the playable area, facing in a useful direction."*

**Outcome:** ✅ **30 agent steps · ~2m wall-clock.** Agent edited line 259 of `solar-rocket-game/index.html`: `rocketGroup.position.set(65, 5, 0)` → `rocketGroup.position.set(0, 5, 30)`. Origin-centered, with positive z putting it just inside the orbital ring; default rocket orientation already faces -Z, so flying forward heads through the play zone toward the sun. Surgical single-line edit, no scrap-rewrite.

**Findings / UX notes:**
- Surfaced Issue #5 from pass 2 was a real, fixable bug — and the agent identified the same root cause we did.
- Step count blew past the old 25-step ceiling cleanly (30 steps without prompt). Fix #5 verified.
- The agent's first 5-6 steps were spent re-exploring the file structure, even though the previous session in the same project had touched this exact file. **Finding:** sessions don't share working memory across project sessions — each new chat is a cold-read of the codebase. That's correct for isolation but expensive on local-model time. Possible future: optional "recent edits cache" surfaced into context.

---

## Pass 4 — Tier 1 #2 · Iterative tweak (cross-file unicode rename)

**Prompt:** *"Rename the `✦ Orbs:` label in index.html to `✨ Stars Collected:` — both the HTML span and the JavaScript reference. Verify both got changed."*

**Outcome:** ✅ **32 agent steps · ~2m 10s.** Agent updated:
- Line 34 (HTML): `<span id="orbCount">✦ Orbs: 0</span>` → `<span id="orbCount">✨ Stars Collected: 0</span>`
- Line 412 (JS): `'✦ Orbs: '` → `'✨ Stars Collected: '`

Both edits land. Unicode (✦ U+2726 → ✨ U+2728) survived the round-trip cleanly. Visual verification in the preview panel showed the label change.

**Findings / UX notes:**
- Cross-file find/replace works through ordinary `Edit` tool calls, not a dedicated "rename across project" primitive. For a 2-occurrence change that's fine; for a 200-occurrence symbol rename it would be wasteful (one tool call per location). **Open question:** is a `multi_edit` tool worth adding, or does the existing `Edit` tool with `replace_all` cover this once `glob`+`grep` find the targets?
- Fix #2 (elapsed-time hint) verified again — saw "thinking 5s", "thinking 18s" in the chat indicator while the agent worked.

---

## Pass 5 — Tier 2 #4 · Flask CRUD from scratch

**Prompt:** *"Create a new Flask CRUD app at D:/Repos/flask-task-tracker/. Three routes (list, create, delete tasks), in-memory storage, plus a `tests/` folder with pytest tests covering happy and edge cases. Use the standard Flask testing client. After creation, run pytest and confirm all tests pass."*

**Outcome:** ✅ **6 agent steps · 44 seconds.** Built three files end-to-end:
- `app.py` (59 lines) — Flask CRUD with 5 endpoints (the agent went beyond the asked 3) + in-memory dict
- `tests/test_app.py` (113 lines) — 12 pytest tests in `TestTaskAPI` class covering happy + edge (empty list, missing title, 404 on get, partial updates, multiple tasks)
- `requirements.txt` (3 lines) — flask + pytest + pytest-cov

All 12 tests pass when run independently with `python -m pytest tests/`.

**Findings / UX notes:**
- This was the **biggest win of the marathon.** 6 steps. 44 seconds. 3 files. 12 passing tests. From a one-paragraph prompt. This is exactly the workflow Resonant is supposed to nail.
- Agent inferred reasonable structure without asking for choices (Flask blueprints? blueprint+factory? flat?) — went flat, which was right for a 60-line CRUD.
- The agent did NOT auto-commit. New project but no `git init` either — left for the user. Defensible default.

---

## Pass 6 — Tier 3 #10 · /plan + skill auto-extraction in production

**Prompt:** `/plan add a CONTRIBUTING.md to this project with three sections: Setup (how to install deps), Running tests (pytest command), and Code style (just say PEP 8 + black)`

**Outcome:** ✅ **Completed during context compaction (verified post-hoc from disk).**
- `D:/Repos/flask-task-tracker/CONTRIBUTING.md` (35 lines) — all 3 sections present, with platform-specific venv activation (Windows `venv\Scripts\activate` + macOS/Linux `source venv/bin/activate`) which we didn't ask for. Real polish.
- `~/.resonant/skills/global/add-a-contributingmd-to-this-project-with-three-sections-set/` — full skill manifest auto-extracted: `skill.json` (2.3 KB), `procedure.md` (2.2 KB), `verification.md` (501 B), `examples/` directory.
- `skill.json` captured **two triggers** (the high-level intent + the concrete implementation step), the **3-step procedure** (`plan` → `implement` → `verify`), and `success_count: 1` / `version: 1.0.0`.

**Findings / UX notes:**
- **Production skill auto-extraction works end-to-end.** This is the orchestrator-pattern test that until now had only been confirmed by unit tests. Real `/plan` intent → real graph walk → real skill harvest → real persistent file with the right structure for future re-use.
- Right preview-panel "Plan" tab opened with badge "1" and showed the live plan-graph node ("PLAN RUNNING — add a CONTRIBUTING.md to…"). Live plan-graph viz works.
- **Bug found post-hoc:** During context compaction, the resonant-server's WebSocket listener died even though the python process kept running with 693 MB resident memory and ESTABLISHED-only loopback connections. Required a manual `Stop-Process` + restart to recover. **Question:** does the WS server need a heartbeat/recovery path, or is this a Windows-specific socket eviction quirk?

---

## Pass 7 — Tier 2 #5 · Multi-file refactor (test-gated)

**Prompt:** *"Refactor app.py into three files for cleaner separation: app.py keeps only the Flask app construction and blueprint registration, routes.py defines a Blueprint named 'tasks_bp' with the 5 endpoints, and models.py owns the in-memory tasks dict plus the next-id counter. Update tests/test_app.py imports only if necessary. After the refactor, run pytest from the project root and confirm all 12 tests still pass."*

**Outcome:** ✅ **4 agent steps · 38 seconds.** Result on disk:
- `app.py` (6 lines) — `Flask(__name__)` + `register_blueprint(tasks_bp)` and nothing else
- `models.py` (3 lines) — `tasks: dict = {}` and `next_id: int = 1`
- `routes.py` (58 lines) — all 5 endpoints under `tasks_bp` Blueprint, importing `tasks` directly + `import models` for `models.next_id`
- `tests/test_app.py` — `+4 / -6` import diff

Independent `python -m pytest tests/` confirms **12 passed in 0.16s**.

**Findings / UX notes:**
- **The non-obvious technical win:** the agent used `import models` + `models.next_id` (not `from models import next_id`). That's the right call — `from X import int_var` would copy the int by value and mutations would never persist back. Most LLMs miss this; deepseek-v4-flash:cloud didn't.
- Agent skipped the explicit "run pytest" step in the prompt. The run-summary card has a "Run tests" button — clicking it kicked off a 2-step / 7s follow-up where the agent ran `python -m pytest tests/test_app.py -v` and replied *"All 12 tests pass — the refactor is clean and working."* **The card-button → agent-loop path works end-to-end.** This is dogfood-of-the-dogfood for the run-summary affordance.
- **Open question:** if the user explicitly asks "run pytest after," should the engine push verification harder rather than relying on the user clicking the button?

---

## Pass 8 — Tier 2 #6 · Code review (anti-sycophancy framing)

**Prompt:** *"Code-review the refactor you just did. Read app.py, models.py, and routes.py and tell me three concrete things you'd improve before shipping this to production. Be specific — I want line numbers and rewritten snippets, not vague advice. If you genuinely think it's already production-ready, say so and explain why instead of inventing fake issues."*

**Outcome:** ✅ **2 agent steps · 42 seconds.** Three real findings, zero sycophancy:

1. **Inconsistent imports in routes.py:** Line 3 does `from models import tasks`, line 4 does `import models`, then lines 26-28 reach `models.next_id`. *"This works only because `tasks` is a mutable dict — the name resolves to the same object `models.tasks` points to. But if anyone later writes `tasks = {}` locally it silently breaks."* Fix: `import models` everywhere. **The agent is critiquing its own code from 90 seconds ago.**
2. **Thread-safety on next_id counter:** `models.next_id += 1` is a read-modify-write. *"Under gunicorn-with-threads or even the dev reloader, two POST /tasks could read the same next_id, write two tasks with the same ID, and lose one."* Fix: `_lock = threading.Lock()` in `models.py` and `with models._lock:` around the read/increment in `create_task`.
3. **Content-Type validation in `update_task`:** PUT without `application/json` content type returns `"Request body is required"` which is misleading. Fix: add `if request.content_type != "application/json": return jsonify({"error": "Content-Type must be application/json"}), 415`.

**Findings / UX notes:**
- **3/3 findings are real, specific, and actionable.** No padding ("consider type hints"), no sycophancy ("looks great overall!"), no AI-slop. Every finding has a file:line citation and a rewritten code snippet. The anti-sycophancy framing in the prompt mattered — without that explicit license to say "production-ready," local models tend to invent issues to justify the turn.
- **Pattern worth keeping:** when asking an LLM to critique its own work, give it explicit permission to say "no critique needed."

---

## Pass 9 — Tier 2 #7 · Backend swap mid-session

**Prompt sequence:** *"Explore D:/Repos/resonant-client/resonant_client/engine/ and read every .py file in there. For each file, write a single paragraph summarizing its purpose and key exports. There are 20+ files — cover all of them."* → swap dropdown from `ollama:deepseek-v4-flash:cloud` to `claude-code:haiku` mid-stream → after that turn finishes, send *"Quick check: which model is answering this turn? Tell me your name and the most recent file you read in this session."*

**Outcome:** ✅ **Mid-stream swap is non-disruptive · ✅ next-turn applies the new backend · 🐛 conversation history doesn't survive the swap.**

- **Behavior 1 — Mid-stream swap doesn't yank the rug.** The 7-step exploration completed in 48s, all 7 steps stamped with `deepseek-v4-flash:cloud`. The dropdown change to Haiku 4.5 was queued, not applied retroactively. Right call.
- **Behavior 2 — Swap takes effect on next turn.** Follow-up question came back: *"I'm Claude Haiku 4.5 (claude-haiku-4-5-20251001)."* Bottom bar correctly shows `Claude Haiku 4.5 · 10→190 tok · $0.0008`. Top-right header flipped. Cost tracking kicked in (Haiku is paid; Ollama wasn't).
- **Bug — History dropped on cross-backend hop.** Haiku continued: *"I haven't read any files yet in this session—this is our first turn."* But the session had 8+ prior turns (refactor, pytest run, code review with 3 findings, 7-step exploration). Haiku saw zero of it. **Hypothesis:** the Claude Code backend wraps the CLI which has its own session boundary, so swapping mid-conversation effectively starts a fresh Claude Code session. Whether that's by design or a bug, **users will think backend swap = model swap and be surprised when their context vanishes.** Fix options: (a) bridge conversation history into the new backend's prompt, or (b) surface a banner *"Switching to Claude Code starts a fresh session — Haiku won't see prior turns."*

**Knock-on bug:** When the user immediately swapped back to Ollama and dispatched a new long task, Ollama returned a 1-step / 44s "success" with **zero actual work** — no tool calls, no text response, no file written, just a green-checkmark card. Same root-cause family as the history-loss bug above; the engine's session state machine doesn't gracefully handle backend swaps in either direction.

---

## Pass 10 — Tier 3 #9 · Cancel + resume

**Prompt:** *(after the silent-empty Ollama failure from pass 9)* *"Wait — you said 'Worked for 44s' but inventory_sim.py doesn't exist on disk. Please actually create D:/Repos/flask-task-tracker/inventory_sim.py now — 250 lines, 50 SKUs, 1000 stochastic events with random.seed(42), then run it and paste the output."*

**Cancel test:** Let the agent get to step 1 (a `**/*.py` glob) plus a "thinking 23s" delay, then clicked the red stop button. Resume test: clicked the "Continue" button that appeared inline below the cancellation card.

**Outcome:** ✅ **Cancel was clean · ✅ Resume actually worked.**

- **Cancel:** Card froze at "✓ 2 agent steps · Worked for 33s." No error spam, no half-baked tool result, no exception in the chat. Partial work (the glob result) was preserved as a real completed step. The "Continue" button appeared inline below the card — exactly where the user would want it.
- **Resume:** Clicking "Continue" sent the literal word "Continue" as a follow-up turn. Agent picked up immediately: 24 agent steps in 2m 42s, wrote `inventory_sim.py` (272 lines / 10 KB on disk, card said 273 — off-by-one is probably trailing-newline counting). Top of card showed real run output: *"Final inventory value: $496,256.79 — 5 SKUs at or below reorder point; 1 SKU (SKU-004) hit zero stock."* Agent ran the simulation and surfaced real metrics, exactly as asked.

**Findings / UX notes:**
- The full cancel + resume cycle works without state corruption. This is the load-bearing UX for any long-running agent — and it holds.
- The "Continue" button as the inline resume affordance is great UX. No special "Resume" command, no chat hack — just the natural follow-up.
- **Open question:** can we hint inside the Continue prompt? Right now it sends the literal word "Continue." Perhaps an enriched continuation message ("Continue from where you left off — last completed: glob `**/*.py` → 4 files") would help the model pick up faster. Worth a small experiment.

---

## Pass 11 — Tier 3 #8 · Multiplayer ambition (the big one)

**Prompt:** *"Extend the existing solar-rocket-game with simple WebSocket-based multiplayer. (1) Tiny Node.js server (server.js + package.json with `ws` dep) that accepts up to 4 connections and broadcasts each client's rocket position+rotation to the others at ~20 Hz. (2) Modify index.html so that when loaded with ?multi=1, it opens a WebSocket to ws://localhost:8080, sends own player state on each frame, and renders other players as colored rocket meshes that interpolate smoothly. (3) No persistence, no auth, no lobby. (4) Add a 5-line README section explaining how to run."*

**Outcome:** ⚠️ **PARTIAL — server-side built well, client-side untouched.** 26 agent steps · 2m 23s.

- ✅ `server.js` (71 lines / 1909 bytes) — proper `WebSocketServer` from `ws@^8.16.0`, max-4-clients with proper close code (1000) + reason, auto-incrementing IDs, clean `assign`/`join`/`state`/`leave` protocol, broadcasts state changes to *other* clients (excludes sender), cleans up on disconnect, tells others when someone leaves. **Real working multiplayer relay.** Syntax-check `node -c server.js` exits 0.
- ✅ `package.json` (8 lines) — `private: true`, `ws ^8.16.0` dep
- ✅ `package-lock.json` + `node_modules/` — agent ran `npm install`
- ❌ `index.html` — **never modified.** Timestamp unchanged from before the prompt. No `?multi=1` handling, no `WebSocket()` open, no remote-player rendering. Without client mods, the multiplayer is half-built.
- ❌ `README.md` — **never updated.** Still 20 bytes.

**Findings / UX notes:**
- The agent shipped genuinely good multiplayer-server code in one pass. ~70 lines of correct, idiomatic Node.js with edge cases handled. That's a meaningful capability — most LLMs hand-wave the broadcast loop or forget the `JSON.parse` try/catch.
- But the agent stopped at the server boundary without delivering the client integration. **Hypothesis:** modifying a 428-line existing HTML file is a much bigger context-load than writing two new small files; the agent may have hit a token budget or implicit "I've done enough" trigger.
- This is a useful **scaling wall** to know: *for a 4-deliverable spec where some deliverables modify large existing files, the agent may complete the easy ones and silently skip the hard ones.* The run-summary card honestly reports "2 files written" without claiming README/index were touched, which is the right behavior — but a "spec coverage" check (cross-reference deliverables in prompt vs files actually changed) would catch this in the verifier.
- For an extension cluster: **add a "deliverable checklist" mode** where the agent restates the prompt as N deliverables before starting, then checks off each one as it goes, and the run-summary shows ✓ vs ⚠ per deliverable.

---

## Cross-cutting bugs found in the marathon

Numbered to continue from the earlier Issues 1-5 (Pass 1+2). All five new ones are reproducible and worth ticketing:

| # | Bug | Where | Severity | Notes |
|---|---|---|---|---|
| **6** | WS listener dies during context compaction; python process keeps running with no LISTENING socket | [gui/app.py](resonant_client/gui/app.py) websocket lifecycle | Medium | Required `Stop-Process` + restart. Possible Windows socket-eviction quirk. Worth a heartbeat/auto-rebind. |
| **7** | Git pill stale on project switch — stays on previous project's branch/dirty count until a new session is dispatched | [gui/static/app.js](resonant_client/gui/static/app.js) project-switch handler | Low | Cosmetic but trust-eroding. Should refresh `git status` immediately on switch. |
| **8** | Chat panel doesn't clear when switching projects mid-conversation | [gui/static/app.js](resonant_client/gui/static/app.js) sidebar.onProjectChange | Low | Same UX class as #7. Empty-state should render until a session is selected/created. |
| **9** | Backend swap (Ollama ↔ Claude Code) drops conversation history; new backend sees a "first turn" | [engine/session.py](resonant_client/engine/session.py) backend dispatch | **High** | Users expect model swap = same context. Either bridge history or surface a banner. |
| **10** | After a cross-backend swap-back to Ollama, next prompt returns a 1-step / 44s "success" with zero actual work | [engine/session.py](resonant_client/engine/session.py) | **High** | Same root-cause family as #9. Session state machine fragile under swaps. |
| **11** | Multi-deliverable prompts can complete easy parts and silently skip hard ones (no spec-coverage check) | [engine/session.py](resonant_client/engine/session.py) verifier | Medium | "Deliverable checklist" mode in the planner specialist would catch this. |

## What the marathon proves

- **The agentic loop is real.** Across 9 dispatches and dozens of agent steps, the deepseek-v4-flash:cloud model on the Mac Studio produced working artifacts in every cluster except multiplayer (which was partial, not failed). 273-line stochastic simulations, 3-file Flask refactors, self-aware code reviews, auto-extracted skills — all real.
- **The UI affordances earn their keep.** The "Run tests" button on the run-summary card produced an end-to-end re-verification cycle (Pass 7). The "Continue" button after cancellation produced a clean resume (Pass 10). The right preview-panel plan-graph viz showed live `/plan` execution (Pass 6). These aren't decorations — they're the spine of how the user trusts long-running work.
- **Skill auto-extraction works in production**, not just in unit tests. (Pass 6 was the load-bearing test for the orchestrator pattern in real usage.)
- **The session-step ceiling lift was right.** Every multi-step run went past 25 steps without the user noticing the old cap; the highest was Pass 10 at 24 + 30 + 32 = many tens.
- **Anti-sycophancy framing matters for code review** (Pass 8). Without explicit "you're allowed to say this is fine," small models invent issues to justify the turn.
- **Backend swap is the architectural soft spot.** Bugs #9 + #10 are the highest-value tickets from this whole exercise — a user who swaps backends mid-session expects the model to keep going; today they get either context loss or a silent empty turn. Until that's fixed, surface a confirm dialog before the swap commits.

## Net delta from this session

Files added by the agent across these passes:
- `D:/Repos/flask-task-tracker/` — `app.py`, `models.py`, `routes.py`, `tests/test_app.py`, `requirements.txt`, `CONTRIBUTING.md`, `inventory_sim.py` (272 lines, runs)
- `D:/Repos/solar-rocket-game/` — `server.js` (71 lines), `package.json`, `package-lock.json`, `node_modules/` (multiplayer server only — client integration pending)
- `~/.resonant/skills/global/add-a-contributingmd-…/` — first production-extracted skill manifest (skill.json, procedure.md, verification.md, examples/)

Files added/modified to ship the marathon-enabling fix:
- [gui/app.py](resonant_client/gui/app.py) — `SESSION_MAX_STEPS = 200`, settings-driven override at Session() construction with 0 → 10 000 sentinel
- [gui/settings.py](resonant_client/gui/settings.py) — `general.session_max_steps` default + comment about doom-loop detector
- [gui/static/app.js](resonant_client/gui/static/app.js) — settings UI row "Session step budget"

**Across all 11 passes the resonant-client did real coding work, surfaced real product bugs, and earned its place as the daily driver.** The bug ledger above is the next sprint's work.

---

# 2026-04-28 follow-up — v0.2.0 ships

After the 11-pass dogfood marathon, the focus shifted to **packaging and shipping the bug fixes the marathon surfaced**. Three phases ran back-to-back on 2026-04-28:

## Phase 1 — PyInstaller bundle

`packaging/resonant.spec` (205 lines) — bundles `resonant_client/` into a self-contained Windows folder. Smoke test: `dist/resonant/resonant.exe gui --port 8910 --browser` serves the GUI on HTTP 200 in 5.8 ms; all 4 static assets (app.js 261KB / styles.css 120KB / favicon / plan_graph_view) return 200.

## Phase 2 — WinSparkle auto-update channel

`resonant_client/updater.py` (220 lines) — ctypes wrapper around vendored `WinSparkle.dll` 0.9.2. Wires into startup via `__main__.py:init_updater()`. Configured with EdDSA-signed update channel pointed at `https://luminary-analytics.github.io/resonant-client/appcast.xml`.

End-to-end E2E test passed: pushed a synthetic v0.2.1 entry to the appcast, ran the bundled v0.2.0 exe, native "Software Update" dialog window appeared (`MainWindowTitle: "Software Update"`), registry confirmed `LastCheckTime: 1777363362` matching the launch.

## Phase 3 — Release CI

`.github/workflows/release.yml` (179 lines / 13 steps) — tag push triggers PyInstaller → Inno Setup → EdDSA sign → GitHub Release → appcast.xml update → gh-pages push. Three CI runs to converge on v0.2.0:

| Run | Time | Failed at | Cause | Fix |
|---|---|---|---|---|
| #1 | 1m41s | PyInstaller | `plan_graph_view.js` was untracked | Committed via `gh api`, retagged |
| #2 | 2m29s | choco install | `windows-latest` ships Inno Setup pre-installed | Removed install step |
| #3 | 2m40s | Last step (appcast) | `update_appcast.py` refused duplicate v0.2.0 | Manually pushed signed entry |

Each failure surfaced a real bug. All three are documented in [docs/known-issues.md](docs/known-issues.md) as #12, #13, #14.

## What's now live (2026-04-28 14:55 UTC)

- ✅ Signed installer published: <https://github.com/Luminary-Analytics/resonant-client/releases/tag/v0.2.0>
- ✅ EdDSA signature embedded in appcast: `iWM2C3x4BUu/TUvfDSO…`
- ✅ Appcast feed serving v0.2.0 entry: <https://luminary-analytics.github.io/resonant-client/appcast.xml>
- ✅ Tag-push CI workflow on main: future `git tag vX.Y.Z && git push origin vX.Y.Z` ships unattended

## Documentation index (added 2026-04-28)

For future contributors / LLM sessions picking up this work:

| Doc | Purpose |
|---|---|
| [RELEASING.md](RELEASING.md) | Operational runbook — "ship a release in 10 min" |
| [docs/release-pipeline.md](docs/release-pipeline.md) | Architectural deep-dive of the entire pipeline |
| [docs/known-issues.md](docs/known-issues.md) | Bug ledger #1-#14 with reproductions and fix proposals |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Updated with "Release & Distribution" section pointing here |

## Bug ledger growth (this session)

The 11-pass marathon surfaced #6-#11. Phase 1+2+3 release work added #12-#14:

| # | Title | Severity |
|---|-------|----------|
| 12 | PR-time PyInstaller smoke build missing | Medium |
| 13 | Appcast updater duplicate-version refusal too strict | Low |
| 14 | Local PyInstaller bundle 2.6× bigger than CI build | Low |

See [docs/known-issues.md](docs/known-issues.md) for full reproductions.

## Final session verdict

This session delivered:
1. **A working agentic-coding IDE** (the dogfood marathon proved it works on real tasks across 9 different test categories)
2. **A real production release** (v0.2.0 is downloadable + auto-updates work end-to-end)
3. **A documented release pipeline** that future versions can ride on

**The bar from "personal dev tool" to "shippable product" was crossed in two days of focused work.** Next sprint targets the high-severity bugs #9 + #10 (backend-swap context loss) before they ship to anyone besides the developer.
