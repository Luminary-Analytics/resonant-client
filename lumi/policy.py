"""Organization policy: settings an administrator locks, and what may run.

A policy is a ``lumi.policy/v1`` JSON document, supplied machine-wide by IT:

* Windows: the registry value ``Policy`` (the JSON text) or ``PolicyFile`` (a
  path) under ``HKLM\\SOFTWARE\\Policies\\Luminary Analytics\\Lumi``, which
  the ADMX template in ``packaging/policy/`` sets through Group Policy or
  Intune; otherwise ``%ProgramData%\\Lumi\\policy.json``;
* macOS: the ``Policy`` key of the ``com.luminaryanalytics.lumi`` managed
  preferences (a configuration profile), otherwise
  ``/Library/Application Support/Lumi/policy.json``;
* Linux: ``/etc/lumi/policy.json``;
* only where none of those exists, ``LUMI_POLICY_FILE`` names a file (pilots,
  CI). It can't replace a machine policy, so users can't swap in their own.

Those locations are writable only by administrators, so a policy there is
trusted as it is. A policy may also be signed: ``{"policy": {...},
"signature": "<base64>", "key_id": "<id>"}`` with an Ed25519 signature over
the policy's canonical JSON. Signed policies verify against keys only an
administrator can set: the ``PolicyKeys`` registry value, ``policy-keys.json``
beside the machine policy file, or a machine policy's ``trusted_keys``. That is
how Lumi Cloud delivers organization policy (see "Lumi Cloud policy" below
and lumi/cloud.py).
A signed policy carries ``expires_at``: past it Lumi keeps enforcing it for
``grace_days`` so people can work offline, then refuses model requests until
a fresh policy arrives.

What a policy can do (every section is optional)::

    {
      "schema": "lumi.policy/v1",
      "organization": "Acme",
      "settings": {"privacy.secret_scan": true, "security.cli_adapters": false},
      "permissions": {"allowed_modes": ["ask", "auto-edit"]},
      "models": {"allowed": ["anthropic:*"], "blocked": ["openrouter:*"],
                 "require_zero_retention": true, "zero_retention_providers": ["anthropic"]},
      "files": {"exclude": ["**/.env", "*.pem"]},
      "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                           "arg_patterns": {"command": "curl"}}]},
      "mcp": {"allowed_servers": ["github"], "allow_stdio": false},
      "extensions": {"allowed_packs": ["team-*"]},
      "pricing": {"prices": {"anthropic:claude-opus-*": {"input": 3.2, "output": 16}}},
      "budgets": [{"scope": "user", "period": "month", "warn_usd": 200, "block_usd": 400}]
    }

Locked settings override the user's value and can't be changed in Settings,
which shows who manages them. Lists match ``fnmatch`` patterns.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "lumi.policy/v1"
REGISTRY_KEY = r"SOFTWARE\Policies\Luminary Analytics\Lumi"
MAC_DOMAIN = "com.luminaryanalytics.lumi"
PERMISSION_MODES = ("ask", "auto-edit", "plan", "bypass")


class PolicyError(ValueError):
    """A policy that can't be used; the message says why."""


