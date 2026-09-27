# Offline and air-gapped operation

Lumi works without the internet. It uses models on this computer (Ollama, EXO)
or an inference server your organization runs, and it can take updates and a
license from files. **Offline mode** makes sure of it: Lumi then reaches only
this computer and the hosts you allow, and anything else it would have
reached is refused at once, with the reason.

Offline mode is for air-gapped networks and for organizations that allow no
traffic to the internet. It works without an [offline license](#the-offline-license),
like the rest of Lumi.

## Turning it on

**Settings > Offline mode** (under Security):

| Setting | Values | Effect |
|---|---|---|
| **Offline mode** (`offline.enabled`) | off (default), on | Lumi connects only to this computer and the allowed hosts. |
| **Allowed hosts** (`offline.allowed_hosts`) | one per line | Hosts Lumi may reach besides this computer. |

It applies at once: the next request Lumi makes is checked, also in a turn
that's already running. The page shows what it does now: the hosts Lumi
reaches, the providers it hides from Models and why, whether update checks
continue, and the offline license.

An allowed host is one of:

- a name: `llm.corp.example`;
- every name under a domain: `*.corp.example` (not `corp.example` itself; list
  that too if you need it);
- an address: `10.20.0.5`, `fd00::5`;
- a network: `10.20.0.0/16`.

Leave out the scheme and port; a pasted URL keeps only its host. `*`, a whole
top-level domain (`*.com`) and `0.0.0.0/0` are refused: they would turn
offline mode off.

This computer is always reachable: `localhost`, the loopback addresses
(`127.0.0.0/8`, `::1`), `0.0.0.0` and `::`, and this computer's own name.
Lumi decides that from the name as written and never looks it up, so
`localhost.example.com` or `127.0.0.1.nip.io` is another computer, whatever it
resolves to. Another computer on your network, such as an Ollama server at
`10.0.0.131`, is reachable only when it's allowed.

## What works offline

- Ollama and EXO on this computer, as always.
- Ollama or EXO on another computer, and an OpenAI-compatible server your
  organization runs (vLLM, LiteLLM, a gateway): add it under
  **Settings > Connections** or **Network**, and allow its host. A connection
  that signs in (OAuth, Microsoft Entra ID) needs its token endpoint allowed
  too, such as `login.microsoftonline.com`. Entra ID without a client ID signs
  in through azure-identity or the Azure CLI, which connect by themselves, so
  it works only when `login.microsoftonline.com` (or the host in
  `AZURE_AUTHORITY_HOST`) is allowed.
- Everything that stays on this computer: sessions, file editing, the agent's
  tools, language servers, checks and previews, project notes, capability
  packs already installed and approved, the audit log, usage records and
  budgets.
- The team library as last synced, and hand-offs already picked up.
- Lumi's browser, for pages on this computer and allowed hosts.
- Updates and a license, from files (below).

If Lumi reaches allowed hosts through a proxy (Settings > Network), allow the
proxy's host too. Git, which runs as its own program, uses the proxy its own
settings name without a check.

## What offline mode refuses

Each refusal says `Offline mode: <feature> needs <host>; allow it or turn
offline mode off.` When your organization's policy turns offline mode on, the
message says to ask your administrator; when that policy can't be used, it
says offline mode stays on until the administrator fixes it.

| Feature | What happens |
|---|---|
| Models from cloud providers (Anthropic, OpenAI, OpenRouter, Kimi, SONN) and connections to hosts that aren't allowed | Hidden from Models, with the reason. A saved conversation that uses one refuses its next turn; fallback models that can't be reached are skipped. |
| Codex and Claude Code | Hidden and refused even when their hosts are allowed: they run as their own programs, whose connections Lumi can't check. |
| Providers from capability packs (extensions) | Hidden and refused for the same reason. |
| Lumi Cloud: sign-in, check-ins, sharing, the team library, hand-offs, reviews, second approvals, tasks from chat | Refused unless your Lumi Cloud's host is allowed. Sign-in says so before opening the browser. |
| [Sending feedback](feedback.md) | Refused unless the feedback address's host is allowed, before the report is prepared: no diagnostics gathered, no DLP check, and **Copy to clipboard** gives only what was typed. With no address set, refused rather than kept to send later. Reports already waiting stay on this computer until they may go, and the dialog doesn't ask the address who reads its reports. |
| Update checks and downloads | WinSparkle doesn't start, and turning offline mode on stops it at once, unless the update site (`luminary-analytics.github.io`) is allowed. **Check for updates** says why. Install updates from a file instead. |
| The agent's browsing (`browser_navigate`, new tabs) | Refused for other hosts, and the model is told why. A `file:` address that names another computer (`file://host/share/…`, `file:////host/share`), which Chrome on Windows opens as a network share, is refused too; this computer's files open. Lumi's Chrome starts with switches that send everything else to a closed port on this computer, so pages' own requests and scripts can't reach other hosts either, link-local addresses included. Turning offline mode on, or changing the allowed hosts, closes the Chrome Lumi started under other rules at once; the next browser tool starts it again. A Chrome that Lumi didn't start isn't used while offline mode is on. |
| Opening an address with computer use (`open_application`) | Refused: the default browser or another program would reach it unchecked. |
| [Panels from capability packs](extensions.md) | Stay closed, and View says why: a panel has no network except WebRTC, which browsers don't let a page's policy turn off and offline mode can't check. Turning offline mode on in Settings closes an open panel at once; when a policy turns it on, the panel's files stop loading at once and View stops offering it the next time Lumi lists panels. |
| Pull requests (GitHub, GitLab, Bitbucket, Azure DevOps) and issue trackers (Jira, Linear) | Refused unless their host is allowed, including the push before a pull request opens: every address the remote pushes to, network shares (`\\host\share`, `//host/share`) and `file://` remotes on other computers included. |
| The OpenTelemetry export of the audit log | Records aren't sent to a collector that isn't allowed; Settings > Privacy & security shows why. The local audit log carries on. |
| MCP servers over HTTP | Don't connect unless their host is allowed; the MCP status shows why. |
| Installing a capability pack from Git (and from your organization's registry) | Refused before Git starts, unless the repository's host is allowed, both as given and as Git's own settings rewrite it (`url.<base>.insteadOf`). Git doesn't follow redirects then. |
| Dictation | The window's own speech recognition is off: it sends audio to Google, Microsoft or Apple, from code Lumi can't see. A transcription service works when its host (and any sign-in host) is allowed. |
| Microsoft Entra ID sign-in without a client ID | Refused unless `login.microsoftonline.com` (or `AZURE_AUTHORITY_HOST`'s host) is allowed, before azure-identity or the Azure CLI starts; the model picker hides such a connection with the reason. |
| The Team preview | Can't start or change team work while offline mode is on: its workers don't follow offline mode yet. Viewing and stopping still work. |

## Where it is enforced

- Lumi's HTTP clients are built with `net.client_options`, which checks each
  request, redirects included, before it connects (`lumi/offline.py`): Lumi
  Cloud, model requests to Anthropic, OpenAI and every OpenAI-compatible
  endpoint (connections, EXO, SONN, OpenRouter, Kimi), model lists of
  connections, sign-in token endpoints, pull requests, issue trackers, HTTP MCP
  servers, dictation and the OpenTelemetry export.
- Where an address leaves Lumi's process, Lumi checks it before handing it
  over: Git for packs and pull-request pushes, the update feed WinSparkle
  reads, the page Lumi Cloud sign-in opens, Azure's own sign-in, and Lumi's
  Chrome (`--proxy-server` to a closed port, a bypass list of this computer
  and the allowed hosts starting with `<-loopback>`, which removes Chrome's
  own direct route to link-local addresses, and no WebRTC UDP around the
  proxy).
- A model turn, and each auxiliary request (titles, summaries), is refused
  before anything is sent when its provider, or where it signs in, can't be
  reached, and discovery doesn't probe providers that can't be. The refusal
  comes before your organization's [DLP rules](dlp.md) look at the request,
  so nothing is checked or recorded for it; a reachable provider's requests
  pass DLP as usual. The terminal UI doesn't ask an unreachable provider
  whether to plan either.
- As a backstop for Lumi's Python process, once offline mode has been on, a
  host name lookup through Python's socket module (`getaddrinfo`,
  `gethostbyname`, and the reverse lookups `gethostbyaddr` and `getnameinfo`)
  for anything else fails at once, and so does a connection or datagram to a
  host given by name. That covers the remaining clients, such as Ollama's own
  API calls, provider catalogs, the chat gateway and Engram, with a generic
  message ("a network connection needs …"). Their requests look up the host
  first, IP addresses included; an asynchronous connection straight to an IP
  address is checked only by Lumi's own clients. It doesn't see native code
  that resolves names by itself, or other programs.
- A name that isn't a plain host name (control characters, spaces, `%`, `/`,
  `@`) never matches an allowed host, so a resolver or a program that decodes
  it can't reach somewhere else.

**Not covered yet.**

- Programs that run as their own processes: commands the agent runs (its
  shell, jobs, previews and checks), MCP servers started as commands, hooks,
  and language servers. Use your firewall, or the machine's network policy,
  for those. The shell sandbox doesn't cut off the network either.
- Computer use drives your desktop: an app it opens or types into (another
  browser, say) connects wherever it's told. `open_application` refuses
  addresses, but turn computer use off where that matters.
- What was already running when offline mode turned on: a model response
  that's streaming finishes, and Team workers keep running until they stop
  (the Team preview refuses new work).
- An organization's Chrome proxy policy (Group Policy or MDM `ProxySettings`,
  `ProxyMode`) outranks the switches Lumi starts Chrome with, so Lumi's
  browser follows that policy instead. A page on this computer that Lumi's
  browser opens can still refer to files on network shares
  (`file://host/…`), which Windows opens by itself.
