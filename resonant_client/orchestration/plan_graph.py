"""
PlanGraph — mutable DAG of nodes representing how an intent is being decomposed.

Replaces the linear sprint/phase model. Nodes can be added, pruned, or rewritten
as the orchestrator learns what the work actually requires. Each node has a
specialization (explore / implement / verify / ...) that drives which kind of
specialist agent runs it.

Snapshots support rollback: every mutation can checkpoint the graph so a user
can restore a past state if a dropped branch turns out to be needed.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional


# ── Enums (string-valued so JSON round-trips trivially) ──────────────────


class NodeStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"

    ALL = frozenset({PENDING, RUNNING, DONE, BLOCKED, ABANDONED})
    TERMINAL = frozenset({DONE, ABANDONED})


class NodeSpecialization:
    EXPLORE = "explore"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REPAIR = "repair"
    RESEARCH = "research"
    PLAN = "plan"

    ALL = frozenset({EXPLORE, IMPLEMENT, VERIFY, REPAIR, RESEARCH, PLAN})


# ── Plan node ────────────────────────────────────────────────────────────


@dataclass
class PlanNode:
    """One unit of work in a plan-graph.

    Identity is `id` (a ULID-ish hex string). Tree structure is via `parent_id`
    (one parent). DAG structure is via `depends_on` (multiple predecessors that
    must reach a terminal status before this node can run).
    """
    id: str
    intent_id: str
    goal: str
    specialization: str = NodeSpecialization.IMPLEMENT
    status: str = NodeStatus.PENDING
    confidence: float = 1.0
    parent_id: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    skill_id: Optional[str] = None
    agent_session_id: Optional[str] = None
    result: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    audit_log: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.specialization not in NodeSpecialization.ALL:
            raise ValueError(
                f"Unknown specialization {self.specialization!r}; "
                f"expected one of {sorted(NodeSpecialization.ALL)}"
            )
        if self.status not in NodeStatus.ALL:
            raise ValueError(
                f"Unknown status {self.status!r}; "
                f"expected one of {sorted(NodeStatus.ALL)}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be in [0, 1]; got {self.confidence!r}")

    def is_terminal(self) -> bool:
        return self.status in NodeStatus.TERMINAL

    def append_audit(self, kind: str, **payload: Any) -> None:
        """Append a one-line decision to this node's audit log."""
        self.audit_log.append({"ts": time.time(), "kind": kind, **payload})
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanNode":
        # Filter unknown keys defensively so older snapshots still load.
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ── Plan graph ───────────────────────────────────────────────────────────