@dataclass
class Policy:
    """A loaded, validated policy. ``None`` fields mean the policy doesn't say."""

    organization: str = "your organization"
    source: str = ""
    signed: bool = False
    issued_at: str = ""
    expires_at: str = ""
    grace_days: int = 7
    settings: dict[str, Any] = field(default_factory=dict)
    allowed_modes: tuple[str, ...] | None = None
    models_allowed: tuple[str, ...] | None = None
    models_blocked: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    shell_rules: tuple[dict, ...] = ()
    mcp_allowed: tuple[str, ...] | None = None
    mcp_allow_stdio: bool = True
    packs_allowed: tuple[str, ...] | None = None
    # Repository URL patterns packs may be installed from (lumi/engine/pack_install.py).
    sources_allowed: tuple[str, ...] | None = None
    trusted_keys: dict[str, str] = field(default_factory=dict)
    # Negotiated prices (lumi/pricing.py): ordered (pattern, Price) pairs.
    prices: tuple = ()
    # Spending rules (lumi/budgets.py), owned by the organization.
    budgets: tuple = ()
    # Model capabilities an administrator states (lumi/capabilities.py).
    capability_overrides: tuple = ()
    # Only providers that keep no data: local ones, the providers named here
    # (the organization's zero data retention agreements) and connections
    # marked zero_retention (see zero_retention_ok).
    require_zero_retention: bool = False
    zero_retention_providers: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    # ── Queries ────────────────────────────────────────────────────────────
    def locked(self, section: str, key: str) -> bool:
        return f"{section}.{key}" in self.settings

    def value(self, section: str, key: str) -> Any:
        return self.settings[f"{section}.{key}"]

    def mode_allowed(self, mode: str) -> bool:
        return self.allowed_modes is None or mode in self.allowed_modes

    def model_allowed(self, backend: str, model: str) -> bool:
        target = f"{backend}:{model}"
        if any(fnmatch.fnmatchcase(target, pattern) for pattern in self.models_blocked):
            return False
        if self.require_zero_retention and not self.zero_retention_ok(backend, model):
            return False
        return self.models_allowed is None or any(
            fnmatch.fnmatchcase(target, pattern) for pattern in self.models_allowed
        )

    def zero_retention_ok(self, backend: str, model: str = "") -> bool:
        """Whether ``backend`` keeps no data: local, named by the policy, or a connection marked so.

        Ollama's cloud models (``...:cloud``, ``...-cloud``) run on ollama.com, so
        they count as local only when the policy names ``ollama``.
        """
        if backend in self.zero_retention_providers:
            return True
        if backend in LOCAL_PROVIDERS:
            return not str(model).endswith((":cloud", "-cloud"))
        return bool(_zero_retention_connection(backend))

    def mcp_server_allowed(self, name: str, *, stdio: bool) -> bool:
        if stdio and not self.mcp_allow_stdio:
            return False
        return self.mcp_allowed is None or any(fnmatch.fnmatchcase(name, p) for p in self.mcp_allowed)

    def pack_allowed(self, pack_id: str) -> bool:
        return self.packs_allowed is None or any(fnmatch.fnmatchcase(pack_id, p) for p in self.packs_allowed)

    def expiry_state(self, now: float | None = None) -> str:
        """"current", "grace" (expired but still enforced) or "expired" (blocks requests)."""
        if not self.expires_at:
            return "current"
        now = time.time() if now is None else now
        expires = _parse_time(self.expires_at)
        if now <= expires:
            return "current"
        return "grace" if now <= expires + self.grace_days * 86400 else "expired"

    def summary(self) -> dict:
        """What Settings shows about the active policy."""
        return {
            "organization": self.organization,
            "source": self.source,
            "signed": self.signed,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "expiry": self.expiry_state(),
            "locked_settings": sorted(self.settings),
            "allowed_modes": list(self.allowed_modes) if self.allowed_modes is not None else None,
            "models_allowed": list(self.models_allowed) if self.models_allowed is not None else None,
            "models_blocked": list(self.models_blocked),
            "exclude": list(self.exclude),
            "shell_rules": len(self.shell_rules),
            "mcp_allowed": list(self.mcp_allowed) if self.mcp_allowed is not None else None,
            "mcp_allow_stdio": self.mcp_allow_stdio,
            "packs_allowed": list(self.packs_allowed) if self.packs_allowed is not None else None,
            "sources_allowed": list(self.sources_allowed) if self.sources_allowed is not None else None,
            "prices": [pattern for pattern, _ in self.prices],
            "budgets": len(self.budgets),
            "capability_overrides": [pattern for pattern, _ in self.capability_overrides],
            "require_zero_retention": self.require_zero_retention,
            "zero_retention_providers": list(self.zero_retention_providers),
        }


