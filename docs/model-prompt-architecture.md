# Model execution and prompt architecture

This document implements the canonical
[agentic harness north star](agentic-harness-north-star.md). Optimize first for
correct completion and time to a trustworthy result. Token efficiency matters
here because smaller, stable inputs reduce local-model prefill latency and leave
the model's attention on the task—not because tokens are scarce.

## Design rule

Resonant uses one compact, model-neutral agent contract with a short execution-
role suffix. The runtime, not prompt prose, owns permissions, cancellation,
context, recovery, and completion evidence. GLM, DeepSeek, Kimi, and other open
models therefore receive the same product contract and a stable provider prefix.

The prompt stack is:

1. Platform, shell, and working-directory facts.
2. A compact inspect, act, verify, and report contract.
3. Clarification and bounded-delegation rules.
4. A primary, sub-agent, or specialist role suffix.
5. Repository instructions, scoped role instructions, and stable task context.

Model-family detection remains in `engine/model_prompts.py` for telemetry and
evaluation labels. It does not fork behavior or produce different prompt text.
Prompt changes must preserve the performance contracts in
`tests/test_model_prompts.py`: the invariant prompt and assembled base prompt
have explicit size ceilings.

## Fast execution path

The normal Ollama/Kimi coding path is deliberately small:

1. Send a byte-stable system prompt and a ten-tool coding core: read, write,
   edit, shell, glob, grep, parallel read batch, sub-agent, user decision, and
   specialized-tool discovery.
2. Stream one assistant response.
3. Execute tool calls and append their results to conversation history.
4. Call the model again directly from those results. Do not insert a synthetic
   “continue” turn or repeat the original goal after every tool.
5. Deliver steering exactly once as an appended user message at the next safe
   model boundary.
6. Stop on a final response. Use deterministic completion gates only when
   concrete evidence says an authorized implementation did not happen.

Uncommon desktop, process, recording, REPL, and git tools are loaded by
`search_tools` only when required. Ollama receives loaded schemas in subsequent
top-level `tools`; Kimi uses its provider-native in-history catalog. Director
mode explicitly adds its orchestration tools and is not limited to the normal
coding core.

This optimizes the common path without removing capabilities. The full tool
catalog, durable agents, worktrees, checkpoints, hooks, artifacts, compaction,
multimodal inputs, and Director mode remain runtime services.

## Long-running behavior

- The original request, conversation, and UI checklist are the durable ledger.
- Reuse settled evidence instead of repeatedly reading the same files.
- Keep phases and completion conditions explicit for non-trivial work.
- Treat tool errors as observations; revise the approach instead of repeating
  an identical failed call.
- Preserve decisions, touched files, verification results, unresolved risks,
  and the exact next action through compaction and worker handoffs.
- Completion requires the requested behavior plus relevant verification.

The runtime reinforces this behavior with mid-turn compaction, stable task
context, tool-output truncation, duplicate-read suppression, cycle nudges,
strict argument validation, and reasoning replay for thinking models. It does
not recite the goal after every tool: append-only history already contains the
request and checklist, while repeated synthetic messages increase prefill work
and can distract weaker models.

## Sub-agents and specialists

`task` delegates a bounded independent assignment. Workers receive an isolated
context and return outcome, evidence, inspected or changed locations, and open
risk. They do not recursively delegate. The parent reviews returned evidence,
integrates changes, resolves conflicts, and owns final verification.

Specialists use the same small base prompt plus a scoped role layer containing
the active graph node, dependency evidence, tool boundary, and output schema.
Director mode is an explicit orchestration path; it should not add planning or
coordination overhead to ordinary single-agent coding turns.

## Design sources

The 2026-07 execution simplification uses [Pi](https://pi.dev/) and the
[Pi coding-agent source](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)
as the primary minimal-loop reference. Resonant adopts the principles of a small
stable prompt, a tiny default tool surface, direct model/tool iteration,
tool-boundary truncation, and steering at safe boundaries. It keeps Resonant's
existing UI and durable runtime instead of copying Pi's product surface.

The prompt also incorporates useful ideas from the supplied
[Claude Code prompt](https://gist.github.com/chigkim/1f37bb2be98d97c952fd79cbb3efb1c6),
[Codex prompt](https://gist.github.com/chigkim/ffed11a3e017d98698707dd24e78af51),
and [Gemini CLI prompt](https://gist.github.com/chigkim/9547badac809e356b0ed005d8a35f7c1):
repository-grounded action, concise progress, scoped delegation, project-
convention precedence, and verification before completion. Resonant's wording
is original and tailored to its tools.

## Evaluation contract

Evaluate execution changes with a fixed task suite for each supported model and
record:

- task completion and acceptance-check pass rate;
- time to first tool call and total wall-clock time;
- provider prompt characters/tokens and advertised tool-schema characters;
- prompt-evaluation duration and cache reuse where the provider reports them;
- malformed/rejected and repeated tool-call rates;
- edit-repair round trips and verification pass rate;
- compaction count and post-compaction goal retention;
- worker handoff completeness and duplicated work.

Change one layer at a time and hold model settings, task inputs, and effective
context windows fixed. Performance is a behavioral measurement, not a reason to
weaken safety boundaries or verification.

## Context roadmap

The next gains should improve usable context rather than merely raise `num_ctx`:

1. Store large tool evidence as durable receipts with short in-context previews.
2. Validate that compaction preserves objective, decisions, checklist, changed
   files, verification, unresolved failures, and exact next action.
3. Build a compact hierarchical repository map and retrieve slices for the
   active phase instead of injecting broad dumps.
4. Give workers isolated budgets and return structured, rehydratable handoffs.
5. Record prompt-prefix hashes and provider prompt-evaluation telemetry.
6. Reserve headroom for the next response, tool arguments, and one large result.
7. Run adversarial long-context evaluations at several effective window sizes.

GLM can profit from a broader hierarchical working set and later compaction when
the effective window supports it. DeepSeek benefits from atomic evidence packets,
exact schemas, and strict reasoning continuity. These are context-policy choices;
they must not grow separate giant system prompts.
