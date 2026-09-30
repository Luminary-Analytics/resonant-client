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
| Anthropic native, and Anthropic connections (a gateway, or Claude on Bedrock with a Bedrock API key) | Eligible as orchestrator and workers with an explicit model and a key; the Messages adapter keeps the one-generation contract (September 30) | Controlled HTTP (429, 529, 5xx) and in-stream overload checks retain one generation attempt; a cut-short stream stays uncertain; scripted Messages-API servers run an orchestrated writer team (plan, write, check, apply, report), a team whose every participant is its own process, a Claude orchestrator over Azure workers, a participant's last request (its tools resent with `tool_choice` none, as the API requires for a history with tool calls), and Stop mid-stream, with signed thinking replayed and requests priced from the catalog | Pending: no Anthropic key has been used |
| OpenAI native, and OpenAI or Azure OpenAI connections with a key | Eligible as orchestrator and workers with an explicit model (or Azure deployment) and a key; the Responses adapter keeps the one-generation contract (September 30) | The same controlled checks; scripted Responses-API servers run an orchestrated read-only team over two rounds with encrypted reasoning replayed, and Azure workers on the deployment endpoint with its `api-key` header (recorded unpriced) | Pending: no OpenAI or Azure key has been used |
| Claude on Vertex AI, Bedrock with AWS sign-in, Azure Entra ID, OAuth, client certificates, capability-pack providers | Unavailable: a participant would sign in with the owner's cloud identity or read a key file, or a pack's process answers its own way | Refused when the team starts, with the reason | Not planned for the alpha |
| Codex CLI / Claude Code CLI | Unavailable as orchestrator and as workers; the Team panel says so and names the models a team runs on | Their independent tool loops do not provide this native execution guard: tool calls are observed after the fact, a turn is many requests, and exclusions and path scopes aren't enforced | Requires a separate qualified adapter |

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
