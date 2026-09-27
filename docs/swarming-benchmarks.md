# Swarming benchmark fixtures

**Status:** SW-002 evaluation inputs and a bounded source benchmark runner, with
scripted harness self-tests. No baseline model runs, comparative performance,
provider qualification, or swarming release claim.

The [swarming plan](swarming-plan.md) requires the same predeclared objectives
and input revisions across single-agent, batch, and swarm execution. The pack in
`tests/fixtures/swarming/` supplies four small standard-library Python projects.
They are materialized only in fresh temporary directories, never in user projects.
The CSV project includes an HTML frontend and a WSGI backend.

| Scenario | Objective and acceptance | Comparison purpose |
| --- | --- | --- |
| `independent_investigation` | Reproduce two independent defects; report both observed/expected values and source locations; preserve source | Independent read work |
| `csv_export` | Separate backend and frontend edits; round-trip quoted, Unicode, multiline and empty CSV; submit the actual form route through local HTTP | Two-writer integration |
| `serial_control` | Migrate a shared normalization/aggregation/rendering pipeline to exact integer cents; reject invalid events | Tightly coupled serial control |
| `interruption_recovery` | Inject interruption after a durable file effect but before its checkpoint; explicitly resume exactly once; reject corrupt state before writing | Recovery and uncertainty |

`scenarios.json` declares prompts, editable files, seed contents, and exact check
names. `references.json` holds deterministic reference solutions solely for
harness self-tests. Never expose the reference pack or external verifier to the
agent being evaluated. SHA-256 manifests pin prompts, inputs, reference solutions,
and acceptance code. `benchmark.py` pins the manifest itself. Changing the pack
requires a deliberate new manifest and input identity; do not compare runs from
different identities as the same benchmark. These hashes establish content
identity, not trusted execution or protection from a machine owner changing code.

Use `benchmark.materialize(scenario_id)` as a context manager for each fresh run.
Keep that context open while the agent works; retain its final candidate and
run record in temporary or normal project-owned runtime storage if needed before
the context exits. `benchmark.evaluate(scenario_id, project)` computes the exact
candidate snapshot, launches the external acceptance process, and returns each
observed check plus the derived accepted state. The verifier checks immutable
inputs and confirms that verification did not change the candidate. A timeout,
exception, missing report, fabricated `accepted` field, or failed behavior cannot
pass. The harness executes controlled candidate code with the operator's rights;
it is not a security sandbox. Its timeout bounds the verifier, not arbitrary
children launched by a malicious candidate.

The CSV check uses real loopback HTTP and parses the form. It does not drive a
browser, prove keyboard accessibility, or qualify the Lumi graphical UI.
The recovery fixture exercises a deterministic file-effect boundary, not an OS
crash, killed worker, supervisor, model request, or hosted service. Runtime fault
and browser/package qualification remain separate release gates.

## Run record schema, version 1

`benchmark.validate_run_record(record)` checks the following JSON structure.
Unknown top-level fields, changed input prompts, missing usage categories,
missing/duplicate/renamed acceptance checks, and success without verification are
rejected. This is operator-reported metadata; schema validation is not independent
authentication of a provider, timing observation, or verification receipt.

| Field | Required content |
| --- | --- |
| `schema_version`, `run_id`, `scenario_id` | Version `1`, unique run identifier, declared scenario |
| `strategy` | `single_agent`, `batch_delegation`, or `swarm` |
| `input_revision`, `manifest_sha256`, `prompt` | Exact pinned input identity and full unmodified objective |
| `client_revision` | Actual client commit plus dirty-source identity if applicable |
| `configuration.actors` | Every coordinator/worker role, explicit provider, model, and generation parameters |
| `configuration.tool_settings`, `permission_settings` | Actual enabled tools, scopes, network access and permission mode; never credentials |
| `configuration.worker_slots`, `total_request_concurrency` | Declared worker slots and total concurrent requests including coordinator |
| `configuration.resource_control` | `operational` or `equal_resource` |
| `configuration.limits`, `platform` | Enforced request/time/cost limits or explicit unavailable values; OS/runtime/hardware |
| `usage.known`, `reserved`, `uncertain` | Separate request, input-token, output-token and USD quantities plus source; `null` means unavailable, not zero |
| `usage.provider_failures` | All observed provider failures with available details |
| `interventions` | Timestamp, kind and detail for every operator action, including repairs, stops and restarts; empty list only if none occurred |
| `timing` | Zoned start, finish, verification timestamps and elapsed seconds; verification time may be null for unverified outcomes |
| `outcome` | `accepted`, `failed`, `interrupted`, or `blocked` |
| `verification` | External evaluator receipt with candidate/verifier hashes and every declared check, or null |

