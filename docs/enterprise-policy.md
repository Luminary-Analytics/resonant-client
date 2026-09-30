# Organization policy for administrators

Lumi reads an organization policy from places only administrators can write.
It locks settings, limits permission modes and models, adds files the agent
must never read, adds shell rules, and allowlists MCP servers and capability
packs. The client enforces it (`lumi/policy.py`); people see locked settings as
"Managed by <organization>" in **Settings > Privacy & security**, which also
summarizes the active policy.

Status: source only, not released. Lumi Cloud will deliver signed policies
later; today a policy is installed on each machine.

## Where Lumi looks

Lumi uses the first of these that exists:

| Platform | Location |
| --- | --- |
| Windows | Registry `HKLM\SOFTWARE\Policies\Luminary Analytics\Lumi`: value `Policy` (the JSON document) or `PolicyFile` (a path). In the path, `%ProgramData%`, `%ALLUSERSPROFILE%`, `%ProgramFiles%`, `%SystemRoot%`, `%windir%` and `%SystemDrive%` expand to the folders Windows reports; other variables, which each person can set, stay as written, so such a path isn't a full path and the policy fails closed. Set them with the ADMX template below, Intune or any registry tool. The file `PolicyFile` names must pass the [file rules](#only-files-only-administrators-can-change-count); if it can't be read, or doesn't pass, Lumi refuses model requests. |
| Windows | `C:\ProgramData\Lumi\policy.json`: the ProgramData folder Windows reports, never the `ProgramData` environment variable, which a person could point at a folder of their own. The folder must be locked down: the MSI creates it that way, and the [icacls recipe](#locking-down-a-policy-folder-on-windows) does it for Group Policy and Intune. |
| macOS | The `Policy` key of the `com.luminaryanalytics.lumi` managed preferences, from a device-scope configuration profile (`packaging/policy/make_mobileconfig.py` makes one; see [Deploying on macOS](deploy-macos.md)) |
| macOS | `/Library/Application Support/Lumi/policy.json` |
| Linux | `/etc/lumi/policy.json` |

`LUMI_POLICY_FILE` names a policy file for pilots and CI. It is read **only
when none of the locations above has a policy**, so people can't replace their
organization's policy with their own.

Lumi reads the policy when it starts. Restart it after changing the policy.
`lumi policy` prints the policy in force as JSON: where it came from, why it
can't be used, and every file Lumi ignored (see
[Checking a computer](deploy-windows.md#checking-a-computer)).

## Only files only administrators can change count

Machine policy comes only from places only administrators can write. The
registry values and a configuration profile's keys are an administrator's by
construction. A file counts only when it, and every folder above it up to a
folder the operating system protects, can't be changed by anyone else:

- **Windows** (the file, and each folder up to ProgramData for a file under
  it, otherwise up to the drive's or the network share's root):
  - the owner is SYSTEM, Administrators or TrustedInstaller. On a network
    share named by its UNC path (`\\server\share\...`, not a mapped drive
    letter), the Domain Admins and Enterprise Admins of the domain this
    computer belongs to count too, never another domain's;
  - no permission entry lets anyone else write, append (in a folder: add files
    or folders), change attributes, delete, delete what's inside, change
    permissions or take ownership. That includes the entry every folder made
    under ProgramData inherits, `BUILTIN\Users:(CI)(WD,AD,WEA,WA)`, which lets
    any user add files to a folder an administrator created. Entries for what
    is created later (inherit-only) and deny entries don't count;
  - ProgramData itself, or the drive's or share's root, may let people add
    folders, but not delete what's in it or change its permissions or owner;
  - no symbolic link or junction on the way, the root included;
  - the path names a file, not an alternate data stream
    (`policy.json:other`).
- **macOS and Linux:** the file and each folder up to `/Library/Application
  Support`, `/Library` (for the configuration profile) or `/etc` are owned by
  root and not writable by their group or others.

Any user may create `C:\ProgramData\Lumi` where no administrator did, and a
folder an administrator creates there inherits that `(CI)(WD,AD)` entry, so
neither is enough. A file that fails is **ignored, never silently**:
Settings > Privacy & security > Organization policy says "Policy file ignored:
writable by non-administrators" with the file and the reason, `lumi policy`
lists it, and the [audit log](audit-log.md) records `policy.file_ignored`.
What happens next depends on whose it is:

- **A file someone other than an administrator owns**, or one in a folder
  someone else owns (as one a person planted would be), reads as if it
  weren't there: the sources below it apply.
- **A file an administrator put there** (it and its folder are an
  administrator's) **in a place others can change**, and **the file
  `PolicyFile` names**, fail closed: an administrator meant a policy to
  apply, so Lumi refuses model requests until the folder is locked down.

The same rules apply to `license.json` beside the machine policy file (an
[offline license](offline.md#the-offline-license)) and, on macOS and Linux, to
`policy-keys.json` and `license-keys.json`.

### Locking down a policy folder on Windows

The MSI creates `%ProgramData%\Lumi` locked: owned by Administrators, full
control for SYSTEM and Administrators, read and execute for Users, nothing
inherited ([Deploying on Windows](deploy-windows.md#the-msi)). With Group
Policy (a computer startup script) or Intune (a platform script), which run as
SYSTEM, create it the same way before copying the policy in:

```powershell
$dir = Join-Path $env:ProgramData 'Lumi'
# Anything already there may be someone else's: start over, then copy all your files in.
if (Test-Path -LiteralPath $dir) {
    icacls.exe $dir /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
    icacls.exe $dir /reset /T /C /Q | Out-Null
    Remove-Item -LiteralPath $dir -Recurse -Force
}
New-Item -ItemType Directory -Path $dir | Out-Null
icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX'
Copy-Item -LiteralPath .\policy.json -Destination $dir
icacls.exe $dir /setowner '*S-1-5-32-544' /T
```

From `cmd.exe`, the two `icacls` lines are:

```
icacls "%ProgramData%\Lumi" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX
icacls "%ProgramData%\Lumi" /setowner *S-1-5-32-544 /T
```

The well-known SIDs (SYSTEM, Administrators, Users) work in every language.
Setting the owner last covers files copied by an administrator account that
owns what it creates (the "Object creator" default-owner setting): a file
owned by a person's account, even an administrator's, doesn't count.
For a `PolicyFile` on a file server, name it by its UNC path and give its
folder, and every folder above it in the share, the same shape:
administrators (this domain's Domain Admins may stay) with full control,
everyone else read at most; the share's root may let people add files, but
not delete them or change permissions. Then check with `lumi policy`.

## The document

```json
{
  "schema": "lumi.policy/v1",
  "organization": "Example Corp",
  "issued_at": "2026-09-25T00:00:00Z",
  "settings": {
    "general.default_permission_mode": "ask",
    "privacy.secret_scan": true,
    "privacy.transcript_retention_days": 30,
    "security.cli_adapters": false,
    "security.computer_use": false,
    "security.chat_gateway": false
  },
  "permissions": {"allowed_modes": ["ask", "auto-edit", "plan"]},
  "models": {"allowed": ["anthropic:*", "conn-*:*"], "blocked": ["openrouter:*"]},
  "files": {"exclude": [".env", "*.pem", "secrets/**"]},
  "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                       "arg_patterns": {"command": "\\b(curl|wget)\\b"},
                       "reason": "No downloads from the agent's shell"}]},
  "mcp": {"allowed_servers": ["github", "docs-*"], "allow_stdio": false},
  "extensions": {"allowed_packs": ["example-*"]},
  "pricing": {"prices": {"anthropic:claude-opus-*": {"input": 3.2, "output": 16}}},
  "budgets": [{"scope": "user", "period": "month", "warn_usd": 200, "block_usd": 400}]
}
```

`packaging/policy/example-policy.json` is a complete example. Every section is
optional.

| Section | Effect |
| --- | --- |
| `settings` | `"section.key": value` pairs that override what people set, locked in Settings and refused by the app's settings commands. Useful keys: `general.default_permission_mode`, `privacy.secret_scan`, `privacy.transcript_retention_days`, `privacy.excluded_paths`, `security.cli_adapters`, `security.computer_use`, `security.chat_gateway`, `security.scheduled_tasks` (see [scheduled tasks](scheduled-tasks.md#for-administrators)), `security.editor_bridge` (VS Code and JetBrains reaching Lumi; see [code editors](code-editors.md)), `cloud.remote_tasks` (tasks from Slack and Teams; see [Lumi Cloud](lumi-cloud.md#tasks-from-slack-and-teams)), `security.shell_sandbox` (`"off"` or `"project"`; see [shell sandbox](shell-sandbox.md)), `security.extension_panels` (`false` turns off capability packs' panels; see [Panels](extensions.md#panels)), `code_hosts.github_hosts` and `code_hosts.gitlab_hosts` (lists of the GitHub Enterprise Server and self-managed GitLab hosts that may receive those tokens, besides github.com and gitlab.com; see [which hosts get the token](github.md#which-hosts-get-the-token)), `swarming.enabled` (`false` keeps the [Team preview](swarming.md#under-an-organization-policy) off), `network.proxy_url`, `network.no_proxy`, `network.system_certificates`, `cost_tracking.budget_alert_usd`, the [audit log](audit-log.md#for-administrators)'s `privacy.audit_log`, `privacy.audit_capture`, `privacy.audit_retention_days`, `audit.otlp_endpoint` and `audit.otlp_auth_header`, and [updates](updates.md#for-administrators)' `updates.mode` (`automatic`, `manual` or `off`), `updates.channel` (`stable` or `beta`) and `updates.pin` (a release line such as `0.20`), and [dictation](voice-input.md#for-administrators)'s `voice.engine` (`off` turns it off), `voice.service`, `voice.model` and `voice.language`, and [offline mode](offline.md#for-administrators)'s `offline.enabled` (`true` or `false`) and `offline.allowed_hosts` (a list of names, `*.domain`, addresses and networks such as `10.20.0.0/16`; `*`, a whole top-level domain such as `*.com` and `0.0.0.0/0` make the policy invalid). When the policy turns offline mode on, only its `offline.allowed_hosts` apply, not hosts people list in Settings. A policy that exists but can't be used keeps offline mode on with no allowed hosts until it's fixed. |
| `permissions.allowed_modes` | Which of `ask`, `auto-edit`, `plan` and `bypass` people may choose. Others are hidden, and a saved default outside the list becomes the first allowed mode. The [terminal UI](terminal-ui.md) starts in the default permission mode from Settings, as the app does, and in the first allowed mode it has when that one isn't allowed. Plans, missions (**Build this roadmap**) and autonomous sessions run unattended in Full-auto (`bypass`), so without it they don't start, even when a person chooses to run one in Full-auto, and one already running stops at its next step (see [orchestration specialists](modern-agent-runtime.md#orchestration-specialists)). A [team](swarming.md#under-an-organization-policy) with writers needs `auto-edit` or `bypass`, and a team the orchestrator runs needs `bypass`, whether or not it applies checked changes; a read-only team the owner reviews runs under any modes. |
| `models.allowed`, `models.blocked` | `provider:model` patterns, for example `anthropic:*`, `ollama:qwen*` or `conn-gateway:*` for a custom connection. Blocked wins. Other models are removed from the model menu and refused if selected. |
| `files.exclude` | Gitignore-style patterns added to every project's file exclusions (see the README's *Keys, network and privacy*). |
| `shell.rules` | Execution-policy rules (`tool_pattern`, `action` allow, prompt or deny, `arg_patterns` regular expressions or `arg_globs` wildcards per argument, `reason`). They are checked before Lumi's built-in and repository rules, so nothing can loosen them, including a repository `lumi-policy.json` with mistakes. An `allow` here doesn't skip an approval prompt: only a trusted repository's own `allow` rule does that, in Auto-edit, when no rule here denies the call or asks about it. A `prompt` here needs a person's answer: where nobody can be asked, as in `lumi run`, a scheduled task or a step of a plan, the call is refused, and a person's own `permission_request` hook can't answer it for them. A rule Lumi can't apply as written, such as `arg_patterns` that isn't an object of regular expressions, makes the whole policy invalid (see [When something is wrong](#when-something-is-wrong)). |
| `mcp.allowed_servers`, `mcp.allow_stdio` | MCP server name patterns that may connect; `allow_stdio: false` refuses command-based servers. |
| `extensions.allowed_packs` | Capability pack id patterns; other packs stay off even if approved. |
| `extensions.trusted_publishers` | Capability pack publishers the organization trusts: a list of `{"name", "public_key"}`, where the key is the base64 Ed25519 public key from `lumi extension keygen`. Packs they signed show under that name. See [Signing a pack](extensions.md#signing-a-pack). |
| `extensions.require_signed` | `true` turns off every capability pack that one of `trusted_publishers` didn't sign, whatever people approved. Publishers a person trusts themselves don't count. |
| `extensions.registry` | Packs the organization approves: a list of `{"id", "name", "url", "commit", "subdir", "digest"}`, where `url` is a public https repository, `commit` has 40 hex digits, and `subdir` and `digest` are optional (`digest` is what `lumi extension check` prints). Settings > Capability packs offers to install them at that commit. Lumi Cloud's Extensions page writes it. See [the registry](extensions.md#your-organizations-registry). |
| `extensions.registry_only` | `true` turns off every pack that isn't in `extensions.registry`, or isn't at its pinned version. |
| `extensions.allowed_sources` | Repository URL patterns that packs may be installed from (Settings > Capability packs > Install from Git), for example `https://github.com/example-corp/*`. Other repositories are refused. |
| `models.require_zero_retention`, `models.zero_retention_providers` | With `true`, only providers that keep no data are allowed: local Ollama and EXO models (not Ollama's `-cloud` models), connections marked **This endpoint keeps no prompts or responses**, and the providers listed, such as `["anthropic"]` for an organization with a zero data retention agreement. Other models are removed from the model menu and refused. |
| `models.capabilities` | Stated capabilities per model pattern (context window, vision, tools, reasoning, computer use, concurrency) that win over inference and provider reports. See [capability overrides](models.md#capability-overrides-for-administrators). |
| `budgets` | Spending rules per user, project or turn: an alert, a question before continuing, and a stop (`warn_usd`, `approve_usd`, `block_usd`), plus `block_unpriced`. See [budgets](usage-and-costs.md#budgets). |
| `pricing.prices` | Negotiated prices in USD per million tokens by `provider:model` pattern (`input`, `output`, optional `cached_input` and `cache_write`). They win over users' prices and Lumi's list; see [usage records and prices](usage-and-costs.md). |
| `oversight` | Share work with the organization's Lumi Cloud: `version` (1), `activity` (each turn's metadata), `messages` (`off`, `redacted` or `full`, with the session's title; secrets always removed), `security_flags`, `retention_days` (1 to 3650), `notice` (the organization's words), `project_paths` and `unattended` (`record`, the default, or `block`: what a scheduled task or a `lumi run` with no interactive terminal does while nobody confirmed the notice as that computer user; `record` runs it, prints the notice with its output and records it as that user and computer, `block` refuses it). Off unless set. People see a notice naming the organization and what it receives, and nothing is sent to a model until they confirm it (a signed record goes to Lumi Cloud). A key or version Lumi doesn't know turns oversight off, with the reason in Settings, and the rest of the policy still applies. See [organization oversight](organization-oversight.md). |
| `dlp` | Data loss prevention rules checked on everything sent to a model provider: `version` (`1`), built-in `detectors` (`credit_card`, `us_ssn`, `iban`, `secrets`, `email`), keyword and pattern `rules`, each `flag`, `redact` or `block` with an optional `scope`, and an optional external `service`. See [data loss prevention](dlp.md). |

Patterns use `*` and `?` wildcards (the `dlp` section's `pattern` rules are
regular expressions).


## Commands a second person approves

`"approvals": {"commands": ["git push --force*", "terraform apply*"], "wait_minutes": 30}`
holds those commands until someone else in the organization approves them in
Lumi Cloud ([second-person approval](second-approval.md)). Patterns match the
whole command (`fnmatch`); `wait_minutes` is 1 to 240. Lumi Cloud's Policy
page writes this section.

## Lumi Cloud

A machine policy with a `cloud` section enrolls the computer in your
organization's Lumi Cloud with an enrollment token. Lumi then applies the
policy you publish there, signed with your organization's key:

```json
{
  "schema": "lumi.policy/v1",
  "organization": "Acme",
  "cloud": {"url": "https://cloud.example.com", "organization_id": "org_…", "enrollment_token": "lce_…"},
  "trusted_keys": {"acme-20260925-1a90d1": "<base64 Ed25519 public key>"}
}
```

The cloud policy replaces the machine policy's own rules once it arrives and
verifies against `trusted_keys` (or `PolicyKeys`, or on macOS and Linux
`policy-keys.json`). Until then, and whenever it doesn't verify, the machine
policy's rules apply. Lumi Cloud's Devices page prints this file when you
create an enrollment token. See [Lumi Cloud](lumi-cloud.md).

## Signed policies and offline use

A policy can be signed with Ed25519 so it can be distributed by less trusted
channels, and so Lumi Cloud can deliver it later:

```json
{"policy": { "schema": "lumi.policy/v1", "...": "..." },
 "signature": "<base64 signature of the policy's canonical JSON>",
 "key_id": "acme-2026"}
```

The signature covers the `policy` object serialized with sorted keys and no
whitespace (`lumi.policy.canonical`). Lumi accepts it only if `key_id` names a
key the machine trusts. Only an administrator can set these keys:

- Windows: the registry value `PolicyKeys` (Group Policy, Intune). A
  `policy-keys.json` file isn't read on Windows; Settings says so if one is
  there. These keys decide which downloaded policy may replace the machine's,
  and a registry policy value can only be an administrator's, while a file
  under ProgramData is theirs only as long as its folder stays locked down;
- macOS: the configuration profile's `PolicyKeys`;
- macOS and Linux: `policy-keys.json` beside the machine policy file, when it
  passes the [file rules](#only-files-only-administrators-can-change-count);
- everywhere: the `trusted_keys` of an unsigned machine policy.

Each maps key ids to base64 Ed25519 public keys.

A signed policy should carry `expires_at` (ISO 8601) and may set `grace_days`
(default 7). After `expires_at`, Lumi keeps enforcing the policy for the grace
period so people can work offline. After that it refuses model requests until
a fresh policy is installed.

## When something is wrong

If a policy exists but is invalid, or a signature doesn't verify, Lumi does not
fall back to "no policy". It refuses model requests, and Settings shows the
error, until the policy is fixed. The same happens after an expired policy's
grace period, and when the file Group Policy's `PolicyFile` names can't be
read (a share out of reach, a missing file, a path that isn't a full one) or
people other than administrators can change it: Lumi never runs with no
policy, or with `C:\ProgramData\Lumi\policy.json` or `LUMI_POLICY_FILE`,
instead. For laptops that leave the network, copy the policy to a local
folder you lock down, or put it in the `Policy` value itself. Lumi can't keep
a last good copy for you: it runs as the person, who can't write the folders
only administrators can.

Invalid includes a section that isn't an object, such as
`"permissions": "ask only"`, and a true-or-false value written as text, such
as `"allow_stdio": "no"`. It also covers a policy file that isn't UTF-8 text,
such as the UTF-16 that Windows PowerShell 5.1's `Out-File` writes by
default. UTF-8 with or without a byte order mark is fine, and a section that
is missing or `null` counts as empty.

A mistake in the `dlp` section is contained to it: an unknown key, an
unsupported `version` or a pattern Lumi won't run leaves the rest of the
policy in force, and model requests are refused, with the reason in the app
and in Settings, until the section is fixed. Without its rules Lumi can't know
what may leave the computer (see [data loss prevention](dlp.md#configuring-it)).

A downloaded Lumi Cloud policy that can't be used doesn't block requests. The
machine policy stays in force, or no policy for an organization someone
joined in the app, and Settings shows why. A downloaded policy that verifies
but has a `dlp` section Lumi can't use is in force, so it refuses requests
like a machine policy would.

## Group Policy and Intune

The MSI package can point Lumi at a policy file as it installs:
`msiexec /i lumi-X.Y.Z.msi /qn POLICYFILE="\\server\share\lumi-policy.json"`
sets the `PolicyFile` value below, and uninstalling removes it. It also
creates `%ProgramData%\Lumi` locked down. See
[Deploying on Windows](deploy-windows.md).

`packaging/policy/lumi.admx` and `packaging/policy/en-US/lumi.adml` define
four machine policies under **Lumi** in the Group Policy editor:

- **Organization policy:** the JSON document, stored in the `Policy` value (REG_MULTI_SZ lines are joined).
- **Organization policy file:** a path, stored in `PolicyFile`. The file must
  pass the [file rules](#only-files-only-administrators-can-change-count).
- **Trusted policy signing keys:** stored in `PolicyKeys`, the only place for
  them on Windows besides the machine policy's `trusted_keys`.
- **Trusted license signing keys:** stored in `LicenseKeys`, the keys an
  [offline license](offline.md#the-offline-license) may be signed with, besides
  those built into Lumi. On Windows this is the only place for them (a
  `license-keys.json` file isn't read there; on macOS and Linux it is, beside
  the machine policy file). The license itself can go in `license.json` beside
  the machine policy file.

Copy the ADMX to `%SystemRoot%\PolicyDefinitions` or the central store, and
the ADML to its `en-US` folder. For Intune, import the ADMX as a custom
administrative template, or set the registry values with a script.

On macOS, a configuration profile carries the policy (`Policy`) and, if
needed, the trusted signing keys (`PolicyKeys`) for Jamf Pro, Intune or
another MDM. `packaging/policy/make_mobileconfig.py` makes the profile from
your policy file and checks it first. See [Deploying on macOS](deploy-macos.md).

## What policy doesn't cover yet

- Codex and Claude Code run their own tool loops. Turn them off with
  `security.cli_adapters: false` if their behavior must follow this policy.
- Shell commands can still read files that `files.exclude` names. Add shell
  rules for sensitive paths.
- Policy isn't yet fetched from Lumi Cloud, and policy loads aren't in the
  [audit log](audit-log.md) yet, apart from files Lumi ignored
  (`policy.file_ignored`). Both are planned with the admin portal.
