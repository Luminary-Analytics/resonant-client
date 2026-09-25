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
| macOS | The `Policy` key of the `com.luminaryanalytics.lumi` managed preferences, from a configuration profile (`packaging/policy/lumi-policy.mobileconfig`) |
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
  "pricing": {"prices": {"anthropic:claude-opus-*": {"input": 3.2, "output": 16}}}
}
```

`packaging/policy/example-policy.json` is a complete example. Every section is
optional.

| Section | Effect |
| --- | --- |
| `settings` | `"section.key": value` pairs that override what people set, locked in Settings and refused by the app's settings commands. Useful keys: `general.default_permission_mode`, `privacy.secret_scan`, `privacy.transcript_retention_days`, `privacy.excluded_paths`, `security.cli_adapters`, `security.computer_use`, `security.chat_gateway`, `network.proxy_url`, `network.no_proxy`, `network.system_certificates`, `cost_tracking.budget_alert_usd`, and the [audit log](audit-log.md#for-administrators)'s `privacy.audit_log`, `privacy.audit_capture`, `privacy.audit_retention_days`, `audit.otlp_endpoint` and `audit.otlp_auth_header`. |
| `permissions.allowed_modes` | Which of `ask`, `auto-edit`, `plan` and `bypass` people may choose. Others are hidden, and a saved default outside the list becomes the first allowed mode. |
| `models.allowed`, `models.blocked` | `provider:model` patterns, for example `anthropic:*`, `ollama:qwen*` or `conn-gateway:*` for a custom connection. Blocked wins. Other models are removed from the model menu and refused if selected. |
| `files.exclude` | Gitignore-style patterns added to every project's file exclusions (see the README's *Keys, network and privacy*). |
| `shell.rules` | Execution-policy rules (`tool_pattern`, `action` allow, prompt or deny, `arg_patterns`, `reason`). They are checked before Lumi's built-in and repository rules, so nothing can loosen them. |
| `mcp.allowed_servers`, `mcp.allow_stdio` | MCP server name patterns that may connect; `allow_stdio: false` refuses command-based servers. |
| `extensions.allowed_packs` | Capability pack id patterns; other packs stay off even if approved. |
| `pricing.prices` | Negotiated prices in USD per million tokens by `provider:model` pattern (`input`, `output`, optional `cached_input` and `cache_write`). They win over users' prices and Lumi's list; see [usage records and prices](usage-and-costs.md). |

Patterns use `*` and `?` wildcards.

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

## Group Policy and Intune

`packaging/policy/lumi.admx` and `packaging/policy/en-US/lumi.adml` define
three machine policies under **Lumi** in the Group Policy editor:

- **Organization policy:** the JSON document, stored in the `Policy` value (REG_MULTI_SZ lines are joined).
- **Organization policy file:** a path, stored in `PolicyFile`.
- **Trusted policy signing keys:** stored in `PolicyKeys`.

Copy the ADMX to `%SystemRoot%\PolicyDefinitions` or the central store, and
the ADML to its `en-US` folder. For Intune, import the ADMX as a custom
administrative template, or set the registry values with a script.

## What policy doesn't cover yet

- Codex and Claude Code run their own tool loops. Turn them off with
  `security.cli_adapters: false` if their behavior must follow this policy.
- Shell commands can still read files that `files.exclude` names. Add shell
  rules for sensitive paths.
- Policy isn't yet fetched from Lumi Cloud, and policy loads aren't in the
  [audit log](audit-log.md) yet. Both are planned with the admin portal.
