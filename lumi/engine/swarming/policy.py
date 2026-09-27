"""Pure, deny-by-intersection policy for explicitly selected native workers.

Scopes are case-preserving lexical paths relative to a captured workspace. This
module does not inspect a filesystem and is not an OS sandbox. Before effects,
the execution adapter must enforce real paths, symlinks, filesystem aliases,
credentials, and current authority. Deserializing a grant validates its shape;
it does not authenticate a caller or authorize an effect.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .models import SwarmError


NATIVE_PROVIDERS = frozenset({"ollama", "exo", "kimi", "openrouter", "sonn"})
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9¹²³]+|lpt[0-9¹²³]+)(?:\..*)?", re.I)
_DIGEST = re.compile(r"[0-9a-f]{64}")


class PolicyDenied(SwarmError):
    """A requested model, tool, or lexical scope exceeds effective policy."""


def _positive_version(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("Policy versions must be positive integers")


def _identifier(value: str) -> None:
    if (type(value) is not str or not value or len(value) > 256
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Identifiers must be nonempty strings without whitespace or controls")


def _fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        # Do not echo unknown keys/values: malformed serialized input may contain credentials.
        raise ValueError("Serialized policy has missing or unknown fields")
    return value


def _string_array(value: Any) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError("Serialized sets and scopes must be arrays of strings")
    if len(value) != len(set(value)):
        raise ValueError("Serialized sets and scopes must not contain duplicates")
    return tuple(value)


def _names(value: frozenset[str]) -> None:
    if type(value) is not frozenset:
        raise ValueError("Policy names must be immutable sets")
    for item in value:
        _identifier(item)


def normalize_scope(path: str) -> str:
    """Normalize a workspace-relative scope without resolving filesystem paths.

    Forward and backward separators are accepted; absolute paths, traversal,
    glob syntax, alternate streams, and ambiguous Windows names are rejected on
    every platform. ``.`` denotes the entire workspace, not its parent.
    """
    if type(path) is not str or not path or len(path) > 4096:
        raise ValueError("A scope must be a nonempty relative path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Scope paths must not contain control characters")
    path = path.replace("\\", "/")
    if path.startswith("/") or any(char in path for char in ':*?[]{}<>|"'):
        raise ValueError("Scope paths must be relative and cannot contain drives, streams, or globs")
    parts = []
    for part in path.split("/"):
        if part == "..":
            raise ValueError("Scope paths must not contain parent traversal")
        if part in ("", "."):
            continue
        if part.endswith((".", " ")) or _DEVICE.fullmatch(part):
            raise ValueError("Scope paths must not use ambiguous Windows names")
        parts.append(part)
    return "/".join(parts) or "."


def _contains(parent: str, child: str) -> bool:
    return parent == "." or child == parent or child.startswith(parent + "/")


def normalize_scopes(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Canonicalize roots, removing duplicates and roots covered by a parent."""
    if type(roots) is not tuple:
        raise ValueError("Scope roots must be an immutable tuple")
    normalized = sorted({normalize_scope(root) for root in roots})
    return tuple(root for root in normalized if not any(
        root != other and _contains(other, root) for other in normalized))


def _covered(requested: tuple[str, ...], allowed: tuple[str, ...]) -> bool:
    return all(any(_contains(parent, child) for parent in allowed) for child in requested)


