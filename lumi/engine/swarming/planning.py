"""Validate untrusted coordinator proposals without executing or accepting work.

The caller supplies effective policy, the explicit model and trusted criterion
names. Model output cannot provide authority, tools, executors or provider
selection. Lexical scope admission is not a filesystem or operating-system
sandbox; the supervisor and execution adapter recheck authority before effects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any

from ..execution_guard import FILE_TOOL_NAMES, SWARM_TOOL_NAMES, WRITE_TOOL_NAMES
from .policy import AssignmentGrant, ModelSelection, PolicyProfile, SwarmPolicy, normalize_scopes

_MAX_BYTES = 65536
_TOP_FIELDS = {"summary", "use_team", "work_items"}
# The captured planning data's own field names (coordinator.py). A live model
# copied coordinator_read_roots into its plan; such echoes carry no meaning
# and are dropped. Any other extra field still refuses the plan.
_INPUT_ECHOES = {"objective", "read_roots", "write_roots", "worker_slots", "coordinator_read_roots",
                 "proposed_work_namespace", "allowed_criteria", "existing_work", "recent_untrusted_findings",
                 "graph_sha256", "untrusted_messages_to_orchestrator"}
_ITEM_FIELDS = {"id", "objective", "role", "dependencies", "read_roots", "write_roots", "criteria"}
_LOGICAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_FENCE = re.compile(r"```(?:json)?\r?\n(.*)\r?\n```\Z", re.DOTALL)
# One fenced JSON block inside prose ("I found both defects... ```json {...} ```").
_EMBEDDED_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)
_FILE_READERS = FILE_TOOL_NAMES - {"artifact_read"}
# One closing tag or special token at the end of a reply: chat-template residue
# a live model appended after its JSON (a "}" followed by "</function>" and "</tool_call>").
_TEMPLATE_TAIL = re.compile(r"(?:</[A-Za-z_][\w:.-]{0,40}>|<\|[^|<>\s]{1,40}\|>)\Z")


class PlanRejected(ValueError):
    """A model proposal is malformed, ambiguous or outside the plan contract."""


@dataclass(frozen=True, slots=True)
class PlannedWorkItem:
    """Immutable validated contract; no assignment or acceptance is implied."""

    id: str
    logical_id: str
    objective: str
    role: str
    dependencies: tuple[str, ...]
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    tools: tuple[str, ...]
    criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("id", "logical_id", "objective", "role"):
            if type(getattr(self, field)) is not str:
                raise TypeError("Work item text must be immutable strings")
        for field in ("dependencies", "read_roots", "write_roots", "tools", "criteria"):
            value = getattr(self, field)
            if type(value) is not tuple or any(type(item) is not str for item in value):
                raise TypeError("Work item collections must be tuples of strings")

    def to_dict(self) -> dict[str, Any]:
        """Return fresh canonical supervisor fields, omitting the logical alias."""
        return {"id": self.id, "objective": self.objective, "role": self.role,
                "dependencies": list(self.dependencies), "read_roots": list(self.read_roots),
                "write_roots": list(self.write_roots), "tools": list(self.tools), "criteria": list(self.criteria)}


@dataclass(frozen=True, slots=True)
class CoordinatorPlan:
    """A validated proposal for trusted review, never an execution receipt."""

    summary: str
    use_team: bool
    work_items: tuple[PlannedWorkItem, ...]

    def __post_init__(self) -> None:
        if type(self.summary) is not str or type(self.use_team) is not bool:
            raise TypeError("Plan summary and team choice require explicit types")
        if type(self.work_items) is not tuple or any(type(item) is not PlannedWorkItem for item in self.work_items):
            raise TypeError("Plan work items must be an immutable tuple")

    def to_work_items(self) -> list[dict[str, Any]]:
        """Copy contracts into the supervisor's plan-command payload shape."""
        return [item.to_dict() for item in self.work_items]

    def to_dict(self) -> dict[str, Any]:
        """Serialize canonical validated output; this is not raw model input."""
        return {"summary": self.summary, "use_team": self.use_team, "work_items": self.to_work_items()}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanRejected("JSON object fields must be unique")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise PlanRejected("JSON numbers must be finite")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise PlanRejected("JSON numbers must be finite")
    return number


