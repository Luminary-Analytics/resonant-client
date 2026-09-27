# Data loss prevention

An [organization policy](enterprise-policy.md) can check everything Lumi sends
to a model provider against the organization's data loss prevention (DLP)
rules: payment card numbers, US Social Security numbers, IBANs, credentials,
email addresses, and the organization's own keywords and patterns. Each rule
records a match, redacts it before the request leaves, or blocks the request.
An organization can also have its own DLP service judge each request.

The client enforces the rules (`lumi/dlp.py`, `lumi/dlp_detectors.py`). People
can see them in **Settings > Privacy & security > Organization policy > Data
loss prevention**, with each rule's name, action and scope, but they can't turn
them off: they aren't settings, they come only from the policy.

Status: source only, not released.

## What is checked

Every request Lumi makes to a model:

- turns in the desktop app, the [terminal UI](terminal-ui.md), [`lumi run`](headless.md)
  and [scheduled tasks](scheduled-tasks.md), the [chat gateway](chat-gateway.md),
  tasks from Slack and Teams, sub-agents, orchestration specialists and
  [Team](swarming.md) workers, in the app's process and in worker processes;
- the requests around them: session titles, compaction summaries, the text
  sent with images to the vision model, the planning check, a specialist's
  structured-output repair, skill extraction and SONN employee advice.

The check sees each request as it will be sent, after the
[secret scan](../README.md) has removed saved keys: the instructions (Lumi's
own, the project's instructions and notes, memory, team notes, skills,
codebase index snippets and `@file`-style attachments), the new message, and
the conversation so far (earlier messages, replies, tool calls and their
results).

A rule's `scope` limits it to kinds of content:

| Kind | What it covers |
| --- | --- |
| `prompt` | What the person typed, including steering messages and assignments Lumi generates for workers |
| `attachment` | `@file:`, `@diff:`, `@issue:`, hand-offs and other context attachments, and text that describes an attached image or document |
| `tool_result` | Tool results, including file contents the agent read, and codebase index snippets |
| `instructions` | System and project instructions, project and team notes, memory and skills |
| `model_output` | The model's earlier replies, its tool calls' arguments and its reasoning, sent back with the conversation |

Some text mixes kinds: a compaction summary quotes requests, replies and tool
results, and so does the transcript sent to the summarizer. Every rule checks
that text, whatever its scope.

## Actions

- **flag**: the request is sent unchanged, and the match is recorded.
- **redact**: each match is replaced with `[REDACTED:<rule>]` in the copy that
  is sent. The conversation on this computer keeps the original, and a quiet
  notice in the conversation says how many matches were redacted and by which
  rules. Tool call arguments are JSON: only their string values (and whole
  numbers) change, so the structure stays valid. A model's reasoning can be
  signed by its provider and can't be edited; reasoning with a match is left
  out of the request instead.
- **block**: the request isn't sent. The turn fails with a message that names
  the rule and where it matched ("us_ssn in your message"), never the text.
  The conversation entry it came from is kept on this computer but marked, and
  later requests send a notice in its place while it still matches a block
  rule, so **Continue** or a new message works without it. A block in the
  instructions or an attachment has no entry to leave out: remove the content
  or ask the administrator.

When one request has matches for several rules, a block wins. Redactions that
overlap become one.

## Built-in detectors

| Detector | What it finds |
| --- | --- |
| `credit_card` | Card numbers of 13 to 19 digits, written together or in groups separated by single spaces or hyphens (4-4-4-4, 4-6-5), with a card network's prefix and length (Visa, Mastercard, American Express, Discover, JCB, Diners Club, UnionPay) and a valid Luhn check digit |
| `us_ssn` | US Social Security numbers written with hyphens or spaces (`123-45-6789`), except numbers never issued (area 000, 666 or 900-999, group 00, serial 0000). Nine digits without separators aren't matched: they're too often something else |
| `iban` | IBANs, compact or in groups of four, with the registered length for their country and a valid mod-97 check |
| `secrets` | The credential formats of the secret scan: private keys, cloud and platform keys and tokens, JSON web tokens, passwords in URLs and in `.env` lines. Only the secret is matched, so `DB_PASSWORD=` stays readable |
| `email` | Email addresses. Usually left off: commit metadata and documentation are full of them |

A detector runs only when the policy lists it.

## Custom rules

