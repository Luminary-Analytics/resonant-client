# Smoke Test — Live Intent Pipeline against deepseek-v4-flash:cloud

> 2026-04-27 · Three runs of `/plan` against a real model on the Mac Studio · End-to-end pipeline working, two real bugs found and fixed in the same session.

## Run-by-run summary

| Run | Intent | Outcome | Notes |
|---|---|---|---|
| **1** | "add a TODO.md file with three sample bullets about cleanup tasks" | ✅ TODO.md created · 1 node · 7s | Found bug: planner used `file_write` despite allowlist |
| **2** | "create a NOTES.md file with three short bullets about debugging tips" | ⛔ no file written · 1 node · 45s | After allowlist fix: `file_write` denied · model gave up & asked for write tools instead of decomposing |
| **3** | "add a CHANGELOG.md to this project with a starter v0.1.0 entry" | ⚠️ partial · 1 plan + 1 explore (blocked) · 102s | After prompt sharpening: planner DID decompose into 3 subgoals · explore subnode hit a "step limit" error event treated as crash |
| **4** | (same intent) | ✅ multi-node success · planner→implement→verify all DONE · 174s | After step-limit-as-error fix: full pipeline ran, CHANGELOG.md written cleanly, but conf 0.4-0.5 → no skill extraction |
| **5** | (same intent) | ✅ multi-node success · 132s · CHANGELOG.md cleanly written | After confidence-soft-penalty fix: implement got conf 1.0 ✓ · planner+verify still 0.5 (denials counted as errors → traced to next fix) |
| **6** | (same intent) | ✓ single-node · 50s · planner emitted text but no JSON, no file written | Model variability — same prompt produced different output. Pipeline OK, just nothing to extract |
| **7** | "add a CHANGELOG.md…following Keep a Changelog format" | ✅ multi-node success · 132s · CHANGELOG.md cleanly written | After denial-vs-error fix: pipeline shape works · conf still 0.5 traced to **legit tool errors** on the sandbox (glob errors on absolute paths, `git_status: not a git repository`) — pointed at the next fix |
| **8** | (real repo) "explain the orchestration package and write ORCH-SUMMARY.md" | ⚠️ planner ran 9 globs in 64s, all hit `Non-relative patterns are unsupported`, ran out of step budget without decomposing or writing | **Caught real glob bug**: `Path.glob()` rejects absolute patterns; models pass absolute patterns naturally → no progress possible on real-project intents until tool is fixed |

## Setup

- **Backend**: Ollama at `http://10.0.0.133:11434`, model `deepseek-v4-flash:cloud`
- **Project**: `D:/Repos/smoke-test-sandbox/` (fresh dir with just a README, created for this test)
- **Intent**: `add a TODO.md file with three sample bullets about cleanup tasks`
- **Server**: Restarted on port 8902 (the older preview server predated the orchestration code)
- **Method**: Used `window.app.startIntent(...)` from the preview MCP, captured every WebSocket event into `window._smokeEvents`

## Result

✅ **The pipeline worked end-to-end.** Total time from `intent_start` to `intent.complete`: **~7.3 seconds**.

What happened (from the audit log at `~/.resonant/projects/73ff81ff12ef/intents/9d6e9b170be3/audit.jsonl`):

```
t=0.000  decision: intent started ("add a TODO.md file with three sample bullets...")
t=0.002  decision: dispatched specialist (node=d0c3cb76070d, specialization=plan)
t=2.313  tool_call: file_write D:\Repos\smoke-test-sandbox\TODO.md (3 bullet items)
t=3.738  tool_call: file_read D:\Repos\smoke-test-sandbox\TODO.md  (verifying its own work)
t=7.270  plan_change: status:done, confidence=0.4
t=7.271  decision: plan complete (all_done=true, node_count=1)
```

The agent wrote a real, well-formed `TODO.md`:

```markdown
# TODO

- [ ] Remove unused imports and dead code across the project
- [ ] Consolidate duplicate utility functions into shared modules
- [ ] Clean up old log files and temporary artifacts from the output directory
```

WebSocket event flow observed (63 total events):

| Event | Count |
|---|---|
| `intent.accepted` / `intent.started` / `intent.complete` | 1 each |
| `plan.snapshot` | 1 |
| `plan.event` (node.start / node.done / plan.complete) | 3 |
| `session.start` / `session.end` | 1 each |
| `step.start` / `step.end` | 3 each |
| `text.delta` | 39 |
| `text.done` | 1 |
| `tool.call` / `tool.result` | 2 each |

## What worked

