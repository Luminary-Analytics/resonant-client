# GLM-5.2 and DeepSeek V4 Pro agent-context playbook

This is the operating plan for making Resonant's two flagship open-model
agents reliable over long coding sessions. It separates **capacity** (how many
tokens the endpoint accepts) from **useful context** (the smallest stable set
of facts that lets the model act correctly).

## Current runtime profile

| Model | Advertised window | Resonant default | Interactive effort | Hard mission phases |
|---|---:|---:|---|---|
| `glm-5.2:cloud` | 976K | 999,424 tokens | High | Max for plan/reflect/verify/repair |
| `deepseek-v4-pro:cloud` | 1M | 1,048,576 tokens | User-selected | Max when thinking is enabled for plan/reflect/verify/repair |

An explicit `RESONANT_OLLAMA_NUM_CTX` still wins. Runtime `/api/show`
metadata can clamp an over-large request, and compaction starts at 75% of the
effective window so output and new tool results retain headroom.

Primary references:

- [Ollama GLM-5.2 model card](https://ollama.com/library/glm-5.2) lists a
  976K context window and High/Max effort.
- [Ollama DeepSeek V4 Pro model card](https://ollama.com/library/deepseek-v4-pro)
  lists a 1M context window and non-thinking/High/Max modes.
- [DeepSeek thinking-mode contract](https://api-docs.deepseek.com/guides/thinking_mode)
  requires reasoning replay across tool-call turns and says thinking mode
  ignores sampling controls.
- [Ollama chat API](https://docs.ollama.com/api/chat) documents top-level
  `think`, `message.thinking`, `format`, usage counters, and `keep_alive`.
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
  documents JSON Schema decoding and currently notes that Ollama Cloud does
  not support it.

## Context stack, in priority order

1. **Immutable prefix**: system contract, project instructions, and a stable,
   consistently ordered tool schema. Do not put timestamps, random IDs, live
   counters, or changing skill lists here.
2. **Repo orientation**: a sub-1K-token dependency-weighted repo map containing
   paths and signatures, not file bodies.
3. **Turn retrieval**: query-specific file candidates, relevant memory, and up
   to six skill descriptions. Resolve this once per user turn and keep it
   byte-stable through every tool step.
4. **Append-only trajectory**: user messages, assistant decisions, normalized
   tool calls, and deterministic tool results. DeepSeek tool-call messages
   retain their reasoning content.
5. **Just-in-time evidence**: paginated file reads/search results. Keep paths,
   line ranges, counts, and continuation calls in context; re-fetch bodies.
6. **Tail recitation**: original objective plus the current checklist after
   every tool step. This is the anti-drift layer and must survive compaction.

The full model window is a safety margin, not a target fill level. Even on a
1M model, a 40K focused trajectory normally beats a 400K dump of undifferentiated
source. Large capacity should let the agent avoid destructive summarization,
not encourage eager repository ingestion.

## Compaction policy

Resonant now checks the actual backend window before every inference step.
When the 75% threshold is crossed:

1. Evict old oversized tool payloads while retaining the newest eight results.
   Receipts preserve tool name and original size and tell the model to re-fetch
   with pagination.
2. If that is insufficient, summarize old conversation turns while preserving
   recent turns verbatim.
3. Re-inject the objective, todo state, repo orientation, and current project
   conventions after the compaction boundary.

Future refinement: replace the prose summary with a validated ledger containing
`decisions`, `files_changed`, `commands_run`, `open_questions`, `failed_approaches`,
and `next_actions`.

## Model-specific rules

### GLM-5.2

- Use top-level `think`; High is the daily-driver setting, Max is reserved for
  hard decision phases.
- Keep vendor sampling at temperature 1.0 / top-p 0.95 for normal agent turns.
- Coerce JSON-stringified object/array tool arguments before schema validation.
- Prefer a concise repo map plus agentic grep/read over loading entire trees.
- Treat XML/argument-template drift as a wire problem, not a reason to weaken
  execution boundaries.

### DeepSeek V4 Pro

- Replay assistant reasoning on every tool-call continuation. Dropping it can
  make the next request invalid and breaks interleaved reasoning.
- Omit temperature/top-p while thinking is enabled; the vendor contract says
  those controls do not apply.
- Use Max for planning, reflection, verification, and repair; High can be used
  for implementation when latency matters.
- Keep the current 65,536 output-token clamp until the Ollama cloud endpoint is
  proven to accept more reliably.
- Continue salvaging leaked DSML tool-call tokens and validate every argument
  against the advertised tool schema before execution.

## Measurement plan

The smoke harness now exposes `glm`, `pro`, and `flash` labels and records:

- convergence and stop reason;
- edit attempts, successful applications, and fuzzy-edit rescues;
- tool-argument validation failures;
- backend retry count;
- structured-output repair count;
- tool-call count and per-iteration duration.

Run every validated mission at least three times per flagship and compare
medians rather than single runs. The first useful experiment matrix is:

| Variable | GLM-5.2 | DeepSeek V4 Pro |
|---|---|---|
| Effort | High vs Max | High vs Max |
| Context cap | 256K vs full advertised | 256K vs full advertised |
| Repo map | on vs off | on vs off |
| Tool-output eviction | 8 vs 16 recent results | 8 vs 16 recent results |

Optimize for convergence first, then malformed-call rate, then edit success,
then latency. Do not choose a profile solely because it consumes more context
or thinking tokens.

## Remaining high-value work

1. Add shadow-git workspace checkpoints and a one-click rewind boundary before
   autonomous edit batches.
2. Replace regex symbol extraction with tree-sitter and real import/reference
   edges; keep the same small repo-map output contract.
3. Move the RAG index cache out of the user's repository with legacy migration.
4. Add OS-enforced filesystem/network sandboxing around shell commands.
5. Use schema-constrained finalization for local Ollama models and a supported
   strict-output API for cloud models; do not send unsupported `format` schemas
   to Ollama Cloud.
6. Re-run the two previously unvalidated smoke specs and establish checked-in
   GLM-5.2 and DeepSeek V4 Pro baselines on the target Mac Studio.
