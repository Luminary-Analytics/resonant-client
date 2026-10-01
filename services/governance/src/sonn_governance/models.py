"""Trusted transport-to-store values; these classes do not authenticate callers.

Only a verified identity adapter may construct Principal for a resource request.
No role, tenant membership, local owner identifier, or token-supplied permission
is accepted as authority. Every store call resolves current database grants.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID


class GovernanceError(Exception):
    """A safe, expected service failure without credentials or database details."""


class AccessDenied(GovernanceError):
    """The resource is unavailable to this authenticated identity."""


class Conflict(GovernanceError):
    """A revision or immutable command identity conflicts with retained state."""


class UnsupportedVersion(GovernanceError):
    """The requested protocol or persisted schema is not supported."""


class InvalidRequest(GovernanceError):
    """The envelope does not conform to the bounded public contract."""


PERMISSIONS = frozenset({
    "metadata_read", "content_read", "control_execute", "policy_admin",
    "membership_admin", "membership_approve", "audit_read", "host_admin", "content_write", "retention_admin",
})
PROJECT_PERMISSIONS = PERMISSIONS - {"membership_admin", "membership_approve"}
SENSITIVE_PERMISSIONS = frozenset({"content_read", "content_write", "retention_admin"})
OWNER_EFFECTS = frozenset({"writer_git", "candidate_git", "candidate_check", "checkout_apply"})


def canonical_json(value: Any) -> str:
    """Canonical finite JSON used only for immutable command semantics."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRequest("invalid JSON value") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identifier(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise InvalidRequest("identifier must be a canonical UUID") from exc
    return value


def bounded_text(value: Any, limit: int, name: str) -> str:
    try:
        valid = (isinstance(value, str) and bool(value) and "\x00" not in value
                 and len(value.encode("utf-8")) <= limit)
    except UnicodeError:
        valid = False
    if not valid:
        raise InvalidRequest(f"invalid {name}")
    return value


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    expires_at: float
    kind: str = "human"

    def __post_init__(self) -> None:
        bounded_text(self.issuer, 2048, "issuer")
        bounded_text(self.subject, 1024, "subject")
        if (self.kind != "human" or isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, (int, float))
                or not math.isfinite(self.expires_at)):
            raise InvalidRequest("unsupported principal")

    @property
    def actor_id(self) -> str:
        """Stable identity binding, never derived from email or display name."""
        return digest({"issuer": self.issuer, "subject": self.subject, "kind": self.kind})


@dataclass(frozen=True)
class HostPrincipal:
    """Constructed only from a verified TLS peer certificate, never a header."""

    certificate_sha256: str
    expires_at: float

    def __post_init__(self) -> None:
        if (not isinstance(self.certificate_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", self.certificate_sha256) is None
                or isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, (int, float))
                or not math.isfinite(self.expires_at)):
            raise InvalidRequest("unsupported host principal")

    @property
    def actor_id(self) -> str:
        return digest({"kind": "host", "certificate_sha256": self.certificate_sha256})


@dataclass(frozen=True)
class ProvisioningPrincipal:
    """Verified provisioning credential binding, independent of human tokens."""

    credential_id: str
    tenant_id: str
    expires_at: float

    def __post_init__(self) -> None:
        identifier(self.credential_id)
        identifier(self.tenant_id)
        if (isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float))
                or not math.isfinite(self.expires_at)):
            raise InvalidRequest("unsupported provisioning principal")

    @property
    def actor_id(self) -> str:
        return digest({"kind": "provisioner", "credential_id": self.credential_id, "tenant_id": self.tenant_id})


@dataclass(frozen=True)
class Query:
    tenant_id: str
    project_id: str | None
    kind: str
    after: int = 0
    limit: int = 100

    def validate(self) -> None:
        identifier(self.tenant_id)
        if self.project_id is not None:
            identifier(self.project_id)
        if self.kind not in {"metadata", "policy", "audit"}:
            raise InvalidRequest("unsupported query")
        if self.kind != "audit" and self.project_id is None:
            raise InvalidRequest("project is required")
        if (type(self.after) is not int or self.after < 0 or type(self.limit) is not int
                or not 1 <= self.limit <= 100):
            raise InvalidRequest("invalid pagination")


