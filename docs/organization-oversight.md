# Organization oversight: activity, messages and security flags

An organization can have Lumi share what its people do with the agent: each
turn's activity, optionally the messages, and security flags. Its Lumi Cloud
shows them to the organization's security staff. This page covers the
desktop app's side. Code: `lumi/oversight.py` (recording, the notice,
sending), `lumi/security_flags.py` (detection) and
`secret_scan.redact_for_sharing` (what is removed before anything leaves).
Lumi Cloud's side is in its own repository.

Status: first pass, source only, not released.

## Off unless the organization turns it on

Without an organization policy that asks for it, Lumi shares nothing of the
kind: prompts, replies and code go only to the model providers people
choose, and Lumi Cloud check-ins stay counts ([Lumi Cloud](lumi-cloud.md)).

Only an [organization policy](enterprise-policy.md) can turn it on, with an
`oversight` section:

```json
"oversight": {
  "version": 1,
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
| `version` | Which keys the section has: `1` (the default). |
| `activity` | Share each turn's metadata (below). Default `false`. |
| `messages` | `off` (default), `redacted` or `full`: share the person's message, Lumi's final reply and the session's title with each turn. Needs `activity: true`. |
| `security_flags` | Share [security flags](#security-flags). Default `false`. Works without `activity`. |
| `retention_days` | How long Lumi Cloud keeps it, counted from when each turn ran: 1 to 3650 days, default 90. |
| `notice` | Up to 500 characters in the organization's own words, shown with Lumi's description, never instead of it. |
| `project_paths` | `true` shares full project paths instead of folder names. |

A `version` or a key this Lumi doesn't know turns oversight **off**, and
Settings > Privacy & security says why (in Organization policy and
Organization oversight); the rest of the policy stays in force. This section
decides what is collected about people, so Lumi collects nothing rather than
guess, and a newer Lumi Cloud never stops model requests on an older Lumi.
Other mistakes (a value of the wrong type, `messages` without `activity`)
make the policy invalid, like a mistake in any section, and an invalid
policy stops model requests until it's fixed ([When something is
wrong](enterprise-policy.md#when-something-is-wrong)).

In Lumi Cloud, administrators set this on the Policy page and publish it;
the page asks them to confirm members will be told whenever a version
collects more than what the members it reaches have now.

## People always know

- **A notice beside the message box** names the organization and what it
  receives, for example: *"Acme receives your sessions and what they did,
  your messages, Lumi's replies and your sessions' titles (shortened,
  without code or secrets) and security flags from Lumi on this computer."*
  The organization's own `notice` follows. It has no close button. **What's
  shared** opens the details.
- **Nothing is recorded until the person confirms the notice** with its
  **I've read this** button. Only that click tells Lumi which policy's
  notice was read (a fingerprint of the organization, this computer's
  enrollment and the `oversight` section); a turn is recorded only if it
  matches the policy in force. A notice that arrives while the window is
  minimized or in the background is never confirmed for the person. A
  policy that collects more, another enrollment, or another organization
  needs the notice confirmed again, and leaving the organization or signing
  out of Lumi Cloud forgets the confirmation. A notice that says nothing is
  collected (the computer isn't enrolled, or the policy comes from
  elsewhere) can't be confirmed.
- **Settings > Privacy & security > Organization oversight** lists exactly
  what is shared and what never is, how long it's kept, what is waiting to
  be sent, what was sent, and anything dropped, refused, expired or
  deleted. It also lists the person's own security flags.
- **About Lumi** says it too, under *What leaves this computer*.
- `lumi run` and the terminal UI print the notice when they start. At an
  interactive terminal that counts as confirmed. An unattended run (a
  scheduled task, CI, a model comparison) records only after the person has
  confirmed the notice in the app or a terminal.
- **The chat gateway** tells each chat in the chat, once per policy, before
  that chat's first turn is recorded. A chat's turns are recorded only after
  its notice was sent, whoever confirmed the notice on the computer; if the
  notice can't be delivered, the chat's turns aren't recorded. The gateway's
  status message includes the notice too.

Lumi never records keystrokes, screenshots, the clipboard or anything done
outside its own turns.

## What a turn record holds

Recorded for every turn that goes through `Session.run`: the app, `lumi run`,
scheduled tasks, the terminal UI, the chat gateway, tasks from chat, plans
and missions. A delegated worker's calls are part of its parent's turn.

With `activity`:

- the session's id, the project folder's name (its path with
  `project_paths`) and where it ran (`app`, `lumi run`, `terminal`,
  `chat gateway`, `task from chat`);
- the turn's number, start and end, provider, model, permission mode and
  outcome (`completed`, `error`, `cancelled`, `stopped`);
- requests, tokens and cost (`null` when the model has no price);
- the tools called, in order, and whether each ran (`ok`), failed
  (`error`), was refused (`denied`) or didn't run; how many files changed.

With `messages` at `redacted`:

- the person's message and Lumi's final reply, with code blocks replaced by
  `[code block, N lines]`, email addresses by `[email]`, secrets removed, and
  cut at 2,000 characters;
- the session's title (an automatic title is the gist of the first
  message, so it is content, not activity), without email addresses or
  secrets;
- for each tool, the arguments that say what it did: `command`, `pattern`,
  `glob`, `query`, `url` (without its query string), `agent_type`, and
  paths relative to the project. Text is cut at 300 characters. A file that
  is excluded appears as `[excluded file]`, one outside the project as
  `[outside the project]`.

At `full`, the message and reply keep code and addresses, up to 20,000
characters, web addresses keep their query strings (secret parameters
removed), and arguments go up to 2,000 characters.

**Secrets are removed at every level, before anything is cut**
(`secret_scan.redact_for_sharing`), whether or not the person turned the
secret scan on: saved keys, well-known credential formats, and credentials
without one: `Authorization`, `Cookie` and API-key headers; secret query
parameters (`api_key=`, `token=`, `key=`, `code=`, signatures); password and
token options (`--password`, `--token`, `mysql -p…`, `curl -u user:…`,
`sshpass -p`); assignments to secret names (`PGPASSWORD=…`,
`db_password = …`, `"apiKey": "…"`); a private key whose end was cut off;
and long random-looking tokens. Hashes, commit ids, UUIDs and paths stay.
The record counts what was removed, never the values.

Never shared, at any level: file contents (a `file_write`'s content is
never an argument that's sent), tool output such as command output and web
pages, images, and the names of excluded files or the patterns that exclude
them.

## Security flags

Detected on the computer, deterministically, never by a model
(`lumi/security_flags.py`):

| Kind | Severity | When |
| --- | --- | --- |
| `destructive_command` | high | A command the [guardrails](shell-sandbox.md) never run, such as deleting the home folder |
| `dangerous_command` | medium | A command the permission mode refuses: a download piped to a shell, a recursive delete |
| `policy_denied` | medium or low | The organization's shell rules refused a call (medium); the project's `lumi-policy.json`, a hook or the review gate did (low) |
| `excluded_file` | medium | The agent tried to read, write or search a file Lumi never reads. The flag says whose rule it was, never the file or the pattern |
| `outside_project` | low | A path outside the project; the path isn't named |
| `approval_denied` | low or medium | The person or their permission hook said no (low); a [second person's approval](second-approval.md) was declined or never came (medium) |
| `secret_redacted` | medium or high | Secrets were removed before a model request or from what was shared; high for private keys and cloud secrets |
| `prompt_injection` | medium or low | What a tool returned tries to instruct the agent: "ignore previous instructions", "you are now…", fake system markers, asking to hide things from the person or to send credentials somewhere, instructions in HTML comments, hidden Unicode tag or direction characters. Medium from web pages, MCP servers, issue trackers and code hosts; low from local files and commands |

The engine marks each refused call with the layer that refused it
(`denied_by` on the `tool.result` event). A flag carries its kind,
severity, tool, session and turn, and a **rule that is a fixed label**
(`security_flags.RULES`: `delete_everything`, `organization_rule`,
`hook_denied`, `excluded_by_organization`, `ignore_instructions` and so on),
never free text. Lumi Cloud stores the rule in the clear, shows it to
everyone who sees oversight and forwards it to SIEM, webhook, email and
Slack destinations, so a hook's reason, a policy rule's words and an
exclusion pattern stay on the computer.

What a person or program wrote goes only in the excerpt: one line, at most
200 characters, secrets removed from the whole text before the excerpt is
cut from it (so a key cut at the excerpt's edge can't leave half of itself
behind), hidden characters shown as `<U+…>`. The excerpt goes only when the
organization also receives messages, and never for excluded files or paths
outside the project. The person sees their own flags, with excerpts, in
Settings.

The injection checks take linear time: tool output is text other people
wrote, and a pattern that backtracked (as an earlier HTML-comment pattern
did, 25 s on 200 KB) would hold the turn. Only the first 200,000 characters
of an output are searched.

## Where it goes, and when it doesn't

Records go only to the Lumi Cloud of the organization whose policy asks for
them: the policy must come from that Lumi Cloud (signed and verified for
this enrolled computer), or be a machine policy that enrolled the computer
there. Otherwise Settings says why nothing is collected.

1. After each turn, Lumi adds the turn and its flags to a queue in
   `~/.lumi/oversight/queue.sqlite3`. The queue holds at most 5,000 records
   (20 MB); past that the oldest are dropped and counted. Records older than
   the policy's `retention_days` are deleted unsent and counted: Lumi Cloud
   would only delete them.
2. A background thread in the app sends them in batches of up to 100 to
   `POST /api/v1/oversight/events`, signed in as the device
   (`lumi/cloud.py`), never on the UI's thread. Failures are retried after
   30 seconds, then up to an hour apart; new turns don't cut that wait
   short, a policy change does. `lumi run` prints its result first, then
   tries for up to 5 seconds before it exits; the app sends the rest.
3. Before every batch Lumi checks the policy again. If the organization
   stopped asking, Lumi Cloud answers `oversight_off`, or the computer left
   the organization, queued records are **deleted, not sent**.
4. A batch Lumi Cloud refuses as invalid isn't sent again; it's counted.

Every record dropped, refused, expired or deleted, and the last error, show
in Settings. The audit log records when a notice was confirmed
(`oversight.notice_shown`, also for each gateway chat), when confirmations
were forgotten (`oversight.notice_forgotten`) and when records were deleted
(`oversight.discarded`).

Lumi Cloud deletes what it keeps `retention_days` after each turn ran.
Viewing messages, titles and excerpts there needs a security admin or
auditor role, or an owner's grant, and every view is recorded in the
organization's activity log.

Files: `~/.lumi/oversight/` holds `queue.sqlite3`, `state.json` (the
counts), `flags.jsonl` (the person's last 500 flags), `notice.json` (the
notice last confirmed), `chats.json` (which gateway chats were sent which
notice) and `sessions.json` (turn numbers).

## Known limits

- The app can be used without confirming the notice: nothing is recorded
  until then, so an organization that relies on oversight sees nothing from
  someone who never confirms it.
- A worker's own secret redactions and model usage aren't in its parent's
  record: the parent sees the worker's tool calls only.
- Codex and Claude Code run their own tool loops: their turns are recorded,
  but their tools' refusals aren't seen by Lumi.
- An unattended run on a computer where nobody has confirmed the notice (a
  CI server with a service account) records nothing.
- Credentials with no name, no known format and no randomness (a short
  password typed as a bare argument) can't be told from ordinary text.
