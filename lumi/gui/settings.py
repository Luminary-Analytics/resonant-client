"""
Persistent settings manager for Lumi.
Reads/writes ~/.lumi/settings.json with section-based access.
"""

import asyncio
import json
import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from ..paths import LEGACY_HOME_DIR_NAME, state_home
from ..secret_scan import SENSITIVE_NAME
from ..secrets_store import PLACEHOLDER, SecretStore, credential_store_name

logger = logging.getLogger(__name__)

# Another program can hold settings.json for a moment (an antivirus scan, a
# sync client, another Lumi writing it): reading or writing it is tried
# again, waiting 0.05 s and twice as long each time, about 1.5 s in all,
# before Lumi gives up. Never on the event loop, which must not stall: there
# each step is tried once (_patient).
_FILE_ATTEMPTS = 6
_FILE_RETRY_SECONDS = 0.05

# The permission mode a new install starts in: Auto-edit, where file edits
# inside the project apply without asking and commands and everything else ask
# (engine/policies.py). New installs started in Full-auto ("bypass") before
# September 27, 2026. Every earlier first launch wrote that default into
# settings.json (``_load`` saves the merged defaults), so an existing install
# keeps the mode its file names; a file without one keeps Full-auto too
# (``_keep_earlier_permission_mode``).
DEFAULT_PERMISSION_MODE = "auto-edit"
EARLIER_DEFAULT_PERMISSION_MODE = "bypass"

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
        "default_permission_mode": DEFAULT_PERMISSION_MODE,
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
    # Settings > Offline mode (lumi/offline.py): no outbound connections but
    # this computer and these hosts (names, *.domain, addresses, networks).
    # An organization's policy can lock either; when it turns offline mode
    # on, only the hosts it allows apply.
    "offline": {
        "enabled": False,
        "allowed_hosts": [],
    },
    # Secrets are masked before settings are sent to the frontend.
    "api_keys": {"anthropic": "", "openai": "", "kimi": "", "openrouter": "", "sonn": "", "telegram_bot": "", "otlp": "",
                 "github": "", "gitlab": "", "bitbucket": "", "azure_devops": "", "jira": "", "linear": "", "slack_bot": "", "slack_app": ""},
    # Issue trackers (engine/issue_trackers.py). Jira's token is api_keys.jira;
    # with an email it's a Jira Cloud API token, without one a personal access
    # token for Jira Server or Data Center. Linear's key is api_keys.linear.
    "issue_trackers": {"jira_url": "", "jira_email": ""},
    # GitHub Enterprise Server and self-managed GitLab hosts that may receive the
    # GitHub or GitLab token, besides github.com and gitlab.com
    # (engine/github_tools.token_hosts); lockable by policy.
    "code_hosts": {"github_hosts": [], "gitlab_hosts": []},
    # Agent pull requests wait for these reviewers (engine/review_gate.py); lockable by policy.
    "review": {"agent_changes": False, "reviewers": []},
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
    # Capability pack publishers the person trusts, by key id (engine/pack_signing.py).
    # Changed only through Settings > Capability packs' trust and forget actions.
    "pack_publishers": {},
    "model_roles": {},
    "agent_runtime": {
        "persist": True,
        "worktree_writers": True,
        "max_parallel_readers": 4,
        "max_parallel_writers": 2,
    },
    "swarming": {
        "version": 1,
        "enabled": False,
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
        # Send feedback (lumi/feedback.py): "on" or "off"; diagnostics in it
        # "allowed" or "never"; and where reports go ("" for the build's
        # feedback address, else the Lumi Cloud this computer uses). An
        # organization's policy can lock each.
        "feedback": "on",
        "feedback_diagnostics": "allowed",
        "feedback_url": "",
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
        # Panels from approved capability packs, in a sandboxed frame (gui/extension_panels.py).
        "extension_panels": True,
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
    # The first-run checklist (lumi/gui/onboarding.py), and whether the launch
    # offer to sign in to Lumi Cloud was answered (lumi/cloud.py: signing in,
    # or continuing without an account).
    "onboarding": {
        "dismissed": False,
        "first_task_done": False,
        "cloud_prompted": False,
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
        # Tasks from Slack and Teams (lumi/remote_tasks.py): off until the
        # person turns it on; an organization's policy can lock it off.
        "remote_tasks": False,
        "remote_tasks_project": "",
        "remote_tasks_mode": "ask",
    },
    # Settings > Updates (lumi/update_channels.py): automatic, manual or off;
    # the stable or beta channel; and a release line ("0.20") to stay on.
    # Read at startup, so a change applies after a restart.
    "updates": {
        "mode": "automatic",
        "channel": "stable",
        "pin": "",
    },
    # Settings > Voice (lumi/voice.py): dictation in the composer. The engine
    # is auto, browser (the webview's recognizer), service or off; the
    # service is "openai" or a connection ("conn-<id>") that transcribes.
    "voice": {
        "engine": "auto",
        "service": "",
        "model": "",
        "language": "",
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
        # Why settings.json couldn't be read, or "". While it's set, Lumi runs
        # on defaults and never writes over the file (_save_locked); the app,
        # the terminal UI and `lumi run` say so.
        self.load_error = ""
        # Why the last save didn't reach settings.json, or "" once one does.
        # Settings and the banner above the message box show it (get_masked).
        self.save_error = ""
        # Called with no arguments, without the lock and on the thread that
        # saved, whenever save_error changes: the app tells the page at once,
        # whatever saved (AppState._settings_file_changed).
        self.on_save_error_changed: Callable[[], None] | None = None
        self._load()

    @property
    def backup_path(self) -> Path:
        """The copy of settings.json as it was before Lumi last wrote it."""
        return self._path.with_name(self._path.name + ".bak")

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

    def stored(self, section: str, key: str, default: Any = None) -> Any:
        """The value saved in settings.json, whatever an organization policy locks.

        For state Lumi records itself that no policy may stand in for, such as
        the Lumi Cloud address that issued the sign-in (lumi/cloud.py). Never
        for ``api_keys``, whose values live in the credential store.
        """
        with self._lock:
            sect = self._data.get(section, DEFAULTS.get(section))
            return sect.get(key, default) if isinstance(sect, dict) else default

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
            changed = self._save_locked()
        if changed:
            self._save_error_changed()

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
            changed = self._save_locked()
        if changed:
            self._save_error_changed()

    def _save_error_changed(self) -> None:
        """Tell ``on_save_error_changed`` that a save stopped, or started again, reaching the file."""
        listener = self.on_save_error_changed
        if listener is None:
            return
        try:
            listener()
        except Exception:
            logger.debug("Couldn't pass on the settings file's state", exc_info=True)

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
        # A settings file Lumi couldn't read: it runs on defaults and saves nothing (_load).
        meta["load_error"] = self.load_error
        # A save that didn't reach the file (_save_locked): changes last until Lumi closes.
        meta["save_error"] = self.save_error
        # What an organization policy manages, for Settings to show and disable.
        from ..policy import load as load_policy

        state = load_policy()
        meta["policy"] = {
            "active": state.policy is not None,
            "error": state.error,
            "summary": state.policy.summary() if state.policy else None,
            # Machine files others could have written, which Lumi didn't use: never silently.
            "ignored": [item.summary() for item in getattr(state, "ignored", ())],
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
        # Which ways of dictating Settings and the policy allow; reads settings only.
        from .. import voice
        meta["voice"] = voice.status(self)
        return data

    def key_present(self, key: str) -> bool:
        """Whether an API key is saved, without reading it from the credential store."""
        with self._lock:
            keys = self._data.get("api_keys")
            return bool(keys.get(key)) if isinstance(keys, dict) else False

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
        """Load from disk, merging with defaults for any missing keys.

        Only a missing file is a new install. A file that exists but can't be
        read or parsed, after the retries a moment's lock needs, is kept as
        it is: Lumi runs on defaults, says why (``load_error``) and saves
        nothing over it, since writing defaults there would lose every
        setting and key it holds.
        """
        with self._lock:
            data, error = self._read_locked()
            if error:
                self.load_error = error
                logger.error(error)
                self._data = {}
            else:
                self._data = data if data is not None else {}
                if data is not None:
                    self._keep_earlier_permission_mode()
            self._apply_defaults()
            self._migrate()
            self._secure_api_keys_locked()
            self._save_locked()

    def _read_locked(self) -> tuple[dict | None, str]:
        """The saved settings: (data, ""), (None, "") without a file, or (None, why) when unreadable."""
        problem = ""
        wait = _FILE_RETRY_SECONDS
        for attempt in range(_FILE_ATTEMPTS if _patient() else 1):
            if attempt:
                time.sleep(wait)
                wait *= 2
            try:
                # utf-8-sig: Notepad and Windows PowerShell 5.1 can save a byte-order mark.
                text = self._path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                return None, ""
            except UnicodeDecodeError:
                problem = "it isn't UTF-8 text"
                continue
            except OSError as exc:  # held by another program, or not readable at all
                problem = exc.strerror or str(exc)
                continue
            try:
                data = json.loads(text)
            except (ValueError, RecursionError) as exc:
                # Maybe half written by a program that writes in place: try again.
                problem = f"it isn't valid JSON ({exc})"
                continue
            if isinstance(data, dict):
                return data, ""
            problem = "it doesn't hold a JSON object"
            break
        backup = (f" The settings as Lumi last saved them before that are in {self.backup_path}."
                  if self.backup_path.is_file() else "")
        return None, (f"Lumi couldn't read its settings file, {self._path}: {problem}. It's using default "
                      f"settings for now and won't save any change over that file.{backup} Fix or replace "
                      "the file, then restart Lumi.")

    def _keep_earlier_permission_mode(self) -> None:
        """Give a settings file that names no permission mode the earlier default.

        Only a new install starts in Auto-edit. Earlier versions wrote their
        Full-auto default into settings.json on first launch, so a file without
        the key, or with an empty one, was written by hand or by another tool,
        and ran in Full-auto until now. A file that can't be read gets the
        new default in memory while it's kept as it is (_load).
        """
        if not isinstance(self._data, dict):
            return
        general = self._data.setdefault("general", {})
        if isinstance(general, dict) and not str(general.get("default_permission_mode") or "").strip():
            general["default_permission_mode"] = EARLIER_DEFAULT_PERMISSION_MODE

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

    def _save_locked(self) -> bool:
        """Write to disk (caller must hold lock); True when ``save_error`` changed.

        Never over a file that couldn't be read (``load_error``). The file as
        it was goes to settings.json.bak first, without its API keys or any
        other credential (_back_up). Then the new file takes the old one's
        place in one step, so a crash or a full disk mid-write leaves the old
        file whole. Windows won't replace a file another program has open
        (an antivirus scan, a sync client, an editor), whatever it shares, so
        then, with the backup made, the file is written in place, as Lumi did
        before. A save that still fails is kept in ``save_error`` for the
        page to show, never dropped silently.
        """
        if self.load_error:
            logger.warning("Settings weren't saved: settings.json couldn't be read, so it is kept as it is.")
            return False
        before = self.save_error
        # A settings.json that is a link (a dotfiles folder, say) stays one: its target is written.
        target = Path(os.path.realpath(self._path))
        temporary = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        content = json.dumps(self._data, indent=2, ensure_ascii=False)
        patient = _patient()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(content, encoding="utf-8")
            backed_up = False
            if target.is_file():
                if sys.platform != "win32":  # keep who may read it (it can hold API keys)
                    os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                backed_up = self._back_up(target, patient=patient)
            try:
                _retry(lambda: os.replace(temporary, target), patient=patient)
            except OSError:
                if not backed_up:
                    raise
                _retry(lambda: target.write_text(content, encoding="utf-8"), patient=patient)
            self.save_error = ""
        except OSError as exc:
            reason = exc.strerror or str(exc)
            self.save_error = (f"Lumi couldn't save its settings to {target}: {reason}. Another program may have "
                               "the file open. Changes made now last until Lumi closes; close that program, "
                               "then change the setting again.")
            logger.error(self.save_error)
        finally:
            temporary.unlink(missing_ok=True)
        return self.save_error != before

    @staticmethod
    def _back_up(target: Path, *, patient: bool = True) -> bool:
        """Keep ``target`` as it is now in ``<name>.bak``, without its credentials; True once it's there.

        The copy leaves out every API key and any field named like a
        credential (``_without_secrets``): the credential store may have just
        taken them out of settings.json, and a backup must not keep them in
        plain text. A backup that can't be made doesn't stop the save, which
        then only replaces the file in one step.
        """
        backup = target.with_name(target.name + ".bak")
        partial = backup.with_name(f"{backup.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            data = json.loads(_retry(lambda: target.read_text(encoding="utf-8-sig"), patient=patient))
            partial.write_text(json.dumps(_without_secrets(data), indent=2, ensure_ascii=False), encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(partial, stat.S_IMODE(target.stat().st_mode))
            _retry(lambda: os.replace(partial, backup), patient=patient)
            return True
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning(f"Couldn't keep a backup of the settings before saving: {exc}")
            return False
        finally:
            partial.unlink(missing_ok=True)


def _without_secrets(data: Any, *, secret: bool = False) -> Any:
    """``data`` with every credential emptied: API keys and fields named like one (a token, a password).

    A key the OS credential store keeps stays as its placeholder, which
    holds no secret.
    """
    if isinstance(data, dict):
        return {key: _without_secrets(value, secret=secret or key == "api_keys" or bool(SENSITIVE_NAME.search(str(key))))
                for key, value in data.items()}
    if isinstance(data, list):
        return [_without_secrets(item, secret=secret) for item in data]
    if secret and isinstance(data, str) and data and data != PLACEHOLDER:
        return ""
    return data


def _patient() -> bool:
    """Whether to wait for a file another program holds: not on a thread running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _retry(action: Callable[[], Any], *, patient: bool = True) -> Any:
    """``action()``, tried again while another program holds the file for a moment; once if not ``patient``."""
    wait = _FILE_RETRY_SECONDS
    attempts = _FILE_ATTEMPTS if patient else 1
    for attempt in range(attempts):
        try:
            return action()
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)
            wait *= 2
    return None
