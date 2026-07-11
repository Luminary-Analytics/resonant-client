# Model-aware agent prompt architecture

This prompt design implements the canonical
[agentic harness north star](agentic-harness-north-star.md). Prompt overlays may
compensate for model behavior, but capabilities, modality routing, lifecycle,
evidence, and recovery belong to the runtime. Optimize prompt behavior for
correct completion and time to a trustworthy result before token efficiency.

Resonant uses one durable agent contract with small model-family and execution-
role overlays. This avoids maintaining several giant prompts that drift apart,
while still giving GLM, DeepSeek, and less-characterized open models the
operating style they handle most reliably.

## Prompt stack

The session assembles instructions in these layers:

1. Platform and working-directory facts.
2. The invariant Resonant agent contract.
3. A model-family profile selected from the backend model name.
4. A primary, sub-agent, or specialist role contract.
5. Repository instructions and scoped role instructions.
6. Stable per-turn retrieved context and the post-tool goal recitation.

The invariant contract defines the inspect → plan → act → verify → record loop,
long-running state discipline, truthful verification, tool-schema discipline,
and delegation contracts. Family profiles change emphasis, not permissions.

| Detected model | Profile | Main emphasis |
| --- | --- | --- |
| `glm-5*`, including `glm-5.2:cloud` | GLM 5.x | Long-horizon phase control, targeted use of large context, interleaved tool decisions, independent discovery and verification |
| Any model name containing `deepseek` | DeepSeek | Research-first planning, strict phase gates, exact tool/output schemas, atomic edits and progressive validation |
| Everything else | Conservative open model | Small evidence-backed steps, narrow reads and edits, immediate checks, cautious delegation |

Detection is intentionally simple and deterministic in
`resonant_client/engine/model_prompts.py`. A new family should add one focused
overlay and tests; it should not copy and fork the invariant contract.

## Long-running agent rules

- The original request and UI checklist are the durable task ledger.
- Every phase has a concrete completion condition.
- Phase handoffs preserve decisions, touched files, commands and results,
  unresolved risks, and the exact next action.
- Tool errors update the plan; identical failed calls are not repeated.
- Retrieved context is a targeted working set, not a repository dump.
- Completion requires implementation plus relevant verification evidence.

The runtime reinforces this prompt contract with mid-turn compaction, stable
turn context, a goal recitation after tool results, doom-loop guards, strict
tool-argument validation, and reasoning-content replay for thinking models.
Those are harness responsibilities; prompt wording is not treated as a
substitute for protocol correctness.

## Sub-agent contract

The `task` tool now asks the parent for a complete assignment containing:

- objective;
- read/write scope;
- relevant context and constraints;
- expected evidence;
- return format.

Workers cannot recursively delegate. Their role prompts require a compact
handoff covering outcome, evidence, inspected or changed locations, risks, and
the recommended parent action. The parent remains responsible for reviewing
evidence, integrating changes, resolving conflicts, and running final checks.

Specialists use the same family profile but receive a separate specialist role
layer containing the active graph node, dependency results, tool boundary, and
any strict output schema. This preserves the specialist's format reminder near
the end of the system prompt.

## Design sources