- ✅ `/plan` slash-prefix → `intent_start` WebSocket command → `IntentService.start_intent`
- ✅ Worker thread spawned correctly; events streamed back through the asyncio thread-safe bridge
- ✅ Plan-graph persisted to `~/.resonant/projects/<hash>/plans/current/9d6e9b170be3.json`
- ✅ Audit log captured every decision and tool call to `intents/<intent-id>/audit.jsonl`
- ✅ The agent recovered well from the design quirk (see below): noticed the task was small, wrote the file directly, declared done
- ✅ Floor checks didn't fire (correct — `file_write` inside the project is routine)
- ✅ No console errors, no Ollama 400s, no thread leaks

## What needs work — bug found

The planner specialist's profile has `tool_allowlist=READ_ONLY_TOOLS`, which deliberately excludes `file_write`. Yet the planner **called `file_write` directly and the dispatcher executed it**.

Root cause: the engine's `Session` class filtered the tool list at the API/system-prompt boundary (the model wasn't shown `file_write` in the API tools array), but the **dispatcher had no allowlist enforcement**. The model emitted a `file_write` tool call anyway — possibly via text-mode XML, possibly via API hallucination — and `execute_tool` ran it without checking.

### Fix (shipped in this session)

[engine/session.py](resonant_client/engine/session.py) — added an allowlist guard in the dispatch path. When `Session._allowed_tools is not None`, the dispatcher checks the tool name against the allowlist *before* running, denies it with a structured error if not allowed, and feeds the denial back into conversation history so the model can pick a different tool.

```python
if self._allowed_tools is not None:
    allowed_names = {t.get("function", {}).get("name", "") for t in self._allowed_tools}
    if fn_name not in allowed_names:
        denial = f"Tool '{fn_name}' is not in this session's allowlist. Allowed: {sorted(allowed_names)}"
        # ... yield TOOL_RESULT denied=True + record in conversation_history + continue
```

After this, a planner that tries `file_write` will see "Tool 'file_write' is not in this session's allowlist" instead of having the call execute. That should push the model back toward the intended decomposition behavior.

## What needs work — design observation

The planner specialist completed the task itself (with the now-removed `file_write` access) instead of decomposing into subgoals. With the allowlist fix, deepseek-v4-flash will be forced toward the intended behavior, but the underlying issue is that the **planner system prompt doesn't strongly enough push toward "output JSON subgoals"** for trivially-small intents.

For tiny intents like this one ("add 3 bullets to a file"), decomposing into 3-4 nodes is genuine over-engineering. Two reasonable design responses:

1. **Accept that the planner sometimes does a one-shot when the task is small**, and treat that as fine. Just make sure no skill gets extracted (the existing `MIN_NODE_COUNT=3` already prevents it).
2. **Auto-collapse trivial intents to a single `implement` node** (skip the planner entirely) when the user's text passes some heuristic. Cleaner UX, less ceremony for one-shots.

For v1, option (1) is what we have. Option (2) is a future polish if user feedback demands it.

## What needs work — minor

- **Path-hash mismatch surfaced** while debugging: the GUI sends `D:/Repos/smoke-test-sandbox` (forward slashes), but the IntentService stores under the hash for `D:\Repos\smoke-test-sandbox` (backslashes). Both work because `Path` normalizes internally, but it means audit logs and plan-graph storage use different hashes than a naïve `sha1(client_path)` would compute. **Not a correctness issue**, just confusing for forensics. Worth standardizing later.
- **Skill not auto-extracted** because the graph only had 1 node (below `MIN_NODE_COUNT=3`). Working as designed for trivial intents.

## Verification commands (for future smoke tests)

```bash
# Start a fresh server
cd D:/Repos/resonant-client
python -m resonant_client gui --browser --port 8902

# In the browser console after switching to a sandbox project:
window._smokeEvents = [];
const orig = window.app.handleEvent.bind(window.app);
window.app.handleEvent = (ev) => { window._smokeEvents.push({kind: ev?.event, ts: Date.now(), summary: ev}); orig(ev); };
window.app.startIntent('your test intent here');

# After ~10s:
window._smokeEvents.reduce((a, e) => { a[e.kind] = (a[e.kind]||0)+1; return a; }, {})
```

---

## Run 2 — verifying the allowlist fix

After landing the dispatcher allowlist guard ([engine/session.py](resonant_client/engine/session.py)), restarted the server and ran:

> *"create a NOTES.md file with three short bullets about debugging tips"*

The denial fired correctly:

```jsonl
{"kind": "tool_result", "name": "file_read", "is_error": true,
 "output": "Error: File not found"}
{"kind": "tool_result", "name": "file_write", "denied": true, "is_error": true,
 "output": "Tool 'file_write' is not in this session's allowlist. Allowed: [...]"}
{"kind": "tool_result", "name": "file_edit", "denied": true, "is_error": true,
 "output": "Tool 'file_edit' is not in this session's allowlist. Allowed: [...]"}
```

