# PLAN — Codebase Intelligence

> **Foundation cluster (pre-v0.2.0).** Shipped state preserved here for reference. See [ROADMAP.md](ROADMAP.md) → "Post-refocus state (v0.3.x → v0.5.9)" for the capability tracks that built on this foundation.
>
> Status: ✅ Shipped · Tasks: 4 / 4 · Last verified: 2026-04-26

## Objective

Tighten the agent's edit-validate loop and clean up the awkward `bash("git ...")` pattern. Pre-cluster: the agent edited a file, then had to remember to run the linter, then had to parse the output. Git operations went through `bash` with all its quoting hazards. There was no persistent REPL, so every Python snippet paid a cold-start cost.

What this cluster delivered:

- Edits trigger automatic lint feedback as a synthetic user-turn input (opt-in via Settings)
- Same opt-in pattern for tests scoped to the edited file
- Five first-class git tools with structured output and clean UI rendering
- Six REPL tools (`repl_python_*` and `repl_node_*`) backed by long-lived subprocesses

## Context

Files a future executor (or anyone extending this cluster) must read first:

- [ARCHITECTURE.md](ARCHITECTURE.md) — `engine/session.py` agentic loop, `engine/tools.py` tool system, sandbox classification
- [resonant_client/engine/session.py](resonant_client/engine/session.py) — `Session.run()`, post-tool hook point where lint/test feedback is injected
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `AGENT_TOOLS`, `TOOL_ICONS`, `execute_tool()` dispatch
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `READ_ONLY_TOOLS`, `FILE_WRITE_TOOLS`, `SHELL_TOOLS` plus the new git/REPL classifications
- [resonant_client/engine/policies.py](resonant_client/engine/policies.py) — `ExecutionPolicy` for project-level overrides
- [resonant_client/engine/lint.py](resonant_client/engine/lint.py) — `detect_linter()`, `run_linter()`
- [resonant_client/engine/auto_test.py](resonant_client/engine/auto_test.py) — `find_test_target()`, `run_tests_for_edit()`
- [resonant_client/engine/git_tools.py](resonant_client/engine/git_tools.py) — `git_status`, `git_diff`, `git_commit`, `git_branch_create`, `git_log`
- [resonant_client/engine/repl.py](resonant_client/engine/repl.py) — `ReplProcess` (Python/Node), `MAX_CONCURRENT_REPLS`
- [resonant_client/gui/settings.py](resonant_client/gui/settings.py) — `general.auto_lint_after_edits`, `general.auto_test_after_edits`, `general.auto_test_command`

## Prior art (do NOT reinvent)

| Feature | Where it lives now |
|---|---|
| `bash` tool with timeout + cancel | [engine/tools.py](resonant_client/engine/tools.py) `_exec_bash` |
| `file_edit` returns structured before/after diff | [engine/tools.py](resonant_client/engine/tools.py) `_exec_file_edit` |
| `DiffReview` extracts changed file paths | [engine/diff_review.py](resonant_client/engine/diff_review.py) |
| Project policy via `resonant-policy.json` | [engine/policies.py](resonant_client/engine/policies.py) |
| Hooks system for pre/post-tool events | [engine/hooks.py](resonant_client/engine/hooks.py) |
| `git_*` tools (5 total) wrap subprocess with safety rails (no `--no-verify`, no `--amend`) | [engine/git_tools.py](resonant_client/engine/git_tools.py) |
| `repl_*` tools (6 total) — Python and Node, with eval-lock + sentinel framing | [engine/repl.py](resonant_client/engine/repl.py) |

**Note on hooks:** the existing `HookRunner` already supports `PostToolUse` events. We deliberately did NOT implement auto-lint and auto-test as hooks. Reasons:

- They're not user code; they're app behavior. Settings discovery > hidden JSON config.
- They need to inject results back into the conversation as a synthetic user turn — hooks today don't have that affordance.
- Easier to gate behind a single Settings toggle than to manage hook installation/uninstallation.