def _intersect_roots(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    overlapping = []
    for left in first:
        for right in second:
            if _contains(left, right):
                overlapping.append(right)
            elif _contains(right, left):
                overlapping.append(left)
    return normalize_scopes(tuple(overlapping))


def scopes_overlap(
    first: tuple[str, ...], second: tuple[str, ...], *, case_sensitive: bool = True,
) -> bool:
    """Report segment-aware lexical overlap, optionally folding filesystem case.

    For Windows writer conflict checks use ``case_sensitive=False``. Containment
    grants preserve exact case, so a spelling mismatch can deny but cannot expand
    a grant. This helper cannot detect symlink, short-name, or mounted aliases.
    """
    if type(case_sensitive) is not bool:
        raise ValueError("Case sensitivity must be a boolean")
    left = normalize_scopes(first)
    right = normalize_scopes(second)
    if not case_sensitive:
        left = tuple(root.casefold() for root in left)
        right = tuple(root.casefold() for root in right)
    return any(_contains(a, b) or _contains(b, a) for a in left for b in right)


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """Explicit provider/model choice; no discovery, defaults, or credentials."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        _identifier(self.provider)
        _identifier(self.model)
        if self.provider not in NATIVE_PROVIDERS:
            raise PolicyDenied("Only native provider protocols are eligible; CLI workers are unavailable")

    def to_dict(self) -> dict[str, Any]:
        """Return only the explicit non-secret model identity."""
        return {"provider": self.provider, "model": self.model}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ModelSelection:
        """Parse a strict model identity; reject credential-bearing extra fields."""
        fields = _fields(value, {"provider", "model"})
        return cls(provider=fields["provider"], model=fields["model"])


@dataclass(frozen=True, slots=True)
class PolicyProfile:
    """Versioned restrictions; an empty set or scope grants no corresponding access."""

    version: int
    allowed_tools: frozenset[str]
    allowed_providers: frozenset[str]
    max_workers: int = 2
    read_roots: tuple[str, ...] = (".",)
    write_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _positive_version(self.version)
        _names(self.allowed_tools)
        _names(self.allowed_providers)
        if not self.allowed_providers <= NATIVE_PROVIDERS:
            raise ValueError("Policy profiles support native provider protocols only")
        if type(self.max_workers) is not int or not 1 <= self.max_workers <= 4:
            raise ValueError("Visible worker slots must be between 1 and 4")
        object.__setattr__(self, "read_roots", normalize_scopes(self.read_roots))
        object.__setattr__(self, "write_roots", normalize_scopes(self.write_roots))

    def to_dict(self) -> dict[str, Any]:
        """Return explicit JSON fields with deterministic ordering for sets."""
        return {"version": self.version, "allowed_tools": sorted(self.allowed_tools),
                "allowed_providers": sorted(self.allowed_providers), "max_workers": self.max_workers,
                "read_roots": list(self.read_roots), "write_roots": list(self.write_roots)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PolicyProfile:
        """Parse complete policy data without silently defaulting missing restrictions."""
        fields = _fields(value, {"version", "allowed_tools", "allowed_providers", "max_workers",
                                 "read_roots", "write_roots"})
        return cls(version=fields["version"], allowed_tools=frozenset(_string_array(fields["allowed_tools"])),
                   allowed_providers=frozenset(_string_array(fields["allowed_providers"])),
                   max_workers=fields["max_workers"], read_roots=_string_array(fields["read_roots"]),
                   write_roots=_string_array(fields["write_roots"]))

    @property
    def digest(self) -> str:
        """Identify normalized effective policy contents, not authenticated authority."""
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AssignmentGrant:
    """Immutable narrowed assignment authority bound to one effective policy."""

    policy_version: int
    model: ModelSelection
    tools: frozenset[str]
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    policy_digest: str

    def __post_init__(self) -> None:
        _positive_version(self.policy_version)
        if type(self.model) is not ModelSelection:
            raise ValueError("A grant requires an explicit model selection")
        _names(self.tools)
        object.__setattr__(self, "read_roots", normalize_scopes(self.read_roots))
        object.__setattr__(self, "write_roots", normalize_scopes(self.write_roots))
        if type(self.policy_digest) is not str or not _DIGEST.fullmatch(self.policy_digest):
            raise ValueError("A grant requires a SHA-256 policy content digest")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only narrowed permissions and non-secret model identity."""
        return {"policy_version": self.policy_version, "model": self.model.to_dict(),
                "tools": sorted(self.tools), "read_roots": list(self.read_roots),
                "write_roots": list(self.write_roots), "policy_digest": self.policy_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AssignmentGrant:
        """Validate persisted grant shape; this method does not authorize its use."""
        fields = _fields(value, {"policy_version", "model", "tools", "read_roots",
                                 "write_roots", "policy_digest"})
        return cls(policy_version=fields["policy_version"], model=ModelSelection.from_dict(fields["model"]),
                   tools=frozenset(_string_array(fields["tools"])),
                   read_roots=_string_array(fields["read_roots"]),
                   write_roots=_string_array(fields["write_roots"]), policy_digest=fields["policy_digest"])


class SwarmPolicy:
    """Combine trusted restrictions and admit only explicitly narrowed requests."""

    @staticmethod
    def intersect(*profiles: PolicyProfile) -> PolicyProfile:
        """Intersect all dimensions; the maximum version is a label, not precedence.

        Every parent restricts the result regardless of version. The digest binds
        the complete resulting restrictions; version alone is not policy identity.
        """
        if not profiles or any(type(profile) is not PolicyProfile for profile in profiles):
            raise ValueError("At least one explicit policy profile is required")
        first = profiles[0]
        tools, providers = first.allowed_tools, first.allowed_providers
        read_roots, write_roots = first.read_roots, first.write_roots
        for profile in profiles[1:]:
            tools &= profile.allowed_tools
            providers &= profile.allowed_providers
            read_roots = _intersect_roots(read_roots, profile.read_roots)
            write_roots = _intersect_roots(write_roots, profile.write_roots)
        return PolicyProfile(version=max(profile.version for profile in profiles), allowed_tools=tools,
                             allowed_providers=providers, max_workers=min(p.max_workers for p in profiles),
                             read_roots=read_roots, write_roots=write_roots)

    @staticmethod
    def admit(
        profile: PolicyProfile, *, model: ModelSelection, tools: tuple[str, ...],
        read_roots: tuple[str, ...], write_roots: tuple[str, ...],
    ) -> AssignmentGrant:
        """Deny any broadened field; never silently change the selected model."""
        if type(profile) is not PolicyProfile or type(model) is not ModelSelection:
            raise ValueError("Admission requires explicit policy and model objects")
        if type(tools) is not tuple:
            raise ValueError("Requested tools must be an immutable tuple")
        for tool in tools:
            _identifier(tool)
        requested_tools = frozenset(tools)
        reads, writes = normalize_scopes(read_roots), normalize_scopes(write_roots)
        if model.provider not in profile.allowed_providers:
            raise PolicyDenied("The selected provider is not allowed by effective policy")
        if not requested_tools <= profile.allowed_tools:
            raise PolicyDenied("Requested tools exceed effective policy")
        if not _covered(reads, profile.read_roots) or not _covered(writes, profile.write_roots):
            raise PolicyDenied("Requested file scopes exceed effective policy")
        return AssignmentGrant(policy_version=profile.version, model=model, tools=requested_tools,
                               read_roots=reads, write_roots=writes, policy_digest=profile.digest)