- **Keywords**: words or phrases, such as project code names. Matching ignores
  case unless `case_sensitive` is true, and matches whole words unless
  `whole_word` is false (so "Falcon" doesn't match "Falconry").
- **Patterns**: Python regular expressions. Matching ignores case unless
  `case_sensitive` is true. Because a scan must take time proportional to the
  text, whatever the text, a pattern is refused when:
  - a repeat has no upper limit: write `{0,100}` for `*`, `{1,100}` for `+`
    and `{n,m}` for `{n,}`;
  - it uses a backreference or a conditional group (`\1`, `(?P=name)`,
    `(?(1)…)`);
  - it can match more than 256 characters, or match empty text;
  - repeats compete for the same characters too much: `\d{1,20}\d{1,20}` could
    split a run of digits 400 ways and is refused, while
    `[a-z0-9-]{1,63}\.corp\.example\.com` is fine, because nothing the first
    repeat gives back can start a dot. Lumi multiplies the ways a pattern
    could match at one position by its longest match, and refuses more than 16
    ways or a product over 128.

  The policy's error message names the rule and what to change.

## Configuring it

```json
"dlp": {
  "version": 1,
  "detectors": {
    "credit_card": "block",
    "us_ssn": {"action": "block"},
    "iban": "redact",
    "secrets": "redact",
    "email": {"action": "flag", "scope": ["prompt", "attachment"]}
  },
  "rules": [
    {"name": "falcon", "keywords": ["Project Falcon", "FALCON-X"], "action": "block"},
    {"name": "customer-id", "pattern": "CUST-\\d{8}", "action": "redact", "scope": ["prompt", "tool_result"]},
    {"name": "internal-host", "pattern": "[a-z0-9-]{1,63}\\.corp\\.example\\.com", "action": "flag", "case_sensitive": true}
  ],
  "service": {"url": "https://dlp.example.com/v1/check", "timeout_seconds": 5, "on_error": "block"}
}
```

| Key | Meaning |
| --- | --- |
| `version` | Required, and `1`. |
| `detectors` | Detector names from the table above, each an action (`"redact"`) or an object with `action` and optional `scope`. |
| `rules` | Up to 100 rules. Each has a `name` (1 to 64 letters, digits, spaces, dots, underscores or hyphens; unique across rules and detectors), an `action`, either `keywords` (1 to 500 phrases of up to 100 characters, 100,000 characters in all) or `pattern` (up to 500 characters), and optionally `scope`, `case_sensitive` and, for keywords, `whole_word`. |
| `service` | An external DLP service, below. |

`action` is `flag`, `redact` or `block`. `scope` lists kinds from the table
above; without it a rule checks every kind.

The section is read strictly: an unknown key, an unsupported `version`, a
misspelled detector or kind, or a pattern that could scan slowly is an error.
An error in the `dlp` section doesn't make the policy disappear. Everything
else in it still applies, and **Lumi refuses every model request**, showing
why in the app and in Settings, until the section is fixed. The same holds for
a policy delivered by Lumi Cloud. Restart Lumi after changing the policy.

Rule names appear in redaction labels, messages and records. Keywords and
patterns never leave the policy: Settings shows names, actions and scope only,
because a keyword can itself name what it protects.

## An external DLP service

With `service`, Lumi also sends each request's text to the organization's DLP
service, after the built-in redactions, and waits for its verdict before the
request goes to the model. The service is reached with **Settings > Connections >
Network**'s proxy and certificate settings. `url` must be `https` (or `http` to
localhost), with no user name or password in it; the first version sends no
credentials of its own. `timeout_seconds` is 0.5 to 60 (default 5).

Lumi posts JSON:

```json
{
  "version": 1,
  "organization": "Example Corp",
  "purpose": "primary",
  "provider": "anthropic",
  "model": "claude-sonnet-5",
  "items": [
    {"id": "0", "kind": "instructions", "text": "…"},
    {"id": "1", "kind": "prompt", "text": "Refund [REDACTED:credit_card] for Jane Doe"}
  ]
}
```

`purpose` is `primary` for a turn's requests and names auxiliary ones
(`title`, `compression`, `planning`, …). Items are sent once: text the service
already allowed or redacted isn't sent again while Lumi runs.

The service answers with one of:

```json
{"action": "allow"}
{"action": "redact", "rule": "person-name", "redactions": ["Jane Doe", {"text": "ACME-7", "rule": "project", "item": "1"}]}
{"action": "block", "rule": "export-control", "items": ["1"]}
```

- `redactions` are literal text to replace wherever it appears (or only in
  `item`) with `[REDACTED:<rule>]`: up to 1,000, each up to 10,000
  characters.
- `items` in a block names the items the service objects to, so Lumi can
  leave their conversation entries out of later requests. Without it, the
  request is refused and nothing is marked.
- `rule` names the verdict in messages and records (`dlp-service` if it's
  missing or not a valid name). Lumi never shows the service's other fields.

A failure (a timeout, no connection, an HTTP error, an answer that isn't one
of the above, or more than 4,000,000 characters of new text) follows
`on_error`: `"block"` (the default) refuses the request, and `"allow"` sends
it with the built-in rules applied. Either way the audit log records a
`dlp.error`.

## Records

Every flag, redaction and block is recorded in the [audit log](audit-log.md)
as `dlp.finding`: the rule, the action, the kind of content, how many matches,
the request's purpose, provider and model, and whether the finding came from a
`rule` or the `service`. Never the matched text. A conversation sends its
history with every request, so content that was already recorded for a
session, provider and model isn't recorded again; a block is recorded each
time, since each blocked request is a separate refusal. `dlp.error` records a
request that couldn't be checked: too large, the service failed (with
`on_error`), or an internal error.

## Limits

- Every detector and rule scans in time proportional to the text. A megabyte
  of adversarial text (long digit runs, card-like groups, repeated key
  headers, secret prefixes) takes 0.03 to 0.3 seconds on the development machine
  for the five detectors, 200 keywords and two patterns together
  (`tests/test_dlp.py`). A conversation's earlier text isn't scanned again:
  results are kept by text for the life of the process.
- A request with more than 16,000,000 characters of text isn't checked, so it
  isn't sent.
- Images and other binary attachments aren't scanned; only their text
  descriptions are. Neither are tool definitions.

## What DLP doesn't cover yet

- Codex and Claude Code read files and run commands through their own tools.
  DLP checks what Lumi hands them (instructions, history and the message), not
  what they read themselves. Turn them off with `security.cli_adapters: false`
  if that matters.
- Text that isn't a model request: dictation audio (its transcript is checked
  when it's sent), MCP servers' and web tools' requests, sharing a conversation
  or a hand-off with Lumi Cloud, SONN task graphs, and the audit log's own
  content capture and OpenTelemetry export.
- A new DLP section applies to text already in a conversation from the next
  request on; nothing sent before it is recalled.
- Detection is pattern-based: there's no named-entity or document
  classification yet beyond what an external service provides.
- The service can't be given a credential in the policy yet.
- The [Team](swarming.md) preview doesn't start work while an organization
  policy applies; its workers already check their requests for when it does.