The mechanism still leverages internals where convenient (e.g., the post-tool callback wiring in `Session.run`).

## Tasks

All four tasks below are ✅ shipped. Each line points to the implementing files and a verify command that passes against the repo today.

---

### Task 4.1 — Auto-lint feedback loop ✅ Shipped

**Lives in:**
- [resonant_client/engine/lint.py](resonant_client/engine/lint.py) — `detect_linter(project_path)` (ruff > flake8 > eslint precedence), `run_linter(spec, file_path, timeout)`
- [resonant_client/engine/session.py](resonant_client/engine/session.py) — post-tool hook: after each successful `file_edit`/`file_write`, when `general.auto_lint_after_edits` is true and a linter is detected, runs the linter and appends a synthetic `{"role": "user", "content": "Linter (<name>) reported:\n<output>"}` to `conversation_history`
- [resonant_client/gui/settings.py](resonant_client/gui/settings.py) — `general.auto_lint_after_edits: bool` (default false)
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — Settings UI toggle in General; small label showing "Detected: ruff" (or "Detected: none") for the current project
- [tests/test_auto_lint.py](tests/test_auto_lint.py) — covers detection across ruff/flake8/eslint configs + injection round-trip
- [tests/test_session_auto_feedback.py](tests/test_session_auto_feedback.py) — end-to-end Session.run injection coverage

**Doom-loop guard (✅):** Tracks per-file content hash; same lint output for the same content hash is skipped, so the agent can't loop fixing-then-re-lint forever on a file the linter is wrong about.

**Verify:**
```bash
pytest tests/test_auto_lint.py tests/test_session_auto_feedback.py -k lint -v
```

Manual:
1. Enable the toggle in Settings.
2. Have the agent edit a Python file with an obvious style violation (e.g., `if x==1:`).
3. Confirm the next agent turn sees a "Linter reported:" user message and fixes the violation.

**Done when (✅):** Toggle works, linter detection covers ruff + flake8 + eslint, feedback round-trips into the conversation, and the doom-loop guard prevents infinite re-injection.

---

### Task 4.2 — Auto-test on edit (opt-in) ✅ Shipped

**Lives in:**
- [resonant_client/engine/auto_test.py](resonant_client/engine/auto_test.py) — `find_test_target(project_path, edited_file)` covers Python (`tests/test_<stem>.py` and mirror layouts) and JS/TS (`<stem>.test.ts`, `__tests__/<stem>.test.ts`), `run_tests_for_edit(project_path, edited_file, command, timeout)`
- [resonant_client/engine/session.py](resonant_client/engine/session.py) — same hook point as auto-lint
- [resonant_client/gui/settings.py](resonant_client/gui/settings.py) — `general.auto_test_after_edits: bool` (default false), `general.auto_test_command: str` (default `pytest -x`)
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — Settings UI: toggle + custom command input + "Detected target file: tests/test_bar.py" preview based on most-recently-edited file
- [tests/test_auto_test.py](tests/test_auto_test.py) — covers test-target discovery + injection round-trip
- [tests/test_session_auto_feedback.py](tests/test_session_auto_feedback.py) — end-to-end coverage

**Doom-loop guard (✅):** Same per-file content hash dedup as auto-lint. If the test command itself fails for environment reasons (e.g., pytest not installed), error message is injected once and the dedup hash persists so the agent doesn't retry forever.

**Verify:**
```bash
pytest tests/test_auto_test.py tests/test_session_auto_feedback.py -k test -v
```

Manual:
1. Enable toggle in Settings.
2. Have the agent edit a function in a way that breaks an existing test.
3. Confirm next turn sees the failure output and the agent fixes it.

**Done when (✅):** Test detection picks the right test for common Python/JS layouts. Failing tests round-trip into the conversation. Doom-loop guard works.

---

