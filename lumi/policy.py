"""Organization policy: settings an administrator locks, and what may run.

A policy is a ``lumi.policy/v1`` JSON document, supplied machine-wide by IT:

* Windows: the registry value ``Policy`` (the JSON text) or ``PolicyFile`` (a
  path) under ``HKLM\\SOFTWARE\\Policies\\Luminary Analytics\\Lumi``, which
  the ADMX template in ``packaging/policy/`` sets through Group Policy or
  Intune; otherwise ``Lumi\\policy.json`` in the ProgramData folder Windows
  reports (``C:\\ProgramData``; never the ``ProgramData`` environment
  variable, which a person can point anywhere);
* macOS: the ``Policy`` key of the ``com.luminaryanalytics.lumi`` managed
  preferences (a configuration profile), otherwise
  ``/Library/Application Support/Lumi/policy.json``;
* Linux: ``/etc/lumi/policy.json``;
* only where none of those exists, ``LUMI_POLICY_FILE`` names a file (pilots,
  CI). It can't replace a machine policy, so users can't swap in their own.

**Machine policy comes only from places only administrators can write.** The
registry values and a configuration profile's keys are an administrator's by
construction. A file counts only when it and every folder above it, up to a
root the operating system protects, can't be changed by anyone else
(lumi/admin_files.py: the owner and access control list on Windows, owner and
mode elsewhere). Any user may create ``C:\\ProgramData\\Lumi`` where no
administrator did, and a folder an administrator makes there inherits a right
for every user to add files, so neither is enough. A file that fails is
ignored (``IgnoredFile``: Settings shows it, the audit log records
``policy.file_ignored``), and:

* a file a person owns, or one in a folder a person owns, as a file they
  planted would be, reads as absent: the sources below it apply, as if it
  weren't there;
* one an administrator put there (it and its folder are an administrator's,
  ``Trust.admin_owned``) in a place others can change, and the file
  Group Policy's ``PolicyFile`` names, fail closed (``PolicyUnavailable``):
  an administrator meant a policy to apply, so Lumi refuses model requests
  rather than running without it. A ``PolicyFile`` that can't be read (a
  share out of reach, a missing file, a path that isn't a full one) fails
  closed the same way, never falling back to a source further down.

A policy may also be signed: ``{"policy": {...},
"signature": "<base64>", "key_id": "<id>"}`` with an Ed25519 signature over
the policy's canonical JSON. Signed policies verify against keys only an
administrator can set: on Windows only the ``PolicyKeys`` registry value (a
``policy-keys.json`` there isn't read), on macOS the configuration profile's
``PolicyKeys``, on macOS and Linux ``policy-keys.json`` beside the machine
policy file when it passes the same check, and everywhere a machine policy's
``trusted_keys``. That is how Lumi Cloud delivers organization policy (see
"Lumi Cloud policy" below and lumi/cloud.py).
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
      "budgets": [{"scope": "user", "period": "month", "warn_usd": 200, "block_usd": 400}],
      "approvals": {"commands": ["git push --force*", "terraform apply*"], "wait_minutes": 30},
      "oversight": {"activity": true, "messages": "redacted", "security_flags": true,
                    "retention_days": 90, "notice": "Questions: security@acme.example",
                    "unattended": "record"},
      "dlp": {"version": 1, "detectors": {"credit_card": "block", "secrets": "redact"},
              "rules": [{"name": "falcon", "keywords": ["Project Falcon"], "action": "block"}]},
      "legal": {"accepted_by_organization": "Acme"}
    }

``approvals`` lists commands (``fnmatch`` patterns over the whole command)
that a second person in the organization approves in Lumi Cloud before they
run (engine/second_approval.py).

``oversight`` has Lumi share work with the organization's Lumi Cloud
(lumi/oversight.py): each turn's activity, messages at a level (``off``,
``redacted`` or ``full``, secrets removed at every level) and security flags,
kept there for ``retention_days``. It is off unless a policy turns it on, and
the person is always told: Lumi shows a notice naming the organization and
what it receives, and sends nothing to a model until they have confirmed
they read it. ``unattended`` says what a run with nobody to show the notice
to does (a scheduled task or ``lumi run`` with no terminal, as its
environment reports) while nobody has confirmed it as that computer user:
``record`` (the default) runs it, prints the notice with its output and
records it; ``block`` refuses it. Its ``version`` (1, the default) says which keys it
may have. A key or a version this Lumi doesn't know turns oversight off,
with the reason in Settings, and leaves the rest of the policy in force:
Lumi never collects less or more than it can describe, and a newer Lumi
Cloud never blocks model requests on an older Lumi. What it shares has passed
the ``dlp`` rules too (``dlp.shareable``): text they redact is shared redacted,
text they block isn't shared.

``dlp`` holds data loss prevention rules for content sent to model providers
(lumi/dlp.py, docs/dlp.md). A ``dlp`` section that can't be used doesn't make
the policy vanish: the rest still applies, and model requests are refused
(``blocked_reason``) until it's fixed.

``legal`` accepts Lumi's terms for the organization's people:
``{"accepted_by_organization": "Acme Corp"}`` (the organization's name) means
the organization accepted the End User License Agreement, and the Alpha and
Beta Test Terms for pre-release builds, for everyone who uses Lumi on the
computer, under its agreement with Luminary Analytics, so Lumi doesn't ask
each person (lumi/terms.py) and About says who accepted. Only a machine policy
counts (``terms_accepted_by``), from the sources above that only an
administrator can write: the Group Policy key in HKLM (its ``Policy`` value,
or the file its ``PolicyFile`` names, which must pass the same check), a
configuration profile, or the machine policy file where only administrators
can change it. Never ``LUMI_POLICY_FILE`` (even naming the machine file), a
Lumi Cloud policy, Settings or a project, which a person can bring
themselves, and nothing while the policy can't be used.

Locked settings override the user's value and can't be changed in Settings,
which shows who manages them. Lists match ``fnmatch`` patterns.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import admin_files

logger = logging.getLogger(__name__)

SCHEMA = "lumi.policy/v1"
# Files an administrator writes: UTF-8, with or without the byte order mark
# Windows PowerShell 5.1 adds for -Encoding utf8.
ADMIN_TEXT = "utf-8-sig"
REGISTRY_KEY = r"SOFTWARE\Policies\Luminary Analytics\Lumi"
MAC_DOMAIN = "com.luminaryanalytics.lumi"
# Where macOS puts device-scope configuration profile settings, per preference domain.
MAC_MANAGED_PREFERENCES = Path("/Library/Managed Preferences")
PERMISSION_MODES = ("ask", "auto-edit", "plan", "bypass")


class PolicyError(ValueError):
    """A policy that can't be used; the message says why."""