def _text(value: Any, *, limit: int = 8192) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise PlanRejected("Plan descriptions require nonempty text")
    try:
        if len(value.encode("utf-8")) > limit:
            raise PlanRejected("Plan description exceeds its byte limit")
    except UnicodeError as exc:
        raise PlanRejected("Plan descriptions require valid UTF-8 text") from exc
    return value


def _names(value: Any, *, logical: bool = False) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise PlanRejected("Plan collections must be arrays of strings")
    if len(value) != len(set(value)):
        raise PlanRejected("Plan collections must not contain duplicates")
    for item in value:
        if (not item.strip() or len(item) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in item)
                or (logical and not _LOGICAL_ID.fullmatch(item))):
            raise PlanRejected("Invalid plan identifier or path")
        try:
            item.encode("utf-8")
        except UnicodeError as exc:
            raise PlanRejected("Plan collections require valid UTF-8 text") from exc
    return tuple(sorted(value))


def _scopes(value: Any) -> tuple[str, ...]:
    raw = _names(value)
    try:
        normalized = normalize_scopes(raw)
    except ValueError as exc:
        raise PlanRejected("Plan paths must be unambiguous workspace-relative scopes") from exc
    # Do not silently drop aliases or covered descendants. The coordinator can
    # choose the intended roots explicitly rather than obscure a broad parent.
    if len(normalized) != len(raw):
        raise PlanRejected("Plan scopes must not contain aliases or redundant overlapping roots")
    return normalized


def _scoped_id(run_id: str, logical_id: str, namespace: str | None = None) -> str:
    # Preserve historical initial-plan identities. Explicit fresh attempts use
    # a separate namespace so common model labels cannot replace prior work.
    identity = [run_id, logical_id] if namespace is None else [run_id, namespace, logical_id]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "work_" + hashlib.sha256(encoded).hexdigest()