- Git uses the proxy its own settings name (`http.proxy`), unchecked.

## For administrators

Lock offline mode in the organization policy's `settings` (see
[Organization policy](enterprise-policy.md)):

```json
"settings": {
  "offline.enabled": true,
  "offline.allowed_hosts": ["llm.corp.example", "10.20.0.0/16"]
}
```

- When the policy turns offline mode on, only the hosts the policy allows are
  reachable; hosts people list in Settings don't apply. With no
  `offline.allowed_hosts`, only each computer itself is reachable.
- A policy whose values Lumi can't apply (a host list that isn't a list, `*`,
  a whole top-level domain such as `*.com`, a network such as `0.0.0.0/0`,
  `"true"` as text) is invalid as a whole, like any invalid policy. A policy
  that exists but can't be used, for that or any other reason (it isn't JSON,
  its signature doesn't verify), keeps offline mode on with no allowed hosts,
  whatever people set, until you fix it: a mistake in it never reads as
  "offline mode off". Lumi refuses model requests then too.
- Allow your Lumi Cloud's host if computers should keep checking in and
  receiving policy.
- Set `updates.mode` to `off` as well if you deploy new versions yourself.

## Updates from a file

On a computer without the internet, bring the update over by hand:

1. On a connected computer, download from the update site the installer
   (`lumi-setup-X.Y.Z.exe`) and the feed that lists it: `appcast.xml`, or
   `appcast-beta.xml` or `appcast-X.Y.xml` for the beta channel or a release
   line. Keep them in one folder, or put both in a .zip.
