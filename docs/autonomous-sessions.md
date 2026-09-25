# Autonomous sessions (experimental)

An autonomous session works through a feature unattended: Lumi first agrees
a spec with you, with acceptance criteria it can check, then plans, builds and
checks in iterations until every criterion passes, the budget runs out, or
you stop it.

Autonomous sessions are experimental and off by default. Turn them on in
**Settings > General > Autonomous sessions (experimental)**; the **∞** button
then appears in the message box.

## Starting one

1. Click **∞**, describe what you want built and choose the folder. Keep
   **Run autonomously** ticked.
2. Lumi interviews you (10 to 25 questions) and writes a **Final spec**. Its
   acceptance criteria are typed, so Lumi can check them:
   `[bash]` a command's exit code or output, `[chrome]` a check in the
   browser, `[vision]` a look at a screenshot, `[manual]` something only
   you can judge.
3. Under the spec, choose:
   - **Time budget**: 1 to 48 hours, or **Full auto** for none.
   - **If it needs a decision and you are away**: wait for you, or decide
     alone after 15 minutes to 4 hours with the option Lumi recommended.
     A decision made that way is recorded as automatic.
   - **Spending limit**: none, $5, $25 or $100.
4. Click **Build autonomously**.

## How it runs

The session keeps a roadmap in `.lumi/roadmap-<id>.md` in the project: the
spec, the items to build, the criteria and a log of each iteration. Each
iteration builds one item as its own planned task and records the commit it
made. Every three iterations, and when the items run out, a **reflect** pass
checks the criteria and adds or blocks items.

It stops when:

| Reason | What happened |
| --- | --- |
| complete | Every acceptance criterion passed. |
| stopped by you | You clicked **Stop** (in-flight tool calls are cancelled). |
| paused by you | You clicked **Pause** (the current iteration finishes first). |
| time budget used up | The time budget elapsed. |
| spending limit reached | The session's model requests cost the limit. |
| iteration cap reached | 100 iterations, a backstop. |
| blocked | The reflect pass said "blocked" three times in a row. |
| sub-tasks kept failing | Two iterations failed in a row. |
| stuck, needs you | The items ran out and the criteria still don't pass. |
| not allowed by policy | Your organization's policy stopped allowing Full-auto (below). |

## Your files, trust and your organization's policy

Each planned task follows the rules a chat in the project follows: files you
exclude (**Settings > Privacy & security**, the project's `.lumiignore` or
your organization's policy) are never read, and the project's instructions,
notes and language servers are used only once you trust the project.

Autonomous sessions run in Full-auto, since nobody is there to approve each
step, and the session runs its own `[bash]` checks. If your organization's
policy doesn't allow Full-auto, **Build autonomously** and **Resume** say so
and start nothing; the spec card stays as it was. If such a policy arrives
while a session runs, the step already running finishes, no further step
starts, and the session stops before its next iteration or reflect pass.

## The spending limit

The limit counts every priced model request while the session runs:
iterations and reflect passes alike. It's checked before each iteration and
every 30 seconds while one runs. When it's reached, the running iteration is
cancelled, so a session can pass the limit by what about 30 seconds of
requests cost. Requests to models without a known price (see
[usage and costs](usage-and-costs.md)) aren't counted.

The roadmap records the limit and the spending so far. A session resumed
after Lumi restarts counts on from there. Your organization's budgets apply
to every request either way.

## Pausing, stopping and resuming

**Pause** and **Stop** are on the session's badge. A session that was
interrupted, for example by closing Lumi, is offered for resuming when you
reopen the project. The time budget starts again on resume; the spending
limit doesn't.

## Not covered yet

- Autonomous sessions are experimental and hidden by default.
- A resumed session's time budget starts over.
- There's no notification outside Lumi when a session finishes or stops.
- Only these preset spending limits can be chosen in the app. Editing
  `**Spending limit:**` in the roadmap takes effect on resume.
