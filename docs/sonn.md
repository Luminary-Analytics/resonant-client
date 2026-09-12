# SONN connection

Available in Resonant v0.18.0. SONN supplies a project-scoped Chat Completions
endpoint; Resonant runs the coding tools and retains project instructions,
permissions, notes, history, and verification through its existing engine.

## Set up in the desktop app

1. Open **Settings > Network**. Paste your complete SONN API base URL, for example
   `https://getsonn.com/v1/workspace/projects/<project-id>/openai/v1`.
2. Under **API keys**, enter your private invitation in **SONN API key**, then
   leave the field to save. The field becomes empty and shows **Stored**. Keys
   are stored locally in `~/.resonant/settings.json`, not returned to the UI.
3. Under **Connections**, select **Check SONN connection & refresh models**.
   This performs authenticated model discovery without generating tokens.
4. Return to your session, open **Models**, and select `sonn-auto` under SONN.
   Connecting does not change your existing model or provider. Save a project
   preference only if you want SONN to apply to that project's new sessions.

`YOUR_PRIVATE_INVITATION` is a placeholder, not a usable key. Keep the real key
out of chat, logs, fixtures, and source control. Stop an active run before
changing its SONN connection. Clearing its credentials disables sending until
you select a working connection; an environment key can still supply access.

For managed configuration, `SONN_API_URL` overrides the saved URL and
`SONN_API_KEY` supplies the key when no saved key exists. HTTPS is required
except for HTTP loopback endpoints used by local services and tests. Embedded
URL credentials, query strings, and fragments are rejected.

## Wire contract and capabilities

- `GET {base_url}/models`, with `Authorization: Bearer <key>`, discovers model IDs
  from the standard `data` array. The documented fallback is `sonn-auto`.
- `POST {base_url}/chat/completions` sends standard messages, streaming SSE,
  optional `max_tokens`, `stream_options.include_usage`, and top-level function
  tools. No extra `/v1` is appended to the supplied project path.
- Text deltas, fragmented tool arguments, tool results, and token usage use the
  existing engine event contract. Retained system summaries survive handoff.
  Moonshot tool catalogs and provider-specific reasoning continuation are not
  replayed to SONN.
- Model catalogs are cached for five minutes per URL and credential fingerprint,
  with at most 16 cached catalogs. The connection button forces a refresh.
  Network probes run outside the UI event loop.
- The supplied contract does not specify vision, reasoning controls, prices,
  parallel tool support, or context size. The adapter uses text-only conservative
  defaults and a 32,768-token context unless discovery supplies a positive
  `context_length`. It attempts standard function tools; live routed-model
  support still needs validation. No SONN dollar-cost accuracy is claimed.
- **Stop** closes the local streaming response. SONN has not supplied a remote
  cancellation endpoint, so this does not prove server computation or billing
  stopped. Errors are sanitized before display and retry diagnostics.

## Validation and troubleshooting

The automated suite exercises mock model discovery, SSE streaming, function
calls, cancellation, credential changes, redaction, and a real engine loop
writing a fixture file. Browser checks use a local mock HTTP endpoint. These
checks validate the client contract; authenticated live SONN behavior remains
pending the user's key entry. See [release evidence](v0.18.0-release-notes.md).

For rejected credentials, check the key and access to the configured project.
For a missing endpoint/model, check the complete project URL and model ID.
For a usage limit, check your SONN account allowance. A successful connection
check proves model discovery only; send a small coding task to validate
generation after selecting SONN.