2. On the offline computer, open **Settings > Updates > Install an update from
   a file**, give the installer, the folder or the .zip, and choose **Check**.
3. When it checks out, choose **Install and restart**.

Lumi checks the file as the online updater checks a download:

- the feed lists the installer, with its size and `sparkle:edSignature`;
- the installer's bytes carry a valid EdDSA (Ed25519) signature by the key
  built into Lumi (`updater.EDDSA_PUBLIC_KEY`, the key WinSparkle uses), so a
  changed installer, or one that isn't a Lumi release, is refused;
- the version is the one inside the signed installer (its Windows version
  resource, `ProductVersion`, which the release build sets), and the feed
  must give the same one. The feed isn't signed, so an older signed installer
  that an edited feed lists as a newer version is refused;
- the size matches, and the version is newer than this copy and one your
  update settings would take (the beta channel for betas; the release line
  you pin);
- the feed doesn't declare a document type or entities, in any encoding.

With updates off, or on a copy installed from the MSI, the PKG, a .deb or an
.rpm, a file is refused too: your organization or package manager updates
Lumi. Installing never happens while an agent turn runs. Lumi runs a copy of
exactly the bytes it checked, saved as `lumi-setup-<version>.exe` in a new
private folder whatever the bundle calls it, records `update.install` in the
audit log with the installer's SHA-256, and closes so the installer can
replace its files. Windows asks for administrator rights, as it does for a
downloaded update.

`lumi updates verify <installer, folder or .zip>` runs the same check from the
command line and prints the result as JSON, without installing. It also works
on macOS and Linux, for checking a bundle before carrying it to a Windows
computer.

