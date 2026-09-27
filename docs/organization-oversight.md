# Organization oversight: activity, messages and security flags

An organization can have Lumi share what its people do with the agent: each
turn's activity, optionally the messages, and security flags. Its Lumi Cloud
shows them to the organization's security staff. This page covers the
desktop app's side. Code: `lumi/oversight.py` (the notice and its
confirmation, admitting turns, recording, sending), `lumi/security_flags.py`
(detection), `secret_scan.redact_for_sharing` (what is removed before
anything leaves) and `dlp.shareable` (the organization's
[DLP rules](dlp.md) on what is shared). Lumi Cloud's side is in its own
repository.

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
  "project_paths": false,
  "unattended": "record"
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
| `unattended` | What a run with nobody to show the notice to does while nobody confirmed it as that computer user: `record` (the default) or `block`. See [runs with nobody at the screen](#runs-with-nobody-at-the-screen). |

A `version` or a key this Lumi doesn't know turns oversight **off**, and
Settings > Privacy & security says why (in Organization policy and
Organization oversight); the rest of the policy stays in force. This section
decides what is collected about people, so Lumi collects nothing rather than
guess, and a newer Lumi Cloud never stops model requests on an older Lumi.
`unattended` is a version 1 key: no Lumi that read version 1 without it was
released, and one that doesn't know it turns oversight off. Other mistakes
(a value of the wrong type, `messages` without `activity`, an `unattended`
other than `record` or `block`) make the policy invalid, like a mistake in
any section, and an invalid policy stops model requests until it's fixed
([When something is wrong](enterprise-policy.md#when-something-is-wrong)).

In Lumi Cloud, administrators set this on the Policy page and publish it;
the page asks them to confirm members will be told whenever a version
collects more than what the members it reaches have now.

## Nothing reaches a model until the notice is confirmed

While oversight is **in force** (the policy asks for something, and records
have [somewhere to go](#where-it-goes-and-when-it-doesnt)) and the person
hasn't confirmed its notice on this computer, Lumi sends nothing to a model:

- **In the app**, a notice above the message box names the organization and
  what it receives, for example: *"Acme receives your sessions and what they
  did, your messages, Lumi's replies and your sessions' titles (shortened,
  without code or secrets) and security flags from Lumi on this computer."*
  The organization's own `notice` follows. It has no close button. Below it:
  *Lumi won't send anything to a model until you confirm you've read this.*
  The message box, its send button, attachments, dictation and the
  autonomous-session button are disabled until the person presses **I've
  read this**; a draft stays in the box. Focus never moves onto the button by
  itself (a key meant for the message box can't confirm the notice): when the
  box locks with focus in it, focus goes to the notice, and Tab reaches
  **What's shared** and **I've read this**. Confirming unlocks the box and
  puts focus back in it. The button takes only a click or key press the
  browser reports as the person's (`isTrusted`), never a click a script
  made. A capability pack's [panel](extensions.md#panels) can still add
  text to the locked box, but it can't send it, and it can't reach the
  button: its sandboxed frame has no access to the page or the app's
  socket, and its bridge has no method that confirms or sends.
- **Every entry point refuses**, with a message that says how to confirm:
  `Session.run` (every turn: the app, `lumi run`, scheduled tasks, the
  terminal UI, the chat gateway, tasks from chat, plan and mission
  specialists, Team workers), `/plan`, a mission's **Build this roadmap**,
  autonomous sessions (start, resume and each iteration), Team (the desktop
  runtime's commands and every step of its orchestrator loop, through
  `service.policy_refusal`, and each participant's start and model request,
  through `TeamGovernance.refusal`; a personal team's reviews and
  bookkeeping, which reach no model, stay available), model comparisons (at
  the start and before each run), model evaluations, a scheduled task's
  **Run now** (the person's own action, unlike the schedule itself),
  dictation, AI Employee advice, and tasks from chat (the chat is told why).
  The server refuses a message even if a page sends one.
- Model requests outside a turn wait too: the terminal UI's planning
  classification (`Session.should_plan`), a session's automatic title after
  its first turn, a specialist's structured-output repair, `[vision]`
  acceptance checks and a mission's skill extraction. A policy can arrive
  while a turn runs, so each asks again rather than trusting the turn's
  start.
- `Session.run` asks before the turn and again before **each model
  request**, so a policy that arrives mid-turn with a notice the person
  hasn't confirmed stops the turn there. A refused turn never reaches the
  conversation's history. A delegated worker follows its parent's surface.
  A new entry point is covered by running through `Session.run`; a test
  (`TestEveryPath`) fails if any code calls the turn loop another way.
- A notice that says nothing is collected (the computer isn't enrolled, or
  the policy comes from elsewhere) blocks nothing and can't be confirmed.
- If the check itself fails while a policy asks for oversight, the turn is
  refused rather than admitted.
- The notice comes before [data loss prevention](dlp.md): a turn or request
  refused for the notice never reaches the DLP checks, so neither the rules
  nor a DLP service see its text, and nothing about it is recorded as a DLP
  finding.

A policy that collects more, another enrollment, or another organization
needs the notice confirmed again, and leaving the organization or signing
out of Lumi Cloud forgets the confirmation (the app locks again at once).
Confirmations are per computer user: another person signing in to the same
computer confirms for themselves.

**Settings > Privacy & security > Organization oversight** lists exactly what
is shared and what never is, who can read messages, what happens to runs
with nobody at the screen, how long it's kept, the person's confirmation
(when, which notice, and whether Lumi Cloud has it), what is waiting to be
sent, what was sent, and anything dropped, refused, expired or deleted. It
also lists the person's own security flags. **About Lumi** says it too, under
*What leaves this computer*.

Lumi never records keystrokes, screenshots, the clipboard or anything done
outside its own turns.

## Confirming the notice: a signed record

A confirmation comes only from a person:

- **app**: the notice's own **I've read this** button (never a status push,
  a timer or a painted page). The page sends the fingerprint and the notice
  as it showed it; a page that showed another policy's notice or text
  confirms nothing.
- **terminal**: `lumi run` at an interactive terminal (standard input and
  standard error are both terminals) and the terminal UI print the notice
  and ask for a typed `yes`; anything else stops without sending anything.
- **gateway**: a chat's own button or reply (below).

Each produces this record, kept with the confirmation in
`~/.lumi/oversight/notice.json` (a chat's in `chats.json`), with the exact
notice text that was shown:

```json
{"kind": "lumi.oversight-acknowledgment/v1", "organization": "<org id>",
 "notice_fingerprint": "<fingerprint>", "notice_sha256": "<sha256 of the notice text shown>",
 "surface": "app|terminal|gateway",
 "person": {"account": "<Lumi Cloud user id or null>", "os_user": "<computer user>", "chat": "<chat id or null>"},
 "device_id": "<enrolled device id>", "acknowledged_at": "2026-09-27T12:34:56Z"}
```

- `organization` and `device_id` are this computer's enrollment;
  `account` is the signed-in Lumi Cloud user (`null` when nobody is signed
  in, and always for a chat, whose people aren't this computer's account).
- The notice text is Lumi's sentence and the organization's words, as the
  surface shows it ("from Lumi on this computer" in the app and terminals,
  "from Lumi through this chat" in a chat).
- **Signed** with the enrolled device's Ed25519 key (`api_keys`
  `lumi_cloud_device_key`, `CloudClient.sign_as_device`) over the record's
  canonical JSON: sorted keys, no whitespace, UTF-8 (`ensure_ascii=False`),
  as policies are signed. The signature is base64url **with** padding.
- The person is unblocked at once. The record then waits in the queue's
  `acknowledgments` table and goes to `POST /api/v1/oversight/acknowledgments`
  (`{"record": ..., "signature": ...}`, signed in as the device) before any
  turn records, retried with the same backoff. A record the key couldn't
  sign when it was made is signed by the sender first.
- Lumi Cloud answers `201 {"id": ..., "policy_version": ...}` (the version
  whose notice the fingerprint matched, or `null`: a confirmation of a notice
  it didn't publish, such as a machine policy's, is kept and shown as
  matching none), or refuses: `409 notice_mismatch` (the record names another
  organization or computer, or the organization never published a notice),
  `422 invalid_signature`. A refusal (also a 400 or 403) is shown in Settings
  and never sent again; it doesn't block the person. A record made under another
  enrollment isn't sent to this one. Processes claim a record before sending
  it, so two never send one twice.

## Runs with nobody at the screen

A scheduled task (`lumi schedule run`, trigger `schedule`) and `lumi run`
without an interactive terminal (CI, service accounts, output or input
piped; trigger `headless`) have nobody to show the notice to.

- If this computer user confirmed the notice for the policy in force (in the
  app or at a terminal), the run proceeds and is recorded as theirs.
- Otherwise `oversight.unattended` decides:
  - `record` (the default): the run proceeds. It prints the notice first
    (on standard error; at the top of the output with `--output text`; as a
    first `lumi.oversight` line with `jsonl`; as the result's first key,
    `oversight`, with `json`), the audit log records
    `oversight.unattended_run`, and its records carry the computer user and
    this computer as who ran it.
  - `block`: the run is refused before anything starts (exit code 2) until
    someone confirms the notice on this computer as that user: in the app,
    or by typing yes at an interactive `lumi run` or the terminal UI.
- A scheduled run's kept result has the notice in its summary, not as an
  error. A model comparison's runs are `lumi run`s too; a comparison only
  starts once its person confirmed the notice, so they run as theirs.
- Tasks from chat never run on a managed computer (AGENTS.md); on a joined
  one they run only after the person confirmed the notice.

Only a surface that says nobody is there (`Session.oversight_unattended`)
runs under `record`; every other turn needs a person's confirmation.

## The chat gateway

The person running `lumi gateway` is shown the notice at their terminal.
The people in each chat confirm it themselves before that chat's requests
run:

- A chat that hasn't confirmed the notice for the policy in force is sent it
  instead of a reply, with an **I've read this** button (Telegram, Slack) and
  *reply "I've read this"*. The request doesn't run and nothing reaches a
  model.
- The button (`/acknowledge <token>`) or the reply `I've read this` (any
  case, with or without the curly apostrophe; also `I have read this`)
  confirms it for that chat and that policy's fingerprint: a signed record
  with `surface: "gateway"` and `person.chat`. A reply counts only once the
  chat was sent that notice, and a button from an older notice is answered
  with the current one. The chat then sends its request again.
- A policy that collects more is confirmed again. **status** repeats the
  notice. The gateway sends records and confirmations to Lumi Cloud while
  it runs.

## What a turn record holds

Recorded for every turn that goes through `Session.run` while oversight is
in force and the turn was admitted. A delegated worker's calls are part of
its parent's turn.

Every record (turns and flags) carries:

- `trigger`: what started it: `app`, `terminal` (the terminal UI, or `lumi
  run` someone confirmed at an interactive terminal), `gateway` (the chat
  gateway and tasks from chat), `schedule`, `headless` (`lumi run` without an
  interactive terminal), `plan`, `mission` (a mission's roadmap or an
  autonomous session) or `team` (a Team worker);
- `unattended`: `true` when nobody was there to be shown the notice
  (`schedule`, `headless`), `false` otherwise;
- `os_user`: the computer user who ran it (who an unattended run belongs
  to).

With `activity`:

- the session's id, the project folder's name (its path with
  `project_paths`) and where it ran (`surface`: `app`, `lumi run`,
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

**The organization's [DLP rules](dlp.md) apply to everything shared, after
secrets are removed and before anything is cut** (`dlp.shareable`), as they
apply to model requests: the person's message as a prompt (a message Lumi
wrote meets every rule), Lumi's reply and tool arguments as the model's
output, and titles and flag excerpts under every rule. A `redact` match
reads `[REDACTED:<rule>]`; text a `block` rule matches never reached a model
and isn't shared either: a message or argument reads `[withheld by data loss
prevention]`, a title is left out and an excerpt is empty. With a DLP
service, its remembered verdicts apply too: values it redacted are replaced,
and text it blocked isn't shared, nor text holding it or part of it. A
service needn't say which text it blocked, so a turn in which a DLP service
refused a request shares no text at all: its activity and flags go, its
messages read `[withheld by data loss prevention]`, and its arguments and
excerpts are left out. A `dlp` section Lumi can't use, which refuses every
request, withholds all text too. Nothing is asked of the service for
oversight: text it hasn't judged, such as Lumi's last reply, meets the rules
alone.

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
200 characters, secrets removed and the organization's DLP rules applied to
the whole text before the excerpt is cut from it (so a key or a card number
cut at the excerpt's edge can't leave half of itself behind), hidden
characters shown as `<U+…>`. Text DLP withholds gives the flag no excerpt;
excerpts are checked against a DLP service's verdicts again when the turn's
records are made, since it judges tool output only when the next request
carries it. The excerpt goes only when the
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
there. Otherwise Settings says why nothing is collected, and nothing is
blocked.

1. After each turn, Lumi adds the turn and its flags to a queue in
   `~/.lumi/oversight/queue.sqlite3`. The queue holds at most 5,000 records
   (20 MB); past that the oldest are dropped and counted. Records older than
   the policy's `retention_days` are deleted unsent and counted: Lumi Cloud
   would only delete them.
2. A background thread sends confirmations of the notice first, then turn
   records in batches of up to 100 to `POST /api/v1/oversight/events`,
   signed in as the device (`lumi/cloud.py`), never on the UI's thread. The
   app, the terminal UI and the chat gateway run it; `lumi run` prints its
   result first, then tries for up to 5 seconds before it exits (the app or
   the next run sends the rest). Failures are retried after 30 seconds, then
   up to an hour apart; new turns don't cut that wait short, a policy change
   does.
3. Before every batch of records Lumi checks the policy again. If the
   organization stopped asking, Lumi Cloud answers `oversight_off`, or the
   computer left the organization, queued records are **deleted, not
   sent**.
4. A batch Lumi Cloud refuses as invalid isn't sent again; it's counted.

Every record dropped, refused, expired or deleted, and the last error, show
in Settings. The audit log records each confirmation
(`oversight.notice_shown`, with its surface, fingerprint and the notice's
SHA-256), when confirmations were forgotten (`oversight.notice_forgotten`),
unattended runs (`oversight.unattended_run`), confirmations Lumi Cloud
refused (`oversight.acknowledgment_refused`) and when records were deleted
(`oversight.discarded`).

Lumi Cloud deletes what it keeps `retention_days` after each turn ran. In
Lumi Cloud, owners, security admins and auditors can read messages, titles
and excerpts through their role; anyone else only if an owner allows them;
and every view is recorded in the organization's activity log.

Files: `~/.lumi/oversight/` holds `queue.sqlite3` (records and confirmations
waiting to be sent), `state.json` (the counts), `flags.jsonl` (the person's
last 500 flags), `notice.json` (this person's confirmation, its record and
signature), `chats.json` (which gateway chats were sent and confirmed which
notice, with their records) and `sessions.json` (turn numbers).

## The contract with Lumi Cloud

What Lumi Cloud implements against (lumi-cloud's oversight ingest):

- **The fingerprint** of the notice in force: the first 16 hex characters of
  SHA-256 over the canonical JSON (sorted keys, no whitespace, UTF-8,
  `ensure_ascii=False`) of `{"device": <device id>, "organization_id":
  <organization id>, "oversight": <the policy's oversight section exactly as
  published>}` (`oversight.notice_fingerprint`). Lumi Cloud computes it for
  the device from each policy version it published to tell which notice was
  confirmed. A machine policy's section that Lumi Cloud didn't publish
  matches no version: Lumi Cloud keeps its confirmation all the same and
  shows that it matched none.
- **Acknowledgments**: `POST /api/v1/oversight/acknowledgments`, device
  authenticated like `POST /api/v1/oversight/events`; body `{"record":
  <the record above>, "signature": "<base64url Ed25519 over the canonical
  JSON of record>"}`. Lumi Cloud verifies it with the device's registered
  public key, checks that `organization` and `device_id` match the
  authenticated device, and stores it immutably. Answers: `201 {"id": ...,
  "policy_version": <matched version or null>}`; `409 notice_mismatch`;
  `422 invalid_signature`.
- **Events** carry `trigger` and `unattended` (and `os_user`) as above; Lumi
  Cloud shows and filters by `trigger` and `unattended`.
- **Policy**: `oversight.unattended`, `record` (the default) or `block`, a
  version 1 key.

## Known limits

- `lumi run` counts as interactive only when standard input and standard
  error are both terminals: a person who pipes a task in (`lumi run -`) is
  an unattended run, which prints the notice and, under `record`, runs.
- The check reads small files before every turn and model request
  (`settings.json`, the confirmation): cheap, but not free.
- A worker's own secret redactions and model usage aren't in its parent's
  record: the parent sees the worker's tool calls only.
- Codex and Claude Code run their own tool loops: their turns are admitted
  and recorded, but their tools' refusals aren't seen by Lumi.
- The app and a `lumi run` (or scheduled task) running at the same time can
  both send a record the run queued: Lumi Cloud keeps it once, by its id,
  but Settings' count of sent records counts it twice.
- A DLP service's verdicts are matched to shared text by content: a text it
  blocked is recognized when it is the shared text, holds it or is part of
  it (spacing aside; a piece shorter than 8 characters only when whole).
  Text the service hasn't judged meets the rules alone, and the flags listed
  in Settings keep excerpts as they were made.
- Credentials with no name, no known format and no randomness (a short
  password typed as a bare argument) can't be told from ordinary text.
