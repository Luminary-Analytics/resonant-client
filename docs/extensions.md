# Extensions: add a model provider

A capability pack can add a **model provider**: a program Lumi starts for
each request to one of its models. The pack's models then appear in the
model menu like any other connection, and the agent uses them with its
usual tools. You don't need Lumi's source to write one; this page and the
SDK in [`sdk/`](https://github.com/Luminary-Analytics/resonant-client/tree/main/sdk)
are the whole contract.

This is the Extension SDK, version 1. Code: `lumi/engine/provider_extensions.py`.
Packs themselves (agents, skills, hooks, MCP servers, approval) are
described in [Writing a capability pack](packs.md).

## Quick start

1. Make a pack from the template, in a clone of this repository:

   ```sh
   python sdk/new_pack.py ~/work/acme-models --name "Acme models"
   ```

   It copies `sdk/templates/provider-python` and the `lumi_extension` SDK
   into the folder. The template's `echo` model works offline; its
   `remote` model forwards to any OpenAI-compatible endpoint.
2. Change `provider.py`, then run its tests in the folder:
   `python -m pytest`.
3. Check it as Lumi will: `lumi extension check ~/work/acme-models`. This
   loads the manifest with Lumi's rules, lists the models, and asks the
   first model a short question.
4. Install it: copy the folder into `~/.lumi/packs/`, or push it to a Git
   repository and use **Install from Git** in Settings > Capability packs.
   Review what it runs there and approve it.
5. In Settings > Connections, choose **Add connection**, set the type to
   **A provider from a capability pack**, pick the provider, and choose
   **Test connection**. The test starts the provider. Then choose **Add connection**.

## The manifest

A provider pack's `lumi-pack.json` adds three fields to the
[usual ones](packs.md#the-manifest):

```json
{
  "id": "acme-models",
  "name": "Acme models",
  "version": "0.1.0",
  "manifest_version": 1,
  "lumi": ">=0.19.2",
  "providers": [
    {
      "id": "acme",
      "name": "Acme",
      "command": {
        "windows": ["python", "provider.py"],
        "macos": ["python3", "provider.py"],
        "linux": ["python3", "provider.py"]
      },
      "models": [{"id": "acme-large", "context_window": 128000, "tools": true}]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `manifest_version` | `1` for this SDK. A pack without it is read as version 0, from before the SDK. A pack with a newer version than this Lumi knows doesn't load, and Settings says why. |
| `lumi` | Optional. The Lumi versions the pack works with: comma-separated `>=`, `>`, `<=`, `<` and `==` clauses, such as `>=0.19.2,<1`. |
| `providers` | Up to 10 providers. |

Each provider:

| Field | Meaning |
|---|---|
| `id` | Lowercase letters, digits and dashes, up to 40. A connection names it with the pack's id. |
| `name` | Shown in Settings. |
| `command` | The program and its arguments, or one list per system under `windows`, `macos`, `linux` or `default`. It runs in the pack's folder. A program given as a relative path (`bin/acme`) must be inside the pack. A bare name (`python3`, `node`) is looked up on PATH, never in Lumi's current folder. |
| `models` | Optional. Each model's `id`, its `context_window` in tokens (at least 1,024; 32,768 when not given) and whether it calls `tools` (default true). When the manifest lists models, Lumi offers them without starting the provider. Otherwise it asks the provider. |

[`sdk/schema/lumi-pack.schema.json`](https://github.com/Luminary-Analytics/resonant-client/blob/main/sdk/schema/lumi-pack.schema.json)
describes the manifest for editors that validate JSON.

## The protocol

Lumi starts the command, writes one line of JSON to its standard input and
closes it. The provider writes one JSON object per line to standard output
and exits. Anything on standard error is shown only if the provider fails.

**Listing models.** Lumi sends:

```json
{"lumi_extension": 1, "method": "models"}
```

and reads one line:

```json
{"type": "models", "models": [{"id": "acme-large", "context_window": 128000, "tools": true}]}
```

**Answering.** Lumi sends:

```json
{"lumi_extension": 1, "method": "stream", "params": {
  "model": "acme-large",
  "messages": [
    {"role": "system", "content": "…instructions…"},
    {"role": "user", "content": "Fix the failing test"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "file_read", "arguments": {"path": "test_app.py"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "…the file…"}
  ],
  "tools": [{"name": "file_read", "description": "…", "parameters": {"type": "object", "…": "…"}}],
  "max_tokens": null}}
```

and reads these lines, in order:

| Line | Meaning |
|---|---|
| `{"type": "text", "text": "…"}` | Part of the answer. Lumi shows each part as it arrives. |
| `{"type": "tool_call", "id": "…", "name": "…", "arguments": {…}}` | Run one of the request's tools. Its result comes back in the next request as a `tool` message. The `id` is optional. |
| `{"type": "done", "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.01}` | The answer is complete. Usage goes to Lumi's usage records and budgets. `cost_usd` is optional: without it Lumi prices the model if it knows it, and otherwise counts the request as unpriced, never as free. |
| `{"type": "error", "message": "…"}` | The request failed. Lumi shows the message. |

Lumi ignores types it doesn't know, so a provider can write lines a later
version defines. Nothing may follow `done` or `error`. An answer that ends
without either is reported as unfinished.

Messages are text. Images from the person or from a tool's screenshot
arrive as notices that describe them, never as the image, so a provider
never claims to have seen one.

## The environment

The provider runs as you, with your environment minus the model keys Lumi
removes from every program it starts (`secrets_store.child_env`), plus:

| Variable | Value |
|---|---|
| `LUMI_PROVIDER_API_KEY` | The key saved with the connection, when there is one. |
| `LUMI_EXTENSION_DATA` | A folder of its own, `~/.lumi/extensions/<pack id>`, for caches, tokens and other files it keeps. |
| `LUMI_EXTENSION_PROTOCOL` | `1`. |
| `PYTHONPYCACHEPREFIX` | Where Python keeps its bytecode cache for extensions, outside the pack. |

**Never write inside the pack's folder.** Any change there turns the pack
off until you approve it again. Python's bytecode cache goes elsewhere for
this reason, which also means `.pyc` files shipped in a pack never run in
place of the source you reviewed.

An answer may take up to 10 minutes (`LUMI_EXTENSION_TIMEOUT_SEC`), and
listing models 30 seconds. **Stop** ends the provider's process.

## The Python SDK

`sdk/python/lumi_extension` uses only the standard library, so a pack can
carry it as it is (the template does).

```python
from lumi_extension import Provider, done, serve, text, tool_call

class Acme(Provider):
    def models(self):
        return [{"id": "acme-large", "context_window": 128000, "tools": True}]

    def stream(self, request):          # request.model, .messages, .tools, .max_tokens, .api_key
        reply = call_acme(request)
        yield text(reply.text)
        for call in reply.tool_calls:
            yield tool_call(call.name, call.arguments, id=call.id)
        yield done(reply.input_tokens, reply.output_tokens)

if __name__ == "__main__":
    raise SystemExit(serve(Acme()))
```

- `serve` answers one request. An exception becomes an `error` line, and an
  answer that forgets `done()` gets one.
- `data_dir()` is the extension's own folder.
- `lumi_extension.testing` runs a provider the way Lumi does:
  `call(command, method, params, cwd=...)` returns the lines it wrote, and
  `check_models` and `check_stream` list what Lumi would reject.

## Other languages

Any program that reads one line and writes lines of JSON can be a provider.
In Node.js:

```js
const request = JSON.parse(require('fs').readFileSync(0, 'utf8').split('\n')[0]);
const say = event => process.stdout.write(JSON.stringify(event) + '\n');
if (request.method === 'models') say({type: 'models', models: [{id: 'hello'}]});
else { say({type: 'text', text: 'Hello from Node'}); say({type: 'done', usage: {input_tokens: 0, output_tokens: 3}}); }
```

## Signing a pack

A signature tells the people who install your pack that it came from you
and hasn't changed since. Line endings in text files don't count, so a
Windows checkout matches a Linux or macOS one; binary files count byte for
byte. It never approves a pack: each person still
reviews and approves it.

1. Make a key once, and keep the private key file secret:

   ```sh
   lumi extension keygen ~/keys/acme-packs.key
   ```

   It prints your **public key** and its **key id**. Publish both where
   people can check them, such as your website or README.
2. Sign the pack after every change, since any change breaks the signature:

   ```sh
   lumi extension sign ~/work/acme-models --key ~/keys/acme-packs.key --publisher "Acme"
   ```

   This writes `lumi-pack.sig` into the pack. It covers every file in the
   pack but itself.

Settings > Capability packs shows what each pack's signature says:

| Shown | Meaning |
|---|---|
| Signed by … · verified | The files match the signature, and the key is one you or your organization trust. The name is the one you or your organization gave it. |
| Signed as "…" with key …, which you haven't trusted | The files match, but nobody has trusted this key yet, so the name is only a claim. Compare the key id with the publisher's before choosing **Trust "…"**. |
| Its signature is invalid | The files changed after signing, or the signature is malformed. The pack can't be approved. |
| Not signed | |

**Trusted publishers** at the bottom of that page lists the keys you
trusted, and any your organization's policy adds. **Forget** removes one of
yours.

An organization can list its publishers and turn off every pack they didn't
sign: `extensions.trusted_publishers` and `extensions.require_signed` in its
[policy](enterprise-policy.md), or Lumi Cloud's Policy page. Only the
organization's own publishers meet that requirement, not ones a person
trusted.

## Your organization's registry

An organization can list the packs it approves in Lumi Cloud's
**Extensions** page. Each entry gives the pack's id, its public https
repository, a full commit, an optional folder, and optionally a content
digest. The policy sends the list to every computer as
`extensions.registry`.

- Settings > Capability packs shows the registry under your
  organization's name. **Install** fetches exactly the pinned commit. When a digest
  is pinned, the files must match it before anything already installed is
  replaced. You still review and approve each pack.
- **Content digest.** `lumi extension check` prints a pack's content digest.
  Line endings in text files don't change it, so the same commit checked out
  on Windows, macOS or Linux gives the same digest.
- **Only the registry's packs.** With `extensions.registry_only`, every
  other pack is off, and so is a registry pack at another version. Install
  the pinned version to turn it back on. A registry pack matches when its
  files match the pinned digest, or, without one, when it was installed from
  that repository, commit and folder.

## Trust

- **Approval pins the pack.** Settings > Capability packs shows each
  provider's command before you approve the pack, and the approval covers
  every file in it.
- **Every request checks again.** Before Lumi starts a provider, the pack
  must still be installed, approved, enabled and unchanged. Otherwise the
  request fails with the reason, and nothing starts.
- **Only personal packs.** Providers come from packs in `~/.lumi/packs` or a
  configured folder, never from a pack inside a project, so a repository
  can't add a model to your menu.
- **It runs as you.** A provider is a program on your computer, outside the
  shell sandbox, like an MCP server from a pack. Approve packs whose code
  you've read or whose publisher you trust.
- **Organization policy applies.** A policy that limits packs
  (`extensions.allowed_packs`) or models limits providers too. See
  [Organization policy](enterprise-policy.md).

- **Signatures say who made it.** A pack whose signature doesn't match its
  files can't be approved, and an organization can require its publishers'
  signatures. See [Signing a pack](#signing-a-pack).

## What version 1 doesn't do

- One process per request; there's no long-running provider yet.
- Text only: no images in, and no reasoning levels.
- Models run one request at a time, and their capabilities come from the
  manifest.

## When something goes wrong

| Message | What to do |
|---|---|
| Approve the … pack in Settings > Capability packs … | Approve it there. "It changed since you approved it" means a file in the pack changed. |
| The … pack isn't installed | The connection names a pack that isn't in `~/.lumi/packs`, or it's inside a project. |
| … isn't installed on this computer (it isn't on PATH) | Install the program the command names, or give its full path. |
| The provider exited with code … | The provider failed before answering; its error output follows, without saved keys. |
| The provider wrote something that isn't JSON | Only JSON lines may go to standard output. Print logs to standard error. |
