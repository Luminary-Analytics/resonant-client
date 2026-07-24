# GLM 5.2 on EXO: QA and performance baseline

Date: 2026-07-23
Harness version under test: 0.11.4 release candidate
Model: `mlx-community/GLM-5.2-mxfp4`
Serving path: EXO OpenAI-compatible API, two-shard `MlxJacclInstance`

## Result

The Resonant execution path is functioning correctly and adds negligible local
overhead. End-to-end latency is dominated by uncached EXO prefill and GLM
generation. EXO's prefix cache is working and is the most important performance
lever: an equivalent warm request was more than 17 times faster than the first
observed uncached request.

The QA pass found and fixed five harness issues:

1. Read-only shell inspection and validation incorrectly closed the user
   alignment window, which could suppress a valid decision and start an
   unnecessary model step.
2. A complete answer followed by a suppressed low-value question could start
   another generation instead of ending the session.
3. EXO advertised image support for text-only models and had no model-selection
   warmup, despite the existing desktop warmup lifecycle.
4. EXO runner-shutdown error chunks ended otherwise recoverable steps. Resonant
   now retries the uncommitted generation up to two times and removes abandoned
   partial prose before replay.
5. EXO transport keepalives were hidden. The working indicator now distinguishes
   an active connection from genuine model progress without allowing keepalives
   to defeat the semantic-idle watchdog.

## Measurements

All times are wall-clock observations from the configured LAN cluster. They are
not synthetic estimates.

| Scenario | Result |
|---|---:|
| EXO `/state` control-plane latency | 314 ms |
| EXO `/v1/models` latency | 208 ms |
| Resonant payload build + JSON serialization | 0.041 ms average |
| Stable system instructions | 2,525 characters |
| Ten-tool compact core schema | 6,492 characters |
| First observed tiny uncached tool request | 64.34 s to first semantic token; 65.60 s total |
| Immediate equivalent warm request | 2.16 s to first semantic token; 3.66 s total |
| Warm-request cache result | 151 of 170 prompt tokens cached |
| First full two-step Resonant tool loop | 20.35 s total |
| Local `file_read` dispatch inside that loop | 12 ms |
| Repeated full two-step Resonant tool loop | 10.00 s total |
| Repeated first-step cache result | 2,285 of 2,286 prompt tokens cached |
| Task after explicit EXO warmup | 2.62 s |
| Long-context native tool probe | 16,849 prompt tokens; 97.62 s to first semantic token |
| Long-context uncached prefill rate | approximately 172 prompt tokens/s |
| Stop during active EXO prefill | session ended in 2.02 s; zero commands left in EXO state |
| Full long-context retention probe | 30,205 prompt tokens; 234.45 s total |
| Long-context marker retention | exact beginning, middle, and end values |
| Cached post-tool final response | 7.58 s; 30,203 of 30,266 prompt tokens cached |

The full two-step test required GLM to call Resonant's native `file_read`, receive
the actual `pyproject.toml` result, and return exactly
`RESULT: resonant-client`. The model completed correctly on both runs.

A second live probe supplied 30,154 tokens of entirely new evidence with unique
markers at the beginning, middle, and end. EXO reported prefill progress at
4,096-token intervals through 100 percent. GLM returned all three values exactly
in a native tool call after 234.45 seconds. The post-tool turn then reused 30,203
cached tokens and returned exactly `LONG_CONTEXT_COMPLETE` in 7.58 seconds.

## Performance interpretation

- Resonant's Python request assembly and local tool dispatch are not meaningful
  bottlenecks at current scale.
- The stable system/tool prefix is being reused across both tool turns and
  separate sessions. Preserving that byte-stable prefix is essential.
- New tool evidence still requires prefill. The harness should continue to use
  paginated reads, duplicate-read receipts, and stale tool-output eviction
  rather than broad repository dumps.
- Cold model compilation can dominate a small request. EXO now implements the
  same background `warm_up` contract already used by the desktop model picker.
  A real task preempts the speculative warmup so the two do not compete.
- Large, genuinely new context remains expensive even with a 1M-token window.
  Maximum context availability should remain uncapped, but operational context
  should be selected for relevance rather than filled indiscriminately.
- A transport keepalive proves the connection is alive but not that inference
  advanced. The UI exposes that distinction, while only token or prefill
  progress resets the 120-second semantic-idle safeguard.
- EXO runner shutdown is recoverable before tool commitment. Resonant retries
  that generation twice with the identical cache-stable request, then surfaces
  a clean terminal failure if the cluster cannot recover.

## Correctness and lifecycle coverage

- Native GLM tool call: pass.
- Tool-result continuation: pass.
- Exact final response: pass.
- Prefix-cache reuse: pass and now reported in the EXO capability profile.
- User cancellation while blocked in provider streaming: pass.
- Remote EXO command cleanup: pass.
- Malformed/repetitive output guards: covered by provider tests.
- Text-only image handling: now produces an explicit textual fallback instead
  of sending an unsupported native image payload.
- Consequential `await_user` after read-only investigation: now parks correctly.
- Suppressed trailing "what next?" after a complete answer: now terminates.
- Long-context beginning/middle/end retention and final-answer transition: pass.
- Transient EXO runner-shutdown replay: deterministic test coverage.
- Repeated runner failure: bounded to three total attempts.

## Serving-side recommendations

Resonant cannot eliminate the cluster's uncached model compute. For further
gains, benchmark the available EXO placements with the official `exo-bench`
tool and enable EXO placement warmups where appropriate. EXO documents
`--warmup` runs and reports prompt and generation throughput for ring/JACCL and
pipeline/tensor placement comparisons:

- <https://github.com/exo-explore/exo#benchmarking>

Keep the same model instance resident between agent steps. Restarting or
replacing the instance discards the cache benefit measured above.

EXO v1.0.69 also documents stale runner-state cleanup and runner-crash error
chunks. Keep the cluster current so its server-side lifecycle fixes complement
Resonant's client-side recovery:

- <https://github.com/exo-explore/exo/releases/tag/v1.0.69>