def _parse_time(text: str) -> float:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError(f"'{text}' is not an ISO 8601 time.") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _patterns(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"{where} must be a list of strings.")
    return tuple(item.strip() for item in value if item.strip())


def canonical(document: dict) -> bytes:
    """The bytes a signature covers: sorted keys, no whitespace, UTF-8."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(document: dict, signature_b64: str, public_key_b64: str) -> bool:
    """Whether ``signature_b64`` is a valid Ed25519 signature of ``document``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), canonical(document))
        return True
    except (InvalidSignature, ValueError):
        return False


# Providers that run on this computer or the local network: nothing leaves.
LOCAL_PROVIDERS = frozenset({"ollama", "exo"})
# Set by the app (connections are in its settings): backend -> keeps no data.
_zero_retention_resolver = None


def set_zero_retention_resolver(resolver) -> None:
    """How ``Policy.zero_retention_ok`` learns which connections keep no data (``conn-<id>``)."""
    global _zero_retention_resolver
    _zero_retention_resolver = resolver


def _zero_retention_connection(backend: str) -> bool:
    resolver = _zero_retention_resolver
    if resolver is None or not str(backend).startswith("conn-"):
        return False
    try:
        return bool(resolver(backend))
    except Exception:  # an unreadable connection doesn't qualify
        return False


