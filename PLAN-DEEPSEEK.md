# PLAN — deepseek-v4-flash specific

> **Foundation cluster (pre-v0.2.0).** Shipped state preserved here for reference. See [ROADMAP.md](ROADMAP.md) → "Post-refocus state (v0.3.x → v0.5.9)" for the capability tracks that built on this foundation.
>
> Status: ✅ Shipped · Tasks: 3 / 3 · Last verified: 2026-04-26

## Objective

Surface the model's specific capabilities so the user can drive them. `deepseek-v4-flash:cloud` (the default model on Ollama at `http://10.0.0.133:11434`) has three thinking modes (low/med/high) and a 1M-token context — pre-cluster, the user had no in-app control over either. This cluster delivered:

- A per-session thinking-mode toggle (UI dropdown + backend wiring + warning about model reload)
- A "big-context profile" Settings preset that bumps `num_ctx` to 131072 and `num_batch` to 2048
- An on-demand `get_runtime_telemetry()` method on `OllamaBackend` that hits `/api/ps` and surfaces context-length / memory / thinking-mode / MoE expert info if available, plus a WebSocket `get_model_telemetry` command and a `model_telemetry` event the badge tooltip consumes

## Context

Files a future executor (or anyone extending this cluster) must read first:

- [resonant_client/backends.py:164](resonant_client/backends.py:164) — `OllamaBackend.__init__` (accepts `thinking="low"|"med"|"high"|None`)
- [resonant_client/backends.py:204](resonant_client/backends.py:204) — `OllamaBackend.get_runtime_telemetry()`
- [resonant_client/gui/sessions.py](resonant_client/gui/sessions.py) — `SessionRecord.thinking_mode` field
- [resonant_client/gui/runtime.py:36](resonant_client/gui/runtime.py:36) — `BackendSpec.thinking_mode`
- [resonant_client/gui/app.py:170](resonant_client/gui/app.py:170) — `_apply_big_context_profile()` env-var override at startup and on settings change
- [resonant_client/gui/app.py:5917](resonant_client/gui/app.py:5917) — WebSocket handler `get_model_telemetry`
- [resonant_client/gui/static/app.js:186](resonant_client/gui/static/app.js:186) — `thinkingModeSelector` element binding; change handler at line 366; `_updateThinkingModeVisibility` at line 1942; `setThinkingMode` sync at line 2053
- [resonant_client/gui/templates/index.html:306](resonant_client/gui/templates/index.html:306) — `<select id="thinking-mode-selector">` (hidden until model name starts with `deepseek-v`)
- [Ollama API docs](https://github.com/ollama/ollama/blob/main/docs/api.md) — `/api/chat` options, `/api/show`, `/api/ps`

## Prior art (do NOT reinvent)

| Feature | Where it lives now |
|---|---|
| `OllamaBackend._ollama_options` env-driven (num_ctx, num_batch, num_gpu) | [backends.py:172](resonant_client/backends.py:172) |
| `RESONANT_OLLAMA_NUM_CTX=32768` default | [backends.py:180](resonant_client/backends.py:180) |
| `keep_alive=120m` for warm-loaded models | [backends.py:197](resonant_client/backends.py:197) |
| `deepseek-v4-flash:cloud` first in `CLOUD_MODELS` | [backends.py](resonant_client/backends.py) |
| Harness state badge in header (consumes `model_telemetry`) | [app.js](resonant_client/gui/static/app.js) `updateHarnessBadge` |
| Per-session `thinking_mode` round-trips through `SessionRecord` and `BackendSpec` | [sessions.py](resonant_client/gui/sessions.py), [runtime.py:36](resonant_client/gui/runtime.py:36) |

## Important constraint (READ FIRST when extending)

The `OllamaBackend` design rule from [ARCHITECTURE.md](ARCHITECTURE.md):

> All requests to a given `OllamaBackend` instance must use **identical** `_ollama_options`. If any option differs between requests, Ollama unloads and reloads the entire model (30-120s penalty, much worse for 284B MoE).

This means **any per-session option change requires a new backend instance**, not just mutating `_ollama_options` mid-flight. The existing `set_thinking_mode` handler enforces this by rebuilding the backend through `BackendSpec.create_backend()` and swapping `state.session`. Future tasks that toggle Ollama options must follow the same pattern (or pay the reload cost knowingly).

## Tasks

All three tasks below are ✅ shipped. Each line points to the implementing files and a verify command that passes against the repo today.

---

### Task 3.1 — Thinking-mode toggle (per-session) ✅ Shipped

**Lives in:**
- [resonant_client/backends.py:164](resonant_client/backends.py:164) — `OllamaBackend(__init__, thinking=None)` normalizes `low`/`med`/`high` and adds `{"think": value}` to `_ollama_options`
- [resonant_client/backends.py:217](resonant_client/backends.py:217) — `get_runtime_telemetry` exposes `supports_thinking` / `active_thinking`
- [resonant_client/gui/sessions.py](resonant_client/gui/sessions.py) — `SessionRecord.thinking_mode: str = ""` round-trips through `to_dict`/`from_dict`/`to_summary`
- [resonant_client/gui/runtime.py:36](resonant_client/gui/runtime.py:36) — `BackendSpec.thinking_mode` field; passed to `create_backend("ollama", thinking=...)` at line 95
- [resonant_client/gui/app.py](resonant_client/gui/app.py) — WebSocket `set_thinking_mode` handler that persists, rebuilds backend, swaps session, sends back the new init payload
- [resonant_client/gui/templates/index.html:306](resonant_client/gui/templates/index.html:306) — `<select id="thinking-mode-selector">` next to `#model-selector`
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — selector visible only when model starts with `deepseek-v`; change handler shows confirm dialog warning about ~30–90s model reload

**Verify:**
```bash
pytest tests/test_deepseek_specific.py -k thinking -v

# Verify the thinking key lands in Ollama options
python -c "from resonant_client.backends import OllamaBackend; \
  b = OllamaBackend('http://10.0.0.133:11434', 'deepseek-v4-flash:cloud', thinking='med'); \
  print(b._ollama_options)"
# Expected: dict that includes 'think': 'med'

# Manual: pick deepseek-v4-flash, change thinking to "high" via the dropdown,
# confirm reload prompt appears, send "what is 7+7", verify response is detailed.
```

**Done when (✅):** Selector appears for deepseek-v* models, persists per session, and the chosen mode is sent to Ollama (verify with `curl http://10.0.0.133:11434/api/ps` showing the loaded model's options).

---

### Task 3.2 — 1M-context Settings preset ✅ Shipped

**Lives in:**
- [resonant_client/gui/app.py:170](resonant_client/gui/app.py:170) — `_apply_big_context_profile()` reads `general.big_context_profile`; if true and the user hasn't already overridden via env var, sets `RESONANT_OLLAMA_NUM_CTX=131072` and `RESONANT_OLLAMA_NUM_BATCH=2048` **before** any `OllamaBackend.__init__` runs
- [resonant_client/gui/app.py](resonant_client/gui/app.py) — `update_settings` re-applies the profile and pushes a "reload backend to take effect" notice
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — Settings UI toggle in the General section with helper text "Bumps Ollama context to 131k tokens and batch to 2048. Best for large-repo sessions. Requires model reload."
- [resonant_client/gui/settings.py](resonant_client/gui/settings.py) — no schema change; `general.big_context_profile` lives as a free-form key

**Defensive behavior (✅):** If the user has already set `RESONANT_OLLAMA_NUM_CTX` via env (process-start value != 32768), the toggle does **not** silently override; the Settings UI shows "Env-var override active; settings preset disabled."

**Verify:**
```bash
pytest tests/test_deepseek_specific.py -k big_context -v

# Confirm env override path still works
RESONANT_OLLAMA_NUM_CTX=99 python -c "from resonant_client.backends import OllamaBackend; \
  print(OllamaBackend('http://x','m')._ollama_options['num_ctx'])"
# Expected: 99

# Manual end-to-end: toggle on → "Reload backend" → wait ~30-90s
# Then check `curl http://10.0.0.133:11434/api/ps` shows context_length: 131072
```

**Done when (✅):** Toggle persists, applies overrides correctly when no user env override is present, refuses to silently override when one is, and explicit reload bumps the running model's context size.

---

### Task 3.3 — MoE expert-utilization telemetry ✅ Shipped (best-effort)

**Lives in:**
- [resonant_client/backends.py:204](resonant_client/backends.py:204) — `OllamaBackend.get_runtime_telemetry(timeout=5.0)`: hits `/api/ps`, parses `loaded_model`, `context_length`, `memory_mb`, `supports_thinking`, `active_thinking`, plus any MoE fields present in `/api/show` `parameters` (best-effort — Ollama's exposure varies per build)
- [resonant_client/gui/app.py:5917](resonant_client/gui/app.py:5917) — WebSocket handler `get_model_telemetry` (on-demand fetch; no leaking poller thread)
- [resonant_client/gui/static/app.js](resonant_client/gui/static/app.js) — `model_telemetry` event handler updates the harness-badge tooltip with "deepseek · 131k ctx · 14.3GB" and (when present) "experts: X/N"

**Design note (✅):** The plan originally called for a 30s polling thread. We landed on **on-demand fetch via the WebSocket command** instead — simpler, no thread-leak risk on disconnect, and the badge refreshes naturally on user focus. If we ever want a live counter we can add the poller, but the cost (one HTTP call per 30s per connected client) wasn't worth it for an info-only display.

**Verify:**
```bash
pytest tests/test_deepseek_specific.py -k telemetry -v

# Live check (requires Mac Studio reachable):
python -c "from resonant_client.backends import OllamaBackend; \
  b = OllamaBackend('http://10.0.0.133:11434', 'deepseek-v4-flash:cloud'); \
  print(b.get_runtime_telemetry())"
```

**Done when (✅):** Telemetry call succeeds against a live Ollama, returns at least `loaded_model` + `context_length` + `memory_mb`. MoE expert fields are returned only if Ollama exposes them (documented as "best-effort" in the SUMMARY for that day).

---

## Overall verification

```bash
cd D:/Repos/resonant-client

# Cluster suite (16 tests)
python -m pytest tests/test_deepseek_specific.py -q

# Sanity round-trip: thinking-mode option lands in Ollama options
python -c "from resonant_client.backends import OllamaBackend; \
  b = OllamaBackend('http://10.0.0.133:11434', 'deepseek-v4-flash:cloud', thinking='high'); \
  assert b._ollama_options.get('think') == 'high'; \
  print('thinking mode round-trips:', b._ollama_options['think'])"

# Sanity round-trip: big-context profile via env
RESONANT_OLLAMA_NUM_CTX=131072 RESONANT_OLLAMA_NUM_BATCH=2048 python -c \
  "from resonant_client.backends import OllamaBackend; \
   b = OllamaBackend('http://x','m'); \
   assert b._ollama_options['num_ctx'] == 131072; \
   assert b._ollama_options['num_batch'] == 2048; \
   print('big-context env override applies:', b._ollama_options)"
```

Manual end-to-end (requires Mac Studio reachable):

1. Toggle big-context profile in Settings → click "Reload backend" → verify `context_length=131072` via `curl http://10.0.0.133:11434/api/ps`.
2. Set thinking="high" on a deepseek session → ask a math problem → verify the response shows extended reasoning.
3. Open the harness-badge tooltip → confirm telemetry line shows context size and memory.

## Success criteria

- [x] Thinking-mode persists per session and round-trips through `SessionRecord` + `BackendSpec`.
- [x] Big-context profile toggle modifies env vars before backend init; respects user env override.
- [x] On-demand telemetry fetch succeeds; no leaked poller threads.
- [x] All gated on "is the model deepseek-v*?" — selectors don't appear for other backends.
- [x] No regression in `pytest`.

## Future / nice-to-haves (not yet built)

| Idea | Where it would go | Why it's not built yet |
|------|-------------------|-----------------------|
| Per-message thinking-mode override (e.g., a slash command `/think high <prompt>`) | `engine/session.py` system-prompt rule + `gui/app.py` WS message parser | Per-message would force per-message Ollama option diff → model unload/reload every turn. Today's "per-session" is the right granularity given the ~30–90s reload cost |
| Live MoE expert-utilization graph (sparklines per layer) | `gui/static/app.js` chart in the preview panel | Requires Ollama to expose per-token expert routing, which it doesn't yet (as of 2026-04). Track upstream: ollama/ollama#XXXX |
| "Auto-fall-back to lower thinking on timeout" — if a `high` request hits the read timeout, retry with `med` | `engine/session.py` retry logic in the Ollama stream path | Risky — would mask backend issues; better to surface the timeout |
| Profile picker in Settings: `quick`, `balanced`, `deep`, mapping to (thinking, num_ctx, num_batch) tuples | `gui/static/app.js` settings render + `app.py` `_apply_*` helpers | Two presets (default, big-context) plus the per-session thinking dropdown cover the field today; adding more would dilute the UI |

## Output

When extending this cluster, append a status entry here:

> 2026-04-26 — All 3 tasks shipped. 16 tests pass. No deviations from this plan. Telemetry landed as on-demand fetch instead of polling thread (cleaner, no leak risk). Per-session thinking_mode round-trips through SessionRecord (key: `thinking_mode`) and BackendSpec (key: `thinking_mode`).
