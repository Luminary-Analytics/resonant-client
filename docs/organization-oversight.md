# Organization oversight: activity, messages and security flags

An organization can have Lumi share what its people do with the agent: each
turn's activity, optionally the messages, and security flags. Its Lumi Cloud
shows them to the organization's security staff. This page covers the
desktop app's side. Code: `lumi/oversight.py` (recording, the notice,
sending) and `lumi/security_flags.py` (detection). Lumi Cloud's side is in
its own repository.

Status: first pass, source only, not released.

## Off unless the organization turns it on

Without an organization policy that asks for it, Lumi shares nothing of the
kind: prompts, replies and code go only to the model providers people
choose, and Lumi Cloud check-ins stay counts ([Lumi Cloud](lumi-cloud.md)).

Only an [organization policy](enterprise-policy.md) can turn it on, with an
`oversight` section:

```json
"oversight": {
  "activity": true,
  "messages": "redacted",
  "security_flags": true,
  "retention_days": 90,
  "notice": "Questions: security@example.com",
  "project_paths": false
}
```

| Key | Meaning |
| --- | --- |
| `activity` | Share each turn's metadata (below). Default `false`. |
| `messages` | `off` (default), `redacted` or `full`: share the person's message and Lumi's final reply with each turn. Needs `activity: true`. |
| `security_flags` | Share [security flags](#security-flags). Default `false`. Works without `activity`. |
| `retention_days` | How long Lumi Cloud keeps it: 1 to 3650 days, default 90. |
| `notice` | Up to 500 characters in the organization's own words, shown with Lumi's description, never instead of it. |
| `project_paths` | `true` shares full project paths instead of folder names. |

Mistakes make the policy invalid, like any other section: a value of the
wrong type, `messages` without `activity`, or a key Lumi doesn't know. That
last one is deliberate: this section decides what is collected about people,
so Lumi refuses a key it can't honor rather than guessing. An invalid policy
stops model requests until it's fixed ([When something is
wrong](enterprise-policy.md#when-something-is-wrong)).

In Lumi Cloud, administrators set this on the Policy page and publish it;
the page warns that members will be told.

## People always know

- **A notice beside the message box** names the organization and what it
  receives, for example: *"Acme receives your sessions and what they did,
  your messages and Lumi's replies (shortened, without code or secrets) and
  security flags from Lumi on this computer."* The organization's own
  `notice` follows. It has no close button. **What's shared** opens the
  details.
- **Settings > Privacy & security > Organization oversight** lists exactly
  what is shared and what never is, how long it's kept, what is waiting to
  be sent, what was sent, and anything dropped, refused or deleted. It also
  lists the person's own security flags.
- **About Lumi** says it too, under *What leaves this computer*.
- **Nothing is recorded before the notice has been shown.** The page tells
  Lumi which policy's notice it showed (a fingerprint of the organization
  and the `oversight` section); a turn is recorded only if that matches the
  policy in force. A policy that changes what is collected needs its notice
  shown again first.
- `lumi run`, the terminal UI and the chat gateway print the notice when
  they start. At an interactive terminal that counts as shown. An
  unattended run (a scheduled task, CI, a model comparison) records only
  after the person has seen the notice in the app or a terminal.
- The chat gateway's status message includes the notice for people in the
  chat.

Lumi never records keystrokes, screenshots, the clipboard or anything done
outside its own turns.

## What a turn record holds

Recorded for every turn that goes through `Session.run`: the app, `lumi run`,
scheduled tasks, the terminal UI, the chat gateway, tasks from chat, plans
and missions. A delegated worker's calls are part of its parent's turn.

With `activity`:

- the session's id, its title (secrets removed), the project folder's name
  (its path with `project_paths`) and where it ran (`app`, `lumi run`,
  `terminal`, `chat gateway`, `task from chat`);
- the turn's number, start and end, provider, model, permission mode and
  outcome (`completed`, `error`, `cancelled`, `stopped`);
- requests, tokens and cost (`null` when the model has no price);
- the tools called, in order, and whether each ran (`ok`), failed
  (`error`), was refused (`denied`) or didn't run; how many files changed.

With `messages` at `redacted`:

- the person's message and Lumi's final reply, with code blocks replaced by
  `[code block, N lines]`, email addresses by `[email]`, secrets removed, and
  cut at 2,000 characters;
