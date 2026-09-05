# Open-model reliability review

Date: 2026-09-05
Scope: the first four recommendations from the implementation review.

## Implemented

- Tool fallback accepts complete bare JSON call envelopes only for advertised
  tools. Prose, fenced examples, incomplete JSON sequences, and informal shell
  text do not become tool calls. XML recovery requires text mode and complete
  envelopes; DSML recovery is limited to DeepSeek deployments. Native tool calls
  remain the primary protocol.
- Context accounting includes tool arguments, replayed reasoning, loaded schemas,
  system and retrieved instructions, image reserves, and message overhead. Short
  histories no longer bypass compression. Every inference step checks the window
  again after compaction. If the request still does not fit, the session retains
  its history and reports an actionable error instead of sending it anyway.
- EXO no longer assumes every model has a million-token context window. It uses
  conservative model inference; `RESONANT_EXO_CONTEXT_TOKENS` sets the actual
  configured deployment window. This is a client budget, not a server setting.
- Compaction archives historical tool output before eviction when an artifact
  store is available. The discoverable `artifact_read` tool retrieves bounded
  character pages without rerunning commands. Restricted workers do not archive
  command evidence they lack permission to retrieve. Without an artifact store,
  command output stays in context.
- Duplicate-read suppression checks that the complete earlier result is still
  present. An eviction receipt, previous deduplication message, or truncated
  preview cannot stand in for the original evidence.
- Summaries must contain decisions, changes, verification, unresolved failures,
  and the next action. Missing fields reject the summary. User requirements,
  checklist state, observed tool evidence, and original multimodal user messages
  are preserved independently of that summary. Summarizer requests are bounded
  and honor cancellation.
- Thinking now distinguishes provider default from explicit off through the UI,
  saved backend configuration, and Ollama request. Off sends `think: false`.
  Level-only GPT-OSS configurations reject off/max before saving the setting.

## Verification

`tests/test_open_model_reliability.py` adds 41 regression cases covering the
reproduced failures, provider response parsing, context pressure, artifact
retrieval, summary retention, multimodal preservation, and UI-to-backend thinking
settings. Existing expectations were updated where they encoded the old behavior.

Validation uses simulated provider responses and real local session/tool logic.
No live model quality or speed benchmark was performed. The full suite passed:
3,116 passed and 2 skipped. Python lint, JavaScript syntax, and whitespace checks
pass. The thinking selector also passed behavior checks for level-only and
non-thinking models.

## Re-evaluation and next work

1. **Calibrate context against real deployments.** Counts remain estimates, not
   tokenizer-exact measurements; image cost is a reserve. EXO still needs explicit
   configuration when model inference does not describe its deployed window.
   Next, use endpoint metadata and actual prompt-usage telemetry to calibrate
   budgets and verify the configured window. Benchmark several window sizes.
2. **Make capability discovery endpoint-specific.** Existing Ollama tool/vision
   caches still use model names. Key them by endpoint and model revision, make
   reported metadata authoritative, and verify tool-call round trips. Generalize
   compatible-API support for vLLM/SGLang as a separate change.
3. **Run ordinary coding evaluations on real open models.** Use fixed multi-file
   repair tasks, long tool loops, malformed calls, and restart scenarios on small
   and large quantized deployments. Measure acceptance-test success, unsupported
   success claims, recovery frequency, and elapsed time over repeated runs.
4. **Tune execution presets from those measurements.** Version sampling, reasoning,
   effective context, parser/template, and quantization settings separately from
   the shared product prompt.

Schema validation ensures required summary fields exist; it cannot prove their
semantic accuracy. Mechanical retention protects the recorded constraints and
evidence, while live evaluations must establish whether models use them correctly.
The stricter text fallback deliberately treats mixed explanatory/tool text as
prose. Deployments relying on ambiguous recovery should use native tool parsing or
emit only their supported call envelope.
