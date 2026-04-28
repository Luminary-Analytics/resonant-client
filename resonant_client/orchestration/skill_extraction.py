"""
Auto-extract a Skill from a completed plan-graph.

The orchestrator calls this when a plan-graph reaches `is_complete()` with a
high enough overall confidence. The result is stashed in the global skills
folder with `success_count = 1`. Users can review/curate via the Skills UI
(Phase 4 surface).
"""

from __future__ import annotations

import re
import time
from typing import Optional

from .plan_graph import NodeStatus, PlanGraph, PlanNode
from .skills import DEFAULT_SCOPE, Skill, save_skill, tokenize


# Quality bar for auto-extraction. Stricter than the user's per-project
# confidence floor so we don't pollute the library with marginal runs.
MIN_OVERALL_CONFIDENCE = 0.8
MIN_NODE_COUNT = 3  # tiny one-shot graphs aren't worth saving


def is_extraction_candidate(graph: PlanGraph) -> bool:
    """Decide whether a completed graph is worth distilling into a skill."""
    if not graph.is_complete():
        return False
    nodes = list(graph.nodes.values())
    if len(nodes) < MIN_NODE_COUNT:
        return False
    # All nodes must be in DONE (not abandoned/blocked). Mixed graphs aren't
    # representative of a successful procedure.
    if not all(n.status == NodeStatus.DONE for n in nodes):
        return False
    overall = sum(n.confidence for n in nodes) / len(nodes)
    return overall >= MIN_OVERALL_CONFIDENCE


def slugify(text: str, *, max_len: int = 60) -> str:
    """Produce a kebab-case slug from a free-form intent string."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]+", "", text).strip().lower()
    cleaned = re.sub(r"[\s-]+", "-", cleaned)
    return cleaned[:max_len].strip("-") or "skill"


def extract_skill(
    graph: PlanGraph,
    *,
    scope: str = DEFAULT_SCOPE,
    project_path: Optional[str] = None,
    stack_sig: Optional[str] = None,
    skill_id_override: Optional[str] = None,
) -> Optional[Skill]:
    """Distill a completed plan-graph into a Skill and persist it.

    Returns the saved Skill, or None when the graph isn't a candidate.
    """
    if not is_extraction_candidate(graph):
        return None

    skill_id = skill_id_override or _derive_skill_id(graph)
    name = _derive_name(graph)
    description = _derive_description(graph)
    triggers = _derive_triggers(graph)
    procedure_steps = _strip_to_procedure(graph)

    # Token bag for similarity matching: intent + every node's goal
    tokens = tokenize(graph.intent)
    for node in graph.nodes.values():
        tokens.extend(tokenize(node.goal))

    skill = Skill(
        id=skill_id,
        name=name,
        description=description,
        scope=scope,
        triggers=triggers,
        prerequisites=[],
        success_count=1,
        fail_count=0,
        last_used_at=time.time(),
        version="1.0.0",
        tokens=sorted(set(tokens)),
        procedure_steps=procedure_steps,
    )

    procedure_md = _build_procedure_md(graph, name=name, description=description)
    verification_md = _build_verification_md(graph)

    save_skill(
        skill,
        procedure_md=procedure_md,
        verification_md=verification_md,
        project_path=project_path,
        stack_sig=stack_sig,
    )
    return skill


# ── Internals ───────────────────────────────────────────────────────────


def _derive_skill_id(graph: PlanGraph) -> str:
    return slugify(graph.intent or "untitled-skill")


def _derive_name(graph: PlanGraph) -> str:
    intent = (graph.intent or "").strip()
    # Title-case first 80 chars of the intent
    return (intent[:80] + ("…" if len(intent) > 80 else "")) or "Skill"


def _derive_description(graph: PlanGraph) -> str:
    """One-sentence summary built from the intent + leaf-node count."""
    intent = (graph.intent or "").strip().rstrip(".")
    leaves = sum(
        1 for n in graph.nodes.values()
        if not any(m.parent_id == n.id for m in graph.nodes.values())
    )
    return f"{intent} (procedure with {leaves} leaf step(s))"


def _derive_triggers(graph: PlanGraph) -> list[str]:
    """A few short trigger phrases for similarity matching."""
    intent = (graph.intent or "").strip()
    triggers: list[str] = []
    if intent:
        triggers.append(intent)
    # Add the goal of the first IMPLEMENT node (often a clean handle)
    for n in sorted(graph.nodes.values(), key=lambda x: x.created_at):
        if n.specialization == "implement" and n.goal:
            triggers.append(n.goal)
            break
    return triggers


def _strip_to_procedure(graph: PlanGraph) -> list[dict]:
    """Reduce the graph to a portable list of {goal, specialization, depends_on_idx}.

    We translate node-id deps into positional indexes so the skill can be loaded
    into a fresh graph with new ids.
    """
    nodes_in_order = sorted(graph.nodes.values(), key=lambda x: x.created_at)
    id_to_idx = {n.id: i for i, n in enumerate(nodes_in_order)}
    out: list[dict] = []
    for n in nodes_in_order:
        out.append({
            "goal": n.goal,
            "specialization": n.specialization,
            "parent_idx": id_to_idx.get(n.parent_id) if n.parent_id else None,
            "depends_on_idx": [id_to_idx[d] for d in n.depends_on if d in id_to_idx],
        })
    return out


def _build_procedure_md(graph: PlanGraph, *, name: str, description: str) -> str:
    """Human-readable procedure: an outline of the nodes that ran."""
    lines: list[str] = [f"# {name}", "", description, ""]
    nodes_in_order = sorted(graph.nodes.values(), key=lambda x: x.created_at)
    for i, n in enumerate(nodes_in_order):
        lines.append(f"## Step {i + 1}: {n.goal}")
        lines.append(f"- Specialization: `{n.specialization}`")
        if n.depends_on:
            depnames = [graph.nodes[d].goal for d in n.depends_on if d in graph.nodes]
            lines.append(f"- Depends on: {', '.join(depnames)}")
        if n.confidence < 1.0:
            lines.append(f"- Final confidence: {n.confidence:.2f}")
        if isinstance(n.result, dict) and n.result.get("summary"):
            lines.append(f"- Outcome: {n.result['summary']}")
        lines.append("")
    return "\n".join(lines)


def _build_verification_md(graph: PlanGraph) -> str:
    """Capture the verification context: which nodes were `verify` and what they found."""
    verify_nodes = [n for n in graph.nodes.values() if n.specialization == "verify"]
    if not verify_nodes:
        return "# Verification\n\n_No explicit verify nodes; success was inferred from confidence._\n"
    lines: list[str] = ["# Verification", ""]
    for n in verify_nodes:
        lines.append(f"## {n.goal}")
        if isinstance(n.result, dict):
            verdict = n.result.get("verdict") or "pass"
            lines.append(f"- Verdict: **{verdict}**")
            if n.result.get("summary"):
                lines.append(f"- Notes: {n.result['summary']}")
        lines.append("")
    return "\n".join(lines)