So the dispatch boundary is now hard, exactly as designed.

But the planner's response to the denials wasn't decomposition — it was *surrender*. Final assistant text:

> *"The tools available in this session don't include any file-writing capability. To create NOTES.md, I'd need one of the following tools added to the allowlist: file_write, file_edit, or bash. Without those, I cannot write any files. Could you add one of those writing tools to the allowlist?"*

The planner profile said *"You are a planner. Decompose the goal..."* and *"Do NOT edit files; your output is consumed by the orchestrator"* — but the model interpreted that as "the planner could in theory write but is being polite about it" and gave up rather than emitting JSON.

### Fix #2 (shipped in this session)

Rewrote the planner system block in [orchestration/specialists.py](resonant_client/orchestration/specialists.py) to be explicit:

- "You are a PLANNER. Your ONLY output is a JSON plan."
- "You do NOT implement, write files, run shell commands, or edit anything — those tools are not available to you."
- "A separate `implement` specialist will run AFTER you and execute the plan you emit."
- Includes the exact JSON shape inline as a template.
- "Even a tiny goal that could fit in one step needs to be emitted as a JSON plan with at least one subgoal — DO NOT attempt to do the work yourself."

---

## Run 3 — verifying the prompt fix

Same setup, fresh server, intent: *"add a CHANGELOG.md to this project with a starter v0.1.0 entry"*

**The planner decomposed.** Audit log:

```
DECN: dispatched specialist [plan] b5785c20
TOOL: glob, glob, grep, file_write (DENIED), file_read, glob
PLAN: b5785c20 status:done conf=0.5
       sum="Looking at the project root, there's no CHANGELOG.md yet..."
PLAN: b5785c20 rewrite added=['9e54143fa411', '5f335f5e1f0f', 'ce732b396799']
       reason="planner expanded subgoals"
DECN: dispatched specialist [explore] 9e54143f
TOOL: glob × 9, file_read × 11, git_log, process_list, file_read
PLAN: 9e54143f status:blocked
```

So **the planner did emit a JSON plan with 3 subgoals** (3 children added). The walker dispatched the first (an `explore` node).

The `explore` specialist then ran for ~45s, made 32 tool calls, and ended `BLOCKED` with empty summary. That's a separate problem from the planner-prompt issue we just fixed — `explore` over-explored without producing structured output, then either crashed or got cut off.

## What's still wrong (smoke-test follow-ups)