class PolicyUnavailable(Exception):
    """A machine policy an administrator set exists but can't be used as it is, so Lumi fails closed.

    Raised while finding the policy (``_load_text``): the file Group Policy
    names can't be read or others can change it, or an administrator's policy
    file sits where others can change it. The state then refuses model
    requests (``blocked_reason``) and never falls back to a source further
    down, which a person could supply.
    """

    def __init__(self, message: str, source: str = ""):
        super().__init__(message)
        self.source = source


UNSAFE_TITLE = "Policy file ignored: writable by non-administrators"
PROFILE_TITLE = "Configuration profile ignored: writable by non-administrators"


@dataclass(frozen=True)
class IgnoredFile:
    """A machine file Lumi didn't use, and why.

    Settings shows it (``lumi policy`` prints it) and the audit log records it
    (``policy.file_ignored``), so a file that isn't used is never silent.
    ``kind``: ``policy`` (the machine policy file), ``policy_file`` (the file
    Group Policy's ``PolicyFile`` names), ``profile`` (a macOS configuration
    profile), ``policy_keys``, ``license`` or ``license_keys``.
    """

    kind: str
    path: str
    reason: str
    title: str = UNSAFE_TITLE

    def summary(self) -> dict:
        return {"kind": self.kind, "path": self.path, "reason": self.reason, "title": self.title}


# How much of people's messages an organization's oversight receives (lumi/oversight.py).
OVERSIGHT_MESSAGE_LEVELS = ("off", "redacted", "full")
# The section's versions this Lumi understands, and each one's keys.
# ``unattended`` is a version 1 key: no Lumi that read version 1 without it
# was released, and one that doesn't know it turns oversight off (fail safe).
OVERSIGHT_VERSIONS = (1,)
OVERSIGHT_KEYS = frozenset({"version", "activity", "messages", "security_flags", "retention_days", "notice",
                            "project_paths", "unattended"})
OVERSIGHT_NOTICE_LIMIT = 500
# What a run nobody can be shown the notice to does while nobody confirmed it
# as that computer user (lumi/oversight.py): run and record it, or refuse it.
OVERSIGHT_UNATTENDED = ("record", "block")


@dataclass(frozen=True)
class Oversight:
    """What the organization's policy has Lumi share with its Lumi Cloud; nothing by default.

    * ``activity``: each turn's metadata: session, project folder name,
      model, outcome, tool names, cost.
    * ``messages``: the person's message and Lumi's final reply with each
      turn, and the session's title: ``off``, ``redacted`` (without code
      blocks or email addresses, shortened) or ``full`` (as written). Secrets
      are removed at every level.
    * ``security_flags``: refused dangerous commands, policy and file denials,
      declined approvals, removed secrets and signs of prompt injection.
    * ``retention_days``: how long Lumi Cloud keeps it.
    * ``notice``: the organization's own words, shown with Lumi's description.
    * ``project_paths``: full project paths instead of folder names.
    * ``unattended``: what a run with nobody to show the notice to does
      while nobody confirmed it as that computer user: ``record`` (run it,
      print the notice with its output, record it) or ``block`` (refuse it).
    * ``error``: why a section this Lumi can't honor (an unknown key or
      version) turned oversight off; shown in Settings.
    """

    activity: bool = False
    messages: str = "off"
    security_flags: bool = False
    retention_days: int = 90
    notice: str = ""
    project_paths: bool = False
    unattended: str = "record"
    version: int = 1
    error: str = ""

    @property
    def enabled(self) -> bool:
        """Whether anything is shared (messages come only with activity; nothing when ``error``)."""
        return not self.error and (self.activity or self.security_flags)

    def summary(self) -> dict:
        return {"version": self.version, "activity": self.activity, "messages": self.messages,
                "security_flags": self.security_flags, "retention_days": self.retention_days,
                "notice": self.notice, "project_paths": self.project_paths, "unattended": self.unattended,
                "enabled": self.enabled, "error": self.error}