**macOS and Linux.** Lumi doesn't update itself there (see
[Updates](updates.md)), so it doesn't install from a file either; the Settings
page says what to do instead. Install the new PKG with your device management
or `sudo installer -pkg lumi-X.Y.Z.pkg -target /` (check it first with
`pkgutil --check-signature`), or the new .deb or .rpm with `apt` or `dnf`, or
replace the AppImage or tarball. A copy running from source can verify a file
but not install it.

## The offline license

A license is a signed file that states the organization, the number of seats,
when it expires and whether it covers offline use. **Settings > Offline mode**
and `lumi license status` show it. In this first version the license only
records and labels your organization's offline entitlement: offline mode and
every other feature work without one, and nothing stops when it expires.

```json
{
  "license": {"schema": "lumi.license/v1", "license_id": "acme-2026-001", "organization": "Acme",
              "seats": 50, "offline": true, "issued_at": "2026-09-27T00:00:00Z",
              "expires_at": "2027-09-30T23:59:59Z"},
  "key_id": "luminary-2026",
  "signature": "<base64 Ed25519 signature>"
}
```

The signature covers the `license` object's canonical JSON (sorted keys, no
spaces, UTF-8), as for a [signed policy](enterprise-policy.md#signed-policies-and-offline-use).

**Where Lumi looks for the license,** first match wins:

1. `license.json` beside the machine policy file: `C:\ProgramData\Lumi\` on
   Windows (the ProgramData folder Windows reports, never the `ProgramData`
   environment variable), `/Library/Application Support/Lumi/` on macOS,
   `/etc/lumi/` on Linux;
2. the file `LUMI_LICENSE_FILE` names, when there is none there;
3. the copy `lumi license install <file>` keeps in `~/.lumi/license.json`.

The license's signature is what counts, so it can be anywhere.

**Which keys it trusts.** A license verifies only against keys built into Lumi
(Luminary Analytics' license-signing keys) or keys an administrator sets, each
a JSON object such as `{"luminary-2026": "<base64 public key>"}`:

- Windows: `LicenseKeys` under `HKLM\SOFTWARE\Policies\Luminary Analytics\Lumi`
  (the ADMX template has it; Group Policy or Intune sets it). A
  `license-keys.json` file isn't read on Windows: any user can create
  `C:\ProgramData\Lumi` where no administrator did, so a file there isn't
  necessarily an administrator's.
- macOS: the `LicenseKeys` key of the configuration profile, or
  `license-keys.json` in `/Library/Application Support/Lumi/`.
- Linux: `/etc/lumi/license-keys.json`.

Keys in your own folders or environment are never trusted. No production key
is built into this version yet, so for now an administrator installs
Luminary's public key in one of those places.

**Commands:**

- `lumi license status` (`--json` for scripts) shows the license in force;
- `lumi license verify <file>` checks a file without installing it;
- `lumi license install <file>` checks it and keeps a copy for Lumi.

### Signing licenses (Luminary Analytics)

`scripts/sign_license.py` makes keys and licenses with the standard library
and `cryptography`:

```sh
# Once: a signing key. Keep the private key offline; the command prints the public entry.
python scripts/sign_license.py keygen --out luminary-license.pem --key-id luminary-2026
# A license for an organization.
python scripts/sign_license.py sign --key luminary-license.pem --key-id luminary-2026 \
    --organization "Acme" --seats 50 --expires 2027-09-30 --offline --out acme.lumi-license.json
# The public entry again, and a check of a license against it.
python scripts/sign_license.py public-key --key luminary-license.pem --key-id luminary-2026
python scripts/sign_license.py verify acme.lumi-license.json --public-key <base64> --key-id luminary-2026
```

`--expires` takes a date (the end of that day, UTC) or an ISO 8601 time. The
script checks each license it signs as Lumi will. Put the public entry in
`BUILTIN_KEYS` in `lumi/license.py` for a release, or give it to the
organization's administrator for `LicenseKeys` (Windows, macOS) or
`license-keys.json` (macOS, Linux).

## Status

Source only, not released. Covered by `tests/test_offline.py`,
`tests/test_offline_features.py`, `tests/test_update_file.py`,
`tests/test_license.py` and `tests/test_policy.py`, with mock transports and
fakes: no test reaches another computer (the backstop's lookups are refused
before they leave, or the hook is called directly). The installer version
reader was compared with Windows' own for installers built locally from
`packaging/installer.iss`. A real air-gapped installation, a real Chrome under
offline mode and installing a real signed installer from a file haven't been
exercised.
