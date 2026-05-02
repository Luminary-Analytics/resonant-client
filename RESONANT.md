# Instructions

This is the Resonant Client project — an Ollama-native agentic coding desktop app, purpose-built for DeepSeek and other open-source local models.

**Positioning rule (since v0.4.0):**
- Want Anthropic? Reach for Claude Code.
- Want OpenAI? Reach for Codex.
- Want DeepSeek / Ollama? This is the tool.

# Conventions

- Use Python 3.11+ features (3.12+ where available)
- Follow the existing code style (type hints, docstrings for public functions)
- Keep modules focused and small
- Prefer event-driven architecture — engine yields `EngineEvent`s consumed by TUI/GUI
- The OllamaBackend implements the streaming interface (`TEXT_DELTA`, `TOOL_CALL`, `DONE`, `ERROR`)
- Tool execution lives in `engine/tools.py` (engine path) or is dispatched by `engine/session.py` for tool-side-effects like `await_user`; never add tool logic to TUI or GUI layers
- Comments matter. Capture **why**, especially when the answer involves a bug-discovery story (Bug #25, the cycle guards, the working_subdir story). Future LLMs reading the code rely on this.

# Architecture (v0.4.0)

```
resonant_client/
├── backends.py              # OllamaBackend ONLY (other backends cut in v0.4.0)
├── network_defaults.py      # resolve_ollama_url chain (env → settings → Mac Studio)
├── events.py                # EngineEvent / ClientCommand enums
├── protocol.py              # Tool prompt building, JSON/XML parsing
├── tui.py                   # Terminal UI (Rich + prompt-toolkit) — Ollama-only
├── engine/
│   ├── session.py           # Agentic loop, cycle guards, await_user dispatch
│   ├── tools.py             # AGENT_TOOLS list + execute_tool dispatch
│   ├── sandbox.py           # Permission levels, READ_ONLY/FILE_WRITE/EXEC tool sets
│   └── ...                  # browser, computer, diff_review, rag, hooks, mcp, memory
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

# Backend (single)

- **Ollama** — local LLMs with adaptive tool-calling (native or XML fallback). The canonical default URL is `http://10.0.0.133:11434` (Mac Studio in the user's infra). Override via `OLLAMA_HOST` env or `network.ollama_url` in `~/.resonant/settings.json`.

DeepSeek tier guidance (revised v0.5.1 — both tiers now usable for
autonomous missions; see `docs/v0.5.1-smoke-results.md`):

**For Mission flow (one-shot grill + plan-graph dispatch):**
- `deepseek-v4-flash:cloud` — recommended default. Fast grill interviews
  (5–15 questions), reliable JSON output for the planner specialist.
- `deepseek-v4-pro:cloud` — recommended when the spec is complex (multi-
  file refactor, large codebase context needed, integration work) OR
  when you want a thorough rigorous grill (10–25 questions per the
  autonomous flow). v0.5.1 added the auto-routing so pro uses
  `PLAN_DEEP` (research-first planner) instead of `PLAN`, which lets
  pro's deliberation work WITH the orchestrator instead of against it.

**For Autonomous Mission (∞ Run autonomously, v0.5.1+):**
- `deepseek-v4-flash:cloud` — **recommended for greenfield work.**
  Single-module specs, fresh modules, scaffolds. Snappy planner +
  decisive implementer. v0.5.1 GA smoke: 1 iter, 340s wall-clock,
  satisfied verdict.
- `deepseek-v4-pro:cloud` — **recommended for context-heavy work.**
  Multi-file refactors, integration tasks, anywhere the planner needs
  to read the existing codebase before decomposing. The autonomous
  daemon auto-routes pro to `PLAN_DEEP` (research-first) — no manual
  config required. v0.5.1 GA smoke: 1 iter, 135s wall-clock (FASTER
  than flash on this workload because the deeper planning phase
  produced a tighter implementer goal), satisfied verdict.

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
- **One backend.** v0.4.0 cut the multi-backend story to focus on the deepseek path. Anthropic / OpenAI users have purpose-built tools elsewhere.

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

If you're an LLM continuing this work after a context reset, the reading order is:

1. **`RESONANT.md`** (this file) — overall conventions
2. **`docs/long-running-agents-phase-2-implementation.md`** — current implementation status + ADRs + module map
3. **`docs/long-running-agents-phase-2.md`** — design doc the implementation is realizing
4. The tests: `tests/test_roadmap.py`, `tests/test_acceptance_check.py`, `tests/test_grill_me_rigorous.py`, `tests/test_reflect.py`, `tests/test_roguelite_integration.py`

If you're working on v0.5.0a5 specifically (the autonomous loop daemon), §6.1 + §7.3 of the implementation guide is your starting point.