- for each tool, the arguments that say what it did: `command`, `pattern`,
  `glob`, `query`, `url`, `agent_type`, and paths relative to the project.
  Text is cut at 300 characters. A file that is excluded appears as
  `[excluded file]`, one outside the project as `[outside the project]`.

At `full`, the message and reply keep code and addresses, up to 20,000
characters, and arguments up to 2,000. **Secrets are removed at every level**
with Lumi's secret scan (`lumi/secret_scan.py`): saved keys and well-known
credential formats, whether or not the person turned the scan on. The
record counts what was removed, never the values.

Never shared, at any level: file contents (a `file_write`'s content is
never an argument that's sent), tool output such as command output and web
pages, images, and the names of excluded files.

## Security flags

Detected on the computer, deterministically, never by a model
(`lumi/security_flags.py`):

| Kind | Severity | When |
| --- | --- | --- |
| `destructive_command` | high | A command the [guardrails](shell-sandbox.md) never run, such as deleting the home folder |
| `dangerous_command` | medium | A command the permission mode refuses: a download piped to a shell, a recursive delete |
| `policy_denied` | medium or low | The organization's shell rules refused a call (medium); the project's `lumi-policy.json`, a hook or the review gate did (low) |
| `excluded_file` | medium | The agent tried to read, write or search a file Lumi never reads. The flag names the rule, never the file |
| `outside_project` | low | A path outside the project; the path isn't named |
| `approval_denied` | low or medium | The person or their permission hook said no (low); a [second person's approval](second-approval.md) was declined or never came (medium) |
| `secret_redacted` | medium or high | Secrets were removed before a model request or from what was shared; high for private keys and cloud secrets |
| `prompt_injection` | medium or low | What a tool returned tries to instruct the agent: "ignore previous instructions", "you are now…", fake system markers, asking to hide things from the person or to send credentials somewhere, instructions in HTML comments, hidden Unicode tag or direction characters. Medium from web pages, MCP servers, issue trackers and code hosts; low from local files and commands |

The engine marks each refused call with the layer that refused it
(`denied_by` on the `tool.result` event). A flag carries its kind, severity,
rule, tool, session and turn. It carries a short excerpt (one line, secrets
removed, hidden characters shown as `<U+…>`) only when the organization also
receives messages. The person sees their own flags, with excerpts, in
Settings.

## Where it goes, and when it doesn't

Records go only to the Lumi Cloud of the organization whose policy asks for
them: the policy must come from that Lumi Cloud (signed and verified for
this enrolled computer), or be a machine policy that enrolled the computer
there. Otherwise Settings says why nothing is collected.

1. After each turn, Lumi appends the turn and its flags to
   `~/.lumi/oversight/queue.jsonl`. The queue holds at most 5,000 records
   (20 MB); past that the oldest are dropped and counted.
2. A background thread in the app sends them in batches of up to 100 to
   `POST /api/v1/oversight/events`, signed in as the device
   (`lumi/cloud.py`), never on the UI's thread. Failures are retried after
   30 seconds, then up to an hour apart. `lumi run` tries for up to 10
   seconds before it exits; the app sends the rest.
3. Before every batch Lumi checks the policy again. If the organization
   stopped asking, Lumi Cloud answers `oversight_off`, or the computer left
   the organization, queued records are **deleted, not sent**.
4. A batch Lumi Cloud refuses as invalid isn't sent again; it's counted.

Every record dropped, refused or deleted, and the last error, show in
Settings. The audit log records when the notice was shown
(`oversight.notice_shown`) and when records were deleted
(`oversight.discarded`).

Lumi Cloud deletes what it keeps after `retention_days`. Viewing messages
there needs a security admin or auditor role, or an owner's grant, and every
view is recorded in the organization's activity log.

Files: `~/.lumi/oversight/` holds `queue.jsonl`, `state.json` (the counts),
`flags.jsonl` (the person's last 500 flags), `notice.json` (the notice last
shown) and `sessions.json` (turn numbers).

## Known limits

- A worker's own secret redactions and model usage aren't in its parent's
  record: the parent sees the worker's tool calls only.
- Codex and Claude Code run their own tool loops: their turns are recorded,
  but their tools' refusals aren't seen by Lumi.
- An unattended run on a computer where nobody has seen the notice (a CI
  server with a service account) records nothing.
