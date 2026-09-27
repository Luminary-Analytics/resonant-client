# Extensions: model providers and panels

A capability pack can add a **model provider**: a program Lumi starts for
each request to one of its models. The pack's models then appear in the
model menu like any other connection, and the agent uses them with its
usual tools. A pack can also add [panels](#panels): pages of its own that
the person opens in Lumi, which run in a sandbox. You don't need Lumi's
source to write either; this page and the
SDK in [`sdk/`](https://github.com/Luminary-Analytics/resonant-client/tree/main/sdk)
are the whole contract.

This is the Extension SDK, version 1. Code: `lumi/engine/provider_extensions.py`
and, for panels, `lumi/gui/extension_panels.py`.
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

## Panels

A pack can add **panels**: pages made of its own HTML, scripts, styles and
images that the person opens in Lumi, such as a build dashboard or a prompt
picker. A panel talks to Lumi through a small bridge. It can read the
project's name and the theme, add text to the message box without sending
it, and show a notice. It has no network access except WebRTC (see
[what panels don't do](#what-panels-dont-do)), and it can't read Lumi's
page, the conversation, settings, keys or files, run tools, or send a
message.

### Declaring a panel

```json
{
  "id": "acme-builds",
  "name": "Acme builds",
  "version": "0.1.0",
  "manifest_version": 1,
  "ui_panels": [
    {"id": "stats", "title": "Build stats", "entry": "panels/stats/index.html"}
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Lowercase letters, digits and dashes, up to 40, different for each panel in the pack. |
| `title` | Shown under View > Panels, in the command palette and above the panel. Up to 60 characters. |
| `entry` | The panel's page: an `.html` file inside the pack, up to 1 MB. |

A pack has up to 10 panels. The panel can load files from its entry's folder
and the folders below it: `.html`, `.js`, `.css`, `.svg`, `.png`, `.jpg`,
`.gif`, `.webp` and `.ico` files, up to 4 MB each. So give each panel a
folder of its own (`panels/stats/`). Paths use `/`, and no part of one may
start with a dot or be `..`. Absolute paths, backslashes and drive letters
are refused. Lumi checks panels when the pack loads: a panel that breaks
these rules makes the manifest invalid, and the whole pack stays off until
it's fixed. Other keys in a panel are ignored. `lumi extension check` lists
the panels it found.

### Opening a panel

Panels from approved, enabled packs are listed under **View > Panels** in the
application menu, and in the command palette (Ctrl+K) as "Open panel: …".
Settings > Capability packs lists a pack's panels for review before you
approve it. A panel opens in a dialog over the conversation. Escape (in the
panel or on the dialog) or × closes it, and focus goes back to the Menu
button or the command palette's button, whichever opened it; it never goes
to the message box. **Panels from capability packs** in Settings > Privacy &
security turns them all off.

Panels open in a browser and in the Windows desktop window. The macOS and
Linux desktop window doesn't open them yet (see
[how panels are isolated](#how-panels-are-isolated)): use
**File > Open in Browser** there.

### Writing the page

Lumi adds its bridge script at the start of each HTML file of the panel,
before anything else, so `window.lumi` is ready for the panel's own scripts:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="panel.css">
</head>
<body>
  <h1>Build stats</h1>
  <button type="button" id="ask">Ask about the failures</button>
  <script src="panel.js"></script>
</body>
</html>
```

```js
lumi.onContext(({project, theme}) => {
  document.querySelector('h1').textContent = `Build stats for ${project}`;
});
document.getElementById('ask').addEventListener('click', async () => {
  await lumi.insert('Why did the last three builds fail?');
  await lumi.toast('Added to your message');
});
```

| Call | What it does |
|---|---|
| `lumi.context()` | Resolves to `{project, theme}`: the project folder's name (never its path) and `"dark"` or `"light"`. |
| `lumi.onContext(fn)` | Calls `fn` with the context when the panel loads and when the theme changes. |
| `lumi.insert(text)` | Adds `text` after what the person has typed. Lumi never sends it: the person reads it, edits it and sends it. See the rules below. |
| `lumi.toast(text)` | Shows a one-line notice of up to 200 characters in the panel's dialog, marked "Panel · *pack name*", apart from Lumi's own notices. One a second. |

Each call returns a Promise that rejects with Lumi's reason when the request
is refused. Before adding text, Lumi checks with its server that the pack is
still installed, approved, enabled and unchanged, and it holds the text to
what the person can review in the message box:

- up to 8,000 characters and 20 lines;
- no invisible characters (Unicode format and control characters other than
  tab and newline), so what shows is what the model gets;
- no padding: runs of spaces become one, indentation keeps at most 8
  columns, and blank lines collapse to one;
- no attachments: an @mention such as `@file:app.py` is split apart
  (`@ file:app.py`), so it attaches nothing unless the person rejoins it;
- never a command: text that would make the message start with `!` (a shell
  command) or `/` (a Lumi command) is refused, and Lumi says so.

Lumi puts the caret where the added text starts and scrolls the message box
to it. Text is refused when no conversation is open.

The bridge closes the panel when the person presses Escape in it, unless the
panel handled the key (`event.preventDefault()`), and marks the page
`<html data-lumi-theme="dark">` or `"light"` so styles can follow the theme:

```css
body { background: #13152e; color: #ebebeb; }
[data-lumi-theme="light"] body { background: #ffffff; color: #1a1d3d; }
```

A panel's page may load scripts, styles and images from its own folder, and
`data:` and `blob:` images. It may not use inline `<script>` elements, `on…=`
attributes, `style="…"` attributes, `eval` or `new Function`; set computed
styles through `element.style`. Module scripts, web fonts and `fetch` need the
network, so they don't work, nor do frames, workers, forms, popups and dialogs
(`alert`). Panel files are served to the panel only: a panel URL pasted into
a browser's address bar is refused.

#### The protocol

`window.lumi` wraps `postMessage`, which a panel can also use directly:

```js
parent.postMessage({lumi: 1, id: 7, method: 'composer.insert', text: 'Hello'}, '*');
// Lumi answers {lumi: 1, id: 7, ok: true, result: null}
// or {lumi: 1, id: 7, ok: false, error: "…"}.
```

The methods are `context`, and `composer.insert` and `toast`, both with
`text`. `id` is optional: a whole number, or a string of up to 64
characters, returned in the answer. Lumi ignores a message without `lumi: 1`,
answers another method with an error, and answers at most 20 requests a
second. It sends `{lumi: 1, event: "context", context: {…}}` when the panel
loads and when the theme changes.

A panel can't close itself. Closing on Escape uses a private channel (a
`MessagePort`) that Lumi's page hands its bridge script as the panel loads.
The script runs first, so the panel's own scripts never see the channel, and
it sends on it only for an Escape key press the browser reports as real. A
panel that could close itself could send the person's next keystrokes to the
message box.

### How panels are isolated

- **An origin of their own.** A panel runs in `<iframe sandbox="allow-scripts">`
  without `allow-same-origin`: an opaque origin with no access to Lumi's
  page, its storage (which holds the launch's access token), cookies or
  socket. The frame can't navigate the window, open popups, submit forms or
  show dialogs.
- **No network except WebRTC.** Each panel file is served with its own
  Content-Security-Policy: scripts, styles and images only from the panel's
  own path (`/panels/<token>/`), `connect-src 'none'`, no frames, workers,
  objects, forms or base URL, and `sandbox allow-scripts`, which sandboxes the
  page even when it's loaded without the frame's sandbox. Lumi's page allows
  frames only from Lumi's own server (`frame-src 'self'`), so a panel can't
  navigate itself to another site either.
- **Only the files you approved.** Lumi serves a panel's files under a random
  token it makes when you open the panel. It isn't the launch token, reads
  only that panel's folder, and ends when the panel closes, when the page
  closes, or after 12 hours. Each file is checked as it's served: panels
  allowed, and the pack installed, approved, enabled, allowed by your
  organization and unchanged. So that a panel loading many files doesn't
  hash its pack for each, a pack found unchanged is trusted for 3 seconds
  under the same approval and policy, but every file's bytes are still
  checked against the approval. The file must be one the approval covered.
- **A revoked or changed pack.** Its open panel can't load anything more or
  add text (each addition checks the pack again), and Lumi closes it: when
  it next lists panels (after a change in Settings, when you come back to
  the window, open the menu or the command palette), or when the app's
  connection drops.
- **A narrow bridge.** Lumi's page takes a message only from that exact frame
  (`event.source`) with the sandbox's origin (`"null"`), in the shape above,
  limited in size and rate. It answers that frame only. A panel's notice
  shows in its dialog, marked as the pack's, and can't replace or pass for
  one of Lumi's. An approval or another dialog always shows above a panel.
- **The desktop window.** Lumi opens panels only where its own window bridge
  can't be reached from the panel's frame. On Windows (WebView2), a
  sandboxed frame can post to the window's bridge, but its messages don't
  reach the app (checked with pywebview 6.1). WebKit, which the window uses
  on macOS and Linux, gives the bridge to every frame, and so does Qt, so
  those windows send the person to the browser. The desktop window also
  refuses a bridge call whose name or id isn't a plain identifier, which
  keeps any frame from running script in Lumi's page through pywebview.
- **Organization policy.** A pack your organization's policy turns off
  (`extensions.allowed_packs`, `require_signed`, `registry_only`) has no
  panels, and a policy can turn panels off for everyone with
  `"settings": {"security.extension_panels": false}`. A policy that can't be
  used, or has expired, turns them off too.

### What panels don't do

- **Only what the bridge offers.** A panel can't read files, settings, keys
  or the conversation, run tools or commands, send messages, or reach the
  pack's model providers and MCP servers.
- **WebRTC isn't covered.** Browsers don't apply the Content-Security-Policy
  to WebRTC, so a panel's script can send data to a server of its choosing
  that way. This was seen in Edge, where the proposed `webrtc 'block'`
  directive is ignored. A panel can only send what it can see: the project's
  name, the theme and what you type into it. Approve packs whose code you've
  read or whose publisher you trust, as for hooks. For the same reason
  [offline mode](offline.md) keeps panels closed, and turning it on closes
  an open one.
- One panel is open at a time, and panels don't open in the macOS and Linux
  desktop window yet.

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
  can't add a model to your menu. Panels, which run in a sandbox, may also
  come from a pack in the project that you approved there; every file they
  load is checked the same way (see [how panels are isolated](#how-panels-are-isolated)).
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
