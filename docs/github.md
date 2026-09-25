# GitHub pull requests

The agent can work with the pull request for the branch you're on:

- read its reviews, comments and checks;
- read why a check failed;
- open the pull request, reply to reviews, and update its title, description
  or draft state.

The code is `lumi/engine/github_tools.py`. The tools are loaded on demand
(through `search_tools`), so they cost nothing until a task needs them.

## Setting up

The repository is the project's `origin` remote. It can be on github.com or
on GitHub Enterprise Server, recognized by a host name containing "github",
or by the host in `GITHUB_SERVER_URL`. Lumi uses Enterprise Server's API at
`https://<host>/api/v3`; `GITHUB_API_URL` overrides it.

The token is **Settings > Connections > API keys > GitHub token**, kept in the
OS credential store. Otherwise `GITHUB_TOKEN` or `GH_TOKEN` is used; GitHub
Actions provides `GITHUB_TOKEN`. A fine-grained token needs, for the
repository:

- **Pull requests: read and write**;
- **Contents: read and write** (to push the branch);
- **Checks: read** and **Actions: read** (for check results and job logs).

The token goes only into request headers. Nothing a tool returns contains it,
and the secret scan removes its value from anything sent to a model.

## The tools

| Tool | What it does | Asks first in Auto accept edits |
|---|---|---|
| `github_pr_view` | The branch's open pull request (or `number`): state, draft, review decisions, review comments with ids, file and line (outdated ones marked), the conversation, and every check with its job id | No: it only reads |
| `github_check_log` | The end of a GitHub Actions job's log (`lines`, default 150), plus earlier lines that mention errors or failures | No: it only reads |
| `github_pr_create` | Pushes the current branch (never forced) and opens a pull request into `base` (default: the repository's default branch); `draft` optional | Yes |
| `github_pr_comment` | Comments on the pull request, or replies in a review thread with `reply_to` (a review comment id) | Yes |
| `github_pr_update` | Changes the title or description, or marks a draft ready for review | Yes |

Under **Ask permissions**, the changing tools ask as well. Under
**Full-auto** (`bypass`), they run without asking. An organization policy's shell rules
can deny them like any tool, for example
`{"tool_pattern": "github_pr_*", "action": "deny"}`.

## From CI

Fix the failing checks of a pull request and reply to its reviews. Run this
only for branches in your own repository, never for pull requests from
forks: the job holds a write token, and `--trust-project` makes the agent
follow the repository's instructions.

```yaml
on:
  workflow_dispatch:
  pull_request_review:
    types: [submitted]

jobs:
  address-review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
      checks: read
      actions: read
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.ref }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "lumi @ git+https://github.com/Luminary-Analytics/resonant-client@main"
      - env:
          GITHUB_TOKEN: ${{ github.token }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          LUMI_KEYCHAIN: "off"
        run: |
          git config user.name "lumi[bot]"
          git config user.email "lumi-bot@users.noreply.github.com"
          lumi run "Read this pull request's reviews and failing checks. Fix what they ask, run the tests, commit, push, and reply to each review comment you addressed." \
            --provider anthropic --model claude-sonnet-5 --mode bypass --trust-project --timeout 1200
```

Pushes made with `GITHUB_TOKEN` don't start other workflows. Use a GitHub
App or personal token if the fix should run the checks again.

## Not covered yet

- Resolving review threads, requesting reviewers and merging aren't tools.
  The agent can't merge.
- Only GitHub Actions job logs can be read; other check providers show their
  status and link.
- Lists are limited to the first 100 review comments, comments and checks.