@dataclass
class PlanGraph:
    """Mutable DAG of plan nodes.

    Identity is `intent_id`. The graph holds all nodes for one intent. Multiple
    parallel intents would be separate PlanGraph instances (potentially forked
    from one another via snapshots).
    """
    intent_id: str
    intent: str = ""
    nodes: dict[str, PlanNode] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    drop_log: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, intent: str) -> "PlanGraph":
        """Construct a fresh graph for a new intent."""
        return cls(intent_id=_new_id(), intent=intent)

    # ── Mutations ──────────────────────────────────────────────────────

    def add_node(self, node: PlanNode) -> None:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id!r} already exists")
        if node.intent_id != self.intent_id:
            raise ValueError(
                f"Node intent_id {node.intent_id!r} does not match graph "
                f"intent_id {self.intent_id!r}"
            )
        if node.parent_id and node.parent_id not in self.nodes:
            raise ValueError(f"Parent node {node.parent_id!r} not in graph")
        for dep in node.depends_on:
            if dep not in self.nodes:
                raise ValueError(f"Dependency {dep!r} not in graph")
        self.nodes[node.id] = node
        self._touch()

    def prune_node(self, node_id: str, *, reason: str = "") -> int:
        """Remove a node and the tree rooted at it (children via parent_id).

        Only cascades through parent_id (tree-shaped). DAG-cross-edges via
        depends_on do NOT cascade — dependents survive the prune; their now-
        dangling depends_on entries are cleaned out so they don't wait forever.
        Returns the number of nodes removed. Reasons are written to drop_log.
        """
        if node_id not in self.nodes:
            return 0
        victims = self._tree_descendants(node_id) | {node_id}
        for vid in victims:
            v = self.nodes.pop(vid, None)
            if v:
                self.drop_log.append({
                    "ts": time.time(),
                    "node_id": vid,
                    "goal": v.goal,
                    "specialization": v.specialization,
                    "reason": reason,
                })
        # Clean dangling depends_on references on surviving nodes.
        for n in self.nodes.values():
            n.depends_on = [d for d in n.depends_on if d in self.nodes]
        self._touch()
        return len(victims)

    def rewrite_subtree(
        self,
        anchor_node_id: str,
        replacement_nodes: list[PlanNode],
        *,
        reason: str = "",
    ) -> None:
        """Replace the subtree rooted at `anchor_node_id` with new nodes.

        `replacement_nodes` should already have correct `parent_id` and
        `depends_on` references; their parents must be either external (not
        being pruned) or other replacement nodes. The anchor node itself is
        also dropped.
        """
        if anchor_node_id not in self.nodes:
            raise ValueError(f"Anchor {anchor_node_id!r} not in graph")
        # Validate replacements before mutating
        replacement_ids = {n.id for n in replacement_nodes}
        for n in replacement_nodes:
            if n.id in self.nodes and n.id != anchor_node_id:
                raise ValueError(f"Replacement id {n.id!r} collides with existing node")
            if n.parent_id and n.parent_id not in self.nodes and n.parent_id not in replacement_ids:
                raise ValueError(
                    f"Replacement {n.id!r} parent {n.parent_id!r} not found in graph "
                    f"or replacements"
                )
        # Drop the old subtree
        self.prune_node(anchor_node_id, reason=reason)
        # Add the new nodes (order so parents land before children)
        for n in _topological(replacement_nodes):
            self.nodes[n.id] = n
        self._touch()

    def mark_running(self, node_id: str, *, agent_session_id: str = "") -> None:
        n = self._require(node_id)
        n.status = NodeStatus.RUNNING
        if agent_session_id:
            n.agent_session_id = agent_session_id
        n.append_audit("status_change", to=NodeStatus.RUNNING)
        self._touch()

    def mark_done(self, node_id: str, *, result: Optional[dict] = None,
                  confidence: Optional[float] = None) -> None:
        n = self._require(node_id)
        n.status = NodeStatus.DONE
        if result is not None:
            n.result = result
        if confidence is not None:
            n.confidence = max(0.0, min(1.0, confidence))
        n.append_audit("status_change", to=NodeStatus.DONE, confidence=n.confidence)
        self._touch()

    def mark_blocked(self, node_id: str, *, reason: str = "") -> None:
        n = self._require(node_id)
        n.status = NodeStatus.BLOCKED
        n.append_audit("status_change", to=NodeStatus.BLOCKED, reason=reason)
        self._touch()

    def mark_abandoned(self, node_id: str, *, reason: str = "") -> None:
        n = self._require(node_id)
        n.status = NodeStatus.ABANDONED
        n.append_audit("status_change", to=NodeStatus.ABANDONED, reason=reason)
        self._touch()

    def update_confidence(self, node_id: str, confidence: float) -> None:
        n = self._require(node_id)
        n.confidence = max(0.0, min(1.0, confidence))
        n.append_audit("confidence", value=n.confidence)
        self._touch()

    # ── Queries ────────────────────────────────────────────────────────

    def next_runnable(self) -> list[PlanNode]:
        """All pending nodes whose dependencies have reached a terminal status.

        Order is stable: by `created_at` then `id`.
        """
        ready: list[PlanNode] = []
        for n in self.nodes.values():
            if n.status != NodeStatus.PENDING:
                continue
            if all(self.nodes[dep].is_terminal() for dep in n.depends_on if dep in self.nodes):
                ready.append(n)
        ready.sort(key=lambda x: (x.created_at, x.id))
        return ready

    def is_complete(self) -> bool:
        """True when every node has reached a terminal status."""
        return all(n.is_terminal() for n in self.nodes.values())

    def root_nodes(self) -> list[PlanNode]:
        return [n for n in self.nodes.values() if n.parent_id is None]

    def children_of(self, node_id: str) -> list[PlanNode]:
        return [n for n in self.nodes.values() if n.parent_id == node_id]

    def critical_path_running(self) -> list[PlanNode]:
        """Currently-running nodes that have at least one dependent. Useful for
        deciding whether a confidence drop should trigger a soft checkpoint."""
        running = [n for n in self.nodes.values() if n.status == NodeStatus.RUNNING]
        result = []
        for n in running:
            if any(n.id in m.depends_on for m in self.nodes.values()):
                result.append(n)
        return result

    # ── Snapshots ──────────────────────────────────────────────────────

    def snapshot(self) -> "PlanGraph":
        """Deep copy of the graph; safe to mutate without affecting the original."""
        return copy.deepcopy(self)

    def restore(self, snapshot: "PlanGraph") -> None:
        """Replace this graph's nodes with the snapshot's. intent_id stays."""
        if snapshot.intent_id != self.intent_id:
            raise ValueError(
                f"Cannot restore snapshot with intent_id {snapshot.intent_id!r} "
                f"into graph {self.intent_id!r}"
            )
        self.nodes = copy.deepcopy(snapshot.nodes)
        self.intent = snapshot.intent
        self.drop_log = list(snapshot.drop_log)
        self._touch()

    # ── Persistence helpers ────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "intent": self.intent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "drop_log": list(self.drop_log),
            "nodes": [n.to_dict() for n in self.nodes.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanGraph":
        graph = cls(
            intent_id=str(data.get("intent_id") or _new_id()),
            intent=str(data.get("intent") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            drop_log=list(data.get("drop_log") or []),
        )
        for raw in data.get("nodes", []) or []:
            node = PlanNode.from_dict(raw)
            graph.nodes[node.id] = node
        return graph

    # ── Internal ───────────────────────────────────────────────────────

    def _touch(self) -> None:
        self.updated_at = time.time()

    def _require(self, node_id: str) -> PlanNode:
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id!r} not in graph")
        return self.nodes[node_id]

    def _tree_descendants(self, node_id: str) -> set[str]:
        """All transitive children of node_id via parent_id (tree-shaped)."""
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for n in self.nodes.values():
                if n.id == current or n.id in seen:
                    continue
                if n.parent_id == current:
                    seen.add(n.id)
                    frontier.append(n.id)
        return seen


# ── Module helpers ───────────────────────────────────────────────────────


def _new_id() -> str:
    """Compact unique id (12 hex chars from a UUID4). Plenty for our scale."""
    return uuid.uuid4().hex[:12]


def new_node_id() -> str:
    """Public id factory for callers that want to construct PlanNodes."""
    return _new_id()


def _topological(nodes: Iterable[PlanNode]) -> list[PlanNode]:
    """Order nodes so parents come before children (parent_id only). Caller
    guarantees no cycles among the supplied nodes."""
    nodes = list(nodes)
    by_id = {n.id: n for n in nodes}
    visited: set[str] = set()
    out: list[PlanNode] = []

    def visit(n: PlanNode) -> None:
        if n.id in visited:
            return
        visited.add(n.id)
        if n.parent_id and n.parent_id in by_id:
            visit(by_id[n.parent_id])
        out.append(n)

    for n in nodes:
        visit(n)
    return out