The architecture distills recurring strengths from the supplied
[Claude Code prompt](https://gist.github.com/chigkim/1f37bb2be98d97c952fd79cbb3efb1c6),
[Codex prompt](https://gist.github.com/chigkim/ffed11a3e017d98698707dd24e78af51),
and [Gemini CLI prompt](https://gist.github.com/chigkim/9547badac809e356b0ed005d8a35f7c1):
repository-grounded action, concise progress, persistent checklists, scoped
delegation, project-convention precedence, and verification before completion.
The wording in Resonant is original and tailored to its tools and runtime.

The model overlays also align with the vendors' current runtime behavior:

- [Z.AI thinking-mode documentation](https://docs.z.ai/guides/capabilities/thinking-mode)
  describes interleaved tool reasoning and preserved thinking for coding agents.
- [GLM-5 documentation](https://docs.z.ai/guides/llm/glm-5) positions the family
  for long-range agent tasks and multi-tool orchestration.
- [DeepSeek thinking-mode documentation](https://api-docs.deepseek.com/guides/thinking_mode)
  requires reasoning-content continuity across tool-call requests.
- [DeepSeek tool-call documentation](https://api-docs.deepseek.com/guides/tool_calls)
  warns that generated arguments still require validation and documents strict
  schema mode.

## Evaluation plan

Prompt changes should be evaluated by behavior rather than preference. Use a
small fixed suite for each supported model family and record:

- task completion and acceptance-check pass rate;
- malformed or rejected tool-call rate;
- repeated-call and read-only-churn interventions;
- edit rejection and repair round trips;
- number of tool steps and wall-clock time;
- unsupported success claims caught by deterministic checks;
- context-compaction count and post-compaction goal retention;
- sub-agent handoff completeness and duplicated-work rate.

Change one prompt layer at a time. Keep the task suite, model settings, tool
schemas, and context budget fixed so improvements can be attributed to the
prompt rather than the harness configuration.

## Context-maximization roadmap for GLM 5.2 and DeepSeek Pro v4

The next gains should come from context quality and recoverability, not simply
raising `num_ctx`. The Context cockpit added in the 2026-07-10 pass provides the
measurements needed to tune these changes.

1. **Budget context by source.** Reserve explicit shares for the invariant
   prompt, durable task ledger, recent dialogue, repository retrieval, worker
   handoffs, and tool output. Let unused shares flow to recent dialogue, but
   never allow logs or a single file read to crowd out the goal and decisions.
2. **Store tool evidence by receipt.** Persist large read/search/test outputs as
   hashed artifacts and keep a compact receipt in the model context. The model
   can rehydrate a range on demand instead of carrying thousands of stale log
   tokens through every inference.
3. **Make compaction invariant-aware.** Summaries must preserve the original
   objective, accepted constraints, user decisions, current checklist,
   modified files, verification results, unresolved failures, and exact next
   action. Validate those fields mechanically before replacing old turns.
4. **Build a hierarchical repository map.** Keep a stable, compact symbol and
   ownership map; retrieve implementation slices only for the active phase.
   Evaluate retrieval recall on known cross-file tasks rather than maximizing
   the number of files injected.
5. **Give workers isolated context budgets.** Send each worker the assignment,
   dependency evidence, relevant repo slices, and output contract—not the full
   parent transcript. Return a structured handoff receipt that the parent can
   inspect or rehydrate.
6. **Track prefix stability.** Hash each prompt layer and record cache-hit or
   byte-stability metrics. Keep the invariant contract and tool schemas fixed;
   append volatile roadmap, retrieval, and role data after the stable prefix.
7. **Reserve generation and tool headroom.** Trigger compaction from the
   effective backend window, not the advertised model maximum. Keep separate
   reserves for the next response, reasoning/tool arguments, and one large
   tool result so a successful call cannot immediately overflow the session.
8. **Add adversarial long-context evaluations.** Measure goal retention,
   decision recall, duplicate exploration, malformed calls, first causal
   divergence, and post-compaction convergence at several context sizes.

Family tuning should differ:

- **GLM 5.2:** retain a broader hierarchical working set and phase ledger,
  exploit independent read fan-out, then force synthesis before edits. Prefer
  later compaction when the effective window truly supports it, while guarding
  lost-in-the-middle with recency plus explicit phase summaries.
- **DeepSeek Pro v4:** keep shorter atomic evidence packets, exact schemas, and
  explicit research/implementation/verification boundaries. Preserve reasoning
  continuity required by the backend, but compact verbose tool transcripts
  earlier and rehydrate evidence by receipt when verification needs it.

The highest-value next implementation slice is the artifact-receipt store plus
mechanically validated durable ledger. Together they increase usable context
without depending on a larger model window and make compaction reversible.
