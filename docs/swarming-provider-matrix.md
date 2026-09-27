# Team execution and provider qualification

This matrix describes source eligibility and observed checks. It does not declare
a supported swarming release. Exact models remain user selections; no provider
name, routing alias or successful scripted response establishes model quality.
See the [evidence ledger](swarming-progress.md) and [live qualification plan](swarming-live-qualification.md).

| Provider path | Source Team eligibility | Current provider-specific evidence | Live qualification |
| --- | --- | --- | --- |
| Ollama native | Eligible with an explicit saved model and usable tool capability | Guarded rejection/timeout and no hidden generation probe tests; packaged workers use an actual loopback HTTP server with scripted responses | Pending selected reachable local model |
| EXO native | Eligible with an explicit saved model | Controlled HTTP failure and stream-restart checks retain one generation attempt | Pending |
| Kimi native | Eligible with an explicit saved model and protected key source | Controlled HTTP failure checks retain one generation attempt | Pending |
| OpenRouter native | Eligible with an explicit saved model and its separate API key | Controlled HTTP failure checks retain one generation attempt; existing adapter owns its wire format and reported costs | Pending |
| SONN native | Eligible with an explicit saved model and project connection | Controlled HTTP failure checks retain one generation attempt; existing adapter preserves the complete project URL and text-only conversion | Pending |
| OpenAI-compatible connection (NVIDIA NIM, vLLM, gateways) | Eligible with an explicit model on a connection of that type using a key or no authentication; the run reads the connection once | Controlled HTTP failure checks retain one generation attempt and its status code; a real worker process runs on a loopback Chat Completions server with the connection's key and headers, which stay out of events | NVIDIA NIM (`https://integrate.api.nvidia.com/v1`): benchmark cases with `moonshotai/kimi-k3` and orchestrated teams with Kimi K3 and `nvidia/nemotron-3-super-120b-a12b` ran live (September 27); see the [benchmarks](swarming-benchmarks.md#live-runs-on-nvidia-nim). No qualification threshold is declared |
| Codex CLI / Claude Code CLI | Unavailable as Team workers | Their independent tool loops do not provide this native execution guard | Requires a separate qualified adapter |

At least one local and one hosted native model must complete the declared live
baseline and comparison before expanding the qualified matrix. A cloud-backed
Ollama model is not evidence of local inference. Existing provider credentials,
account discovery and ordinary single-session support do not authorize a paid
qualification run or qualify concurrent work.

## Native execution contract

| Capability | Enforced source behavior and limits |
| --- | --- |
| Delegation and messaging | Asynchronous owned native children, scoped durable mailboxes and explicit input-delivery receipts. Peer text does not grant authority or prove comprehension. |
| Files and tools | Read/write scopes are checked at dispatch. Writers require isolated Git worktrees. Worker tools are bounded; arbitrary project code is not an OS sandbox. |
| Cancellation | Pause and Stop close new admission. Already admitted calls/effects may still finish; actual process cleanup and interrupted request uncertainty remain separately visible. |
| Request accounting | Primary and auxiliary generation share durable allowances. Hidden generation retries are disabled. A request count is not a token or dollar ceiling. |
| Check provenance | Trusted named checks bind the exact candidate and observed process outcome. Model prose and a successful unrelated shell command cannot accept a result. |
| Content and modality | Artifact references carry identity and provenance; they grant no content access and establish no visual inspection. Binary/image artifact delivery is unsupported in the Team text-artifact tool. Native provider modality adaptation remains capability-driven. |
| Managed execution | Explicit organization setup adds current host, owner, policy and shared-resource admission. Host credentials remain outside workers. Offline admission fails closed while local Stop remains usable. |

Scripted native, real HTTP, process, Git, browser and database checks establish
the behavior exercised in those fixtures. They do not measure model correctness,
provider throughput, comparative speedup or independent-machine availability.