@dataclass(frozen=True)
class CommandEnvelope:
    protocol_version: int
    command_id: str
    tenant_id: str
    project_id: str | None
    expected_revision: int
    operation: str
    payload: Mapping[str, Any]

    def semantics(self) -> dict:
        """Snapshot mutable caller arguments before opening a transaction."""
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise UnsupportedVersion("unsupported command version")
        identifier(self.tenant_id)
        identifier(self.command_id)
        if self.project_id is not None:
            identifier(self.project_id)
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise InvalidRequest("invalid expected revision")
        if self.operation not in {"set_policy", "set_membership", "enroll_host", "revoke_host",
                                  "decide_grant", "revoke_grant", "map_scim_group"}:
            raise InvalidRequest("unsupported operation")
        if not isinstance(self.payload, Mapping):
            raise InvalidRequest("payload must be an object")
        value = {"protocol_version": self.protocol_version, "command_id": self.command_id,
                 "tenant_id": self.tenant_id, "project_id": self.project_id,
                 "expected_revision": self.expected_revision, "operation": self.operation,
                 "payload": dict(self.payload)}
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > 32768:
            raise InvalidRequest("command is too large")
        return json.loads(encoded)


def policy_document(value: Any) -> dict:
    """Policy storage contract, not a claim of deployed execution enforcement."""
    keys = {"policy_version", "allowed_models", "allowed_tools", "max_workers",
            "request_limit", "content_mode"}
    if not isinstance(value, dict):
        raise InvalidRequest("invalid policy fields")
    version = value.get("policy_version")
    if type(version) is not int or version not in {1, 2}:
        raise UnsupportedVersion("unsupported policy version")
    if version == 2:
        keys.add("allowed_effects")
    if set(value) != keys:
        raise InvalidRequest("invalid policy fields")
    if version == 2:
        effects = value["allowed_effects"]
        if (type(effects) is not list or any(type(effect) is not str or effect not in OWNER_EFFECTS for effect in effects)
                or len(effects) != len(set(effects))):
            raise InvalidRequest("invalid owner effect allowlist")
    if type(value["max_workers"]) is not int or not 1 <= value["max_workers"] <= 4:
        raise InvalidRequest("invalid worker limit")
    if type(value["request_limit"]) is not int or not 0 <= value["request_limit"] <= 1000000:
        raise InvalidRequest("invalid request limit")
    if not isinstance(value["content_mode"], str) or value["content_mode"] not in {"none", "explicit"}:
        raise InvalidRequest("invalid content mode")
    models, tools = value["allowed_models"], value["allowed_tools"]
    if not isinstance(models, list) or len(models) > 64:
        raise InvalidRequest("invalid model allowlist")
    for model in models:
        if not isinstance(model, dict) or set(model) != {"provider", "model"}:
            raise InvalidRequest("invalid model selection")
        bounded_text(model["provider"], 128, "provider")
        bounded_text(model["model"], 256, "model")
    if not isinstance(tools, list) or len(tools) > 128:
        raise InvalidRequest("invalid tool allowlist")
    for tool in tools:
        if not isinstance(tool, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool) is None:
            raise InvalidRequest("invalid tool name")
    if len({canonical_json(model) for model in models}) != len(models) or len(set(tools)) != len(tools):
        raise InvalidRequest("duplicate allowlist value")
    return value


def membership_document(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "actor_id", "active", "tenant_permissions", "project_permissions"
    }:
        raise InvalidRequest("invalid membership fields")
    if not isinstance(value["actor_id"], str) or re.fullmatch(r"[a-f0-9]{64}", value["actor_id"]) is None:
        raise InvalidRequest("invalid actor identity")
    if type(value["active"]) is not bool:
        raise InvalidRequest("invalid membership state")
    grants = value["tenant_permissions"]
    if (not isinstance(grants, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in grants)
            or len(set(grants)) != len(grants)):
        raise InvalidRequest("invalid tenant permissions")
    projects = value["project_permissions"]
    if not isinstance(projects, dict) or len(projects) > 128:
        raise InvalidRequest("invalid project permissions")
    for project, grants in projects.items():
        identifier(project)
        if (not isinstance(grants, list)
                or any(not isinstance(p, str) or p not in PROJECT_PERMISSIONS for p in grants)
                or len(set(grants)) != len(grants)):
            raise InvalidRequest("invalid project permissions")
    return value
