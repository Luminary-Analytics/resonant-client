"""Offline licenses: a signed file stating who may use Lumi offline, how many seats, until when.

A license is a ``lumi.license/v1`` document signed like a signed policy
(lumi/policy.py): an Ed25519 signature over the document's canonical JSON::

    {"license": {"schema": "lumi.license/v1", "license_id": "acme-2026-001",
                 "organization": "Acme", "seats": 50, "offline": true,
                 "issued_at": "2026-09-27T00:00:00Z", "expires_at": "2027-09-30T00:00:00Z"},
     "key_id": "luminary-2026", "signature": "<base64>"}

Luminary Analytics signs licenses with ``scripts/sign_license.py``. A license
verifies only against keys this computer's administrator controls or that are
built into Lumi, never against keys from a user-writable place:

* ``BUILTIN_KEYS`` below (Luminary's license-signing keys);
* the ``LicenseKeys`` value of the policy registry key (Windows), the
  ``LicenseKeys`` key of the configuration profile (macOS), or
  ``license-keys.json`` beside the machine policy file: ``{"<key id>": "<base64
  Ed25519 public key>"}``.

The license file itself can live anywhere, since its signature is what
counts. Lumi reads the first of: ``license.json`` beside the machine policy
file (``%ProgramData%\\Lumi``, ``/Library/Application Support/Lumi``,
``/etc/lumi``); ``LUMI_LICENSE_FILE`` when there is none; the copy
``lumi license install`` keeps in Lumi's own folder (``~/.lumi/license.json``).

First pass: a license labels offline use as licensed, and ``lumi license
status`` and Settings > Offline mode show it. Nothing needs one: offline mode
and every other feature work without it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "lumi.license/v1"
# Luminary Analytics' license-signing public keys ({key id: base64 Ed25519
# public key}). Empty until Luminary pins its production key (``python
# scripts/sign_license.py public-key --key <private key>`` prints the entry);
# until then licenses verify only against keys an administrator installs.
BUILTIN_KEYS: dict[str, str] = {}
MAX_BYTES = 64 * 1024
LICENSE_FILE = "license.json"
KEYS_FILE = "license-keys.json"


class LicenseError(ValueError):
    """A license that can't be used; the message says why."""


@dataclass(frozen=True)
class License:
    """A verified license."""

    license_id: str
    organization: str
    seats: int
    offline: bool
    issued_at: str
    expires_at: str
    key_id: str
    source: str = ""

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) > _parse_time(self.expires_at)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        return {"license_id": self.license_id, "organization": self.organization, "seats": self.seats,
                "offline": self.offline, "issued_at": self.issued_at, "expires_at": self.expires_at,
                "key_id": self.key_id, "source": self.source, "expired": self.expired(now)}


def _parse_time(text: str) -> float:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LicenseError(f"'{text}' is not an ISO 8601 time.") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def canonical(document: dict) -> bytes:
    """The bytes a license signature covers: the same canonical JSON as a signed policy."""
    from .policy import canonical as policy_canonical

    return policy_canonical(document)


def parse(data: Any, *, trusted_keys: dict[str, str], source: str = "") -> License:
    """Verify a signed license document and return it; LicenseError says why not."""
    from .policy import verify_signature

    if not isinstance(data, dict) or "signature" not in data:
        raise LicenseError("A license must be signed: {\"license\": {...}, \"key_id\": ..., \"signature\": ...}.")
    document = data.get("license")
    key_id = str(data.get("key_id") or "")
    if not isinstance(document, dict):
        raise LicenseError("A signed license needs its 'license' object.")
    if key_id not in trusted_keys:
        raise LicenseError(f"The license is signed with key '{key_id}', which this computer doesn't trust.")
    if not verify_signature(document, str(data.get("signature") or ""), trusted_keys[key_id]):
        raise LicenseError("The license's signature doesn't match its contents.")
    if document.get("schema") != SCHEMA:
        raise LicenseError(f"Unknown license schema {document.get('schema')!r}; expected {SCHEMA}.")
    organization = " ".join(str(document.get("organization") or "").split())
    if not organization:
        raise LicenseError("The license doesn't name an organization.")
    seats = document.get("seats")
    if isinstance(seats, bool) or not isinstance(seats, int) or seats < 1:
        raise LicenseError("The license's seats must be a whole number of at least 1.")
    offline = document.get("offline", False)
    if not isinstance(offline, bool):
        raise LicenseError("The license's 'offline' must be true or false.")
    expires_at = str(document.get("expires_at") or "")
    if not expires_at:
        raise LicenseError("The license doesn't say when it expires.")
    _parse_time(expires_at)
    issued_at = str(document.get("issued_at") or "")
    if issued_at:
        _parse_time(issued_at)
    return License(license_id=str(document.get("license_id") or ""), organization=organization[:200],
                   seats=seats, offline=offline, issued_at=issued_at, expires_at=expires_at,
                   key_id=key_id, source=source)


# ── Where keys and licenses come from ──────────────────────────────────────


def _machine_folder() -> Path:
    from .policy import machine_policy_file

    return machine_policy_file().parent


def _managed_key_texts() -> list[str]:
    """``LicenseKeys`` from the policy registry key (Windows) or the configuration profile (macOS)."""
    from . import policy

    texts: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, policy.REGISTRY_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "LicenseKeys")
                texts.append("\n".join(value) if isinstance(value, list) else str(value))
        except (ImportError, OSError):
            pass
    if sys.platform == "darwin":
        profile = policy.MAC_MANAGED_PREFERENCES / f"{policy.MAC_DOMAIN}.plist"
        if profile.is_file():
            import plistlib

            try:
                data = plistlib.loads(profile.read_bytes())
            except Exception:
                data = {}
            value = data.get("LicenseKeys") if isinstance(data, dict) else None
            if isinstance(value, dict):
                texts.append(json.dumps(value))
            elif isinstance(value, str):
                texts.append(value)
    return texts