### Task 4.3 — First-class git tools ✅ Shipped

**Lives in:**
- [resonant_client/engine/git_tools.py](resonant_client/engine/git_tools.py) — five functions, each a thin subprocess wrapper with `shell=False` and per-call timeout; safety rails enforced (no `--no-verify`, no `--amend`, no force-push)
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `git_status`, `git_diff`, `git_commit`, `git_branch_create`, `git_log` registered (lines 743–817)
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `git_status` / `git_diff` / `git_log` in `READ_ONLY_TOOLS`; `git_commit` / `git_branch_create` require approval outside `bypass`
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — `TOOL_ICONS` mapping for git tools; structured rendering (file list for status, diff hunks for diff, commit info card for commit, table for log)
- [tests/test_git_tools.py](tests/test_git_tools.py) — 29 tests using `tmp_path` git repos (init → add → commit cycles, edge cases for empty repo, no-changes, paths filter)

**Verify:**
```bash
pytest tests/test_git_tools.py -v

# Confirm registration
python -c "from resonant_client.engine import tools; \
  names = {t['function']['name'] for t in tools.AGENT_TOOLS}; \
  assert {'git_status','git_diff','git_commit','git_branch_create','git_log'}.issubset(names); \
  print('git tools registered')"
```

Manual:
1. Have the agent run `git_status` — confirm structured output, no shell-quoting issues.
2. Have it create a branch + commit — confirm the commit shows up in `git log`.
3. Confirm `git_commit` always prompts for approval in `auto-edit` mode (not just `ask`).

**Done when (✅):** All 5 tools work, output is structured (parseable, not raw stdout), UI renders cards (not just `<pre>` walls), safety rails enforced.

---

### Task 4.4 — Persistent REPL tool ✅ Shipped

**Lives in:**
- [resonant_client/engine/repl.py](resonant_client/engine/repl.py) — `ReplProcess(lang="python"|"node", cwd)` with sentinel-framed stdin/stdout protocol; `MAX_CONCURRENT_REPLS=4`, `DEFAULT_EVAL_TIMEOUT=30.0`, per-eval lock to serialize calls on one REPL
- [resonant_client/engine/tools.py](resonant_client/engine/tools.py) — `repl_python_start`, `repl_python_eval`, `repl_python_stop`, `repl_node_start`, `repl_node_eval`, `repl_node_stop` registered (lines 963–1037)
- [resonant_client/engine/sandbox.py](resonant_client/engine/sandbox.py) — `repl_*_start` and `repl_*_stop` are state-changing; `repl_*_eval` is in shell-equivalent category (always-prompt outside `bypass`)
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — REPL outputs render as a code block with a sticky `[repl_python:abc123]` badge; "kill" X on each REPL badge in the terminal-bar area
- [tests/test_repl.py](tests/test_repl.py) — 22 tests: round-trip `x = 41; x + 1` → `x` (state persisted), timeout enforcement, concurrent-cap enforcement, kill-on-stop

**Lifecycle (✅):** `state.repls: dict[str, ReplProcess]` lives on `AppState`. On WebSocket disconnect or session end, all REPLs spawned by that session are killed (no orphan processes).

**Verify:**
```bash
pytest tests/test_repl.py -v

# Confirm registration
python -c "from resonant_client.engine import tools; \
  names = {t['function']['name'] for t in tools.AGENT_TOOLS}; \
  assert {'repl_python_start','repl_python_eval','repl_python_stop', \
          'repl_node_start','repl_node_eval','repl_node_stop'}.issubset(names); \
  print('repl tools registered')"
```

Manual: ask the agent *"use repl_python to compute fibonacci(20) incrementally"*. Confirm one REPL serves multiple eval calls (state visible across calls).

**Done when (✅):** REPL state persists across evals. Timeouts work. REPLs are killed on session end / disconnect. UI shows REPL provenance badge.

---

