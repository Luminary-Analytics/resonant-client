# Audit log

Lumi keeps a local record of what it and its agent did: each turn, each model
call's usage, tool calls and results, file changes, approvals, secret
redactions, settings changes and project trust decisions. The records are
hash-chained, so an edited, removed or reordered record is detected. They can
also be streamed to an OpenTelemetry collector.

The code is `lumi/audit.py`. It is separate from the per-mission audit trail of
autonomous runs (`lumi/orchestration/audit.py`) and from the flight recorder's
debugging traces.

## Where and what

Records are JSON lines in `~/.lumi/audit/YYYY-MM-DD.jsonl`, one file per UTC
day. `LUMI_STATE_HOME` moves the whole folder.

```json
{"v": 1, "seq": 42, "ts": "2026-09-25T04:00:00.123Z", "type": "tool.result",
 "session": "<conversation id>", "project": "C:/src/app",
 "data": {"agent": "", "tool": "bash", "call_id": "c7", "error": false,
          "denied": false, "elapsed": 1.25,
          "output": {"chars": 5120, "sha256": "…"}},
 "prev": "<hash of record 41>", "hash": "<sha256 of prev + this record>"}
```

| Type | When | Data |
|---|---|---|
| `turn.start` | A turn begins (GUI, gateway, terminal UI or a worker) | provider, model, permission mode, subagent, prompt, image count |
| `turn.end` | It ends | `outcome` (`completed`, `error`, `cancelled`, `stopped`), elapsed seconds |
| `model.usage` | Each model response | provider, model, purpose, input/output/cached tokens, cost and where its price came from ([usage records](usage-and-costs.md)) |
| `tool.call` | The model asks for a tool | tool, call id, argument names, `path`, and `command`/`pattern`/`query`/`url` by capture level |
| `tool.result` | A tool finishes or is refused | error, denied, elapsed, output by capture level |
| `file.change` | A successful `file_write`, `file_edit` or `file_replace`, or a Codex file change | path |
| `approval` | You or a permission hook decides a prompted call | tool, `by` (`user` or `hook`), `decision` |
| `privacy.redaction` | The secret scan removed credentials before a request | counts by kind |
| `settings.change` | Settings saves a change | section and key names, never values |
| `trust.decision` | A project is trusted, restricted or forgotten | project, decision |
| `budget.warning`, `budget.approval`, `budget.block` | A [budget](usage-and-costs.md#budgets) alerts, asks (with the answer) or stops a turn | owner, scope, period, spend, threshold, `decision` |
| `error` | A turn reports an error | code, message by capture level |

Turns from the chat gateway name their chat as `gateway:<chat id>`. A
delegated worker's events are recorded once, by the worker's own turn, with
its `agent` id.

## Capture levels

**Privacy & security > Audit log > What the audit log captures**
(`privacy.audit_capture`):

- **Metadata only** (the default): tool names, outcomes, sizes, file paths and
  SHA-256 digests. Never prompts, file contents, commands or output. A digest of
  short text can be confirmed by guessing the text, so this level is not
  anonymization.
- **Content, secrets removed** (`redacted`): content up to 2,000 characters per
  field, with known credential formats replaced.
- **Full content** (`full`): content up to 20,000 characters per field.

Saved key values are removed at every level, including from paths.

## Verifying the chain

**Privacy & security > Audit log status** shows the folder, the result of
checking every record's hash and link, and the export's state. **Verify again**
repeats the check.

What verification can and can't show:

- An edited record fails its hash. A removed or reordered record breaks the
  link to the one before.
- Deleting the newest records, or whole days older than every remaining one,
  leaves a valid chain. Export to a collector to keep a copy the user can't
  change.
- The GUI, the terminal UI and the gateway share the log through an OS file
  lock, so their records form one chain.

## Retention

`privacy.audit_retention_days` (default 365; 0 keeps everything) deletes whole
days older than the limit when the desktop app starts and daily while it runs.
Transcript retention
(`privacy.transcript_retention_days`) doesn't touch the audit log.
**Keep an audit log** (`privacy.audit_log`) turns recording off; existing
records stay until their retention ends.

## OpenTelemetry export

**Privacy & security > Send audit records to OpenTelemetry**:

- `audit.otlp_endpoint`: an OTLP/HTTP collector such as
  `https://collector.example.com:4318`. Lumi posts JSON to `/v1/traces` unless
  the URL already ends with it.
- `audit.otlp_auth_header`: the header that carries the token (default
  `Authorization`; for example `x-honeycomb-team`).
- The token is **Connections > API keys > OpenTelemetry collector token**
  (`api_keys.otlp`), kept in the OS credential store like other keys. Include a
  scheme such as `Bearer …` if the collector expects one.

Each record becomes a span. Model usage spans follow the OpenTelemetry GenAI
semantic conventions (`gen_ai.operation.name` `chat`, `gen_ai.provider.name`
and the older `gen_ai.system`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`). A tool's result is
an `execute_tool` span with `gen_ai.tool.name` and `gen_ai.tool.call.id`; the
model's request for it is a `tool.call` span. Other fields
appear as `lumi.*` attributes; content fields contribute a digest, plus their
text at the content levels. A session's records share one trace id.

Export runs on a background thread from a queue of up to 5,000 spans. When the
collector is slow or down, spans are dropped instead of slowing Lumi; the
status panel shows sent, waiting and dropped counts and the last error. The
local log is unaffected.

## For administrators

An [organization policy](enterprise-policy.md) can lock any of these settings,
for example:

```json
"settings": {
  "privacy.audit_log": true,
  "privacy.audit_capture": "redacted",
  "privacy.audit_retention_days": 400,
  "audit.otlp_endpoint": "https://otel.example.com:4318",
  "audit.otlp_auth_header": "Authorization",
  "api_keys.otlp": "Bearer <ingest token>"
}
```

A token in the policy is readable by anyone who can read the policy source
(the registry value, profile or file), so use an ingest-only token.

## Not covered yet

- Sign-ins, policy loads, extension installs and updates are not recorded yet.
- Codex and Claude Code report their own tool calls; Lumi records what they
  report, not what they didn't.
- Records are not signed, and there is no server-side retention. Lumi Cloud's
  audit service is planned with the admin portal.
