"""
Specialist registry — per-node agent specializations.

Each specialization is a thin profile: a system-prompt block, a tool allowlist,
and a step budget. The walker (see `walker.py`) picks one per node and spawns a
short-lived agent session bound to that profile.

This replaces the old fixed planner / generator / evaluator role triad. Roles
are dynamic now — one intent might spawn 12 agent instances of 5 different
specializations as the plan-graph evolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..engine.sandbox import EXEC_TOOLS, FILE_WRITE_TOOLS, READ_ONLY_TOOLS
from .plan_graph import NodeSpecialization


# ── Tool allowlists per specialization ──────────────────────────────────


# Tools that an `implement` specialist gets. We intentionally don't include
# `task` here — sub-agent recursion is the orchestrator's job, not a specialist's.
ALL_EDIT_TOOLS = READ_ONLY_TOOLS | FILE_WRITE_TOOLS | EXEC_TOOLS

# Web fetching for `research` — tighter than the full edit set, no shell.
RESEARCH_TOOLS = READ_ONLY_TOOLS | frozenset({
    "browser_navigate", "browser_click", "browser_type",
})

# Tools that `verify` is allowed to call. Reads + bash for tests, no edits.
VERIFY_TOOLS = READ_ONLY_TOOLS | frozenset({"bash"})


# ── Specialist profile ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SpecialistProfile:
    """Describes one specialization. Composed at runtime into a Session config."""
    name: str
    description: str
    system_block: str
    tool_allowlist: frozenset
    max_steps: int
    confidence_threshold: float  # below this → orchestrator spawns a verify sibling


# ── Registry ─────────────────────────────────────────────────────────────


SPECIALISTS: dict[str, SpecialistProfile] = {

    NodeSpecialization.EXPLORE: SpecialistProfile(
        name="explore",
        description="Gather context. Read files, search code, browse docs. Never edit.",
        system_block=(
            "You are an explorer. Your job is to read code and docs to build a clear "
            "picture of what currently exists, not to change anything. Use file_read, "
            "glob, grep, and browser_* read tools. Do NOT call file_write, file_edit, "
            "bash, or any state-mutating tool.\n\n"
            "Stay focused: you have a small step budget. Read the 2-4 files most "
            "relevant to the goal, then STOP and write a concrete summary. Do not "
            "explore the whole repo. The summary you produce becomes context for "
            "the next specialist — if you don't summarize, downstream work fails.\n\n"
            "End your response with a 3-6 line summary covering: relevant file "
            "paths, key function/class names, observed behavior or constraints, "
            "and anything the implementer needs to know."
        ),
        tool_allowlist=READ_ONLY_TOOLS,
        max_steps=8,
        confidence_threshold=0.7,
    ),

    NodeSpecialization.IMPLEMENT: SpecialistProfile(
        name="implement",
        description="Make targeted changes. Edit files, run scripts, install deps.",
        system_block=(
            "You are an implementer. Make the specific change described in your goal "
            "and nothing more. Don't refactor unrelated code, don't add features that "
            "weren't asked for, don't introduce abstractions for hypothetical future "
            "use. When you finish, summarize what files you touched and the diff "
            "shape — the verifier needs to know what to check."
        ),
        tool_allowlist=ALL_EDIT_TOOLS,
        max_steps=24,
        confidence_threshold=0.6,
    ),

    NodeSpecialization.VERIFY: SpecialistProfile(
        name="verify",
        description="Confirm the change works. Run tests, check output, no edits.",
        system_block=(
            "You are a verifier. The implementer says they're done. Your job is to "
            "confirm the change actually works: read what they touched, run focused "
            "tests with bash, check edge cases. You may NOT edit files. Report a "
            "concrete verdict: pass / revise (with specific failing checks) / blocked."
        ),
        tool_allowlist=VERIFY_TOOLS,
        max_steps=12,
        confidence_threshold=0.8,
    ),

    NodeSpecialization.REPAIR: SpecialistProfile(
        name="repair",
        description="Fix exactly what verify flagged. Surgical edits only.",
        system_block=(
            "You are a repairer. The verifier flagged specific failures. Fix only "
            "those. Don't add scope, don't refactor, don't make 'while-I'm-here' "
            "changes. Reproduce the failure, fix it, summarize the change. After "
            "you finish, the verifier will re-check."
        ),
        tool_allowlist=ALL_EDIT_TOOLS,
        max_steps=16,
        confidence_threshold=0.7,
    ),

    NodeSpecialization.RESEARCH: SpecialistProfile(
        name="research",
        description="External lookup — docs, web, examples — no project edits.",
        system_block=(
            "You are a researcher. Look outside the project: docs, examples on the "
            "web, library source. Don't edit project files. Return concrete findings "
            "with citations (URL or file:line). If you can't find what you need, "
            "say so explicitly."
        ),
        tool_allowlist=RESEARCH_TOOLS,
        max_steps=10,
        confidence_threshold=0.7,
    ),

    NodeSpecialization.PLAN: SpecialistProfile(
        name="plan",
        description="Decompose this subtree. Output is a JSON plan, not code or files.",
        system_block=(
            "You are a PLANNER. Your ONLY output is a JSON plan. You do NOT "
            "implement, write files, run shell commands, or edit anything — "
            "those tools are not available to you. A separate `implement` "
            "specialist will run AFTER you and execute the plan you emit.\n\n"
            "Decompose the goal into 2-6 concrete subgoals. End your response "
            "with a single fenced JSON code block in this exact shape:\n\n"
            "```json\n"
            "{\n"
            '  "subgoals": [\n'
            '    {"goal": "...", "specialization": "explore"},\n'
            '    {"goal": "...", "specialization": "implement", "depends_on": [0]},\n'
            '    {"goal": "...", "specialization": "verify", "depends_on": [1]}\n'
            "  ]\n"
            "}\n"
            "```\n\n"
            "Allowed `specialization` values: explore, implement, verify, repair, "
            "research, plan. Use `depends_on` indices to express dependencies on "
            "earlier subgoals. Even a tiny goal that could fit in one step needs "
            "to be emitted as a JSON plan with at least one subgoal — DO NOT "
            "attempt to do the work yourself. Use file_read / glob / grep first "
            "if you need to understand the codebase before planning."
        ),
        tool_allowlist=READ_ONLY_TOOLS,
        max_steps=8,
        confidence_threshold=0.6,
    ),
}


# ── Helpers ─────────────────────────────────────────────────────────────


def get_specialist(specialization: str) -> SpecialistProfile:
    """Return the profile, raising on unknown name."""
    if specialization not in SPECIALISTS:
        raise KeyError(
            f"Unknown specialization {specialization!r}; "
            f"expected one of {sorted(SPECIALISTS)}"
        )
    return SPECIALISTS[specialization]


def assemble_system_prompt(
    *,
    specialization: str,
    node_goal: str,
    intent: str,
    project_conventions: str = "",
    extra_context: str = "",
) -> str:
    """Compose the system prompt for one specialist's session.

    Order:
      1. Project conventions (AGENTS.md / RESONANT.md content) — what THIS codebase wants
      2. Specialist system block — how this kind of agent behaves
      3. Active node goal + parent intent — the immediate work
      4. Optional extra context — caller-supplied (e.g. results of prerequisite nodes)
    """
    profile = get_specialist(specialization)
    parts: list[str] = []

    if project_conventions.strip():
        parts.append("--- PROJECT CONVENTIONS ---")
        parts.append(project_conventions.strip())
        parts.append("--- END PROJECT CONVENTIONS ---")

    parts.append(f"--- SPECIALIZATION: {profile.name.upper()} ---")
    parts.append(profile.system_block)

    parts.append(f"--- ACTIVE NODE ---")
    parts.append(f"Intent: {intent}")
    parts.append(f"Goal:   {node_goal}")

    if extra_context.strip():
        parts.append("--- CONTEXT FROM PRIOR NODES ---")
        parts.append(extra_context.strip())

    return "\n".join(parts)


def filter_tools_for_specialist(specialization: str, tools: list[dict]) -> list[dict]:
    """Return only the tools this specialist is allowed to call.

    `tools` is the OpenAI function-calling format list from `engine/tools.py`.
    Tools whose names aren't in the allowlist are silently dropped.
    """
    profile = get_specialist(specialization)
    allowed = profile.tool_allowlist
    out = []
    for t in tools:
        name = t.get("function", {}).get("name", "")
        if name in allowed:
            out.append(t)
    return out