### Task 4.5 (optional follow-up) — Update the system prompt ✅ Shipped

**Lives in:**
- [resonant_client/engine/session.py](resonant_client/engine/session.py) `get_system_instructions()` — added rules nudging the model to prefer `git_status` / `git_diff` / `git_commit` / `git_branch_create` over `bash(git ...)`, and `repl_python_start` over repeated `bash(python -c ...)` for incremental work.

**Verify (manual):** ask the agent to "show me the git status and stage README.md". Observe it calls `git_status` then `git_commit(paths=['README.md'])` rather than two `bash` calls.

**Done when (✅):** System-prompt rules ship; manual test shows the model prefers structured tools over `bash` for git/REPL work.

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# Cluster suite (one shot)
python -m pytest tests/test_auto_lint.py tests/test_auto_test.py \
                  tests/test_session_auto_feedback.py \
                  tests/test_git_tools.py tests/test_repl.py -q

# Tool registry sanity check (all 11 cluster tools)
python -c "from resonant_client.engine import tools; \
  names = {t['function']['name'] for t in tools.AGENT_TOOLS}; \
  expected = {'git_status','git_diff','git_commit','git_branch_create','git_log', \
              'repl_python_start','repl_python_eval','repl_python_stop', \
              'repl_node_start','repl_node_eval','repl_node_stop'}; \
  missing = expected - names; \
  assert not missing, f'missing: {missing}'; \
  print('all 11 cluster tools registered')"
```

Manual end-to-end:
1. Enable auto-lint. Edit a Python file with `if x==1:` (no spaces around `==`). Confirm ruff complains and the agent fixes it.
2. Use `git_status` → `git_branch_create("feature/test")` → `git_commit("test commit")` → `git_log(limit=1)`. All structured output.
3. Start `repl_python_start`, `repl_python_eval` a few statements, confirm state persists across calls.

## Success criteria

- [x] Lint detection works for ruff + eslint (the two most common in this repo's typical projects); flake8 covered as a bonus.
- [x] Auto-lint and auto-test both have working doom-loop guards (per-file content-hash dedup).
- [x] All 5 git tools and 6 REPL tools registered, dispatched, and tested.
- [x] System prompt nudges the model toward structured tools where they exist.
- [x] No regression in `pytest`.

## Future / nice-to-haves (not yet built)

| Idea | Where it would go | Why it's not built yet |
|------|-------------------|-----------------------|
| `git_push` (with branch confirmation) and `git_pull --ff-only` | `engine/git_tools.py` + sandbox always-ask | Push affects shared state; we deferred until the safety prompt UX is more explicit |
| `git_blame(path, line)` returning the originating commit summary | `engine/git_tools.py` | Useful for "why was this written?" but the agent rarely asks |
| TypeScript `tsc --noEmit` as an auto-lint backend | `engine/lint.py` `detect_linter` | tsc is project-wide, not per-file → much slower than ruff/eslint, so the feedback-loop benefit is weaker |
| REPL sessions that survive WebSocket disconnect (re-attach by repl_id) | `engine/repl.py` lifecycle, `gui/app.py` cleanup | Nice safety net but adds state to manage; today's "kill on disconnect" is simpler and matches the bash-tool model |
| `pytest --collect-only` integration so the auto-test detector can show "no test for this file yet" hints | `engine/auto_test.py` | Detector heuristics cover the common cases; this would be polish |
| Coverage-aware test selection (only re-run tests that touched the edited line) | `engine/auto_test.py` + coverage.py integration | Big effort, marginal benefit at the agent's typical edit cadence |

## Output

When extending this cluster, append a status entry here:

> 2026-04-26 — All 4 tasks (+ optional Task 4.5) shipped. 12+12+29+22 tests pass across the four test files. No deviations from this plan. Auto-feedback hook implemented as a callback in `Session.run` rather than via `HookRunner` (intentional — see "Note on hooks" above).