def parse_plan(
    text: str, *, run_id: str, policy: PolicyProfile, model: ModelSelection,
    allowed_criteria: frozenset[str], namespace: str | None = None, allow_no_work: bool = False,
) -> CoordinatorPlan:
    """Parse one strict JSON proposal and admit its complete requested scopes.

    A single optional JSON fence is accepted. No scope is clipped to fit policy;
    broad requests are denied. Role tools are derived from policy, never from
    model fields. Implement items require actual write scope/capability; explore
    and verify items are read-only. Worker count is scheduling policy, not a cap
    on the number of sequential graph nodes. Logical IDs contain 1–80 ASCII
    letters/digits/dots/underscores/hyphens and start with a letter or digit.
    No parallel speedup is inferred.
    """
    if type(policy) is not PolicyProfile or type(model) is not ModelSelection:
        raise TypeError("Planning requires explicit trusted policy and model objects")
    if (type(run_id) is not str or not run_id.strip() or len(run_id) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in run_id)):
        raise ValueError("Planning requires a captured run identity")
    if namespace is not None:
        _text(namespace, limit=256)
    if type(allowed_criteria) is not frozenset:
        raise TypeError("Trusted criteria must be an immutable set")
    _names(list(allowed_criteria))
    if type(text) is not str:
        raise PlanRejected("A coordinator proposal must be UTF-8 text")
    try:
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise PlanRejected("A coordinator proposal must not exceed 64 KiB")
        run_id.encode("utf-8")
    except UnicodeError as exc:
        raise PlanRejected("A coordinator proposal must be valid UTF-8 text") from exc
    source = text.strip()
    for _ in range(8):  # Residue only: any other text after the JSON still refuses the plan.
        tail = _TEMPLATE_TAIL.search(source)
        if tail is None:
            break
        source = source[:tail.start()].rstrip()
    if source.startswith("```"):
        fence = _FENCE.fullmatch(source)
        if fence is None:
            raise PlanRejected("Only one complete JSON fence is permitted")
        source = fence.group(1)
    elif not source.startswith("{"):
        # Prose around exactly one fenced block is still unambiguous; two
        # blocks, or none, leave the whole text to be the JSON object.
        fences = _EMBEDDED_FENCE.findall(source)
        if len(fences) == 1:
            source = fences[0]
    try:
        proposal = json.loads(source, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        # Do not echo a model response; rejected extras may contain credentials.
        raise PlanRejected("Coordinator output must be one strict JSON object") from exc
    if type(proposal) is not dict or not _TOP_FIELDS <= set(proposal) <= _TOP_FIELDS | _INPUT_ECHOES:
        raise PlanRejected("Coordinator proposal has missing or unsupported fields")
    summary = _text(proposal["summary"])
    use_team = proposal["use_team"]
    raw_items = proposal["work_items"]
    if type(use_team) is not bool:
        raise PlanRejected("The team choice must be an explicit boolean")
    # A follow-up (namespace) may propose no more work: the objective is met,
    # and its summary is the final report. So may an orchestrator that owns
    # its team (allow_no_work) and could answer without workers.
    if type(raw_items) is not list or len(raw_items) > 256 or (not raw_items and namespace is None and not allow_no_work):
        raise PlanRejected("A coordinator plan must contain 1 to 256 work items")
    if raw_items and not use_team and len(raw_items) != 1:
        raise PlanRejected("A serial recommendation requires exactly one work item")
    specifications: dict[str, tuple[dict[str, Any], AssignmentGrant]] = {}
    for item in raw_items:
        if type(item) is not dict or set(item) != _ITEM_FIELDS:
            raise PlanRejected("Work item has missing or unsupported fields")
        logical_id = item["id"]
        _names([logical_id], logical=True)
        if logical_id in specifications:
            raise PlanRejected("Work item logical IDs must be unique")
        objective = _text(item["objective"])
        role = item["role"]
        if type(role) is not str or role not in {"explore", "implement", "verify"}:
            raise PlanRejected("Unsupported work item role")
        dependencies = _names(item["dependencies"], logical=True)
        reads, writes = _scopes(item["read_roots"]), _scopes(item["write_roots"])
        criteria = _names(item["criteria"])
        if not criteria or not set(criteria) <= allowed_criteria:
            raise PlanRejected("Every work item requires trusted allowed acceptance criteria")
        if role != "implement" and writes:
            raise PlanRejected("Only implement work items may request write scope")
        if role == "implement" and (not writes or "owner_review" in criteria):
            raise PlanRejected("Writer work requires write scope and independently executed criteria")
        candidates = FILE_TOOL_NAMES | SWARM_TOOL_NAMES
        if role == "implement":
            candidates = (candidates | WRITE_TOOL_NAMES) - {"swarm_submit"}
        tools = candidates & policy.allowed_tools
        if reads and not tools & _FILE_READERS:
            raise PlanRejected("Readable scopes require a policy-allowed file reader")
        if writes and not tools & WRITE_TOOL_NAMES:
            raise PlanRejected("Write scopes require a policy-allowed file writer")
        grant = SwarmPolicy.admit(policy, model=model, tools=tuple(sorted(tools)), read_roots=reads, write_roots=writes)
        specifications[logical_id] = ({"objective": objective, "role": role,
                                       "dependencies": dependencies, "criteria": criteria}, grant)
    visited: set[str] = set()
    visiting: set[str] = set()
    def visit(logical_id: str) -> None:
        if logical_id in visiting:
            raise PlanRejected("Work item dependencies contain a cycle")
        if logical_id in visited:
            return
        if logical_id not in specifications:
            raise PlanRejected("Work item dependency is absent from this proposal")
        visiting.add(logical_id)
        for dependency in specifications[logical_id][0]["dependencies"]:
            visit(dependency)
        visiting.remove(logical_id)
        visited.add(logical_id)
    for logical_id in specifications:
        visit(logical_id)
    items = []
    for logical_id, (spec, grant) in sorted(specifications.items()):
        items.append(PlannedWorkItem(id=_scoped_id(run_id, logical_id, namespace), logical_id=logical_id,
            objective=spec["objective"], role=spec["role"],
            dependencies=tuple(sorted(_scoped_id(run_id, dependency, namespace) for dependency in spec["dependencies"])),
            read_roots=grant.read_roots, write_roots=grant.write_roots,
            tools=tuple(sorted(grant.tools)), criteria=spec["criteria"]))
    return CoordinatorPlan(summary=summary, use_team=use_team, work_items=tuple(items))
