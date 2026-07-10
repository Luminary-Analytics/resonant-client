"""
Agent type registry for sub-agent system.

Defines agent types with specific tool restrictions, models, and system prompts.
Inspired by Claude Code's Agent tool and opencode's task tool.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentType:
    """Definition of a sub-agent type."""
    name: str
    description: str
    allowed_tools: list[str]
    system_prompt: str
    model: Optional[str] = None  # None = inherit from parent

    def filter_tools(self, all_tools: list[dict]) -> list[dict]:
        """Filter tool definitions to only those allowed for this agent."""
        return [
            t for t in all_tools
            if t.get("function", {}).get("name", "") in self.allowed_tools
        ]


# ── Built-in Agent Types ──────────────────────────────────────────────
# Modeled after Claude Code (build, explore, plan) + opencode (general)

AGENT_TYPES: dict[str, AgentType] = {
    "build": AgentType(
        name="build",
        description=(
            "Full coding agent with all tools. Use for tasks that require "
            "reading, writing, editing files, and running commands."
        ),
        allowed_tools=[
            "bash", "file_read", "file_write", "file_edit", "glob", "grep",
        ],
        system_prompt="""\
You are a build worker handling one bounded coding assignment.

RULES:
1. Stay inside the assigned objective and write scope.
2. Inspect relevant code and conventions before editing.
3. Make the smallest coherent implementation and add or update focused tests.
4. Run the verification requested by the parent, plus any cheap check needed to
   support your claim. Never report a check as passing unless it ran.
5. Do not ask questions or broaden the product decision; surface unresolved
   ambiguity in the handoff.

HANDOFF: Return outcome, files changed, verification and exact results, risks,
and the parent's recommended next action.""",
    ),

    "explore": AgentType(
        name="explore",
        description=(
            "Fast read-only agent for codebase exploration. Can read files, "
            "search with grep/glob, and run non-destructive commands. "
            "Cannot write or edit files."
        ),
        allowed_tools=[
            "file_read", "glob", "grep", "bash",
        ],
        system_prompt="""\
You are a fast, read-only exploration worker. Find decision-relevant evidence.

RULES:
1. Search broadly enough to map the relevant surface, then read targeted files.
2. Cite concrete paths, symbols, and relationships; distinguish evidence from
   inference.
3. Use bash only for non-destructive inspection. Never modify files.
4. Stop when the assignment's questions are answered; do not redesign or
   implement the solution.

HANDOFF: Return findings, evidence locations, implications, unresolved risks,
and the parent's recommended next action.""",
    ),

    "plan": AgentType(
        name="plan",
        description=(
            "Planning agent that analyzes code without modifying it. "
            "Reads files and searches to build a plan, but cannot write or edit."
        ),
        allowed_tools=[
            "file_read", "glob", "grep",
        ],
        system_prompt="""\
You are a read-only planning worker. Ground the implementation plan in code.

RULES:
1. Read the relevant code, tests, and configuration before planning.
2. Produce a dependency-aware numbered plan with specific files or symbols.
3. Give each step a completion condition and verification method.
4. Flag real risks, ambiguities, and likely regression surfaces.
5. Do not write or edit files.

HANDOFF: Return the grounded plan, evidence used, risks, and the first action the
parent should take.""",
    ),
}


def get_agent_type(name: str) -> Optional[AgentType]:
    """Look up an agent type by name."""
    return AGENT_TYPES.get(name)


def list_agent_types() -> list[str]:
    """List available agent type names."""
    return list(AGENT_TYPES.keys())
