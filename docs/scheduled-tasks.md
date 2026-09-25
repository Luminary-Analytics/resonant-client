# Scheduled tasks

A scheduled task is a saved prompt that Lumi runs at set times, for example a
nightly dependency check or a Monday summary of last week's commits. It runs
even when the desktop app is closed: the operating system's scheduler starts
it.

Add one in **Settings > Scheduled tasks**, or with `lumi schedule add`.

## What a schedule is

| Field | Meaning |
| --- | --- |
| Name | How the schedule is listed. |
| Task | The prompt, up to 20,000 characters. |
| Project folder | The folder the task works in. |
| Time and days | This computer's local time. No days ticked means every day. |
| Stop after | A run stops after this many minutes (1 to 720, default 60). |
| What it may do | The permission mode: **Read only (ask)**, **Edit files (auto-edit)** or **Everything (bypass)**. |
| Model | A model the app knows, or your default model. |

Each run is an unattended [`lumi run`](headless.md), so everything that
applies to one applies here:

- Nobody is asked anything. In **Read only**, a tool call that would need
  approval is refused; choose **Edit files** or **Everything** only for tasks
  you'd let run without watching.
- Your organization's policy, budgets, file exclusions, the audit log, the
  command guardrails and the [shell sandbox](shell-sandbox.md) apply.
- Your own [hooks](headless.md#hooks) in `settings.json` run, as in the app.
  A guard hook that refuses a call, or can't run or runs out of time, stops
  that call, and the result counts it as a refused call.
- A repository's own instructions, notes and policy allow rules apply only if
  the project is trusted in the app (**Settings > Project trust**). A schedule
  never trusts a project itself. In **Edit files**, those allow rules run the
  commands they match. Your own `permission_request` hook can allow others.
  Other commands are refused.
- Model requests are counted in **Usage & cost** like any other.

A schedule never runs twice at once. If a run is still going when the next
one is due, or when you click **Run now**, the new one doesn't start.

## Where it runs

Saving a schedule registers it with the operating system, for the signed-in
user only:

- **Windows:** a Task Scheduler task in the `Lumi` folder, named by the
  schedule's id. It runs when you're signed in.
- **macOS:** a LaunchAgent, `~/Library/LaunchAgents/ai.lumi.schedule.<id>.plist`.
- **Linux:** a line in your crontab, ending `# lumi-schedule:<id>`.

The entry starts `lumi schedule run <id>` with the installed Lumi (running
from source, `python -m lumi`). Pausing a schedule removes the entry; resuming
adds it again. Removing a schedule removes the entry and its kept results.

Schedules are kept in `~/.lumi/schedules.json`.

## Results

Each run keeps its result in `~/.lumi/schedules/<id>/`, the last 30 of them:
when it started and finished, its exit code and status (the same statuses as
[`lumi run`](headless.md#the-result)), its answer, the files it changed, any
error and its cost. The full `lumi run` summary is kept with it.

Settings shows each schedule's last run and its answer. **Run now** starts a
run straight away, in the background, the same way the scheduler would; it
keeps going if you close Lumi, and the page updates when it finishes.

## From the command line

```sh
lumi schedule add "Summarize yesterday's commits." --name "Digest" --project . --at 08:00 --days mon,tue,wed,thu,fri
lumi schedule list
lumi schedule run <id>
lumi schedule disable <id>
lumi schedule enable <id>
lumi schedule remove <id>
```

`add` also takes `--mode` (`ask`, `auto-edit` or `bypass`; default `ask`),
`--provider`, `--model` and `--max-minutes`. `run` exits with the run's
`lumi run` exit code, or 2 if the schedule is unknown or already running.

## For administrators

`security.scheduled_tasks` turns the feature off. People can set it in
**Settings > Privacy & security**; an [organization policy](enterprise-policy.md)
can lock it:

```json
{
  "settings": {"security.scheduled_tasks": false}
}
```

When it's off, nothing can be added, resumed or run now, and a run the
operating system still starts is refused and recorded as refused in the
schedule's results. Existing schedules can still be paused and removed.

A policy's `permissions.allowed_modes` applies too: a schedule can't be saved
with a mode the policy doesn't allow, and `lumi run` refuses one at run time.

## Not covered yet

- Runs don't wake a sleeping computer, and a Windows schedule doesn't run
  while you're signed out.
- A run missed while the computer was off isn't made up later. One missed
  while it slept is skipped on Windows and Linux; macOS runs it once when
  the computer wakes.
- There's no notification when a run finishes or fails; check Settings or
  the results folder.
- Schedules aren't shared with a team or managed from Lumi Cloud.