Time to a trustworthy result starts at task dispatch and includes interruptions,
operator intervention, rework, integration, and final verification. The fixture
verifier's own duration is not an agent completion time. Preserve failed attempts
and interrupted request uncertainty in the run's supporting trace. Never replace
unknown usage with zero or infer a real model benchmark from reference execution.

Before a pilot, fix exact user-selected models, tools, resource limits, repetitions,
ordering, outcome scoring, and a retention location. Run multiple repetitions for
all three strategies on identical fixture identities and disclose unsupported
batch modes. Keep operational and equal-resource comparisons separate. Establish
the numerical benefit threshold from the baseline before comparative pilot runs.
No such model selection, threshold, paid run, or comparative result is supplied
by this pack.

Run its self-tests with:

```sh
python -m pytest -q tests/test_swarm_benchmark_fixtures.py
```

## Source benchmark runner

`scripts/swarm_benchmark.py` runs all four pinned scenarios. The three repair
scenarios use guarded native writers, isolated Git worktrees, candidate
combination, an external named check, application to the fresh fixture checkout,
and separate acceptance. Investigation uses guarded readers with no write tools:
the host assembles their exact JSON findings into `report.json`, then the external
verifier reproduces the source behavior. Missing, duplicate, foreign or malformed
findings fail before assembly. The host supplies no finding values or corrections.
Passing reproduction receipts are bound to each exact submitted handoff before
acceptance; this is an executed check, not an owner review.

| Mode | Actual execution |
| --- | --- |
| `single` | One guarded native Session with the scenario's complete read or editable scope. This is a controlled baseline, not ordinary unrestricted chat or a CLI agent. |
| `swarm` | Two scoped CSV writers or independent investigation readers. Serial-control and processor repair each retain one deliberately unsplit writer. Assignments are predeclared; there is no model coordinator. |
| `current-batch` | Reported as unsupported. The native `task_batch` tool exists, but this harness has no equivalent bounded accounting and integration adapter. It is never replaced by simulated batch work. |

Operational mode permits the two CSV writers or investigation readers to run
concurrently. Equal-resource mode limits dispatch to one active participant in
every mode. Both use the declared total model request allowance; a two-participant
plan divides it between the participants.
Coordinator calls are absent. Auxiliary native requests, when used, pass through
the same request accounting. Do not combine operational and equal-resource
records into a single comparison.

The processor repair scenario asks a writer to repair `processor.py`. Its external
checks inject the pinned application exception after a durable effect, then make
an explicit second invocation to verify exactly-once resume and corrupt-state
rejection. Both modes receive this same unsplit task. This normal benchmark lane
does not kill a worker, assert model-request uncertainty, require recovery
accounting, or automatically resume interrupted model execution. Runtime worker
fault tests are separate evidence and cannot substitute for these fixture checks.

The current `task_batch` path starts ordinary child Sessions without the swarm
execution guard or a shared main/auxiliary model-request allowance. It can share
the parent's mutable backend, and ordinary writer delegation can fall back to the
shared checkout if worktree creation fails. Thus it does not currently provide
equivalent durable request reservations, scoped effect receipts, or fail-closed
writer isolation for this comparison. A future opt-in guarded child factory and
budget broker need their own integration review. This runner leaves the ordinary
task paths unchanged and reports batch as unsupported rather than renaming a
swarm or simulated adapter.

The CLI requires explicit provider, model, endpoint, request allowance, wall
deadline, modes, scenarios, repetitions, ordering seed and output location.
By default it only writes `protocol.json`; it constructs no backend and makes
no provider call. For example, from the source checkout in PowerShell:

```powershell
python scripts/swarm_benchmark.py --provider ollama --model your-explicit-model --endpoint http://127.0.0.1:11434 --request-limit 12 --wall-seconds 120 --resource-control equal_resource --modes single swarm current-batch --scenarios independent_investigation csv_export serial_control interruption_recovery --repetitions 3 --order-seed 37 --output "$env:TEMP/sonn-benchmark-protocol"
```