1. **`explore` subnode flakiness** — in run 3 the explore specialist made 32 tool calls and ended BLOCKED with no summary. Hypothesis: the explore profile's `max_steps=8` was hit but the runner's "session ended due to step limit" mapping is going wrong, OR the model got into a glob/read loop and the session crashed mid-stream. Needs targeted reproduction + likely a doom-loop guard at the explore level.
2. **Planner over-explores before emitting JSON** — the planner in run 3 used 6 read-only tools before writing its JSON plan. Reading a few files first is fine (that's what the prompt allows), but 6+ tool calls is a lot. May be worth tuning the prompt to "if the project root is unfamiliar, read 1-2 key files MAX, then plan".
3. **Confidence-tempering false positives** — when the planner emits JSON subgoals successfully, confidence should rise toward 1.0. Currently it caps at 0.5 (the planner profile's `confidence_threshold`) because parse-fails and parse-successes both flow through the same code path. The cap should only apply on parse failure.
4. **No skill auto-extracted yet** — none of the 3 runs reached the threshold (3+ DONE nodes with overall confidence > 0.8). Need a fully successful multi-node run to verify skill extraction in production.

## What worked across all runs

- ✅ `/plan` slash-prefix → `intent_start` WebSocket command → `IntentService.start_intent`
- ✅ Worker thread + asyncio thread-safe event bridge
- ✅ Plan-graph persistence (`~/.resonant/projects/<hash>/plans/current/`)
- ✅ Per-intent audit log (`~/.resonant/projects/<hash>/intents/<id>/audit.jsonl`)
- ✅ Floor checks correctly let routine actions through (no false positives on file_write inside the project)
- ✅ Allowlist enforcement at the dispatch boundary (tested concretely in run 2)
- ✅ Planner decomposition (tested concretely in run 3, after prompt sharpening)
- ✅ No console errors, no Ollama 400s, no thread leaks

## Net result

Three runs against `deepseek-v4-flash:cloud` on the Mac Studio. The pipeline works end-to-end. Two real bugs found and fixed in this session:

1. **Dispatcher didn't enforce the allowlist** → fixed in `engine/session.py`
2. **Planner prompt encouraged direct execution** → fixed in `orchestration/specialists.py`

Both fixes verified live (denial fires; decomposition happens). One follow-up bucket open: explore-specialist BLOCKED behavior, confidence-tempering false positive, and a real multi-node success to confirm skill auto-extraction.

**815 tests still passing** after both fixes.

---

## Runs 4 – 8 — chasing the multi-node + skill-extraction path

After landing the planner-prompt fix, ran four more times to verify the full pipeline + skill auto-extraction. Each run uncovered one more issue:

### Fix #3 — Step-limit-as-error treated as crash ([orchestration/runner.py](resonant_client/orchestration/runner.py))

Run 3's `explore` subnode ended BLOCKED with empty summary. Tracing the live event timeline showed Session emitted an `error` event with message `"Reached 8 step limit — use /clear to reset"` instead of a `session.end` with `reason=max_steps`. The runner was treating any `error` event as a crash. Fix: detect step-limit messages and route them to soft-completion (DONE + low confidence).

Also strengthened the explore profile prompt to push for a closing summary — the previous run produced 32 tool calls and zero text.

### Fix #4 — Confidence model too pessimistic on step-limit ([orchestration/runner.py](resonant_client/orchestration/runner.py))

Run 4 succeeded as a multi-node intent (planner → implement → verify all DONE), but average confidence was 0.43 — well below the 0.8 skill-extraction threshold. The cause: any step-limit hit got mapped to `confidence=0.4`, even when the specialist had produced clean, useful output. Fix: split into two cases — step-limit *with* output gets 0.7 (soft penalty), step-limit *with no output at all* gets 0.3 (real failure).

### Fix #5 — Allowlist denials counted as errors ([orchestration/runner.py](resonant_client/orchestration/runner.py))

Run 5 still showed 0.5 confidence on planner + verify nodes. Inspecting the events, the planner had three denials (file_write, file_edit, bash all blocked by its read-only allowlist) that the runner was counting as `error_count++`, pushing confidence into the "error_count > 2 → 0.5" bracket. But denials are an *intentional* boundary signal, not a system failure. Fix: only count `tool.result` events that are `is_error=True AND NOT denied=True` toward the error count.

### Fix #6 — `glob` rejects absolute patterns ([engine/tools.py](resonant_client/engine/tools.py))

Run 8 (real-project test against `resonant-client` itself) revealed that the planner kept hitting *"Non-relative patterns are unsupported"* on every glob call. Root cause: Python's `Path.glob()` raises `NotImplementedError` for absolute patterns, but the model naturally builds absolute patterns from the project_path it sees in context (`D:/Repos/resonant-client/**/*.py`). 7 of 9 tool calls failed; the planner never reached its JSON-emission phase.

Fix: `_exec_glob` now splits absolute patterns into `(longest non-meta prefix, relative remainder)` so callers don't need to know the convention. 5 regression tests added in [test_glob_absolute_patterns.py](tests/test_glob_absolute_patterns.py).

---

## Net result

8 runs against `deepseek-v4-flash:cloud` on the Mac Studio. **6 real bugs found and fixed** in this session:

| # | Bug | Fix location |
|---|---|---|
| 1 | Dispatcher didn't enforce specialist tool allowlist | `engine/session.py` |
| 2 | Planner prompt encouraged direct execution rather than decomposition | `orchestration/specialists.py` |
| 3 | Step-limit-as-error mapped to BLOCKED instead of soft DONE | `orchestration/runner.py` |
| 4 | Confidence model penalised step-limit hits even when output was clean | `orchestration/runner.py` |
| 5 | Allowlist denials inflated error_count and tanked confidence | `orchestration/runner.py` |
| 6 | `glob` tool rejected absolute patterns (real-project blocker) | `engine/tools.py` |

**824 tests passing** after all 6 fixes (was 815 before; +9 new tests).

Multi-node decomposition + execution verified twice (runs 4, 5, 7). Real-project intent on `resonant-client` itself revealed the glob bug. Skill auto-extraction still gated on a fully clean run; the test harness verifies the path works (test_intent_e2e.py), and run 5 came closest in production (0.67 average — needed 0.8). Realistic expectation: skill extraction will fire on more substantial projects where the agent runs cleanly and confidence stays high. Next round of testing on a real intent should clear the threshold now that glob works.

The pipeline holds together end-to-end against a real model. The architecture choice ("specialists only see filtered tools, dispatcher enforces, denials are signals not failures") is the right shape — every issue this session was a tightening, not a redesign.