def _oversight(value: Any) -> Oversight:
    """The ``oversight`` section.

    A version or a key this Lumi doesn't know turns oversight off with the
    reason (``Oversight.error``) rather than guessing at what the organization
    wants collected, and without invalidating the rest of the policy: a
    newer Lumi Cloud's section must never stop model requests on this
    computer. Other mistakes (a value of the wrong type) make the policy
    invalid, like a mistake in any section.
    """
    if value is None:
        return Oversight()
    if not isinstance(value, dict):
        raise PolicyError("oversight must be an object.")
    version = value.get("version", 1)
    if isinstance(version, bool) or version not in OVERSIGHT_VERSIONS:
        shown = json.dumps(version) if isinstance(version, (int, float, str, bool)) or version is None else "that"
        error = (f"The policy's oversight section is version {shown[:40]}, which this version of Lumi doesn't "
                 "understand, so it collects nothing. Update Lumi, or ask your administrator.")
        logger.warning("Organization oversight is off: %s", error)
        return Oversight(error=error)
    unknown = sorted(str(key) for key in value if key not in OVERSIGHT_KEYS)
    if unknown:
        names = ", ".join(name[:40] for name in unknown[:5])
        error = (f"The policy's oversight section asks for {names}, which this version of Lumi doesn't "
                 "understand, so it collects nothing. Update Lumi, or ask your administrator.")
        logger.warning("Organization oversight is off: %s", error)
        return Oversight(error=error)
    activity = _flag(value.get("activity"), "oversight.activity")
    messages = value.get("messages", "off")
    if messages is None:
        messages = "off"
    if messages not in OVERSIGHT_MESSAGE_LEVELS:
        raise PolicyError('oversight.messages must be "off", "redacted" or "full".')
    if messages != "off" and not activity:
        raise PolicyError('oversight.messages needs "activity": true; messages are shared with each turn\'s activity.')
    retention = value.get("retention_days", 90)
    if isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 3650:
        raise PolicyError("oversight.retention_days must be a whole number of days from 1 to 3650.")
    notice = value.get("notice") or ""
    if not isinstance(notice, str) or len(notice) > OVERSIGHT_NOTICE_LIMIT:
        raise PolicyError(f"oversight.notice must be text of at most {OVERSIGHT_NOTICE_LIMIT} characters.")
    unattended = value.get("unattended", "record")
    if unattended is None:
        unattended = "record"
    if unattended not in OVERSIGHT_UNATTENDED:
        raise PolicyError('oversight.unattended must be "record" or "block".')
    return Oversight(activity=activity, messages=messages,
                     security_flags=_flag(value.get("security_flags"), "oversight.security_flags"),
                     retention_days=retention, notice=" ".join(notice.split()),
                     project_paths=_flag(value.get("project_paths"), "oversight.project_paths"),
                     unattended=unattended)


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
    # Capability pack publishers the organization trusts ({key_id: {name, public_key}},
    # engine/pack_signing.py), and whether packs they didn't sign stay off.
    publishers: dict[str, dict] = field(default_factory=dict)
    require_signed: bool = False
    # The organization's registry of packs, each pinned to a commit and maybe a
    # content digest ({pack id: entry}), and whether every other pack stays off.
    registry: dict[str, dict] = field(default_factory=dict)
    registry_only: bool = False
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
    # Commands a second person approves in Lumi Cloud before they run (engine/second_approval.py).
    approval_commands: tuple[str, ...] = ()
    approval_wait_minutes: int = 30
    # What Lumi shares with the organization's Lumi Cloud (lumi/oversight.py); nothing unless set.
    oversight: Oversight = field(default_factory=Oversight)
    # Data loss prevention rules (lumi/dlp.py: a DlpPolicy), or why the dlp
    # section can't be used, which refuses model requests (blocked_reason).
    dlp: Any = None
    dlp_error: str = ""
    # The organization that accepted Lumi's terms for this computer's people
    # (legal.accepted_by_organization); it counts only in a machine policy
    # (terms_accepted_by, lumi/terms.py).
    terms_accepted_by: str = ""
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
            "pack_publishers": sorted(entry["name"] for entry in self.publishers.values()),
            "require_signed_packs": self.require_signed,
            "registry_packs": sorted(self.registry),
            "registry_only": self.registry_only,
            "prices": [pattern for pattern, _ in self.prices],
            "budgets": len(self.budgets),
            "capability_overrides": [pattern for pattern, _ in self.capability_overrides],
            "require_zero_retention": self.require_zero_retention,
            "zero_retention_providers": list(self.zero_retention_providers),
            "approval_commands": list(self.approval_commands),
            "oversight": self.oversight.summary(),
            # Rule names and actions only: keywords and patterns can name what they protect.
            "dlp": self.dlp.summary() if self.dlp is not None else None,
            "dlp_error": self.dlp_error,
            "terms_accepted_by": self.terms_accepted_by,
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


def _pack_publishers(value: Any) -> dict[str, dict]:
    if value is None:
        return {}
    from .engine.pack_signing import PackSigningError, publishers

    try:
        return publishers(value)
    except PackSigningError as exc:
        raise PolicyError(f"extensions.trusted_publishers: {exc}") from exc


def _pack_registry(value: Any) -> dict[str, dict]:
    """The organization's registry of packs by id; PolicyError for an entry Lumi can't pin."""
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) > 500:
        raise PolicyError("extensions.registry must be a list of up to 500 packs.")
    registry: dict[str, dict] = {}
    for entry in value:
        entry = entry if isinstance(entry, dict) else {}
        pack_id, url = str(entry.get("id") or ""), str(entry.get("url") or "").strip()
        commit, digest = str(entry.get("commit") or "").lower(), str(entry.get("digest") or "").lower()
        subdir = str(entry.get("subdir") or "").strip("/")
        if not (re.fullmatch(r"[A-Za-z0-9._-]{1,80}", pack_id) and url.startswith("https://")
                and re.fullmatch(r"[0-9a-f]{40}", commit) and (not digest or re.fullmatch(r"[0-9a-f]{64}", digest))
                and (not subdir or (re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", subdir)
                                    and ".." not in subdir.split("/")))):
            raise PolicyError(f"extensions.registry: {pack_id or 'an entry'} needs an id, an https url, a "
                              "40-character commit, and optionally a folder and a 64-character digest.")
        registry[pack_id] = {"id": pack_id, "name": " ".join(str(entry.get("name") or pack_id).split())[:120],
                             "url": url, "commit": commit, "subdir": subdir, "digest": digest}
    return registry


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


def _section(document: dict, name: str) -> dict:
    """A section of the document: an object, or {} when it's missing or null.

    A section of any other type is a mistake, not an empty section. Read as
    empty, ``"permissions": "ask only"`` would drop the limits the
    administrator meant to set.
    """
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{name} must be an object.")
    return value


def _dlp_section(document: dict) -> tuple[Any, str]:
    """The dlp section's rules, or why they can't be used.

    A mistake here is contained: the rest of the policy still applies, and
    blocked_reason refuses model requests until the section is fixed, so an
    organization's DLP rules never silently stop applying.
    """
    value = document.get("dlp")
    if value is None:
        return None, ""
    from .dlp import DlpError, parse_section

    try:
        return parse_section(value), ""
    except DlpError as exc:
        return None, str(exc)
    except Exception as exc:  # anything else that stops the rules from being built
        return None, f"The dlp section couldn't be read ({type(exc).__name__})."


def _terms_accepted_by(section: dict) -> str:
    """``legal.accepted_by_organization``: the name of the organization that accepted Lumi's terms, or ''.

    A value that isn't a name makes the policy invalid (model requests stop until it's fixed), like a
    mistake in any section: read loosely, it could accept the terms for people nobody asked.
    """
    value = section.get("accepted_by_organization")
    if value is None:
        return ""
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name or len(name) > 200:
        raise PolicyError("legal.accepted_by_organization must be the name of the organization that accepts "
                          "Lumi's terms for its people (at most 200 characters).")
    return name


