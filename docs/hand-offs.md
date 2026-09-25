# Handing off work

Use a hand-off to pass work you started in Lumi to a teammate or to a CI
run, so they can continue it. Right-click a conversation in the sidebar (or
use its **⋯** button) and choose **Hand off…**.

## What goes with it

- **The conversation**, as a [shared copy](lumi-cloud.md#sharing-a-conversation)
  has it: your messages, Lumi's replies, and a line for each action ("Read
  src/auth.py", "Ran `pytest -q`"), marked when it failed.
  - It never holds what tools returned: file contents, command output and
    pages stay on your computer.
  - Saved keys, and anything that looks like a token or password, are removed
    first. The project appears only by its folder's name.
- **Your note**, under **What's left**: what should happen next, and anything
  they need to know. Secrets are removed from it too.
- **Where the work is**: the repository's address, the branch and the commit.
  - A user name, password or token in the address is removed.
  - The dialog tells you when changes aren't committed or commits aren't
    pushed. The next person only gets what's in the repository, so commit and
    push first.

## To a teammate

Sign in to your organization's Lumi Cloud (**Settings > Lumi account**), then
choose **A teammate** and pick them from the list of your organization's
members.

- Lumi Cloud keeps the hand-off for them and emails them.
- Their Lumi shows **Hand-offs for you** under **New session**. Lumi checks
  for new ones when it starts and each time you open that list.
- Only the two of you can read it, including in Lumi Cloud under
  **Hand-offs**, where you can withdraw it until they pick it up. They can
  dismiss it instead.

To continue it:

1. Open **Hand-offs for you**, and choose the folder to continue in under
   **Continue in**.
   - Lumi suggests a recent project that is a clone of the same repository.
   - Lumi says whether that folder is on the handed-off branch and commit, and
     what to do if it isn't. It never switches branches, fetches or pulls for
     you.
2. Choose **Continue**. Lumi starts a new conversation in that folder, with a
   first message such as `@handoff:hof_… Continue the work Ada handed off:
   API rename.` for you to review before sending.

`@handoff:<id>` attaches the hand-off as context: framed as information about
the work so far, not as instructions. It stays attached for the rest of that
conversation, even after Lumi restarts. Lumi keeps picked-up hand-offs in
its state folder (`handoffs/<id>.json`), so the mention works offline.

## To a CI run

Choose **A CI run**. Lumi saves the hand-off in the project as
`.lumi/handoffs/<name>.json` and shows the command to run:

```bash
lumi run --handoff .lumi/handoffs/rate-limit-tweak-20260925-1509.json
```

Commit the file with your branch. The CI job checks out the branch and runs
[`lumi run`](headless.md) with `--handoff`. The task defaults to continuing
the work, and the hand-off comes with it. A file for CI names you but doesn't
include your email address. `@handoff:.lumi/handoffs/<name>.json` attaches a
hand-off file in the app too.

## Limits

- A hand-off is a snapshot. To hand off newer work, hand it off again.
- The next person's conversation starts fresh: they get the hand-off as
  context, not your conversation's history, tool results or checkpoints.
- Handing off to a teammate needs your organization's Lumi Cloud; a CI
  hand-off doesn't.
