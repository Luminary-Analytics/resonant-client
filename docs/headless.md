# Running Lumi without a UI

`lumi run` runs one task and reports the result, for servers, containers and CI
jobs. It uses the same engine as the desktop app. These all apply:

- organization policy;
- [budgets](usage-and-costs.md#budgets);
- [usage records](usage-and-costs.md);
- the [audit log](audit-log.md);
- file exclusions and the secret scan;
- the path checks, command guardrails and, when it's on, the
  [shell sandbox](shell-sandbox.md).

The code is `lumi/headless.py`.

```bash
lumi run "Fix the failing test in tests/test_api.py" --provider anthropic --model claude-sonnet-5 --mode bypass
```

## Options

| Option | Meaning |
|---|---|
| `PROMPT` | The task. `-` reads it from stdin; `--prompt-file FILE` reads (more of) it from a file |
| `--handoff FILE_OR_ID` | Continue work handed off in the app ([hand-offs](hand-offs.md)): a hand-off file such as `.lumi/handoffs/<name>.json` (relative to the current folder or the project), or the id of one you picked up. The task defaults to "Continue the work in this hand-off." |
| `--project DIR` | The project folder (default: the current folder) |
| `--provider`, `--model` | `anthropic`, `openai`, `openrouter`, `sonn`, `kimi`, `exo`, `ollama`, `codex`, `claude-code` or a connection (`conn-<id>`). `LUMI_PROVIDER` and `LUMI_MODEL` work too; otherwise the desktop defaults apply |
| `--mode` | What the agent may do without asking: `ask` (read only), `auto-edit` (the default: edit files; other actions refused unless a trusted repository's `lumi-policy.json` allows them) or `bypass` (everything) |
| `--trust-project` | Apply the repository's instructions (`AGENTS.md` and others), notes and `lumi-policy.json` allow rules for this run. In `auto-edit`, the allow rules run the commands they match ([project trust](desktop-workflow.md#project-trust-and-lumi-policyjson)) |
| `--policy-digest SHA256` | With `--trust-project`, apply the allow rules only if `lumi-policy.json` has this SHA-256 (model comparisons pass the version trusted in the app) |
| `--max-requests N` | Stop after N model requests |
| `--timeout SECONDS` | Stop after this long |
| `--output` | `json` (the default), `text` (the answer as it streams; a summary on stderr) or `jsonl` (every engine event, then the result) |

Nobody is asked anything during a run:

- A tool call the mode doesn't allow is refused.
- A budget that needs approval stops the run.
- The agent's own questions get no answer.

Keys come from Settings or the environment: `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `SONN_API_KEY`, `MOONSHOT_API_KEY` or
`EXO_API_KEY`. A connection's key must be saved in Settings.

## The result

```json
{
  "version": 1,
  "run_id": "5f0c…",
  "status": "completed",
  "outcome": "changed_verified",
  "text": "Fixed the off-by-one in paginate() and ran the tests.",
  "errors": [],
  "provider": "anthropic", "model": "claude-sonnet-5", "mode": "bypass",
  "project": "/work",
  "changed_files": ["src/pagination.py"],
  "checks": [{"status": "passed", "…": "…"}],
  "tool_calls": 7, "denied_calls": 0, "model_requests": 5,
  "usage": {"calls": 5, "input_tokens": 48210, "output_tokens": 2310, "cost_usd": 0.1195, "unpriced_calls": 0},
  "elapsed": 41.2
}
```

`outcome` is the engine's classification: `answered`, `changed_verified`,
`changed_unverified`, `no_changes_needed`, `needs_input`, `incomplete` or
`failed`. `status` and the exit code say what a job should do:

| Exit | `status` | Meaning |
|---|---|---|
| 0 | `completed` | The task was answered or done. Check `outcome` and `checks` if you need verified changes |
| 1 | `failed` | The run failed: a provider error, or no usable result |
| 2 | — | The command or its setup is wrong: no provider, model or key, a mode or model the policy doesn't allow (the message is on stderr) |
| 3 | `needs_attention`, `budget_exceeded`, `request_limit`, `timeout` | It stopped for a person: it needs input, is incomplete, was refused an action it tried (`denied_calls`), or hit a limit |

The usage records and audit records of a run carry the conversation id
`headless:<run_id>`.

## In GitHub Actions

Fix a failing build on a pull request. This runs in a disposable runner, so
`bypass` is reasonable. Keep it to repositories whose instructions you trust,
since `--trust-project` makes the agent follow them. For less than everything,
use `--mode auto-edit` with `allow` rules in the repository's
`lumi-policy.json` for the commands the task needs, such as the test command.

```yaml
jobs:
  fix:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "lumi @ git+https://github.com/Luminary-Analytics/resonant-client@main"
      - name: Ask Lumi to fix the tests
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          LUMI_KEYCHAIN: "off"
        run: |
          lumi run "The test suite fails. Find the cause, fix it and run the tests." \
            --provider anthropic --model claude-sonnet-5 --mode bypass --trust-project \
            --timeout 900 > lumi-result.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: lumi-result
          path: lumi-result.json
```

To have the agent read a pull request's reviews and failing checks, and
reply, give the job a GitHub token; see [GitHub pull requests](github.md#from-ci).

Review a diff without changing anything:

```bash
git diff origin/main...HEAD | lumi run - --prompt-file .github/review-prompt.md --mode ask --output text
```

## In a container

`packaging/docker/Dockerfile` builds an image with `lumi` as its entry point.
It includes git and ripgrep, and has no desktop app, browser or computer use.

```bash
docker build -f packaging/docker/Dockerfile -t lumi .
docker run --rm -v "$PWD:/work" -e ANTHROPIC_API_KEY lumi \
  run "Update the changelog for the last release" --provider anthropic --model claude-sonnet-5
```

The image runs as user `lumi` (uid 1000) with `LUMI_KEYCHAIN=off`. Usage
records and the audit log are in `/home/lumi/.lumi`; mount a volume there to
keep them. An organization policy file can be mounted and named with
`LUMI_POLICY_FILE`.

To run a task at set times on this computer, even with the app closed, use a
[scheduled task](scheduled-tasks.md): each run is a `lumi run`.

## Not covered yet

- MCP servers, capability packs, hooks and the codebase index aren't
  connected in a headless run.
- The conversation isn't saved for the desktop app to open. The usage records
  and the audit log keep what happened.
- The image isn't published to a registry.