def _grace_days(value: Any) -> int:
    """``grace_days`` as a whole number of days; null means none."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PolicyError("grace_days must be a whole number of days.")
    try:
        return max(0, int(value))
    except (ValueError, OverflowError) as exc:
        raise PolicyError("grace_days must be a whole number of days.") from exc


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

    settings = _section(document, "settings")
    if not all(isinstance(k, str) and "." in k for k in settings):
        raise PolicyError("'settings' must map 'section.key' names to values.")
    # Which Lumi Cloud this computer uses, and the sign-in recorded for it, come only from the machine
    # policy's own 'cloud' section (lumi/cloud.py): a lock that moved the address would take the
    # person's account tokens to another Lumi Cloud. Tasks from chat may still be turned off.
    cloud_locks = sorted(name for name in settings if name.startswith("cloud.") and name != "cloud.remote_tasks")
    if cloud_locks:
        raise PolicyError(f"'settings' can't lock '{cloud_locks[0]}': the Lumi Cloud address comes only from a "
                          "machine policy's 'cloud' section (only 'cloud.remote_tasks' may be locked).")
    if "cloud.remote_tasks" in settings and not isinstance(settings["cloud.remote_tasks"], bool):
        raise PolicyError("'cloud.remote_tasks' must be true or false.")
    from .feedback import validate_policy_settings as validate_feedback_settings

    try:
        # privacy.feedback, privacy.feedback_diagnostics and privacy.feedback_url (lumi/feedback.py)
        validate_feedback_settings(settings)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    from .offline_rules import validate_policy_settings as validate_offline_settings
    from .update_channels import validate_policy_settings

    try:
        validate_policy_settings(settings)
        # A list Lumi can't read must not become "no hosts" or "all hosts".
        validate_offline_settings(settings)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    if "security.shell_sandbox" in settings and settings["security.shell_sandbox"] not in ("off", "project"):
        raise PolicyError("'security.shell_sandbox' must be \"off\" or \"project\".")
    from .voice import validate as validate_voice

    for name in [name for name in settings if name.startswith("voice.")]:
        try:
            validate_voice(name.partition(".")[2], settings[name])
        except ValueError as exc:
            raise PolicyError(f"'{name}': {exc}") from exc
    if "review.agent_changes" in settings and not isinstance(settings["review.agent_changes"], bool):
        raise PolicyError("'review.agent_changes' must be true or false.")
    reviewers = settings.get("review.reviewers", [])
    if not isinstance(reviewers, list) or not all(isinstance(name, str) for name in reviewers):
        raise PolicyError("'review.reviewers' must list GitHub usernames or organization/team names.")
    for name in ("code_hosts.github_hosts", "code_hosts.gitlab_hosts"):
        # The hosts that may receive the GitHub or GitLab token (engine/github_tools.token_hosts).
        if name in settings:
            from .net import host_names

            if not isinstance(settings[name], list):
                raise PolicyError(f"'{name}' must list host names.")
            try:
                host_names(settings[name])
            except ValueError as exc:
                raise PolicyError(f"'{name}': {exc}") from exc
    permissions = _section(document, "permissions")
    modes = permissions.get("allowed_modes")
    allowed_modes = _patterns(modes, "permissions.allowed_modes") if modes is not None else None
    if allowed_modes is not None:
        unknown = [m for m in allowed_modes if m not in PERMISSION_MODES]
        if unknown or not allowed_modes:
            raise PolicyError(f"permissions.allowed_modes must list some of {', '.join(PERMISSION_MODES)}.")
    models = _section(document, "models")
    mcp = _section(document, "mcp")
    allow_stdio = mcp.get("allow_stdio")
    if allow_stdio is not None and not isinstance(allow_stdio, bool):
        # Read loosely, "no" would allow command-based servers.
        raise PolicyError("mcp.allow_stdio must be true or false.")
    extensions = _section(document, "extensions")
    files = _section(document, "files")
    cloud_section = _section(document, "cloud")  # read by load(); another type would silently skip enrollment
    if cloud_section.get("url") is not None:
        from .cloud import CloudError, normalize_url

        try:
            normalize_url(str(cloud_section.get("url") or ""))
        except CloudError as exc:
            raise PolicyError(f"cloud.url: {exc}") from exc
    shell = _section(document, "shell")
    shell_rules = shell.get("rules") or []
    if not isinstance(shell_rules, list) or not all(isinstance(rule, dict) for rule in shell_rules):
        raise PolicyError("shell.rules must be a list of rule objects.")
    if shell_rules:
        # Checked now, so a rule that can't be applied makes the policy
        # invalid (model requests stop) instead of failing a tool call.
        from .engine.policies import ExecutionPolicy

        try:
            ExecutionPolicy.from_rules(shell_rules)
        except ValueError as exc:
            raise PolicyError(f"shell.rules {exc}.") from exc
    expires_at = str(document.get("expires_at") or "")
    if expires_at:
        _parse_time(expires_at)
    from .pricing import parse_prices

    try:
        prices = parse_prices(_section(document, "pricing").get("prices") or {})
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
    raw_keys = document.get("trusted_keys")
    if raw_keys is None:
        raw_keys = {}
    if not isinstance(raw_keys, dict) or not all(isinstance(value, str) for value in raw_keys.values()):
        raise PolicyError("trusted_keys must map key ids to base64 Ed25519 public keys.")
    approvals = document.get("approvals")
    if approvals is None:
        approvals = {}
    if not isinstance(approvals, dict):
        raise PolicyError("approvals must be an object with commands and wait_minutes.")
    approval_commands = _patterns(approvals.get("commands"), "approvals.commands")
    if len(approval_commands) > 100 or any(len(pattern) > 300 for pattern in approval_commands):
        raise PolicyError("approvals.commands holds at most 100 patterns of 300 characters.")
    wait_minutes = approvals.get("wait_minutes", 30)
    if isinstance(wait_minutes, bool) or not isinstance(wait_minutes, int) or not 1 <= wait_minutes <= 240:
        raise PolicyError("approvals.wait_minutes must be a whole number from 1 to 240.")
    oversight = _oversight(document.get("oversight"))
    dlp, dlp_error = _dlp_section(document)
    terms_accepted_by = _terms_accepted_by(_section(document, "legal"))

    return Policy(
        organization=str(document.get("organization") or "your organization"),
        source=source,
        signed=signed,
        issued_at=str(document.get("issued_at") or ""),
        expires_at=expires_at,
        grace_days=_grace_days(document.get("grace_days", 7)),
        settings=dict(settings),
        allowed_modes=allowed_modes,
        models_allowed=_patterns(models["allowed"], "models.allowed") if "allowed" in models else None,
        models_blocked=_patterns(models.get("blocked"), "models.blocked"),
        require_zero_retention=_flag(models.get("require_zero_retention"), "models.require_zero_retention"),
        zero_retention_providers=_patterns(models.get("zero_retention_providers"),
                                           "models.zero_retention_providers"),
        exclude=_patterns(files.get("exclude"), "files.exclude"),
        shell_rules=tuple(shell_rules),
        mcp_allowed=_patterns(mcp["allowed_servers"], "mcp.allowed_servers") if "allowed_servers" in mcp else None,
        mcp_allow_stdio=allow_stdio is not False,
        packs_allowed=(
            _patterns(extensions["allowed_packs"], "extensions.allowed_packs")
            if "allowed_packs" in extensions else None
        ),
        sources_allowed=(
            _patterns(extensions["allowed_sources"], "extensions.allowed_sources")
            if "allowed_sources" in extensions else None
        ),
        publishers=_pack_publishers(extensions.get("trusted_publishers")),
        require_signed=_flag(extensions.get("require_signed"), "extensions.require_signed"),
        registry=_pack_registry(extensions.get("registry")),
        registry_only=_flag(extensions.get("registry_only"), "extensions.registry_only"),
        trusted_keys={str(k): str(v) for k, v in raw_keys.items()},
        prices=prices,
        budgets=budgets,
        capability_overrides=capability_overrides,
        approval_commands=approval_commands,
        approval_wait_minutes=wait_minutes,
        oversight=oversight,
        dlp=dlp,
        dlp_error=dlp_error,
        terms_accepted_by=terms_accepted_by,
        raw=document,
    )


# ── Where machine policy lives ──────────────────────────────────────────────


REGISTRY_VALUES = ("Policy", "PolicyFile", "PolicyKeys")


def _registry_values() -> dict[str, str]:
    """The values set under the policy registry key (HKLM), as text; {} where there is no key.

    Only an administrator (Group Policy, Intune, the MSI) can write this key,
    so it is trusted as it is. A key that exists but can't be read raises
    OSError: only an administrator could have made it so, and what it says
    can't be known, so the policy fails closed. REG_MULTI_SZ lines (the ADMX
    ``multiText`` element stores one line per string) are joined.
    """
    if sys.platform != "win32":
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY)
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    with key:
        for name in REGISTRY_VALUES:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                continue
            values[name] = "\n".join(str(line) for line in value) if isinstance(value, list) else str(value)
    return values


def _registry_policy() -> tuple[str, str] | None:
    """(json text, source) from the Windows policy registry key, if set.

    ``Policy`` holds the document. ``PolicyFile`` names a file, which must be
    readable and only administrators may be able to change (``_policy_file``);
    otherwise this raises PolicyUnavailable and the policy fails closed.
    """
    try:
        values = _registry_values()
    except OSError as exc:
        raise PolicyUnavailable(f"The Group Policy key HKLM\\{REGISTRY_KEY} couldn't be read "
                                f"({exc.strerror or exc}).", source=f"Group Policy (HKLM\\{REGISTRY_KEY})") from exc
    document = values.get("Policy", "")
    if document.strip():
        return document, f"Group Policy (HKLM\\{REGISTRY_KEY})"
    named = values.get("PolicyFile", "").strip()
    return _policy_file(named) if named else None


def _policy_file(value: str) -> tuple[str, str]:
    """The policy in the file Group Policy's ``PolicyFile`` names, or PolicyUnavailable.

    Group Policy says this computer has a policy, so every way the file can't
    be used fails closed rather than falling back to a source further down,
    which a person could supply: a path that isn't a full one (a variable
    other than a machine folder stays as written), an alternate data stream
    (``policy.json:other``), a file that can't be read (a share out of reach,
    a missing file), and a file that people who aren't administrators can
    change, or that sits where they can (lumi/admin_files.py).

    The check and the read open the path separately; the check proved the
    file and the folders above it can be changed only by administrators, so
    replacing either in between takes administrator rights.
    """
    text = expand_machine_variables(value)
    source = f"{text} (set by Group Policy)"
    if not os.path.isabs(text):
        raise PolicyUnavailable(f"The policy file Group Policy names, {text}, isn't a full path.", source)
    if admin_files.names_stream(text):
        raise PolicyUnavailable(f"The policy file Group Policy names, {text}, is an alternate data stream, "
                                "not a file.", source)
    path = Path(text)
    try:
        path.stat()
    except OSError as exc:
        raise PolicyUnavailable(f"The policy file Group Policy names couldn't be read: {text} "
                                f"({exc.strerror or exc}).", source) from exc
    trust = admin_files.check(path, _protected_root(path))
    if not trust.trusted:
        _ignore(IgnoredFile("policy_file", text, trust.reason))
        raise PolicyUnavailable(f"{UNSAFE_TITLE}. {trust.reason}. Group Policy names {text} as this "
                                "computer's organization policy, and only administrators may be able to "
                                "change it and the folders above it.", source)
    try:
        return path.read_text(encoding=ADMIN_TEXT), source
    except OSError as exc:
        raise PolicyUnavailable(f"The policy file Group Policy names couldn't be read: {text} "
                                f"({exc.strerror or exc}).", source) from exc


def _macos_managed_policy() -> tuple[str, str] | None:
    """(json text, source) from a configuration profile (Jamf, Intune, any MDM), if one sets it.

    macOS writes managed preferences as root; a plist anyone else could have
    written is ignored (``_usable``).
    """
    if sys.platform != "darwin":
        return None
    path = MAC_MANAGED_PREFERENCES / f"{MAC_DOMAIN}.plist"
    if not path.is_file() or not _usable(path, MAC_MANAGED_PREFERENCES.parent, "profile", fail_closed=True,
                                         title=PROFILE_TITLE):
        return None
    return managed_preferences_policy(path)


def managed_preferences_policy(path: Path) -> tuple[str, str] | None:
    """The ``Policy`` key of a managed preferences plist: a JSON string or a dictionary.

    A profile with no ``Policy`` key sets no policy. One that can't be read, or
    whose ``Policy`` is empty or another type, raises ValueError, so the policy
    fails closed (load) rather than reading as "no policy".
    """
    if not path.is_file():
        return None
    import plistlib

    source = f"configuration profile ({MAC_DOMAIN})"
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception as exc:
        raise ValueError(f"The {source} couldn't be read from {path}: {exc}") from exc
    if not isinstance(data, dict) or "Policy" not in data:
        return None
    value = data["Policy"]
    if isinstance(value, dict):
        return json.dumps(value), source
    if isinstance(value, str) and value.strip():
        return value, source
    kind = "an empty string" if isinstance(value, str) else type(value).__name__
    raise ValueError(f"The {source} sets Policy to {kind}; it must be the policy's JSON text or a dictionary.")


def managed_preferences_keys(path: Path) -> str:
    """The ``PolicyKeys`` of a managed preferences plist as JSON text, or "" (like the registry's)."""
    if not path.is_file():
        return ""
    import plistlib

    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        return ""  # managed_preferences_policy reports a profile that can't be read
    value = data.get("PolicyKeys") if isinstance(data, dict) else None
    if isinstance(value, dict):
        return json.dumps(value)
    return value if isinstance(value, str) else ""


_windows_folders: dict[str, str] = {}


def _known_folder(guid: str, fallback: str) -> str:
    """A Windows folder as the operating system reports it (SHGetKnownFolderPath).

    Never from the environment: a person can start Lumi with ``ProgramData``
    or ``SystemDrive`` pointing at a folder they control, and a policy or a
    trusted key found there would be theirs, not an administrator's. The
    folders read here are fixed ones, which a person can't redirect either.
    """
    if guid in _windows_folders:
        return _windows_folders[guid]
    folder = fallback
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        value = uuid.UUID(guid)
        known = _GUID(value.fields[0], value.fields[1], value.fields[2], (ctypes.c_ubyte * 8)(*value.bytes[8:]))
        path = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(ctypes.byref(known), 0, None, ctypes.byref(path))
        try:
            if result == 0 and path.value:
                folder = path.value
        finally:
            ctypes.WinDLL("ole32").CoTaskMemFree(path)
    except Exception:  # not Windows, or no shell: the default location
        logger.debug("The known folder %s couldn't be read; using %s", guid, fallback, exc_info=True)
    _windows_folders[guid] = folder
    return folder


def _program_data() -> str:
    return _known_folder("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97", r"C:\ProgramData")  # FOLDERID_ProgramData


def expand_machine_variables(text: str) -> str:
    """``%ProgramData%`` and other machine folders in a path an administrator set, from the operating system.

    Only machine folders expand (ProgramData, ALLUSERSPROFILE, ProgramFiles,
    SystemRoot, windir, SystemDrive); anything else stays as written, so a
    path that depends on a person's environment isn't a full path, and the
    policy fails closed, rather than pointing at a folder they chose.
    """
    windows = _known_folder("F38BF404-1D43-42F2-9305-67DE0B28FC23", r"C:\Windows")  # FOLDERID_Windows
    values = {"programdata": _program_data(), "allusersprofile": _program_data(),
              "programfiles": _known_folder("905E63B6-C1BF-494E-B29C-65B732D3D21A", r"C:\Program Files"),
              "systemroot": windows, "windir": windows, "systemdrive": windows[:2]}
    return re.sub(r"%([^%]+)%", lambda match: values.get(match.group(1).lower(), match.group(0)), text)


def machine_policy_file() -> Path:
    if sys.platform == "win32":
        return Path(_program_data()) / "Lumi" / "policy.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Lumi/policy.json")
    return Path("/etc/lumi/policy.json")


