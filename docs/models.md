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
| `vision` | Describing images for a chat model that can't see them (below), and delegated vision work |
| `plan`, `explore`, `implement`, `test`, `review` | Delegated work (`task`) of that kind |

A role without a line uses the chat model.

## Images for models that can't see them

Some chat models only read text. If yours is one of them and **Models for
roles** has a `vision` line (for example `vision anthropic:claude-sonnet-5`),
Lumi asks that model to describe each image before the chat model's request:

- pictures you attach;
- screenshots from tools such as the browser or computer use.

The description transcribes visible text exactly, then describes the layout,
controls, charts and anything unusual. The chat model receives it as
`[Image: …, described by anthropic:claude-sonnet-5]` followed by the text. The
image itself stays in the conversation for models that can see it.

- Each image is described once. The description is saved with the
  conversation, so later turns don't ask again.
- The conversation shows "… described 1 image for …".
- These requests are recorded in the usage records with the purpose
  `image_description`, and priced like other requests.
- An image that fails to be described twice keeps the usual notice ("image
  attached, no textual representation"); Lumi doesn't guess.
- Without a `vision` model, nothing changes: a text-only model gets the notice.

A custom connection's **Models accept images** setting decides whether its
models get images. When it's off, they get the descriptions, or the notice,
instead of the image.

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