def _flag(value, where: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PolicyError(f"{where} must be true or false.")
    return value

def parse(data: Any, *, source: str, trusted_keys: dict[str, str] | None = None,
          require_signature: bool = False) -> Policy:
    """Validate a policy document (signed or not) and return it."""
    if not isinstance(data, dict):
        raise PolicyError("A policy must be a JSON object.")
    signed = "signature" in data
    if signed:
        document = data.get("policy")
        key_id = str(data.get("key_id") or "")
        keys = trusted_keys or {}
        if not isinstance(document, dict):
            raise PolicyError("A signed policy needs its 'policy' object.")
        if key_id not in keys:
            raise PolicyError(f"The policy is signed with key '{key_id}', which this machine doesn't trust.")
        if not verify_signature(document, str(data.get("signature") or ""), keys[key_id]):
            raise PolicyError("The policy's signature doesn't match its contents.")
    elif require_signature:
        raise PolicyError("This policy must be signed.")
    else:
        document = data
    if document.get("schema") != SCHEMA:
        raise PolicyError(f"Unknown policy schema {document.get('schema')!r}; expected {SCHEMA}.")

    settings = document.get("settings") or {}
    if not isinstance(settings, dict) or not all(isinstance(k, str) and "." in k for k in settings):
        raise PolicyError("'settings' must map 'section.key' names to values.")
    from .update_channels import validate_policy_settings

    try:
        validate_policy_settings(settings)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    if "security.shell_sandbox" in settings and settings["security.shell_sandbox"] not in ("off", "project"):
        raise PolicyError("'security.shell_sandbox' must be \"off\" or \"project\".")
    if "review.agent_changes" in settings and not isinstance(settings["review.agent_changes"], bool):
        raise PolicyError("'review.agent_changes' must be true or false.")
    reviewers = settings.get("review.reviewers", [])
    if not isinstance(reviewers, list) or not all(isinstance(name, str) for name in reviewers):
        raise PolicyError("'review.reviewers' must list GitHub usernames or organization/team names.")
    permissions = document.get("permissions") or {}
    modes = permissions.get("allowed_modes")
    allowed_modes = _patterns(modes, "permissions.allowed_modes") if modes is not None else None
    if allowed_modes is not None:
        unknown = [m for m in allowed_modes if m not in PERMISSION_MODES]
        if unknown or not allowed_modes:
            raise PolicyError(f"permissions.allowed_modes must list some of {', '.join(PERMISSION_MODES)}.")
    models = document.get("models") or {}
    mcp = document.get("mcp") or {}
    extensions = document.get("extensions") or {}
    shell_rules = (document.get("shell") or {}).get("rules") or []
    if not isinstance(shell_rules, list) or not all(isinstance(rule, dict) for rule in shell_rules):
        raise PolicyError("shell.rules must be a list of rule objects.")
    expires_at = str(document.get("expires_at") or "")
    if expires_at:
        _parse_time(expires_at)
    from .pricing import parse_prices

    try:
        prices = parse_prices((document.get("pricing") or {}).get("prices") or {})
    except ValueError as exc:
        raise PolicyError(f"pricing.prices: {exc}") from exc
    from .budgets import parse_rules

    organization = str(document.get("organization") or "your organization")
    try:
        budgets = parse_rules(document.get("budgets"), owner=organization)
    except ValueError as exc:
        raise PolicyError(f"budgets: {exc}") from exc
    from .capabilities import parse_capability_overrides

    try:
        capability_overrides = parse_capability_overrides(models.get("capabilities"))
    except ValueError as exc:
        raise PolicyError(f"models.capabilities: {exc}") from exc
    raw_keys = document.get("trusted_keys") or {}
    if not isinstance(raw_keys, dict):
        raise PolicyError("trusted_keys must map key ids to base64 Ed25519 public keys.")

    return Policy(
        organization=str(document.get("organization") or "your organization"),
        source=source,
        signed=signed,
        issued_at=str(document.get("issued_at") or ""),
        expires_at=expires_at,
        grace_days=max(0, int(document.get("grace_days", 7) or 0)),
        settings=dict(settings),
        allowed_modes=allowed_modes,
        models_allowed=_patterns(models["allowed"], "models.allowed") if "allowed" in models else None,
        models_blocked=_patterns(models.get("blocked"), "models.blocked"),
        require_zero_retention=_flag(models.get("require_zero_retention"), "models.require_zero_retention"),
        zero_retention_providers=_patterns(models.get("zero_retention_providers"),
                                           "models.zero_retention_providers"),
        exclude=_patterns((document.get("files") or {}).get("exclude"), "files.exclude"),
        shell_rules=tuple(shell_rules),
        mcp_allowed=_patterns(mcp["allowed_servers"], "mcp.allowed_servers") if "allowed_servers" in mcp else None,
        mcp_allow_stdio=mcp.get("allow_stdio", True) is not False,
        packs_allowed=(
            _patterns(extensions["allowed_packs"], "extensions.allowed_packs")
            if "allowed_packs" in extensions else None
        ),
        sources_allowed=(
            _patterns(extensions["allowed_sources"], "extensions.allowed_sources")
            if "allowed_sources" in extensions else None
        ),
        trusted_keys={str(k): str(v) for k, v in raw_keys.items()},
        prices=prices,
        budgets=budgets,
        capability_overrides=capability_overrides,
        raw=document,
    )


# ── Where machine policy lives ──────────────────────────────────────────────


def _registry_policy() -> tuple[str, str] | None:
    """(json text, source) from the Windows policy registry key, if set."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY) as key:
            for name in ("Policy", "PolicyFile"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if isinstance(value, list):
                    # The ADMX "multiText" element stores one line per string.
                    value = "\n".join(str(line) for line in value)
                if name == "Policy" and str(value).strip():
                    return str(value), f"Group Policy (HKLM\\{REGISTRY_KEY})"
                if name == "PolicyFile" and str(value).strip():
                    path = Path(os.path.expandvars(str(value)))
                    return path.read_text(encoding="utf-8"), f"{path} (set by Group Policy)"
    except OSError:
        return None
    return None


def _macos_managed_policy() -> tuple[str, str] | None:
    if sys.platform != "darwin":
        return None
    path = Path("/Library/Managed Preferences") / f"{MAC_DOMAIN}.plist"
    if not path.is_file():
        return None
    import plistlib

    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        return None
    value = data.get("Policy")
    if isinstance(value, str) and value.strip():
        return value, f"configuration profile ({MAC_DOMAIN})"
    if isinstance(value, dict):
        return json.dumps(value), f"configuration profile ({MAC_DOMAIN})"
    return None


def machine_policy_file() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Lumi" / "policy.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Lumi/policy.json")
    return Path("/etc/lumi/policy.json")


def _machine_file_policy() -> tuple[str, str] | None:
    path = machine_policy_file()
    if path.is_file():
        return path.read_text(encoding="utf-8"), str(path)
    return None


def _load_text() -> tuple[str, str] | None:
    """The policy text and where it came from. Machine sources always win."""
    for finder in (_registry_policy, _macos_managed_policy, _machine_file_policy):
        found = finder()
        if found:
            return found
    override = os.environ.get("LUMI_POLICY_FILE", "").strip()
    if override:
        path = Path(override)
        return path.read_text(encoding="utf-8"), str(path)
    return None


def machine_keys() -> dict[str, str]:
    """Signing keys an administrator trusts: registry ``PolicyKeys`` or policy-keys.json."""
    texts: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "PolicyKeys")
                texts.append(str(value))
        except (ImportError, OSError):
            pass
    keys_file = machine_policy_file().with_name("policy-keys.json")
    if keys_file.is_file():
        try:
            texts.append(keys_file.read_text(encoding="utf-8"))
        except OSError:
            pass
    keys: dict[str, str] = {}
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("Ignoring policy keys that aren't JSON")
            continue
        if isinstance(data, dict):
            keys.update({str(k): str(v) for k, v in data.items()})
    return keys


@dataclass
class PolicyState:
    """The active policy, or why there is none (for Settings to explain)."""

    policy: Policy | None = None
    error: str = ""
    source: str = ""
    # True when ``policy`` came from Lumi Cloud; ``machine`` is then the
    # machine policy that set it up (None for an organization joined in the app).
    cloud: bool = False
    machine: Policy | None = None
    # Why a downloaded Lumi Cloud policy isn't in force, if one exists.
    cloud_error: str = ""


# ── Lumi Cloud policy ───────────────────────────────────────────────────────
#
# A computer enrolled in a Lumi Cloud organization (lumi/cloud.py) keeps the
# organization's latest signed policy in ~/.lumi/cloud/policy.json. That file
# is user-writable, so it counts only when its signature verifies:
#
# * with a machine policy that names a ``cloud`` section, against the keys an
#   administrator set (machine keys and that policy's ``trusted_keys``). The
#   cloud policy then replaces the machine policy's own rules, which apply
#   until the first download and whenever the download is unusable;
# * without a machine policy, against the keys pinned when the person joined
#   the organization in the app (``cloud.device.trusted_keys`` in
#   settings.json). They chose to join, and can leave; nothing here overrides
#   a machine policy.


def cloud_policy_path() -> Path:
    from .paths import state_home

    return state_home() / "cloud" / "policy.json"


def _joined_device() -> dict:
    """The organization this person joined from the app, from settings.json."""
    from .paths import state_home

    try:
        data = json.loads((state_home() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    device = (data.get("cloud") or {}).get("device") if isinstance(data, dict) else None
    return device if isinstance(device, dict) and device.get("id") else {}


def _cloud_document() -> dict | None:
    path = cloud_policy_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PolicyError("The downloaded policy isn't a JSON object.")
    return data


def _with_cloud_policy(state: PolicyState, keys: dict[str, str]) -> PolicyState:
    """``state`` with the downloaded Lumi Cloud policy applied, if one is there and verifies."""
    machine = state.policy
    try:
        document = _cloud_document()
        if document is None:
            return state
        organization = machine.organization if machine else str(_joined_device().get("organization_name") or "")
        how = f"set up by {machine.source}" if machine else "joined in this app"
        cloud = parse(document, source=f"Lumi Cloud: {organization or 'your organization'} ({how})",
                      trusted_keys=keys, require_signature=True)
    except (OSError, ValueError, PolicyError) as exc:
        note = f"{machine.organization}'s machine policy applies instead." if machine else ""
        return PolicyState(policy=machine, source=state.source, machine=None,
                           cloud_error=f"The Lumi Cloud policy can't be used: {exc} {note}".strip())
    return PolicyState(policy=cloud, source=cloud.source, cloud=True, machine=machine)


_lock = threading.Lock()
_state = PolicyState()
_loaded = False


def load(*, force: bool = False) -> PolicyState:
    """Read machine policy once (or again with ``force``) and return it."""
    global _state, _loaded
    with _lock:
        if _loaded and not force:
            return _state
        _loaded = True
        try:
            found = _load_text()
        except OSError as exc:
            _state = PolicyState(error=f"The policy file couldn't be read: {exc}")
            return _state
        if not found:
            # No machine policy: an organization this person joined in the app.
            joined = _joined_device()
            keys = joined.get("trusted_keys") if isinstance(joined.get("trusted_keys"), dict) else {}
            _state = _with_cloud_policy(PolicyState(), {str(k): str(v) for k, v in keys.items()}) if joined \
                else PolicyState()
            return _state
        text, source = found
        try:
            data = json.loads(text)
            keys = machine_keys()
            if isinstance(data, dict) and "signature" not in data:
                # The machine policy may name keys that sign policies it doesn't
                # hold itself (for example the organization's Lumi Cloud policy).
                keys.update({str(k): str(v) for k, v in (data.get("trusted_keys") or {}).items()})
            policy = parse(data, source=source, trusted_keys=keys)
            _state = PolicyState(policy=policy, source=source)
            if isinstance(policy.raw.get("cloud"), dict):
                _state = _with_cloud_policy(_state, keys)
        except (ValueError, PolicyError) as exc:
            # A broken machine policy must not silently mean "no policy":
            # model requests are refused until IT fixes it (see blocked_reason).
            _state = PolicyState(error=f"The organization policy at {source} is invalid: {exc}", source=source)
        return _state


def machine_cloud_settings() -> dict:
    """The ``cloud`` section of the machine policy (an administrator's), or {}."""
    state = load()
    machine = state.machine if state.cloud else state.policy
    section = machine.raw.get("cloud") if machine else None
    return dict(section) if isinstance(section, dict) else {}


def trusted_cloud_keys() -> dict[str, str]:
    """Keys that may sign this computer's Lumi Cloud policy (machine-set, or pinned at joining)."""
    state = load()
    machine = state.machine if state.cloud else state.policy
    if machine is not None and isinstance(machine.raw.get("cloud"), dict):
        keys = machine_keys()
        keys.update(machine.trusted_keys)
        return keys
    joined = _joined_device()
    pinned = joined.get("trusted_keys") if isinstance(joined.get("trusted_keys"), dict) else {}
    return {str(k): str(v) for k, v in pinned.items()}


def current() -> Policy | None:
    return load().policy


def blocked_reason() -> str:
    """Why model requests are refused under policy, or an empty string."""
    state = load()
    if state.error:
        return state.error + " Ask your administrator to fix it."
    policy = state.policy
    if policy and policy.expiry_state() == "expired":
        return (
            f"{policy.organization}'s policy expired on {policy.expires_at} and its offline grace "
            "period has ended. Connect so Lumi can fetch a current policy, or ask your administrator."
        )
    return ""


def set_for_tests(policy: Policy | None, error: str = "") -> None:
    """Install a policy directly (tests and fixtures only)."""
    global _state, _loaded
    with _lock:
        _state = PolicyState(policy=policy, error=error, source=policy.source if policy else "")
        _loaded = True