def machine_root() -> Path:
    """The protected folder the machine policy folder sits in: ProgramData, /Library/Application Support or /etc.

    Checks of the files beside the machine policy stop here (lumi/admin_files.py).
    """
    return machine_policy_file().parent.parent


def _protected_root(path: Path) -> Path | None:
    """ProgramData, for a file under it that Group Policy names; otherwise the drive's or share's root."""
    if sys.platform != "win32":
        return None
    root = Path(_program_data())
    inside = os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(str(root)).rstrip("\\") + "\\")
    return root if inside else None


def _usable(path: Path, root: Path, kind: str, *, fail_closed: bool = False, title: str = UNSAFE_TITLE) -> bool:
    """Whether a machine file only an administrator can have written may be used (lumi/admin_files.py).

    One that others could have written or can change is ignored, visibly
    (``_ignore``). With ``fail_closed``, a policy an administrator put there
    (``Trust.admin_owned``) raises PolicyUnavailable: an administrator meant
    it to apply, so Lumi refuses model requests until it's moved or its
    folder is locked down. Any other reads as absent, as a file a person
    planted must. Callers then read the file by its path: replacing it, or a
    folder above it, after this check takes administrator rights.
    """
    trust = admin_files.check(path, root)
    if trust.trusted:
        return True
    _ignore(IgnoredFile(kind, str(path), trust.reason, title))
    if fail_closed and trust.admin_owned:
        raise PolicyUnavailable(f"{title}. {trust.reason}. An administrator put {path} there, and only "
                                "administrators may be able to change it and the folders above it.", str(path))
    return False