def machine_keys() -> dict[str, str]:
    """License-signing keys an administrator set: ``LicenseKeys`` or license-keys.json."""
    from . import policy

    texts = _managed_key_texts()
    keys_file = _machine_folder() / KEYS_FILE
    if keys_file.is_file():
        try:
            texts.append(keys_file.read_text(encoding=policy.ADMIN_TEXT))
        except OSError:
            pass
    keys: dict[str, str] = {}
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("Ignoring license keys that aren't JSON")
            continue
        if isinstance(data, dict):
            keys.update({str(k): str(v) for k, v in data.items()})
    return keys


def trusted_keys() -> dict[str, str]:
    """Keys a license may be signed with: built in, then the administrator's."""
    return {**machine_keys(), **BUILTIN_KEYS}


def user_license_path() -> Path:
    from .paths import state_home

    return state_home() / LICENSE_FILE


def _license_file() -> tuple[Path, str] | None:
    """The license file in use and a description of where it is, or None."""
    machine = _machine_folder() / LICENSE_FILE
    if machine.is_file():
        return machine, str(machine)
    override = os.environ.get("LUMI_LICENSE_FILE", "").strip()
    if override:
        return Path(override), f"{override} (LUMI_LICENSE_FILE)"
    user = user_license_path()
    if user.is_file():
        return user, str(user)
    return None


def read_file(path: Path) -> Any:
    """A license file's JSON; LicenseError for one that can't be read."""
    try:
        if path.stat().st_size > MAX_BYTES:
            raise LicenseError("That file is too large to be a license.")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise LicenseError(f"The license couldn't be read: {exc}") from exc
    except ValueError as exc:
        raise LicenseError(f"The license isn't JSON: {exc}") from exc


@dataclass(frozen=True)
class LicenseState:
    """The license in force, or why there is none."""

    license: License | None = None
    error: str = ""
    source: str = ""


_lock = threading.Lock()
_state: LicenseState | None = None


def load(*, force: bool = False) -> LicenseState:
    """Read and verify the license once (again with ``force``). Never raises."""
    global _state
    with _lock:
        if _state is not None and not force:
            return _state
        found = None
        try:
            found = _license_file()
            if found is None:
                _state = LicenseState()
            else:
                path, source = found
                _state = LicenseState(license=parse(read_file(path), trusted_keys=trusted_keys(), source=source),
                                      source=source)
        except LicenseError as exc:
            _state = LicenseState(error=str(exc), source=found[1] if found else "")
        except Exception as exc:  # anything else is a license Lumi can't use, never a crash
            logger.exception("The license couldn't be checked")
            _state = LicenseState(error=f"The license couldn't be checked: {exc}")
        return _state


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = None


def status(now: float | None = None) -> dict[str, Any]:
    """What ``lumi license status`` and Settings show."""
    state = load()
    if state.license is None:
        return {"present": bool(state.error), "valid": False, "error": state.error, "source": state.source,
                "offline_use": "unlicensed", "keys": len(trusted_keys())}
    info = state.license.summary(now)
    covered = state.license.offline and not info["expired"]
    return {"present": True, "valid": True, "error": "", **info,
            "offline_use": "licensed" if covered else "unlicensed", "keys": len(trusted_keys())}


def install(path: Path) -> License:
    """Verify a license file and keep a copy in Lumi's folder, for ``lumi license status``."""
    granted = parse(read_file(Path(path)), trusted_keys=trusted_keys(), source=str(path))
    target = user_license_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    shutil.copyfile(path, temporary)
    temporary.replace(target)
    load(force=True)
    return granted


def describe(info: dict[str, Any]) -> str:
    """One paragraph for people: what the license says, or why there is none."""
    if not info.get("valid"):
        if info.get("error"):
            return f"The license at {info.get('source') or 'its file'} can't be used: {info['error']}"
        return ("No license is installed. Offline mode and everything else work without one; a license "
                "records your organization's offline entitlement.")
    seats = f"{info['seats']} seat{'s' if info['seats'] != 1 else ''}"
    when = f"expired {info['expires_at']}" if info.get("expired") else f"valid until {info['expires_at']}"
    use = "Offline use is licensed." if info.get("offline_use") == "licensed" else (
        "Offline use isn't covered: the license has expired." if info.get("expired") and info.get("offline")
        else "Offline use isn't covered by this license.")
    return f"Licensed to {info['organization']}, {seats}, {when}. {use}"


def main(argv: list[str] | None = None) -> int:
    """``lumi license status [--json]``, ``lumi license verify <file>``, ``lumi license install <file>``."""
    parser = argparse.ArgumentParser(prog="lumi license", description="Offline licenses: show, check or install one.")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status", help="Show the license in force")
    show.add_argument("--json", action="store_true", help="Print JSON, for scripts")
    check = commands.add_parser("verify", help="Check a license file without installing it")
    check.add_argument("file", type=Path)
    put = commands.add_parser("install", help="Check a license file and keep a copy for Lumi")
    put.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            info = status()
            print(json.dumps(info, indent=2) if args.json else describe(info))
            return 0 if info["valid"] or not info["present"] else 1
        if args.command == "verify":
            granted = parse(read_file(args.file), trusted_keys=trusted_keys(), source=str(args.file))
            print(describe({"valid": True, **granted.summary(),
                            "offline_use": "licensed" if granted.offline and not granted.expired() else "unlicensed"}))
            return 0
        install(args.file)
        print(f"Installed. {describe(status())}")
        return 0
    except LicenseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
