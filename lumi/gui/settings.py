"""
Persistent settings manager for Lumi.
Reads/writes ~/.lumi/settings.json with section-based access.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any
from ..paths import LEGACY_HOME_DIR_NAME, state_home
from ..secrets_store import PLACEHOLDER, SecretStore, credential_store_name

logger = logging.getLogger(__name__)

DEFAULTS = {
    "general": {
        "display_name": "",
        "show_companion": False,
        "default_backend": "",
        "default_model": "",
        # Models a turn continues with when a request fails, in order
        # ("provider:model"), and models for roles ("role provider:model").
        "fallback_models": [],
        "role_models": [],
        "default_permission_mode": "bypass",
        "theme": "dark",
        "max_model_requests": 0,
        # Sprint workflow (planner / generator / evaluator). Off by default — most
        # users want a plain agentic loop and never opt into the structured
        # planner/generator/evaluator pattern. When off, no .resonant-harness/
        # directory is created and the harness preamble is never injected.
        "harness_enabled": False,
        # Autonomous sessions (gui/autonomous_loop.py): experimental and off
        # by default; on shows the Autonomous button and the mission views.
        "autonomous_sessions": False,
        # Full-Autonomy tenet — concrete dials. The agent runs everything else
        # without asking; only these floor checks pause for explicit approval.
        # See lumi/orchestration/autonomy.py.
        "budget_usd_max": 5.00,
        "autonomy_protected_branches": ["main", "master", "prod", "production"],
        "autonomy_external_paths": [],   # empty → use the defaults from autonomy.py
        # On-screen glow and banner while the agent drives the mouse and
        # keyboard. Opt-*out*, deliberately: someone watching their own cursor
        # move needs to know it is Lumi, and making that visibility
        # something you must switch on inverts the default that matters.
        # Windows only; a no-op elsewhere.
        "computer_use_indicator": True,
    },
    "network": {
        # Empty means use OLLAMA_HOST or the local endpoint default.
        "ollama_url": "",
        # Empty means use EXO_API_URL/EXO_BASE_URL or the local EXO endpoint.
        "exo_url": "",
        # SONN requires an explicitly configured workspace/project API base URL.
        "sonn_url": "",
        # Corporate networks (lumi/net.py): an outbound proxy, hosts that bypass
        # it (local addresses always do), and TLS verification with the
        # operating system's certificate store rather than a bundled list.
        "proxy_url": "",
        "no_proxy": "",
        "system_certificates": True,
        # v0.4.4 (T1.4) — `resonant_api_url` and `remote_engine_ws_url`
        # were dropped here. Pre-v0.4.0 settings.json files that still
        # carry those keys load fine — Python dict tolerance ignores
        # unknown keys; nothing reads them anymore.
    },
    # Secrets are masked before settings are sent to the frontend.
    "api_keys": {"anthropic": "", "openai": "", "kimi": "", "openrouter": "", "sonn": "", "telegram_bot": "", "otlp": "",
                 "github": "", "gitlab": "", "bitbucket": "", "azure_devops": "", "jira": "", "linear": "", "slack_bot": "", "slack_app": ""},
    # Issue trackers (engine/issue_trackers.py). Jira's token is api_keys.jira;
    # with an email it's a Jira Cloud API token, without one a personal access
    # token for Jira Server or Data Center. Linear's key is api_keys.linear.
    "issue_trackers": {"jira_url": "", "jira_email": ""},
    # Custom model connections (gateways, Azure, Bedrock, Vertex); see lumi/connections.py.
    # Each one's key is stored in api_keys as conn_<id>.
    "connections": [],
    "project_models": {},
    "model_favorites": {"models": []},
    # Chat-channel gateway (`lumi gateway`, docs/chat-gateway.md): work with
    # the agent from Telegram or Slack. Only allowlisted chats are served.
    # It grants remote control, so this section and its tokens (api_keys
    # telegram_bot, slack_bot, slack_app) are edited in the file, never from
    # the Settings page (gui/ws_commands.py).
    "gateway": {
        "channel": "",            # "telegram" (default) or "slack"
        "project": "",            # default: the folder the gateway starts in
        "mode": "",               # "ask" (default), "auto-edit" or "bypass"
        "backend": "",
        "model": "",
        "allowed_chat_ids": [],   # Telegram chat IDs
        "slack_allowed": [],      # Slack channel or user IDs
        "approval_minutes": 10,
        "telegram_api_url": "",   # a self-hosted Bot API server; default api.telegram.org
    },
    "hooks": [],
    "mcp_servers": {
        # Deliberately empty. BrowserOS shipped as a default entry while
        # browsing required an external MCP server; browsing is native as of
        # v0.11.15, so the entry only produced a "tool server unavailable"
        # notice for a server nobody needed. Users who want BrowserOS — or any
        # other MCP server — add it from Settings → MCP Servers.
    },
    "lsp_servers": {},
    "plugins": {},
    "model_roles": {},
    "agent_runtime": {
        "persist": True,
        "worktree_writers": True,
        "max_parallel_readers": 4,
        "max_parallel_writers": 2,
    },
    "artifacts": {
        "persist": True,
        "inline_text_limit": 8000,
    },
    "keyboard_shortcuts": {},
    # Settings > Privacy. The scan removes well-known credential formats from
    # tool output and messages before a model request (lumi/secret_scan.py);
    # saved key values are removed from tool output either way.
    "privacy": {
        "secret_scan": False,
        # Gitignore-style patterns the agent never reads, lists or sends
        # (lumi/engine/exclusions.py), in addition to a project's .lumiignore.
        "excluded_paths": [],
        # Delete local transcripts and session logs this many days after
        # their last activity; 0 keeps them (lumi/gui/retention.py).
        "transcript_retention_days": 0,
        # The local audit log (lumi/audit.py): metadata only unless the capture
        # level is "redacted" or "full"; kept this many days.
        "audit_log": True,
        "audit_capture": "metadata",
        "audit_retention_days": 365,
    },
    # Live OpenTelemetry export of the audit records (OTLP/HTTP JSON). The
    # collector token, if any, is api_keys.otlp.
    "audit": {
        "otlp_endpoint": "",
        "otlp_auth_header": "Authorization",
    },
    # Tools that act outside Lumi's own tool loop. Organization policy can
    # turn them off for everyone; these are the local switches.
    "security": {
        "cli_adapters": True,     # Codex and Claude Code backends
        "computer_use": True,     # screenshots, mouse and keyboard control
        "chat_gateway": True,     # `lumi gateway` (Telegram)
        "scheduled_tasks": True,  # `lumi schedule`: unattended runs at set times
        "editor_bridge": True,    # VS Code and JetBrains reach Lumi (gui/editor_bridge.py)
        # "project": the agent's commands, jobs and previews run in an OS
        # sandbox that writes only to the project and temporary folders
        # (lumi/engine/os_sandbox.py, macOS and Linux).
        "shell_sandbox": "off",
    },
    "cost_tracking": {
        "enabled": True,
        # Budgets (lumi/budgets.py): warn past the alert, ask before going past
        # the daily limit, and stop a turn at the per-turn limit.
        "budget_alert_usd": None,
        "daily_limit_usd": None,
        "turn_limit_usd": None,
        # Your own prices (lumi/pricing.py): {"provider:model-glob": {"input": ..,
        # "output": .., "cached_input": .., "cache_write": ..}} in USD per million tokens.
        "price_overrides": {},
    },
    "engram": {
        "enabled": False,
        "server_url": "",
    },
    # The first-run checklist (lumi/gui/onboarding.py).
    "onboarding": {
        "dismissed": False,
        "first_task_done": False,
    },
    # Settings > Lumi account (lumi/cloud.py): the Lumi Cloud address, the
    # signed-in account and this computer's enrollment. The sign-in's refresh
    # token and the device key are secrets: they live in api_keys
    # (lumi_cloud_refresh, lumi_cloud_device_key), in the credential store.
    "cloud": {
        "url": "",
        "account": {},
        "device": {},
        "last_checkin": "",
        "policy_version": None,
        "usage_since": "",
    },
    # Settings > Updates (lumi/update_channels.py): automatic, manual or off;
    # the stable or beta channel; and a release line ("0.20") to stay on.
    # Read at startup, so a change applies after a restart.
    "updates": {
        "mode": "automatic",
        "channel": "stable",
        "pin": "",
    },
}


class SettingsManager:
    """Thread-safe settings manager with JSON persistence."""

    def __init__(self, path: str | Path | None = None, secrets: SecretStore | None = None):
        self._path = Path(path) if path else state_home() / "settings.json"
        self._lock = threading.Lock()
        self._data: dict = {}
        # API keys live in the OS credential store when one exists; settings.json
        # then keeps only a placeholder (see lumi/secrets_store.py). Never in the
        # pre-rebrand ~/.resonant folder: while it is still in use, an older SONN
        # Client may share it, and that version would read the placeholder as its key.
        self._secrets = secrets if secrets is not None else SecretStore()
        self._keychain = self._secrets.available and self._path.parent.name != LEGACY_HOME_DIR_NAME
        self._load()

    @staticmethod
    def _policy():
        """The organization policy (lumi/policy.py), or None."""
        from ..policy import current

        return current()

    def locked_values(self, section: str) -> dict[str, Any]:
        """Keys of ``section`` an organization policy locks, with their values."""
        policy = self._policy()
        if not policy:
            return {}
        prefix = section + "."
        return {name[len(prefix):]: value for name, value in policy.settings.items() if name.startswith(prefix)}

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        """Get a value. get('general', 'theme') or get('hooks').

        Values an organization policy locks win over what is stored; the
        stored value comes back if the policy stops locking it.
        """
        locked = self.locked_values(section)
        if key is not None and key in locked:
            return locked[key]
        with self._lock:
            sect = self._data.get(section, DEFAULTS.get(section))
            if key is None:
                if locked and isinstance(sect, dict):
                    return {**sect, **locked}
                return sect
            if isinstance(sect, dict):
                value = sect.get(key, default)
                if section == "api_keys" and value == PLACEHOLDER:
                    return self._secrets.get(key) or default
                return value
            return default

    def set(self, section: str, key: str | None, value: Any) -> None:
        """Set a value and persist. set('general', 'theme', 'light') or set('hooks', None, [...])."""
        with self._lock:
            if key is None:
                if section == "api_keys" and isinstance(value, dict):
                    value = {k: self._store_secret_locked(k, v) for k, v in value.items()}
                self._data[section] = value
            else:
                if section not in self._data:
                    self._data[section] = {}
                if section == "api_keys":
                    value = self._store_secret_locked(key, value)
                self._data[section][key] = value
            self._save_locked()

    def get_all(self) -> dict:
        """Return the full settings dict (deep copy), with policy-locked values applied."""
        with self._lock:
            data = json.loads(json.dumps(self._data))
        policy = self._policy()
        for name, value in (policy.settings.items() if policy else ()):
            section, _, key = name.partition(".")
            if isinstance(data.setdefault(section, {}), dict):
                data[section][key] = value
        return data

    def update_section(self, section: str, updates: dict) -> None:
        """Merge updates into a section."""
        with self._lock:
            if section not in self._data:
                self._data[section] = {}
            if section == "api_keys" and isinstance(updates, dict):
                updates = {k: self._store_secret_locked(k, v) for k, v in updates.items()}
            if isinstance(self._data[section], dict):
                self._data[section].update(updates)
            else:
                self._data[section] = updates
            self._save_locked()

    def get_masked(self) -> dict:
        """Return frontend-safe settings data with secret presence metadata."""
        data = self.get_all()
        meta = data.setdefault("_meta", {})
        if "api_keys" in data:
            present = {}
            for key, value in data["api_keys"].items():
                present[key] = bool(value)
                data["api_keys"][key] = ""
            meta["api_keys_present"] = present
        meta["secret_storage"] = self.secret_storage()
        # What an organization policy manages, for Settings to show and disable.
        from ..policy import load as load_policy

        state = load_policy()
        meta["policy"] = {
            "active": state.policy is not None,
            "error": state.error,
            "summary": state.policy.summary() if state.policy else None,
        }
        meta["locked"] = (
            {name: state.policy.organization for name in state.policy.settings} if state.policy else {}
        )
        # Offered by Settings > Privacy & security; nothing is excluded by default.
        from ..engine.exclusions import COMMON_SECRET_PATTERNS
        meta["common_exclusions"] = list(COMMON_SECRET_PATTERNS)
        # Whether the shell sandbox can run here; never waits for the check.
        from ..engine import os_sandbox
        meta["shell_sandbox"] = os_sandbox.status()
        return data

    def secret_storage(self) -> dict:
        """Where API keys are kept, for Settings to explain."""
        reason = self._secrets.reason
        if self._secrets.available and not self._keychain:
            reason = "Keys stay in settings.json until your data moves from ~/.resonant to ~/.lumi."
        return {"keychain": self._keychain, "reason": reason, "store": credential_store_name()}

    def _store_secret_locked(self, key: str, value: Any) -> Any:
        """Put one API key in the credential store; return what settings.json keeps."""
        if not self._keychain or not isinstance(value, str) or value == PLACEHOLDER:
            return value
        if not value:
            self._secrets.delete(key)
            return ""
        return PLACEHOLDER if self._secrets.set(key, value) else value

    def _secure_api_keys_locked(self) -> None:
        """Move plaintext keys from settings.json into the credential store."""
        keys = self._data.get("api_keys")
        if not self._keychain or not isinstance(keys, dict):
            return
        for key, value in list(keys.items()):
            if isinstance(value, str) and value and value != PLACEHOLDER:
                keys[key] = self._store_secret_locked(key, value)

    def _load(self) -> None:
        """Load from disk, merging with defaults for any missing keys."""
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning(f"Failed to read settings: {exc}")
                    self._data = {}
            else:
                self._data = {}
            self._apply_defaults()
            self._migrate()
            self._secure_api_keys_locked()
            self._save_locked()

    def _migrate(self) -> None:
        """Retire settings that shipped as defaults and no longer apply.

        Removing an entry from DEFAULTS does nothing for existing installs —
        `_apply_defaults` only adds missing keys, it never prunes. Every user
        who ever launched a build before v0.11.15 has a `browseros` entry
        written into their settings.json, and it reports itself as an
        unavailable tool server on a machine that has no reason to run one.

        Only the untouched stock entry is dropped. A changed URL means the user
        configured BrowserOS deliberately, and that is theirs to keep.
        """
        servers = self._data.get("mcp_servers")
        if not isinstance(servers, dict):
            return
        browseros = servers.get("browseros")
        if not isinstance(browseros, dict):
            return
        if str(browseros.get("url") or "") == "http://127.0.0.1:9239/mcp":
            servers.pop("browseros", None)
            logger.info(
                "Removed the stock browseros MCP entry; browsing is built in "
                "(add it back from Settings if you use BrowserOS)"
            )

    def _apply_defaults(self) -> None:
        """Merge missing keys from DEFAULTS into current data."""
        for section, default_value in DEFAULTS.items():
            if section not in self._data:
                self._data[section] = (
                    dict(default_value) if isinstance(default_value, dict)
                    else list(default_value) if isinstance(default_value, list)
                    else default_value
                )
            elif isinstance(default_value, dict) and isinstance(self._data[section], dict):
                for key, value in default_value.items():
                    if key not in self._data[section]:
                        self._data[section][key] = value

    def _save_locked(self) -> None:
        """Write to disk (caller must hold lock)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"Failed to save settings: {exc}")
