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
| Windows | Registry `HKLM\SOFTWARE\Policies\Luminary Analytics\Lumi`: value `Policy` (the JSON document) or `PolicyFile` (a path; environment variables are expanded). Set them with the ADMX template below, Intune or any registry tool. |
| Windows | `%ProgramData%\Lumi\policy.json` |
| macOS | The `Policy` key of the `com.luminaryanalytics.lumi` managed preferences, from a device-scope configuration profile (`packaging/policy/make_mobileconfig.py` makes one; see [Deploying on macOS](deploy-macos.md)) |
| macOS | `/Library/Application Support/Lumi/policy.json` |
| Linux | `/etc/lumi/policy.json` |

`LUMI_POLICY_FILE` names a policy file for pilots and CI. It is read **only
when none of the locations above has a policy**, so people can't replace their
organization's policy with their own.

Lumi reads the policy when it starts. Restart it after changing the policy.

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
| `settings` | `"section.key": value` pairs that override what people set, locked in Settings and refused by the app's settings commands. Useful keys: `general.default_permission_mode`, `privacy.secret_scan`, `privacy.transcript_retention_days`, `privacy.excluded_paths`, `security.cli_adapters`, `security.computer_use`, `security.chat_gateway`, `security.scheduled_tasks` (see [scheduled tasks](scheduled-tasks.md#for-administrators)), `security.editor_bridge` (VS Code and JetBrains reaching Lumi; see [code editors](code-editors.md)), `cloud.remote_tasks` (tasks from Slack and Teams; see [Lumi Cloud](lumi-cloud.md#tasks-from-slack-and-teams)), `security.shell_sandbox` (`"off"` or `"project"`; see [shell sandbox](shell-sandbox.md)), `network.proxy_url`, `network.no_proxy`, `network.system_certificates`, `cost_tracking.budget_alert_usd`, the [audit log](audit-log.md#for-administrators)'s `privacy.audit_log`, `privacy.audit_capture`, `privacy.audit_retention_days`, `audit.otlp_endpoint` and `audit.otlp_auth_header`, and [updates](updates.md#for-administrators)' `updates.mode` (`automatic`, `manual` or `off`), `updates.channel` (`stable` or `beta`) and `updates.pin` (a release line such as `0.20`). |
| `permissions.allowed_modes` | Which of `ask`, `auto-edit`, `plan` and `bypass` people may choose. Others are hidden, and a saved default outside the list becomes the first allowed mode. |
| `models.allowed`, `models.blocked` | `provider:model` patterns, for example `anthropic:*`, `ollama:qwen*` or `conn-gateway:*` for a custom connection. Blocked wins. Other models are removed from the model menu and refused if selected. |
| `files.exclude` | Gitignore-style patterns added to every project's file exclusions (see the README's *Keys, network and privacy*). |
| `shell.rules` | Execution-policy rules (`tool_pattern`, `action` allow, prompt or deny, `arg_patterns` regular expressions or `arg_globs` wildcards per argument, `reason`). They are checked before Lumi's built-in and repository rules, so nothing can loosen them, including a repository `lumi-policy.json` with mistakes. An `allow` here doesn't skip an approval prompt: only a trusted repository's own `allow` rule does that, in Auto-edit, when no rule here denies the call or asks about it. A rule Lumi can't apply as written, such as `arg_patterns` that isn't an object of regular expressions, makes the whole policy invalid (see [When something is wrong](#when-something-is-wrong)). |
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

Patterns use `*` and `?` wildcards.


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
verifies against `trusted_keys` (or `PolicyKeys` / `policy-keys.json`). Until
then, the machine policy's rules apply. Lumi Cloud's Devices page prints this
file when you create an enrollment token. See [Lumi Cloud](lumi-cloud.md).

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

- the registry value `PolicyKeys`;
- `policy-keys.json` beside the machine policy file;
- the `trusted_keys` of an unsigned machine policy.

Each maps key ids to base64 Ed25519 public keys.

A signed policy should carry `expires_at` (ISO 8601) and may set `grace_days`
(default 7). After `expires_at`, Lumi keeps enforcing the policy for the grace
period so people can work offline. After that it refuses model requests until
a fresh policy is installed.

## When something is wrong

If a policy exists but is invalid, or a signature doesn't verify, Lumi does not
fall back to "no policy". It refuses model requests, and Settings shows the
error, until the policy is fixed. The same happens after an expired policy's
grace period.

Invalid includes a section that isn't an object, such as
`"permissions": "ask only"`, and a true-or-false value written as text, such
as `"allow_stdio": "no"`. It also covers a policy file that isn't UTF-8 text,
such as the UTF-16 that Windows PowerShell 5.1's `Out-File` writes by
default. UTF-8 with or without a byte order mark is fine, and a section that
is missing or `null` counts as empty.

A downloaded Lumi Cloud policy that can't be used doesn't block requests. The
machine policy stays in force, or no policy for an organization someone
joined in the app, and Settings shows why.

## Group Policy and Intune

The MSI package can point Lumi at a policy file as it installs:
`msiexec /i lumi-X.Y.Z.msi /qn POLICYFILE="\\server\share\lumi-policy.json"`
sets the `PolicyFile` value below, and uninstalling removes it. See
[Deploying on Windows](deploy-windows.md).

`packaging/policy/lumi.admx` and `packaging/policy/en-US/lumi.adml` define
three machine policies under **Lumi** in the Group Policy editor:

- **Organization policy:** the JSON document, stored in the `Policy` value (REG_MULTI_SZ lines are joined).
- **Organization policy file:** a path, stored in `PolicyFile`.
- **Trusted policy signing keys:** stored in `PolicyKeys`.

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
  [audit log](audit-log.md) yet. Both are planned with the admin portal.
