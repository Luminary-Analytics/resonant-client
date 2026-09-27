"""Offline mode's host rules: what is this computer, and what an allowed host matches.

Standard library only: lumi/policy.py validates a policy's ``offline.*``
settings with ``validate_policy_settings``, also in the Python the MDM profile
maker runs (packaging/policy/make_mobileconfig.py), which has no httpx.
lumi/offline.py applies the rules.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable

SECTION = "offline"
MAX_ALLOWED_HOSTS = 200
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
_own_names: frozenset[str] | None = None


@dataclass(frozen=True)
class _Rule:
    kind: str  # "name", "suffix", "address", "network"
    value: Any


def normalize_host(host: Any) -> str:
    """A host as offline mode compares it: lowercase, no brackets, IDNA-encoded."""
    if isinstance(host, (bytes, bytearray)):
        text = bytes(host).decode("ascii", "replace")
    else:
        text = str(host if host is not None else "")
    text = text.strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text and not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            return text
    return text


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _this_computer() -> frozenset[str]:
    global _own_names
    if _own_names is None:
        try:
            name = socket.gethostname().strip().lower()
        except OSError:
            name = ""
        _own_names = frozenset({name} if name else set())
    return _own_names


def is_local_host(host: Any) -> bool:
    """Whether ``host`` is this computer, decided from the name as written."""
    name = normalize_host(host)
    if not name:
        return False
    if name == "localhost":
        return True
    address = _address(name)
    if address is not None:
        return address.is_loopback or address.is_unspecified
    return name in _this_computer()


def _matches(name: str, rules: Iterable[_Rule]) -> bool:
    address = _address(name)
    bare = name[:-1] if name.endswith(".") else name
    for rule in rules:
        if rule.kind == "address":
            if address is not None and address == rule.value:
                return True
        elif rule.kind == "network":
            if address is not None and address.version == rule.value.version and address in rule.value:
                return True
        elif address is None:
            if rule.kind == "name" and bare == rule.value:
                return True
            if rule.kind == "suffix" and bare.endswith("." + rule.value):
                return True
    return False


def _parse_entry(raw: str) -> str:
    """One ``allowed_hosts`` entry, normalized; ValueError with the fix."""
    text = str(raw or "").strip()
    if "://" in text:  # a pasted URL: keep its host
        text = urllib.parse.urlsplit(text).hostname or ""
    text = normalize_host(text)
    if not text:
        raise ValueError("An allowed host can't be empty.")
    if len(text) > 253:
        raise ValueError(f"{text[:40]}… is too long for a host name.")
    if text in {"*", "*.*", "."}:
        raise ValueError("Allowing every host turns offline mode off; turn it off instead.")
    if "/" in text:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError(f"{text} isn't a network such as 10.20.0.0/16.") from None
        if network.prefixlen == 0:
            raise ValueError("Allowing every address turns offline mode off; turn it off instead.")
        return str(network)
    address = _address(text)
    if address is not None:
        return str(ipaddress.ip_address(text))
    if ":" in text:
        raise ValueError(f"Leave the port out of {text}: list the host only.")
    wildcard = text.startswith("*.")
    labels = (text[2:] if wildcard else text).rstrip(".").split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError(f"{text} isn't a host name. List names such as llm.corp.example or *.corp.example, "
                         "addresses such as 10.20.0.5, or networks such as 10.20.0.0/16.")
    if wildcard and len(labels) < 2:
        raise ValueError(f"{text} would allow a whole top-level domain; name the organization's domain.")
    return ("*." if wildcard else "") + ".".join(labels)


def parse_allowed_hosts(value: Any) -> tuple[str, ...]:
    """``offline.allowed_hosts`` from Settings or a policy; ValueError with the fix.

    Takes a list or text with one entry per line (commas and spaces also
    separate them). Duplicates are dropped.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("offline.allowed_hosts must be a list of hosts.")
    entries: list[str] = []
    for line in value:
        if line.strip().startswith("#"):
            continue
        for item in re.split(r"[\s,]+", line):
            if not item:
                continue
            entry = _parse_entry(item)
            if entry not in entries:
                entries.append(entry)
    if len(entries) > MAX_ALLOWED_HOSTS:
        raise ValueError(f"List at most {MAX_ALLOWED_HOSTS} allowed hosts.")
    return tuple(entries)


def _compile(entries: Iterable[str]) -> tuple[_Rule, ...]:
    rules: list[_Rule] = []
    for entry in entries:
        if "/" in entry:
            rules.append(_Rule("network", ipaddress.ip_network(entry, strict=False)))
        elif _address(entry) is not None:
            rules.append(_Rule("address", _address(entry)))
        elif entry.startswith("*."):
            rules.append(_Rule("suffix", entry[2:]))
        else:
            rules.append(_Rule("name", entry))
    return tuple(rules)


def validate_setting(key: str, value: Any) -> Any:
    """The value to store for ``offline.<key>``; ValueError with the fix."""
    if key == "enabled":
        if not isinstance(value, bool):
            raise ValueError("offline.enabled must be true or false.")
        return value
    if key == "allowed_hosts":
        return list(parse_allowed_hosts(value))
    raise ValueError(f"offline.{key} isn't an offline setting; use enabled or allowed_hosts.")


def validate_policy_settings(settings: dict[str, Any]) -> None:
    """Refuse a policy whose ``offline.*`` values Lumi couldn't apply (lumi/policy.py)."""
    for name, value in settings.items():
        if name.startswith(SECTION + "."):
            validate_setting(name.split(".", 1)[1], value)


def reset_for_tests() -> None:
    """Forget this computer's cached name (tests)."""
    global _own_names
    _own_names = None
