"""
Digital Org Chart — Agent Hierarchy System.

Defines a tree of agent roles with parent-child relationships.
Each node has a role, description, model, and knows its place
in the hierarchy. Used by the Command Center to coordinate
multi-agent workflows with explicit delegation chains.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class OrgNode:
    """A single node in the org chart representing an agent role."""

    id: str = ""
    role: str = ""                      # "Project Coordinator", "Backend Dev", etc.
    description: str = ""               # What this agent does
    model: str = ""                     # backend:model (e.g., "claude-code:opus")
    parent_id: Optional[str] = None     # None = reports to user (root)
    children_ids: list[str] = field(default_factory=list)
    status: str = "idle"                # idle | running | completed | failed
    agent_task_id: str = ""             # BackgroundTask ID when active

    def __post_init__(self):
        if not self.id:
            self.id = f"node_{uuid.uuid4().hex[:10]}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OrgNode":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class OrgChart:
    """
    A hierarchical org chart of agent roles.

    The user is always implicitly at the top. Root nodes (parent_id=None)
    report directly to the user.
    """

    def __init__(self, nodes: Optional[list[OrgNode]] = None):
        self._nodes: dict[str, OrgNode] = {}
        for node in (nodes or []):
            self._nodes[node.id] = node

    def add_node(
        self,
        role: str,
        description: str = "",
        model: str = "",
        parent_id: Optional[str] = None,
    ) -> OrgNode:
        """Add a new node to the chart."""
        node = OrgNode(role=role, description=description, model=model, parent_id=parent_id)
        self._nodes[node.id] = node

        # Update parent's children list
        if parent_id and parent_id in self._nodes:
            parent = self._nodes[parent_id]
            if node.id not in parent.children_ids:
                parent.children_ids.append(node.id)

        return node

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and reparent its children to its parent."""
        node = self._nodes.get(node_id)
        if not node:
            return False

        # Reparent children to removed node's parent
        for child_id in node.children_ids:
            child = self._nodes.get(child_id)
            if child:
                child.parent_id = node.parent_id
                if node.parent_id and node.parent_id in self._nodes:
                    parent = self._nodes[node.parent_id]
                    if child_id not in parent.children_ids:
                        parent.children_ids.append(child_id)

        # Remove from parent's children list
        if node.parent_id and node.parent_id in self._nodes:
            parent = self._nodes[node.parent_id]
            parent.children_ids = [cid for cid in parent.children_ids if cid != node_id]

        del self._nodes[node_id]
        return True

    def update_node(self, node_id: str, **kwargs) -> Optional[OrgNode]:
        """Update node fields."""
        node = self._nodes.get(node_id)
        if not node:
            return None

        old_parent = node.parent_id
        for key, value in kwargs.items():
            if hasattr(node, key) and key != "id":
                setattr(node, key, value)

        # Handle parent change
        new_parent = kwargs.get("parent_id")
        if new_parent is not None and new_parent != old_parent:
            # Remove from old parent
            if old_parent and old_parent in self._nodes:
                old = self._nodes[old_parent]
                old.children_ids = [cid for cid in old.children_ids if cid != node_id]
            # Add to new parent
            if new_parent and new_parent in self._nodes:
                new = self._nodes[new_parent]
                if node_id not in new.children_ids:
                    new.children_ids.append(node_id)

        return node

    def get_node(self, node_id: str) -> Optional[OrgNode]:
        return self._nodes.get(node_id)

    def get_roots(self) -> list[OrgNode]:
        """Get root nodes (report directly to user)."""
        return [n for n in self._nodes.values() if not n.parent_id]

    def get_children(self, node_id: str) -> list[OrgNode]:
        """Get direct children of a node."""
        node = self._nodes.get(node_id)
        if not node:
            return []
        return [self._nodes[cid] for cid in node.children_ids if cid in self._nodes]

    def get_tree(self) -> list[dict]:
        """Build nested tree structure for frontend rendering."""
        def _build(node: OrgNode) -> dict:
            return {
                **node.to_dict(),
                "children": [_build(self._nodes[cid]) for cid in node.children_ids if cid in self._nodes],
            }
        return [_build(root) for root in self.get_roots()]

    def get_node_context(self, node_id: str, project_name: str = "") -> str:
        """Generate hierarchy-aware context for an agent's system prompt."""
        node = self._nodes.get(node_id)
        if not node:
            return ""

        lines = [
            f"## Your Position in the Organization",
            f"Role: {node.role}",
        ]
        if node.description:
            lines.append(f"Description: {node.description}")

        # Who you report to
        if node.parent_id and node.parent_id in self._nodes:
            parent = self._nodes[node.parent_id]
            lines.append(f"Reports to: {parent.role} (they coordinate your work)")
        else:
            lines.append(f"Reports to: The user (you are the top-level coordinator)")

        # Who reports to you
        children = self.get_children(node_id)
        if children:
            lines.append(f"\nDirect reports:")
            for child in children:
                desc = f": {child.description}" if child.description else ""
                lines.append(f"  - {child.role}{desc}")
            lines.append(f"\nYou can delegate tasks to your reports using the spawn_worker command.")
            lines.append(f"Monitor their progress with check_workers.")

        lines.append(f"\n## Communication Protocol")
        lines.append(f"- Report progress and findings to your supervisor clearly")
        if children:
            lines.append(f"- When delegating to reports, provide clear objectives and acceptance criteria")
        lines.append(f"- Escalate blockers to your supervisor")

        return "\n".join(lines)

    def get_all_nodes(self) -> list[OrgNode]:
        return list(self._nodes.values())

    def to_dict(self) -> list[dict]:
        return [n.to_dict() for n in self._nodes.values()]

    @classmethod
    def from_dict(cls, data: list[dict]) -> "OrgChart":
        nodes = [OrgNode.from_dict(d) for d in data]
        return cls(nodes)
