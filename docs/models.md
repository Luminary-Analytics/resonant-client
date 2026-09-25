# Fallback models, roles and capabilities

## Fallback models

**Settings > General > If the model fails, continue with** lists up to five
models, one `provider:model` per line (`general.fallback_models`), for
example `anthropic:claude-sonnet-5` or `ollama:qwen3:32b`.

When a model request fails before any answer arrives, the turn continues with
the next usable model on the list. Failures that trigger this include
provider errors, overload, rate limits past the provider's retries, and
connection failures. The same step is retried, and the conversation keeps its
history:

- The conversation shows *"anthropic:claude-opus-5-5 failed (HTTP 529
  overloaded); continuing with anthropic:claude-sonnet-5."*
- The audit log records `model.fallback`, with both models and the reason.
- A fallback that is the failing model itself, that the organization's policy
  doesn't allow, that an organization budget can't price
  (`block_unpriced`), or that can't be built (no key, say) is skipped.
- The next turn tries your chosen model first again.

A request that fails after the model began answering isn't retried
elsewhere; the turn stops with the error, as before.

`lumi run` takes `--fallback provider:model` (repeatable) in addition to the
Settings list.

## Models for roles

**Settings > General > Models for roles** takes one `role provider:model`
per line (`general.role_models`), for example `summarize ollama:qwen3:8b`.
Advanced options per role (thinking level, step limit) stay in
`settings.json` under `model_roles`; a line here overrides that entry's
provider and model.

| Role | Used for |
|---|---|
| `summarize` | Naming sessions and compacting long conversations. This also lets sessions be named while the chat model is Codex or Claude Code, without starting their tool loop |
| `plan`, `explore`, `implement`, `test`, `review`, `vision` | Delegated work (`task`) of that kind |

A role without a line uses the chat model.

## Capability overrides for administrators

Lumi infers each model's context window, vision, tool calling and reasoning
levels from its name and from what the provider reports (`lumi/capabilities.py`).
An organization policy can state them instead:

```json
"models": {
  "capabilities": {
    "claude-*": {"context_window": 100000},
    "qwen3*": {"tools": true, "reasoning": ["low", "high"], "computer_use": false},
    "llava*": {"vision": false}
  }
}
```

| Field | Effect |
|---|---|
| `context_window` | The window Lumi plans prompts and compaction for |
| `vision` | Whether images and screenshots are sent to the model |
| `tools`, `parallel_tools` | Native tool calling |
| `reasoning` | The thinking levels offered, or `false` for none |
| `computer_use` | Whether the model may drive the desktop. By default it follows vision and tools |
| `max_safe_concurrency` | How many requests Lumi sends to it at once |

Patterns match the model name. The first matching pattern wins, and it wins
over what a provider reports at runtime. Values of the wrong type make the
policy invalid, which blocks requests until the policy is fixed.