def _machine_file_policy() -> tuple[str, str] | None:
    path = machine_policy_file()
    if path.is_file() and _usable(path, machine_root(), "policy", fail_closed=True):
        return path.read_text(encoding=ADMIN_TEXT), str(path)
    return None


class _Found(tuple):
    """What ``_load_text`` found: ``(text, source)``, which unpacks like any pair.

    ``machine`` says a machine source gave it: the Group Policy key in HKLM
    (``Policy``, or the file its ``PolicyFile`` names), a configuration
    profile, or the machine policy file, each only where an administrator can
    have written it. ``LUMI_POLICY_FILE``, which a person can set, never is,
    even when it names the same file. Only a machine policy may accept Lumi's
    terms for an organization (``terms_accepted_by``).
    """

    machine: bool = False

    def __new__(cls, text: str, source: str, *, machine: bool) -> "_Found":
        found = super().__new__(cls, (text, source))
        found.machine = machine
        return found


def _load_text() -> tuple[str, str] | None:
    """The policy text and where it came from. Machine sources always win.

    A machine source that can't be used as it is raises PolicyUnavailable
    (fail closed); one that only someone other than an administrator can have
    written is skipped as if absent (``_usable``). The result is a ``_Found``,
    which says whether a machine source gave it.
    """
    for finder in (_registry_policy, _macos_managed_policy, _machine_file_policy):
        try:
            found = finder()
        except (OSError, ValueError) as exc:  # ValueError: a file that isn't UTF-8 text
            # A machine source that can't be read is an administrator's policy that can't be used.
            raise PolicyUnavailable(f"The policy file couldn't be read: {exc}") from exc
        if found:
            text, source = found
            return _Found(text, source, machine=True)
    override = os.environ.get("LUMI_POLICY_FILE", "").strip()
    if override:
        path = Path(override)
        return _Found(path.read_text(encoding=ADMIN_TEXT), str(path), machine=False)
    return None