`--provider openai-compatible` runs any Chat Completions endpoint as a Lumi
connection (see [Team preview](swarming.md)); the records name that provider
and the endpoint's origin. For NVIDIA NIM, with the key in an environment
variable:

```powershell
python scripts/swarm_benchmark.py --provider openai-compatible --endpoint https://integrate.api.nvidia.com/v1 --api-key-env NVIDIA_API_KEY --model moonshotai/kimi-k3 --request-limit 12 --wall-seconds 180 --resource-control equal_resource --modes single --scenarios independent_investigation csv_export serial_control interruption_recovery --repetitions 3 --order-seed 37 --output "$env:TEMP/lumi-benchmark-nim" --execute-live
```

`--execute-live` invokes the selected provider and can incur its normal charges.
No live run has been performed as part of these self-tests. A future authorized
run must use a new output directory outside the repository; every invocation
writes its exact protocol before dispatch. Hosted providers require an explicit
`--api-key-env VARIABLE_NAME` source. Credentials and full endpoint paths are
excluded from public records; endpoint identity is pinned by a hash. OpenRouter
uses its canonical API endpoint. Model defaults are preserved and recorded;
optional `--thinking-mode` is explicit. There is no enforced dollar budget:
`cost_limit_usd` is null. The enforced request allowance is a request count, not
a dollar or tool-call limit.

The protocol pins fixture inputs and checks, executable source including dirty
Python files, resource settings, seeded ordering and all planned repetitions.
Every case receives a new temporary project and private runtime state. Seed
commits use fixed identity/date metadata so equivalent seeds have equal Git
revisions. Source or endpoint changes after declaration stop dispatch and leave
scheduled omissions visible. No failure is silently retried or replaced. The
wall deadline revokes dispatch and requests Stop; actual cleanup and verification
overrun remains in elapsed time. It is not a claim that an unresponsive process
or arbitrary candidate descendants were contained within a hard wall limit.
An unresolved model request, worker cleanup, named check, Git effect or integration
process stops the study and leaves later cases undispatched. An independently
correct candidate cannot override that uncertainty or count as a completed run.

Acceptance code and reference solutions stay outside model-readable scopes.
Checks execute real candidate behavior, not a model's claimed check result. The
final exported artifact is checked again independently, and aggregation repeats
that check. A structurally valid success record cannot substitute for an artifact
that passes the exact pinned checks. The harness automatically approves only its
fresh fixture checkout after those predeclared checks; it does not imply human
review of model output or apply changes to a user project.

The output contains:

- `protocol.json`: predeclared inputs, settings, schedule and identity hash.
- Each case's `execution.json`, `run.json`, `runtime-report.json` and
  `observations.json`: assigned scopes, seed revision, request allowances,
  provider failures, typed errors, confirmed cleanup and interventions.
- Each case's `worker-events.json`: the workers' own events (tool calls and
  results, refusals, errors), with keys redacted and long text shortened. It can
  contain fixture file contents and model text; it is for local diagnosis.
- Each safely captured `candidate/`: actual resulting files, without Git state.
- `comparison.json` and `comparison.md`: every planned group, failed/interrupted
  outcome, missing/invalid record, correctness result and observed elapsed time.

The temporary runtime paths are retained in case observations for inspection;
they can contain private request transcripts. They are not automatically shared
or deleted, and live unknown workers must be resolved before manual cleanup.
Public summaries keep known, reserved and uncertain usage distinct. Partial
token/USD subtotals retain their per-request missing-data counts in the runtime
report. Missing provider cost stays null. Local runtime JSON is operator-reported
evidence, not an authenticated receipt; a machine owner can edit it. Aggregation
checks configuration consistency and independently verifies retained candidates.

Scripted tests exercise the actual guarded native Sessions, file tools, Git
integration and external checks through the trusted injected-backend test seam.
Managed child fixtures additionally exercise real native process boundaries for
investigation and processor repair; only the trusted scripted backend reads the
reference pack, outside the project and never through model input or granted
file tools. Their records explicitly separate managed processes from the trusted
thread fixture seam. A separate test-only crash fixture terminates an actual
native child after request admission: its unresolved request remains unknown,
the outcome stays interrupted and later scheduled cases are not dispatched. It
does not count as a successful processor-recovery benchmark or authorize replay.
They prove operational two-writer admission, the one-writer serial control,
equal-resource execution, and rejection of false success, altered scope/budget,
missing results, failed providers and exhausted allowances. Their timings are
harness observations, not model performance. No numerical benefit threshold or
reference model baseline is established, so reports deliberately emit no speedup
or benefit claim even when every scripted artifact passes. Live repeated model
results, unsupported batch qualification and broader release gates remain open.

