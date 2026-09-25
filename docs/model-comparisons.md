# Comparing models on your own tasks

Before switching yourself or your team to another model, run the same tasks
with each candidate and compare which one passes your checks, what it costs
and how long it takes.

Open **Settings > Model evaluations > Compare models on your tasks**.

## A comparison

| Field | Meaning |
| --- | --- |
| Name | How the comparison is listed. |
| Project | A git repository with at least one commit. |
| Models | Two to six models the app knows. |
| Tasks | Up to 20. Each is a prompt and a one-line check command, such as a test, that passes with exit code 0. |
| What it may do | The permission mode: **Read only (ask)**, **Edit files (auto-edit)** or **Everything (bypass)**. A task that has to run commands, such as tests, needs **Everything**, or **Edit files** in a trusted project whose `lumi-policy.json` allows those commands ([project trust](desktop-workflow.md#project-trust-and-lumi-policyjson)) or with a `permission_request` [hook](headless.md#hooks) of yours that allows them. |
| Stop each run after | 1 to 60 minutes (default 10). |

Click **Create comparison**, then **Run**. One comparison runs at a time;
**Stop** ends the current run and keeps the results so far.

## How each run works

Every task runs once per model:

1. Lumi makes a detached git worktree of the project's last commit, under
   Lumi's own state folder. Runs never see your uncommitted work or each
   other's changes, and your checkout isn't touched.
2. The task runs there as an unattended [`lumi run`](headless.md) with that
   model. Nobody is asked anything, your organization's policy and budgets
   apply, and its requests count in **Usage & cost**. Repository
   instructions apply only if the project is trusted in the app. So do the
   `allow` rules in its `lumi-policy.json`, and only if the last commit's copy
   is the version you trusted.

   Your own [hooks](headless.md#hooks) run too, in the worktree. Comparisons
   try models you don't rely on yet, unattended and often with **Everything**,
   so a guard of yours matters most there. The results also show how each
   model does with your usual setup. Hooks that act on every session, such as
   a notification when one ends, act once per run.
3. The task's check runs in the worktree. Exit code 0 passes. The check is
   your command: it passes the command guardrails and runs in the
   [shell sandbox](shell-sandbox.md) when that's on. It has 10 minutes.
4. Lumi keeps the run's diff and removes the worktree.

## Results

For each model, the page shows how many tasks passed, the total cost of
priced requests and the median run time. Open a task to see each model's
status, time, cost, how many files it changed, and the end of the check's
output. Diffs are kept in `~/.lumi/model_evals/<id>/`, up to 200 KB each.
Lumi keeps the 30 newest comparisons.

A comparison that was running when Lumi closed is shown as interrupted; run
it again.

## Not covered yet

- Runs happen one after another, not in parallel.
- Each task runs once per model, so one lucky or unlucky run counts fully.
  Repeated runs are not available yet.
- Comparisons aren't shared with a team or run from Lumi Cloud.
- Codex and Claude Code models can be chosen, and they run their own tool
  loops, as in [`lumi run`](headless.md).