WINDOWS_KEYS_FILE_TITLE = "Policy signing keys file ignored: not read on Windows"
WINDOWS_KEYS_FILE_REASON = ("On Windows, Lumi takes policy signing keys only from Group Policy (the PolicyKeys "
                            "registry value) and from the machine policy's trusted_keys, which only administrators "
                            "can set")


def _note_unread_files() -> None:
    """Note machine files this platform's Lumi never reads, so an administrator can see why they don't apply."""
    if sys.platform == "win32":
        keys_file = machine_policy_file().with_name("policy-keys.json")
        if keys_file.is_file():
            _ignore(IgnoredFile("policy_keys", str(keys_file), WINDOWS_KEYS_FILE_REASON, WINDOWS_KEYS_FILE_TITLE))


def machine_keys() -> dict[str, str]:
    """Signing keys an administrator trusts, for signed policies and Lumi Cloud's.

    * Windows: only ``PolicyKeys`` in the policy registry key (Group Policy,
      Intune), which only an administrator can set. ``policy-keys.json`` isn't
      read there: these keys decide which downloaded policy replaces the
      machine's, and a file under ProgramData is an administrator's only while
      its folder stays locked down, which is easy to get wrong.
    * macOS: ``PolicyKeys`` of the configuration profile.
    * macOS and Linux: ``policy-keys.json`` beside the machine policy file,
      when only root can have written it (lumi/admin_files.py).

    Keys that are ignored only leave fewer keys: a downloaded policy they
    would have verified isn't applied, and the machine policy stays.
    """
    texts: list[str] = []
    if sys.platform == "win32":
        try:
            value = _registry_values().get("PolicyKeys", "")
        except OSError:
            value = ""  # _registry_policy fails the policy closed
        if value.strip():
            texts.append(value)
    else:
        if sys.platform == "darwin":
            profile = MAC_MANAGED_PREFERENCES / f"{MAC_DOMAIN}.plist"
            if profile.is_file() and _usable(profile, MAC_MANAGED_PREFERENCES.parent, "profile", title=PROFILE_TITLE):
                profile_keys = managed_preferences_keys(profile)
                if profile_keys:
                    texts.append(profile_keys)
        keys_file = machine_policy_file().with_name("policy-keys.json")
        if keys_file.is_file() and _usable(keys_file, machine_root(), "policy_keys",
                                           title="Policy signing keys ignored: writable by non-administrators"):
            try:
                texts.append(keys_file.read_text(encoding=ADMIN_TEXT))
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
    # The machine policy (``machine``, or ``policy`` when it isn't Lumi
    # Cloud's) came from a machine source only an administrator can write
    # (``_Found.machine``: the HKLM Group Policy key or the PolicyFile it
    # names, a configuration profile, the machine policy file), not
    # LUMI_POLICY_FILE. Only then may it accept Lumi's terms (terms_accepted_by).
    # With ``error``: the policy that can't be used is such a source's (machine_error).
    from_machine: bool = False
    # Machine files found but not used (see IgnoredFile), for Settings to show.
    ignored: tuple[IgnoredFile, ...] = ()


# ── Files Lumi ignores ──────────────────────────────────────────────────────
#
# Every machine file that isn't used is logged, listed in the state Settings
# shows (while a load collects them) and recorded once per process in the
# audit log, so an administrator sees why a file doesn't apply and a planted
# one leaves a trace.

_collecting: list[IgnoredFile] | None = None
_recorded: set[tuple[str, str, str]] = set()
_recorded_lock = threading.Lock()


def _ignore(item: IgnoredFile) -> None:
    logger.warning("%s: %s (%s)", item.title, item.path, item.reason)
    if _collecting is not None and item not in _collecting:
        _collecting.append(item)


def _record_ignored(items: tuple[IgnoredFile, ...] | list[IgnoredFile]) -> None:
    """``policy.file_ignored`` in the audit log, once per file and reason in this process. Never raises."""
    fresh = []
    with _recorded_lock:
        for item in items:
            if (item.kind, item.path, item.reason) not in _recorded:
                _recorded.add((item.kind, item.path, item.reason))
                fresh.append(item)
    if not fresh:
        return
    try:
        from . import audit

        for item in fresh:
            audit.record("policy.file_ignored", kind=item.kind, path=audit.name(item.path),
                         reason=audit.name(item.reason))
    except Exception:
        logger.debug("Couldn't record an ignored machine file", exc_info=True)