```sh
python -m pytest -q tests/test_swarm_benchmark_fixtures.py tests/test_swarm_benchmark_runner.py
```

## Live runs on NVIDIA NIM

September 27, 2026: `moonshotai/kimi-k3` at `https://integrate.api.nvidia.com/v1`
through `--provider openai-compatible`. Single-agent baseline protocol: 12
requests and a 180-second deadline per case, equal-resource control, order
seed 37, managed worker processes. These are the first live runs of the
runner. No benefit threshold is declared, and none of this measures swarm
benefit.

- **Smoke.** One investigation case was accepted and independently verified:
  2 requests, 4,032 input and 399 output tokens, 20.7 seconds, no provider
  failures. The key appeared in no record.
- **First baseline.** Each of the three cases dispatched failed before reading a
  file. The model opened with `glob("*")`, which searches outside a narrow
  assignment, and the guard ended the worker. Fixed: a call outside the
  assignment is now refused and reported to the model, which then narrowed it.
- **Second baseline, after that fix.** 2 of 4 dispatched cases were accepted
  and independently verified: serial control (76 seconds) and processor repair
  (156 seconds).
  - One investigation failed on its own merits: its findings didn't match the
    strict JSON contract.
  - One processor repair ended when a provider response couldn't be fully
    observed. That request stays uncertain, so the study stopped with 8 cases
    undispatched, as the protocol requires.
  - The ledger now keeps the provider's status code for such failures, and each
    case keeps `worker-events.json`.
- **Third baseline.** Processor repair was accepted and independently verified
  (93 seconds).
  - The investigation failed on the contract again. The findings were right,
    but the cache values were the strings `"False"`/`"True"`, not JSON
    booleans.
  - Serial control was interrupted by `429 Too Many Requests`. That was
    self-inflicted: a live team ran on the same key at the same time. A
    supervised request now waits out a 429, which generated nothing.

### Orchestrated teams, live

September 27, 2026, `engine/swarming/autopilot.py`, headless, with real worker
processes and 2 rounds:

- **Kimi K3, the two-defect investigation.** The orchestrator read both files
  and answered directly (work items `[]`, the answer in its summary). The team
  completed in 24 seconds with 2 requests, and the report was correct. Before
  the fixes this run found, the same answer was rejected: a first plan had to
  propose work, and prose before the JSON wasn't accepted.
- **Kimi K3, a five-module review** (cache.py, urls.py, ledger.py, processor.py,
  backend.py). Both orchestrator turns failed:
  - one on a 429;
  - one ended with `<|close|>!!!!...` instead of its plan;
  - after a retry, three empty replies.

  The loop retried once and then handed the team back, as designed. Such
  replies are now asked again.
- **Nemotron 3 Super (`nvidia/nemotron-3-super-120b-a12b`), the same review.**
  Completed in 303 seconds with 32 requests, all settled.
  - The orchestrator planned five tasks, four ran at once, and one failed task
    was retried once.
  - Round 2 read the findings and wrote the combined report.
  - The findings were uneven: it found cache.py's unit bug but reported no
    defect in urls.py (it missed `doseq`), and ledger.py's findings were
    generic. The workers exchanged no messages; the modules are independent.
- **The same review in the app, with Nemotron 3 Super.** The source app
  ran in a throwaway home with an NVIDIA NIM connection. The team was
  started from the Team panel with real clicks and typing: 2 rounds, 4
  worker slots, 12 requests per worker. It completed in about 8 minutes.
  - The orchestrator planned 6 tasks. After they were accepted it planned 6
    more from the findings, then its closing turn wrote the report that the
    panel's Orchestrator section shows.
  - 12 workers ran, and each was accepted under the grant (`autonomy:<owner>`).
  - 70 model requests, all settled, none uncertain.
  - The round-2 work caught urls.py's list handling.
  - An earlier in-app attempt, before the overload wait and the last-request
    notice, had 3 of 4 workers end uncertain on "Service temporarily
    overloaded". Another attempt had 2 workers use all 8 requests without
    submitting.
