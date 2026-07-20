# Instructions

This is Resonant Client: a provider-adaptive agentic coding desktop app. Its
flagship mission is making open models served through Ollama excellent at
difficult, long-running coding work; Kimi and Codex adapters are also supported
through the same model-neutral runtime contract.

**Positioning rule:** Resonant owns the durable harness—context, tools,
workers, evidence, recovery, and user control. Provider adapters translate wire
formats and capabilities; they do not fork the product contract.

# Conventions

- Use Python 3.11+ features (3.12+ where available)
- Follow the existing code style (type hints, docstrings for public functions)
- Keep modules focused and small
- Prefer event-driven architecture — engine yields `EngineEvent`s consumed by TUI/GUI
- The OllamaBackend implements the streaming interface (`TEXT_DELTA`, `TOOL_CALL`, `DONE`, `ERROR`)
- Tool execution lives in `engine/tools.py` (engine path) or is dispatched by `engine/session.py` for tool-side-effects like `await_user`; never add tool logic to TUI or GUI layers
- Comments matter. Capture **why**, especially when the answer involves a bug-discovery story (Bug #25, the cycle guards, the working_subdir story). Future LLMs reading the code rely on this.

# Architecture (v0.10.0)

```
resonant_client/
├── backends.py              # Provider adapters and model capability discovery
├── network_defaults.py      # resolve_ollama_url chain (env → settings → Mac Studio)
├── events.py                # EngineEvent / ClientCommand enums
├── protocol.py              # Tool prompt building, JSON/XML parsing
├── tui.py                   # Terminal UI (Rich + prompt-toolkit)
├── engine/
│   ├── session.py           # Agentic loop, durable workers, checkpoints, lifecycle
│   ├── tools.py             # AGENT_TOOLS list + execute_tool dispatch
│   ├── sandbox.py           # Permission levels, READ_ONLY/FILE_WRITE/EXEC tool sets
│   └── ...                  # artifacts, agents, context, traces, hooks, MCP, worktrees
├── orchestration/           # plan-graph multi-specialist runner (v0.3.5)
│   ├── plan_graph.py        # PlanNode (with v0.3.5 working_subdir field)
│   ├── runner.py            # LocalSpecialistRunner + working_subdir propagation
│   ├── walker.py            # GraphWalker
│   └── specialists.py       # plan / implement / verify / repair / research / explore
└── gui/
    ├── app.py               # Starlette WS handler — command dispatch, events
    ├── runtime.py           # BackendSpec + build_session
    ├── sessions.py          # ProjectManager + safe-default project path (v0.3.3)
    ├── settings.py          # ~/.resonant/settings.json
    ├── diagnostics.py       # Help → Save Diagnostics ZIP (v0.3.4)
    ├── templates/index.html # Frameless desktop app shell
    └── static/{app.js,styles.css}
```

# Providers and flagship models

- **Ollama** — local/open models with adaptive tool-calling (native or text fallback).
- **Kimi** — Moonshot API adapter with the same harness contract.
- **Codex** — installed CLI adapter for users who explicitly choose it.

The default Ollama endpoint is `http://127.0.0.1:11434`. Override it with
`OLLAMA_HOST` or `network.ollama_url` in `~/.resonant/settings.json`.

Model tier guidance:

**Default (v0.6.5+):** `glm-5.2:cloud` — the flagship (756B, 1M
context, native tool calling). It's the out-of-the-box model on the
Mac Studio; the deepseek-v4 tiers below stay one click away in the
model dropdown (or via Settings → general → default_model) and sit on
a separate cloud quota, so they double as the 503 fallback.

**Secondary — DeepSeek tiers** (the v0.5.2–v0.6.4 default; see
`docs/v0.5.1-smoke-results.md` for the data behind the pro/flash
split). PLAN_DEEP is the unconditional planner for autonomous
missions (v0.5.4a1; was per-tier-routed via PLANNER_BY_TIER in
v0.5.1–v0.5.3).

**For Mission flow (one-shot grill + plan-graph dispatch):**
- `deepseek-v4-pro:cloud` — **recommended DeepSeek tier.** Thorough grill
  (10-25 question rigorous mode for autonomous; 5-15 for standard),
  PLAN_DEEP planner reads codebase context before decomposing.
- `deepseek-v4-flash:cloud` — fall back to flash for very simple
  specs (1-2 criteria, single function) where pro's deliberation
  is overhead. Also useful when iterating on a spec / running
  multiple short missions back-to-back.

**For Autonomous Mission (∞ Run autonomously, v0.5.1+):**
- `deepseek-v4-pro:cloud` — **recommended DeepSeek tier.** v0.5.1 GA smoke:
  1 iter, 135s wall-clock (FASTER than flash on the wordcount spec
  because PLAN_DEEP produced a tighter implementer goal). Especially
  good for context-heavy work (multi-file refactors, integrations).
- `deepseek-v4-flash:cloud` — fall back for greenfield work where
  there's no codebase to read. v0.5.1 GA smoke: 1 iter, 340s.

**Walker auto-retry (v0.5.1+):** when ANY planner returns
unparseable output (no JSON envelope), the walker spawns a retry
sibling with a JSON-format reminder. Capped at 1 retry by default.
This catches edge cases for any model; you don't need to tune it
per tier.

# Mission flow (v0.3.x architecture)

1. **Mission toggle** in chat header opens the composer.
2. **Composer** has an explicit project-path field (v0.3.3 — Bug #25 fix) so the agent always writes where the user expects.
3. **Grill phase** (`orchestration/grill_me.py`) — interviewer asks one question at a time with recommendations.
4. **Spec emission** — model emits a structured `## Final spec` block; frontend renders "Build this roadmap" CTA.
5. **Dispatch** — full spec hands off to `intent_service.start_intent`; `LocalSpecialistRunner` walks the plan-graph.
6. **Specialist runs** with:
   - **Cycle guards** (v0.3.3): windowed signature dedup + read-only churn cap
   - **`working_subdir` propagation** (v0.3.5): siblings inherit the parent's scaffold subdir
   - **`await_user` tool** (v0.3.5): escape hatch for stuck agents
7. **Diagnostics ZIP** (v0.3.4): Help menu bundles redacted logs + intent audits + settings into a ZIP under `~/Downloads`.

# GUI event flow

1. User sends message via WebSocket
2. `app.py` spawns `_engine_thread()` running `session.run()` with callbacks (`on_permission`, `on_choice`, `on_user_input`)
3. Engine yields events → thread-safe queue → async WebSocket handler
4. Frontend `app.js` handles each event type (`tool.call`, `tool.result`, `text.delta`, `await_user`, `mission.spec_ready`, `plan.snapshot`, `plan.event`, `diagnostics_saved`)
5. Terminal bar tracks active `bash` tool calls by `call_id`

# Key invariants

- **Project path is never silently `os.getcwd()` when cwd is a system / install dir.** Bug #25 — see `gui/sessions._safe_default_project_path` and `_is_unsafe_cwd`.
- **`working_subdir` only refines, never regresses.** A child can sharpen `web/` to `web/api/`, but never broaden `web/api/` back to `web/`.
- **`await_user` is universally available** to all specialists (implement / explore / verify / repair / research / plan). The escape hatch must be available everywhere.
- **Cycle guards trip BEFORE the step cap.** The 24/50/etc. step caps still exist as a final safety net but the windowed signature guard catches loops at 3 occurrences in 12 calls.
- **Diagnostics ZIP redaction is overzealous on purpose.** False positives produce a slightly less informative log line; false negatives leak credentials.
- **Routing is explicit.** Model changes happen only at visible phase/worker
  boundaries through configured roles; never silently during a model turn.
- **Durable state is external to the repo.** Agent records, artifacts,
  checkpoints, traces, and managed worktrees live under `~/.resonant/projects`.
- **Writer isolation is conservative.** Parallel writers use worktrees and
  never stash, reset, or merge into a dirty user checkout.

# Documentation map for future humans / LLMs

- **`README.md`** — the user-facing positioning + install + usage
- **`RESONANT.md`** — this file: architecture + invariants for agents working in the codebase
- **`ARCHITECTURE.md`** — module-by-module deep dive
- **`docs/long-running-agents.md`** — Mission flow design doc (Phase 1)
- **`docs/long-running-agents-phase-1-review.md`** — postmortem on the v0.3.x mission iterations
- **`docs/long-running-agents-phase-2.md`** — Autonomous Mission design doc (Phase 2 / v0.5.0). The WHY and WHAT.
- **`docs/long-running-agents-phase-2-implementation.md`** — Autonomous Mission implementation guide. The HOW, STATUS, and DECISIONS made during a1–a4. **Start here if you're picking up v0.5.0 work after a context reset.**
- **`docs/mission-cross-model-test-plan.md`** — Chrome MCP E2E test plans + per-model verdicts (now historical — v0.4.0 collapsed to single model family)
- **`docs/v0.4.0-cut.md`** — what was removed and why (read this if you wonder why a backend module is missing)
- **`docs/v0.4.x-deepseek-harness-roadmap.md`** — backlog of deepseek-specific optimizations (Tier 1 + Tier 2 done; Tier 3 deferred to v0.5.x)
- **`docs/modern-agent-runtime.md`** — durable workers, checkpoints, hooks,
  traces, context broker, model roles, capability packs, and artifact bus

If you're an LLM continuing this work after a context reset, the reading order is:

1. **`RESONANT.md`** (this file) — overall conventions
2. **`docs/long-running-agents-phase-2-implementation.md`** — current implementation status + ADRs + module map
3. **`docs/long-running-agents-phase-2.md`** — design doc the implementation is realizing
4. The tests: `tests/test_roadmap.py`, `tests/test_acceptance_check.py`, `tests/test_grill_me_rigorous.py`, `tests/test_reflect.py`, `tests/test_roguelite_integration.py`

If you're working on v0.5.0a5 specifically (the autonomous loop daemon), §6.1 + §7.3 of the implementation guide is your starting point.