def note_ignored(item: IgnoredFile) -> None:
    """Log and audit a machine file another module (lumi/license.py) didn't use; it shows its own status."""
    logger.warning("%s: %s (%s)", item.title, item.path, item.reason)
    _record_ignored((item,))


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
        data = json.loads((state_home() / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return {}
    section = data.get("cloud") if isinstance(data, dict) else None
    device = section.get("device") if isinstance(section, dict) else None
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
    except Exception as exc:  # any unusable download, not only the failures parse() names
        note = f"{machine.organization}'s machine policy applies instead." if machine else ""
        return PolicyState(policy=machine, source=state.source, machine=None,
                           cloud_error=f"The Lumi Cloud policy can't be used: {exc} {note}".strip(),
                           from_machine=state.from_machine)
    return PolicyState(policy=cloud, source=cloud.source, cloud=True, machine=machine,
                       from_machine=state.from_machine)


_lock = threading.Lock()
_state = PolicyState()
_loaded = False


def load(*, force: bool = False) -> PolicyState:
    """Read machine policy once (or again with ``force``) and return it.

    Never raises. A policy that exists but can't be read or used is an error
    state, which refuses model requests (blocked_reason), on this call and
    every later one: a mistake in it must never read as "no policy". Machine
    files that weren't used are in ``ignored`` and the audit log.
    """
    global _state, _loaded
    with _lock:
        if _loaded and not force:
            return _state
        try:
            _state = _read_state()
        except Exception as exc:  # whatever went wrong, fail closed
            logger.exception("The organization policy couldn't be loaded")
            # Whichever source failed, Lumi can't tell what an administrator's policy says (machine_error).
            _state = PolicyState(error=f"The organization policy couldn't be loaded: {exc}", from_machine=True)
        _loaded = True
        state = _state
    _record_ignored(state.ignored)
    return state


def _read_state() -> PolicyState:
    """The policy in force (see load), with the machine files it didn't use."""
    global _collecting
    ignored: list[IgnoredFile] = []
    _collecting = ignored
    try:
        _note_unread_files()
        state = _state_from_sources()
    finally:
        _collecting = None
    return replace(state, ignored=tuple(ignored)) if ignored else state


def _state_from_sources() -> PolicyState:
    """The policy in force: the machine policy, Lumi Cloud's, or none (see load)."""
    try:
        found = _load_text()
    except PolicyUnavailable as exc:  # an administrator's policy Lumi can't use as it is: fail closed
        return PolicyState(error=str(exc), source=exc.source, from_machine=True)
    except (OSError, ValueError) as exc:  # LUMI_POLICY_FILE; ValueError: a file that isn't UTF-8 text
        return PolicyState(error=f"The policy file couldn't be read: {exc}")
    if not found:
        # No machine policy: an organization this person joined in the app.
        joined = _joined_device()
        if not joined:
            return PolicyState()
        keys = joined.get("trusted_keys") if isinstance(joined.get("trusted_keys"), dict) else {}
        return _with_cloud_policy(PolicyState(), {str(k): str(v) for k, v in keys.items()})
    text, source = found
    # Anything but a machine source (LUMI_POLICY_FILE, or a stand-in that says nothing) never accepts terms.
    from_machine = getattr(found, "machine", False) is True
    try:
        data = json.loads(text)
        keys = machine_keys()
        named = data.get("trusted_keys") if isinstance(data, dict) and "signature" not in data else None
        if isinstance(named, dict):
            # The machine policy may name keys that sign policies it doesn't
            # hold itself (for example the organization's Lumi Cloud policy).
            keys.update({str(k): str(v) for k, v in named.items()})
        policy = parse(data, source=source, trusted_keys=keys)
    except Exception as exc:
        # A broken machine policy must not silently mean "no policy": model
        # requests are refused until IT fixes it (see blocked_reason). That
        # holds for any failure, not only the mistakes parse() names.
        return PolicyState(error=f"The organization policy at {source} is invalid: {exc}", source=source,
                           from_machine=from_machine)
    state = PolicyState(policy=policy, source=source, from_machine=from_machine)
    if isinstance(policy.raw.get("cloud"), dict):
        state = _with_cloud_policy(state, keys)
    return state


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


def oversight_settings() -> Oversight:
    """What the policy in force has Lumi share with the organization's Lumi Cloud (off without one)."""
    policy = current()
    return policy.oversight if policy is not None else Oversight()


def enrolled_device() -> dict:
    """This computer's Lumi Cloud enrollment from settings.json ({} when it isn't enrolled)."""
    return dict(_joined_device())


def terms_accepted_by() -> tuple[str, str]:
    """(organization, source) when the machine policy accepts Lumi's terms for this computer's people.

    Only a machine policy from a source only an administrator can write counts
    (``PolicyState.from_machine``): the HKLM Group Policy key or the file its ``PolicyFile`` names, a
    configuration profile, or the machine policy file (lumi/admin_files.py checks the files). Its
    ``legal.accepted_by_organization`` holds while a Lumi Cloud policy it set up is in force. A Lumi Cloud
    policy, ``LUMI_POLICY_FILE``, Settings and projects can't accept for anyone. ('', '') otherwise, and
    while the policy can't be used (a PolicyFile that can't be read fails closed, so it accepts nothing).
    """
    state = load()
    machine = state.machine if state.cloud else state.policy
    if state.error or not state.from_machine or machine is None or not machine.terms_accepted_by:
        return "", ""
    return machine.terms_accepted_by, machine.source


def machine_error() -> str:
    """Why an administrator's machine policy can't be used (``blocked_reason``), or ''.

    Such a policy decides nothing until it's fixed, not even whether the organization accepted Lumi's
    terms for this computer's people (``terms_accepted_by``): Lumi shows this error instead of asking the
    person to accept the terms, and records no personal acceptance meanwhile (lumi/terms.py). A
    ``LUMI_POLICY_FILE`` that can't be used, which a person set, blocks model requests too, but can't
    accept for anyone, so the terms stay the person's.
    """
    state = load()
    return blocked_reason() if state.error and state.from_machine else ""


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
    if policy and policy.dlp_error:
        # Fail closed: without its rules, nothing may leave for a model provider.
        return (
            f"{policy.organization}'s data loss prevention rules can't be applied: {policy.dlp_error} "
            "Lumi won't send model requests until your administrator fixes the policy's dlp section."
        )
    return ""


def full_auto_refusal() -> str:
    """Why missions and autonomous sessions can't run under policy, or an empty string.

    Their specialists run in Full-auto (``bypass``), since nobody is there to
    answer an approval, and an autonomous session's loop runs its ``[bash]``
    acceptance checks itself. Without ``bypass`` in ``permissions.allowed_modes``
    they don't start, and a specialist that would start after such a policy
    arrives doesn't run (orchestration/runner.py).
    """
    policy = current()
    if policy and not policy.mode_allowed("bypass"):
        return (f"{policy.organization}'s policy doesn't allow Full-auto, which missions and "
                "autonomous sessions run in.")
    return ""


def status() -> dict:
    """The policy in force, as ``lumi policy`` prints it and administrators check it.

    Where it came from, why it can't be used (then Lumi refuses model
    requests: ``blocked``), what it sets (``summary``), and every machine file
    Lumi ignored and why (``ignored``).
    """
    state = load()
    policy = state.policy
    return {"active": policy is not None, "organization": policy.organization if policy else "",
            "source": state.source, "error": state.error, "blocked": blocked_reason(),
            "cloud": state.cloud, "cloud_error": state.cloud_error,
            "ignored": [item.summary() for item in state.ignored],
            "summary": policy.summary() if policy else None}


def main(argv: list[str] | None = None) -> int:
    """``lumi policy``: print the organization policy in force as JSON.

    For administrators and detection scripts checking a deployment: it reads
    the policy as the app does and changes nothing but the audit log (an
    ignored file is recorded there). Exits 1 while the policy makes Lumi
    refuse model requests, else 0.
    """
    if argv:
        print("usage: lumi policy", file=sys.stderr)
        return 2
    info = status()
    print(json.dumps(info, indent=2))
    return 1 if info["blocked"] else 0


def set_for_tests(policy: Policy | None, error: str = "", *, machine: bool = False) -> None:
    """Install a policy directly (tests and fixtures only); ``machine`` as if an administrator set it."""
    global _state, _loaded
    with _lock:
        _state = PolicyState(policy=policy, error=error, source=policy.source if policy else "",
                             from_machine=machine)
        _loaded = True
    with _recorded_lock:
        _recorded.clear()
