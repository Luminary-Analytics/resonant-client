"""Versioned storage contracts for trusted runtime callers.

Scope and authority objects must be constructed from authenticated runtime
context, never by unpacking model arguments or browser messages. They express
captured ownership; they are not authentication tokens or worker capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1


class SwarmError(Exception):
    """Base class for rejected durable swarm operations."""


class ScopeDenied(SwarmError):
    """The record is absent or outside the caller's captured scope."""


class StaleAuthority(SwarmError):
    """The captured supervisor or execution epoch is no longer current."""


class Conflict(SwarmError):
    """The requested transition conflicts with durable state."""


class AdmissionClosed(Conflict):
    """The run is not admitting new work or effects."""


class WorkerPaused(AdmissionClosed):
    """The captured participant is paused; admission may retry after resume."""


class AllowanceExceeded(Conflict):
    """There is insufficient unallocated request allowance."""


class IdempotencyConflict(Conflict):
    """A command key was reused for different arguments."""


class SchemaVersionError(SwarmError):
    """This client cannot safely interpret the database schema."""


class LeaseExpired(StaleAuthority):
    """The supervisor's persisted execution lease has expired."""


class RevisionConflict(Conflict):
    """A command was prepared against an obsolete run revision."""


@dataclass(frozen=True, slots=True)
class Command:
    """Versioned command data; trusted authority travels separately."""

    command_id: str
    run_id: str
    expected_revision: int
    epoch: int
    kind: str
    payload: dict[str, Any]
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """A committed transition and replay cursor, not execution evidence."""

    revision: int
    event_cursor: int
    state: str
    result: dict[str, Any]


def require_id(value: str) -> None:
    """Validate an opaque identifier without rewriting its identity."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Identifiers must contain 1 to 256 characters")


@dataclass(frozen=True, slots=True)
class Scope:
    """Explicit tenant and captured session ownership from trusted context."""

    tenant_id: str
    owner_id: str
    project_id: str
    session_id: str

    def __post_init__(self) -> None:
        for value in self.values():
            require_id(value)

    @classmethod
    def personal(cls, owner_id: str, project_id: str, session_id: str) -> Scope:
        """Create a non-null personal tenant; this grants no extra authority."""
        return cls(f"personal:{owner_id}", owner_id, project_id, session_id)

    def values(self) -> tuple[str, str, str, str]:
        """Return fields in the persistence boundary's canonical order."""
        return self.tenant_id, self.owner_id, self.project_id, self.session_id


@dataclass(frozen=True, slots=True)
class RunAuthority:
    """Captured local supervisor identity, checked on every mutation."""

    scope: Scope
    run_id: str
    supervisor_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """Runtime-bound worker identity; no model-supplied owner or role field."""

    scope: Scope
    run_id: str
    attempt_id: str
    worker_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class Claim:
    """Committed assignment and dispatch intent, not proof of execution."""

    context: AttemptContext
    work_item_id: str
    reservation_id: str
    dispatch_id: str
    reserved_requests: int


@dataclass(frozen=True, slots=True)
class Event:
    """One durable event in a run's monotonic sequence."""

    sequence: int
    kind: str
    epoch: int
    payload: dict[str, Any]
    occurred_at: float | None = None
    run_state: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """Untrusted task data with runtime-attributed sender and recipient."""

    id: str
    sequence: int
    sender_attempt_id: str
    recipient_attempt_id: str
    epoch: int
    kind: str
    body: str
    reply_to: str | None


@dataclass(frozen=True, slots=True)
class Receipt:
    """Durable transport/input inclusion evidence, never model comprehension."""

    message_id: str
    recipient_attempt_id: str
    epoch: int
    stage: str
    model_request_id: str | None
    input_revision: int | None
