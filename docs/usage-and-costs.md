# Usage records and prices

Lumi records every model call it makes: the turn's calls, a delegated worker's
calls, and auxiliary requests such as session titles and conversation
compaction. Each record carries the tokens, the price used, and who, where and
why. **Settings > Usage & cost** summarizes them, and `lumi usage` exports
them.

The code is `lumi/usage.py` (records), `lumi/pricing.py` (prices) and
`lumi/gui/costs.py` (the daily totals Settings shows).

## The records

One JSON line per call in `~/.lumi/usage/YYYY-MM.jsonl`, one file per UTC
month:

```json
{"v": 1, "id": "5f0c…", "ts": "2026-09-25T04:00:00.123Z", "user": "alice",
 "project": "C:/src/app", "session": "<conversation id>", "agent": "",
 "purpose": "turn", "provider": "anthropic", "model": "claude-sonnet-5",
 "input_tokens": 12000, "cached_tokens": 9000, "cache_write_tokens": 0,
 "output_tokens": 800, "reasoning_tokens": 0,
 "cost_usd": 0.0122, "computed_cost_usd": 0.0122, "reported_cost_usd": null,
 "price_source": "catalog", "elapsed": 3.2}
```

| Field | Meaning |
|---|---|
| `user` | The operating-system account that ran Lumi |
| `project`, `session`, `agent` | The project folder, the saved conversation (`gateway:<chat>` for the chat gateway) and the worker, if any |
| `purpose` | `turn`, `subagent`, `title` or `compression` |
| `input_tokens` | Every prompt token, cached ones included; `cached_tokens` and `cache_write_tokens` are the parts read from or written to the provider's prompt cache |
| `cost_usd` | The cost used for totals and budgets: the reported cost when there is one, otherwise the computed one. `null` when the model has no price |
| `computed_cost_usd`, `reported_cost_usd` | Lumi's own calculation and the provider's, kept side by side |
| `price_source` | `reported`, `organization`, `override`, `local`, `catalog`, `subscription` or `unpriced` |

**Settings > Usage & cost > Enable cost tracking** (`cost_tracking.enabled`)
turns recording off. The GUI, the terminal UI and the chat gateway share the
files through an OS file lock.

## How a call is priced

In this order:

1. **Reported:** the provider said what the call cost (OpenRouter).
2. **Organization:** an [organization policy](enterprise-policy.md)'s
   `pricing.prices`, for negotiated rates.
3. **Yours:** the prices under **Usage & cost > Prices**
   (`cost_tracking.price_overrides`).
4. **Local:** Ollama, EXO and LM Studio models count as $0.
5. **Subscription:** Codex, Claude Code and Ollama cloud models are covered by
   their plans. They are recorded with their tokens and no per-call cost.
6. **Catalog:** Lumi's bundled prices. They were checked on September 25, 2026
   against [Anthropic's](https://platform.claude.com/docs/en/about-claude/pricing)
   and [OpenAI's](https://developers.openai.com/api/docs/pricing) published
   pages (standard tier; no batch, fast-mode or regional pricing). The Moonshot
   entry was carried over from an earlier release and not rechecked.
7. **Unpriced:** anything else, including SONN and custom connections. The
   cost is `null`. Totals count these calls separately instead of treating
   them as free.

Prompt-cache reads and writes use their own prices when the list has them.

### Setting your own prices

One model per line: a pattern, the input and output prices in USD per million
tokens, then optionally the cached-input and cache-write prices. `#` starts a
comment.

```text
# Our Azure deployment
conn-*:gpt-5.4 2.2 13 0.22
sonn:* 1 4
```

A pattern is an `fnmatch` glob. It is matched against `provider:model`, or
against the model alone when it has no provider part. The first matching line
wins.

An organization policy uses the same prices as JSON:

```json
"pricing": {"prices": {"anthropic:claude-opus-*": {"input": 3.2, "output": 16, "cached_input": 0.16}}}
```

## Exporting

```bash
lumi usage
```

`lumi usage` summarizes this month by model. Options:

- `--since` and `--until` take a date as `YYYY-MM-DD`; `--until` is the day
  after the last.
- `--by` groups the summary by `provider`, `project`, `purpose`, `user` or
  `session`.
- `--format csv` or `--format jsonl` exports the records themselves.

## Not covered yet

- The quick "should this turn plan first?" classification some backends run,
  and skill-mission extraction, are not recorded.
- Autonomous mission budgets (`general.budget_usd_max`) count priced calls
  only. An unpriced call adds its tokens.
- The records stay on the machine. Team totals and budgets across machines
  need Lumi Cloud.
